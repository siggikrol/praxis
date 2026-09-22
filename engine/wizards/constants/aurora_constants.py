# modules/wizards/constants/aurora_constants.py
# Generic configuration constants for Aurora (RDS) forms and logic

AURORA_ENGINE_CHOICES = [
    ("aurora-postgresql", "aurora-postgresql"),
    ("aurora-mysql", "aurora-mysql"),
]

AURORA_ENGINE_PROFILES = {
    "aurora-postgresql": {
        "engine": "aurora-postgresql",
        "engine_version": "15.10",
        "param_group_family": "aurora-postgresql15",
        "enabled_logs_exports": '["postgresql"]',
        "parameters": [
            {
                "name": "shared_preload_libraries",
                "value": "pg_cron,pgaudit,pg_stat_statements",
                "apply_method": "pending-reboot",
            },
            {
                "name": "pgaudit.log",
                "value": "function,role,ddl,misc",
                "apply_method": "immediate",
            },
            {
                "name": "pgaudit.role",
                "value": "rds_pgaudit",
                "apply_method": "pending-reboot",
            },
            {"name": "log_connections", "value": "1", "apply_method": "immediate"},
        ],
    },
    "aurora-mysql": {
        "engine": "aurora-mysql",
        "engine_version": "8.0",
        "param_group_family": "aurora-mysql8.0",
        "enabled_logs_exports": '["error","general","slowquery"]',
        "parameters": [
            {"name": "max_connections", "value": "16000", "apply_method": "immediate"},
            {
                "name": "aws_default_s3_role",
                "value": "arn:aws:iam::<account_id>:role/<ssa-prefix>-<env>-rds_s3_role",
                "apply_method": "immediate",
            },
            {
                "name": "activate_all_roles_on_login",
                "value": "1",
                "apply_method": "immediate",
            },
        ],
    },
}

AURORA_DEFAULTS = {
    "engine": "aurora-postgresql",
    "engine_version": AURORA_ENGINE_PROFILES["aurora-postgresql"]["engine_version"],
    "database_username": "rds_admin",
    "create_param_group": "true",
    "use_name_prefix": "false",
    "param_group_family": AURORA_ENGINE_PROFILES["aurora-postgresql"]["param_group_family"],
    "enabled_logs_exports": AURORA_ENGINE_PROFILES["aurora-postgresql"]["enabled_logs_exports"],
    "enable_s3_export_lambda": "false",
}


def get_aurora_engine_profile(engine: str | None) -> dict:
    normalized = (engine or "").strip().lower()
    profile = AURORA_ENGINE_PROFILES.get(normalized) or AURORA_ENGINE_PROFILES[AURORA_DEFAULTS["engine"]]
    return {
        "engine": profile["engine"],
        "engine_version": profile["engine_version"],
        "param_group_family": profile["param_group_family"],
        "enabled_logs_exports": profile["enabled_logs_exports"],
        "parameters": [dict(item) for item in profile.get("parameters", [])],
    }

# NOTE:
# Instance classes are now fetched from the AWS RDS API and cached (see
# `services/aws_instance_types/aurora_instance_classes.py`). These lists are kept as
# a static fallback if AWS is unavailable or credentials lack DescribeOrderableDBInstanceOptions.
AURORA_INSTANCE_CLASS_GROUPS = [
    ("Burstable", [
        ("db.t2.micro", "db.t2.micro"),
        ("db.t2.small", "db.t2.small"),
        ("db.t2.medium", "db.t2.medium"),
        ("db.t3.micro", "db.t3.micro"),
        ("db.t3.small", "db.t3.small"),
        ("db.t3.medium", "db.t3.medium"),
        ("db.t3.large", "db.t3.large"),
        ("db.t4g.micro", "db.t4g.micro"),
        ("db.t4g.small", "db.t4g.small"),
        ("db.t4g.medium", "db.t4g.medium"),
        ("db.t4g.large", "db.t4g.large"),
    ]),
    ("General Purpose", [
        ("db.m4.large", "db.m4.large"),
        ("db.m4.xlarge", "db.m4.xlarge"),
        ("db.m4.2xlarge", "db.m4.2xlarge"),
        ("db.m4.4xlarge", "db.m4.4xlarge"),
        ("db.m4.10xlarge", "db.m4.10xlarge"),
        ("db.m4.16xlarge", "db.m4.16xlarge"),
        ("db.m5.large", "db.m5.large"),
        ("db.m5.xlarge", "db.m5.xlarge"),
        ("db.m5.2xlarge", "db.m5.2xlarge"),
        ("db.m5.4xlarge", "db.m5.4xlarge"),
        ("db.m5.8xlarge", "db.m5.8xlarge"),
        ("db.m5.12xlarge", "db.m5.12xlarge"),
        ("db.m5.16xlarge", "db.m5.16xlarge"),
        ("db.m5.24xlarge", "db.m5.24xlarge"),
        ("db.m6g.large", "db.m6g.large"),
        ("db.m6g.xlarge", "db.m6g.xlarge"),
        ("db.m6g.2xlarge", "db.m6g.2xlarge"),
        ("db.m6g.4xlarge", "db.m6g.4xlarge"),
        ("db.m6g.8xlarge", "db.m6g.8xlarge"),
        ("db.m6g.12xlarge", "db.m6g.12xlarge"),
        ("db.m6g.16xlarge", "db.m6g.16xlarge"),
        ("db.m6i.large", "db.m6i.large"),
        ("db.m6i.xlarge", "db.m6i.xlarge"),
        ("db.m6i.2xlarge", "db.m6i.2xlarge"),
        ("db.m6i.4xlarge", "db.m6i.4xlarge"),
        ("db.m6i.8xlarge", "db.m6i.8xlarge"),
        ("db.m6i.12xlarge", "db.m6i.12xlarge"),
        ("db.m6i.16xlarge", "db.m6i.16xlarge"),
        ("db.m6i.24xlarge", "db.m6i.24xlarge"),
        ("db.m6i.32xlarge", "db.m6i.32xlarge"),
        ("db.m7g.large", "db.m7g.large"),
        ("db.m7g.xlarge", "db.m7g.xlarge"),
        ("db.m7g.2xlarge", "db.m7g.2xlarge"),
        ("db.m7g.4xlarge", "db.m7g.4xlarge"),
        ("db.m7g.8xlarge", "db.m7g.8xlarge"),
        ("db.m7g.12xlarge", "db.m7g.12xlarge"),
        ("db.m7g.16xlarge", "db.m7g.16xlarge"),
    ]),
    ("Memory Optimized", [
        ("db.r4.large", "db.r4.large"),
        ("db.r4.xlarge", "db.r4.xlarge"),
        ("db.r4.2xlarge", "db.r4.2xlarge"),
        ("db.r4.4xlarge", "db.r4.4xlarge"),
        ("db.r4.8xlarge", "db.r4.8xlarge"),
        ("db.r4.16xlarge", "db.r4.16xlarge"),
        ("db.r5.large", "db.r5.large"),
        ("db.r5.xlarge", "db.r5.xlarge"),
        ("db.r5.2xlarge", "db.r5.2xlarge"),
        ("db.r5.4xlarge", "db.r5.4xlarge"),
        ("db.r5.8xlarge", "db.r5.8xlarge"),
        ("db.r5.12xlarge", "db.r5.12xlarge"),
        ("db.r5.16xlarge", "db.r5.16xlarge"),
        ("db.r5.24xlarge", "db.r5.24xlarge"),
        ("db.r6g.large", "db.r6g.large"),
        ("db.r6g.xlarge", "db.r6g.xlarge"),
        ("db.r6g.2xlarge", "db.r6g.2xlarge"),
        ("db.r6g.4xlarge", "db.r6g.4xlarge"),
        ("db.r6g.8xlarge", "db.r6g.8xlarge"),
        ("db.r6g.12xlarge", "db.r6g.12xlarge"),
        ("db.r6g.16xlarge", "db.r6g.16xlarge"),
        ("db.r6i.large", "db.r6i.large"),
        ("db.r6i.xlarge", "db.r6i.xlarge"),
        ("db.r6i.2xlarge", "db.r6i.2xlarge"),
        ("db.r6i.4xlarge", "db.r6i.4xlarge"),
        ("db.r6i.8xlarge", "db.r6i.8xlarge"),
        ("db.r6i.12xlarge", "db.r6i.12xlarge"),
        ("db.r6i.16xlarge", "db.r6i.16xlarge"),
        ("db.r6i.24xlarge", "db.r6i.24xlarge"),
        ("db.r6i.32xlarge", "db.r6i.32xlarge"),
        ("db.r7g.large", "db.r7g.large"),
        ("db.r7g.xlarge", "db.r7g.xlarge"),
        ("db.r7g.2xlarge", "db.r7g.2xlarge"),
        ("db.r7g.4xlarge", "db.r7g.4xlarge"),
        ("db.r7g.8xlarge", "db.r7g.8xlarge"),
        ("db.r7g.12xlarge", "db.r7g.12xlarge"),
        ("db.r7g.16xlarge", "db.r7g.16xlarge"),
    ]),
]

AURORA_INSTANCE_CLASSES = [
    option
    for _group, group_options in AURORA_INSTANCE_CLASS_GROUPS
    for option in group_options
]

AURORA_PARAM_APPLY_METHODS = [
    ("pending-reboot", "pending-reboot"),
    ("immediate", "immediate"),
]

AURORA_DEFAULT_PARAMETERS_BY_ENGINE = {
    engine: [dict(item) for item in profile.get("parameters", [])]
    for engine, profile in AURORA_ENGINE_PROFILES.items()
}
AURORA_DEFAULT_PARAMETERS = [
    dict(item)
    for item in AURORA_DEFAULT_PARAMETERS_BY_ENGINE[AURORA_DEFAULTS["engine"]]
]

# NOTE:
# Engine versions can be fetched from the AWS RDS API and cached (see
# `services/aws_instance_types/aurora_engine_versions.py`). These lists are kept as
# a static fallback if AWS is unavailable or credentials lack DescribeDBEngineVersions.
AURORA_ENGINE_VERSION_GROUPS_BY_ENGINE = {
    "aurora-postgresql": [
        ("Available", [
            ("15.10", "15.10"),
            ("15.9", "15.9"),
            ("14.15", "14.15"),
            ("13.18", "13.18"),
        ]),
    ],
    "aurora-mysql": [
        ("Available", [
            ("8.0", "8.0"),
            ("5.7", "5.7"),
        ]),
    ],
}


def get_aurora_engine_version_fallback_groups(engine: str | None) -> list[tuple[str, list[tuple[str, str]]]]:
    normalized = (engine or "").strip().lower()
    groups = AURORA_ENGINE_VERSION_GROUPS_BY_ENGINE.get(normalized)
    if groups:
        return [(label, list(options)) for label, options in groups]
    default_engine = AURORA_DEFAULTS["engine"]
    return [
        (label, list(options))
        for label, options in AURORA_ENGINE_VERSION_GROUPS_BY_ENGINE.get(default_engine, [])
    ]


def get_aurora_engine_version_fallback_choices(engine: str | None) -> list[tuple[str, str]]:
    groups = get_aurora_engine_version_fallback_groups(engine)
    return [option for _group, options in groups for option in options]
