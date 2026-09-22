"""Readiness planning fields backed by the provisioning wizard's AWS catalogues."""
from flask import current_app, has_app_context

from engine.wizards.components import readiness_components

FIELDS = (
    ('opensearch_engine_version', 'OpenSearch engine version'),
    ('opensearch_instance_type', 'OpenSearch data instance type'),
    ('opensearch_instance_count', 'OpenSearch data instance count'),
    ('opensearch_master_type', 'OpenSearch master instance type'),
    ('opensearch_master_count', 'OpenSearch master instance count'),
)


def enabled(data):
    return any(c.key == 'camunda_opensearch' for c in readiness_components(data))


def _refresh_async():
    return has_app_context() and not current_app.testing


def catalogs(region, version):
    from services.aws_instance_types.opensearch_versions import get_version_options
    from services.aws_instance_types.opensearch_catalog import get_catalog, catalog_meta
    versions, meta = get_version_options(region, refresh_async=_refresh_async())
    rows, stale = get_catalog(region, version or '3.3', refresh_async=_refresh_async())
    def pack(values, source, stale):
        return {'options': [{'value': v, 'label': v} for v in values], 'source': source, 'stale': stale}
    result = {'opensearch_engine_version': pack(versions, meta.source, meta.stale)}
    for role, key in [('data', 'opensearch_instance_type'), ('master', 'opensearch_master_type')]:
        values = sorted({row['InstanceType'] for row in rows or [] if role in row.get('InstanceRole', [])})
        result[key] = pack(values, catalog_meta(region, version or '3.3').source, stale)
    return result


def validate(data, *, aws=False):
    if not enabled(data):
        return []
    errors = []
    for key, label in FIELDS:
        value = str(data.get(key) or '').strip()
        if not value:
            errors.append(f'{label} is required.')
        elif key.endswith('_count') and (not value.isdigit() or int(value) < 1):
            errors.append(f'{label} must be a positive integer.')
    if not aws or errors:
        return errors
    region, version = data.get('aws_region'), data.get('opensearch_engine_version')
    for key, catalog in catalogs(region, version).items():
        allowed = {o['value'] for o in catalog['options']}
        if allowed and not catalog['stale'] and data.get(key) not in allowed:
            errors.append(f'{dict(FIELDS)[key]} is not available in the selected AWS region catalog.')
    from services.aws_instance_types.opensearch_catalog import get_catalog
    for role, type_key, count_key in [('data', 'opensearch_instance_type', 'opensearch_instance_count'), ('master', 'opensearch_master_type', 'opensearch_master_count')]:
        limits, stale = get_catalog(region, version, data[type_key], refresh_async=_refresh_async())
        bounds = (limits or {}).get(role, {}).get('InstanceLimits', {}).get('InstanceCountLimits', {})
        count = int(data[count_key])
        if not stale and (count < bounds.get('MinimumInstanceCount', 1) or count > bounds.get('MaximumInstanceCount', float('inf'))):
            errors.append(f'{dict(FIELDS)[count_key]} is outside the AWS limits for this instance type.')
    return errors
