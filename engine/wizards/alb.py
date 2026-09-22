# envs/wizard/alb.py
from __future__ import annotations
from flask import request, redirect, url_for, session
from constants import DEFAULT_EXTERNAL_ALB_ALLOWED_IPS
from engine.wizards.forms.alb_form import ALB_GROUP_KEY

def _default_ips() -> list[str]:
    d = DEFAULT_EXTERNAL_ALB_ALLOWED_IPS
    return list(d.get(ALB_GROUP_KEY, [])) if isinstance(d, dict) else list(d or [])

def prefill_groups(form) -> None:
    """Seed one entry with helpful defaults on GET if empty (does not overwrite)."""
    groups = getattr(form, "groups", None)
    if groups is None:
        return
    if len(groups) == 0:
        groups.append_entry()
    for entry in groups:
        entry.form.group_key.data = ALB_GROUP_KEY
    if request.method == "POST":
        return
    g0 = groups[0].form
    if not (g0.description.data or "").strip():
        g0.description.data = "PRAXIS-EU"
    if not (g0.cidrs.data or "").strip():
        g0.cidrs.data = "\n".join(_default_ips())

def _sk(env_slug: str, key: str) -> str:
    return f"{env_slug}:{key}"

def _load_groups_from_session(env_slug: str, key: str):
    data = session.get(_sk(env_slug, key)) or {}
    groups = list(data.get("groups") or [])
    return data, groups

def _save_groups_to_session(env_slug: str, key: str, data: dict, groups: list[dict]) -> None:
    data = dict(data or {})
    data["groups"] = groups
    session[_sk(env_slug, key)] = data
    session.modified = True

def post_actions(env_slug: str, wizard_steps: list, idx: int, key: str, form):
    if key != "alb" or request.method != "POST":
        return None

    # We support three patterns:
    #  1) name="action" value="add_group" / "remove_group:<i>"
    #  2) name="add_group" (no value)
    #  3) name="remove_group" value="<i>"
    action = request.form.get("action", "")

    # Capture current form state so we don't lose edits when adding/removing.
    current_groups = []
    for entry in getattr(form, "groups", []) or []:
        current_groups.append({
            "group_key": ALB_GROUP_KEY,
            "description": entry.form.description.data or "",
            "cidrs": entry.form.cidrs.data or "",
        })

    # --- ADD ---
    if action == "add_group" or ("add_group" in request.form):
        data, _ = _load_groups_from_session(env_slug, key)
        groups = list(current_groups)
        groups.append({"group_key": ALB_GROUP_KEY, "description": "", "cidrs": ""})
        _save_groups_to_session(env_slug, key, data, groups)
        return redirect(url_for(f"{env_slug}_wizard.wizard_step", step=wizard_steps[idx][0]))

    # --- REMOVE (pattern 3) ---
    if "remove_group" in request.form:
        try:
            i = int(request.form.get("remove_group"))
        except Exception:
            i = -1
        data, _ = _load_groups_from_session(env_slug, key)
        groups = list(current_groups)
        if 0 < i < len(groups):  # never remove index 0
            groups.pop(i)
            _save_groups_to_session(env_slug, key, data, groups)
        return redirect(url_for(f"{env_slug}_wizard.wizard_step", step=wizard_steps[idx][0]))

    # --- REMOVE (pattern 1) ---
    if action.startswith("remove_group:"):
        try:
            i = int(action.split(":", 1)[1])
        except Exception:
            i = -1
        data, _ = _load_groups_from_session(env_slug, key)
        groups = list(current_groups)
        if 0 < i < len(groups):  # never remove index 0
            groups.pop(i)
            _save_groups_to_session(env_slug, key, data, groups)
        return redirect(url_for(f"{env_slug}_wizard.wizard_step", step=wizard_steps[idx][0]))

    return None

def extract_groups(section: dict) -> tuple[dict, list]:
    """
    Convert alb.groups -> top-level external_alb_allowed_ips list your finish step expects.
    Leaves 'alb' (without 'groups') in place.
    """
    if not isinstance(section, dict):
        return section, []
    alb_clean = dict(section)
    groups = alb_clean.pop("groups", []) or []
    out = []
    for g in groups:
        if not isinstance(g, dict):
            continue
        desc = (g.get("description") or "").strip()
        cidrs_raw = g.get("cidrs") or ""
        ips = [p.strip() for p in (cidrs_raw or "").replace(",", "\n").splitlines() if p.strip()]
        if not desc or not ips:
            continue
        out.append({ALB_GROUP_KEY: ips, "description": desc})
    return alb_clean, out
