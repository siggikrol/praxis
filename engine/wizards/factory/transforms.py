import json
import re

from engine.wizards.forms.alb_form import ALB_GROUP_KEY

def _vpc_output_keymap(d: dict) -> dict:
    """Rename wizard fields to YAML keys without affecting form logic."""
    if not isinstance(d, dict):
        return d

    env_maps = {k: v for k, v in d.items() if isinstance(v, dict)}
    if env_maps:
        out = dict(d)
        for key, value in env_maps.items():
            out[key] = _vpc_output_keymap(value)
        return out

    out = dict(d)
    if "zones" in out:
        out["availability_zones"] = out.pop("zones")
    if "zone_ids" in out:
        out["filter_az_zone_ids"] = out.pop("zone_ids")
    out.pop("custom_aws_region", None)
    return out


def _subnets_output_group(d: dict) -> dict:
    """Convert flat keys like 'staging_ilp' → nested { 'staging': { 'ilp': {...} } }."""
    if not isinstance(d, dict):
        return d

    out: dict = {}

    for k, v in d.items():
        if isinstance(k, str) and "_" in k:
            env_type, env_name = k.split("_", 1)
            out.setdefault(env_type, {})[env_name] = v
        else:
            # pass-through for any keys without _
            out[k] = v

    return out

def _transform_alb(form_data):
    """
    Convert ALB form structure into final spec structure:
    external_alb_allowed_ips:
      - ips-eu:
          - cidr1
          - cidr2
        description: xyz
    """
    groups = form_data.get("groups", [])
    out = []

    for g in groups:
        desc = str(g.get("description", "")).strip()
        raw = g.get("cidrs", "") or ""

        # split newline or comma
        cidrs = [c.strip() for c in re.split(r"[\n,]+", raw) if c.strip()]

        out.append({
            ALB_GROUP_KEY: cidrs,
            "description": desc
        })

    return {"external_alb_allowed_ips": out}


def _transform_pc_source(form_data):
    """
    Reshape pc_source form data into discrete whitelist sections and
    drop internal-only fields like profile/profile_change.
    """
    if not isinstance(form_data, dict):
        return {}

    def _clean_list(val):
        if isinstance(val, (list, tuple, set)):
            return [v for v in val if v not in (None, "")]
        return []

    bootstrap = _clean_list(form_data.get("bootstrap"))
    deployment = _clean_list(form_data.get("deployment"))
    iac = _clean_list(form_data.get("iac"))
    helm = _clean_list(form_data.get("helm"))
    helm_iac_cds = _clean_list(form_data.get("helm_iac_cds"))
    helm_app_cds = _clean_list(form_data.get("helm_app_cds"))
    helm_bootstrap_cds = _clean_list(form_data.get("helm_bootstrap_cds"))

    out = {}
    if helm:
        out["helm_whitelist"] = {"helm": helm}
    if iac:
        out["iac_whitelists"] = {"iac": iac}
    if bootstrap:
        out["bootstrap_whitelist"] = {"bootstrap": bootstrap}
    if deployment:
        out["deployment_whitelist"] = {"deployment": deployment}
    if helm_iac_cds or helm_app_cds or helm_bootstrap_cds:
        out["rancher_helm_pipelines"] = {
            "helm_iac_cds": helm_iac_cds,
            "helm_app_cds": helm_app_cds,
            "helm_bootstrap_cds": helm_bootstrap_cds,
        }

    return out


def _parse_kv_lines(raw):
    if not isinstance(raw, str):
        return {}
    out = {}
    for line in raw.splitlines():
        line = line.strip()
        if not line:
            continue
        if ":" not in line:
            continue
        key, value = line.split(":", 1)
        key = key.strip()
        value = value.strip()
        if not key or not value:
            continue
        out[key] = value
    return out


def _parse_instance_types(raw):
    if isinstance(raw, (list, tuple, set)):
        return [str(v).strip() for v in raw if str(v).strip()]
    if not isinstance(raw, str):
        return []
    parts = [p.strip() for p in raw.replace("\n", ",").split(",")]
    return [p for p in parts if p]


def _parse_list_values(raw):
    if isinstance(raw, (list, tuple, set)):
        return [str(v).strip() for v in raw if str(v).strip()]
    if not isinstance(raw, str):
        return []
    parts = [p.strip() for p in raw.replace("\n", ",").split(",")]
    return [p for p in parts if p]


def _parse_taints(raw):
    if not isinstance(raw, str):
        return {}
    out = {}
    for line in raw.splitlines():
        line = line.strip()
        if not line or ":" not in line:
            continue
        parts = [p.strip() for p in line.split(":")]
        parts = [p for p in parts if p != ""]
        if len(parts) != 3:
            continue
        key, value, effect = parts
        out[key] = {"key": key, "value": value, "effect": effect}
    return out


def _parse_bool(raw, default=False):
    if raw in (None, ""):
        return default
    if isinstance(raw, bool):
        return raw
    if isinstance(raw, (int, float)):
        return bool(raw)
    if isinstance(raw, str):
        return raw.strip().lower() in {"1", "true", "yes", "on"}
    return bool(raw)


def _group_rancher_section(section):
    if not isinstance(section, dict):
        return section

    label_fields = {
        "domain_name",
        "common_env_name",
        "env_type",
        "product",
        "customer",
        "support_organization",
        "istio",
        "vault",
        "multi_mesh",
        "dev_tools",
        "aws_account_alias",
        "rancherPrefix",
        "monitoring_type",
    }
    settings_fields = {
        "fetch_waf",
        "fetch_cert",
        "fetch_vault_db",
        "enable_meta",
        "enable_kubeconfig_secret",
        "include_rabbit_labels",
        "include_vault_labels",
        "include_rds_labels",
        "include_rds_endpoint",
        "include_vault_rds_labels",
        "include_redis_labels",
        "include_waf_labels",
    }

    core = {}
    labels = {}
    settings = {}
    annotations = {}

    for key, value in section.items():
        if key == "extra_labels":
            labels.update(_parse_kv_lines(value))
            continue
        if key == "annotations":
            annotations.update(_parse_kv_lines(value))
            continue
        if key in label_fields:
            if key == "env_type" and isinstance(value, str) and "-" in value:
                # Ensure labels.env_type stays without the suffix (e.g., 'staging' instead of 'staging-c')
                labels[key] = value.split("-")[0]
            else:
                labels[key] = value
            continue
        if key in settings_fields:
            settings[key] = value
            continue
        core[key] = value

    if labels:
        core["labels"] = labels
    if settings:
        core["settings"] = settings
    if annotations:
        core["annotations"] = annotations

    return core


def _transform_rancher(form_data):
    if not isinstance(form_data, dict):
        return form_data

    env_maps = {k: v for k, v in form_data.items() if isinstance(v, dict)}
    if env_maps:
        out = dict(form_data)
        for env_name, env_map in env_maps.items():
            out[env_name] = _group_rancher_section(env_map)
        return out

    return _group_rancher_section(form_data)


def extract_grafana_from_rancher(form_data):
    """Move the retained IAM-role Grafana flag out of the Rancher section."""
    if not isinstance(form_data, dict):
        return form_data, {}

    rancher = {}
    grafana = {}
    env_maps = {key: value for key, value in form_data.items() if isinstance(value, dict)}
    if not env_maps:
        clean = dict(form_data)
        enabled = _parse_bool(clean.pop("grafana_cloud_setup", False), default=False)
        return clean, {"grafana_cloud_setup": enabled}

    for env_name, value in form_data.items():
        if not isinstance(value, dict):
            rancher[env_name] = value
            continue
        clean = dict(value)
        enabled = _parse_bool(clean.pop("grafana_cloud_setup", False), default=False)
        rancher[env_name] = clean
        grafana[env_name] = {"grafana_cloud_setup": enabled}
    return rancher, grafana


def _build_node_group(name, instance_types_raw, min_size, max_size, desired_size,
                      capacity_type, disk_size, iam_role_use_name_prefix, ami_id,
                      labels_raw, taints_raw):
    name = (name or "").strip()
    if not name:
        return None
    instance_types = _parse_instance_types(instance_types_raw or "")
    if not instance_types:
        return None
    out = {
        "instance_types": instance_types,
        "min_size": min_size,
        "max_size": max_size,
        "desired_size": desired_size,
        "iam_role_use_name_prefix": _parse_bool(iam_role_use_name_prefix, default=False),
    }
    if capacity_type:
        out["capacity_type"] = capacity_type
    if disk_size not in (None, "", 0):
        out["disk_size"] = disk_size
    ami_id = str(ami_id or "").strip()
    if ami_id:
        out["ami_id"] = ami_id
    labels = _parse_kv_lines(labels_raw)
    if labels:
        out["labels"] = labels
    taints = _parse_taints(taints_raw)
    if taints:
        out["taints"] = taints
    return out


def _build_node_security_group_rule(rule):
    if not isinstance(rule, dict):
        return None

    name = str(rule.get("name") or "").strip()
    rule_type = str(rule.get("type") or "").strip().lower()
    protocol = str(rule.get("protocol") or "").strip().lower()
    from_port = rule.get("from_port")
    to_port = rule.get("to_port")
    source_type = str(rule.get("source_type") or "").strip()

    if not name or rule_type not in {"ingress", "egress"} or not protocol:
        return None
    if from_port in (None, "") or to_port in (None, ""):
        return None

    out = {
        "name": name,
        "type": rule_type,
        "protocol": protocol,
        "from_port": from_port,
        "to_port": to_port,
    }

    description = str(rule.get("description") or "").strip()
    if description:
        out["description"] = description

    if source_type == "source_cluster_security_group":
        out["source_cluster_security_group"] = True
    elif source_type == "self":
        out["self"] = True
    elif source_type == "source_node_security_group":
        out["source_node_security_group"] = True
    elif source_type == "cidr_blocks":
        vals = _parse_list_values(rule.get("source_values") or "")
        if vals:
            out["cidr_blocks"] = vals
    elif source_type == "ipv6_cidr_blocks":
        vals = _parse_list_values(rule.get("source_values") or "")
        if vals:
            out["ipv6_cidr_blocks"] = vals
    elif source_type == "prefix_list_ids":
        vals = _parse_list_values(rule.get("source_values") or "")
        if vals:
            out["prefix_list_ids"] = vals
    elif source_type == "source_security_group_id":
        sg_id = str(rule.get("source_security_group_id") or "").strip()
        if sg_id:
            out["source_security_group_id"] = sg_id

    return out


def _transform_eks_env(section):
    if not isinstance(section, dict):
        return section

    out = {}
    for key, val in section.items():
        if (
            key.startswith("default_node_group_")
            or key == "additional_node_groups"
            or key == "additional_node_security_group_rules"
            or key == "eks_node_security_group_additional_rules"
            or key == "bypass_node_security_group_rule_setup"
            or key == "bypass_node_security_group_rule_setup_acknowledged"
        ):
            continue
        out[key] = val

    managed = {}
    default_ami_id = section.get("default_node_group_ami_id")
    if section.get("default_node_group_enabled", True):
        ng = _build_node_group(
            section.get("default_node_group_name"),
            section.get("default_node_group_instance_types"),
            section.get("default_node_group_min_size"),
            section.get("default_node_group_max_size"),
            section.get("default_node_group_desired_size"),
            section.get("default_node_group_capacity_type"),
            section.get("default_node_group_disk_size"),
            section.get("default_node_group_iam_role_use_name_prefix"),
            section.get("default_node_group_ami_id"),
            section.get("default_node_group_labels"),
            section.get("default_node_group_taints"),
        )
        if ng:
            managed[(section.get("default_node_group_name") or "").strip()] = ng

    for group in section.get("additional_node_groups") or []:
        if not isinstance(group, dict):
            continue
        ng = _build_node_group(
            group.get("name"),
            group.get("instance_types"),
            group.get("min_size"),
            group.get("max_size"),
            group.get("desired_size"),
            group.get("capacity_type"),
            group.get("disk_size"),
            group.get("iam_role_use_name_prefix"),
            group.get("ami_id") or default_ami_id,
            group.get("labels"),
            group.get("taints"),
        )
        if ng:
            managed[(group.get("name") or "").strip()] = ng

    if managed:
        out["eks_managed_node_groups"] = managed

    sg_rules = []
    for rule in section.get("additional_node_security_group_rules") or []:
        built = _build_node_security_group_rule(rule)
        if built:
            sg_rules.append(built)

    # Backward compatibility with previous textarea JSON shape.
    if not sg_rules:
        legacy = section.get("eks_node_security_group_additional_rules")
        if isinstance(legacy, str) and legacy.strip():
            try:
                legacy = json.loads(legacy)
            except Exception:
                legacy = []
        if isinstance(legacy, list):
            for item in legacy:
                if isinstance(item, dict):
                    sg_rules.append(item)

    if sg_rules:
        out["eks_node_security_group_additional_rules"] = sg_rules

    return out


def _ensure_multi_vpc_ilp_node_groups(section: dict) -> dict:
    """Add required RMQ/Vault groups to an ILP card when they are absent."""
    section = dict(section)
    groups = [
        dict(group)
        for group in (section.get("additional_node_groups") or [])
        if isinstance(group, dict)
    ]
    names = {str(group.get("name") or "").strip().lower() for group in groups}
    default_name = str(section.get("default_node_group_name") or "ng0").strip()
    name_base = default_name[:-4] if default_name.lower().endswith("-ng0") else default_name
    instance_types = section.get("default_node_group_instance_types") or []

    for suffix, dedicated in (("rmq", "rabbitmq"), ("vault", "vault")):
        if suffix in names or any(name.endswith(f"-{suffix}") for name in names):
            continue
        groups.append(
            {
                "name": f"{name_base}-{suffix}" if name_base else suffix,
                "instance_types": instance_types,
                "min_size": 3,
                "max_size": 6,
                "desired_size": 3,
                "capacity_type": "",
                "disk_size": None,
                "iam_role_use_name_prefix": False,
                "ami_id": "",
                "labels": f"dedicated: {dedicated}",
                "taints": f"dedicated: {dedicated}: NO_SCHEDULE",
            }
        )
    section["additional_node_groups"] = groups
    return section


def _transform_eks(form_data):
    if not isinstance(form_data, dict):
        return form_data

    out = {}
    for key, val in form_data.items():
        out[key] = val

    multi_vpc_clusters = {"ilp", "cgs", "pmv"}
    for key, val in list(form_data.items()):
        if not isinstance(val, dict):
            continue
        env_key, separator, cluster = key.rpartition("_")
        if separator and env_key and cluster in multi_vpc_clusters:
            cluster_data = dict(val)
            if cluster == "ilp":
                cluster_data = _ensure_multi_vpc_ilp_node_groups(cluster_data)
            else:
                # Specialized RabbitMQ/Vault node groups belong to ILP only.
                # Enforce that invariant even if stale or crafted form data
                # submits additional groups for CGS/PMV.
                cluster_data["additional_node_groups"] = []
            transformed = _transform_eks_env(cluster_data)
            out.pop(key, None)
            out.setdefault(env_key, {})[cluster] = transformed
        else:
            out[key] = _transform_eks_env(val)
    return out


def _transform_project_settings(form_data):
    """
    Ensure environment_type list includes suffixes if defined in environment_suffixes.
    This is required for Core Validation to match resource keys.
    """
    if not isinstance(form_data, dict):
        return form_data

    out = dict(form_data)
    # Descriptive workspace metadata must never change the infrastructure schema.
    from engine.wizards.presentation import METADATA_FIELDS
    for key in METADATA_FIELDS:
        out.pop(key, None)
    env_types = out.get("environment_type")
    suffixes_raw = out.get("environment_suffixes")

    if not isinstance(env_types, list):
        return out

    suffixes = {}
    if isinstance(suffixes_raw, str) and suffixes_raw.strip():
        try:
            suffixes = json.loads(suffixes_raw)
        except Exception:
            suffixes = {}

    new_env_types = []
    for env in env_types:
        base = str(env).strip().lower()
        # If it already has a suffix, split it to re-apply consistently
        if "-" in base:
            base = base.split("-")[0]
        
        suffix = str(suffixes.get(base, "")).strip().lower()
        full_name = f"{base}-{suffix}" if suffix else base
        new_env_types.append(full_name)

    out["environment_type"] = new_env_types
    return out


TRANSFORM_MAP = {
    "alb": _transform_alb,
    "subnets": _subnets_output_group,
    "vpc": _vpc_output_keymap,
    "pc_source": _transform_pc_source,
    "rancher": _transform_rancher,
    "eks": _transform_eks,
    "project_settings": _transform_project_settings,
}
