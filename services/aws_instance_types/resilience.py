"""Shared refresh outcomes and retry policy. Call while holding the cache lock."""
import contextvars
import functools
import inspect
import random
import time

from .common import read_json, write_json_atomic

_force = contextvars.ContextVar('catalog_force_refresh', default=False)


def manual_refresh(fn):
    @functools.wraps(fn)
    def wrapped(*args, **kwargs):
        token = _force.set(True)
        try:
            return fn(*args, **kwargs)
        finally:
            _force.reset(token)
    return wrapped


def refresh_status(path):
    return read_json(path + '.status.json') or {}


def retry_allowed(path):
    return _force.get() or time.time() >= refresh_status(path).get('next_retry_at', 0)


def record_result(path, result):
    previous = refresh_status(path)
    now = time.time()
    success = bool(result.get('ok'))
    failures = 0 if success else min(int(previous.get('failures', 0)) + 1, 8)
    delay = 0 if success else min(3600, 30 * 2 ** (failures - 1)) * random.uniform(0.8, 1.2)
    error = None if success else str(result.get('error') or 'Refresh failed')
    write_json_atomic(path + '.status.json', {
        'last_attempt_at': now,
        'last_success_at': now if success else previous.get('last_success_at'),
        'failures': failures, 'next_retry_at': now + delay,
        'error': error,
    })


def catalog_refresh(fn):
    signature = inspect.signature(fn)
    @functools.wraps(fn)
    def wrapped(*args, **kwargs):
        path = signature.bind(*args, **kwargs).arguments['cache_path']
        if not retry_allowed(path):
            return {'ok': False, 'skipped': True, 'cache_path': path,
                    'error': refresh_status(path).get('error'),
                    'next_retry_at': refresh_status(path).get('next_retry_at')}
        try:
            result = fn(*args, **kwargs)
        except Exception as exc:
            result = {'ok': False, 'error': str(exc), 'cache_path': path}
        try:
            record_result(path, result)
        except OSError:
            # Cache permissions must not break the wizard's fallback path.
            pass
        return result
    return wrapped
