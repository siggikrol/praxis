from __future__ import annotations

import io
from textwrap import dedent, indent
from typing import Any

import yaml

from constants import (
    DOMAIN_CHOICES,
    RANCHER_CLUSTER_TO_RANCHER_PREFIX,
    RANCHER_CLUSTER_TO_REGION,
)
from engine.wizards.constants.project_constants import UNIFIED_ENV_TYPES
from engine.wizards.constants.vpc_constants import VPC_AZ_PROFILES
from engine.wizards.constants.replacements_constants import (
    RANCHER_CONTEXT_TO_INTEGRATION,
    rancher_worker_pool_id_for_context,
)

TEMPLATE_PREFIX_SUFFIXES = {
    "rgs": "rgs",
    "single-vpc": "ctlst",
    "multi-vpc": "ctlst",
    "loyalty": "playon",
}


def list_wizard_templates(
    default_pc_version: str = "",
    release_manifest: dict[str, Any] | None = None,
    vpc_profile: str = "",
    template_envs: list[str] | None = None,
    template_prefix: str = "",
    template_domain: str = "",
    rancher_cluster: str = "",
    rancher_context: str = "",
) -> list[dict[str, str]]:
    templates = [
        {
            "key": "loyalty",
            "label": "Loyalty Template",
            "description": "Starter YAML scaffold for the Loyalty wizard flow.",
            "draft_title": "loyalty-template",
            "spec_yaml": _loyalty_template(
                default_pc_version,
                vpc_profile=vpc_profile,
                template_envs=template_envs,
                template_prefix=template_prefix,
                template_domain=template_domain,
                rancher_cluster=rancher_cluster,
            ),
        },
        {
            "key": "single-vpc",
            "label": "Single-VPC Template",
            "description": "Starter YAML scaffold for the Single-VPC wizard flow.",
            "draft_title": "single-vpc-template",
            "spec_yaml": _single_vpc_template(
                default_pc_version,
                vpc_profile=vpc_profile,
                template_envs=template_envs,
                template_prefix=template_prefix,
                template_domain=template_domain,
                rancher_cluster=rancher_cluster,
            ),
        },
        {
            "key": "multi-vpc",
            "label": "Multi-VPC Template",
            "description": "Starter YAML scaffold for the Multi-VPC wizard flow.",
            "draft_title": "multi-vpc-template",
            "spec_yaml": _single_vpc_template(
                default_pc_version,
                vpc_profile=vpc_profile,
                template_envs=template_envs,
                template_prefix=template_prefix,
                template_domain=template_domain,
                rancher_cluster=rancher_cluster,
                template_key="multi-vpc",
            ),
        },
        {
            "key": "rgs",
            "label": "RGS Template",
            "description": "Starter YAML scaffold for the RGS wizard flow.",
            "draft_title": "rgs-template",
            "spec_yaml": _rgs_template(
                default_pc_version,
                vpc_profile=vpc_profile,
                template_envs=template_envs,
                template_prefix=template_prefix,
                template_domain=template_domain,
                rancher_cluster=rancher_cluster,
                rancher_context=rancher_context,
            ),
        },
    ]
    if release_manifest:
        for template in templates:
            template["spec_yaml"] = append_release_manifest_block(
                template["spec_yaml"],
                release_manifest,
            )
    return templates


def get_wizard_template(
    template_key: str,
    default_pc_version: str = "",
    release_manifest: dict[str, Any] | None = None,
    vpc_profile: str = "",
    template_envs: list[str] | None = None,
    template_prefix: str = "",
    template_domain: str = "",
    rancher_cluster: str = "",
    rancher_context: str = "",
) -> dict[str, str] | None:
    wanted = (template_key or "").strip().lower()
    if not wanted:
        return None
    for template in list_wizard_templates(
        default_pc_version,
        release_manifest,
        vpc_profile,
        template_envs,
        template_prefix,
        template_domain,
        rancher_cluster,
        rancher_context,
    ):
        if template["key"] == wanted:
            return template
    return None


def append_release_manifest_block(
    spec_yaml: str,
    release_manifest: dict[str, Any] | None,
) -> str:
    release_block = _release_manifest_block(release_manifest)
    if not release_block:
        return spec_yaml
    base = (spec_yaml or "").rstrip()
    return f"{base}\n\n{release_block}\n"


def _spec_header(spec_type: str, default_pc_version: str) -> str:
    lines = [f"spec_type: {spec_type}"]
    pc_version = (default_pc_version or "").strip()
    if pc_version:
        lines.append(f"praxis_core_version: {pc_version}")
    return "\n".join(lines)


def _compose_template(spec_type: str, default_pc_version: str, body: str) -> str:
    header = _spec_header(spec_type, default_pc_version)
    return f"{header}\n{dedent(body).strip()}\n"


def _release_manifest_block(release_manifest: dict[str, Any] | None) -> str:
    if not isinstance(release_manifest, dict) or not release_manifest:
        return ""

    stream = io.StringIO()
    stream.write("# --- DEPLOYMENT VERSIONS ---\n")
    if release_manifest.get("bundle") is not None:
        stream.write(f"bundle: {release_manifest.get('bundle')}\n")
    if release_manifest.get("version") is not None:
        stream.write(f"version: {release_manifest.get('version')}\n")

    for section in ("iac", "helm", "catalyst", "bootstrap", "rgs", "add_on"):
        if section in release_manifest:
            yaml.safe_dump({section: release_manifest[section]}, stream, sort_keys=False)

    text = stream.getvalue().strip()
    return text if text != "# --- DEPLOYMENT VERSIONS ---" else ""


def _normalize_vpc_profile_key(profile_key: str | None) -> str:
    return str(profile_key or "").strip().lower()


def _resolve_vpc_profile(profile_key: str | None) -> tuple[str, dict[str, Any] | None]:
    normalized = _normalize_vpc_profile_key(profile_key)
    if not normalized or normalized == "custom":
        return normalized, None
    return normalized, VPC_AZ_PROFILES.get(normalized)


def _yaml_list(items: list[str], indent: int) -> str:
    prefix = " " * indent
    return "\n".join(f"{prefix}- {item}" for item in items)


def _render_env_vpc_block(
    *,
    env_names: list[str],
    profile_key: str,
    profile: dict[str, Any] | None,
    transit_gateway_id: str,
    vpc_endpoint_service: str,
) -> str:
    lines = ["vpc:"]
    for env_name in env_names:
        lines.append(f"  {env_name}:")
        if profile_key:
            lines.append(f"    vpc_az_profile: {profile_key}")
        lines.extend(
            [
                f"    transit_gateway_id: {transit_gateway_id}",
                f"    vpc_endpoint_service: {vpc_endpoint_service}",
            ]
        )
        if profile:
            az_names = [str(item).strip() for item in profile.get("az_names", []) if str(item).strip()]
            az_zone_ids = [str(item).strip() for item in profile.get("az_zone_ids", []) if str(item).strip()]
            if az_names:
                lines.append("    availability_zones:")
                lines.append(_yaml_list(az_names, 6))
            if az_zone_ids:
                lines.append("    filter_az_zone_ids:")
                lines.append(_yaml_list(az_zone_ids, 6))
    return "\n".join(lines)


def _render_flat_vpc_block(
    *,
    default_profile_key: str,
    default_transit_gateway_id: str,
    default_vpc_endpoint_service: str,
    default_az_names: list[str],
    default_az_zone_ids: list[str],
    override_vpc_profile: str,
) -> tuple[str, str]:
    override_key, override_profile = _resolve_vpc_profile(override_vpc_profile)
    if override_key == "custom":
        lines = [
            "vpc:",
            "  vpc_az_profile: custom",
            "  transit_gateway_id: __REQUIRED_TRANSIT_GATEWAY_ID__",
            "  vpc_endpoint_service: __REQUIRED_VPCE_SERVICE_NAME__",
        ]
        return "\n".join(lines), ""

    if override_profile:
        az_names = [str(item).strip() for item in override_profile.get("az_names", []) if str(item).strip()]
        az_zone_ids = [str(item).strip() for item in override_profile.get("az_zone_ids", []) if str(item).strip()]
        lines = [
            "vpc:",
            f"  vpc_az_profile: {override_key}",
            f"  transit_gateway_id: {override_profile['transit_gateway_id']}",
            f"  vpc_endpoint_service: {override_profile['vpc_endpoint_service']}",
        ]
        if az_names:
            lines.append("  availability_zones:")
            lines.append(_yaml_list(az_names, 4))
        if az_zone_ids:
            lines.append("  filter_az_zone_ids:")
            lines.append(_yaml_list(az_zone_ids, 4))
        return "\n".join(lines), str(override_profile.get("aws_region") or "").strip()

    lines = [
        "vpc:",
        f"  vpc_az_profile: {default_profile_key}",
        f"  transit_gateway_id: {default_transit_gateway_id}",
        f"  vpc_endpoint_service: {default_vpc_endpoint_service}",
        "  availability_zones:",
        _yaml_list(default_az_names, 4),
        "  filter_az_zone_ids:",
        _yaml_list(default_az_zone_ids, 4),
    ]
    return "\n".join(lines), ""


def _resolve_rancher_cluster(cluster_name: str | None) -> tuple[str, str, str]:
    selected = str(cluster_name or "").strip()
    if not selected:
        return "", "", ""
    return (
        selected,
        str(RANCHER_CLUSTER_TO_REGION.get(selected) or "").strip(),
        str(RANCHER_CLUSTER_TO_RANCHER_PREFIX.get(selected) or "").strip(),
    )


def _normalize_rancher_context(context_name: str | None) -> str:
    return str(context_name or "").strip().lower()


def _resolve_rancher_context(context_name: str | None) -> tuple[str, str, str]:
    selected = _normalize_rancher_context(context_name)
    if not selected:
        return "", "", ""
    integration_id = str(RANCHER_CONTEXT_TO_INTEGRATION.get(selected) or "").strip()
    worker_pool_id = str(rancher_worker_pool_id_for_context(selected) or "").strip()
    return selected, integration_id, worker_pool_id


def _resolve_template_envs(
    selected_envs: list[str] | tuple[str, ...] | str | None,
    default_envs: list[str],
) -> list[str]:
    if isinstance(selected_envs, str):
        raw_values = [selected_envs]
    elif selected_envs is None:
        raw_values = []
    else:
        raw_values = list(selected_envs)

    normalized_values: list[str] = []
    for value in raw_values:
        normalized = str(value or "").strip().lower()
        if normalized and normalized in UNIFIED_ENV_TYPES and normalized not in normalized_values:
            normalized_values.append(normalized)

    return normalized_values or list(default_envs)


def _resolve_template_prefix(selected_prefix: str | None) -> str:
    return str(selected_prefix or "").strip().lower()


def template_prefix_suffix(template_key: str | None) -> str:
    return str(TEMPLATE_PREFIX_SUFFIXES.get(str(template_key or "").strip().lower()) or "").strip()


def template_prefix_placeholder(template_key: str | None) -> str:
    suffix = template_prefix_suffix(template_key)
    if not suffix:
        return "<customer>-prefix"
    return f"<customer>-{suffix}"


def normalize_template_prefix(template_key: str | None, selected_prefix: str | None) -> str:
    normalized = _resolve_template_prefix(selected_prefix)
    suffix = template_prefix_suffix(template_key)
    if not normalized or not suffix:
        return normalized

    parts = [part for part in normalized.split("-") if part]
    if not parts:
        return ""

    known_suffixes = set(TEMPLATE_PREFIX_SUFFIXES.values())
    if parts[-1] in known_suffixes:
        parts = parts[:-1]

    if not parts:
        return ""

    return "-".join([*parts, suffix])


def _resolve_template_domain(selected_domain: str | None) -> str:
    selected = str(selected_domain or "").strip().lower()
    if not selected:
        return ""
    valid_domains = {value for value, _label in DOMAIN_CHOICES}
    return selected if selected in valid_domains else ""


def _derive_customer_code(prefix: str | None) -> str:
    normalized = _resolve_template_prefix(prefix)
    if not normalized:
        return ""
    return normalized.split("-", 1)[0].upper()


def _derive_repo_description(prefix: str | None) -> str:
    normalized = _resolve_template_prefix(prefix)
    if not normalized:
        return ""
    return normalized.replace("-", " ")


def _scoped_placeholder(token: str, env_name: str) -> str:
    normalized_env = str(env_name or "").strip().lower()
    if not normalized_env or normalized_env == "templateing":
        return f"__{token}__"
    scoped_env = normalized_env.upper().replace("-", "_")
    return f"__{scoped_env}_{token}__"


def _default_rgs_rancher_context_for_env(env_name: str) -> str:
    defaults = {
        "dev": "rancher-devops",
        "test": "rancher-devops",
        "showroom": "rancher-devops",
        "training": "rancher-devops",
        "sit": "rancher-nonprod",
        "staging": "rancher-preprod",
        "uat": "rancher-preprod",
        "preprod": "rancher-preprod",
        "prod": "rancher-prod",
    }
    return defaults.get(str(env_name or "").strip().lower(), "")


def _default_rgs_rancher_cluster_for_env(env_name: str) -> str:
    defaults = {
        "dev": "PRAXIS-DevOps",
        "test": "PRAXIS-DevOps",
        "showroom": "PRAXIS-DevOps",
        "training": "PRAXIS-DevOps",
        "sit": "PRAXIS-Rancher",
        "staging": "PRAXIS-Rancher-Preprod",
        "uat": "PRAXIS-Rancher-Preprod",
        "preprod": "PRAXIS-Rancher-Preprod",
        "prod": "PRAXIS-Rancher-Prod",
    }
    return defaults.get(str(env_name or "").strip(), "")


def _loyalty_template(
    default_pc_version: str,
    *,
    vpc_profile: str = "",
    template_envs: list[str] | None = None,
    template_prefix: str = "",
    template_domain: str = "",
    rancher_cluster: str = "",
) -> str:
    env_names = _resolve_template_envs(template_envs, ["dev"])
    prefix_value = normalize_template_prefix("loyalty", template_prefix)
    prefix_token = prefix_value or "__REQUIRED_PREFIX__"
    customer_value = _derive_customer_code(prefix_value) or "__REQUIRED_CUSTOMER__"
    domain_value = _resolve_template_domain(template_domain) or "__REQUIRED_DOMAIN_NAME__"
    environment_value = prefix_value or "__REQUIRED_ENVIRONMENT_NAME__"
    iac_repo_value = f"{prefix_value}-iac" if prefix_value else "__REQUIRED_IAC_REPO__"
    profile_key, profile = _resolve_vpc_profile(vpc_profile)
    selected_cluster, rancher_region_override, rancher_prefix_override = _resolve_rancher_cluster(
        rancher_cluster
    )
    aws_region = str(profile.get("aws_region") or "").strip() if profile else "eu-central-1"
    vpc_block = _render_env_vpc_block(
        env_names=env_names,
        profile_key=profile_key,
        profile=profile,
        transit_gateway_id=str(profile.get("transit_gateway_id") or "").strip()
        if profile
        else "__REQUIRED_TRANSIT_GATEWAY_ID__",
        vpc_endpoint_service=str(profile.get("vpc_endpoint_service") or "").strip()
        if profile
        else "__REQUIRED_VPCE_SERVICE_NAME__",
    )
    indented_vpc_block = indent(vpc_block, "        ")
    environment_list_block = _yaml_list(env_names, 12)
    spacelift_lines = ["spacelift_settings:"]
    rancher_lines = ["rancher:"]
    subnet_lines = ["subnets:"]
    eks_env_lines: list[str] = []
    aurora_env_lines: list[str] = []

    for env_name in env_names:
        rancher_lines.extend(
            [
                f"  {env_name}:",
                f"    rancher_cluster_name: {selected_cluster or '__REQUIRED_RANCHER_CLUSTER__'}",
                f"    rancher_aws_region: {rancher_region_override or aws_region}",
                "    helm_git_branch: main",
                "    helm_app_git_branch: main",
                "    helm_bootstrap_git_branch: main",
                "    multi_mesh: false",
                "    aws_account_alias: __REQUIRED_AWS_ACCOUNT_ALIAS__",
                f"    rancherPrefix: {rancher_prefix_override or env_name}",
            ]
        )
        spacelift_lines.extend(
            [
                f"  {env_name}:",
                "    aws_integration_id: __REQUIRED_AWS_INTEGRATION_ID__",
            ]
        )
        subnet_lines.extend(
            [
                f"  {env_name}:",
                "    vpc_cidr_block: 10.20.0.0/24",
            ]
        )
        eks_env_lines.extend(
            [
                f"  {env_name}:",
                "    eks_managed_node_groups:",
                f"      {prefix_token}-{env_name}-ng0:",
                "        instance_types:",
                "          - t3.large",
                "        min_size: 1",
                "        max_size: 3",
                "        desired_size: 1",
            ]
        )
        aurora_env_lines.extend(
            [
                f"  {env_name}:",
                "    aurora_instance_class: db.r6g.large",
            ]
        )

    spacelift_block = indent("\n".join(spacelift_lines), "        ")
    rancher_block = indent("\n".join(rancher_lines), "        ")
    subnet_block = indent("\n".join(subnet_lines), "        ")
    eks_env_block = indent("\n".join(eks_env_lines), "        ")
    aurora_env_block = indent("\n".join(aurora_env_lines), "        ")
    return _compose_template(
        "loyalty",
        default_pc_version,
        f"""\
        # --- PROJECT SETTINGS ---
        project_settings:
          environment_type:
{environment_list_block}
        # --- REPOSITORY SETTINGS ---
        repository_settings:
          new_prefix: {prefix_token}
          repo_description: Loyalty environment
        # --- SPACELIFT SETTINGS ---
{spacelift_block}
        # --- REPLACEMENTS ---
        replacements:
          ssa_prefix: {prefix_token}
          ssa_iac_git_repo: {iac_repo_value}
        # --- COMMON SETTINGS ---
        common:
          aws_region: {aws_region}
          environment: {environment_value}
          domain_name: {domain_value}
          support_organization: architects
          customer: {customer_value}
          product: loyalty
        # --- RANCHER SETTINGS ---
{rancher_block}
        # --- GRAFANA SETTINGS ---
        grafana: {{}}
        # --- VPC SETTINGS ---
{indented_vpc_block}
        # --- SUBNET SETTINGS ---
{subnet_block}
        # --- EKS SETTINGS ---
        eks:
          eks_cluster_version: "1.30"
          eks_ssh_allow_access_from_cidrs:
            - 10.0.0.0/8
          eks_endpoint_private_access: true
          eks_endpoint_public_access: false
          eks_endpoint_public_access_cidrs: []
          eks_api_allow_access_from_cidrs:
            - 10.0.0.0/8
          ami_type: AL2_x86_64
{eks_env_block}
        # --- AURORA SETTINGS ---
        aurora:
          engine: aurora-postgresql
          engine_version: "15.4"
{aurora_env_block}
        # --- KAFKA SETTINGS ---
        kafka: {{}}
        """,
    )


def _single_vpc_template(
    default_pc_version: str,
    *,
    vpc_profile: str = "",
    template_envs: list[str] | None = None,
    template_prefix: str = "",
    template_domain: str = "",
    rancher_cluster: str = "",
    template_key: str = "single-vpc",
) -> str:
    is_multi_vpc = template_key == "multi-vpc"
    env_names = _resolve_template_envs(template_envs, ["dev"])
    prefix_value = normalize_template_prefix(template_key, template_prefix)
    prefix_token = prefix_value or "__REQUIRED_PREFIX__"
    customer_value = _derive_customer_code(prefix_value) or "__REQUIRED_CUSTOMER__"
    domain_value = _resolve_template_domain(template_domain) or "__REQUIRED_DOMAIN_NAME__"
    environment_value = prefix_value or "__REQUIRED_ENVIRONMENT_NAME__"
    iac_repo_value = f"{prefix_value}-iac" if prefix_value else "__REQUIRED_IAC_REPO__"
    profile_key, profile = _resolve_vpc_profile(vpc_profile)
    selected_cluster, rancher_region_override, rancher_prefix_override = _resolve_rancher_cluster(
        rancher_cluster
    )
    aws_region = str(profile.get("aws_region") or "").strip() if profile else "eu-central-1"
    vpc_block = _render_env_vpc_block(
        env_names=env_names,
        profile_key=profile_key,
        profile=profile,
        transit_gateway_id=str(profile.get("transit_gateway_id") or "").strip()
        if profile
        else "__REQUIRED_TRANSIT_GATEWAY_ID__",
        vpc_endpoint_service=str(profile.get("vpc_endpoint_service") or "").strip()
        if profile
        else "__REQUIRED_VPCE_SERVICE_NAME__",
    )
    indented_vpc_block = indent(vpc_block, "        ")
    environment_list_block = _yaml_list(env_names, 12)
    deployments = ["ilp", "pmv", "cgs", "rmq", "common"]
    if is_multi_vpc:
        deployments = ["ilp", "pmv", "cgs", "cr", "vault", "rmq", "common"]
    deployment_list_block = _yaml_list(deployments, 12)
    spacelift_lines = ["spacelift_settings:"]
    rancher_lines = ["rancher:"]
    subnet_lines = ["subnets:"]
    eks_env_lines: list[str] = []
    aurora_env_lines: list[str] = []
    redis_env_lines: list[str] = []

    for env_name in env_names:
        spacelift_lines.extend(
            [
                f"  {env_name}:",
                "    aws_integration_id: __REQUIRED_AWS_INTEGRATION_ID__",
            ]
        )
        rancher_lines.extend(
            [
                f"  {env_name}:",
                f"    rancher_cluster_name: {selected_cluster or '__REQUIRED_RANCHER_CLUSTER__'}",
                f"    rancher_aws_region: {rancher_region_override or aws_region}",
                "    helm_git_branch: main",
                "    helm_app_git_branch: main",
                "    helm_bootstrap_git_branch: main",
                f"    multi_mesh: {'true' if is_multi_vpc else 'false'}",
                "    aws_account_alias: __REQUIRED_AWS_ACCOUNT_ALIAS__",
                f"    rancherPrefix: {rancher_prefix_override or env_name}",
            ]
        )
        subnet_lines.extend(
            [
                f"  {env_name}:",
                "    ilp:",
                "      vpc_cidr_block: 10.10.0.0/24",
                "    pmv:",
                "      vpc_cidr_block: 10.10.1.0/24",
                "    cgs:",
                "      vpc_cidr_block: 10.10.2.0/24",
                "    rmq:",
                "      vpc_cidr_block: 10.10.3.0/24",
                "    common:",
                "      vpc_cidr_block: 10.10.4.0/24",
            ]
        )
        if is_multi_vpc:
            eks_env_lines.append(f"  {env_name}:")
            for cluster_name in ("ilp", "cgs", "pmv"):
                eks_env_lines.extend(
                    [
                        f"    {cluster_name}:",
                        "      eks_managed_node_groups:",
                        f"        {prefix_token}-{env_name}-{cluster_name}-ng0:",
                        "          instance_types:",
                        "            - t3.large",
                        "          min_size: 1",
                        "          max_size: 3",
                        "          desired_size: 1",
                    ]
                )
        else:
            eks_env_lines.extend(
                [
                    f"  {env_name}:",
                    "    eks_managed_node_groups:",
                    f"      {prefix_token}-{env_name}-ng0:",
                    "        instance_types:",
                    "          - t3.large",
                    "        min_size: 1",
                    "        max_size: 3",
                    "        desired_size: 1",
                ]
            )
        aurora_env_lines.extend(
            [
                f"  {env_name}:",
                "    aurora_instance_class: db.r6g.large",
            ]
        )
        redis_env_lines.extend(
            [
                f"  {env_name}:",
                "    redis_instance_type: cache.t4g.small",
                "    redis_cluster_size: 1",
            ]
        )

    spacelift_block = indent("\n".join(spacelift_lines), "        ")
    rancher_block = indent("\n".join(rancher_lines), "        ")
    subnet_block = indent("\n".join(subnet_lines), "        ")
    eks_env_block = indent("\n".join(eks_env_lines), "        ")
    aurora_env_block = indent("\n".join(aurora_env_lines), "        ")
    redis_env_block = indent("\n".join(redis_env_lines), "        ")
    return _compose_template(
        template_key,
        default_pc_version,
        f"""\
        # --- PROJECT SETTINGS ---
        project_settings:
          environment_type:
{environment_list_block}
          deployments:
{deployment_list_block}
        # --- REPOSITORY SETTINGS ---
        repository_settings:
          new_prefix: {prefix_token}
          repo_description: {'Multi-VPC' if is_multi_vpc else 'Single-VPC'} environment
        # --- SPACELIFT SETTINGS ---
{spacelift_block}
        # --- REPLACEMENTS ---
        replacements:
          ssa_prefix: {prefix_token}
          ssa_iac_git_repo: {iac_repo_value}
        # --- PC SOURCE SETTINGS ---
        helm_whitelist:
          helm: []
        iac_whitelists:
          iac: []
        bootstrap_whitelist:
          bootstrap: []
        deployment_whitelist:
          deployment: []
        rancher_helm_pipelines:
          helm_iac_cds: []
          helm_app_cds: []
          helm_bootstrap_cds: []
        # --- COMMON SETTINGS ---
        common:
          aws_region: {aws_region}
          environment: {environment_value}
          domain_name: {domain_value}
          support_organization: architects
          customer: {customer_value}
          product: catalyst
        # --- RANCHER SETTINGS ---
{rancher_block}
        # --- GRAFANA SETTINGS ---
        grafana: {{}}
        # --- VPC SETTINGS ---
{indented_vpc_block}
        # --- SUBNET SETTINGS ---
{subnet_block}
        # --- EKS SETTINGS ---
        eks:
          eks_cluster_version: "1.30"
          eks_ssh_allow_access_from_cidrs:
            - 10.0.0.0/8
          eks_endpoint_private_access: true
          eks_endpoint_public_access: false
          eks_endpoint_public_access_cidrs: []
          eks_api_allow_access_from_cidrs:
            - 10.0.0.0/8
          ami_type: AL2_x86_64
{eks_env_block}
        # --- AURORA SETTINGS ---
        aurora:
          engine: aurora-postgresql
          engine_version: "15.4"
{aurora_env_block}
        # --- REDIS SETTINGS ---
        redis:
{redis_env_block}
        # --- ALB SETTINGS ---
        external_alb_allowed_ips: []
        # --- ILP OPTIONAL SETTINGS ---
        formkiq: {{}}
        mkodo: {{}}
        """,
    )


def _rgs_template(
    default_pc_version: str,
    *,
    vpc_profile: str = "",
    template_envs: list[str] | None = None,
    template_prefix: str = "",
    template_domain: str = "",
    rancher_cluster: str = "",
    rancher_context: str = "",
) -> str:
    env_names = _resolve_template_envs(template_envs, ["templateing"])
    prefix_value = normalize_template_prefix("rgs", template_prefix)
    prefix_token = prefix_value or "__SSA_PREFIX__"
    repo_description = _derive_repo_description(prefix_value) or "__REPO_DESCRIPTION__"
    customer_value = _derive_customer_code(prefix_value) or "__CUSTOMER__"
    domain_value = _resolve_template_domain(template_domain) or "__DOMAIN_NAME__"
    iac_repo_value = f"{prefix_value}-iac" if prefix_value else f"{prefix_token}-iac"
    vpc_block, vpc_region_override = _render_flat_vpc_block(
        default_profile_key="use1-shared-loyalty",
        default_transit_gateway_id="tgw-07526f4ceee38a429",
        default_vpc_endpoint_service="com.amazonaws.vpce.us-east-1.vpce-svc-09e7e29afb47370cc",
        default_az_names=["us-east-1c", "us-east-1b", "us-east-1a"],
        default_az_zone_ids=["use1-az1", "use1-az2", "use1-az6"],
        override_vpc_profile=vpc_profile,
    )
    aws_region = vpc_region_override or "us-east-1"
    indented_vpc_block = indent(vpc_block, "        ")

    def branch_value(env_name: str) -> str:
        normalized = str(env_name or "").strip().lower()
        if not normalized or normalized == "templateing":
            return "__SSA_IAC_GIT_BRANCH__"
        return normalized

    def resolved_cluster_for_env(env_name: str) -> tuple[str, str, str]:
        selected_cluster = str(rancher_cluster or "").strip() or _default_rgs_rancher_cluster_for_env(env_name)
        cluster_name, cluster_region, cluster_prefix = _resolve_rancher_cluster(selected_cluster)
        fallback_region = aws_region if vpc_region_override else "__RANCHER_AWS_REGION__"
        return (
            cluster_name or _scoped_placeholder("RANCHER_CLUSTER_NAME", env_name),
            cluster_region or fallback_region,
            cluster_prefix or "__RANCHER_PREFIX__",
        )

    def resolved_context_for_env(env_name: str) -> tuple[str, str, str]:
        selected_context = _normalize_rancher_context(rancher_context) or _default_rgs_rancher_context_for_env(
            env_name
        )
        context_name, cloud_integration_id, worker_pool_id = _resolve_rancher_context(selected_context)
        return (
            context_name or _scoped_placeholder("RANCHER_CONTEXT", env_name),
            cloud_integration_id or _scoped_placeholder("RANCHER_CLOUD_INTEGRATION_ID", env_name),
            worker_pool_id or _scoped_placeholder("SSA_RANCHER_WORKER_POOL_ID", env_name),
        )

    spacelift_lines = ["spacelift_settings:"]
    replacement_lines = [
        "replacements:",
        f"  ssa_prefix: {prefix_token}",
        f"  ssa_iac_git_repo: {iac_repo_value}",
        "  ssa_helm_context: harbor_helm_auth",
        "  ssa_shared_vault_context: shared_vault_approle_auth",
        "  protect_stack_delete: 'true'",
        "  istio: 'false'",
        "  vault: 'false'",
    ]
    rancher_lines = ["rancher:"]
    grafana_lines = ["grafana:"]
    subnet_lines = ["subnets:"]
    eks_env_lines: list[str] = []
    aurora_env_lines: list[str] = []
    redis_env_lines: list[str] = []

    for env_name in env_names:
        branch = branch_value(env_name)
        rancher_cluster_name, rancher_region, rancher_prefix = resolved_cluster_for_env(env_name)
        rancher_context_name, rancher_cloud_integration, rancher_worker_pool = resolved_context_for_env(
            env_name
        )
        aws_integration_id = _scoped_placeholder("AWS_INTEGRATION_ID", env_name)
        spacelift_space_id = _scoped_placeholder("SPACELIFT_SPACE_ID", env_name)
        vpc_cidr_block = _scoped_placeholder("VPC_CIDR_BLOCK", env_name)

        spacelift_lines.extend(
            [
                f"  {env_name}:",
                f"    aws_integration_id: {aws_integration_id}",
                f"    spacelift_space_id: {spacelift_space_id}",
                f"    description: Spacelift setup for {prefix_token}-{branch}",
            ]
        )
        replacement_lines.extend(
            [
                f"  {env_name}:",
                f"    ssa_rancher_context: {rancher_context_name}",
                f"    ssa_rancher_cloud_integration: {rancher_cloud_integration}",
                f"    ssa_rancher_worker_pool_id: {rancher_worker_pool}",
                f"    ssa_iac_git_branch: {branch}",
            ]
        )
        rancher_lines.extend(
            [
                f"  {env_name}:",
                f"    rancher_cluster_name: {rancher_cluster_name}",
                f"    rancher_aws_region: {rancher_region}",
                "    rancher_gitrepo_auth_type: https",
                "    rancher_github_auth: praxis-github-ssh-key",
                f"    helm_git_branch: {branch}",
                f"    helm_app_git_branch: {branch}",
                f"    helm_bootstrap_git_branch: {branch}",
                f"    rancherPrefix: {rancher_prefix}",
                "    labels:",
                "      istio: false",
                "      vault: false",
                f"      aws_account_alias: praxis-{prefix_token}-{branch}",
                "      dev_tools: false",
                f"      env_type: {branch}",
                f"      domain_name: {domain_value}",
                f"      common_env_name: {prefix_token}",
                "      product: RGS",
                f"      customer: {customer_value}",
                "      support_organization: PRAXIS",
                "      multi_mesh: false",
                "    settings:",
                "      fetch_waf: false",
                "      fetch_cert: true",
                "      fetch_vault_db: false",
                "      enable_meta: true",
                "      enable_kubeconfig_secret: false",
                "      include_rabbit_labels: false",
                "      include_vault_labels: false",
                "      include_rds_labels: false",
                "      include_rds_endpoint: false",
                "      include_vault_rds_labels: false",
                "      include_redis_labels: false",
                "      include_waf_labels: false",
            ]
        )
        grafana_lines.extend(
            [
                f"  {env_name}:",
                "    grafana_cloud_setup: 'false'",
                "    grafana_external_id: ''",
            ]
        )
        subnet_lines.extend(
            [
                f"  {env_name}:",
                f"    vpc_cidr_block: {vpc_cidr_block}",
            ]
        )
        eks_env_lines.extend(
            [
                f"  {env_name}:",
                "    eks_managed_node_groups:",
                f"      {prefix_token}-{branch}-ng0:",
                "        instance_types:",
                "          - t3a.xlarge",
                "        min_size: 3",
                "        max_size: 6",
                "        desired_size: 3",
            ]
        )
        aurora_env_lines.extend(
            [
                f"  {env_name}:",
                "    aurora_instance_class: db.r6g.large",
            ]
        )
        redis_env_lines.extend(
            [
                f"  {env_name}:",
                "    redis_instance_type: cache.t4g.small",
                "    redis_cluster_size: 3",
                "    redis_cluster_shard: 1",
                "    redis_cluster_replica: 2",
            ]
        )
    environment_list_block = _yaml_list(env_names, 12)
    spacelift_block = indent("\n".join(spacelift_lines), "        ")
    replacement_block = indent("\n".join(replacement_lines), "        ")
    rancher_block = indent("\n".join(rancher_lines), "        ")
    grafana_block = indent("\n".join(grafana_lines), "        ")
    subnet_block = indent("\n".join(subnet_lines), "        ")
    eks_env_block = indent("\n".join(eks_env_lines), "        ")
    aurora_env_block = indent("\n".join(aurora_env_lines), "        ")
    redis_env_block = indent("\n".join(redis_env_lines), "        ")
    return _compose_template(
        "rgs",
        default_pc_version,
        f"""\
        # --- PROJECT SETTINGS ---
        project_settings:
          environment_type:
{environment_list_block}
          deployments:
            - rgs
            - common
            - rmq
        # --- REPOSITORY SETTINGS ---
        repository_settings:
          new_prefix: {prefix_token}
          repo_description: {repo_description}
        # --- SPACELIFT SETTINGS ---
{spacelift_block}
        # --- REPLACEMENTS ---
{replacement_block}
        # --- HELM WHITELIST ---
        helm_whitelist:
          helm:
            - helm-app-db-resource-deployment
            - helm-aws-sm-bootstrap
            - helm-external-dns-records
            - helm-external-secrets
            - helm-finops
            - helm-monitoring
            - helm-nginx-ingress
            - helm-rmq
            - helm-support-charts
        # --- DEPLOYMENT WHITELIST ---
        deployment_whitelist:
          deployment:
            - application-rgs
            - application-rng
        # --- RANCHER HELM PIPELINES ---
        rancher_helm_pipelines:
          helm_iac_cds:
            - helm-app-db-resource-deployment/rgs
            - helm-aws-sm-bootstrap/rgs
            - helm-aws-sm-bootstrap/rng
            - helm-external-dns-records/rgs
            - helm-external-secrets
            - helm-finops
            - helm-nginx-ingress
            - helm-rmq
            - helm-support-charts
          helm_app_cds:
            - application-rgs
            - application-rng
          helm_bootstrap_cds:
            - bootstrap-rgs
            - bootstrap-rng
        # --- COMMON ---
        common:
          aws_region: {aws_region}
          environment: {prefix_token}
          cert_cluster_mode: singlezone
          support_organization: PRAXIS
          customer: {customer_value}
          product: RGS
          domain_name: {domain_value}
        # --- RANCHER ---
{rancher_block}
        # --- GRAFANA ---
{grafana_block}
        # --- VPC ---
{indented_vpc_block}
        # --- SUBNETS ---
{subnet_block}
        # --- EKS ---
        eks:
          eks_cluster_version: '1.34'
          eks_ssh_allow_access_from_cidrs: '["10.0.0.0/8"]'
          eks_endpoint_private_access: true
          eks_endpoint_public_access: false
          eks_endpoint_public_access_cidrs: '["0.0.0.0/0"]'
          eks_api_allow_access_from_cidrs: '["10.0.0.0/8"]'
          eks_systems_administrators_role: AWSReservedSSO_Administrator-8-Hours_
          ami_type: amazon-linux-2023
          enable_lbc_irsa: 'false'
          enable_asc_irsa: 'false'
          enable_es_irsa: 'false'
          enable_edns_irsa: 'false'
          allow_rancher_user: 'false'
{eks_env_block}
        # --- AURORA ---
        aurora:
          engine: aurora-postgresql
          engine_version: '15.10'
          database_username: rds_admin
          create_db_cluster_parameter_group: 'true'
          db_cluster_parameter_group_use_name_prefix: 'false'
          db_cluster_parameter_group_family: aurora-postgresql15
          db_cluster_parameter_group_parameters:
            - name: shared_preload_libraries
              value: pg_cron,pgaudit,pg_stat_statements
              apply_method: pending-reboot
            - name: pgaudit.log
              value: function,role,ddl,misc
              apply_method: immediate
            - name: pgaudit.role
              value: rds_pgaudit
              apply_method: pending-reboot
            - name: log_connections
              value: '1'
              apply_method: immediate
          enabled_cloudwatch_logs_exports: '["postgresql"]'
          enable_s3_export_lambda: 'false'
{aurora_env_block}
        # --- REDIS ---
        redis:
{redis_env_block}
        # --- EXTERNAL ALB ALLOWED IPS ---
        external_alb_allowed_ips:
          - ips:
              - __IP1__
              - __IP2__
              - __IP3__
            description: Allowed ingress sources
        """,
    )
