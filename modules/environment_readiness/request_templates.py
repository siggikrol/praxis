from __future__ import annotations

from typing import Any

from .connectivity_manifest import build_connectivity_manifest
from modules.ncr.networking import applies, fingerprint, render


def _text(data: dict[str, Any], key: str) -> str:
    return str(data.get(key) or "").strip()


def _common(data: dict[str, Any], draft_url: str) -> list[dict[str, str]]:
    return [
        {"key": "requested_for", "label": "Requested for", "value": _text(data, "requester"), "type": "text"},
        {"key": "customer", "label": "Customer", "value": _text(data, "customer"), "type": "text"},
        {"key": "environment_name", "label": "Environment name", "value": _text(data, "environment_name"), "type": "text"},
        {"key": "setup_type", "label": "Praxis setup", "value": _text(data, "setup_type").replace("-", " ").title(), "type": "text"},
        {"key": "environment_type", "label": "Environment type", "value": _text(data, "environment_type"), "type": "text"},
        {"key": "production_classification", "label": "Classification", "value": _text(data, "production_classification"), "type": "text"},
        {"key": "aws_region", "label": "Requested AWS region", "value": _text(data, "aws_region"), "type": "text"},
        {"key": "target_date", "label": "Required by", "value": _text(data, "target_date"), "type": "date"},
        {"key": "jira_epic", "label": "Jira environment epic", "value": _text(data, "jira_epic"), "type": "text"},
        {"key": "architect", "label": "Architect", "value": _text(data, "architect"), "type": "text"},
        {"key": "devops_receiver", "label": "Receiving DevOps owner", "value": _text(data, "devops_receiver"), "type": "text"},
        {"key": "draft_url", "label": "Praxis readiness draft", "value": draft_url, "type": "url"},
    ]


def build_request_template(
    request_type: str, data: dict[str, Any], *, draft_url: str, include_details: bool = False, content_bundle=None
) -> dict[str, Any]:
    environment = _text(data, "environment_name") or "new environment"
    common = _common(data, draft_url)
    if request_type == "aws":
        fields = [
            {
                "key": "short_description", "label": "Short description",
                "value": f"Create AWS account for {environment}", "type": "text",
            },
            *common,
            {
                "key": "account_purpose", "label": "Account purpose",
                "value": f"Praxis foundation account for {environment}", "type": "textarea",
            },
            {
                "key": "required_access", "label": "Required access",
                "value": "Spacelift execution role and DevOps administrator access", "type": "textarea",
            },
            {
                "key": "business_justification", "label": "Business justification / notes",
                "value": _text(data, "additional_notes") or f"Required so Praxis provisioning can start for {environment}.",
                "type": "textarea",
            },
        ]
        return {
            "request_type": "aws", "title": "AWS Account Service Request",
            "description": "Request the account and access prerequisites needed before Praxis can provision the foundation.",
            "fields": fields,
        }
    if request_type == "ncr":
        connectivity = build_connectivity_manifest(data, include_details=include_details, content_bundle=content_bundle)
        definition = connectivity["presentation"]
        context = {**connectivity["context"], "environment": environment,
                   "draft_url": draft_url, "manifest": connectivity["text"]}
        conditions = {**data, "include_details": include_details, "has_additional_notes": bool(_text(data, "additional_notes")), "has_allocation_review": bool(context["profile"]["allocation"].get("review"))}
        fields = [{**field, "value": render(field["value"], context).strip()}
                  for field in definition["fields"] if applies(field, conditions)
                  and (include_details or field["key"] != "draft_url")]
        versions = {key: connectivity[key] for key in ("profile_hash", "template_hash")}
        versions["template_hash"] = fingerprint({"template": versions["template_hash"], "omit_draft_url": True})
        return {
            "request_type": "ncr", "title": render(definition["title"], context),
            "description": render(definition["description"], context), "fields": fields,
            **versions, "content_version": fingerprint(versions),
            "requires_confirmation": [note for note in context['profile'].get('requires_confirmation', [])
                                      if not (context.get('rancher_url') and 'BDE ingress Rancher' in note)],
        }
    raise ValueError("Unsupported ServiceNow request type.")


def template_values(template: dict[str, Any]) -> dict[str, str]:
    return {
        str(field.get("key")): str(field.get("value") or "").strip()
        for field in template.get("fields", [])
        if isinstance(field, dict) and field.get("key")
    }


def template_text(template: dict[str, Any]) -> str:
    lines = [str(template.get("title") or "ServiceNow Request"), ""]
    for field in template.get("fields", []):
        if not isinstance(field, dict):
            continue
        lines.extend((str(field.get("label") or field.get("key") or "Field"), str(field.get("value") or "-"), ""))
    return "\n".join(lines).strip() + "\n"
