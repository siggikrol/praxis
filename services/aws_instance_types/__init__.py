"""
AWS option catalog helpers.

Used by the wizards to populate dropdowns from AWS APIs (cached on disk), with
static fallbacks when AWS is unavailable.
"""

from .eks_cluster_versions import (
    get_eks_cluster_version_options,
    refresh_eks_cluster_versions_cache,
)
from .eks_instance_types import (
    get_eks_instance_type_options,
    refresh_eks_instance_type_cache,
)
from .aurora_instance_classes import (
    get_aurora_instance_class_options,
    refresh_aurora_instance_class_cache,
)
from .aurora_engine_versions import (
    get_aurora_engine_version_options,
    refresh_aurora_engine_version_cache,
)
from .redis_node_types import (
    get_redis_node_type_options,
    refresh_redis_node_type_cache,
)
from .kafka_catalog import (
    get_kafka_version_options,
    get_kafka_broker_node_instance_type_options,
    refresh_kafka_catalog_cache,
)

__all__ = [
    "get_eks_cluster_version_options",
    "refresh_eks_cluster_versions_cache",
    "get_eks_instance_type_options",
    "refresh_eks_instance_type_cache",
    "get_aurora_instance_class_options",
    "refresh_aurora_instance_class_cache",
    "get_aurora_engine_version_options",
    "refresh_aurora_engine_version_cache",
    "get_redis_node_type_options",
    "refresh_redis_node_type_cache",
    "get_kafka_version_options",
    "get_kafka_broker_node_instance_type_options",
    "refresh_kafka_catalog_cache",
]
