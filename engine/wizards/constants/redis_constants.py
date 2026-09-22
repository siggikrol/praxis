# modules/wizards/constants/redis_constants.py
# Generic configuration constants for Redis form.

# Default Redis node types to show in dropdown
# NOTE:
# Node types are now fetched from the AWS ElastiCache API and cached (see
# `services/aws_instance_types/redis_node_types.py`). These lists are kept as
# a static fallback if AWS is unavailable or credentials lack DescribeReservedCacheNodesOfferings.
REDIS_NODE_TYPE_GROUPS = [
    ("Burstable", [
        ("cache.t2.micro", "cache.t2.micro"),
        ("cache.t2.small", "cache.t2.small"),
        ("cache.t2.medium", "cache.t2.medium"),
        ("cache.t3.micro", "cache.t3.micro"),
        ("cache.t3.small", "cache.t3.small"),
        ("cache.t3.medium", "cache.t3.medium"),
        ("cache.t4g.micro", "cache.t4g.micro"),
        ("cache.t4g.small", "cache.t4g.small"),
        ("cache.t4g.medium", "cache.t4g.medium"),
    ]),
    ("General Purpose", [
        ("cache.m4.large", "cache.m4.large"),
        ("cache.m4.xlarge", "cache.m4.xlarge"),
        ("cache.m4.2xlarge", "cache.m4.2xlarge"),
        ("cache.m4.4xlarge", "cache.m4.4xlarge"),
        ("cache.m5.large", "cache.m5.large"),
        ("cache.m5.xlarge", "cache.m5.xlarge"),
        ("cache.m5.2xlarge", "cache.m5.2xlarge"),
        ("cache.m5.4xlarge", "cache.m5.4xlarge"),
        ("cache.m5.12xlarge", "cache.m5.12xlarge"),
        ("cache.m6g.large", "cache.m6g.large"),
        ("cache.m6g.xlarge", "cache.m6g.xlarge"),
        ("cache.m6g.2xlarge", "cache.m6g.2xlarge"),
        ("cache.m6g.4xlarge", "cache.m6g.4xlarge"),
        ("cache.m6g.8xlarge", "cache.m6g.8xlarge"),
        ("cache.m6g.12xlarge", "cache.m6g.12xlarge"),
        ("cache.m6g.16xlarge", "cache.m6g.16xlarge"),
    ]),
    ("Memory Optimized", [
        ("cache.r4.large", "cache.r4.large"),
        ("cache.r4.xlarge", "cache.r4.xlarge"),
        ("cache.r4.2xlarge", "cache.r4.2xlarge"),
        ("cache.r4.4xlarge", "cache.r4.4xlarge"),
        ("cache.r4.8xlarge", "cache.r4.8xlarge"),
        ("cache.r4.16xlarge", "cache.r4.16xlarge"),
        ("cache.r5.large", "cache.r5.large"),
        ("cache.r5.xlarge", "cache.r5.xlarge"),
        ("cache.r5.2xlarge", "cache.r5.2xlarge"),
        ("cache.r5.4xlarge", "cache.r5.4xlarge"),
        ("cache.r5.12xlarge", "cache.r5.12xlarge"),
        ("cache.r6g.large", "cache.r6g.large"),
        ("cache.r6g.xlarge", "cache.r6g.xlarge"),
        ("cache.r6g.2xlarge", "cache.r6g.2xlarge"),
        ("cache.r6g.4xlarge", "cache.r6g.4xlarge"),
        ("cache.r6g.8xlarge", "cache.r6g.8xlarge"),
        ("cache.r6g.12xlarge", "cache.r6g.12xlarge"),
        ("cache.r6g.16xlarge", "cache.r6g.16xlarge"),
        ("cache.r7g.large", "cache.r7g.large"),
        ("cache.r7g.xlarge", "cache.r7g.xlarge"),
        ("cache.r7g.2xlarge", "cache.r7g.2xlarge"),
        ("cache.r7g.4xlarge", "cache.r7g.4xlarge"),
        ("cache.r7g.8xlarge", "cache.r7g.8xlarge"),
        ("cache.r7g.12xlarge", "cache.r7g.12xlarge"),
        ("cache.r7g.16xlarge", "cache.r7g.16xlarge"),
    ]),
]

REDIS_NODE_TYPES = [
    option
    for _group, group_options in REDIS_NODE_TYPE_GROUPS
    for option in group_options
]

# Default recommended values per environment profile
REDIS_DEFAULTS = {
    "instance_type": "cache.t3.medium",
    "cluster_size": 3,
    "shards": 1,
    "replicas": 2,
}

# Validation boundaries
REDIS_VALIDATION_LIMITS = {
    "cluster_size_min": 1,
    "shards_min": 1,
    "replicas_min": 0,
}
