"""Presentation of existing wizard forms; no infrastructure schema changes."""
STAGES = ("Environment", "Cloud", "Architecture", "Capabilities", "Review")
ARCHITECTURES = {
    "single-vpc": "Single-VPC", "multi-vpc": "Multi-VPC", "unified": "Combined architecture",
    "rgs": "Application platform", "loyalty": "Application services",
}
LABELS = {
    "project_settings": "Environment", "common": "Cloud and shared AWS settings",
    "vpc": "Networking · VPC", "subnets": "Networking · Subnets",
    "subnets_default": "Networking · Subnets", "eks": "Container Platform · EKS",
    "aurora": "Relational Database · Aurora", "redis": "Cache · Redis",
    "alb": "Load Balancing · ALB", "vault_database": "Secrets · Vault database",
    "rancher": "Container Platform · Rancher", "sumologic": "Observability · Logs",
    "grafana": "Observability · Grafana", "finish": "Review Environment",
}
ENVIRONMENT_LABELS = {"dev": "Development", "test": "Test", "sit": "SIT", "uat": "UAT",
                      "staging": "Staging", "prod": "Production", "preprod": "Pre-production"}
METADATA_FIELDS = ("environment_name", "environment_description", "environment_owner")


def stage_for(step):
    if step == "project_settings":
        return "Environment"
    if step == "common":
        return "Cloud"
    if step == "finish":
        return "Review"
    if step in {"repository_settings", "spacelift_settings", "replacements", "vpc", "subnets",
                "subnets_default", "availability_zones", "filter_az_zone_ids", "external_alb_allowed_ips"}:
        return "Architecture"
    return "Capabilities"


def ordered_steps(steps):
    # Stable within each group: existing form keys and their relative order are retained.
    return sorted(steps, key=lambda item: STAGES.index(stage_for(item[0])))


def environment_review(env_slug, state):
    def saved(key):
        value = state.get(f"{env_slug}:{key}")
        return value if isinstance(value, dict) else {}

    project, common = saved("project_settings"), saved("common")
    def values(data, key):
        found = []
        if isinstance(data, dict):
            for name, value in data.items():
                if name == key and value:
                    found.append(str(value))
                elif isinstance(value, (dict, list)):
                    found.extend(values(value, key))
        elif isinstance(data, list):
            for item in data:
                found.extend(values(item, key))
        return found

    capabilities = []
    for key, label in (("vpc", "Networking"), ("eks", "Container Platform"),
                       ("aurora", "Relational Database"), ("redis", "Cache"),
                       ("alb", "Load Balancing"), ("vault_database", "Secrets"),
                       ("sumologic", "Observability")):
        data = saved(key)
        status = "Configured" if data else "Not configured"
        if key == "aurora" and data:
            engines = values(data, "engine")
            status = ", ".join(dict.fromkeys(e.replace("aurora-postgresql", "PostgreSQL").replace("aurora-mysql", "MySQL") for e in engines)) or "Aurora configured"
        capabilities.append((label, status))
    return {
        "name": project.get("environment_name") or common.get("environment") or "Untitled environment",
        "types": [ENVIRONMENT_LABELS.get(e, e.title()) for e in project.get("environment_type", [])],
        "description": project.get("environment_description") or "",
        "owner": project.get("environment_owner") or common.get("support_organization") or "Not specified",
        "region": common.get("aws_region") or "Region not selected",
        "architecture": ARCHITECTURES.get(env_slug, "Custom architecture"),
        "capabilities": capabilities,
    }
