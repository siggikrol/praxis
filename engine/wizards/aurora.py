from __future__ import annotations

import re

from flask import request, redirect, url_for, session

from engine.wizards.constants.aurora_constants import (
    AURORA_DEFAULTS,
    get_aurora_engine_profile,
)
from engine.wizards.forms.spacelift_settings import get_aws_integration_account_id
from engine.wizards.factory.utils import _key
from engine.wizards.utils import prune
from services.spacelift_api import list_aws_integrations


def _sk(env_slug: str, key: str) -> str:
    return f"{env_slug}:{key}"


def _selected_env_types(env_slug: str) -> list[str]:
    proj_cfg = session.get(_key(env_slug, "project_settings"), {}) or {}
    raw = proj_cfg.get("environment_type") or []
    if isinstance(raw, str):
        return [raw.strip()] if raw.strip() else []
    out: list[str] = []
    if isinstance(raw, (list, tuple)):
        for item in raw:
            value = str(item or "").strip()
            if value:
                out.append(value)
    return out


def _selected_spacelift_integration(env_slug: str) -> tuple[str, str]:
    cfg = session.get(_key(env_slug, "spacelift_settings"), {}) or {}
    if not isinstance(cfg, dict):
        return "", ""

    env_types = _selected_env_types(env_slug)
    candidate_envs = list(env_types)
    for env_name in cfg.keys():
        if isinstance(cfg.get(env_name), dict) and env_name not in candidate_envs:
            candidate_envs.append(env_name)

    for env_name in candidate_envs:
        env_map = cfg.get(env_name) or {}
        if not isinstance(env_map, dict):
            continue
        integration_id = str(env_map.get("aws_integration_id") or "").strip()
        if integration_id:
            return integration_id, env_name
    return "", (candidate_envs[0] if candidate_envs else "")


def _integration_account_id(integration_id: str) -> str:
    integration_id = (integration_id or "").strip()
    if not integration_id:
        return ""

    # Prefer account ID already resolved by Spacelift settings cache/options.
    cached = get_aws_integration_account_id(integration_id)
    if cached:
        return cached

    # Fallback: live lookup from Spacelift API.
    try:
        items = list_aws_integrations()
    except Exception:
        return ""
    for item in items:
        if not isinstance(item, dict):
            continue
        if str(item.get("id") or "").strip() != integration_id:
            continue
        role_arn = str(item.get("roleArn") or "").strip()
        match = re.match(r"^arn:aws:iam::(\d{12}):role/.+$", role_arn)
        if match:
            return match.group(1)
    return ""


def _normalize_slug(value: str) -> str:
    out = re.sub(r"[^a-z0-9-]+", "-", (value or "").strip().lower())
    out = re.sub(r"-{2,}", "-", out).strip("-")
    return out


def _derive_param_group_family(engine: str, engine_version: str) -> str:
    eng = (engine or "").strip().lower()
    ver = (engine_version or "").strip().lower()

    profile = get_aurora_engine_profile(eng)
    fallback_family = str(profile.get("param_group_family") or "").strip()

    if eng == "aurora-postgresql":
        match = re.match(r"^(\d+)", ver)
        if match:
            return f"aurora-postgresql{match.group(1)}"
        return fallback_family

    if eng == "aurora-mysql":
        match = re.match(r"^(\d+)(?:\.(\d+))?", ver)
        if match:
            major = match.group(1)
            minor = match.group(2) or "0"
            return f"aurora-mysql{major}.{minor}"
        return fallback_family

    return fallback_family


def _resolve_ssa_prefix(env_slug: str) -> str:
    repl_cfg = session.get(_key(env_slug, "replacements"), {}) or {}
    prefix = _normalize_slug(str(repl_cfg.get("ssa_prefix") or ""))
    if prefix:
        return prefix

    repo_cfg = session.get(_key(env_slug, "repository_settings"), {}) or {}
    prefix = _normalize_slug(str(repo_cfg.get("new_prefix") or ""))
    if prefix:
        return prefix

    common_cfg = session.get(_key(env_slug, "common"), {}) or {}
    return _normalize_slug(str(common_cfg.get("environment") or ""))


def _default_mysql_s3_role_arn(env_slug: str) -> str:
    integration_id, env_name = _selected_spacelift_integration(env_slug)
    account_id = _integration_account_id(integration_id)

    if not env_name:
        env_types = _selected_env_types(env_slug)
        env_name = env_types[0] if env_types else ""

    env_token = _normalize_slug(env_name)
    ssa_prefix = _resolve_ssa_prefix(env_slug)
    account_token = account_id or "<account_id>"
    prefix_token = ssa_prefix or "<ssa-prefix>"
    env_part = env_token or "<env>"
    return f"arn:aws:iam::{account_token}:role/{prefix_token}-{env_part}-rds_s3_role"


def _apply_mysql_dynamic_defaults(env_slug: str, parameters: list[dict]) -> list[dict]:
    role_arn = _default_mysql_s3_role_arn(env_slug)
    out: list[dict] = []
    for item in parameters or []:
        row = dict(item or {})
        if str(row.get("name") or "").strip() == "aws_default_s3_role":
            row["value"] = role_arn
        out.append(row)
    return out


def _is_role_placeholder(value: str) -> bool:
    text = (value or "").strip()
    if not text:
        return True
    return (
        "<account_id>" in text
        or "<ssa-prefix>" in text
        or "<env>" in text
    )


def _apply_mysql_dynamic_placeholders_only(env_slug: str, parameters: list[dict]) -> list[dict]:
    """
    Replace aws_default_s3_role only when it still contains template placeholders.
    Keeps user-entered custom role ARNs intact.
    """
    role_arn = _default_mysql_s3_role_arn(env_slug)
    out: list[dict] = []
    for item in parameters or []:
        row = dict(item or {})
        if str(row.get("name") or "").strip() == "aws_default_s3_role" and _is_role_placeholder(str(row.get("value") or "")):
            row["value"] = role_arn
        out.append(row)
    return out


def prefill_form_defaults(env_slug: str, form) -> None:
    """
    Update Aurora form values during normal render/submit flow so mysql role ARN
    is resolved without requiring the explicit "apply engine defaults" action.
    """
    try:
        engine = str(getattr(form, "engine").data or "").strip().lower()
        engine_version = str(getattr(form, "engine_version").data or "").strip()
    except Exception:
        return

    family_field = getattr(form, "db_cluster_parameter_group_family", None)
    if family_field is not None:
        derived_family = _derive_param_group_family(engine, engine_version)
        if derived_family:
            family_field.data = derived_family

    if engine != "aurora-mysql":
        return
    params_field = getattr(form, "db_cluster_parameter_group_parameters", None)
    entries = getattr(params_field, "entries", None) if params_field is not None else None
    if not entries:
        return

    role_arn = _default_mysql_s3_role_arn(env_slug)
    for entry in entries:
        sub = getattr(entry, "form", None)
        if not sub:
            continue
        name_field = getattr(sub, "name", None)
        value_field = getattr(sub, "value", None)
        name = (name_field.data or "").strip() if name_field else ""
        current = (value_field.data or "").strip() if value_field else ""
        if name == "aws_default_s3_role" and value_field is not None and _is_role_placeholder(current):
            value_field.data = role_arn


def normalize_payload(env_slug: str, payload: dict) -> dict:
    """
    Normalize Aurora payload before saving to session.
    Ensures param group family and mysql aws_default_s3_role are normalized on submit.
    """
    if not isinstance(payload, dict):
        return payload

    engine = str(payload.get("engine") or "").strip().lower()
    engine_version = str(payload.get("engine_version") or "").strip()
    out = dict(payload)

    derived_family = _derive_param_group_family(engine, engine_version)
    if derived_family:
        out["db_cluster_parameter_group_family"] = derived_family

    if engine != "aurora-mysql":
        return out

    params = payload.get("db_cluster_parameter_group_parameters")
    if not isinstance(params, list):
        return out

    out["db_cluster_parameter_group_parameters"] = _apply_mysql_dynamic_placeholders_only(env_slug, params)
    return out


def post_actions(env_slug: str, wizard_steps: list, idx: int, key: str, form):
    if key != "aurora" or request.method != "POST":
        return None

    action = (request.form.get("action") or "").strip().lower()
    if action != "apply_engine_defaults":
        return None

    data = prune(form.data)
    selected_engine = (data.get("engine") or request.form.get("engine") or AURORA_DEFAULTS["engine"]).strip()
    profile = get_aurora_engine_profile(selected_engine)
    parameters = [dict(item) for item in profile["parameters"]]
    if profile["engine"] == "aurora-mysql":
        parameters = _apply_mysql_dynamic_defaults(env_slug, parameters)

    data["engine"] = profile["engine"]
    data["engine_version"] = profile["engine_version"]
    data["db_cluster_parameter_group_family"] = profile["param_group_family"]
    data["enabled_cloudwatch_logs_exports"] = profile["enabled_logs_exports"]
    data["db_cluster_parameter_group_parameters"] = parameters

    session[_sk(env_slug, key)] = data
    session.modified = True

    return redirect(url_for(f"{env_slug}_wizard.wizard_step", step=wizard_steps[idx][0]))
