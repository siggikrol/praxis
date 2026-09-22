"""Navigation and readable summaries for finish-page validation errors."""
import re

LABELS = {
    "project_settings": "Project Settings", "repository_settings": "Repository Settings",
    "spacelift_settings": "Spacelift Settings", "replacements": "Replacements",
    "common": "Common Settings", "rancher": "Rancher", "vpc": "VPC",
    "subnets": "Subnets", "eks": "EKS", "aurora": "Aurora", "redis": "Redis",
    "vault_database": "Vault Database", "formkiq": "FormKiQ", "grafana": "Grafana",
    "alb": "ALB", "camunda_opensearch": "Camunda OpenSearch", "pc_source": "PC Source",
}


def validation_display(error):
    message = str(error)
    release = bool(getattr(error, "deployment_versions_link", False))
    step = None
    if not release:
        if message.startswith("Camunda OpenSearch"):
            step = "camunda_opensearch"
        elif "external_alb_allowed_ips" in message:
            step = "alb"
        else:
            for key in LABELS:
                if re.search(r"(?<![\w])" + re.escape(key) + r"(?![\w])", message, re.I):
                    step = key
                    break
    message = message.replace("opensearch_additional_cidr_blocks:", "Additional worker CIDRs:")
    return {"message": message, "step": step, "label": LABELS.get(step, "Deployment Versions" if release else "Setup"), "deployment_versions_link": release}
