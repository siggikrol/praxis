"""Camunda input contract and deterministic Studio-to-Core spec conversion."""
import ipaddress
import re
from copy import deepcopy

MODULE = "praxis_camunda_opensearch"
LOG_TYPES = ("INDEX_SLOW_LOGS", "SEARCH_SLOW_LOGS", "ES_APPLICATION_LOGS", "AUDIT_LOGS")
# Defaults from the inspected wrapper, not the customer-specific UAT context.
DEFAULTS = {
    "eks_camunda_namespace": "cw8",
    "eks_camunda_zeebe_service_account": "c8-zeebe",
    "eks_camunda_optimize_service_account": "c8-optimize",
    "opensearch_engine_version": "3.3",
    "opensearch_instance_type": "r7g.large.search",
    "opensearch_instance_count": 3,
    "opensearch_master_type": "m7g.medium.search",
    "opensearch_master_count": 3,
    "opensearch_ebs_volume_type": "gp3",
    "opensearch_ebs_volume_size": 50,
    "opensearch_ebs_iops": 3000,
    "opensearch_ebs_throughput": 125,
    "opensearch_custom_endpoint_enabled": False,
    "opensearch_service_linked_role_propagation_delay": "120s",
    "opensearch_off_peak_window_start_time_hours": 0,
    "opensearch_off_peak_window_start_time_minutes": 0,
    "opensearch_auto_tune_desired_state": "DISABLED",
    "opensearch_auto_tune_rollback_on_disable": "NO_ROLLBACK",
    "opensearch_enabled_logs": list(LOG_TYPES),
    "opensearch_additional_cidr_blocks": ["10.0.0.0/8"],
}
STRING_INPUTS = (
    "eks_camunda_namespace", "eks_camunda_zeebe_service_account",
    "eks_camunda_optimize_service_account", "opensearch_domain_name",
    "opensearch_custom_endpoint", "opensearch_custom_endpoint_certificate_id",
    "opensearch_master_user_secret_name",
)
INPUTS = frozenset(DEFAULTS) | set(STRING_INPUTS) | {"opensearch_create_iam_service_linked_role"}
SHARED_INPUTS = (
    "eks_camunda_namespace",
    "eks_camunda_zeebe_service_account",
    "eks_camunda_optimize_service_account",
    "opensearch_engine_version",
    "opensearch_enabled_logs",
    "opensearch_auto_tune_desired_state",
    "opensearch_auto_tune_rollback_on_disable",
    "opensearch_service_linked_role_propagation_delay",
)
INTEGER_BOUNDS = {
    "opensearch_instance_count": (1, None), "opensearch_master_count": (1, None),
    "opensearch_ebs_volume_size": (1, None), "opensearch_ebs_iops": (0, None),
    "opensearch_ebs_throughput": (0, None),
    "opensearch_off_peak_window_start_time_hours": (0, 23),
    "opensearch_off_peak_window_start_time_minutes": (0, 59),
}


def normalize(values):
    result = dict(values)
    for key, value in result.items():
        if isinstance(value, str):
            result[key] = value.strip()
    result.setdefault("opensearch_additional_cidr_blocks", list(DEFAULTS["opensearch_additional_cidr_blocks"]))
    cidrs = result["opensearch_additional_cidr_blocks"]
    if isinstance(cidrs, str):
        result["opensearch_additional_cidr_blocks"] = [v.strip() for v in re.split(r"[,\n]", cidrs) if v.strip()]
    role = result.get("opensearch_create_iam_service_linked_role")
    if role in ("true", "false"):
        result["opensearch_create_iam_service_linked_role"] = role == "true"
    return result


def validate_config(values, multi=False):
    """Validate saved state too, not only WTForms POST data."""
    errors = {}
    def error(key, message):
        errors.setdefault(key, []).append(message)

    if not isinstance(values, dict):
        return {"configuration": ["Configuration must be a mapping."]}
    cfg = normalize(values)
    for key in INPUTS - set(INTEGER_BOUNDS) - {"opensearch_additional_cidr_blocks", "opensearch_enabled_logs", "opensearch_custom_endpoint_enabled", "opensearch_create_iam_service_linked_role"}:
        if key.startswith("opensearch_custom_endpoint") or key == "opensearch_master_user_secret_name":
            continue
        if not isinstance(cfg.get(key), str) or not cfg[key]:
            error(key, "This value is required.")
    for key, (minimum, maximum) in INTEGER_BOUNDS.items():
        value = cfg.get(key)
        if type(value) is not int or value < minimum or (maximum is not None and value > maximum):
            error(key, f"Enter an integer from {minimum}" + (f" to {maximum}." if maximum is not None else " or greater."))
    for key in ("opensearch_custom_endpoint_enabled", "opensearch_create_iam_service_linked_role"):
        if type(cfg.get(key)) is not bool:
            error(key, "Choose an explicit value.")
    if multi and cfg.get("cluster_target") not in ("ilp", "cgs", "pmv"):
        error("cluster_target", "Select the EKS cluster hosting Camunda.")
    patterns = {
        "eks_camunda_namespace": r"[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?",
        "eks_camunda_zeebe_service_account": r"[a-z0-9](?:[a-z0-9.-]{0,251}[a-z0-9])?",
        "eks_camunda_optimize_service_account": r"[a-z0-9](?:[a-z0-9.-]{0,251}[a-z0-9])?",
        "opensearch_domain_name": r"[a-z][a-z0-9-]{2,27}",
        "opensearch_engine_version": r"[0-9]+\.[0-9]+",
        "opensearch_instance_type": r"[a-z][a-z0-9]*\.[a-z0-9]+\.search",
        "opensearch_master_type": r"[a-z][a-z0-9]*\.[a-z0-9]+\.search",
        "opensearch_service_linked_role_propagation_delay": r"(?:[0-9]+(?:\.[0-9]+)?(?:ms|s|m|h))+",
        "opensearch_master_user_secret_name": r"[A-Za-z0-9/_+=.@-]+-[A-Za-z0-9]{6}",
    }
    for key, pattern in patterns.items():
        if cfg.get(key) and (not isinstance(cfg[key], str) or not re.fullmatch(pattern, cfg[key])):
            error(key, "Invalid format." if key != "opensearch_master_user_secret_name" else "Enter the secret reference including its six-character generated suffix, not an ARN or credentials.")
    if cfg.get("opensearch_custom_endpoint_enabled") is True:
        endpoint = cfg.get("opensearch_custom_endpoint", "")
        if not isinstance(endpoint, str) or len(endpoint) > 253 or not re.fullmatch(r"(?:[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?\.)+[a-z]{2,63}", endpoint):
            error("opensearch_custom_endpoint", "Enter a hostname without https:// or a path.")
        certificate = cfg.get("opensearch_custom_endpoint_certificate_id", "")
        if certificate and (not isinstance(certificate, str) or not re.fullmatch(r"[0-9a-fA-F]{8}(?:-[0-9a-fA-F]{4}){3}-[0-9a-fA-F]{12}", certificate)):
            error("opensearch_custom_endpoint_certificate_id", "Enter the ACM certificate ID (UUID), not its ARN.")
    choices = {
        "opensearch_ebs_volume_type": {"gp2", "gp3", "io1", "standard"},
        "opensearch_auto_tune_desired_state": {"ENABLED", "DISABLED"},
        "opensearch_auto_tune_rollback_on_disable": {"NO_ROLLBACK", "DEFAULT_ROLLBACK"},
    }
    for key, allowed in choices.items():
        if not isinstance(cfg.get(key), str) or cfg[key] not in allowed:
            error(key, "Select a supported value.")
    logs = cfg.get("opensearch_enabled_logs")
    if not isinstance(logs, list) or any(log not in LOG_TYPES for log in logs):
        error("opensearch_enabled_logs", "Select supported log types.")
    cidrs = cfg.get("opensearch_additional_cidr_blocks")
    try:
        if not isinstance(cidrs, list):
            raise ValueError()
        for cidr in cidrs:
            if not isinstance(cidr, str):
                raise ValueError()
            ipaddress.IPv4Network(cidr, strict=True)
    except (ValueError, TypeError):
        error("opensearch_additional_cidr_blocks", "Enter additional approved IPv4 network CIDRs, one per line; network stack subnet CIDRs are included automatically.")
    return errors


def effective_config(raw, env):
    """Shared defaults plus explicit environment overrides; accept older saved cards."""
    values = raw.get(env)
    if not isinstance(values, dict):
        return values
    shared = {key: raw[key] for key in SHARED_INPUTS if key in raw}
    if not shared:
        return values
    local = dict(values)
    if local.get("override_shared_settings") is not True:
        local = {key: value for key, value in local.items() if key not in SHARED_INPUTS}
    return {**shared, **local}


def migrate_form_state(raw, env_names):
    """Lift old repeated fields without losing differing environment choices."""
    result = deepcopy(raw or {})
    if not any(key in result for key in SHARED_INPUTS):
        first = next((result[env] for env in env_names if isinstance(result.get(env), dict)), {})
        for key in SHARED_INPUTS:
            if key in first:
                result[key] = deepcopy(first[key])
        for env in env_names:
            local = result.get(env)
            if isinstance(local, dict):
                local["override_shared_settings"] = any(
                    key in local and local[key] != result.get(key) for key in SHARED_INPUTS
                )
    for key in ("eks_camunda_namespace", "eks_camunda_zeebe_service_account", "eks_camunda_optimize_service_account"):
        if not result.get(key):
            result[key] = DEFAULTS[key]
    shared = {key: result[key] for key in SHARED_INPUTS if key in result}
    for env in env_names:
        local = result.get(env, {})
        if isinstance(local, dict):
            result[env] = {**shared, **local}
    return result


def suggested_custom_endpoint(common, env):
    """Use the shared environment identity, including its selected suffix."""
    common = common or {}
    prefix = str(common.get("environment") or "").strip()
    domain = str(common.get("domain_name") or "").strip().strip(".")
    return f"c8-opensearch.{prefix}-{env}.int.{domain}" if prefix and domain else ""


def build_spec(raw, env_names, multi=False, common=None):
    """Emit only current environments and contract inputs, never shared EKS identity."""
    output, errors = {}, []
    raw = raw if isinstance(raw, dict) else {}
    for env in env_names:
        values = effective_config(raw, env)
        if isinstance(values, dict) and not values.get("opensearch_custom_endpoint"):
            values = {**values, "opensearch_custom_endpoint": suggested_custom_endpoint(common, env)}
        if isinstance(values, dict) and values.get("opensearch_cidr_blocks"):
            errors.append(f"Camunda OpenSearch ({env}): legacy CIDR override requires review. Move only additional approved worker ranges to opensearch_additional_cidr_blocks and clear opensearch_cidr_blocks; subnet ranges now come from the network stack.")
            continue
        issues = validate_config(values, multi)
        for field, messages in issues.items():
            errors.extend(f"Camunda OpenSearch ({env}): {field}: {message}" for message in messages)
        if issues:
            continue
        cfg = normalize(values)
        inputs = {key: cfg[key] for key in sorted(INPUTS) if key in cfg}
        if not inputs["opensearch_custom_endpoint_enabled"]:
            inputs.pop("opensearch_custom_endpoint", None)
            inputs.pop("opensearch_custom_endpoint_certificate_id", None)
        inputs.pop("opensearch_custom_endpoint_certificate_id", None)
        # Absence selects wrapper-owned creation; never export empty placeholders.
        for name in ("opensearch_master_user_secret_name", "opensearch_custom_endpoint_certificate_id"):
            if not inputs.get(name):
                inputs.pop(name, None)
        # Core's contract collector understands <environment>.<deployment>.
        output[env] = {cfg["cluster_target"]: inputs} if multi else inputs
    return output, errors
