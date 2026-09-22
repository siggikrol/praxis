# --- Vault Database Constants ---

# Available RDS instance types for Vault
VAULT_DB_INSTANCE_TYPES = [
    ("db.t4g.small", "db.t4g.small"),
    ("db.t4g.medium", "db.t4g.medium"),
    ("db.r6g.large", "db.r6g.large"),
    ("db.r6g.xlarge", "db.r6g.xlarge"),
    ("db.r6g.2xlarge", "db.r6g.2xlarge"),
]

# PostgreSQL versions are discovered from AWS RDS. There is deliberately no
# static version fallback because retired patch versions cannot be used to
# discover orderable instance classes.
VAULT_DB_ENGINE_VERSION_GROUPS = []
VAULT_DB_ENGINE_VERSIONS = []

# Default configuration for Vault database
VAULT_DB_DEFAULTS = {
    "admin_user": "rds_admin",
    "instance_type": "db.r6g.xlarge",
    "family": "postgres15",
    "engine_version": "",
    "allocated_storage": 100,
}

# Default DB parameter group entries
VAULT_DB_DEFAULT_PARAMETERS = [
    {"name": "shared_preload_libraries", "value": "pgaudit,pg_stat_statements", "apply_method": "pending-reboot"},
    {"name": "pgaudit.log", "value": "function,role,ddl,misc", "apply_method": "immediate"},
    {"name": "pgaudit.role", "value": "rds_pgaudit", "apply_method": "pending-reboot"},
    {"name": "log_connections", "value": "1", "apply_method": "immediate"},
    {"name": "rds.force_ssl", "value": "1", "apply_method": "pending-reboot"},
    {"name": "max_connections", "value": "5000", "apply_method": "pending-reboot"},
    {"name": "track_commit_timestamp", "value": "1", "apply_method": "pending-reboot"},
]
