# modules/wizards/constants/project_constants.py
# Generic configuration constants for Project Settings forms

# Default deployment modules that can be selected in Multi-VPC wizard
ENV_TYPES = ['staging', 'uat', 'preprod', 'prod']
UNIFIED_ENV_TYPES = [
    'dev', 'test', 'sit', 'showroom', 'training', 'staging', 'uat', 'preprod', 'prod'
]


ENV_DEFAULT_ENV_TYPES = ['uat', 'prod']

ENV_TYPE_CHOICES = [(e, e) for e in ENV_TYPES]
from engine.wizards.presentation import ENVIRONMENT_LABELS
UNIFIED_ENV_TYPES_CHOICES = [(e, ENVIRONMENT_LABELS.get(e, e.title())) for e in UNIFIED_ENV_TYPES]


PROJECT_DEPLOYMENT_CHOICES = [
    ("ilp", "ilp"),
    ("pmv", "pmv"),
    ("cgs", "cgs"),
    ("cr", "cr"),
    ("vault", "vault"),
    ("rmq", "rmq"),
    ("common", "common"),
    ("rgs", "rgs"),    
]

# Default selections for deployment modules
PROJECT_DEPLOYMENT_DEFAULTS = ["ilp", "pmv", "cgs", "cr", "vault", "rmq", "common"]
SINGLE_VPC_DEPLOYMENT_DEFAULTS = ["ilp", "pmv", "cgs", "rmq", "common"]

# Environment type defaults and validation patterns
PROJECT_DEFAULT_SUFFIX = "ctlst"

PROJECT_ENVIRONMENT_HELP = (
    "Only the base prefix, e.g. 'acme-ctlst'. "
    "SSA will append environment parts automatically "
    "(e.g. 'ilp-acme-ctlst-uat')."
)
