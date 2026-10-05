"""Warm immutable module documentation for every catalog owner once per day."""
import json
import logging
import os
import threading
import time

from . import documentation, store


logger = logging.getLogger(__name__)
_STARTED = False
_START_LOCK = threading.Lock()
_REFRESH_NAME = 'module-documentation'


def _truthy(value):
    return str(value or '').strip().lower() in ('1', 'true', 'yes', 'on')


def _seconds(name, default, minimum=1):
    try:
        return max(minimum, int(os.getenv(name, default)))
    except (TypeError, ValueError):
        return default


def _interval_seconds():
    return _seconds('PRAXIS_MODULE_DOC_CACHE_REFRESH_SECONDS', 24 * 60 * 60, 60)


def _check_seconds():
    return _seconds('PRAXIS_MODULE_DOC_CACHE_CHECK_SECONDS', 60 * 60, 60)


def _lease_seconds():
    return _seconds('PRAXIS_MODULE_DOC_CACHE_LEASE_SECONDS', 60 * 60, 60)


def _modules():
    with store.connection() as db:
        rows = db.execute(
            'SELECT * FROM tf_objects WHERE kind=? ORDER BY owner,name', ('module',)
        ).fetchall()
    return [store.decode(row) for row in rows]


def _refresh_table(db):
    db.execute('''CREATE TABLE IF NOT EXISTS tf_module_cache_refresh (
        name TEXT PRIMARY KEY, last_started REAL NOT NULL,
        last_completed REAL NOT NULL, lease_until REAL NOT NULL,
        result TEXT NOT NULL)''')
    db.execute(
        'INSERT OR IGNORE INTO tf_module_cache_refresh VALUES(?,?,?,?,?)',
        (_REFRESH_NAME, 0, 0, 0, '{}'),
    )


def _claim(now, interval, lease):
    with store.connection() as db:
        _refresh_table(db)
        changed = db.execute('''UPDATE tf_module_cache_refresh
            SET last_started=?,lease_until=? WHERE name=?
            AND lease_until<=? AND last_completed<=?''',
            (now, now + lease, _REFRESH_NAME, now, now - interval),
        ).rowcount
    return bool(changed)


def _complete(now, result):
    with store.connection() as db:
        _refresh_table(db)
        db.execute('''UPDATE tf_module_cache_refresh
            SET last_completed=?,lease_until=0,result=? WHERE name=?''',
            (now, json.dumps(result), _REFRESH_NAME),
        )


def _abandon():
    with store.connection() as db:
        _refresh_table(db)
        db.execute(
            'UPDATE tf_module_cache_refresh SET lease_until=0 WHERE name=?',
            (_REFRESH_NAME,),
        )


def refresh_all():
    modules = _modules()
    result = {'modules': len(modules), 'warmed': 0, 'without_releases': 0, 'failed': 0}
    for item in modules:
        try:
            docs = documentation.load(item)
            if docs['selected']:
                result['warmed'] += 1
            else:
                result['without_releases'] += 1
        except Exception:
            result['failed'] += 1
            logger.exception(
                'Module documentation cache refresh failed owner=%s module=%s',
                item['owner'], item['name'],
            )
    return result


def refresh_if_due(now=None):
    started = time.time() if now is None else now
    if not _claim(started, _interval_seconds(), _lease_seconds()):
        return None
    try:
        result = refresh_all()
    except Exception:
        _abandon()
        raise
    _complete(time.time() if now is None else now, result)
    logger.info('Module documentation cache refresh completed result=%s', result)
    return result


def _run(app, stop_event=None):
    stop = stop_event or threading.Event()
    stop.wait(_seconds('PRAXIS_MODULE_DOC_CACHE_START_DELAY_SECONDS', 5))
    while not stop.is_set():
        try:
            with app.app_context():
                refresh_if_due()
        except Exception:
            logger.exception('Module documentation cache refresh crashed')
        stop.wait(_check_seconds())


def start_documentation_cache_refresher(app):
    global _STARTED
    if not _truthy(os.getenv('PRAXIS_MODULE_DOC_CACHE_REFRESH_ENABLED', '1')):
        return False
    with _START_LOCK:
        if _STARTED:
            return False
        _STARTED = True
        thread = threading.Thread(
            target=_run,
            args=(app,),
            name='module-documentation-cache-refresher',
            daemon=True,
        )
        thread.start()
    return True
