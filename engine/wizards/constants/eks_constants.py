EKS_DEFAULTS = {
    "cluster_version": "1.34",
    "ssh_allow_access_from_cidrs": '["10.0.0.0/8"]',
    "endpoint_private_access": True,
    "endpoint_public_access": False,
    "endpoint_public_access_cidrs": '["0.0.0.0/0"]',
    "api_allow_access_from_cidrs": '["10.0.0.0/8"]',
    "ami_type": "amazon-linux-2023",
    "systems_admin_role": "AWSReservedSSO_Administrator-8-Hours_",
    "default_node_group": {
        "enabled": True,
        "name": "ng0",
        "instance_types": "t3.medium",
        "min_size": 2,
        "max_size": 3,
        "desired_size": 2,
    },
}

EKS_VALIDATION_LIMITS = {
    "volume_size_min": 1,
    "iops_min": 100,
    "throughput_min": 1,
}

EKS_NODE_TYPES = [
    ('t3.micro',   't3.micro'),
    ('t3.small',   't3.small'),
    ('t3.medium',  't3.medium'),
    ('t3.large',   't3.large'),
    ('t3a.large',  't3a.large'),
    ('t3a.xlarge', 't3a.xlarge'),
    ('t3a.2xlarge','t3a.2xlarge'),
    ('m5.large',   'm5.large'),
    ('m5.xlarge',  'm5.xlarge'),
    ('r5.large',   'r5.large'),
    ('r5.xlarge',  'r5.xlarge'),
]

EKS_TAINT_EFFECTS = [
    "NO_SCHEDULE",
    "PREFER_NO_SCHEDULE",
    "NO_EXECUTE",
]

# NOTE:
# Cluster versions can be fetched from the AWS EKS API and cached on disk (see
# `services/aws_instance_types/eks_cluster_versions.py`). These lists are kept as
# a static fallback if AWS is unavailable or credentials lack DescribeClusterVersions.
EKS_CLUSTER_VERSION_GROUPS = [
    ("Standard Support", [
        ("1.35", "1.35"),
        ("1.34", "1.34"),
        ("1.33", "1.33"),
        ("1.32", "1.32"),
    ]),
    ("Extended Support", [
        ("1.31", "1.31"),
        ("1.30", "1.30"),
        ("1.29", "1.29"),
    ]),
]

EKS_CLUSTER_VERSIONS = [
    option
    for _group, group_options in EKS_CLUSTER_VERSION_GROUPS
    for option in group_options
]

# NOTE:
# Instance types are now fetched from the AWS EC2 API and cached (see
# `services/aws_instance_types/eks_instance_types.py`). These lists are kept as
# a static fallback if AWS is unavailable or credentials lack DescribeInstanceTypes.
EKS_INSTANCE_TYPE_GROUPS = [
    ("Burstable", [
        ("t3.micro", "t3.micro"),
        ("t3.small", "t3.small"),
        ("t3.medium", "t3.medium"),
        ("t3.large", "t3.large"),
        ("t3.xlarge", "t3.xlarge"),
        ("t3.2xlarge", "t3.2xlarge"),

        ("t3a.micro", "t3a.micro"),
        ("t3a.small", "t3a.small"),
        ("t3a.medium", "t3a.medium"),
        ("t3a.large", "t3a.large"),
        ("t3a.xlarge", "t3a.xlarge"),
        ("t3a.2xlarge", "t3a.2xlarge"),

        ("t4g.micro", "t4g.micro"),
        ("t4g.small", "t4g.small"),
        ("t4g.medium", "t4g.medium"),
        ("t4g.large", "t4g.large"),
        ("t4g.xlarge", "t4g.xlarge"),
        ("t4g.2xlarge", "t4g.2xlarge"),
    ]),

    ("General Purpose", [
        ("m5.large", "m5.large"),
        ("m5.xlarge", "m5.xlarge"),
        ("m5.2xlarge", "m5.2xlarge"),
        ("m5.4xlarge", "m5.4xlarge"),
        ("m5.8xlarge", "m5.8xlarge"),
        ("m5.12xlarge", "m5.12xlarge"),
        ("m5.16xlarge", "m5.16xlarge"),
        ("m5.24xlarge", "m5.24xlarge"),

        ("m6i.large", "m6i.large"),
        ("m6i.xlarge", "m6i.xlarge"),
        ("m6i.2xlarge", "m6i.2xlarge"),
        ("m6i.4xlarge", "m6i.4xlarge"),
        ("m6i.8xlarge", "m6i.8xlarge"),
        ("m6i.12xlarge", "m6i.12xlarge"),
        ("m6i.16xlarge", "m6i.16xlarge"),
        ("m6i.24xlarge", "m6i.24xlarge"),
        ("m6i.32xlarge", "m6i.32xlarge"),

        ("m6g.large", "m6g.large"),
        ("m6g.xlarge", "m6g.xlarge"),
        ("m6g.2xlarge", "m6g.2xlarge"),
        ("m6g.4xlarge", "m6g.4xlarge"),
        ("m6g.8xlarge", "m6g.8xlarge"),
        ("m6g.12xlarge", "m6g.12xlarge"),
        ("m6g.16xlarge", "m6g.16xlarge"),

        ("m7g.large", "m7g.large"),
        ("m7g.xlarge", "m7g.xlarge"),
        ("m7g.2xlarge", "m7g.2xlarge"),
        ("m7g.4xlarge", "m7g.4xlarge"),
        ("m7g.8xlarge", "m7g.8xlarge"),
        ("m7g.12xlarge", "m7g.12xlarge"),
        ("m7g.16xlarge", "m7g.16xlarge"),
    ]),

    ("Compute Optimized", [
        ("c5.large", "c5.large"),
        ("c5.xlarge", "c5.xlarge"),
        ("c5.2xlarge", "c5.2xlarge"),
        ("c5.4xlarge", "c5.4xlarge"),
        ("c5.9xlarge", "c5.9xlarge"),
        ("c5.12xlarge", "c5.12xlarge"),
        ("c5.18xlarge", "c5.18xlarge"),
        ("c5.24xlarge", "c5.24xlarge"),

        ("c6i.large", "c6i.large"),
        ("c6i.xlarge", "c6i.xlarge"),
        ("c6i.2xlarge", "c6i.2xlarge"),
        ("c6i.4xlarge", "c6i.4xlarge"),
        ("c6i.8xlarge", "c6i.8xlarge"),
        ("c6i.12xlarge", "c6i.12xlarge"),
        ("c6i.16xlarge", "c6i.16xlarge"),
        ("c6i.24xlarge", "c6i.24xlarge"),

        ("c6g.large", "c6g.large"),
        ("c6g.xlarge", "c6g.xlarge"),
        ("c6g.2xlarge", "c6g.2xlarge"),
        ("c6g.4xlarge", "c6g.4xlarge"),
        ("c6g.8xlarge", "c6g.8xlarge"),
        ("c6g.12xlarge", "c6g.12xlarge"),
        ("c6g.16xlarge", "c6g.16xlarge"),

        ("c7g.large", "c7g.large"),
        ("c7g.xlarge", "c7g.xlarge"),
        ("c7g.2xlarge", "c7g.2xlarge"),
        ("c7g.4xlarge", "c7g.4xlarge"),
        ("c7g.8xlarge", "c7g.8xlarge"),
        ("c7g.12xlarge", "c7g.12xlarge"),
        ("c7g.16xlarge", "c7g.16xlarge"),
    ]),

    ("Memory Optimized", [
        ("r5.large", "r5.large"),
        ("r5.xlarge", "r5.xlarge"),
        ("r5.2xlarge", "r5.2xlarge"),
        ("r5.4xlarge", "r5.4xlarge"),
        ("r5.8xlarge", "r5.8xlarge"),
        ("r5.12xlarge", "r5.12xlarge"),
        ("r5.16xlarge", "r5.16xlarge"),
        ("r5.24xlarge", "r5.24xlarge"),

        ("r6i.large", "r6i.large"),
        ("r6i.xlarge", "r6i.xlarge"),
        ("r6i.2xlarge", "r6i.2xlarge"),
        ("r6i.4xlarge", "r6i.4xlarge"),
        ("r6i.8xlarge", "r6i.8xlarge"),
        ("r6i.12xlarge", "r6i.12xlarge"),
        ("r6i.16xlarge", "r6i.16xlarge"),
        ("r6i.24xlarge", "r6i.24xlarge"),
        ("r6i.32xlarge", "r6i.32xlarge"),

        ("r6g.large", "r6g.large"),
        ("r6g.xlarge", "r6g.xlarge"),
        ("r6g.2xlarge", "r6g.2xlarge"),
        ("r6g.4xlarge", "r6g.4xlarge"),
        ("r6g.8xlarge", "r6g.8xlarge"),
        ("r6g.12xlarge", "r6g.12xlarge"),
        ("r6g.16xlarge", "r6g.16xlarge"),

        ("r7g.large", "r7g.large"),
        ("r7g.xlarge", "r7g.xlarge"),
        ("r7g.2xlarge", "r7g.2xlarge"),
        ("r7g.4xlarge", "r7g.4xlarge"),
        ("r7g.8xlarge", "r7g.8xlarge"),
        ("r7g.12xlarge", "r7g.12xlarge"),
        ("r7g.16xlarge", "r7g.16xlarge"),
    ]),

    ("GPU / Accelerated", [
        ("g4dn.xlarge", "g4dn.xlarge"),
        ("g4dn.2xlarge", "g4dn.2xlarge"),
        ("g4dn.4xlarge", "g4dn.4xlarge"),
        ("g4dn.8xlarge", "g4dn.8xlarge"),
        ("g4dn.12xlarge", "g4dn.12xlarge"),

        ("g5.xlarge", "g5.xlarge"),
        ("g5.2xlarge", "g5.2xlarge"),
        ("g5.4xlarge", "g5.4xlarge"),
        ("g5.8xlarge", "g5.8xlarge"),
        ("g5.12xlarge", "g5.12xlarge"),

        ("p3.2xlarge", "p3.2xlarge"),
        ("p3.8xlarge", "p3.8xlarge"),
        ("p3.16xlarge", "p3.16xlarge"),

        ("p4d.24xlarge", "p4d.24xlarge"),
        ("p5.48xlarge", "p5.48xlarge"),
    ]),
]

EKS_INSTANCE_TYPES = [
    option
    for _group, group_options in EKS_INSTANCE_TYPE_GROUPS
    for option in group_options
]
