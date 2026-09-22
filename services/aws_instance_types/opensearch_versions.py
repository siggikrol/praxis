"""Supported OpenSearch engine versions, excluding legacy Elasticsearch."""
import re
import time

import boto3
from botocore.config import Config

from .common import (AwsCatalogMeta, BackgroundRefreshRegistry, acquire_lock, cache_dir,
                     cache_paths, is_stale, normalize_region, read_json, release_lock,
                     ttl_seconds, write_json_atomic)
from .resilience import catalog_refresh, manual_refresh, refresh_status


def _cache_paths(region):
    return cache_paths(cache_dir_path=cache_dir('PS_OPENSEARCH_CATALOG_CACHE_DIR'),
                       cache_stem='opensearch_versions', region=region)


def _fetch_versions(region):
    client = boto3.session.Session(region_name=region).client('opensearch', config=Config(
        connect_timeout=5, read_timeout=20, retries={'max_attempts': 2, 'mode': 'standard'}))
    versions = set()
    args = {'MaxResults': 100}
    while True:
        response = client.list_versions(**args)
        for value in response.get('Versions', []):
            match = re.fullmatch(r'OpenSearch_([0-9]+\.[0-9]+)', value)
            if match:
                versions.add(match[1])
        if not response.get('NextToken'):
            return sorted(versions, key=lambda value: tuple(map(int, value.split('.'))), reverse=True)
        args['NextToken'] = response['NextToken']


@catalog_refresh
def _refresh_unlocked(region, cache_path):
    versions = _fetch_versions(region)
    payload = {'schema': 1, 'generated_at': time.time(), 'region': region,
               'groups': [['OpenSearch Versions', versions]], 'count': len(versions)}
    write_json_atomic(cache_path, payload)
    return {'ok': True, 'cache_path': cache_path, 'count': len(versions),
            'generated_at': payload['generated_at']}


def _refresh(region, force):
    region = normalize_region(region)
    path, lock_path = _cache_paths(region)
    lock = acquire_lock(lock_path, blocking=force)
    if lock is None:
        return {'ok': False, 'error': 'Cache refresh already running or lock unavailable', 'cache_path': path}
    try:
        payload = read_json(path) or {}
        if not force and not is_stale(payload.get('generated_at'), ttl_seconds('PS_OPENSEARCH_CATALOG_TTL_SECONDS')):
            return {'ok': True, 'skipped': True, 'cache_path': path}
        return _refresh_unlocked(region, path)
    finally:
        release_lock(lock)


@manual_refresh
def refresh_opensearch_versions_cache(region=None):
    return _refresh(region, True)


def refresh_opensearch_versions_cache_if_stale(region=None):
    return _refresh(region, False)


def get_version_options(region=None, *, force=False, refresh_async=True):
    region = normalize_region(region)
    path, _ = _cache_paths(region)
    if force:
        refresh_opensearch_versions_cache(region)
    payload = read_json(path) or {}
    versions = []
    if payload.get('schema') == 1:
        versions = [version for _, values in payload.get('groups', []) for version in values
                    if re.fullmatch(r'[0-9]+\.[0-9]+', version)]
    stale = is_stale(payload.get('generated_at'), ttl_seconds('PS_OPENSEARCH_CATALOG_TTL_SECONDS'))
    if stale and refresh_async and not force:
        BackgroundRefreshRegistry.spawn(f'opensearch-versions:{region}',
                                        lambda: refresh_opensearch_versions_cache_if_stale(region))
    meta = AwsCatalogMeta(catalog='opensearch_versions', region=region, cache_path=path,
                          generated_at=payload.get('generated_at'), stale=stale,
                          count=len(versions), source='aws_cache' if versions else 'fallback',
                          error=refresh_status(path).get('error'))
    return versions, meta
