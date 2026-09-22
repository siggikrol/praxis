"""One catalog definition for refresh, cache inspection, controls and IAM requirements."""
from dataclasses import dataclass
from importlib import import_module
from pathlib import Path
import os

from .common import cache_dir, read_json, ttl_seconds


@dataclass(frozen=True)
class Catalog:
    key: str
    label: str
    module: str
    function: str
    directory_env: str
    ttl_env: str
    stem: str
    actions: tuple[str, ...]
    kind: str = ''
    provenance: str = 'aws_cache'

    def module_object(self):
        return import_module(f'services.aws_instance_types.{self.module}')

    def engines(self):
        if self.kind == 'aurora':
            default = os.getenv('PS_AURORA_ENGINE') or 'aurora-postgresql'
            return [default, 'postgres'] if self.key == 'aurora_versions' and default != 'postgres' else [default]
        if self.kind == 'ami':
            from engine.wizards.constants.eks_constants import EKS_DEFAULTS
            return [f"{EKS_DEFAULTS['cluster_version']}/{EKS_DEFAULTS['ami_type']}"]
        return ['']

    def path(self, region, engine=''):
        module = self.module_object()
        if self.kind == 'aurora':
            name, _, version = engine.partition('/')
            kwargs = {'engine_version': version or None} if self.key == 'aurora' else {}
            return module._cache_paths(engine=name, region=region, **kwargs)[0]
        if self.kind == 'ami':
            version, ami_type = engine.split('/', 1)
            return module._cache_paths(region, version, ami_type)[0]
        if self.key == 'kafka_versions':
            return module._version_cache_paths(region)[0]
        if self.key == 'kafka_broker_types':
            return module._broker_type_cache_paths(region)[0]
        return module._cache_paths(region)[0]

    def refresh(self, region, engine='', *, force=False):
        module = self.module_object()
        if self.kind == 'opensearch':
            if not engine:
                return module.refresh_opensearch_catalog(region, force=force)
            return module.refresh_catalog_entry(region, engine, limits=self.key == 'opensearch_limits')
        kwargs = {}
        if self.kind == 'aurora':
            name, _, version = (engine or self.engines()[0]).partition('/')
            kwargs['engine'] = name
            if self.key == 'aurora':
                kwargs['engine_version'] = version or None
        elif self.kind == 'ami':
            version, ami_type = (engine or self.engines()[0]).split('/', 1)
            kwargs.update(cluster_version=version, ami_type=ami_type)
        function = self.function if force else self.function + '_if_stale'
        return getattr(module, function)(region, **kwargs)


CATALOGS = (
    Catalog('opensearch_versions', 'OpenSearch Engine Versions', 'opensearch_versions', 'refresh_opensearch_versions_cache', 'PS_OPENSEARCH_CATALOG_CACHE_DIR', 'PS_OPENSEARCH_CATALOG_TTL_SECONDS', 'opensearch_versions.', ('es:ListVersions',)),
    Catalog('eks_versions', 'EKS Cluster Versions', 'eks_cluster_versions', 'refresh_eks_cluster_versions_cache', 'PS_EKS_CLUSTER_VERSIONS_CACHE_DIR', 'PS_EKS_CLUSTER_VERSIONS_TTL_SECONDS', 'eks_cluster_versions.', ('eks:DescribeClusterVersions',)),
    Catalog('eks', 'EKS Instance Types', 'eks_instance_types', 'refresh_eks_instance_type_cache', 'PS_EKS_INSTANCE_TYPES_CACHE_DIR', 'PS_EKS_INSTANCE_TYPES_TTL_SECONDS', 'eks_instance_types.', ('ec2:DescribeInstanceTypes',)),
    Catalog('eks_ami', 'EKS AMI IDs', 'eks_ami', 'refresh_eks_ami_cache', 'PS_EKS_AMI_CACHE_DIR', 'PS_EKS_AMI_TTL_SECONDS', 'eks_ami.', ('ssm:GetParameter',), 'ami'),
    Catalog('aurora_versions', 'Aurora / PostgreSQL Engine Versions', 'aurora_engine_versions', 'refresh_aurora_engine_version_cache', 'PS_AURORA_ENGINE_VERSIONS_CACHE_DIR', 'PS_AURORA_ENGINE_VERSIONS_TTL_SECONDS', 'aurora_engine_versions.', ('rds:DescribeDBEngineVersions',), 'aurora'),
    Catalog('aurora', 'Aurora Instance Classes', 'aurora_instance_classes', 'refresh_aurora_instance_class_cache', 'PS_AURORA_INSTANCE_CLASSES_CACHE_DIR', 'PS_AURORA_INSTANCE_CLASSES_TTL_SECONDS', 'aurora_instance_classes.', ('rds:DescribeOrderableDBInstanceOptions',), 'aurora'),
    Catalog('redis', 'Redis Node Types', 'redis_node_types', 'refresh_redis_node_type_cache', 'PS_REDIS_NODE_TYPES_CACHE_DIR', 'PS_REDIS_NODE_TYPES_TTL_SECONDS', 'redis_node_types.', ('elasticache:DescribeReservedCacheNodesOfferings',)),
    Catalog('kafka_versions', 'Kafka Versions', 'kafka_catalog', 'refresh_kafka_version_cache', 'PS_KAFKA_CATALOG_CACHE_DIR', 'PS_KAFKA_CATALOG_TTL_SECONDS', 'kafka_versions.', ('kafka:ListKafkaVersions',)),
    Catalog('kafka_broker_types', 'Kafka Broker Node Types', 'kafka_catalog', 'refresh_kafka_broker_node_instance_type_cache', 'PS_KAFKA_CATALOG_CACHE_DIR', 'PS_KAFKA_CATALOG_TTL_SECONDS', 'kafka_broker_node_instance_types.', ('ec2:DescribeInstanceTypeOfferings',), provenance='curated_ec2_filtered'),
    Catalog('opensearch', 'OpenSearch Instance Types', 'opensearch_catalog', '', 'PS_OPENSEARCH_CATALOG_CACHE_DIR', 'PS_OPENSEARCH_CATALOG_TTL_SECONDS', 'opensearch_', ('es:ListInstanceTypeDetails',), 'opensearch'),
    Catalog('opensearch_limits', 'OpenSearch Instance Limits', 'opensearch_catalog', '', 'PS_OPENSEARCH_CATALOG_CACHE_DIR', 'PS_OPENSEARCH_CATALOG_TTL_SECONDS', 'opensearch_', ('es:DescribeInstanceTypeLimits',), 'opensearch'),
)
BY_KEY = {catalog.key: catalog for catalog in CATALOGS}


def requested_catalogs(key):
    if key in ('', 'all'):
        # The OpenSearch aggregate refresh also includes its limit caches.
        return [c.key for c in CATALOGS if c.key != 'opensearch_limits']
    if key == 'kafka':
        return ['kafka_versions', 'kafka_broker_types']
    if key not in BY_KEY:
        raise ValueError(f'Unknown catalog: {key}')
    return [key]


def entries(regions):
    found = {}
    for catalog in CATALOGS:
        if catalog.kind == 'opensearch':
            continue
        for region in regions:
            for engine in catalog.engines():
                path = catalog.path(region, engine)
                found[path] = dict(catalog=catalog.key, region=region, engine=engine or None,
                                   engine_safe=None, cache_path=path)
        for path in Path(cache_dir(catalog.directory_env)).glob(catalog.stem + '*.json'):
            if path.name.endswith('.status.json'):
                continue
            payload = read_json(str(path)) or {}
            region = payload.get('region') or path.name[:-5].rsplit('.', 1)[-1]
            engine = payload.get('engine') if catalog.kind == 'aurora' else ''
            if catalog.key == 'aurora' and payload.get('engine_version'):
                engine = f"{engine}/{payload['engine_version']}"
            if catalog.kind == 'ami':
                engine = f"{payload.get('cluster_version', '')}/{payload.get('ami_type', '')}"
                if not all(engine.split('/')):
                    continue
            if catalog.kind == 'aurora' and not engine:
                continue
            found[str(path)] = dict(catalog=catalog.key, region=region, engine=engine or None,
                                    engine_safe=None, cache_path=str(path))
    from .opensearch_catalog import cache_entries
    for entry in cache_entries(regions):
        found[entry['cache_path']] = entry
    return list(found.values())


def refresh_region(region, *, force=False, catalogs=None, engine=''):
    results = {}
    for key in catalogs or requested_catalogs('all'):
        catalog = BY_KEY[key]
        variants = [engine] if engine else catalog.engines()
        if not engine and catalog.kind in ('ami', 'aurora'):
            variants = sorted(set(variants + [e['engine'] for e in entries([region])
                                              if e['catalog'] == key and e['region'] == region and e['engine']]))
        for variant in variants:
            name = key + (':' + variant if variant else '')
            try:
                results[name] = catalog.refresh(region, variant, force=force)
            except Exception as exc:
                results[name] = {'ok': False, 'error': str(exc)}
    return results


def iam_policy():
    return {'Version': '2012-10-17', 'Statement': [{
        'Sid': 'ReadAwsCatalogApis', 'Effect': 'Allow',
        'Action': sorted({action for catalog in CATALOGS for action in catalog.actions}),
        'Resource': '*',
    }]}
