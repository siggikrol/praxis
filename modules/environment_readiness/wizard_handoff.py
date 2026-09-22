from __future__ import annotations

import json
import os
from copy import deepcopy
from typing import Any

from engine.wizards.constants.replacements_constants import (
    RANCHER_CONTEXT_TO_CLUSTER,
    RANCHER_CONTEXT_TO_INTEGRATION,
    rancher_worker_pool_id_for_context,
)


SETUP_WIZARDS = {
    "catalyst-single-vpc": ("single-vpc", "Catalyst Single-VPC"),
    "catalyst-multi-vpc": ("multi-vpc", "Catalyst Multi-VPC"),
    "rgs": ("rgs", "RGS"),
    "loyalty": ("loyalty", "Loyalty"),
}


def wizard_for_setup(setup_type: str) -> tuple[str, str] | None:
    return SETUP_WIZARDS.get(str(setup_type or "").strip())


def _text(data: dict[str, Any], key: str) -> str:
    return str(data.get(key) or "").strip()


def _base_environment(data: dict[str, Any]) -> str:
    name = _text(data, "environment_name").lower()
    env_type = _text(data, "environment_type").lower()
    suffix = _text(data, "environment_suffix").lower()
    ending = f"-{env_type}" + (f"-{suffix}" if suffix else "")
    return name[: -len(ending)] if ending and name.endswith(ending) else name


def build_wizard_state(
    data: dict[str, Any], env_slug: str, bootstrap: dict[str, Any] | None = None
) -> dict[str, Any]:
    """Translate an immutable readiness snapshot into native wizard session state."""
    env_type = _text(data, "environment_type").lower()
    suffix = _text(data, "environment_suffix").lower()
    env_key = f"{env_type}-{suffix}" if suffix else env_type
    customer = _text(data, "customer")
    product = _text(data, "product")
    domain = _text(data, "dns_domain")
    base_environment = _base_environment(data)
    rancher_target = _text(data, "rancher_target")
    rancher_context = next(
        (context for context, cluster in RANCHER_CONTEXT_TO_CLUSTER.items() if cluster == rancher_target),
        "",
    )
    bootstrap = bootstrap if isinstance(bootstrap, dict) else {}

    state: dict[str, Any] = {
        "release_selection": {
            # Release bundles are stored below normalized customer folders.
            "customer": customer.lower(),
            "release_key": _text(data, "release_archive"),
        },
        f"{env_slug}:project_settings": {
            "environment_type": [env_type],
            "environment_suffixes": json.dumps({env_type: suffix} if suffix else {}),
        },
        f"{env_slug}:repository_settings": {
            "new_prefix": base_environment,
            "repo_description": f"PRAXIS {product} {customer} Setup",
        },
        f"{env_slug}:replacements": {
            "ssa_prefix": base_environment,
            env_key: {
                "ssa_rancher_context": rancher_context,
                "ssa_rancher_cloud_integration": RANCHER_CONTEXT_TO_INTEGRATION.get(rancher_context, ""),
                "ssa_rancher_worker_pool_id": rancher_worker_pool_id_for_context(rancher_context) if rancher_context else "",
                "ssa_iac_git_branch": env_key,
            },
        },
        f"{env_slug}:common": {
            "aws_region": _text(data, "aws_region"),
            "environment": base_environment,
            "customer": customer,
            "product": product,
            "domain_name": domain,
            "support_organization": os.getenv("PS_SUPPORT_ORGANIZATION", "PRAXIS"),
        },
        f"{env_slug}:subnets": {},
        f"{env_slug}:eks": {
            "eks_cluster_version": _text(data, "eks_version"),
        },
        f"{env_slug}:aurora": {
            "engine_version": _text(data, "aurora_version"),
            env_key: {"aurora_instance_class": _text(data, "aurora_instance_type")},
        },
        f"{env_slug}:redis": {
            env_key: {"redis_instance_type": _text(data, "redis_instance_type")},
        },
        f"{env_slug}:rancher": {
            env_key: {
                "rancher_cluster_name": rancher_target,
                "domain_name": domain,
                "common_env_name": base_environment,
                "customer": customer,
                "product": product,
                "support_organization": os.getenv("PS_SUPPORT_ORGANIZATION", "PRAXIS"),
                "env_type": env_type,
            },
        },
    }
    # Keep the published profile rather than implicitly upgrading old snapshots.
    if data.get("component_profile") and env_slug in {"single-vpc", "multi-vpc"}:
        state[f"{env_slug}:component_profile"] = deepcopy(data["component_profile"])

    spacelift_space_id = _text(bootstrap, "spacelift_space_id")
    aws_integration_id = _text(bootstrap, "aws_integration_id")
    if spacelift_space_id and aws_integration_id:
        state[f"{env_slug}:spacelift_settings"] = {
            env_key: {
                "aws_integration_id": aws_integration_id,
                "spacelift_space_id": spacelift_space_id,
                "description": f"Spacelift setup for {spacelift_space_id}",
            }
        }

    if env_slug == "multi-vpc":
        for deployment in ("ilp", "cgs", "pmv", "cr"):
            state[f"{env_slug}:subnets"][f"{env_key}_{deployment}"] = {
                "vpc_cidr_block": _text(data, f"vpc_cidr_{deployment}")
            }
    else:
        state[f"{env_slug}:subnets"][env_key] = {
            "vpc_cidr_block": _text(data, "vpc_cidr")
        }

    instance_type = _text(data, "eks_instance_types")
    eks_targets = [env_key]
    if env_slug == "multi-vpc":
        eks_targets = [f"{env_key}_{cluster}" for cluster in ("ilp", "cgs", "pmv")]
    for target in eks_targets:
        state[f"{env_slug}:eks"][target] = {
            "default_node_group_instance_types": [instance_type]
        }

    if env_slug in {"single-vpc", "multi-vpc"}:
        state[f"{env_slug}:vault_database"] = {
            "vault_database_engine_version": _text(data, "vault_database_version"),
            env_key: {
                "vault_database_instance_type": _text(data, "vault_database_instance_type")
            },
        }

    from .camunda import enabled as camunda_enabled, FIELDS as CAMUNDA_FIELDS
    if camunda_enabled(data):
        local = {}
        for key, _label in CAMUNDA_FIELDS:
            value = _text(data, key)
            if value and key != "opensearch_engine_version":
                local[key] = int(value) if key.endswith("_count") else value
        state[f"{env_slug}:camunda_opensearch"] = {
            "opensearch_engine_version": _text(data, "opensearch_engine_version"),
            env_key: local,
        }

    return state


def merge_wizard_state(current: dict[str, Any], imported: dict[str, Any]) -> dict[str, Any]:
    """Overlay readiness-owned values while retaining wizard-only choices."""
    merged = dict(current)
    for key, value in imported.items():
        if key.endswith(":component_profile"):
            merged[key] = deepcopy(value)
        elif isinstance(value, dict) and isinstance(merged.get(key), dict):
            merged[key] = merge_wizard_state(merged[key], value)
        else:
            merged[key] = value
    return merged
