from __future__ import annotations

import difflib
import re
from datetime import datetime
from typing import Any

from flask import Blueprint, current_app, redirect, render_template, request, session, url_for

from engine.wizards.factory.core_validate_jobs import (
    feature_state,
    get_job,
    start_validation_job,
)
from constants import DOMAIN_CHOICES, RANCHER_EKS_CLUSTERS
from engine.wizards.constants.project_constants import UNIFIED_ENV_TYPES_CHOICES
from engine.wizards.constants.vpc_constants import VPC_AZ_PROFILE_CHOICES
from engine.wizards.constants.replacements_constants import RANCHER_CONTEXT_CHOICES

from .confluence import ConfluenceError, load_config, publish_revision
from .starter_templates import (
    get_wizard_template,
    list_wizard_templates,
    normalize_template_prefix,
    template_prefix_placeholder,
    template_prefix_suffix,
)
from .workbench import (
    analyze_spec_yaml,
    create_draft,
    create_validation,
    delete_draft,
    derive_title,
    get_draft,
    get_validation_by_job,
    latest_matching_successful_validation,
    latest_successful_validation,
    list_drafts,
    list_publications,
    list_validations,
    record_publication,
    release_reserved_revision,
    reserve_next_revision_number,
    sync_draft_jobs,
    update_draft,
    update_draft_confluence_parent,
)

bp = Blueprint(
    "spec_validator",
    __name__,
    url_prefix="/spec-validator",
    template_folder="templates",
)

_JOB_ID_RE = re.compile(r"^[a-f0-9]{32}$")
_ENV_SLUG = "spec-validator"
_ARCHITECTURE_FIELDS = ("architect", "customer", "environments", "purpose", "status", "ticket")
_SECTION_GUIDANCE = {
    "spec_type": ("Spec family", "Selects the Praxis Core contract and validation rules."),
    "project_settings": ("Environment shape", "Declares target environments and the deployments each environment receives."),
    "repository_settings": ("Repository naming", "Controls generated repository prefixes and human-readable descriptions."),
    "spacelift_settings": ("Cloud access", "Maps each environment to the Spacelift AWS integration used for provisioning."),
    "replacements": ("Template substitutions", "Connects naming, repositories, Rancher contexts, and platform identifiers."),
    "common": ("Shared identity", "Defines customer, product, domain, region, and the common environment name."),
    "rancher": ("Cluster registration", "Defines management clusters, account aliases, labels, and multi-mesh behavior."),
    "vpc": ("Network foundation", "Defines CIDRs, availability zones, Transit Gateway, and endpoint services."),
    "subnets": ("Workload networks", "Allocates component-specific CIDR ranges inside each environment VPC."),
    "eks": ("Kubernetes capacity", "Defines Kubernetes versions, access, clusters, and managed node groups."),
    "aurora": ("Relational database", "Defines Aurora engine versions and per-environment instance capacity."),
    "redis": ("Cache capacity", "Defines Redis instance types and cluster sizes per environment."),
}


def _format_ts(epoch: float | int | None) -> str:
    if not epoch:
        return "-"
    try:
        return datetime.fromtimestamp(float(epoch)).strftime("%Y-%m-%d %H:%M:%S")
    except Exception:
        return "-"


def _current_owner() -> str:
    owner = str(session.get("user") or "").strip()
    if owner:
        return owner
    return "local-user"


def _draft_profile_from_request() -> tuple[str | None, dict[str, str] | None]:
    if "mode" not in request.form and not any(field in request.form for field in _ARCHITECTURE_FIELDS):
        return None, None
    mode = "prerequisite" if request.form.get("mode") == "prerequisite" else "playground"
    metadata = {
        field: str(request.form.get(field) or "").strip()[:500]
        for field in _ARCHITECTURE_FIELDS
    }
    return mode, metadata


def _local_spec_checks(draft: dict[str, Any], analysis: dict[str, Any]) -> list[dict[str, Any]]:
    yaml_text = str(draft.get("working_yaml") or "")
    parsed_ok = bool(analysis.get("parsed_ok"))
    mapping_ok = parsed_ok and analysis.get("top_type") == "mapping"
    contract_ok = mapping_ok and bool(str(analysis.get("spec_type") or "").strip())
    placeholders = sorted(set(re.findall(r"__[A-Z0-9_]+__", yaml_text)))
    return [
        {"key": "syntax", "label": "YAML syntax", "ok": parsed_ok, "detail": "Valid YAML" if parsed_ok else str(analysis.get("error") or "Invalid YAML")},
        {"key": "structure", "label": "Spec structure", "ok": mapping_ok, "detail": "Top-level mapping" if mapping_ok else "A top-level YAML mapping is required"},
        {"key": "contract", "label": "Spec contract", "ok": contract_ok, "detail": f"spec_type: {analysis.get('spec_type')}" if contract_ok else "spec_type is required"},
        {"key": "inputs", "label": "Required inputs", "ok": not placeholders, "detail": "No placeholders remain" if not placeholders else f"{len(placeholders)} placeholder value(s) remain"},
    ]


def _prerequisite_metadata_blockers(draft: dict[str, Any]) -> list[str]:
    if draft.get("mode") != "prerequisite":
        return []
    metadata = draft.get("architecture_metadata") or {}
    labels = {
        "architect": "Architect",
        "customer": "Customer",
        "environments": "Environments",
        "purpose": "Purpose",
        "status": "Decision status",
        "ticket": "Related ticket",
    }
    missing = [labels[field] for field in _ARCHITECTURE_FIELDS if not str(metadata.get(field) or "").strip()]
    return [f"Architecture metadata required: {', '.join(missing)}."] if missing else []


def _defaults_from_state(state: dict[str, Any]) -> dict[str, str]:
    defaults = state.get("defaults")
    if not isinstance(defaults, dict):
        defaults = {}
    return {
        "harbor_registry_project": str(defaults.get("harbor_registry_project") or "").strip(),
        "pc_version": str(defaults.get("pc_version") or "").strip(),
    }


def _selected_template_pc_version(default_pc_version: str) -> str:
    selected = str(request.args.get("pc_version") or "").strip()
    return selected or str(default_pc_version or "").strip()


def _selected_template_vpc_profile() -> str:
    return str(request.args.get("vpc_profile") or "").strip().lower()


def _selected_template_envs() -> list[str]:
    raw_values = request.args.getlist("template_env")
    if not raw_values:
        csv_values = str(request.args.get("template_env_csv") or "").strip()
        if csv_values:
            raw_values = csv_values.split(",")

    selected: list[str] = []
    for value in raw_values:
        normalized = str(value or "").strip().lower()
        if normalized and normalized in {env for env, _label in UNIFIED_ENV_TYPES_CHOICES} and normalized not in selected:
            selected.append(normalized)
    return selected


def _selected_template_prefix() -> str:
    return str(request.args.get("template_prefix") or "").strip().lower()


def _selected_template_domain() -> str:
    return str(request.args.get("template_domain") or "").strip().lower()


def _selected_template_rancher_cluster() -> str:
    return str(request.args.get("rancher_cluster") or "").strip()


def _selected_template_rancher_context() -> str:
    return str(request.args.get("rancher_context") or "").strip().lower()


def _template_options_ready(
    *,
    template_key: str,
    pc_version: str,
    template_prefix: str,
    template_envs: list[str],
    template_domain: str,
    vpc_profile: str,
    rancher_cluster: str,
    rancher_context: str,
    release_customer: str,
    release_key: str,
) -> bool:
    if not template_key:
        return False

    core_ready = all(
        [
            str(pc_version or "").strip(),
            str(template_prefix or "").strip(),
            bool(template_envs),
            str(template_domain or "").strip(),
            str(vpc_profile or "").strip(),
            str(rancher_cluster or "").strip(),
            str(rancher_context or "").strip(),
        ]
    )
    release_ready = (not release_customer and not release_key) or (release_customer and release_key)
    return core_ready and release_ready


def _template_release_context(template_key: str) -> dict[str, Any]:
    context: dict[str, Any] = {
        "customer_choices": [],
        "release_choices": [],
        "selected_customer": str(request.args.get("release_customer") or "").strip(),
        "selected_release_key": str(request.args.get("release_key") or "").strip(),
        "error": "",
        "manifest": None,
        "source": {"bucket": "", "prefix": ""},
        "summary": None,
    }
    if not template_key:
        return context

    try:
        from modules.praxisrelease.service import (
            BUCKET,
            fetch_release_manifest,
            filter_release_names_for_wizard,
            get_release_source,
            list_customer_releases,
            list_customers,
        )
    except Exception as exc:
        context["error"] = f"Release selector unavailable: {exc}"
        return context

    context["source"] = get_release_source()

    try:
        context["customer_choices"] = [(customer, customer) for customer in list_customers()]
    except Exception as exc:
        context["error"] = f"Could not load release customers: {exc}"
        return context

    selected_customer = str(context["selected_customer"] or "").strip()
    if not selected_customer:
        context["selected_release_key"] = ""
        return context

    try:
        releases = list_customer_releases(BUCKET, selected_customer)
        releases = filter_release_names_for_wizard(releases, template_key)
        context["release_choices"] = [(release, release) for release in releases]
    except Exception as exc:
        context["error"] = f"Could not load releases for {selected_customer}: {exc}"
        context["selected_release_key"] = ""
        return context

    valid_release_keys = {value for value, _label in context["release_choices"]}
    selected_release_key = str(context["selected_release_key"] or "").strip()
    if selected_release_key not in valid_release_keys:
        context["selected_release_key"] = ""
        return context

    try:
        manifest = fetch_release_manifest(BUCKET, selected_release_key, selected_customer)
    except Exception as exc:
        context["error"] = f"Could not load deployment versions from {selected_release_key}: {exc}"
        context["selected_release_key"] = ""
        return context

    if isinstance(manifest, dict):
        context["manifest"] = manifest
        context["summary"] = {
            "bundle": str(manifest.get("bundle") or "").strip(),
            "version": str(manifest.get("version") or "").strip(),
        }
    return context


def _decode_uploaded_spec_text() -> tuple[str, str]:
    uploaded = request.files.get("spec_file")
    name = (getattr(uploaded, "filename", "") or "").strip()
    if not uploaded or not name:
        return "", ""

    raw = uploaded.read()
    if not raw:
        raise ValueError("Uploaded file is empty.")
    if len(raw) > 900_000:
        raise ValueError("Uploaded file is too large (max 900KB).")

    try:
        return raw.decode("utf-8"), name
    except UnicodeDecodeError:
        try:
            return raw.decode("utf-8-sig"), name
        except UnicodeDecodeError as exc:
            raise ValueError("Uploaded file must be UTF-8 encoded text.") from exc


def _spec_text_from_request(*, required: bool) -> tuple[str, str]:
    uploaded_text, uploaded_name = _decode_uploaded_spec_text()
    if uploaded_text.strip():
        text = uploaded_text
    else:
        text = (request.form.get("spec_yaml") or "").replace("\r\n", "\n")

    if required and not text.strip():
        raise ValueError("Provide YAML content or upload a spec file.")
    if text and len(text.encode("utf-8")) > 900_000:
        raise ValueError("Spec YAML is too large (max 900KB).")

    if text and not text.endswith("\n"):
        text += "\n"
    return text, uploaded_name


def _validation_message(exc: Exception) -> str:
    if isinstance(exc, ValueError):
        return str(exc)
    current_app.logger.exception("Spec validation request failed")
    return "Validation request failed."


def _workspace_validation_defaults(
    defaults: dict[str, str],
    validations: list[dict[str, Any]],
) -> dict[str, str]:
    if not validations:
        return defaults
    latest = validations[0]
    return {
        "harbor_registry_project": str(
            latest.get("harbor_registry_project") or defaults.get("harbor_registry_project") or ""
        ).strip(),
        "pc_version": str(latest.get("pc_version") or defaults.get("pc_version") or "").strip(),
    }


def _publish_blockers(
    *,
    confluence: dict[str, Any],
    matching_validation: dict[str, Any] | None,
    latest_success: dict[str, Any] | None,
    draft: dict[str, Any],
) -> list[str]:
    blockers: list[str] = []
    analysis = analyze_spec_yaml(str(draft.get("working_yaml") or ""))
    blockers.extend(_prerequisite_metadata_blockers(draft))
    failed_local_checks = [check["label"] for check in _local_spec_checks(draft, analysis) if not check["ok"]]
    if failed_local_checks:
        blockers.append(f"Local readiness checks incomplete: {', '.join(failed_local_checks)}.")
    if not confluence.get("enabled"):
        blockers.append(str(confluence.get("reason") or "Confluence is not configured.").strip())
        return blockers

    if matching_validation is not None:
        return blockers

    if latest_success and latest_success.get("content_hash") != draft.get("working_hash"):
        blockers.append("Draft changed after the last successful validation.")
        blockers.append("Run Full Validation again for the current YAML.")
    else:
        blockers.append("No successful validation exists for the current draft.")
        blockers.append("Run Full Validation before publishing.")
    return blockers


def _build_yaml_comparison(
    *,
    label: str,
    compared_at: float | int | None,
    compare_yaml: str,
    current_yaml: str,
) -> dict[str, Any]:
    current_lines = current_yaml.splitlines()
    compare_lines = compare_yaml.splitlines()
    diff_lines = list(
        difflib.unified_diff(
            compare_lines,
            current_lines,
            fromfile=label.lower().replace(" ", "-"),
            tofile="current-draft",
            lineterm="",
        )
    )
    added = sum(1 for line in diff_lines if line.startswith("+") and not line.startswith("+++"))
    removed = sum(1 for line in diff_lines if line.startswith("-") and not line.startswith("---"))
    changed_blocks = sum(1 for line in diff_lines if line.startswith("@@"))
    preview_limit = 220
    preview_lines = diff_lines[:preview_limit]
    return {
        "label": label,
        "compared_at": compared_at,
        "identical": current_lines == compare_lines,
        "added": added,
        "removed": removed,
        "changed_blocks": changed_blocks,
        "preview": "\n".join(preview_lines),
        "truncated": len(diff_lines) > preview_limit,
    }


def _render_index(*, error: str = "", notice: str = "", form_data: dict[str, str] | None = None):
    owner = _current_owner()
    state = feature_state()
    defaults = _defaults_from_state(state)
    drafts = list_drafts(owner)
    confluence = load_config().as_dict()
    selected_template_key = (request.args.get("template") or "").strip().lower()
    selected_template_pc_version = _selected_template_pc_version(defaults.get("pc_version") or "")
    selected_template_vpc_profile = _selected_template_vpc_profile()
    selected_template_envs = _selected_template_envs()
    selected_template_env_csv = ",".join(selected_template_envs)
    env_choice_map = dict(UNIFIED_ENV_TYPES_CHOICES)
    selected_template_env_summary = ", ".join(
        env_choice_map.get(env_name, env_name.title()) for env_name in selected_template_envs
    )
    selected_template_prefix = normalize_template_prefix(
        selected_template_key,
        _selected_template_prefix(),
    )
    selected_template_prefix_suffix = template_prefix_suffix(selected_template_key)
    selected_template_prefix_placeholder = template_prefix_placeholder(selected_template_key)
    selected_template_domain = _selected_template_domain()
    selected_template_rancher_cluster = _selected_template_rancher_cluster()
    selected_template_rancher_context = _selected_template_rancher_context()
    template_release = _template_release_context(selected_template_key)
    template_options_ready = _template_options_ready(
        template_key=selected_template_key,
        pc_version=selected_template_pc_version,
        template_prefix=selected_template_prefix,
        template_envs=selected_template_envs,
        template_domain=selected_template_domain,
        vpc_profile=selected_template_vpc_profile,
        rancher_cluster=selected_template_rancher_cluster,
        rancher_context=selected_template_rancher_context,
        release_customer=str(template_release.get("selected_customer") or ""),
        release_key=str(template_release.get("selected_release_key") or ""),
    )
    wizard_templates = list_wizard_templates(
        selected_template_pc_version,
        release_manifest=template_release.get("manifest"),
        vpc_profile=selected_template_vpc_profile,
        template_envs=selected_template_envs,
        template_prefix=selected_template_prefix,
        template_domain=selected_template_domain,
        rancher_cluster=selected_template_rancher_cluster,
        rancher_context=selected_template_rancher_context,
    )
    selected_template = get_wizard_template(
        selected_template_key,
        selected_template_pc_version,
        release_manifest=template_release.get("manifest"),
        vpc_profile=selected_template_vpc_profile,
        template_envs=selected_template_envs,
        template_prefix=selected_template_prefix,
        template_domain=selected_template_domain,
        rancher_cluster=selected_template_rancher_cluster,
        rancher_context=selected_template_rancher_context,
    )
    effective_form_data = form_data or {"title": "", "spec_yaml": ""}
    if form_data is None and selected_template:
        effective_form_data = {
            "title": selected_template["draft_title"],
            "spec_yaml": selected_template["spec_yaml"],
        }
    return render_template(
        "spec_validator/index.html",
        drafts=drafts,
        core_validate=state,
        defaults=defaults,
        confluence=confluence,
        wizard_templates=wizard_templates,
        selected_template_key=selected_template_key,
        selected_template_pc_version=selected_template_pc_version,
        selected_template_vpc_profile=selected_template_vpc_profile,
        selected_template_envs=selected_template_envs,
        selected_template_env_csv=selected_template_env_csv,
        selected_template_env_summary=selected_template_env_summary,
        selected_template_prefix=selected_template_prefix,
        selected_template_prefix_suffix=selected_template_prefix_suffix,
        selected_template_prefix_placeholder=selected_template_prefix_placeholder,
        selected_template_domain=selected_template_domain,
        template_env_choices=UNIFIED_ENV_TYPES_CHOICES,
        template_domain_choices=[("", "Template placeholder"), *DOMAIN_CHOICES],
        template_vpc_profile_choices=[("", "Template default"), *VPC_AZ_PROFILE_CHOICES],
        selected_template_rancher_cluster=selected_template_rancher_cluster,
        template_rancher_cluster_choices=[("", "Template default"), *RANCHER_EKS_CLUSTERS],
        selected_template_rancher_context=selected_template_rancher_context,
        template_rancher_context_choices=[("", "Template placeholders"), *RANCHER_CONTEXT_CHOICES],
        template_release_customer=template_release.get("selected_customer") or "",
        template_release_customers=template_release.get("customer_choices") or [],
        template_release_key=template_release.get("selected_release_key") or "",
        template_release_choices=template_release.get("release_choices") or [],
        template_release_error=template_release.get("error") or "",
        template_release_source=template_release.get("source") or {"bucket": "", "prefix": ""},
        template_release_summary=template_release.get("summary"),
        template_options_ready=template_options_ready,
        release_examine_available="praxisrelease.examine_release" in current_app.view_functions,
        error=error,
        notice=notice,
        form_data=effective_form_data,
        format_ts=_format_ts,
    )


def _render_workspace(draft_id: str, *, error: str = "", notice: str = ""):
    owner = _current_owner()
    draft = get_draft(owner, draft_id)
    if not draft:
        return "draft not found", 404

    sync_draft_jobs(draft_id)
    validations = list_validations(draft_id)
    publications = list_publications(draft_id)
    state = feature_state()
    defaults = _defaults_from_state(state)
    validation_defaults = _workspace_validation_defaults(defaults, validations)
    draft_analysis = analyze_spec_yaml(str(draft.get("working_yaml") or ""))
    confluence = load_config().as_dict()
    matching_validation = latest_matching_successful_validation(
        draft_id,
        content_hash=str(draft.get("working_hash") or ""),
    )
    latest_success = latest_successful_validation(draft_id)
    latest_publication = publications[0] if publications else None
    local_spec_checks = _local_spec_checks(draft, draft_analysis)
    publish_blockers = _publish_blockers(
        confluence=confluence,
        matching_validation=matching_validation,
        latest_success=latest_success,
        draft=draft,
    )

    snapshot_comparisons: list[dict[str, Any]] = []
    current_yaml = str(draft.get("working_yaml") or "")
    if latest_success:
        snapshot_comparisons.append(
            _build_yaml_comparison(
                label="Last Successful Validation",
                compared_at=latest_success.get("created_at"),
                compare_yaml=str(latest_success.get("spec_yaml") or ""),
                current_yaml=current_yaml,
            )
        )
    if latest_publication:
        snapshot_comparisons.append(
            _build_yaml_comparison(
                label="Last Published Revision",
                compared_at=latest_publication.get("created_at"),
                compare_yaml=str(latest_publication.get("spec_yaml") or ""),
                current_yaml=current_yaml,
            )
        )
    baseline_yaml = str(draft.get("baseline_yaml") or "")
    template_comparison = None
    if baseline_yaml:
        template_comparison = _build_yaml_comparison(
            label=(f"{draft.get('source_template_key')} starter" if draft.get("source_template_key") else "Initial draft"),
            compared_at=draft.get("created_at"),
            compare_yaml=baseline_yaml,
            current_yaml=current_yaml,
        )
        snapshot_comparisons.insert(0, template_comparison)
    section_guidance = [
        {"key": key, "title": _SECTION_GUIDANCE[key][0], "description": _SECTION_GUIDANCE[key][1]}
        for key in draft_analysis.get("top_level_keys", [])
        if key in _SECTION_GUIDANCE
    ]

    return render_template(
        "spec_validator/workspace.html",
        draft=draft,
        draft_analysis=draft_analysis,
        validations=validations,
        publications=publications,
        core_validate=state,
        validation_defaults=validation_defaults,
        confluence=confluence,
        matching_validation=matching_validation,
        publish_ready=matching_validation is not None and confluence["enabled"] and not publish_blockers,
        publish_reason=publish_blockers[0] if publish_blockers else "",
        publish_blockers=publish_blockers,
        latest_success=latest_success,
        latest_publication=latest_publication,
        snapshot_comparisons=snapshot_comparisons,
        template_comparison=template_comparison,
        local_spec_checks=local_spec_checks,
        section_guidance=section_guidance,
        architecture_metadata_complete=not _prerequisite_metadata_blockers(draft),
        error=error,
        notice=notice,
        is_prod=bool(current_app.config.get("RUNTIME_ENV_IS_PROD")),
        format_ts=_format_ts,
    )


def _start_validation_for_draft(
    *,
    owner: str,
    draft: dict[str, Any],
    harbor_registry_project: str,
    pc_version: str,
) -> str:
    job = start_validation_job(
        env_slug=_ENV_SLUG,
        spec_yaml=str(draft.get("working_yaml") or ""),
        harbor_registry_project=harbor_registry_project,
        pc_version=pc_version,
        validation_mode="offline",
    )
    job_id = str(job.get("job_id") or "").strip()
    if not _JOB_ID_RE.fullmatch(job_id):
        raise RuntimeError("Validation job id was missing from Praxis Core response")
    create_validation(
        owner=owner,
        draft_id=str(draft.get("id") or ""),
        spec_yaml=str(draft.get("working_yaml") or ""),
        harbor_registry_project=harbor_registry_project,
        pc_version=pc_version,
        job_id=job_id,
        state=str(job.get("state") or "queued"),
    )
    return job_id


@bp.get("/")
def index():
    return _render_index(
        error=(request.args.get("error") or "").strip(),
        notice=(request.args.get("notice") or "").strip(),
    )


@bp.post("/drafts")
def create_draft_route():
    try:
        spec_yaml, uploaded_name = _spec_text_from_request(required=True)
    except ValueError as exc:
        return _render_index(
            error=str(exc),
            form_data={
                "title": (request.form.get("title") or "").strip(),
                "spec_yaml": (request.form.get("spec_yaml") or "").replace("\r\n", "\n"),
            },
        )

    title = derive_title(
        title=(request.form.get("title") or "").strip(),
        spec_yaml=spec_yaml,
        uploaded_filename=uploaded_name,
    )
    mode, metadata = _draft_profile_from_request()
    draft = create_draft(
        owner=_current_owner(),
        title=title,
        spec_yaml=spec_yaml,
        mode=mode or "playground",
        architecture_metadata=metadata,
        source_template_key=(request.form.get("source_template_key") or "").strip(),
    )
    return redirect(
        url_for(
            "spec_validator.draft_page",
            draft_id=draft["id"],
            notice="Draft created.",
        )
    )


@bp.post("/drafts/<draft_id>/delete")
def delete_draft_route(draft_id: str):
    owner = _current_owner()
    draft = get_draft(owner, draft_id)
    if not draft:
        return redirect(url_for("spec_validator.index", error="Draft not found."))

    deleted = delete_draft(owner=owner, draft_id=draft_id)
    if not deleted:
        return redirect(url_for("spec_validator.index", error="Draft not found."))

    notice = "Deleted draft. Confluence pages were not removed."
    return redirect(url_for("spec_validator.index", notice=notice))


@bp.post("/drafts/<draft_id>/fork")
def fork_draft_route(draft_id: str):
    owner = _current_owner()
    source = get_draft(owner, draft_id)
    if not source:
        return "draft not found", 404
    source_yaml = str(source.get("working_yaml") or "")
    metadata = dict(source.get("architecture_metadata") or {})
    metadata["status"] = "POC"
    fork = create_draft(
        owner=owner,
        title=f"{source.get('title') or 'Untitled Spec'} - Fork",
        spec_yaml=source_yaml,
        mode=str(source.get("mode") or "playground"),
        architecture_metadata=metadata,
        source_template_key=str(source.get("source_template_key") or ""),
        source_draft_id=draft_id,
        baseline_yaml=source_yaml,
    )
    return redirect(url_for("spec_validator.draft_page", draft_id=fork["id"], notice="Fork created with source traceability."))


@bp.get("/drafts/<draft_id>")
def draft_page(draft_id: str):
    return _render_workspace(
        draft_id,
        error=(request.args.get("error") or "").strip(),
        notice=(request.args.get("notice") or "").strip(),
    )


@bp.post("/drafts/<draft_id>/save")
def save_draft(draft_id: str):
    owner = _current_owner()
    existing = get_draft(owner, draft_id)
    if not existing:
        return "draft not found", 404

    try:
        spec_yaml, uploaded_name = _spec_text_from_request(required=True)
    except ValueError as exc:
        return _render_workspace(draft_id, error=str(exc))

    title = derive_title(
        title=(request.form.get("title") or "").strip(),
        spec_yaml=spec_yaml,
        uploaded_filename=uploaded_name,
    )
    mode, metadata = _draft_profile_from_request()
    update_draft(owner=owner, draft_id=draft_id, title=title, spec_yaml=spec_yaml, mode=mode, architecture_metadata=metadata)
    return redirect(
        url_for("spec_validator.draft_page", draft_id=draft_id, notice="Draft saved.")
    )


@bp.post("/drafts/<draft_id>/check-yaml")
def check_yaml_draft(draft_id: str):
    owner = _current_owner()
    existing = get_draft(owner, draft_id)
    if not existing:
        return "draft not found", 404

    try:
        spec_yaml, uploaded_name = _spec_text_from_request(required=True)
    except ValueError as exc:
        return _render_workspace(draft_id, error=str(exc))

    title = derive_title(
        title=(request.form.get("title") or "").strip(),
        spec_yaml=spec_yaml,
        uploaded_filename=uploaded_name,
    )
    mode, metadata = _draft_profile_from_request()
    draft = update_draft(owner=owner, draft_id=draft_id, title=title, spec_yaml=spec_yaml, mode=mode, architecture_metadata=metadata)
    if not draft:
        return "draft not found", 404

    analysis = analyze_spec_yaml(str(draft.get("working_yaml") or ""))
    if analysis.get("parsed_ok"):
        return redirect(
            url_for(
                "spec_validator.draft_page",
                draft_id=draft_id,
                notice="YAML parsed successfully.",
            )
        )

    return redirect(
        url_for(
            "spec_validator.draft_page",
            draft_id=draft_id,
            error=str(analysis.get("error") or "YAML parse error."),
        )
    )


@bp.post("/drafts/<draft_id>/validate")
def validate_draft(draft_id: str):
    owner = _current_owner()
    state = feature_state()
    defaults = _defaults_from_state(state)
    if not state.get("enabled"):
        reason = str(state.get("reason") or "Core validation is unavailable.").strip()
        return redirect(url_for("spec_validator.draft_page", draft_id=draft_id, error=reason))

    existing = get_draft(owner, draft_id)
    if not existing:
        return "draft not found", 404

    try:
        spec_yaml, uploaded_name = _spec_text_from_request(required=True)
    except ValueError as exc:
        return _render_workspace(draft_id, error=str(exc))

    title = derive_title(
        title=(request.form.get("title") or "").strip(),
        spec_yaml=spec_yaml,
        uploaded_filename=uploaded_name,
    )
    mode, metadata = _draft_profile_from_request()
    draft = update_draft(owner=owner, draft_id=draft_id, title=title, spec_yaml=spec_yaml, mode=mode, architecture_metadata=metadata)
    if not draft:
        return "draft not found", 404

    is_prod = bool(current_app.config.get("RUNTIME_ENV_IS_PROD"))
    harbor_registry_project = (
        request.form.get("harbor_registry_project") or defaults["harbor_registry_project"]
    ).strip()
    if is_prod:
        pc_version = defaults["pc_version"]
    else:
        pc_version = (request.form.get("pc_version") or defaults["pc_version"]).strip()

    try:
        job_id = _start_validation_for_draft(
            owner=owner,
            draft=draft,
            harbor_registry_project=harbor_registry_project,
            pc_version=pc_version,
        )
    except Exception as exc:
        return redirect(
            url_for(
                "spec_validator.draft_page",
                draft_id=draft_id,
                error=_validation_message(exc),
            )
        )

    return redirect(url_for("spec_validator.job_page", job_id=job_id))


@bp.post("/drafts/<draft_id>/publish")
def publish_draft(draft_id: str):
    owner = _current_owner()
    existing = get_draft(owner, draft_id)
    if not existing:
        return "draft not found", 404

    try:
        spec_yaml, uploaded_name = _spec_text_from_request(required=True)
    except ValueError as exc:
        return _render_workspace(draft_id, error=str(exc))

    title = derive_title(
        title=(request.form.get("title") or "").strip(),
        spec_yaml=spec_yaml,
        uploaded_filename=uploaded_name,
    )
    mode, metadata = _draft_profile_from_request()
    draft = update_draft(owner=owner, draft_id=draft_id, title=title, spec_yaml=spec_yaml, mode=mode, architecture_metadata=metadata)
    if not draft:
        return "draft not found", 404

    sync_draft_jobs(draft_id)
    validation = latest_matching_successful_validation(
        draft_id,
        content_hash=str(draft.get("working_hash") or ""),
    )
    if not validation:
        return redirect(
            url_for(
                "spec_validator.draft_page",
                draft_id=draft_id,
                error="Current draft does not match a successful validation.",
            )
        )

    blockers = _publish_blockers(
        confluence=load_config().as_dict(),
        matching_validation=validation,
        latest_success=validation,
        draft=draft,
    )
    if blockers:
        return redirect(url_for("spec_validator.draft_page", draft_id=draft_id, error=blockers[0]))

    revision_number = reserve_next_revision_number(draft_id=draft_id, owner=owner)
    try:
        publish_result = publish_revision(
            draft=draft,
            validation=validation,
            revision_number=revision_number,
            published_by=owner,
        )
    except ConfluenceError as exc:
        release_reserved_revision(draft_id=draft_id, revision_number=revision_number)
        return redirect(
            url_for("spec_validator.draft_page", draft_id=draft_id, error=str(exc))
        )

    try:
        record_publication(
            owner=owner,
            draft_id=draft_id,
            validation_id=int(validation["id"]),
            revision_number=revision_number,
            content_hash=str(validation.get("content_hash") or ""),
            spec_yaml=str(validation.get("spec_yaml") or ""),
            confluence_parent_page_id=str(publish_result["confluence_parent_page_id"]),
            confluence_parent_page_url=str(publish_result["confluence_parent_page_url"]),
            confluence_revision_page_id=str(publish_result["confluence_revision_page_id"]),
            confluence_revision_page_url=str(publish_result["confluence_revision_page_url"]),
            reservation_required=True,
        )
    except Exception:
        current_app.logger.exception(
            "Spec Workbench publication record failed after Confluence publish for draft %s",
            draft_id,
        )
        try:
            update_draft_confluence_parent(
                owner=owner,
                draft_id=draft_id,
                confluence_parent_page_id=str(publish_result["confluence_parent_page_id"]),
                confluence_parent_page_url=str(publish_result["confluence_parent_page_url"]),
            )
        except Exception:
            current_app.logger.exception(
                "Spec Workbench could not persist Confluence parent linkage after publication failure for draft %s",
                draft_id,
            )
        return redirect(
            url_for(
                "spec_validator.draft_page",
                draft_id=draft_id,
                error=(
                    "Confluence publish succeeded, but the local publication record could not be saved."
                ),
            )
        )
    return redirect(
        url_for(
            "spec_validator.draft_page",
            draft_id=draft_id,
            notice=f"Published Rev {revision_number:03d} to Confluence.",
        )
    )


@bp.post("/jobs")
def start_job():
    state = feature_state()
    defaults = _defaults_from_state(state)
    if not state.get("enabled"):
        reason = str(state.get("reason") or "Core validation is unavailable.").strip()
        return redirect(url_for("spec_validator.index", error=reason))

    try:
        spec_yaml, uploaded_name = _spec_text_from_request(required=True)
    except ValueError as exc:
        return redirect(url_for("spec_validator.index", error=str(exc)))

    title = derive_title(title="", spec_yaml=spec_yaml, uploaded_filename=uploaded_name)
    draft = create_draft(owner=_current_owner(), title=title, spec_yaml=spec_yaml)

    is_prod = bool(current_app.config.get("RUNTIME_ENV_IS_PROD"))
    harbor_registry_project = (
        request.form.get("harbor_registry_project") or defaults["harbor_registry_project"]
    ).strip()
    if is_prod:
        pc_version = defaults["pc_version"]
    else:
        pc_version = (request.form.get("pc_version") or defaults["pc_version"]).strip()

    try:
        job_id = _start_validation_for_draft(
            owner=_current_owner(),
            draft=draft,
            harbor_registry_project=harbor_registry_project,
            pc_version=pc_version,
        )
    except Exception as exc:
        return redirect(
            url_for(
                "spec_validator.draft_page",
                draft_id=draft["id"],
                error=_validation_message(exc),
            )
        )

    return redirect(url_for("spec_validator.job_page", job_id=job_id))


@bp.get("/jobs/<job_id>")
def job_page(job_id: str):
    if not _JOB_ID_RE.fullmatch((job_id or "").strip()):
        return "invalid job id", 400

    validation = get_validation_by_job(job_id)
    if validation and str(validation.get("owner") or "") != _current_owner():
        return "job not found", 404

    job = get_job(job_id)
    if not job:
        return "job not found", 404
    if str(job.get("env_slug") or _ENV_SLUG) != _ENV_SLUG:
        return "job not found", 404

    draft_url = url_for("spec_validator.index")
    back_label = "Back to Spec Workbench"
    source_label = "spec-workbench"
    if validation:
        draft_url = url_for("spec_validator.draft_page", draft_id=validation["draft_id"])
        back_label = "Back to Draft"
        draft = get_draft(_current_owner(), str(validation["draft_id"]))
        if draft:
            source_label = str(draft.get("title") or "spec-workbench")

    return render_template(
        "spec_validator/run.html",
        job=job,
        status_url=url_for("spec_validator.job_status", job_id=job_id),
        index_url=draft_url,
        back_label=back_label,
        source_label=source_label,
        has_draft=validation is not None,
    )


@bp.get("/jobs/<job_id>.json")
def job_status(job_id: str):
    if not _JOB_ID_RE.fullmatch((job_id or "").strip()):
        return {"ok": False, "error": "invalid job id"}, 400

    validation = get_validation_by_job(job_id)
    if validation and str(validation.get("owner") or "") != _current_owner():
        return {"ok": False, "error": "job not found"}, 404

    job = get_job(job_id)
    if not job:
        return {"ok": False, "error": "job not found"}, 404
    if str(job.get("env_slug") or _ENV_SLUG) != _ENV_SLUG:
        return {"ok": False, "error": "job not found"}, 404

    return job, 200
