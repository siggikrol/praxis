from __future__ import annotations

import ipaddress
import re
from datetime import date
from typing import Any

from constants import AWS_REGIONS as WIZARD_AWS_REGIONS, RANCHER_EKS_CLUSTERS


STATUS_CHOICES = (
    ("", "Choose status"),
    ("confirmed", "Confirmed and verified"),
    ("requested", "In progress / ticket raised"),
    ("blocked", "Blocked"),
)

ENVIRONMENT_TYPES = (
    "dev", "test", "sit", "showroom", "training", "staging", "uat", "preprod", "prod"
)

ENVIRONMENT_TYPE_TITLE_ALIASES = {
    "dev": ("dev", "development"),
    "test": ("test", "testing"),
    "sit": ("sit", "system integration"),
    "showroom": ("showroom",),
    "training": ("training",),
    "staging": ("staging", "stage"),
    "uat": ("uat", "user acceptance"),
    "preprod": ("preprod", "pre prod", "preproduction", "pre production"),
    "prod": ("prod", "production"),
}

AWS_REGIONS = tuple(code for code, _label in WIZARD_AWS_REGIONS)

PRODUCT_PREFIXES = {
    "catalyst": "ctlst",
    "loyalty": "playon",
    "rgs": "rgs",
}

SETUP_TYPES = {
    "catalyst-single-vpc": {"label": "Catalyst single-vpc", "prefix": "ctlst", "product": "Catalyst"},
    "catalyst-multi-vpc": {"label": "Catalyst multi-vpc", "prefix": "ctlst", "product": "Catalyst"},
    "rgs": {"label": "RGS", "prefix": "rgs", "product": "RGS"},
    "loyalty": {"label": "Loyalty", "prefix": "playon", "product": "Loyalty"},
}

BASE_DNS_DOMAINS = ("example.com", "natlot.be")

FIELD_HELP = {
    "title": "A human-readable title used as the heading of the generated Confluence handoff.",
    "customer": "The uppercase customer code used by Praxis naming, for example LNL.",
    "product": "The platform being provisioned. This determines the naming prefix such as ctlst, playon, or rgs.",
    "setup_type": "The Praxis foundation/template family DevOps will use to provision this environment.",
    "environment_suffix": "Optional short discriminator appended to the generated environment name, for example e, c, or v.",
    "environment_name": "The canonical Praxis environment name used across AWS, repositories, and platform labels.",
    "environment_type": "The lifecycle tier. It controls naming, account placement, and production safeguards.",
    "aws_account_id": "The 12-digit AWS account returned by the AWS account service request. It is an allocated value and is not required before the request is raised.",
    "aws_region": "The requested AWS region. Confirm the final region again when recording the allocated account details.",
    "target_date": "The requested date by which prerequisites should be ready for DevOps to begin.",
    "requester": "The person accountable for preparing and maintaining this handoff.",
    "architect": "The architect responsible for the technical design and prerequisite decisions.",
    "devops_receiver": "The exact signed-in DevOps identity expected to receive and provision this environment. This identity controls lifecycle and provisioning actions.",
    "jira_epic": "The Jira epic used to track the complete environment delivery and its dependent work.",
    "spec_link": "Optional link to a draft or validated Spec Workbench/Confluence specification.",
    "production_classification": "Whether ServiceNow should route this as a PROD or NONPROD foundation request.",
    "requested_network_size": "The requested network capacity or CIDR prefix size. Networking returns the actual non-overlapping VPC CIDR later.",
    "aws_service_request": "The ServiceNow AWS account catalog request (REQ/RITM). Creating this request starts fulfillment; it does not mean the account is ready.",
    "ncr_ticket": "The ServiceNow NCR/network request (REQ/RITM/SCTASK) used to allocate and configure networking.",
    "vpc_cidr": "The actual approved, non-overlapping address range returned by Networking after the NCR is fulfilled.",
    "vpc_cidr_ilp": "Multi-VPC only: the approved ILP VPC address range returned by Networking.",
    "vpc_cidr_cgs": "Multi-VPC only: the approved CGS VPC address range returned by Networking.",
    "vpc_cidr_pmv": "Multi-VPC only: the approved PMV VPC address range returned by Networking.",
    "vpc_cidr_cr": "Multi-VPC only: the approved CR VPC address range returned by Networking.",
    "firewall_zone": "The actual network security zone assigned by Networking after request fulfillment.",
    "dns_domain": "Only the shared base DNS suffix. Praxis generates the environment-specific hostnames.",
    "rancher_target": "Select the Rancher management cluster that will own this environment. These are the same cluster choices used by the Rancher card in every Praxis wizard, keeping the handoff and later setup aligned.",
    "release_archive": "The approved product release bundle that must be available before deployment.",
    "eks_version": "The EKS Kubernetes version that the selected AWS region currently supports, sourced from the shared AWS catalog used by the Praxis wizard.",
    "eks_instance_types": "The primary/default EC2 instance type planned for EKS. Detailed per-node-group sizing remains in the Praxis wizard. Choices come from the shared regional AWS catalog.",
    "aurora_version": "The Aurora engine version available in the selected AWS region for this setup's database engine.",
    "aurora_instance_type": "The regional Aurora DB instance class planned for this environment.",
    "redis_instance_type": "The ElastiCache node type available in the selected AWS region.",
    "vault_database_version": "Catalyst only: the PostgreSQL engine version for the Vault database, sourced from the regional RDS catalog.",
    "vault_database_instance_type": "Catalyst only: the regional RDS instance class for the Vault database.",
    "name_limit_confirmation": "Checks the documented Terraform EKS node-group name_prefix failure. The prefix must be 1–38 characters because Terraform appends a generated suffix to produce the final AWS EKS node-group name, whose maximum is 63 characters.",
    "risks": "Known conditions that may delay or change the environment delivery. Enter None when empty.",
    "assumptions": "Decisions currently treated as true but not yet independently verified. Enter None when empty.",
    "testing_owner": "The person accountable for validating the completed environment and accepting the handoff.",
    "additional_notes": "Optional context that will help the receiving team understand this request.",
}

REQUIREMENT_HELP = {
    "aws_account": "DevOps needs working account access and permissions before any infrastructure can be created.",
    "naming": "Conflicting names propagate into AWS, Git, Rancher, DNS, and Spacelift and are costly to correct later.",
    "cidr": "The VPC and subnet ranges must be approved and non-overlapping before network resources are created.",
    "firewall": "The security zone determines routing and which PROD/NONPROD firewall policies apply.",
    "tgw": "This is work for the Networking team, not Praxis or the form operator. Networking must configure/share the Transit Gateway in the target AWS account, ensure the account can use it, and complete the environment VPC attachment. Evidence should identify the Networking ticket plus the Transit Gateway share and VPC attachment.",
    "vpce": "The correct regional endpoint service is required for private platform connectivity.",
    "internet": "EKS nodes need verified outbound access to retrieve images, packages, and AWS service responses.",
    "vpn": "Operators must be able to reach private environment endpoints from approved office or VPN networks.",
    "rancher": "The new cluster must be able to register with and remain reachable from the selected Rancher target.",
    "vault": "Workloads require network access to shared Vault for authentication and secret retrieval.",
    "harbor": "Nodes and deployment tooling must reach Harbor to pull the approved container images.",
    "iam_access": "These roles must already exist in the target AWS account: the Spacelift execution role used by Praxis automation and the administrator role used by DevOps engineers for setup, verification, and troubleshooting.",
    "grafana": "Grafana region and firewall access are needed when monitoring is part of this environment.",
    "jumphost": "A jump host and its access path are required when private resources cannot be reached directly.",
}

REQUIREMENT_GROUPS: tuple[dict[str, Any], ...] = (
    {
        "key": "networking",
        "title": "Networking Prerequisites",
        "description": "Confirm connectivity, routing, DNS, and shared platform endpoints.",
        "items": (
            ("cidr", "CIDR and subnet allocation is approved"),
            ("firewall", "Firewall zone and NONPROD/PROD alignment is confirmed"),
            ("tgw", "Networking team has configured the Transit Gateway in the target AWS account and completed the VPC attachment"),
            ("vpce", "VPC endpoint service and target region are confirmed"),
            ("internet", "Outbound internet access for VPC/EKS nodes is verified"),
            ("vpn", "VPN/office network access is verified"),
            ("rancher", "Rancher connectivity from the target cluster is verified"),
            ("vault", "Shared Vault connectivity is verified"),
            ("harbor", "Required Harbor endpoints are reachable"),
        ),
    },
    {
        "key": "aws_platform",
        "title": "AWS Account and Platform Prerequisites",
        "description": "Confirm access and external ownership that must exist before Praxis starts.",
        "items": (
            ("aws_account", "AWS account created and accessible"),
            ("naming", "Environment naming is agreed and consistent"),
            ("iam_access", "IAM roles for Spacelift execution and DevOps administrator access exist"),
        ),
    },
)

CONDITIONAL_REQUIREMENTS: tuple[dict[str, str], ...] = (
    {"flag": "grafana_required", "key": "grafana", "group": "networking", "question": "Does this environment require Grafana monitoring?", "label": "Grafana region and firewall access are confirmed"},
    {"flag": "jumphost_required", "key": "jumphost", "group": "aws_platform", "question": "Does this environment require a jump host?", "label": "Jump host creation and access are confirmed"},
)

IDENTITY_FIELDS: tuple[tuple[str, str], ...] = (
    ("title", "Foundation readiness title"),
    ("customer", "Customer"),
    ("product", "Product/platform"),
    ("setup_type", "Environment setup"),
    ("environment_name", "Environment name"),
    ("environment_type", "Environment type"),
    ("aws_region", "AWS region"),
    ("target_date", "Target date"),
    ("requester", "Requester/owner"),
    ("architect", "Architect"),
    ("devops_receiver", "Receiving DevOps owner"),
    ("jira_epic", "Jira environment epic"),
)

OPTIONAL_IDENTITY_FIELDS: tuple[tuple[str, str], ...] = (
    ("environment_suffix", "Environment prefix (optional)"),
    ("spec_link", "Spec or Spec Workbench link"),
)

TECHNICAL_FIELDS: tuple[tuple[str, str], ...] = (
    ("production_classification", "PROD/NONPROD classification"),
    ("requested_network_size", "Requested network size"),
    ("dns_domain", "Base DNS domain"),
    ("rancher_target", "Rancher target"),
    ("release_archive", "Release archive/version"),
    ("eks_version", "EKS version"),
    ("eks_instance_types", "EKS instance size"),
    ("aurora_version", "Aurora version"),
    ("aurora_instance_type", "Aurora instance size"),
    ("redis_instance_type", "Redis instance type"),
)

SERVICE_REQUEST_FIELDS: tuple[tuple[str, str], ...] = (
    ("aws_service_request", "AWS account ServiceNow request"),
    ("ncr_ticket", "NCR/network ServiceNow request"),
)

ALLOCATED_FIELDS: tuple[tuple[str, str], ...] = (
    ("aws_account_id", "Allocated AWS account ID"),
    ("vpc_cidr", "Allocated VPC CIDR"),
    ("firewall_zone", "Assigned firewall zone"),
)

MULTI_VPC_CIDR_FIELDS: tuple[tuple[str, str], ...] = (
    ("vpc_cidr_ilp", "Allocated ILP VPC CIDR"),
    ("vpc_cidr_cgs", "Allocated CGS VPC CIDR"),
    ("vpc_cidr_pmv", "Allocated PMV VPC CIDR"),
    ("vpc_cidr_cr", "Allocated CR VPC CIDR"),
)

CATALYST_TECHNICAL_FIELDS: tuple[tuple[str, str], ...] = (
    ("vault_database_version", "Vault database engine version"),
    ("vault_database_instance_type", "Vault database instance type"),
)


def all_requirements(data: dict[str, Any]) -> list[dict[str, str]]:
    requirements = []
    for group in REQUIREMENT_GROUPS:
        group_key = str(group["key"])
        requirements.extend(
            {"key": key, "label": label, "group": group_key}
            for key, label in group["items"]
        )
        requirements.extend(
            {"key": item["key"], "label": item["label"], "group": group_key}
            for item in CONDITIONAL_REQUIREMENTS
            if item.get("group", "aws_platform") == group_key
            and str(data.get(item["flag"]) or "") == "yes"
        )
    return requirements


def validate_readiness(data: dict[str, Any], *, request_only: bool = False) -> list[str]:
    errors: list[str] = []
    allocated_fields = tuple(
        field for field in ALLOCATED_FIELDS if field[0] != "vpc_cidr"
    ) + (MULTI_VPC_CIDR_FIELDS if data.get("setup_type") == "catalyst-multi-vpc" else (("vpc_cidr", "Allocated VPC CIDR"),))
    required_fields = (*IDENTITY_FIELDS, *TECHNICAL_FIELDS) if request_only else (*IDENTITY_FIELDS, *TECHNICAL_FIELDS, *SERVICE_REQUEST_FIELDS, *allocated_fields)
    for key, label in required_fields:
        if not str(data.get(key) or "").strip():
            errors.append(f"{label} is required.")

    account_id = str(data.get("aws_account_id") or "").strip()
    if account_id and (not account_id.isdigit() or len(account_id) != 12):
        errors.append("AWS account ID must contain exactly 12 digits.")
    target_date = str(data.get("target_date") or "").strip()
    if target_date:
        try:
            date.fromisoformat(target_date)
        except ValueError:
            errors.append("Target date must use YYYY-MM-DD format.")
    region = str(data.get("aws_region") or "").strip()
    if region and region not in AWS_REGIONS:
        errors.append("AWS region must be one of the supported Praxis AWS regions.")
    classification = str(data.get("production_classification") or "").strip()
    if classification and classification not in {"PROD", "NONPROD"}:
        errors.append("PROD/NONPROD classification must be PROD or NONPROD.")
    environment_name = str(data.get("environment_name") or "").strip()
    if environment_name and (
        len(environment_name) > 40
        or not re.fullmatch(r"[a-z0-9]+(?:-[a-z0-9]+)*", environment_name)
    ):
        errors.append("Environment name must be a lowercase hyphenated name of at most 40 characters.")
    customer = str(data.get("customer") or "").strip()
    if customer and not re.fullmatch(r"[A-Z0-9]{2,12}", customer):
        errors.append("Customer code must contain 2–12 uppercase letters or numbers (for example LNL).")
    if environment_name and customer and not environment_name.startswith(f"{customer.lower()}-"):
        errors.append("Environment name must start with the lowercase customer code followed by a hyphen.")
    environment_type = str(data.get("environment_type") or "").strip()
    if environment_type and environment_type not in ENVIRONMENT_TYPES:
        errors.append("Environment type must be one of the supported Praxis environment types.")
    title = str(data.get("title") or "").strip()
    title_words = " ".join(re.findall(r"[a-z0-9]+", title.casefold()))
    title_aliases = ENVIRONMENT_TYPE_TITLE_ALIASES.get(environment_type, ())
    if title and title_aliases and not any(
        re.search(rf"(?:^| ){re.escape(alias)}(?: |$)", title_words)
        for alias in title_aliases
    ):
        errors.append(
            f"Foundation readiness title must identify the selected {environment_type.upper()} "
            "environment type. Update the title or select the matching environment type."
        )
    if environment_name and environment_type and not re.search(
        rf"(?:^|-){re.escape(environment_type)}(?:-[a-z0-9]+)?$", environment_name
    ):
        errors.append("Environment name must end with its environment type, optionally followed by a suffix.")
    environment_suffix = str(data.get("environment_suffix") or "").strip()
    if environment_suffix and not re.fullmatch(r"[a-z0-9]{1,8}", environment_suffix):
        errors.append("Environment prefix must contain 1–8 lowercase letters or numbers.")
    product = str(data.get("product") or "").strip()
    if product and (len(product) > 40 or not re.fullmatch(r"[A-Za-z0-9]+(?:[ -][A-Za-z0-9]+)*", product)):
        errors.append("Product/platform may contain letters, numbers, spaces, and single hyphens only.")
    setup_type = str(data.get("setup_type") or "").strip()
    if setup_type and setup_type not in SETUP_TYPES:
        errors.append("Environment setup must be one of the supported Praxis setup types.")
    setup = SETUP_TYPES.get(setup_type) or {}
    expected_product = str(setup.get("product") or "")
    if expected_product and product.casefold() != expected_product.casefold():
        errors.append(f"{setup.get('label')} setups must use product/platform {expected_product}.")
    expected_classification = "PROD" if environment_type == "prod" else "NONPROD"
    if environment_type and classification and classification != expected_classification:
        errors.append(
            f"{environment_type.upper()} environments must use {expected_classification} classification."
        )
    platform_prefix = str(setup.get("prefix") or "")
    if not platform_prefix:
        platform_prefix = PRODUCT_PREFIXES.get(product.lower(), "")
    if environment_name and customer and environment_type and platform_prefix:
        expected = f"{customer.lower()}-{platform_prefix}-{environment_type}"
        if environment_suffix:
            expected = f"{expected}-{environment_suffix}"
        if environment_name != expected:
            setup_label = str(setup.get("label") or product or "the selected setup")
            errors.append(
                f"Environment name must be {expected} for {setup_label} and the selected optional prefix."
            )
    cidr_fields = MULTI_VPC_CIDR_FIELDS if data.get("setup_type") == "catalyst-multi-vpc" else (("vpc_cidr", "VPC CIDR"),)
    networks: list[tuple[str, ipaddress.IPv4Network]] = []
    for cidr_key, cidr_label in cidr_fields:
        vpc_cidr = str(data.get(cidr_key) or "").strip()
        if not vpc_cidr:
            continue
        try:
            network = ipaddress.IPv4Network(vpc_cidr, strict=True)
        except ValueError:
            errors.append(f"{cidr_label} must be a valid IPv4 network such as 10.226.128.0/21.")
            continue
        for other_label, other in networks:
            if network.overlaps(other):
                errors.append(f"{cidr_label} overlaps {other_label} ({other}).")
        networks.append((cidr_label, network))
    spec_link = str(data.get("spec_link") or "").strip()
    if spec_link and not re.match(r"https?://", spec_link, re.IGNORECASE):
        errors.append("Approved spec must be an http(s) Confluence or Spec Workbench link.")
    dns_domain = str(data.get("dns_domain") or "").strip()
    base_dns_label = r"[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?"
    if dns_domain and not re.fullmatch(rf"{base_dns_label}\.{base_dns_label}", dns_domain):
        errors.append(
            "Base DNS domain must contain only the base suffix, such as example.com or natlot.be; "
            "the wizard creates the full environment names."
        )
    elif dns_domain and dns_domain not in BASE_DNS_DOMAINS:
        errors.append("Base DNS domain must be one of the supported shared DNS domains.")
    rancher_target = str(data.get("rancher_target") or "").strip()
    if rancher_target and rancher_target not in {value for value, _label in RANCHER_EKS_CLUSTERS}:
        errors.append("Rancher target must be one of the supported Praxis management clusters.")
    eks_version = str(data.get("eks_version") or "").strip()
    if eks_version and not re.fullmatch(r"1\.\d{2}", eks_version):
        errors.append("EKS version must be a supported Kubernetes 1.x version such as 1.34.")
    eks_instance_types = str(data.get("eks_instance_types") or "").strip()
    if eks_instance_types and not re.fullmatch(r"(?=[^.]*\d)[a-z0-9-]+\.[a-z0-9]+", eks_instance_types):
        errors.append("EKS instance type must be a valid EC2 instance type such as m6i.large.")
    for key, label, prefix in (
        ("aurora_instance_type", "Aurora instance type", "db."),
        ("redis_instance_type", "Redis instance type", "cache."),
        ("vault_database_instance_type", "Vault database instance type", "db."),
    ):
        value = str(data.get(key) or "").strip()
        if value and value != "db.serverless" and not re.fullmatch(rf"{re.escape(prefix)}(?=[^.]*\d)[a-z0-9-]+\.[a-z0-9]+", value):
            errors.append(f"{label} must use the expected {prefix} AWS resource prefix.")
    for key, label in (
        ("aurora_version", "Aurora engine version"),
        ("vault_database_version", "Vault database engine version"),
    ):
        value = str(data.get(key) or "").strip()
        if value and not re.fullmatch(r"\d+(?:\.\d+)*(?:\.[A-Za-z0-9_-]+)*", value):
            errors.append(f"{label} must be a numeric AWS engine version.")
    release_archive = str(data.get("release_archive") or "").strip()
    if release_archive and not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]{0,126}(?:\.tar\.gz|\.tgz)", release_archive):
        errors.append("Release archive must be a safe .tar.gz or .tgz filename.")
    jira_epic = str(data.get("jira_epic") or "").strip()
    if jira_epic and not (re.fullmatch(r"[A-Z][A-Z0-9]+-\d+", jira_epic) or re.match(r"https?://", jira_epic, re.I)):
        errors.append("Jira environment epic must be an uppercase Jira key or an http(s) URL.")
    ncr_ticket = str(data.get("ncr_ticket") or "").strip()
    if ncr_ticket and not (re.fullmatch(r"[A-Z][A-Z0-9]+-?\d+", ncr_ticket) or re.match(r"https?://", ncr_ticket, re.I)):
        errors.append("NCR ticket must be an uppercase ticket/reference key or an http(s) URL.")
    aws_service_request = str(data.get("aws_service_request") or "").strip()
    if aws_service_request and not re.fullmatch(r"[A-Z][A-Z0-9]+-?\d+", aws_service_request):
        errors.append("AWS account ServiceNow request must be an uppercase REQ or RITM reference.")
    if str(data.get("setup_type") or "").startswith("catalyst-"):
        for key, label in (
            ("vault_database_version", "Vault database engine version"),
            ("vault_database_instance_type", "Vault database instance type"),
        ):
            if not str(data.get(key) or "").strip():
                errors.append(f"{label} is required for Catalyst setups.")

    for item in CONDITIONAL_REQUIREMENTS:
        if str(data.get(item["flag"]) or "") not in {"yes", "no"}:
            errors.append(f"Answer whether {item['label'].lower()} is applicable.")

    from .camunda import validate as validate_camunda
    errors.extend(validate_camunda(data))

    if request_only:
        return errors

    requirement_data = data.get("requirements") if isinstance(data.get("requirements"), dict) else {}
    for item in all_requirements(data):
        values = requirement_data.get(item["key"])
        values = values if isinstance(values, dict) else {}
        status = str(values.get("status") or "")
        owner = str(values.get("owner") or "").strip()
        evidence = str(values.get("evidence") or "").strip()
        notes = str(values.get("notes") or "").strip()
        if status != "confirmed":
            errors.append(f"{item['label']} must be confirmed and verified.")
        if status == "blocked" and not notes:
            errors.append(f"{item['label']} is blocked and needs an explanation in Notes.")
        if not owner:
            errors.append(f"{item['label']} needs an owner.")
        if not evidence:
            errors.append(f"{item['label']} needs evidence or a ticket reference.")

    if str(data.get("networking_attested") or "") != "yes":
        errors.append("Networking attestation must be confirmed by the responsible Networking administrator.")
    if str(data.get("aws_platform_attested") or "") != "yes":
        errors.append("AWS and Platform attestation must be confirmed by the responsible AWS/Platform administrator.")

    for key, label in (("risks", "Risks"), ("assumptions", "Assumptions"), ("testing_owner", "Testing/sign-off owner")):
        if not str(data.get(key) or "").strip():
            errors.append(f"{label} is required. Enter 'None' when there are no known items.")
    return errors
