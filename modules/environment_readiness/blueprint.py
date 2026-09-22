from __future__ import annotations
from .camunda import FIELDS as CAMUNDA_FIELDS, enabled as camunda_enabled, catalogs as camunda_catalogs, validate as validate_camunda

import csv
from datetime import datetime, timezone
import io
import logging
import os
import re
from typing import Any
from urllib.parse import quote
from uuid import uuid4

from flask import Blueprint, Response, abort, current_app, flash, jsonify, redirect, render_template, request, session, url_for
from itsdangerous import BadSignature, URLSafeTimedSerializer
from constants import AWS_REGIONS as WIZARD_AWS_REGION_CHOICES, RANCHER_EKS_CLUSTERS, RANCHER_CLUSTER_TO_URL

from .requirements import (
    CONDITIONAL_REQUIREMENTS,
    AWS_REGIONS,
    BASE_DNS_DOMAINS,
    CATALYST_TECHNICAL_FIELDS,
    ENVIRONMENT_TYPES,
    ENVIRONMENT_TYPE_TITLE_ALIASES,
    FIELD_HELP,
    IDENTITY_FIELDS,
    ALLOCATED_FIELDS,
    OPTIONAL_IDENTITY_FIELDS,
    MULTI_VPC_CIDR_FIELDS,
    PRODUCT_PREFIXES,
    REQUIREMENT_GROUPS,
    REQUIREMENT_HELP,
    SETUP_TYPES,
    SERVICE_REQUEST_FIELDS,
    STATUS_CHOICES,
    TECHNICAL_FIELDS,
    all_requirements,
    validate_readiness,
)
from .origins import ORIGIN_GROUPS, SOURCE_DOCUMENTS
from .request_templates import build_request_template, template_text, template_values
from .servicenow import ServiceNowClient, ServiceNowError, ServiceNowOutcomeUnknown
from modules.ncr.networking import NetworkingContentError
from copy import deepcopy
from .store import (
    DraftWriteConflict,
    claim_servicenow_submission,
    clarify_setup,
    complete_servicenow_submission,
    complete_setup_bootstrap,
    delete_draft,
    find_completed_setup_by_source_draft,
    find_current_setup_by_environment,
    export_setup_rows,
    get_draft,
    get_servicenow_submission,
    get_setup,
    find_active_draft_by_environment,
    list_all_drafts,
    list_drafts,
    list_setups,
    link_wizard_workspace,
    publish_setup,
    reassign_setup_devops_owner,
    reopen_completed_setup,
    release_servicenow_submission,
    revision_changes,
    save_draft,
    setup_dashboard,
    setup_insights,
    transfer_setup_ownership,
    transition_setup,
)
from .workflow import VERIFICATION_INPUTS, readiness_summary
from .wizard_handoff import build_wizard_state, merge_wizard_state, wizard_for_setup
from engine.wizards.components import new_component_profile


logger = logging.getLogger(__name__)


bp = Blueprint(
    "environment_readiness",
    __name__,
    url_prefix="/environment-readiness",
    template_folder="templates",
)


@bp.errorhandler(DraftWriteConflict)
def stale_draft_conflict(error: DraftWriteConflict):
    return Response(str(error), status=409, mimetype="text/plain")


@bp.errorhandler(NetworkingContentError)
def networking_content_error(error):
    return Response(str(error), status=422, mimetype='text/plain')

SESSION_KEY = "environment_readiness:draft"
DRAFTS_KEY = "environment_readiness:drafts"
ACTIVE_DRAFT_KEY = "environment_readiness:active_draft"


def _clean(value: Any, *, limit: int = 2000) -> str:
    return str(value or "").strip()[:limit]


def _handoff_fields(data: dict[str, Any]) -> tuple[tuple[str, str], ...]:
    allocated = tuple(field for field in ALLOCATED_FIELDS if field[0] != "vpc_cidr")
    allocated += MULTI_VPC_CIDR_FIELDS if data.get("setup_type") == "catalyst-multi-vpc" else (("vpc_cidr", "Allocated VPC CIDR"),)
    catalyst = CATALYST_TECHNICAL_FIELDS if str(data.get("setup_type") or "").startswith("catalyst-") else ()
    return (*IDENTITY_FIELDS, *OPTIONAL_IDENTITY_FIELDS, *TECHNICAL_FIELDS, *allocated, *catalyst, *(CAMUNDA_FIELDS if camunda_enabled(data) else ()))


def _logged_in_user() -> str:
    """Return the common identity populated by both basic and Azure AD authentication."""
    return _clean(session.get("user"), limit=200)


def _identity_matches(identity: str, assignment: str) -> bool:
    identity = identity.strip().casefold()
    assignment = assignment.strip().casefold()
    return bool(identity and assignment and identity == assignment)


def _is_setup_admin(user: str | None = None) -> bool:
    identity = (user if user is not None else _logged_in_user()).strip().casefold()
    configured_admins = os.getenv("ENVIRONMENT_READINESS_ADMIN_USERS", "admin")
    admins = {value.strip().casefold() for value in configured_admins.split(",") if value.strip()}
    return bool(identity and identity in admins)


def _can_edit_setup(record: dict[str, Any]) -> bool:
    """The authoritative owner controls definition changes and revisions."""
    if not current_app.config.get("AUTH_ENABLED", False):
        return True
    user = _logged_in_user()
    return _is_setup_admin(user) or bool(
        user and user.casefold() == str(record.get("setup_owner") or "").strip().casefold()
    )


def _can_operate_setup(record: dict[str, Any]) -> bool:
    """The setup owner and Receiving DevOps can operate the setup lifecycle."""
    if not current_app.config.get("AUTH_ENABLED", False):
        return True
    user = _logged_in_user()
    return (
        _is_setup_admin(user)
        or _identity_matches(user, str(record.get("setup_owner") or ""))
        or _identity_matches(user, str(record.get("devops_owner") or ""))
    )


def _can_reopen_setup(record: dict[str, Any]) -> bool:
    """Only the authoritative owner may restart work after completion."""
    if not current_app.config.get("AUTH_ENABLED", False):
        return True
    return _identity_matches(_logged_in_user(), str(record.get("setup_owner") or ""))


def _require_setup_editor(record: dict[str, Any]) -> None:
    if not _can_edit_setup(record):
        abort(403)


def _require_setup_operator(record: dict[str, Any]) -> None:
    if not _can_operate_setup(record):
        abort(403)


def _known_praxis_users() -> list[str]:
    try:
        from services.session_activity import list_known_users

        users = list_known_users(
            session_dir=current_app.config.get("SESSION_FILE_DIR", "/tmp/flask_sessions")
        )
    except Exception:
        users = []
    current_user = _logged_in_user()
    if current_user:
        users.append(current_user)
    return sorted(set(users), key=str.casefold)


def _cached_aws_account_choices() -> list[tuple[str, str]]:
    try:
        from engine.wizards.forms.spacelift_settings import get_cached_aws_account_choices

        return get_cached_aws_account_choices()
    except Exception:
        return []


def _prefill_servicenow_evidence(data: dict[str, Any]) -> bool:
    """Use request references as default evidence without replacing specific evidence."""
    requirements = data.get("requirements")
    if not isinstance(requirements, dict):
        requirements = {}
        data["requirements"] = requirements
    references = {
        "networking": _clean(data.get("ncr_ticket"), limit=80),
        "aws_platform": _clean(data.get("aws_service_request"), limit=80),
    }
    changed = False
    grouped_keys = {
        str(group["key"]): [key for key, _label in group["items"]]
        for group in REQUIREMENT_GROUPS
    }
    for item in CONDITIONAL_REQUIREMENTS:
        grouped_keys.setdefault(str(item.get("group") or "aws_platform"), []).append(item["key"])
    for item in all_requirements(data):
        if item["key"] not in grouped_keys.setdefault(item["group"], []):
            grouped_keys[item["group"]].append(item["key"])
    for group, keys in grouped_keys.items():
        reference = references.get(group, "")
        if not reference:
            continue
        for key in keys:
            values = requirements.get(key)
            if not isinstance(values, dict):
                values = {}
                requirements[key] = values
            if not _clean(values.get("evidence"), limit=500):
                values["evidence"] = reference
                changed = True
    return changed


def _payload_from_form(previous: dict[str, Any] | None = None) -> dict[str, Any]:
    if not isinstance(previous, dict):
        submitted_id = _clean(request.form.get("draft_id"), limit=40)
        previous = (
            get_draft(submitted_id, _logged_in_user() or "anonymous") or {}
            if submitted_id else _saved_data()
        )
    data: dict[str, Any] = {
        key: _clean(request.form.get(key))
        for key, _label in (*IDENTITY_FIELDS, *OPTIONAL_IDENTITY_FIELDS, *TECHNICAL_FIELDS, *SERVICE_REQUEST_FIELDS, *ALLOCATED_FIELDS, *MULTI_VPC_CIDR_FIELDS, *CATALYST_TECHNICAL_FIELDS, *CAMUNDA_FIELDS)
    }
    # Profile metadata is server-owned. Legacy drafts retain their historical scope.
    if previous.get("component_profile"):
        data["component_profile"] = previous["component_profile"]
    elif request.form.get("upgrade_component_profile") == "yes":
        data["component_profile"] = new_component_profile("single-vpc")
    if current_app.config.get("AUTH_ENABLED", False) and _logged_in_user():
        data["requester"] = _logged_in_user()
    elif not data.get("requester"):
        data["requester"] = _logged_in_user()
    for item in CONDITIONAL_REQUIREMENTS:
        data[item["flag"]] = _clean(request.form.get(item["flag"]), limit=8)
    for key in ("risks", "assumptions", "testing_owner", "additional_notes"):
        data[key] = _clean(request.form.get(key), limit=5000)
    for key in (
        "aws_service_request_url", "aws_service_request_status",
        "ncr_ticket_url", "ncr_ticket_status",
    ):
        data[key] = _clean(request.form.get(key), limit=1000)

    requirements: dict[str, dict[str, str]] = {}
    known_keys = {item["key"] for item in all_requirements({item["flag"]: "yes" for item in CONDITIONAL_REQUIREMENTS})}
    for key in known_keys:
        requirements[key] = {
            "status": _clean(request.form.get(f"req_{key}_status"), limit=20),
            "owner": _clean(request.form.get(f"req_{key}_owner"), limit=200),
            "evidence": _clean(request.form.get(f"req_{key}_evidence"), limit=500),
            "notes": _clean(request.form.get(f"req_{key}_notes"), limit=1000),
        }
    # Resolve section defaults before validation/audit; row overrides remain explicit.
    item_groups = {item["key"]: item["group"] for item in all_requirements(data)}
    for group, ticket in (("networking", "ncr_ticket"), ("aws_platform", "aws_service_request")):
        for field in ("owner", "evidence"):
            name = f"{group}_default_{field}"
            data[name] = _clean(request.form.get(name, previous.get(name, "")), limit=500)
        for key, row in requirements.items():
            if item_groups.get(key) != group:
                continue
            for field in ("owner", "evidence"):
                if not row[field]:
                    default = data[f"{group}_default_{field}"] or (data.get(ticket, "") if field == "evidence" else "")
                    row[field] = default
                    if default:
                        row[f"{field}_inherited"] = True
    previous_rows = previous.get("requirements") or {}
    for key, row in requirements.items():
        old = previous_rows.get(key, {})
        changed_row = any(row.get(field) != old.get(field) for field in row)
        # Retain historical text without requiring or accepting a new result field.
        if old.get("verification_result"):
            row["verification_result"] = old["verification_result"]
        if row["status"] == "confirmed" and row["owner"] and row["evidence"]:
            row["verified_by"] = (_logged_in_user() or "Unknown user") if changed_row or not old.get("verified_by") else old["verified_by"]
            row["verified_at"] = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC") if changed_row or not old.get("verified_at") else old["verified_at"]
    if "camunda_opensearch" in previous_rows:
        requirements["camunda_opensearch"] = previous_rows["camunda_opensearch"]  # Historical only.
    data["requirements"] = requirements
    requirement_groups: dict[str, set[str]] = {
        str(group["key"]): {key for key, _label in group["items"]}
        for group in REQUIREMENT_GROUPS
    }
    for item in CONDITIONAL_REQUIREMENTS:
        requirement_groups.setdefault(str(item.get("group") or "aws_platform"), set()).add(item["key"])
    now = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    previous_requirements = previous.get("requirements") if isinstance(previous.get("requirements"), dict) else {}
    for group in ("networking", "aws_platform"):
        attested = request.form.get(f"{group}_attested") == "yes"
        data[f"{group}_attested"] = "yes" if attested else ""
        keys = requirement_groups.get(group, set())
        changed = any(requirements.get(key, {}) != previous_requirements.get(key, {}) for key in keys)
        if attested:
            data[f"{group}_attested_by"] = (
                _logged_in_user() if changed or previous.get(f"{group}_attested") != "yes"
                else _clean(previous.get(f"{group}_attested_by"), limit=200)
            ) or "Unknown user"
            data[f"{group}_attested_at"] = (
                now if changed or previous.get(f"{group}_attested") != "yes"
                else _clean(previous.get(f"{group}_attested_at"), limit=40)
            ) or now
        else:
            data[f"{group}_attested_by"] = ""
            data[f"{group}_attested_at"] = ""
    _prefill_servicenow_evidence(data)
    saved_templates = previous.get("servicenow_templates")
    if isinstance(saved_templates, dict):
        data["servicenow_templates"] = saved_templates
    for key in (
        "_change_impacts",
        "_reopened_from_setup_id",
        "_reopened_from_logical_id",
        "_reopened_from_revision",
        "_reopen_reason",
        "_reopened_at",
    ):
        if previous.get(key) not in (None, ""):
            data[key] = previous[key]
    if previous.get("_version") is not None:
        data["_version"] = previous["_version"]
    return data


def _markdown_cell(value: Any) -> str:
    return _clean(value, limit=5000).replace("|", "\\|").replace("\n", "<br>") or "-"


def build_markdown(data: dict[str, Any], *, enforce_readiness: bool = True) -> str:
    errors = validate_readiness(data)
    if errors and enforce_readiness:
        raise ValueError("Foundation prerequisite handoff is incomplete.")

    lines = [
        f"# {_clean(data.get('title'))}",
        "",
        "## Foundation Provisioning Readiness",
        "",
        "**Status:** READY TO START PRAXIS PROVISIONING",
        f"**Generated:** {datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M UTC')}",
        "",
        ("> Every prerequisite required to begin environment foundation provisioning was reported as confirmed and includes an owner and evidence reference."
         if not errors else
         "> Historical published snapshot. Some fields do not meet the current Provisioning Entry Gate rules; the stored values below are preserved unchanged."),
        "> Publication requests acknowledgement from the receiving DevOps owner. Provisioning may begin only after that acknowledgement is recorded in Praxis.",
        "> This record does not certify final environment, application, testing, or production readiness.",
        "",
        "## Environment Handoff",
        "",
        "| Field | Value |",
        "|---|---|",
    ]
    for key, label in _handoff_fields(data):
        value = _clean(data.get(key), limit=5000)
        rendered_value = _markdown_cell(value)
        if key == "jira_epic" and re.match(r"https?://", value, re.I):
            jira_url = value
            rendered_value = f"[{_markdown_cell(value)}]({jira_url})"
        elif key == "spec_link" and re.match(r"https?://", value, re.I):
            rendered_value = f"[Open specification]({value})"
        lines.append(f"| {label} | {rendered_value} |")

    lines.extend(
        [
            "",
            "## Issued Requests and Working Links",
            "",
            "| Request | Reference | Status | Link |",
            "|---|---|---|---|",
        ]
    )
    for label, reference_key, status_key, url_key in (
        ("AWS account request", "aws_service_request", "aws_service_request_status", "aws_service_request_url"),
        ("NCR / network request", "ncr_ticket", "ncr_ticket_status", "ncr_ticket_url"),
    ):
        request_url = _clean(data.get(url_key), limit=1000)
        link = f"[Open request]({request_url})" if re.match(r"https?://", request_url, re.I) else "-"
        status = _clean(data.get(status_key), limit=100).replace("_", " ").title() or "Reference recorded"
        lines.append(
            f"| {label} | {_markdown_cell(data.get(reference_key))} | {_markdown_cell(status)} | {link} |"
        )

    requirement_values = data.get("requirements") or {}
    lines.extend(
        [
            "",
            "## Verified Prerequisites",
            "",
            "| Requirement | Status | Owner | Evidence / Ticket | Verified by | Verified at | Notes |",
            "|---|---|---|---|---|---|---|",
        ]
    )
    for item in all_requirements(data):
        values = requirement_values.get(item["key"], {})
        status = _clean(values.get("status"), limit=20).replace("_", " ").title() or "Not recorded"
        lines.append(
            f"| {_markdown_cell(item['label'])} | {status} | {_markdown_cell(values.get('owner'))} | "
            f"{_markdown_cell(values.get('evidence'))} | "
            f"{_markdown_cell(values.get('verified_by'))} | {_markdown_cell(values.get('verified_at'))} | {_markdown_cell(values.get('notes'))} |"
        )

    lines.extend(["", "### Technical team attestations", ""])
    for group, label in (("networking", "Networking"), ("aws_platform", "AWS and Platform")):
        if data.get(f"{group}_attested") == "yes":
            lines.append(
                f"- [x] **{label}:** Implemented, verified, and tested successfully — "
                f"{_markdown_cell(data.get(f'{group}_attested_by'))}, "
                f"{_markdown_cell(data.get(f'{group}_attested_at'))}"
            )
        else:
            lines.append(f"- [ ] **{label}:** Attestation not recorded")

    lines.extend(
        [
            "",
            "## Risks, Assumptions, and Sign-off",
            "",
            f"**Known risks:** {_markdown_cell(data.get('risks'))}",
            "",
            f"**Assumptions:** {_markdown_cell(data.get('assumptions'))}",
            "",
            f"**Testing/sign-off owner:** {_markdown_cell(data.get('testing_owner'))}",
            "",
            f"**Additional notes:** {_markdown_cell(data.get('additional_notes'))}",
            "",
            "## Foundation Provisioning Decision",
            "",
            "The requester/architect confirms that the prerequisites above are complete and that the receiving DevOps team may begin environment foundation provisioning with Praxis. Final environment and product acceptance remain separate activities.",
            "",
        ]
    )
    return "\n".join(lines)


def _saved_data() -> dict[str, Any]:
    active_draft_id = _clean(session.get(ACTIVE_DRAFT_KEY), limit=40)
    if active_draft_id:
        draft = get_draft(active_draft_id, _logged_in_user() or "anonymous")
        if draft is not None:
            return draft
        session.pop(ACTIVE_DRAFT_KEY, None)
        session.pop(SESSION_KEY, None)
        session.modified = True
    value = session.get(SESSION_KEY)
    return value if isinstance(value, dict) else {
        "requirements": {}, "component_profile": new_component_profile("single-vpc")
    }


def _migrate_environment_prefix(data: dict[str, Any]) -> bool:
    """Preserve the optional trailing prefix used by drafts saved before it had its own field."""
    if _clean(data.get("environment_suffix"), limit=8):
        return False
    customer = _clean(data.get("customer"), limit=12).lower()
    environment_type = _clean(data.get("environment_type"), limit=20)
    setup_type = _clean(data.get("setup_type"), limit=40)
    setup = SETUP_TYPES.get(setup_type) or {}
    platform_prefix = _clean(setup.get("prefix"), limit=20)
    base = f"{customer}-{platform_prefix}-{environment_type}"
    environment_name = _clean(data.get("environment_name"), limit=40)
    if not all((customer, platform_prefix, environment_type)) or not environment_name.startswith(f"{base}-"):
        return False
    prefix = environment_name[len(base) + 1 :]
    if not re.fullmatch(r"[a-z0-9]{1,8}", prefix):
        return False
    data["environment_suffix"] = prefix
    return True


def _saved_drafts() -> dict[str, dict[str, Any]]:
    owner = _logged_in_user() or "anonymous"
    legacy = session.pop(DRAFTS_KEY, None)
    if isinstance(legacy, dict):
        for draft_id, draft in legacy.items():
            if isinstance(draft, dict):
                save_draft(str(draft_id), owner, draft)
        session.modified = True
    return {
        str(draft["_draft_id"]): draft
        for draft in list_drafts(owner)
        if draft.get("_draft_id")
    }


def _requested_draft_version() -> int | None:
    raw = _clean(request.form.get("draft_version"), limit=20)
    if not raw:
        session_data = session.get(SESSION_KEY)
        raw = _clean(session_data.get("_version"), limit=20) if isinstance(session_data, dict) else ""
    if not raw:
        return None
    try:
        return max(0, int(raw))
    except ValueError:
        raise DraftWriteConflict("The submitted draft version is invalid. Reload the draft.")


def _save_draft(draft_id: str, data: dict[str, Any]) -> dict[str, Any]:
    owner = _logged_in_user() or "anonymous"
    expected_version = _requested_draft_version()
    if get_draft(draft_id, owner) is not None and expected_version is None:
        raise DraftWriteConflict("The draft version is missing. Reload the draft before saving.")
    return save_draft(
        draft_id,
        owner,
        data,
        expected_version=expected_version,
    )


def _active_draft_conflict(
    data: dict[str, Any], draft_id: str, owner: str
) -> dict[str, str] | None:
    return find_active_draft_by_environment(
        _clean(data.get("environment_name"), limit=40),
        exclude_draft_id=draft_id,
        exclude_owner=owner,
    )


def _draft_conflict_message(conflict: dict[str, str]) -> str:
    environment = _clean(conflict.get("environment_name"), limit=40)
    owner = _clean(conflict.get("owner"), limit=320)
    return (
        f"An active draft already exists for {environment}, owned by {owner}. "
        "Open the existing draft instead of creating another environment record."
    )


def _current_setup_draft_conflict(
    data: dict[str, Any], draft_id: str
) -> dict[str, Any] | None:
    """Reject unrelated drafts while preserving the current source/reopen workflow."""
    existing = find_current_setup_by_environment(
        _clean(data.get("environment_name"), limit=40)
    )
    if existing is None:
        return None
    if str(existing.get("source_draft_id") or "") == draft_id:
        return None
    authorized_reopen = (
        str(existing.get("status") or "") == "completed"
        and str(data.get("_reopened_from_setup_id") or "") == str(existing.get("id") or "")
    )
    return None if authorized_reopen else existing


def _setup_draft_conflict_message(conflict: dict[str, Any]) -> str:
    environment = _clean(conflict.get("environment_name"), limit=40)
    status = {
        "ready": "Awaiting acknowledgement",
        "acknowledged": "Acknowledged",
        "started": "Setup started",
        "paused": "On hold",
        "completed": "Completed",
    }.get(str(conflict.get("status") or ""), "In progress")
    return (
        f"{environment} already has a current setup ({status}). "
        "Open the existing setup instead of creating another environment draft."
    )


def _share_serializer() -> URLSafeTimedSerializer:
    return URLSafeTimedSerializer(current_app.secret_key, salt="environment-readiness-verification")


def _shared_draft(token: str) -> tuple[str, str, int] | None:
    try:
        max_age = int(
            current_app.config.get("ENVIRONMENT_READINESS_SHARE_MAX_AGE")
            or os.getenv("ENVIRONMENT_READINESS_SHARE_MAX_AGE_SECONDS", str(7 * 24 * 60 * 60))
        )
        shared = _share_serializer().loads(token, max_age=max_age)
        draft_id = _clean(shared.get("draft"), limit=40)
        owner = _clean(shared.get("owner"), limit=320)
        step = max(5, min(6, int(shared.get("step"))))
    except (BadSignature, AttributeError, TypeError, ValueError):
        return None
    return (draft_id, owner, step) if draft_id and owner else None


def _shared_verification_payload(
    existing: dict[str, Any], submitted: dict[str, Any], step: int
) -> dict[str, Any]:
    """Apply only the requirement group authorized by a verification link."""
    group = "networking" if step == 5 else "aws_platform"
    allowed_keys = {
        key
        for requirement_group in REQUIREMENT_GROUPS
        if requirement_group["key"] == group
        for key, _label in requirement_group["items"]
    }
    allowed_keys.update(
        item["key"] for item in CONDITIONAL_REQUIREMENTS if item.get("group") == group
    )
    allowed_keys.update(item["key"] for item in all_requirements(existing) if item["group"] == group)
    merged = dict(existing)
    existing_requirements = existing.get("requirements")
    requirements = dict(existing_requirements) if isinstance(existing_requirements, dict) else {}
    submitted_requirements = submitted.get("requirements")
    submitted_requirements = submitted_requirements if isinstance(submitted_requirements, dict) else {}
    for key in allowed_keys:
        if key in submitted_requirements:
            requirements[key] = submitted_requirements[key]
    merged["requirements"] = requirements
    for suffix in ("attested", "attested_by", "attested_at", "default_owner", "default_evidence"):
        merged[f"{group}_{suffix}"] = submitted.get(f"{group}_{suffix}", "")
    merged["_step"] = step
    merged["_saved_at"] = submitted.get("_saved_at")
    return merged


def _validate_bootstrap(values: dict[str, Any]) -> tuple[list[str], list[tuple[str, str]], list[tuple[str, str]]]:
    errors: list[str] = []
    integrations: list[tuple[str, str]] = []
    spaces: list[tuple[str, str]] = []
    try:
        from engine.wizards.forms.spacelift_settings import spacelift_bootstrap_options
        integrations, spaces = spacelift_bootstrap_options()
        if str(values.get("spacelift_space_id") or "") not in {value for value, _ in spaces}:
            errors.append("Select an existing Spacelift space from the list.")
        if str(values.get("aws_integration_id") or "") not in {value for value, _ in integrations}:
            errors.append("Select an existing Spacelift AWS integration from the list.")
    except Exception:
        errors.append("Spacelift could not be reached, so the space and AWS integration could not be verified.")
    admin_reference = str(values.get("admin_stack_reference") or "").strip()
    if not re.fullmatch(r"https://[^\s]{3,990}", admin_reference, re.I):
        errors.append("Enter the HTTPS URL of the applied Spacelift admin stack.")
    return errors, integrations, spaces


def _rank_bootstrap_options(
    data: dict[str, Any], options: list[tuple[str, str]], *, kind: str
) -> tuple[list[tuple[str, str]], list[tuple[str, str]]]:
    """Put likely environment definitions first without rejecting naming exceptions."""
    normalize = lambda value: re.sub(r"[^a-z0-9]+", "-", str(value or "").lower()).strip("-")
    environment = normalize(data.get("environment_name"))
    customer = normalize(data.get("customer"))
    product = normalize(data.get("product"))
    env_type = normalize(data.get("environment_type"))
    suffix = normalize(data.get("environment_suffix"))
    classification = normalize(data.get("production_classification"))
    environment_variants = {environment}
    if product:
        environment_variants.add(environment.replace("-ctlst-", f"-{product}-"))
        environment_variants.add(environment.replace("-playon-", f"-{product}-"))

    def score(option: tuple[str, str]) -> int:
        haystack = normalize(f"{option[1]} {option[0]}")
        if kind == "space":
            value = 100 if any(candidate and candidate in haystack for candidate in environment_variants) else 0
            value += 20 if customer and customer in haystack else 0
            value += 20 if product and product in haystack else 0
            value += 15 if env_type and env_type in haystack else 0
            return value
        value = 35 if customer and customer in haystack else 0
        value += 30 if product and product in haystack else 0
        value += 25 if env_type and env_type in haystack else 0
        value += 10 if suffix and suffix in haystack else 0
        value += 8 if classification and classification in haystack else 0
        return value

    scored = [(score(option), option) for option in options]
    threshold = 50 if kind == "space" else 60
    suggested = [option for value, option in scored if value >= threshold]
    suggested.sort(key=lambda option: (-score(option), option[1].casefold()))
    suggested_values = {value for value, _label in suggested}
    remaining = [option for option in options if option[0] not in suggested_values]
    return suggested, remaining


def _validate_technical_catalog_selections(data: dict[str, Any]) -> list[str]:
    """Verify submitted capacity values against the same cached catalogs as the wizards."""
    region = _clean(data.get("aws_region"), limit=30)
    setup_type = _clean(data.get("setup_type"), limit=40)
    if region not in AWS_REGIONS or setup_type not in SETUP_TYPES:
        return []
    from engine.wizards.constants.aurora_constants import (
        AURORA_INSTANCE_CLASSES,
        AURORA_INSTANCE_CLASS_GROUPS,
        get_aurora_engine_version_fallback_choices,
        get_aurora_engine_version_fallback_groups,
    )
    from engine.wizards.constants.eks_constants import (
        EKS_CLUSTER_VERSION_GROUPS, EKS_CLUSTER_VERSIONS,
        EKS_INSTANCE_TYPE_GROUPS, EKS_INSTANCE_TYPES,
    )
    from engine.wizards.constants.redis_constants import REDIS_NODE_TYPE_GROUPS, REDIS_NODE_TYPES
    from engine.wizards.constants.vault_constants import VAULT_DB_INSTANCE_TYPES
    from services.aws_instance_types.aurora_engine_versions import get_aurora_engine_version_options
    from services.aws_instance_types.aurora_instance_classes import get_aurora_instance_class_options
    from services.aws_instance_types.eks_cluster_versions import get_eks_cluster_version_options
    from services.aws_instance_types.eks_instance_types import get_eks_instance_type_options
    from services.aws_instance_types.redis_node_types import get_redis_node_type_options

    aurora_engine = "aurora-mysql" if setup_type == "loyalty" else "aurora-postgresql"
    _g, eks_versions, _meta = get_eks_cluster_version_options(
        region=region, fallback_groups=EKS_CLUSTER_VERSION_GROUPS,
        fallback_choices=EKS_CLUSTER_VERSIONS, refresh_async_if_stale=False,
    )
    _g, eks_types, _meta = get_eks_instance_type_options(
        region=region, fallback_groups=EKS_INSTANCE_TYPE_GROUPS,
        fallback_choices=EKS_INSTANCE_TYPES, refresh_async_if_stale=False,
    )
    _g, aurora_versions, _meta = get_aurora_engine_version_options(
        region=region, engine=aurora_engine,
        fallback_groups=get_aurora_engine_version_fallback_groups(aurora_engine),
        fallback_choices=get_aurora_engine_version_fallback_choices(aurora_engine),
        refresh_async_if_stale=False,
    )
    _g, aurora_types, _meta = get_aurora_instance_class_options(
        region=region, engine=aurora_engine,
        engine_version=_clean(data.get("aurora_version"), limit=30) or None,
        fallback_groups=AURORA_INSTANCE_CLASS_GROUPS,
        fallback_choices=AURORA_INSTANCE_CLASSES, refresh_async_if_stale=False,
    )
    _g, redis_types, _meta = get_redis_node_type_options(
        region=region, fallback_groups=REDIS_NODE_TYPE_GROUPS,
        fallback_choices=REDIS_NODE_TYPES, refresh_async_if_stale=False,
    )
    catalogs: list[tuple[str, str, list[tuple[str, str]]]] = [
        ("eks_version", "EKS version", eks_versions),
        ("eks_instance_types", "EKS instance type", eks_types),
        ("aurora_version", "Aurora engine version", aurora_versions),
        ("aurora_instance_type", "Aurora instance type", aurora_types),
        ("redis_instance_type", "Redis instance type", redis_types),
    ]
    if setup_type.startswith("catalyst-"):
        _g, vault_versions, _meta = get_aurora_engine_version_options(
            region=region, engine="postgres", refresh_async_if_stale=False,
        )
        _g, vault_types, _meta = get_aurora_instance_class_options(
            region=region, engine="postgres",
            engine_version=_clean(data.get("vault_database_version"), limit=30) or None,
            fallback_choices=VAULT_DB_INSTANCE_TYPES, refresh_async_if_stale=False,
        )
        catalogs.extend((
            ("vault_database_version", "Vault database engine version", vault_versions),
            ("vault_database_instance_type", "Vault database instance type", vault_types),
        ))
    errors: list[str] = []
    for key, label, options in catalogs:
        value = _clean(data.get(key), limit=100)
        allowed = {option for option, _display in options}
        if value and allowed and value not in allowed:
            errors.append(f"{label} is not available in the selected AWS region catalog.")
    errors.extend(validate_camunda(data, aws=True))
    return errors


def _delete_draft(draft_id: str) -> None:
    delete_draft(draft_id, _logged_in_user() or "anonymous")


def _errors_by_step(errors: list[str]) -> dict[int, list[str]]:
    grouped: dict[int, list[str]] = {step: [] for step in range(1, 8)}
    networking_labels = {
        label for group in REQUIREMENT_GROUPS if group["key"] == "networking" for _key, label in group["items"]
    }
    aws_labels = {
        label for group in REQUIREMENT_GROUPS if group["key"] == "aws_platform" for _key, label in group["items"]
    }
    networking_conditional_labels = {
        item["label"] for item in CONDITIONAL_REQUIREMENTS if item.get("group") == "networking"
    }
    aws_conditional_labels = {
        item["label"] for item in CONDITIONAL_REQUIREMENTS if item.get("group") != "networking"
    }
    technical_labels = {label for _key, label in (*TECHNICAL_FIELDS, *CATALYST_TECHNICAL_FIELDS, *CAMUNDA_FIELDS)}
    service_request_labels = {label for _key, label in SERVICE_REQUEST_FIELDS}
    allocated_labels = {label for _key, label in ALLOCATED_FIELDS}
    signoff_prefixes = ("Risks ", "Assumptions ", "Testing/sign-off owner ")
    for error in errors:
        if error.startswith("Networking attestation") or any(label in error for label in networking_labels | networking_conditional_labels):
            step = 5
        elif error.startswith("AWS and Platform attestation") or any(label in error for label in aws_labels | aws_conditional_labels):
            step = 6
        elif error.startswith("Answer whether"):
            step = 2
        elif any(error.startswith(label) for label in service_request_labels) or error.startswith("NCR ticket"):
            step = 3
        elif any(error.startswith(label) for label in allocated_labels) or error.startswith(("AWS account ID", "VPC CIDR")):
            step = 4
        elif any(error.startswith(label) for label in technical_labels) or error.startswith(
            ("Base DNS domain", "Release archive")
        ):
            step = 2
        elif error.startswith(signoff_prefixes):
            step = 7
        else:
            step = 1
        grouped[step].append(error)
    return {step: messages for step, messages in grouped.items() if messages}


@bp.get("/release-options")
def release_options():
    customer_code = _clean(request.args.get("customer"), limit=20)
    setup_type = _clean(request.args.get("setup_type"), limit=40)
    if not customer_code or setup_type not in SETUP_TYPES:
        return jsonify({"releases": [], "error": "Select Customer and Environment Setup first."}), 400

    wizard_slug = {
        "catalyst-single-vpc": "single-vpc",
        "catalyst-multi-vpc": "multi-vpc",
        "rgs": "rgs",
        "loyalty": "loyalty",
    }.get(setup_type, "")
    try:
        from modules.praxisrelease.service import (
            BUCKET,
            filter_release_names_for_wizard,
            list_customer_releases,
            list_customers,
        )

        customers = list_customers()
        matched_customer = next(
            (candidate for candidate in customers if candidate.casefold() == customer_code.casefold()),
            "",
        )
        if not matched_customer:
            return jsonify(
                {"releases": [], "error": f"No release folder was found for customer {customer_code}."}
            )
        releases = list_customer_releases(BUCKET, matched_customer)
        if wizard_slug:
            releases = filter_release_names_for_wizard(releases, wizard_slug)
        return jsonify({"releases": releases, "customer": matched_customer})
    except Exception:
        logger.warning("Readiness release catalog lookup failed", exc_info=True)
        return jsonify({"releases": [], "error": "Release catalog unavailable. Try again or check the Releases module."}), 503


@bp.get("/technical-options")
def technical_options():
    region = _clean(request.args.get("region"), limit=30)
    setup_type = _clean(request.args.get("setup_type"), limit=40)
    if region not in AWS_REGIONS:
        return jsonify({"error": "Select a supported AWS region first."}), 400

    from engine.wizards.constants.aurora_constants import (
        AURORA_INSTANCE_CLASSES,
        AURORA_INSTANCE_CLASS_GROUPS,
        get_aurora_engine_version_fallback_choices,
        get_aurora_engine_version_fallback_groups,
    )
    from engine.wizards.constants.eks_constants import (
        EKS_CLUSTER_VERSION_GROUPS, EKS_CLUSTER_VERSIONS, EKS_INSTANCE_TYPE_GROUPS, EKS_INSTANCE_TYPES,
    )
    from engine.wizards.constants.redis_constants import REDIS_NODE_TYPE_GROUPS, REDIS_NODE_TYPES
    from engine.wizards.constants.vault_constants import VAULT_DB_INSTANCE_TYPES
    from services.aws_instance_types.aurora_engine_versions import get_aurora_engine_version_options
    from services.aws_instance_types.aurora_instance_classes import get_aurora_instance_class_options
    from services.aws_instance_types.eks_cluster_versions import get_eks_cluster_version_options
    from services.aws_instance_types.eks_instance_types import get_eks_instance_type_options
    from services.aws_instance_types.redis_node_types import get_redis_node_type_options

    aurora_engine = "aurora-mysql" if setup_type == "loyalty" else "aurora-postgresql"
    aurora_version = _clean(request.args.get("aurora_version"), limit=30)
    vault_version = _clean(request.args.get("vault_version"), limit=30)
    _g, eks_versions, eks_meta = get_eks_cluster_version_options(
        region=region, fallback_groups=EKS_CLUSTER_VERSION_GROUPS, fallback_choices=EKS_CLUSTER_VERSIONS, refresh_async_if_stale=False,
    )
    _g, eks_types, eks_type_meta = get_eks_instance_type_options(
        region=region, fallback_groups=EKS_INSTANCE_TYPE_GROUPS, fallback_choices=EKS_INSTANCE_TYPES, refresh_async_if_stale=False,
    )
    _g, aurora_versions, aurora_meta = get_aurora_engine_version_options(
        region=region,
        engine=aurora_engine,
        fallback_groups=get_aurora_engine_version_fallback_groups(aurora_engine),
        fallback_choices=get_aurora_engine_version_fallback_choices(aurora_engine),
        refresh_async_if_stale=False,
    )
    effective_aurora_version = aurora_version or (aurora_versions[0][0] if aurora_versions else "")
    _g, aurora_types, aurora_type_meta = get_aurora_instance_class_options(
        region=region, engine=aurora_engine, engine_version=effective_aurora_version or None,
        fallback_groups=AURORA_INSTANCE_CLASS_GROUPS, fallback_choices=AURORA_INSTANCE_CLASSES, refresh_async_if_stale=False,
    )
    _g, redis_types, redis_meta = get_redis_node_type_options(
        region=region, fallback_groups=REDIS_NODE_TYPE_GROUPS, fallback_choices=REDIS_NODE_TYPES, refresh_async_if_stale=False,
    )
    vault_versions: list[tuple[str, str]] = []
    vault_types: list[tuple[str, str]] = []
    vault_meta = None
    vault_type_meta = None
    if setup_type.startswith("catalyst-"):
        _g, vault_versions, vault_meta = get_aurora_engine_version_options(region=region, engine="postgres", refresh_async_if_stale=False)
        if not vault_versions:
            # Vault uses standard RDS PostgreSQL, which has a separate cache from
            # the Aurora PostgreSQL catalog. Populate it on demand so a newly
            # selected region never leaves the required version field empty.
            from services.aws_instance_types.aurora_engine_versions import refresh_aurora_engine_version_cache

            refresh_aurora_engine_version_cache(region, engine="postgres")
            _g, vault_versions, vault_meta = get_aurora_engine_version_options(
                region=region,
                engine="postgres",
                refresh_async_if_stale=False,
            )
        effective_vault_version = vault_version or (vault_versions[0][0] if vault_versions else "")
        _g, vault_types, vault_type_meta = get_aurora_instance_class_options(
            region=region, engine="postgres", engine_version=effective_vault_version or None,
            fallback_choices=VAULT_DB_INSTANCE_TYPES, refresh_async_if_stale=False,
        )

    def pack(options, meta):
        return {
            "options": [{"value": value, "label": label} for value, label in options],
            "source": getattr(meta, "source", "unavailable") if meta else "unavailable",
            "stale": bool(getattr(meta, "stale", False)) if meta else True,
        }

    return jsonify({
        **(camunda_catalogs(region, _clean(request.args.get("opensearch_version"), limit=30)) if setup_type.startswith("catalyst-") else {}),
        "region": region,
        "aurora_engine": aurora_engine,
        "eks_versions": pack(eks_versions, eks_meta),
        "eks_instance_types": pack(eks_types, eks_type_meta),
        "aurora_versions": pack(aurora_versions, aurora_meta),
        "aurora_instance_types": pack(aurora_types, aurora_type_meta),
        "redis_instance_types": pack(redis_types, redis_meta),
        "vault_versions": pack(vault_versions, vault_meta),
        "vault_instance_types": pack(vault_types, vault_type_meta),
    })


@bp.post("/autosave")
def autosave():
    share = _shared_draft(_clean(request.form.get("share_token"), limit=2000))
    shared_existing = get_draft(share[0], share[1]) if share else None
    if share and shared_existing is None:
        return jsonify({"ok": False, "error": "This verification link no longer references an active draft."}), 410
    data = _payload_from_form(shared_existing)
    review_kind = _clean(request.form.get("acknowledge_request_review"), limit=20)
    if review_kind:
        if share:
            return jsonify({"ok": False, "error": "Request review belongs to the draft owner, not a shared verification link."}), 403
        templates = deepcopy(data.get("servicenow_templates") or {})
        template = templates.get(review_kind)
        if review_kind not in {"aws", "ncr"} or not isinstance(template, dict) or not template.get("needs_review"):
            return jsonify({"ok": False, "error": "This request has no pending review. Reload the draft."}), 400
        template["review_acknowledged_by"] = _logged_in_user() or "anonymous"
        template["review_acknowledged_at"] = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
        data["servicenow_templates"] = templates

    try:
        next_step = max(1, min(7, int(request.form.get("wizard_step", "1"))))
    except (TypeError, ValueError):
        next_step = 1
    data["_step"] = next_step
    data["_saved_at"] = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    draft_id = (
        _clean(request.form.get("draft_id"), limit=40)
        or _clean(session.get(ACTIVE_DRAFT_KEY), limit=40)
        or uuid4().hex[:12]
    )
    data["_draft_id"] = draft_id
    if find_completed_setup_by_source_draft(draft_id) is not None:
        return jsonify({
            "ok": False,
            "error": "This completed setup source draft is read-only. Reopen the setup to make changes.",
        }), 409
    save_owner = share[1] if share and share[0] == draft_id else (_logged_in_user() or "anonymous")
    if share:
        if share[0] != draft_id:
            return jsonify({"ok": False, "error": "The verification link does not match this draft."}), 403
        data = _shared_verification_payload(shared_existing or {}, data, share[2])
    conflict = _active_draft_conflict(data, draft_id, save_owner)
    if conflict:
        conflict_owner = _clean(conflict.get("owner"), limit=320)
        conflict_url = (
            url_for("environment_readiness.workspace", draft=conflict.get("id"))
            if conflict_owner == save_owner
            else url_for(
                "environment_readiness.drafts", scope="team",
                q=conflict.get("environment_name"),
            )
        )
        return jsonify({
            "ok": False,
            "error": _draft_conflict_message(conflict),
            "conflict": conflict,
            "conflict_url": conflict_url,
        }), 409
    setup_conflict = _current_setup_draft_conflict(data, draft_id)
    if setup_conflict:
        return jsonify({
            "ok": False,
            "error": _setup_draft_conflict_message(setup_conflict),
            "conflict": {
                "id": setup_conflict.get("id"),
                "environment_name": setup_conflict.get("environment_name"),
                "status": setup_conflict.get("status"),
            },
            "conflict_url": url_for(
                "environment_readiness.setup_detail", setup_id=setup_conflict["id"]
            ),
            "conflict_link_label": "Open existing setup",
        }), 409
    try:
        expected_version = _requested_draft_version()
        if get_draft(draft_id, save_owner) is not None and expected_version is None:
            raise DraftWriteConflict("The draft version is missing. Reload the draft before saving.")
        data = save_draft(
            draft_id,
            save_owner,
            data,
            expected_version=expected_version,
        )
    except DraftWriteConflict as exc:
        return jsonify({"ok": False, "error": str(exc), "reload": True}), 409
    session[ACTIVE_DRAFT_KEY] = draft_id
    session[SESSION_KEY] = data
    session.permanent = True
    session.modified = True
    return jsonify({
        "ok": True,
        "draft_id": draft_id,
        "saved_at": data["_saved_at"],
        "step": share[2] if share else next_step,
        "version": data["_version"],
        "verification_audit": {key: {field: row.get(field, "") for field in ("verified_by", "verified_at")} for key, row in data.get("requirements", {}).items()},
        "summary_html": render_template("environment_readiness/_readiness_summary.html", data=data, readiness_summary=readiness_summary(data), share_token=request.form.get("share_token", "")),
        "verification": {
            **{f"{group}_attested": data.get(f"{group}_attested", "") for group in VERIFICATION_INPUTS},
            **{f"req_{key}_status": row.get("status", "") for key, row in data.get("requirements", {}).items()},
        },
    })


@bp.post("/verification-link")
def verification_link():
    draft_id = _clean(request.form.get("draft_id"), limit=40)
    try:
        step = int(request.form.get("step", ""))
    except ValueError:
        step = 0
    owner = _logged_in_user() or "anonymous"
    if step not in {5, 6} or not draft_id or get_draft(draft_id, owner) is None:
        abort(400)
    if find_completed_setup_by_source_draft(draft_id) is not None:
        return jsonify({
            "ok": False,
            "error": "Completed setup source drafts cannot issue verification links.",
        }), 409
    token = _share_serializer().dumps({"draft": draft_id, "owner": owner, "step": step})
    return jsonify({"ok": True, "url": url_for("environment_readiness.workspace", share=token, _external=True)})


@bp.route("/", methods=["GET", "POST"])
def index():
    if request.method == "POST":
        return workspace()
    return redirect(url_for("environment_readiness.published_setups"))


@bp.get("/information")
def information():
    return render_template("environment_readiness/landing.html", page_mode="information")


@bp.route("/drafts", methods=["GET", "POST"])
def drafts():
    if request.method == "POST":
        return workspace()
    _saved_drafts()  # Import any legacy session-backed drafts before building the team view.
    current_user = _logged_in_user() or "anonymous"
    all_drafts = {
        str(draft["_draft_id"]): draft
        for draft in list_all_drafts()
        if draft.get("_draft_id")
    }
    setup_by_draft: dict[str, dict[str, Any]] = {}
    for setup in list_setups(current_only=False):
        source_draft_id = str(setup.get("source_draft_id") or "")
        if source_draft_id:
            setup_by_draft.setdefault(source_draft_id, setup)
    for draft_id, draft in all_drafts.items():
        setup = setup_by_draft.get(draft_id)
        if setup:
            draft["_published_setup_id"] = setup.get("id", "")
            draft["_published_setup_status"] = setup.get("status", "ready")
        draft["_bucket"] = (
            "active" if not setup
            else "completed" if setup.get("status") == "completed"
            else "published"
        )
        draft["_is_mine"] = draft.get("_owner") == current_user

    active_name_groups: dict[str, list[dict[str, Any]]] = {}
    for draft in all_drafts.values():
        name = _clean(draft.get("environment_name"), limit=40).casefold()
        if name and draft.get("_bucket") == "active":
            active_name_groups.setdefault(name, []).append(draft)
    duplicate_groups = [group for group in active_name_groups.values() if len(group) > 1]
    for group in duplicate_groups:
        for draft in group:
            draft["_duplicate_count"] = len(group)

    customer_filter = _clean(request.args.get("customer"), limit=40)
    environment_type_filter = _clean(request.args.get("environment_type"), limit=20)
    search_query = _clean(request.args.get("q"), limit=200)
    view_filter = _clean(request.args.get("view"), limit=20) or "active"
    if view_filter not in {"active", "published", "completed", "all"}:
        view_filter = "active"
    scope_filter = _clean(request.args.get("scope"), limit=20) or "mine"
    if scope_filter not in {"mine", "team"}:
        scope_filter = "mine"
    draft_customers = sorted(
        {
            _clean(draft.get("customer"), limit=40)
            for draft in all_drafts.values()
            if _clean(draft.get("customer"), limit=40)
        },
        key=str.casefold,
    )
    draft_environment_types = sorted(
        {
            _clean(draft.get("environment_type"), limit=20)
            for draft in all_drafts.values()
            if _clean(draft.get("environment_type"), limit=20)
        },
        key=str.casefold,
    )
    base_drafts = []
    for draft_id, draft in all_drafts.items():
        haystack = " ".join(str(draft.get(key) or "") for key in (
            "environment_name", "title", "_owner", "setup_type", "customer", "environment_type"
        )).casefold()
        if scope_filter == "mine" and not draft.get("_is_mine"):
            continue
        if customer_filter and _clean(draft.get("customer"), limit=40).casefold() != customer_filter.casefold():
            continue
        if environment_type_filter and _clean(draft.get("environment_type"), limit=20).casefold() != environment_type_filter.casefold():
            continue
        if search_query and search_query.casefold() not in haystack:
            continue
        base_drafts.append((draft_id, draft))
    draft_view_counts = {
        key: sum(1 for _draft_id, draft in base_drafts if key == "all" or draft.get("_bucket") == key)
        for key in ("active", "published", "completed", "all")
    }
    base_draft_ids = {draft_id for draft_id, _draft in base_drafts}
    visible_duplicate_groups = [
        group for group in duplicate_groups
        if any(str(draft.get("_draft_id")) in base_draft_ids for draft in group)
    ]
    visible_drafts = [
        (draft_id, draft) for draft_id, draft in base_drafts
        if view_filter == "all" or draft.get("_bucket") == view_filter
    ]
    saved_drafts = sorted(
        visible_drafts,
        key=lambda item: str(item[1].get("_saved_at") or ""),
        reverse=True,
    )
    return render_template(
        "environment_readiness/landing.html",
        page_mode="drafts",
        saved_drafts=saved_drafts,
        draft_customers=draft_customers,
        draft_customer_filter=customer_filter,
        draft_environment_types=draft_environment_types,
        draft_environment_type_filter=environment_type_filter,
        draft_search_query=search_query,
        draft_view_filter=view_filter,
        draft_scope_filter=scope_filter,
        draft_view_counts=draft_view_counts,
        duplicate_groups=visible_duplicate_groups,
        current_user=current_user,
        has_any_drafts=bool(all_drafts),
        active_draft_id=_clean(session.get(ACTIVE_DRAFT_KEY), limit=40),
    )


@bp.get("/setups")
def published_setups():
    status_filter = _clean(request.args.get("status"), limit=20) or "active"
    customer_filter = _clean(request.args.get("customer"), limit=40)
    search_query = _clean(request.args.get("q"), limit=200)
    mine_filter = _clean(request.args.get("mine"), limit=20) or "all"
    urgency_filter = _clean(request.args.get("urgency"), limit=30)
    sort_by = _clean(request.args.get("sort"), limit=30)
    sort_direction = _clean(request.args.get("direction"), limit=4) or "asc"
    try:
        page = max(1, int(request.args.get("page", "1")))
    except (TypeError, ValueError):
        page = 1
    dashboard = setup_dashboard(
        status_filter=status_filter,
        customer=customer_filter,
        query=search_query,
        mine_filter=mine_filter,
        current_user=_logged_in_user(),
        urgency_filter=urgency_filter,
        sort_by=sort_by,
        sort_direction=sort_direction,
        page=page,
    )
    return render_template(
        "environment_readiness/landing.html",
        page_mode="published",
        published_setups=dashboard["setups"],
        setup_dashboard=dashboard,
    )


@bp.get("/insights")
def insights():
    period = _clean(request.args.get("period"), limit=8) or "90"
    customer = _clean(request.args.get("customer"), limit=40)
    setup_type = _clean(request.args.get("setup_type"), limit=50)
    environment_type = _clean(request.args.get("environment_type"), limit=30)
    return render_template(
        "environment_readiness/insights.html",
        insights=setup_insights(
            period=period, customer=customer, setup_type=setup_type,
            environment_type=environment_type,
        ),
    )


@bp.get("/insights.csv")
def export_insights_csv():
    period = _clean(request.args.get("period"), limit=8) or "90"
    customer = _clean(request.args.get("customer"), limit=40)
    setup_type = _clean(request.args.get("setup_type"), limit=50)
    environment_type = _clean(request.args.get("environment_type"), limit=30)
    report = setup_insights(
        period=period, customer=customer, setup_type=setup_type,
        environment_type=environment_type,
    )
    output = io.StringIO()
    writer = csv.writer(output)

    def cell(value: Any) -> Any:
        if isinstance(value, (int, float)):
            return round(float(value), 2)
        text_value = str(value or "")
        return f"'{text_value}" if text_value.startswith(("=", "+", "-", "@")) else text_value

    allowed_sections = {
        "overview", "attention", "workload", "complexity", "forecast", "prerequisites",
        "revision_quality", "trends", "customers", "data_quality", "delivery_timing",
        "owner_contribution", "longest_setups",
    }
    customized = "sections" in request.args
    selected_sections = [
        section for section in _clean(request.args.get("sections"), limit=1000).split(",")
        if section in allowed_sections
    ] if customized else []

    if customized:
        writer.writerow(["Section", "Item", "Scope", "Metric", "Value", "Detail", "Record ID"])

        def insight_row(section: str, item: Any, scope: Any, metric: Any, value: Any, detail: Any = "", record_id: Any = "") -> None:
            writer.writerow(cell(value) for value in (section, item, scope, metric, value, detail, record_id))

        if "overview" in selected_sections:
            for metric, value in (
                ("Setups in window", report["published"]), ("Completed", report["completed"]),
                ("Active", report["active"]), ("On hold", report["on_hold"]),
                ("Completion rate", f"{report['completion_rate']}%"),
            ):
                insight_row("Overview", "Selected scope", customer or "All customers", metric, value)
        if "attention" in selected_sections:
            insight_row(
                "Attention needed", "Selected scope", customer or "All customers",
                "Total attention signals", report["attention_total"],
            )
            for category, items in report["attention"].items():
                for item in items:
                    insight_row("Attention needed", item["environment"], item["customer"], item["reason"], item.get("age_days"), category.replace("_", " ").title(), item["id"])
        if "workload" in selected_sections:
            for owner in report["workload"]["owners"]:
                insight_row("Workload and capacity", owner["owner"], "Receiving DevOps owner", "Active / queue / incoming", owner["active"], f"Queue {owner['queue']}; incoming {owner['incoming']}; completed 30d {owner['completed_30_days']}; {owner['signal_label']}")
            insight_row("Workload and capacity", "Queue flow", "Last 30 days", "Published vs started", report["workload"]["queue_delta"], f"{report['workload']['published_30_days']} published; {report['workload']['started_30_days']} started; queue {report['workload']['queue_direction']}")
        if "complexity" in selected_sections:
            for profile in report["complexity_profiles"]:
                insight_row("Complexity-adjusted delivery", profile["label"], "Selected scope", "Setups / median execution", profile["setups"], f"Completed {profile['completed']}; execution {profile['median_execution']}; preparation {profile['median_preparation']}; on time {profile['on_time_rate']}%")
        if "forecast" in selected_sections:
            insight_row(
                "Delivery forecast", "Selected scope", customer or "All customers",
                "Active setups forecast", len(report["forecasts"]),
            )
            for forecast in report["forecasts"]:
                insight_row("Delivery forecast", forecast["environment"], forecast["customer"], "Estimated completion", forecast["estimated_completion"] or "Not enough data", f"{forecast['confidence']} confidence; {forecast['sample']} comparable; execution {forecast['execution']['range']}", forecast["id"])
        if "prerequisites" in selected_sections:
            for key, label in (("networking", "Networking readiness"), ("aws_platform", "AWS & Platform readiness"), ("finalization", "Handoff finalization")):
                metric = report["prerequisites"][key]
                insight_row("Prerequisite bottlenecks", label, "Selected scope", "Median duration", metric["label"], f"{metric['sample']} measured setups")
            for blocker, count in report["prerequisites"]["blocker_counts"].items():
                insight_row("Prerequisite bottlenecks", blocker, "Selected scope", "Final blocker count", count)
        if "revision_quality" in selected_sections:
            for metric, value in (
                ("Revision rate", f"{report['quality']['revision_rate']}%"),
                ("Average revisions", report["quality"]["average_revisions"]),
                ("Target change rate", f"{report['quality']['target_change_rate']}%"),
                ("On original target", f"{report['quality']['original_on_time_rate']}%"),
                ("On current target", f"{report['quality']['current_on_time_rate']}%"),
            ):
                insight_row("Revision reliability", "Selected scope", customer or "All customers", metric, value)
        if "trends" in selected_sections:
            for bucket in report["trend"]:
                insight_row("Delivery trends", bucket["label"], "Reporting interval", "Completed / execution / preparation", bucket["completed"], f"Execution {bucket['execution_label']}; preparation {bucket['preparation_label']}; on time {bucket['on_time_rate']}%; revised {bucket['revision_rate']}%; held {bucket['hold_rate']}%")
        if "customers" in selected_sections:
            for profile in report["customer_profiles"]:
                insight_row("Customer profiles", profile["customer"], "Customer", "Setups / completed", profile["setups"], f"Completed {profile['completed']}; active {profile['active']}; execution {profile['median_execution']}; on time {profile['on_time_rate']}%; {profile['sample_label']}")
        if "data_quality" in selected_sections:
            insight_row(
                "Data quality", "Selected scope", customer or "All customers",
                "Issues", report["data_quality"]["issue_count"],
                f"{report['data_quality']['errors']} errors; {report['data_quality']['warnings']} warnings; {report['data_quality']['records_checked']} records checked",
            )
            for issue in report["data_quality"]["issues"]:
                insight_row("Data quality", issue["environment"], issue["customer"], issue["label"], issue["severity"].title(), issue["detail"], issue["id"])
        if "delivery_timing" in selected_sections:
            for key, label in (("preparation", "Draft preparation"), ("queue", "DevOps queue"), ("execution", "Praxis execution"), ("active_execution", "Active execution"), ("end_to_end", "End-to-end")):
                metric = report["metrics"][key]
                insight_row("Delivery timing", label, "Selected scope", "Median duration", metric["label"], f"{metric['sample']} measured setups")
        if "owner_contribution" in selected_sections:
            for owner in report["owners"]:
                insight_row("Receiving DevOps contribution", owner["owner"], "Receiving DevOps owner", "Completed", owner["completed"], f"Median execution {owner['median_execution']}; active {owner['median_active']}; on time {owner['on_time_percent']}%")
        if "longest_setups" in selected_sections:
            for setup in report["longest"]:
                insight_row("Longest setups", setup["environment_name"], setup["customer"], "Praxis execution", setup["execution_label"], f"Owner {setup.get('devops_owner') or 'Unassigned'}; preparation {setup['preparation_label']}; queue {setup['queue_label']}", setup["id"])
    else:
        writer.writerow([
            "Environment", "Customer", "Setup type", "Environment type", "Complexity profile",
            "Complexity points", "Complexity factors", "Receiving DevOps owner",
            "Setup owner", "Requester", "Published by", "Completed by", "Status", "Target date",
            "Original target date", "Published revisions", "Recorded target changes",
            "Draft created", "Published", "Acknowledged", "Started", "Completed",
            "Networking ready", "AWS and Platform ready", "Final prerequisite blocker",
            "Prerequisite preparation hours", "DevOps queue hours", "Praxis execution hours",
            "Networking readiness hours", "AWS and Platform readiness hours", "Finalization hours",
            "Recorded hold hours", "Active execution hours", "End-to-end hours",
        ])

    for record in report["records"] if not customized else []:
        payload = record.get("payload") if isinstance(record.get("payload"), dict) else {}
        writer.writerow(cell(value) for value in (
            record.get("environment_name"), record.get("customer"), record.get("setup_type"),
            payload.get("environment_type"),
            record.get("complexity", {}).get("label"), record.get("complexity", {}).get("points"),
            "; ".join(record.get("complexity", {}).get("factors", [])),
            record.get("devops_owner"), record.get("setup_owner"), record.get("requester"), record.get("published_by"),
            record.get("completed_by"), record.get("status"), record.get("target_date"),
            record.get("original_target_date"), record.get("revision_count"),
            record.get("target_change_count"),
            record.get("draft_created_at") or payload.get("_created_at"), record.get("published_at"),
            record.get("acknowledged_at"), record.get("started_at"), record.get("completed_at"),
            payload.get("networking_attested_at"), payload.get("aws_platform_attested_at"),
            record.get("final_blocker"),
            record.get("preparation_hours"), record.get("queue_hours"), record.get("execution_hours"),
            record.get("networking_ready_hours"), record.get("aws_platform_ready_hours"),
            record.get("finalization_hours"),
            record.get("paused_hours"), record.get("active_execution_hours"), record.get("end_to_end_hours"),
        ))
    filter_slugs = [
        re.sub(r"[^a-z0-9]+", "-", value.lower()).strip("-")
        for value in (customer, setup_type, environment_type)
        if value
    ]
    suffix = f"-{'-'.join(filter_slugs)}" if filter_slugs else ""
    period_label = "all-time" if period == "all" else f"{period}-days"
    filename = f"foundation-insights-{period_label}{suffix}-{datetime.now(timezone.utc).strftime('%Y%m%d')}.csv"
    return Response(
        "\ufeff" + output.getvalue(),
        mimetype="text/csv; charset=utf-8",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


@bp.get("/export.csv")
def export_csv():
    status_filter = _clean(request.args.get("status"), limit=20) or "active"
    customer_filter = _clean(request.args.get("customer"), limit=40)
    search_query = _clean(request.args.get("q"), limit=200)
    mine_filter = _clean(request.args.get("mine"), limit=20) or "all"
    urgency_filter = _clean(request.args.get("urgency"), limit=30)
    sort_by = _clean(request.args.get("sort"), limit=30)
    sort_direction = _clean(request.args.get("direction"), limit=4) or "asc"
    rows = export_setup_rows(
        status_filter=status_filter,
        customer=customer_filter,
        query=search_query,
        mine_filter=mine_filter,
        current_user=_logged_in_user(),
        urgency_filter=urgency_filter,
        sort_by=sort_by,
        sort_direction=sort_direction,
    )

    def safe_cell(value: Any) -> str:
        text = str(value or "")
        return f"'{text}" if text.startswith(("=", "+", "-", "@")) else text

    output = io.StringIO(newline="")
    writer = csv.writer(output)
    writer.writerow(
        (
            "Customer", "Environment", "Setup type", "AWS account ID", "Setup owner", "Receiving DevOps owner",
            "Original target date", "Current target date", "Status", "Published at", "Published by",
            "Acknowledged at", "Acknowledged by", "Started at", "Started by", "Completed at",
            "Days active", "Latest hold reason", "Current revision",
        )
    )
    status_labels = {
        "ready": "Awaiting acknowledgement",
        "acknowledged": "Acknowledged",
        "started": "Setup started",
        "paused": "On hold",
        "completed": "Completed",
    }
    for row in rows:
        writer.writerow(
            safe_cell(value)
            for value in (
                row.get("customer"), row.get("environment_name"),
                str(row.get("setup_type") or "").replace("-", " ").title(), row.get("aws_account_id"),
                row.get("setup_owner"), row.get("devops_owner"), row.get("original_target_date"), row.get("target_date"),
                status_labels.get(str(row.get("status")), row.get("status")), row.get("published_at"),
                row.get("published_by"), row.get("acknowledged_at"), row.get("acknowledged_by"),
                row.get("started_at"), row.get("started_by"), row.get("completed_at"),
                row.get("days_active"), row.get("hold_reason"), row.get("revision"),
            )
        )
    filename = f"foundation-setups-{datetime.now(timezone.utc).strftime('%Y%m%d')}.csv"
    return Response(
        "\ufeff" + output.getvalue(),
        mimetype="text/csv; charset=utf-8",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


@bp.get("/why")
def origins():
    return render_template(
        "environment_readiness/origins.html",
        origin_groups=ORIGIN_GROUPS,
        source_documents=SOURCE_DOCUMENTS,
    )


@bp.get("/setup/new")
def new_workspace():
    """Start a blank draft without deleting any previously saved draft."""
    session.pop(SESSION_KEY, None)
    session.pop(ACTIVE_DRAFT_KEY, None)
    session.modified = True
    return redirect(url_for("environment_readiness.workspace"))


@bp.route("/setup", methods=["GET", "POST"])
def workspace():
    requested_draft_id = _clean(request.args.get("draft"), limit=40)
    share_token = _clean(request.args.get("share"), limit=2000)
    shared = _shared_draft(share_token) if share_token else None
    shared_data = None
    if request.method == "GET" and shared:
        completed_setup = find_completed_setup_by_source_draft(shared[0])
        if completed_setup is not None:
            flash("This completed setup is read-only. Reopen it to create a new revision draft.", "info")
            return redirect(
                url_for("environment_readiness.setup_detail", setup_id=completed_setup["id"])
            )
        requested_draft = get_draft(shared[0], shared[1])
        if requested_draft is not None:
            requested_draft["_step"] = shared[2]
            shared_data = requested_draft
            session[SESSION_KEY] = requested_draft
            session[ACTIVE_DRAFT_KEY] = shared[0]
            session.modified = True
    if request.method == "GET" and requested_draft_id:
        completed_setup = find_completed_setup_by_source_draft(requested_draft_id)
        if completed_setup is not None:
            flash("This completed setup is read-only. Reopen it to create a new revision draft.", "info")
            return redirect(
                url_for("environment_readiness.setup_detail", setup_id=completed_setup["id"])
            )
        requested_draft = _saved_drafts().get(requested_draft_id)
        if requested_draft is not None:
            session[SESSION_KEY] = requested_draft
            session[ACTIVE_DRAFT_KEY] = requested_draft_id
            session.modified = True
    data = shared_data or _saved_data()
    if _prefill_servicenow_evidence(data):
        session[SESSION_KEY] = data
        session.modified = True
    if _migrate_environment_prefix(data):
        session[SESSION_KEY] = data
        session.modified = True
    if not _clean(data.get("requester")) and _logged_in_user():
        data["requester"] = _logged_in_user()
    active_draft_id = _clean(session.get(ACTIVE_DRAFT_KEY), limit=40)
    errors: list[str] = []
    notice = ""
    notice_is_error = False
    try:
        current_step = max(1, min(7, int(str(data.get("_step") or "1"))))
    except (TypeError, ValueError):
        current_step = 1
    if request.method == "POST":
        action = _clean(request.form.get("_action"), limit=30)
        if action == "new":
            session.pop(SESSION_KEY, None)
            session.pop(ACTIVE_DRAFT_KEY, None)
            session.modified = True
            return redirect(url_for("environment_readiness.workspace"))
        submitted_draft_id = _clean(request.form.get("draft_id"), limit=40) or active_draft_id
        completed_setup = find_completed_setup_by_source_draft(submitted_draft_id)
        if completed_setup is not None:
            session.pop(SESSION_KEY, None)
            session.pop(ACTIVE_DRAFT_KEY, None)
            session.modified = True
            flash("This completed setup is read-only. Reopen it to create a new revision draft.", "info")
            return redirect(
                url_for("environment_readiness.setup_detail", setup_id=completed_setup["id"])
            )
        if action == "delete":
            delete_id = _clean(request.form.get("draft_id"), limit=40)
            _delete_draft(delete_id)
            if active_draft_id == delete_id:
                session.pop(SESSION_KEY, None)
                session.pop(ACTIVE_DRAFT_KEY, None)
            session.modified = True
            return redirect(url_for("environment_readiness.drafts"))
        if action == "reset":
            if active_draft_id:
                _delete_draft(active_draft_id)
            session.pop(SESSION_KEY, None)
            session.pop(ACTIVE_DRAFT_KEY, None)
            session.modified = True
            return redirect(url_for("environment_readiness.drafts"))
        data = _payload_from_form()
        try:
            current_step = max(1, min(7, int(request.form.get("wizard_step", "1"))))
        except (TypeError, ValueError):
            current_step = 1
        data["_step"] = current_step
        session[SESSION_KEY] = data
        draft_owner = _logged_in_user() or "anonymous"
        candidate_draft_id = (
            _clean(request.form.get("draft_id"), limit=40)
            or active_draft_id
            or uuid4().hex[:12]
        )
        duplicate_conflict = _active_draft_conflict(data, candidate_draft_id, draft_owner)
        if duplicate_conflict:
            errors = [_draft_conflict_message(duplicate_conflict)]
            notice = errors[0]
            notice_is_error = True
            current_step = 1
            data["_step"] = current_step
            action = "duplicate_conflict"
        setup_conflict = _current_setup_draft_conflict(data, candidate_draft_id)
        if setup_conflict:
            errors = [_setup_draft_conflict_message(setup_conflict)]
            notice = errors[0]
            notice_is_error = True
            current_step = 1
            data["_step"] = current_step
            action = "duplicate_conflict"
        if action in {"create_servicenow_aws", "create_servicenow_ncr"}:
            request_type = "aws" if action.endswith("aws") else "ncr"
            current_step = 3
            data["_step"] = current_step
            draft_id = candidate_draft_id
            data["_draft_id"] = draft_id
            active_draft_id = draft_id
            missing = validate_readiness(data, request_only=True)
            if not missing:
                missing.extend(_validate_technical_catalog_selections(data))
            data["_saved_at"] = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
            data = _save_draft(draft_id, data)
            session[ACTIVE_DRAFT_KEY] = draft_id
            session[SESSION_KEY] = data
            session.permanent = True
            session.modified = True
            if not missing:
                return redirect(
                    url_for("environment_readiness.servicenow_review", request_type=request_type, draft=draft_id)
                )
            errors = missing
            notice = "Complete Environment and Technical Requirements before preparing requests."
            notice_is_error = True
        if action == "save":
            data["_saved_at"] = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
            draft_id = _clean(request.form.get("draft_id"), limit=40) or active_draft_id or uuid4().hex[:12]
            data["_draft_id"] = draft_id
            data = _save_draft(draft_id, data)
            session[ACTIVE_DRAFT_KEY] = draft_id
            session[SESSION_KEY] = data
            active_draft_id = draft_id
            session.permanent = True
        elif action in {"generate", "publish"}:
            draft_id = candidate_draft_id
            data["_draft_id"] = draft_id
            data["_saved_at"] = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
            data = _save_draft(draft_id, data)
            session[ACTIVE_DRAFT_KEY] = draft_id
            session[SESSION_KEY] = data
            active_draft_id = draft_id
            session.permanent = True
        session.modified = True
        validation_errors = validate_readiness(data)
        if action in {"generate", "publish"} and not validation_errors:
            validation_errors.extend(_validate_technical_catalog_selections(data))
        if action == "publish" and not validation_errors:
            existing = find_current_setup_by_environment(_clean(data.get("environment_name"), limit=40))
            if existing is not None:
                return redirect(url_for("environment_readiness.publish_conflict"))
            try:
                record = publish_setup(
                    data,
                    published_by=_logged_in_user(),
                    setup_owner=_logged_in_user() or "anonymous",
                )
            except ValueError:
                return redirect(url_for("environment_readiness.publish_conflict"))
            return redirect(url_for("environment_readiness.setup_detail", setup_id=record["id"]))
        if action in {"generate", "publish"} and not validation_errors:
            return redirect(url_for("environment_readiness.preview"))
        if action in {"generate", "publish"} and validation_errors:
            errors = validation_errors
            grouped_errors = _errors_by_step(validation_errors)
            current_step = min(grouped_errors) if grouped_errors else 1
            data["_step"] = current_step
            session[SESSION_KEY] = data
        if action == "save":
            notice = "Progress saved."
        elif action == "duplicate_conflict":
            pass
        elif action not in {"create_servicenow_aws", "create_servicenow_ncr"}:
            notice = "Complete every foundation prerequisite before publishing the setup."
    elif data.get("_saved_at"):
        notice = f"Saved draft resumed from {data['_saved_at']}."

    current_errors = validate_readiness(data)
    step_errors = _errors_by_step(errors)
    saved_drafts = sorted(
        _saved_drafts().items(),
        key=lambda item: str(item[1].get("_saved_at") or ""),
        reverse=True,
    )
    return render_template(
        "environment_readiness/index.html",
        data=data,
        errors=errors,
        step_errors=step_errors,
        readiness_summary=readiness_summary(data),
        verification_inputs={key: sorted(fields) for key, fields in VERIFICATION_INPUTS.items()},
        missing_count=len(current_errors),
        complete=not current_errors,
        notice=notice,
        notice_is_error=notice_is_error,
        current_step=current_step,
        share_token=share_token if shared else "",
        shared_step=shared[2] if shared else 0,
        active_draft_id=active_draft_id,
        saved_drafts=saved_drafts,
        requirement_groups=REQUIREMENT_GROUPS,
        conditional_requirements=CONDITIONAL_REQUIREMENTS,
        environment_types=ENVIRONMENT_TYPES,
        environment_type_title_aliases=ENVIRONMENT_TYPE_TITLE_ALIASES,
        aws_regions=AWS_REGIONS,
        aws_region_choices=WIZARD_AWS_REGION_CHOICES,
        aws_account_choices=_cached_aws_account_choices(),
        rancher_clusters=RANCHER_EKS_CLUSTERS,
        rancher_urls=RANCHER_CLUSTER_TO_URL,
        base_dns_domains=BASE_DNS_DOMAINS,
        product_prefixes=PRODUCT_PREFIXES,
        known_praxis_users=_known_praxis_users(),
        setup_types=SETUP_TYPES,
        field_help=FIELD_HELP,
        requirement_help=REQUIREMENT_HELP,
        status_choices=STATUS_CHOICES,
        servicenow=ServiceNowClient().configuration(),
    )


@bp.route("/servicenow/<request_type>/review", methods=["GET", "POST"])
def servicenow_review(request_type: str):
    if request_type not in {"aws", "ncr"}:
        abort(404)
    owner = _logged_in_user() or "anonymous"
    draft_id = _clean(request.values.get("draft_id") or request.args.get("draft"), limit=40)
    if not draft_id:
        draft_id = _clean(session.get(ACTIVE_DRAFT_KEY), limit=40)
    data = get_draft(draft_id, owner) if draft_id else None
    if data is None:
        return redirect(url_for("environment_readiness.workspace"))
    completed_setup = find_completed_setup_by_source_draft(draft_id)
    if completed_setup is not None:
        flash("This completed setup is read-only. Reopen it to create a new revision draft.", "info")
        return redirect(
            url_for("environment_readiness.setup_detail", setup_id=completed_setup["id"])
        )

    draft_url = url_for("environment_readiness.workspace", draft=draft_id, _external=True)
    try:
        generated = build_request_template(request_type, data, draft_url=draft_url)
    except NetworkingContentError as exc:
        return Response(str(exc), status=422, mimetype="text/plain")
    current_template = deepcopy(generated)
    saved_templates = data.get("servicenow_templates")
    saved_templates = saved_templates if isinstance(saved_templates, dict) else {}
    saved_template = saved_templates.get(request_type)
    if isinstance(saved_template, dict) and request_type == 'ncr' and request.method == 'GET':
        changed = saved_template.get('needs_review') or saved_template.get('content_version') != current_template.get('content_version')
        submission_record = get_servicenow_submission(draft_id, owner, request_type)
        if changed and not data.get('ncr_ticket') and not submission_record:
            # Keep previous manual edits for audit while showing the current release automatically.
            history = list(data.get('servicenow_review_history', []))
            history.append(deepcopy(saved_template))
            data['servicenow_review_history'] = history
            saved_template = {**deepcopy(current_template), 'needs_review': False}
            saved_templates['ncr'] = saved_template
            data['servicenow_templates'] = saved_templates
            data = save_draft(draft_id, owner, data, expected_version=data['_version'])
            flash('The networking requirements have been updated automatically. Review the current request before sending. Previous edits are retained in the audit history.', 'info')
    if isinstance(saved_template, dict):
        if request_type == 'ncr':
            # Preserve the ENTIRE reviewed definition, including removed fields/title.
            generated = deepcopy(saved_template)
            generated['needs_review'] = bool(saved_template.get('needs_review')) or (
                saved_template.get('content_version') != current_template.get('content_version')
            )
            current_values = template_values(current_template)
            for field in generated['fields']:
                field['current_value'] = current_values.get(field['key'], '(removed from current template)')
            data['servicenow_templates']['ncr'] = {
                **generated, 'fields': [{k: v for k, v in f.items() if k != 'current_value'} for f in generated['fields']]
            }
            if request.method == 'GET' and generated.get('needs_review') and not saved_template.get('needs_review'):
                data = save_draft(draft_id, owner, data, expected_version=data['_version'])
        saved_values = template_values(saved_template)
        for field in generated["fields"] if request_type != 'ncr' else []:
            if field["key"] in saved_values:
                field["current_value"] = field["value"]
                field["value"] = saved_values[field["key"]]

    notice = ""
    error = ""
    client = ServiceNowClient()
    if request.method == "POST":
        action = _clean(request.form.get("review_action"), limit=30)
        if action not in {'refresh_template', 'save_review', 'submit_oauth', 'attach_manual'}:
            return Response('Unsupported review action.', status=400)
        if request_type == 'ncr' and action in {'save_review', 'submit_oauth'} and (
            generated.get('needs_review') or request.form.get('content_version') != current_template['content_version']
        ):
            return Response('Networking requirements changed while this page was open. Reload to get the latest template, then review and save or submit.', status=409, mimetype='text/plain')
        if action == 'refresh_template':
            generated = deepcopy(current_template)
        for field in generated["fields"] if action not in {'refresh_template', 'attach_manual'} else []:
            field["value"] = (
                str(request.form.get(f"template_{field['key']}") or '').strip()
            )
        if action != 'attach_manual':
            generated["needs_review"] = False
        saved_templates[request_type] = {
            **generated, 'fields': [{k: v for k, v in f.items() if k != 'current_value'} for f in generated['fields']]
        }
        data["servicenow_templates"] = saved_templates
        data["_saved_at"] = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")

        # Reject stale forms before any external side effect, and persist the reviewed template.
        if action == "submit_oauth":
            data = _save_draft(draft_id, data)
        reference_field = "aws_service_request" if request_type == "aws" else "ncr_ticket"
        if action == "submit_oauth":
            submission_owner = _logged_in_user() or "anonymous"
            claimed = False
            if _clean(data.get(reference_field), limit=80):
                error = f"A ServiceNow request is already attached as {data[reference_field]}."
            elif validate_readiness(data, request_only=True):
                error = "The request inputs need review. Return to the readiness wizard and resolve the request milestone blockers."
            elif any(not _clean(field.get("value")) for field in generated["fields"]):
                error = "Complete every request-template field before submitting it."
            elif not client.configured(request_type):
                error = "OAuth submission is not configured yet. Use the manual ServiceNow handoff below."
            else:
                try:
                    claimed = claim_servicenow_submission(draft_id, submission_owner, request_type, snapshot=generated)
                    if not claimed:
                        prior = get_servicenow_submission(draft_id, submission_owner, request_type)
                        if prior and prior["status"] == "completed":
                            complete_servicenow_submission(
                                draft_id, submission_owner, request_type,
                                request_number=prior["request_number"], request_url=prior["request_url"],
                            )
                            return redirect(url_for("environment_readiness.workspace", draft=draft_id))
                        raise ServiceNowOutcomeUnknown(
                            "A submission is in progress or its outcome is unknown. Check ServiceNow "
                            "and attach the existing request; do not create another request."
                        )
                    created = client.create_catalog_request(
                        request_type,
                        data,
                        draft_url=draft_url,
                        overrides={**template_values(generated), 'request_content': template_text(generated)},
                    )
                    data[reference_field] = created.number
                    data[f"{reference_field}_url"] = created.url
                    data[f"{reference_field}_status"] = created.status
                    complete_servicenow_submission(
                        draft_id,
                        submission_owner,
                        request_type,
                        request_number=created.number,
                        request_url=created.url,
                    )
                    notice = f"ServiceNow request {created.number} was submitted and attached."
                except ServiceNowOutcomeUnknown as exc:
                    error = str(exc)
                except ServiceNowError as exc:
                    if claimed:
                        release_servicenow_submission(draft_id, submission_owner, request_type)
                    error = str(exc)
        elif action == "attach_manual":
            reference = _clean(request.form.get("manual_reference"), limit=80).upper()
            reference_url = _clean(request.form.get("manual_reference_url"), limit=1000)
            if not re.fullmatch(r"[A-Z][A-Z0-9]+-?\d+", reference):
                error = "Enter the real ServiceNow REQ, RITM, or SCTASK number after creating it in the portal."
            elif reference_url and not re.match(r"https?://", reference_url, re.I):
                error = "The ServiceNow request link must be an http(s) URL."
            else:
                data[reference_field] = reference
                data[f"{reference_field}_url"] = reference_url or client.portal_url
                data[f"{reference_field}_status"] = "manually_submitted"
                notice = f"ServiceNow request {reference} was attached to this readiness draft."
        elif action == "refresh_template":
            notice = "Template refreshed from the current draft. Review it and update the existing ServiceNow request if one has already been raised."
        elif action == "save_review":
            notice = "Request template saved. It has not been submitted to ServiceNow."

        if action == "submit_oauth":
            data = get_draft(draft_id, owner) or data
        else:
            _prefill_servicenow_evidence(data)
            data = _save_draft(draft_id, data)
            if action == "attach_manual" and notice:
                complete_servicenow_submission(
                    draft_id, owner, request_type,
                    request_number=data[reference_field], request_url=data[f"{reference_field}_url"],
                    request_status="manually_submitted",
                )
                data = get_draft(draft_id, owner) or data
        session[SESSION_KEY] = data
        session[ACTIVE_DRAFT_KEY] = draft_id
        session.modified = True
        if notice and action in {"submit_oauth", "attach_manual"}:
            return redirect(url_for("environment_readiness.workspace", draft=draft_id))

    return render_template(
        "environment_readiness/servicenow_review.html",
        data=data,
        request_template=generated,
        request_text=template_text(generated),
        request_debug_text=(template_text(build_request_template(
            request_type, data, draft_url=draft_url, include_details=True
        )) if request_type == 'ncr' else ''),
        request_type=request_type,
        servicenow=client.configuration(),
        submission=get_servicenow_submission(draft_id, owner, request_type),
        notice=notice,
        error=error,
        draft_id=draft_id,
    )


@bp.route("/publish-conflict", methods=["GET", "POST"])
def publish_conflict():
    data = _saved_data()
    errors = validate_readiness(data)
    if errors:
        return redirect(url_for("environment_readiness.workspace"))
    existing = find_current_setup_by_environment(_clean(data.get("environment_name"), limit=40))
    if existing is None:
        return redirect(url_for("environment_readiness.workspace"))
    if request.method == "POST":
        action = _clean(request.form.get("conflict_action"), limit=30)
        if action == "open_existing":
            return redirect(url_for("environment_readiness.setup_detail", setup_id=existing["id"]))
        if action == "cancel":
            return redirect(url_for("environment_readiness.workspace"))
        if action == "create_revision":
            _require_setup_editor(existing)
            if existing.get("status") == "completed":
                if not _can_reopen_setup(existing):
                    abort(403)
                if str(data.get("_reopened_from_setup_id") or "") != str(existing["id"]):
                    abort(409)
            try:
                record = publish_setup(
                    data,
                    published_by=_logged_in_user(),
                    revision_of=str(existing["logical_id"]),
                    expected_revision=int(existing["revision"]),
                    expected_setup_owner=(
                        None
                        if not current_app.config.get("AUTH_ENABLED", False) or _is_setup_admin()
                        else _logged_in_user()
                    ),
                )
            except ValueError:
                abort(409)
            return redirect(url_for("environment_readiness.setup_detail", setup_id=record["id"]))
        abort(400)
    previous_data = existing.get("payload") or {}
    changed_fields = [
        (change["field"], change["before"], change["after"])
        for change in revision_changes(previous_data, data)
    ]
    is_reopen_draft = (
        existing.get("status") == "completed"
        and str(data.get("_reopened_from_setup_id") or "") == str(existing["id"])
    )
    return render_template(
        "environment_readiness/publish_conflict.html",
        data=data,
        existing=existing,
        changed_fields=changed_fields,
        can_edit_existing=(
            _can_edit_setup(existing)
            and (existing.get("status") != "completed" or is_reopen_draft)
        ),
        completed_locked=existing.get("status") == "completed" and not is_reopen_draft,
        is_reopen_draft=is_reopen_draft,
    )


@bp.get("/preview")
def preview():
    data = _saved_data()
    errors = validate_readiness(data)
    if errors:
        return redirect(url_for("environment_readiness.workspace"))
    return render_template(
        "environment_readiness/preview.html",
        data=data,
        markdown=build_markdown(data),
        handoff_fields=(*_handoff_fields(data), *SERVICE_REQUEST_FIELDS),
        active_requirements=all_requirements(data),
    )


@bp.get("/download.md")
def download_markdown():
    data = _saved_data()
    errors = validate_readiness(data)
    if errors:
        return redirect(url_for("environment_readiness.workspace"))
    markdown = build_markdown(data)
    filename = "-".join(_clean(data.get("environment_name")).lower().split()) or "praxis-foundation"
    return Response(
        markdown,
        mimetype="text/markdown",
        headers={"Content-Disposition": f'attachment; filename="{filename}-foundation-readiness.md"'},
    )


@bp.get("/setups/<setup_id>")
def setup_detail(setup_id: str):
    record = get_setup(_clean(setup_id, limit=64))
    if record is None:
        abort(404)
    data = record["payload"]
    snapshot_errors = validate_readiness(data)
    source_draft = None
    wizard_definition = wizard_for_setup(record.get("setup_type", ""))
    bootstrap_errors = session.pop("environment_readiness:bootstrap_errors", [])
    bootstrap_input = session.pop("environment_readiness:bootstrap_input", {})
    spacelift_integrations: list[tuple[str, str]] = []
    spacelift_spaces: list[tuple[str, str]] = []
    other_spacelift_integrations: list[tuple[str, str]] = []
    other_spacelift_spaces: list[tuple[str, str]] = []
    spacelift_options_error = ""
    bootstrap_valid = bool(record.get("wizard_workspace_id"))
    wizard_access = ""
    if record.get("wizard_workspace_id"):
        from services.wizard_workspaces import get_workspace_for_user

        workspace = get_workspace_for_user(
            _logged_in_user() or "local-user", str(record["wizard_workspace_id"])
        )
        wizard_access = str((workspace or {}).get("access") or "")
    if record.get("status") == "started" and not record.get("wizard_workspace_id"):
        stored_bootstrap = record.get("bootstrap") if isinstance(record.get("bootstrap"), dict) else {}
        stored_errors, spacelift_integrations, spacelift_spaces = _validate_bootstrap(stored_bootstrap)
        spacelift_integrations, other_spacelift_integrations = _rank_bootstrap_options(
            data, spacelift_integrations, kind="integration"
        )
        spacelift_spaces, other_spacelift_spaces = _rank_bootstrap_options(
            data, spacelift_spaces, kind="space"
        )
        bootstrap_valid = bool(stored_bootstrap.get("completed_at")) and not stored_errors
        if stored_bootstrap.get("completed_at") and stored_errors and not bootstrap_errors:
            bootstrap_errors = stored_errors
        if not (spacelift_integrations or other_spacelift_integrations) or not (spacelift_spaces or other_spacelift_spaces):
            spacelift_options_error = "Spacelift spaces or AWS integrations could not be loaded. Refresh the page before confirming bootstrap."
    if (
        record.get("source_draft_id")
        and record.get("status") != "completed"
        and _can_edit_setup(record)
    ):
        source_draft = get_draft(
            str(record["source_draft_id"]), _logged_in_user() or "anonymous"
        )
    return render_template(
        "environment_readiness/setup_detail.html",
        record=record,
        data=data,
        markdown=build_markdown(data, enforce_readiness=False),
        snapshot_errors=snapshot_errors,
        source_draft=source_draft,
        handoff_fields=(*_handoff_fields(data), *SERVICE_REQUEST_FIELDS),
        active_requirements=all_requirements(data),
        today_iso=datetime.now(timezone.utc).date().isoformat(),
        wizard_definition=wizard_definition,
        bootstrap_errors=bootstrap_errors,
        bootstrap_input=bootstrap_input,
        spacelift_integrations=spacelift_integrations,
        spacelift_spaces=spacelift_spaces,
        other_spacelift_integrations=other_spacelift_integrations,
        other_spacelift_spaces=other_spacelift_spaces,
        spacelift_options_error=spacelift_options_error,
        bootstrap_valid=bootstrap_valid,
        can_edit_setup=_can_edit_setup(record) and record.get("status") != "completed",
        can_operate_setup=_can_operate_setup(record) and record.get("status") != "completed",
        can_reopen_setup=(
            bool(record.get("is_current"))
            and record.get("status") == "completed"
            and _can_reopen_setup(record)
        ),
        known_praxis_users=_known_praxis_users(),
        wizard_is_owner=(
            not record.get("wizard_workspace_id") or wizard_access == "owner"
        ),
        wizard_can_edit=(
            not record.get("wizard_workspace_id") or wizard_access in {"owner", "editor"}
        ),
    )


@bp.post("/setups/<setup_id>/reopen")
def reopen_setup(setup_id: str):
    existing = get_setup(_clean(setup_id, limit=64))
    if existing is None:
        abort(404)
    if not _can_reopen_setup(existing):
        abort(403)
    reason = _clean(request.form.get("reopen_reason"), limit=1000)
    target_date = _clean(request.form.get("reopen_target_date"), limit=10)
    detail_url = url_for("environment_readiness.setup_detail", setup_id=existing["id"])
    try:
        draft = reopen_completed_setup(
            existing["id"],
            owner=_logged_in_user() or str(existing.get("setup_owner") or "anonymous"),
            reason=reason,
            target_date=target_date,
        )
    except ValueError as exc:
        flash(str(exc), "warning")
        return redirect(detail_url)
    if draft is None:
        abort(404)
    session[ACTIVE_DRAFT_KEY] = str(draft["_draft_id"])
    session[SESSION_KEY] = draft
    session.permanent = True
    session.modified = True
    flash(
        "A new revision draft was created. The completed record remains read-only until this draft is published.",
        "success",
    )
    return redirect(
        url_for("environment_readiness.workspace", draft=draft["_draft_id"])
    )


@bp.post("/setups/<setup_id>/transfer-owner")
def transfer_setup_owner(setup_id: str):
    existing = get_setup(_clean(setup_id, limit=64))
    if existing is None:
        abort(404)
    _require_setup_editor(existing)
    new_owner = _clean(request.form.get("new_setup_owner"), limit=200)
    detail_url = url_for("environment_readiness.setup_detail", setup_id=existing["id"])
    if _identity_matches(new_owner, str(existing.get("setup_owner") or "")):
        flash("You are already the setup owner. Nothing was changed.", "info")
        return redirect(detail_url)
    try:
        record = transfer_setup_ownership(
            existing["id"],
            new_owner=new_owner,
            changed_by=_logged_in_user(),
            expected_owner=(
                None
                if not current_app.config.get("AUTH_ENABLED", False) or _is_setup_admin()
                else _logged_in_user()
            ),
        )
    except ValueError as exc:
        flash(str(exc), "warning")
        return redirect(detail_url)
    if record is None:
        abort(404)
    if (
        _clean(session.get(ACTIVE_DRAFT_KEY), limit=40)
        == str(existing.get("source_draft_id") or "")
    ):
        session.pop(ACTIVE_DRAFT_KEY, None)
        session.pop(SESSION_KEY, None)
        session.modified = True
    return redirect(url_for("environment_readiness.setup_detail", setup_id=record["id"]))


@bp.post("/setups/<setup_id>/reassign-devops-owner")
def reassign_devops_owner(setup_id: str):
    existing = get_setup(_clean(setup_id, limit=64))
    if existing is None:
        abort(404)
    _require_setup_editor(existing)
    detail_url = url_for("environment_readiness.setup_detail", setup_id=existing["id"])
    if existing.get("status") == "completed":
        flash(
            "Completed setups are read-only. Reopen the setup before changing the Receiving DevOps owner.",
            "warning",
        )
        return redirect(detail_url)
    new_owner = _clean(request.form.get("new_devops_owner"), limit=200)
    reason = _clean(request.form.get("reassignment_reason"), limit=1000)
    current_owner = str(existing.get("devops_owner") or "").strip()
    if not new_owner:
        flash("Select the new Receiving DevOps owner.", "warning")
        return redirect(detail_url)
    if _identity_matches(new_owner, current_owner):
        flash("That user is already the Receiving DevOps owner. Nothing was changed.", "info")
        return redirect(detail_url)
    if not reason:
        flash("Explain why the Receiving DevOps owner is changing.", "warning")
        return redirect(detail_url)

    workspace_id = str(existing.get("wizard_workspace_id") or "").strip()
    workspace_handoff = None
    if workspace_id:
        from services.wizard_workspaces import handoff_workspace_access

        workspace_handoff = handoff_workspace_access(
            workspace_id=workspace_id,
            current_assignee=current_owner,
            new_assignee=new_owner,
        )
        if workspace_handoff is None:
            flash(
                "The linked provisioning workspace could not be handed over, so the DevOps owner was not changed.",
                "warning",
            )
            return redirect(detail_url)

    try:
        record = reassign_setup_devops_owner(
            existing["id"],
            new_owner=new_owner,
            changed_by=_logged_in_user(),
            reason=reason,
            expected_devops_owner=current_owner,
            wizard_owner=(workspace_handoff or {}).get("owner"),
        )
    except ValueError as exc:
        if workspace_handoff is not None:
            from services.wizard_workspaces import handoff_workspace_access

            restored = handoff_workspace_access(
                workspace_id=workspace_id,
                current_assignee=new_owner,
                new_assignee=current_owner,
            )
            if restored is None:
                logger.error(
                    "Unable to restore wizard workspace access after DevOps reassignment failed",
                    extra={"setup_id": existing["id"], "workspace_id": workspace_id},
                )
        flash(str(exc), "warning")
        return redirect(detail_url)
    if record is None:
        abort(404)
    if (
        _clean(session.get(ACTIVE_DRAFT_KEY), limit=40)
        == str(existing.get("source_draft_id") or "")
    ):
        session.pop(ACTIVE_DRAFT_KEY, None)
        session.pop(SESSION_KEY, None)
        session.modified = True
    flash(
        f"Receiving DevOps ownership reassigned from {current_owner} to {new_owner}.",
        "success",
    )
    return redirect(url_for("environment_readiness.setup_detail", setup_id=record["id"]))


@bp.post("/setups/<setup_id>/clarification")
def setup_clarification(setup_id: str):
    record = get_setup(_clean(setup_id, limit=64))
    if record is None:
        abort(404)
    resolve = request.form.get("clarification_action") == "respond"
    if resolve:
        _require_setup_editor(record)
    else:
        _require_setup_operator(record)
    try:
        clarify_setup(record["id"], actor=_logged_in_user() or "Unknown user",
                      note=_clean(request.form.get("clarification_note"), limit=1000), resolve=resolve)
    except ValueError as exc:
        flash(str(exc), "warning")
        return redirect(url_for("environment_readiness.setup_detail", setup_id=record["id"])), 303
    flash("Response recorded; DevOps must acknowledge the handoff again." if resolve else "Returned to the setup owner for clarification.", "success")
    return redirect(url_for("environment_readiness.setup_detail", setup_id=record["id"]))


@bp.post("/setups/<setup_id>/start")
def start_setup(setup_id: str):
    existing = get_setup(_clean(setup_id, limit=64))
    if existing is None:
        abort(404)
    _require_setup_operator(existing)
    action = _clean(request.form.get("status_action"), limit=20) or "started"
    status_by_action = {
        "acknowledged": "acknowledged",
        "started": "started",
        "resume": "started",
        "paused": "paused",
        "completed": "completed",
    }
    to_status = status_by_action.get(action)
    if to_status is None:
        abort(400)
    try:
        record = transition_setup(
            _clean(setup_id, limit=64),
            to_status=to_status,
            changed_by=_logged_in_user(),
            note=_clean(request.form.get("status_note"), limit=1000),
            revised_target_date=_clean(request.form.get("revised_target_date"), limit=10),
        )
    except ValueError:
        abort(400)
    if record is None:
        abort(404)
    return redirect(url_for("environment_readiness.setup_detail", setup_id=record["id"]))


@bp.post("/setups/<setup_id>/bootstrap")
def setup_bootstrap(setup_id: str):
    existing = get_setup(_clean(setup_id, limit=64))
    if existing is None:
        abort(404)
    _require_setup_operator(existing)
    if existing.get("status") != "started":
        abort(409)
    values = {
        "spacelift_space_id": _clean(request.form.get("spacelift_space_id"), limit=200),
        "admin_stack_reference": _clean(request.form.get("admin_stack_reference"), limit=1000),
        "aws_integration_id": _clean(request.form.get("aws_integration_id"), limit=200),
    }
    errors, _integrations, _spaces = _validate_bootstrap(values)
    if errors:
        session["environment_readiness:bootstrap_errors"] = errors
        session["environment_readiness:bootstrap_input"] = values
        session.modified = True
        return redirect(url_for("environment_readiness.setup_detail", setup_id=setup_id))
    try:
        record = complete_setup_bootstrap(
            _clean(setup_id, limit=64),
            **values,
            completed_by=_logged_in_user(),
        )
    except ValueError:
        abort(400)
    if record is None:
        abort(404)
    return redirect(url_for("environment_readiness.setup_detail", setup_id=record["id"]))


@bp.post("/setups/<setup_id>/wizard")
def start_setup_wizard(setup_id: str):
    from services.wizard_workspaces import (
        create_workspace,
        current_owner,
        delete_workspace,
        get_workspace_for_user,
        load_workspace_state,
        sync_workspace,
    )

    record = get_setup(_clean(setup_id, limit=64))
    if record is None:
        abort(404)
    _require_setup_operator(record)
    if record.get("status") != "started":
        abort(409)
    definition = wizard_for_setup(record.get("setup_type", ""))
    if definition is None:
        abort(400)
    env_slug, _label = definition
    owner = current_owner(session)
    workspace_id = str(record.get("wizard_workspace_id") or "")
    bootstrap = record.get("bootstrap") if isinstance(record.get("bootstrap"), dict) else {}
    bootstrap_errors, _integrations, _spaces = _validate_bootstrap(bootstrap)
    if not workspace_id and (not bootstrap.get("completed_at") or bootstrap_errors):
        abort(409)
    if workspace_id:
        workspace = get_workspace_for_user(owner, workspace_id)
        if workspace is None:
            if str(record.get("wizard_owner") or "") != owner:
                abort(403)
            from .store import unlink_wizard_workspace

            unlink_wizard_workspace(workspace_id, wizard_owner=owner)
            workspace_id = ""
            workspace = None
    if not workspace_id:
        workspace = create_workspace(
            owner=owner,
            env_slug=env_slug,
            readiness_setup_id=str(record["id"]),
            state=build_wizard_state(record["payload"], env_slug, bootstrap),
            current_step="project_settings",
        )
        try:
            link_wizard_workspace(
                record["id"],
                workspace_id=str(workspace["id"]),
                wizard_slug=env_slug,
                wizard_owner=owner,
            )
        except ValueError:
            delete_workspace(owner=owner, workspace_id=str(workspace["id"]))
            abort(409)

    load_workspace_state(workspace, session)
    if workspace_id and request.form.get("refresh_from_readiness") == "1":
        if str(workspace.get("access") or "") != "owner":
            abort(403)
        imported = build_wizard_state(record["payload"], env_slug, bootstrap)
        for key, value in imported.items():
            current = session.get(key)
            session[key] = merge_wizard_state(current, value) if isinstance(current, dict) and isinstance(value, dict) else value
        session.modified = True
        sync_workspace(
            workspace_id=str(workspace["id"]),
            owner=str(workspace["owner"]),
            env_slug=env_slug,
            session_obj=session,
            current_step=str(workspace.get("current_step") or "project_settings"),
            spec_yaml="",
            latest_job_id="",
        )
    return redirect(url_for(f"{env_slug}_wizard.wizard_step", step="project_settings"))


@bp.get("/setups/<setup_id>/download.md")
def download_published_markdown(setup_id: str):
    record = get_setup(_clean(setup_id, limit=64))
    if record is None:
        abort(404)
    data = record["payload"]
    filename = "-".join(_clean(data.get("environment_name")).lower().split()) or "praxis-foundation"
    return Response(
        build_markdown(data, enforce_readiness=False),
        mimetype="text/markdown",
        headers={"Content-Disposition": f'attachment; filename="{filename}-foundation-readiness.md"'},
    )
