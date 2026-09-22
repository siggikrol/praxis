# constants.py

AWS_REGION_NAMES = [
    ('us-east-1', 'US East (N. Virginia)'),
    ('us-east-2', 'US East (Ohio)'),
    ('us-west-1', 'US West (N. California)'),
    ('us-west-2', 'US West (Oregon)'),
    ('af-south-1', 'Africa (Cape Town)'),
    ('ap-east-1', 'Asia Pacific (Hong Kong)'),
    ('ap-south-1', 'Asia Pacific (Mumbai)'),
    ('ap-northeast-3', 'Asia Pacific (Osaka)'),
    ('ap-northeast-2', 'Asia Pacific (Seoul)'),
    ('ap-southeast-1', 'Asia Pacific (Singapore)'),
    ('ap-southeast-2', 'Asia Pacific (Sydney)'),
    ('ap-northeast-1', 'Asia Pacific (Tokyo)'),
    ('ca-central-1', 'Canada (Central)'),
    ('eu-central-1', 'EU (Frankfurt)'),
    ('eu-west-1', 'EU (Ireland)'),
    ('eu-west-2', 'EU (London)'),
    ('eu-west-3', 'EU (Paris)'),
    ('eu-north-1', 'EU (Stockholm)'),
    ('eu-south-1', 'EU (Milan)'),
    ('me-south-1', 'Middle East (Bahrain)'),
    ('sa-east-1', 'South America (São Paulo)'),
    ('us-gov-east-1', 'AWS GovCloud (US-East)'),
    ('us-gov-west-1', 'AWS GovCloud (US-West)'),
]

AWS_REGIONS = [(code, f"{code} - {name}") for code, name in AWS_REGION_NAMES]

BOOL_CHOICES = [('true', 'true'), ('false', 'false')]

DEPLOYMENTS = [
    ('ilp','ilp'), ('pmv','pmv'), ('cgs','cgs'),
    ('cr','cr'), ('vault','vault'), ('rmq','rmq'),
    ('common','common'),
]

# --- Canonical environment types (edit here) ---
ENV_TYPES = ['staging', 'uat', 'preprod', 'prod']
# Default selection to show pre-checked in the forms
ENV_DEFAULT_ENV_TYPES = ['uat', 'prod']
# Choices tuple for SelectMultipleField
ENV_TYPE_CHOICES = [(e, e) for e in ENV_TYPES]
# Legacy alias to avoid breaking older imports
ENV_LIST = ENV_TYPES

# Subnet “apps” that get their own CIDR per env (centralized)
SUBNET_DEPLOYMENTS = ['ilp', 'cgs', 'pmv', 'cr']


REDIS_NODE_TYPES = [
    ('cache.t3.small','cache.t3.small'),
    ('cache.t3.medium','cache.t3.medium'),
    ('cache.t3.large','cache.t3.large'),
    ('cache.m5.large','cache.m5.large'),
    ('cache.m5.xlarge','cache.m5.xlarge'),
    ('cache.m5.2xlarge','cache.m5.2xlarge'),
    ('cache.r6g.large','cache.r6g.large'),
    ('cache.r6g.xlarge','cache.r6g.xlarge'),
    ('cache.r6g.2xlarge','cache.r6g.2xlarge'),
]

AZ_NAMES = ['us-east-1a','us-east-1b','us-east-1c']
AZ_ZONE_IDS = ['use1-az1','use1-az2','use1-az4']

AURORA_INSTANCE_CLASSES = [
    ("db.t3.small",   "db.t3.small"),
    ("db.t3.medium",  "db.t3.medium"),
    ("db.t3.large",   "db.t3.large"),
    ("db.r5.large",   "db.r5.large"),
    ("db.r5.xlarge",  "db.r5.xlarge"),
    ("db.r5.2xlarge", "db.r5.2xlarge"),
    ("db.r6g.large",  "db.r6g.large"),
    ("db.r6g.xlarge", "db.r6g.xlarge"),
    ("db.r6g.2xlarge","db.r6g.2xlarge"),
]

# Profiles: everything except instance_class is locked by the profile.
# instance_class can be overridden per-env in the form.
AURORA_PROFILES = {
    "dev-small": {
        "engine": "aurora-postgresql",
        "engine_version": "15",
        "cluster_size": 2,
        "instance_class": "db.t4g.medium",
        "storage_gb": 100,
        "backup_retention_days": 3,
    },
    "staging-medium": {
        "engine": "aurora-postgresql",
        "engine_version": "15",
        "cluster_size": 2,
        "instance_class": "db.t4g.large",
        "storage_gb": 200,
        "backup_retention_days": 7,
    },
    "prod-ha": {
        "engine": "aurora-postgresql",
        "engine_version": "15",
        "cluster_size": 3,
        "instance_class": "db.r6g.large",
        "storage_gb": 400,
        "backup_retention_days": 14,
    },
}


VAULT_DB_INSTANCE_TYPES = [
    ("db.t3.small",   "db.t3.small"),
    ("db.t3.medium",  "db.t3.medium"),
    ("db.t3.large",   "db.t3.large"),
    ("db.r5.large",   "db.r5.large"),
    ("db.r5.xlarge",  "db.r5.xlarge"),
    ("db.r5.2xlarge", "db.r5.2xlarge"),
    ("db.r6g.large",  "db.r6g.large"),
    ("db.r6g.xlarge", "db.r6g.xlarge"),
    ("db.r6g.2xlarge","db.r6g.2xlarge"),
]



RANCHER_EKS_CLUSTERS = [
    ("eu-shared-rancher-nonprod", "eu-shared-rancher-nonprod"),
    ("ngl-rancher",               "ngl-rancher"),
    ("PRAXIS-DevOps",            "PRAXIS-DevOps"),
    ("PRAXIS-Rancher",           "PRAXIS-Rancher"),
    ("PRAXIS-Rancher-Preprod",   "PRAXIS-Rancher-Preprod"),
    ("PRAXIS-Rancher-Prod",      "PRAXIS-Rancher-Prod"),
]

# NEW: cluster -> AWS region (adjust any that differ)
RANCHER_CLUSTER_TO_REGION = {
    "eu-shared-rancher-nonprod": "eu-central-1",
    "ngl-rancher":               "eu-central-1",
    "PRAXIS-DevOps":            "us-east-1",
    "PRAXIS-Rancher":           "us-east-1",
    "PRAXIS-Rancher-Preprod":   "us-east-1",
    "PRAXIS-Rancher-Prod":      "us-east-1",
}

RANCHER_CLUSTER_TO_RANCHER_PREFIX = {
    "eu-shared-rancher-nonprod": "nonprod",
    "ngl-rancher":               "eu-shared",
    "PRAXIS-DevOps":            "devops",
    "PRAXIS-Rancher":           "us-nonprod",
    "PRAXIS-Rancher-Preprod":   "preprod",
    "PRAXIS-Rancher-Prod":      "prod",
}
# Full service URLs for the Rancher target selector. Prefixes above are shared
# with provisioning; URL shape comes from the Catalyst networking guide.
RANCHER_CLUSTER_TO_URL = {
    name: f"https://rancher-{prefix}.example.com/"
    for name, prefix in RANCHER_CLUSTER_TO_RANCHER_PREFIX.items()
}

RANCHER_PREFIXES = [
    ("nonprod",   "nonprod"),
    ("eu-shared", "eu-shared"),
    ("devops",    "devops"),
    ("us-nonprod","us-nonprod"),
    ("preprod",   "preprod"),
    ("prod",      "prod"),
]
RANCHER_GITREPO_AUTH_TYPE = [
    ("https", "https"),
    ("ssh", "ssh"),
]

RANCHER_GITHUB_AUTH = [
    ("praxis-github-ssh-key", "praxis-github-ssh-key"),
    ("praxis-github-token", "praxis-github-token"),
]

ENV_TYPE = [
    ("dev", "dev"),
    ("staging", "staging"),
    ("sit", "sit"),
    ("uat", "uat"),
    ("prod", "prod"),
]

DOMAIN_CHOICES = [
    ("example.com", "example.com"),
    ("example.org", "example.org"),
    ("eu.example.com", "eu.example.com"),
    ("ngl-stage.com", "ngl-stage.com"),
]

MONITORING_TYPES = [
    ("oss", "oss"),
    ("cloud", "cloud"),
]
# constants.py
DEFAULT_EXTERNAL_ALB_ALLOWED_IPS = [
    "82.117.198.222/32",
    "212.30.199.42/32",
    "45.153.141.100/32",
    "91.126.34.98/32",
    "109.245.58.241/32",
]
