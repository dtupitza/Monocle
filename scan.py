#!/usr/bin/env python3

import monocle.sanitized as conf

import asyncio
try:
    if conf.UVLOOP:
        from uvloop import EventLoopPolicy
        asyncio.set_event_loop_policy(EventLoopPolicy())
except ImportError:
    pass

from multiprocessing.managers import BaseManager, DictProxy
from queue import Queue, Full
from argparse import ArgumentParser
from signal import signal, SIGINT, SIGTERM, SIG_IGN
from logging import getLogger, basicConfig, WARNING, INFO
from logging.handlers import RotatingFileHandler
from os.path import exists, join
from sys import platform
from concurrent.futures import TimeoutError

import time

# Check whether config has all necessary attributes
_required = (
    'DB_ENGINE',
    'GRID',
    'MAP_START',
    'MAP_END'
)
for setting_name in _required:
    if not hasattr(conf, setting_name):
        raise AttributeError('Please set "{}" in config'.format(setting_name))
# Set defaults for missing config options
_optional = {
    'PROXIES': None,
    'NOTIFY_IDS': None,
    'NOTIFY_RANKING': None,
    'CONTROL_SOCKS': None,
    'HASH_KEY': None,
    'SMART_THROTTLE': False,
    'MAX_CAPTCHAS': 0,
    'ACCOUNTS': (),
    'ENCOUNTER': None,
    'NOTIFY': False,
    'AUTHKEY': b'm3wtw0',
    'SPIN_POKESTOPS': False,
    'GET_GYM_DETAILS': False,
    'SPIN_COOLDOWN': 300,
    'GYM_COOLDOWN': 180,
    'COMPLETE_TUTORIAL': False,
    'INCUBATE_EGGS': False,
    'MAP_WORKERS': True,
    'APP_SIMULATION': True,
    'ITEM_LIMITS': None,
    'MAX_RETRIES': 3,
    'MORE_POINTS': True,
    'GIVE_UP_KNOWN': 75,
    'GIVE_UP_UNKNOWN': 60,
    'SKIP_SPAWN': 90,
    'LOGIN_TIMEOUT': 2.5,
    'PLAYER_LOCALE': {'country': 'US', 'language': 'en', 'timezone': 'America/Denver'},
    'CAPTCHA_KEY': None,
    'CAPTCHAS_ALLOWED': 3,
    'DIRECTORY': None,
    'FORCED_KILL': None,
    'SWAP_WORST': 600,
    'REFRESH_RATE': 0.6,
    'SPEED_LIMIT': 19.5,
    'COROUTINES_LIMIT': None,
    'GOOD_ENOUGH': None,
    'SEARCH_SLEEP': 2.5,
    'STAT_REFRESH': 5,
    'FAVOR_CAPTCHA': True
}
for setting_name, default in _optional.items():
    if not hasattr(conf, setting_name):
        setattr(conf, setting_name, default)
del (_optional, _required)

# validate PROXIES input and cast to set if needed
if conf.PROXIES:
    if isinstance(conf.PROXIES, (tuple, list)):
        conf.PROXIES = set(conf.PROXIES)
    elif isinstance(conf.PROXIES, str):
        conf.PROXIES = {conf.PROXIES}
    elif not isinstance(conf.PROXIES, set):
        raise ValueError('PROXIES must be either a list, set, tuple, or str.')

# ensure that user's latitudes and longitudes are different
if (conf.MAP_START[0] == conf.MAP_END[0]
        or conf.MAP_START[1] == conf.MAP_END[1]):
    raise ValueError('The latitudes and longitudes of your MAP_START and MAP_END must differ.')

# disable bag cleaning if not spinning PokéStops
if conf.ITEM_LIMITS and not conf.SPIN_POKESTOPS:
    conf.ITEM_LIMITS = None

# ensure that numbers are valid
try:
    if conf.SCAN_DELAY < 10:
        raise ValueError('SCAN_DELAY must be at least 10.')
except (TypeError, AttributeError):
    conf.SCAN_DELAY = 10
try:
    if conf.SIMULTANEOUS_LOGINS < 1:
        raise ValueError('SIMULTANEOUS_LOGINS must be at least 1.')
except (TypeError, AttributeError):
    conf.SIMULTANEOUS_LOGINS = 4
try:
    if conf.SIMULTANEOUS_SIMULATION < 1:
        raise ValueError('SIMULTANEOUS_SIMULATION must be at least 1.')
except (TypeError, AttributeError):
    conf.SIMULTANEOUS_SIMULATION = conf.SIMULTANEOUS_LOGINS

if conf.ENCOUNTER not in (None, 'notifying', 'all'):
    raise ValueError("Valid ENCOUNTER settings are: None, 'notifying', and 'all'")

if conf.DIRECTORY is None:
    if exists(join('..', 'pickles')):
        conf.DIRECTORY = '..'
    else:
        conf.DIRECTORY = ''

if conf.FORCED_KILL is True:
    conf.FORCED_KILL = ('0.57.2', '0.55.0', '0.53.0', '0.53.1', '0.53.2')

if not conf.COROUTINES_LIMIT:
    conf.COROUTINES_LIMIT = conf.GRID[0] * conf.GRID[1]
from sqlalchemy.exc import DBAPIError
from aiopogo import close_sessions, activate_hash_server

from monocle.shared import LOOP, get_logger, SessionManager, ACCOUNTS
from monocle.utils import get_address, dump_pickle
from monocle.worker import Worker
from monocle.overseer import Overseer
from monocle.db_proc import DB_PROC
from monocle.db import FORT_CACHE
from monocle.spawns import SPAWNS


class AccountManager(BaseManager):
    pass


class CustomQueue(Queue):
    def full_wait(self, maxsize=0, timeout=None):
        '''Block until queue size falls below maxsize'''
        starttime = time.monotonic()
        with self.not_full:
            if maxsize > 0:
                if timeout is None:
                    while self._qsize() >= maxsize:
                        self.not_full.wait()
                elif timeout < 0:
                    raise ValueError("'timeout' must be a non-negative number")
                else:
                    endtime = time.monotonic() + timeout
                    while self._qsize() >= maxsize:
                        remaining = endtime - time.monotonic()
                        if remaining <= 0.0:
                            raise Full
                        self.not_full.wait(remaining)
            self.not_empty.notify()
        endtime = time.monotonic()
        return endtime - starttime


_captcha_queue = CustomQueue()
_extra_queue = Queue()
_worker_dict = {}

def get_captchas():
    return _captcha_queue

def get_extras():
    return _extra_queue

def get_workers():
    return _worker_dict

def mgr_init():
    signal(SIGINT, SIG_IGN)


def parse_args():
    parser = ArgumentParser()
    parser.add_argument(
        '--no-status-bar',
        dest='status_bar',
        help='Log to console instead of displaying status bar',
        action='store_false'
    )
    parser.add_argument(
        '--log-level',
        choices=['DEBUG', 'INFO', 'WARNING', 'ERROR'],
        default=WARNING
    )
    parser.add_argument(
        '--bootstrap',
        dest='bootstrap',
        help='Bootstrap even if spawns are known.',
        action='store_true'
    )
    parser.add_argument(
        '--no-pickle',
        dest='pickle',
        help='Do not load spawns from pickle',
        action='store_false'
    )
    return parser.parse_args()


def configure_logger(filename='scan.log'):
    if filename:
        handlers = (RotatingFileHandler(filename, maxBytes=500000, backupCount=4),)
    else:
        handlers = None
    basicConfig(
        format='[{asctime}][{levelname:>8s}][{name}] {message}',
        datefmt='%Y-%m-%d %X',
        style='{',
        level=INFO,
        handlers=handlers
    )


def exception_handler(loop, context):
    try:
        log = getLogger('eventloop')
        log.error('A wild exception appeared!')
        log.error(context)
    except Exception:
        print('Exception in exception handler.')


def cleanup(overseer, manager):
    try:
        overseer.running = False
        print('Exiting, please wait until all tasks finish')

        log = get_logger('cleanup')
        print('Finishing tasks...')

        LOOP.create_task(overseer.exit_progress())
        pending = asyncio.Task.all_tasks(loop=LOOP)
        gathered = asyncio.gather(*pending, return_exceptions=True)
        try:
            LOOP.run_until_complete(asyncio.wait_for(gathered, 40))
        except TimeoutError as e:
            print('Coroutine completion timed out, moving on.')
        except Exception as e:
            log = get_logger('cleanup')
            log.exception('A wild {} appeared during exit!', e.__class__.__name__)

        overseer.refresh_dict()

        print('Dumping pickles...')
        dump_pickle('accounts', ACCOUNTS)
        FORT_CACHE.pickle()
        if conf.CACHE_CELLS:
            dump_pickle('cells', Worker.cell_ids)

        DB_PROC.stop()
        print("Updating spawns pickle...")
        try:
            SPAWNS.update()
        except Exception as e:
            log.warning('A wild {} appeared while updating spawns during exit!', e.__class__.__name__)
        while not DB_PROC.queue.empty():
            pending = DB_PROC.queue.qsize()
            # Spaces at the end are important, as they clear previously printed
            # output - \r doesn't clean whole line
            print('{} DB items pending     '.format(pending), end='\r')
            time.sleep(.5)
    finally:
        print('Closing pipes, sessions, and event loop...')
        manager.shutdown()
        SessionManager.close()
        close_sessions()
        LOOP.close()
        print('Done.')


def main():
    args = parse_args()
    log = get_logger()
    if args.status_bar:
        configure_logger(filename=join(conf.DIRECTORY, 'scan.log'))
        log.info('-' * 37)
        log.info('Starting up!')
    else:
        configure_logger(filename=None)
    log.setLevel(args.log_level)

    AccountManager.register('captcha_queue', callable=get_captchas)
    AccountManager.register('extra_queue', callable=get_extras)
    if conf.MAP_WORKERS:
        AccountManager.register('worker_dict', callable=get_workers,
                                proxytype=DictProxy)
    address = get_address()
    manager = AccountManager(address=address, authkey=conf.AUTHKEY)
    try:
        manager.start(mgr_init)
    except (OSError, EOFError) as e:
        if platform == 'win32' or not isinstance(address, str):
            raise OSError('Another instance is running with the same manager address. Stop that process or change your MANAGER_ADDRESS.') from e
        else:
            raise OSError('Another instance is running with the same socket. Stop that process or: rm {}'.format(address)) from e

    LOOP.set_exception_handler(exception_handler)

    overseer = Overseer(manager)
    overseer.start(args.status_bar)
    launcher = LOOP.create_task(overseer.launch(args.bootstrap, args.pickle))
    activate_hash_server(conf.HASH_KEY)
    if platform != 'win32':
        LOOP.add_signal_handler(SIGINT, launcher.cancel)
        LOOP.add_signal_handler(SIGTERM, launcher.cancel)
    try:
        LOOP.run_until_complete(launcher)
    except (KeyboardInterrupt, SystemExit):
        launcher.cancel()
    finally:
        cleanup(overseer, manager)


if __name__ == '__main__':
    main()
