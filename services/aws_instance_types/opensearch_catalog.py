"""Region/version-scoped OpenSearch catalogs using the shared disk cache."""
import hashlib
import logging
import time

import boto3
from botocore.config import Config

from .common import (AwsCatalogMeta, BackgroundRefreshRegistry, acquire_lock, cache_dir, cache_paths,
                     is_stale, normalize_region, read_json, release_lock, ttl_seconds,
                     write_json_atomic)

from .resilience import retry_allowed, record_result

logger = logging.getLogger(__name__)


def _paths(region, version, instance_type):
    key = hashlib.sha256(f'{version}:{instance_type}'.encode()).hexdigest()[:24]
    return cache_paths(cache_dir_path=cache_dir('PS_OPENSEARCH_CATALOG_CACHE_DIR'),
                       cache_stem=f'opensearch_{key}', region=region)


def _fetch(region, version, instance_type):
    client = boto3.session.Session(region_name=region).client(
        'opensearch', config=Config(connect_timeout=5, read_timeout=20,
                                   retries={'max_attempts': 2, 'mode': 'standard'}))
    args = {'EngineVersion': f'OpenSearch_{version}'}
    if instance_type:
        return client.describe_instance_type_limits(
            **args, InstanceType=instance_type).get('LimitsByRole', {})
    rows = []
    while True:
        response = client.list_instance_type_details(**args, MaxResults=100)
        rows.extend(response.get('InstanceTypeDetails', []))
        if not response.get('NextToken'):
            return rows
        args['NextToken'] = response['NextToken']


def get_catalog(region, version, instance_type='', *, force=False, refresh_async=True, wait=False):
    region = normalize_region(region)
    path, lock_path = _paths(region, version, instance_type)
    payload = read_json(path) or {}
    ttl = ttl_seconds('PS_OPENSEARCH_CATALOG_TTL_SECONDS')

    def refresh():
        lock = acquire_lock(lock_path, blocking=force or wait)
        if lock is None:
            return
        try:
            if not force and not retry_allowed(path):
                return
            existing = read_json(path) or {}
            if not force and not is_stale(existing.get('generated_at'), ttl):
                return
            data = _fetch(region, version, instance_type)
            if not data:
                raise ValueError('AWS returned an empty OpenSearch catalog')
            write_json_atomic(path, {'schema': 1, 'generated_at': time.time(), 'data': data,
                                     'region': region, 'version': version, 'instance_type': instance_type})
            record_result(path, {'ok': True})
        except Exception as exc:
            code = getattr(exc, 'response', {}).get('Error', {}).get('Code', type(exc).__name__)
            try:
                record_result(path, {'ok': False, 'error': code})
                status = read_json(path + '.status.json') or {}
                status.update(region=region, version=version, instance_type=instance_type)
                write_json_atomic(path + '.status.json', status)
            except OSError:
                pass
            logger.warning('OpenSearch catalog refresh failed for %s / %s', region, version, exc_info=True)
        finally:
            release_lock(lock)

    if force or wait:
        refresh()
        payload = read_json(path) or {}
    elif refresh_async and is_stale(payload.get('generated_at'), ttl):
        BackgroundRefreshRegistry.spawn(f'opensearch:{region}:{version}:{instance_type}', refresh)
    return payload.get('data'), is_stale(payload.get('generated_at'), ttl)


def catalog_meta(region, version, instance_type=''):
    region = normalize_region(region)
    path, _ = _paths(region, version, instance_type)
    payload = read_json(path) or {}
    return AwsCatalogMeta(
        catalog='opensearch', region=region, cache_path=path,
        generated_at=payload.get('generated_at'),
        stale=is_stale(payload.get('generated_at'), ttl_seconds('PS_OPENSEARCH_CATALOG_TTL_SECONDS')),
        count=len(payload.get('data') or []),
        source='aws_cache' if payload.get('data') else 'fallback',
        error=(read_json(path + '.status.json') or {}).get('error'),
    )


def refresh_opensearch_catalog(region, *, force=False):
    region = normalize_region(region)
    results = []
    for entry in cache_entries([region]):
        if entry['region'] != region:
            continue
        version, _, instance_type = entry['engine'].partition('/')
        meta = catalog_meta(region, version, instance_type)
        if force or (meta.stale and retry_allowed(meta.cache_path)):
            get_catalog(region, version, instance_type, force=force, refresh_async=False, wait=True)
        meta = catalog_meta(region, version, instance_type)
        results.append({'ok': meta.source == 'aws_cache' and not meta.error,
                        'engine': entry['engine'], 'error': meta.error})
    meta = catalog_meta(region, '3.3')
    return {'ok': all(result['ok'] for result in results), 'results': results,
            'generated_at': meta.generated_at, 'error': next((r['error'] for r in results if r['error']), None),
            'cache_path': meta.cache_path}


def refresh_catalog_entry(region, engine='3.3', *, limits=False):
    import re
    version, _, instance_type = (engine or '3.3').partition('/')
    if not re.fullmatch(r'[0-9]+\.[0-9]+', version) or (limits and not re.fullmatch(r'[a-z0-9]+\.[a-z0-9]+\.search', instance_type)):
        return {'ok': False, 'error': 'Invalid OpenSearch version or instance type'}
    instance_type = instance_type if limits else ''
    get_catalog(region, version, instance_type, force=True, refresh_async=False)
    meta = catalog_meta(region, version, instance_type)
    return {'ok': meta.source == 'aws_cache' and not meta.error,
            'error': meta.error, 'cache_path': meta.cache_path, 'generated_at': meta.generated_at}


def cache_entries(regions):
    """Discover all version/type caches, including failures before the first success."""
    from pathlib import Path
    known = {(region, '3.3', kind) for region in regions
             for kind in ('', 'r7g.large.search', 'm7g.medium.search')}
    for path in Path(cache_dir('PS_OPENSEARCH_CATALOG_CACHE_DIR')).glob('opensearch_*.json'):
        payload = read_json(str(path)) or {}
        if payload.get('region') and payload.get('version'):
            known.add((payload['region'], payload['version'], payload.get('instance_type', '')))
    return [{'catalog': 'opensearch_limits' if kind else 'opensearch',
             'region': region, 'engine': version + ('/' + kind if kind else ''),
             'engine_safe': None, 'cache_path': _paths(region, version, kind)[0]}
            for region, version, kind in sorted(known)]
