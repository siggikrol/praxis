# envs/wizard/validators.py
from __future__ import annotations
from typing import Dict, List
from constants import SUBNET_DEPLOYMENTS

def validate_for_context_generator(cfg: dict) -> List[str]:
    """
    Copy of your semantic checks – unchanged in spirit.
    """
    errors: List[str] = []
    spec_type = str(cfg.get("spec_type") or "").strip().lower()
    requires_redis = spec_type != "loyalty"

    required_sections = [
        "project_settings", "common", "subnets",
        "vpc", "rancher", "eks", "aurora"
    ]
    if requires_redis:
        required_sections.append("redis")
    for sec in required_sections:
        if sec not in cfg:
            errors.append(f"Missing top-level section: {sec}")
    if errors:
        return errors

    ps = cfg.get("project_settings", {})
    common = cfg.get("common", {})
    subnets = cfg.get("subnets", {})
    vpc = cfg.get("vpc", {})
    rancher = cfg.get("rancher", {})
    eks = cfg.get("eks", {})
    aurora = cfg.get("aurora", {})
    redis = cfg.get("redis", {})
    external_alb_allowed_ips = cfg.get("external_alb_allowed_ips", [])

    et = ps.get("environment_type")
    suffixes_raw = ps.get("environment_suffixes", {})

    import json
    if isinstance(suffixes_raw, str):
        try:
            suffixes = json.loads(suffixes_raw)
        except Exception:
            suffixes = {}
    else:
        suffixes = suffixes_raw if isinstance(suffixes_raw, dict) else {}

    if not et:
        errors.append("project_settings.environment_type is required (list like ['uat','prod']).")
        et_list = []
        full_et_list = []
    else:
        et_list = et if isinstance(et, list) else [et]
        bad = [x for x in et_list if not isinstance(x, str)]
        if bad:
            errors.append("project_settings.environment_type must be a list of strings.")
        
        full_et_list = []
        for e in et_list:
            if not isinstance(e, str):
                continue
            suffix = suffixes.get(e, "")
            full_et_list.append(f"{e}-{suffix}" if suffix else e)

    for key in ("aws_region", "environment", "domain_name", "support_organization", "customer", "product"):
        if not common.get(key):
            errors.append(f"common.{key} is required.")

    def _validate_vpc_section(section: dict, prefix: str):
        for key in ("transit_gateway_id", "vpc_endpoint_service"):
            if not section.get(key):
                errors.append(f"{prefix}{key} is required.")

    if not isinstance(vpc, dict):
        errors.append("vpc must be a mapping.")
    else:
        vpc_envs = [k for k, v in vpc.items() if isinstance(v, dict)]
        if vpc_envs:
            envs_to_check = full_et_list or vpc_envs
            for env_name in envs_to_check:
                env_map = vpc.get(env_name)
                if not isinstance(env_map, dict):
                    errors.append(f"vpc.{env_name} must be a mapping.")
                    continue
                _validate_vpc_section(env_map, f"vpc.{env_name}.")
            for env_name in envs_to_check:
                if env_name not in vpc:
                    errors.append(
                        f"vpc.{env_name} section is required for environment_type '{env_name}'."
                    )
        else:
            _validate_vpc_section(vpc, "vpc.")

    rancher_keys = [
        "rancher_cluster_name", "rancher_aws_region",
        "helm_git_branch", "helm_app_git_branch", "helm_bootstrap_git_branch",
        "multi_mesh", "aws_account_alias", "rancherPrefix",
    ]

    def _rancher_value(section: dict, key: str):
        if key in section:
            return section.get(key)
        labels = section.get("labels")
        if isinstance(labels, dict) and key in labels:
            return labels.get(key)
        settings = section.get("settings")
        if isinstance(settings, dict) and key in settings:
            return settings.get(key)
        return None

    def _validate_rancher_section(section: dict, prefix: str):
        for key in rancher_keys:
            val = _rancher_value(section, key)
            if val in (None, ""):
                errors.append(f"{prefix}{key} is required.")

    if not isinstance(rancher, dict):
        errors.append("rancher must be a mapping.")
    else:
        top_has_required = any(k in rancher for k in rancher_keys)
        nested_envs = [k for k, v in rancher.items() if isinstance(v, dict)]
        if top_has_required and not nested_envs:
            _validate_rancher_section(rancher, "rancher.")
        elif not top_has_required and nested_envs:
            envs_to_check = full_et_list or list(rancher.keys())
            for et_name in envs_to_check:
                et_map = rancher.get(et_name)
                if not isinstance(et_map, dict):
                    errors.append(f"rancher.{et_name} must be a mapping.")
                    continue
                _validate_rancher_section(et_map, f"rancher.{et_name}.")
            for et_name in envs_to_check:
                if et_name not in rancher:
                    errors.append(f"rancher.{et_name} section is required for environment_type '{et_name}'.")
        elif top_has_required and nested_envs:
            _validate_rancher_section(rancher, "rancher.")
            for et_name in nested_envs:
                et_map = rancher.get(et_name) or {}
                if not isinstance(et_map, dict):
                    errors.append(f"rancher.{et_name} must be a mapping.")
                    continue
                _validate_rancher_section(et_map, f"rancher.{et_name}.")
        else:
            errors.append("rancher must contain required keys or per-environment sections.")

    eks_core = [
        "eks_cluster_version",
        "eks_ssh_allow_access_from_cidrs",
        "eks_endpoint_private_access",
        "eks_endpoint_public_access",
        "eks_endpoint_public_access_cidrs",
        "eks_api_allow_access_from_cidrs",
        "enable_load_balancer_controller_irsa",
        "enable_cluster_autoscaler_irsa",
        "enable_external_secrets_irsa",
        "enable_external_dns_irsa",
        "enable_rancher_access",
        "ami_type",
    ]
    for key in eks_core:
        if key not in eks:
            errors.append(f"eks.{key} is required.")

    # EKS node group validation (per-env or top-level). Multi-VPC specs scope
    # node groups one level deeper, under each real EKS cluster target:
    # eks.<environment>.<cluster>.eks_managed_node_groups.
    if isinstance(eks, dict):
        env_maps = [k for k, v in eks.items() if isinstance(v, dict)]
        if env_maps:
            envs_to_check = full_et_list or env_maps
            for env_name in envs_to_check:
                env_map = eks.get(env_name)
                if not isinstance(env_map, dict):
                    errors.append(f"eks.{env_name} must be a mapping.")
                    continue
                ngs = env_map.get("eks_managed_node_groups")
                if isinstance(ngs, dict) and ngs:
                    continue

                if spec_type in {"multi-vpc", "multi_vpc"}:
                    infrastructure = cfg.get("infrastructure") or {}
                    clusters = infrastructure.get("clusters") if isinstance(infrastructure, dict) else None
                    if isinstance(clusters, dict):
                        cluster_names = [str(name).strip().lower() for name in clusters if str(name).strip()]
                    elif isinstance(clusters, list):
                        cluster_names = [str(name).strip().lower() for name in clusters if str(name).strip()]
                    else:
                        deployments = ps.get("deployments") or []
                        if isinstance(deployments, str):
                            deployments = [deployments]
                        non_cluster_targets = {"common", "vault", "rmq", "cr"}
                        cluster_names = [
                            str(name).strip().lower()
                            for name in deployments
                            if str(name).strip().lower() not in non_cluster_targets
                        ]
                    if not cluster_names:
                        cluster_names = [
                            str(name).strip().lower()
                            for name, value in env_map.items()
                            if isinstance(value, dict) and str(name).strip()
                        ]

                    if not cluster_names:
                        errors.append(f"eks.{env_name} must define at least one EKS cluster node-group mapping.")
                        continue

                    for cluster_name in dict.fromkeys(cluster_names):
                        cluster_map = env_map.get(cluster_name)
                        cluster_ngs = (
                            cluster_map.get("eks_managed_node_groups")
                            if isinstance(cluster_map, dict)
                            else None
                        )
                        if not isinstance(cluster_ngs, dict) or not cluster_ngs:
                            errors.append(
                                f"eks.{env_name}.{cluster_name}.eks_managed_node_groups is required."
                            )
                else:
                    errors.append(f"eks.{env_name}.eks_managed_node_groups is required.")
        else:
            ngs = eks.get("eks_managed_node_groups")
            if not isinstance(ngs, dict) or not ngs:
                errors.append("eks.eks_managed_node_groups is required.")

    # --- Aurora validation: top-level engine/version required; instance class can be per-env ---
    if "engine" not in aurora:
        errors.append("aurora.engine is required.")
    if "engine_version" not in aurora:
        errors.append("aurora.engine_version is required.")

    def _validate_env_classes(section: dict, key_required: str) -> None:
        if not isinstance(section, dict):
            errors.append(f"{key_required} section must be a mapping.")
            return
        env_keys = [k for k, v in section.items() if isinstance(v, dict)]
        if env_keys:
            envs_to_check = full_et_list or env_keys
            for env in envs_to_check:
                env_map = section.get(env)
                if not isinstance(env_map, dict):
                    errors.append(f"{key_required}.{env} must be a mapping.")
                    continue
                if "aurora_instance_class" not in env_map or not str(env_map.get("aurora_instance_class", "")).strip():
                    errors.append(f"{key_required}.{env}.aurora_instance_class is required.")
        else:
            if "aurora_instance_class" not in section or not str(section.get("aurora_instance_class", "")).strip():
                errors.append(f"{key_required}.aurora_instance_class is required.")

    _validate_env_classes(aurora, "aurora")

    # --- Redis validation: redis_instance_type/redis_cluster_size can be per-env ---
    def _validate_redis(section: dict) -> None:
        if not isinstance(section, dict):
            errors.append("redis must be a mapping.")
            return
        env_keys = [k for k, v in section.items() if isinstance(v, dict)]
        if env_keys:
            envs_to_check = full_et_list or env_keys
            for env in envs_to_check:
                env_map = section.get(env)
                if not isinstance(env_map, dict):
                    errors.append(f"redis.{env} must be a mapping.")
                    continue
                if "redis_instance_type" not in env_map or not str(env_map.get("redis_instance_type", "")).strip():
                    errors.append(f"redis.{env}.redis_instance_type is required.")
                if "redis_cluster_size" not in env_map or not str(env_map.get("redis_cluster_size", "")).strip():
                    errors.append(f"redis.{env}.redis_cluster_size is required.")
        else:
            if "redis_instance_type" not in section or not str(section.get("redis_instance_type", "")).strip():
                errors.append("redis.redis_instance_type is required.")
            if "redis_cluster_size" not in section or not str(section.get("redis_cluster_size", "")).strip():
                errors.append("redis.redis_cluster_size is required.")

    if "redis" in cfg:
        _validate_redis(redis)

    if "external_alb_allowed_ips" in cfg:
        if not isinstance(external_alb_allowed_ips, list) or not external_alb_allowed_ips:
            errors.append("external_alb_allowed_ips must contain at least one group.")
        else:
            for idx, group in enumerate(external_alb_allowed_ips, start=1):
                if not isinstance(group, dict):
                    errors.append(f"external_alb_allowed_ips[{idx}] must be a mapping.")
                    continue
                description = str(group.get("description") or "").strip()
                cidrs = group.get("ips")
                if not description:
                    errors.append(f"external_alb_allowed_ips[{idx}].description is required.")
                if not isinstance(cidrs, list) or not [cidr for cidr in cidrs if str(cidr).strip()]:
                    errors.append(f"external_alb_allowed_ips[{idx}].ips must contain at least one CIDR.")

    if not isinstance(subnets, dict) or not subnets:
        errors.append("subnets must be a non-empty mapping.")
    else:
        for env_type in full_et_list:
            et_map = subnets.get(env_type)
            if not isinstance(et_map, dict):
                errors.append(f"subnets.{env_type} must be a mapping.")
                continue

            # Allow either single CIDR per env (DefaultSubnetsForm) or per-deployment CIDRs (SubnetsForm).
            if "vpc_cidr_block" in et_map:
                cidr = str(et_map.get("vpc_cidr_block", "")).strip()
                if not cidr:
                    errors.append(f"subnets.{env_type}.vpc_cidr_block is required.")
                continue

            for dep in SUBNET_DEPLOYMENTS:
                v = et_map.get(dep, {})
                if not isinstance(v, dict) or not str(v.get("vpc_cidr_block", "")).strip():
                    errors.append(f"subnets.{env_type}.{dep}.vpc_cidr_block is required.")

    deployments = cfg.get("project_settings", {}).get("deployments")
    disabled_sections = set(cfg.get("_disabled_sections") or [])
    if deployments and isinstance(deployments, list) and "ilp" in deployments:
        for sec in ("external_alb_allowed_ips", "formkiq"):
            if sec in disabled_sections:
                continue
            if sec not in cfg:
                errors.append(f"Missing section required for ILP: {sec}")

    if deployments and isinstance(deployments, list) and "vault" in deployments:
        if "vault_database" in disabled_sections:
            return errors
        if "vault_database" not in cfg:
            errors.append("Missing section required for VAULT: vault_database")

    return errors
