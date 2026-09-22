import typing as t
import json
from flask import current_app, session


def _key(env_slug: str, prefix: str) -> str:
    """Build a session key namespaced by environment slug."""
    return f"{env_slug}:{prefix}"


def _template_exists(name: str) -> bool:
    """
    Check if a Jinja2 template exists anywhere in the app's search path.
    Works for blueprint-relative paths like 'praxisrelease/wizard.html'.
    """
    try:
        # Use Flask's Jinja loader (supports blueprint templates)
        current_app.jinja_env.get_or_select_template(name)
        return True
    except Exception:
        return False

def _get_step_index(wizard_steps: list[tuple[str, t.Any]], step: str) -> int:
    """Return the index of a step within wizard_steps."""
    for i, (k, _) in enumerate(wizard_steps):
        if k == step:
            return i
    return -1


def get_env_types_from_session(env_slug, default_envs=None):
    """Return selected environment types from session or defaults, including suffixes."""
    from engine.wizards.constants.project_constants import ENV_DEFAULT_ENV_TYPES
    if default_envs is None:
        default_envs = ENV_DEFAULT_ENV_TYPES
    proj_cfg = session.get(f"{env_slug}:project_settings", {}) or {}
    env_types = proj_cfg.get("environment_type", default_envs)

    # Process suffixes
    suffixes_raw = proj_cfg.get("environment_suffixes", "{}")
    try:
        suffixes = json.loads(suffixes_raw)
    except Exception:
        suffixes = {}

    result = []
    seen_bases = set()

    for env in env_types:
        if not env:
            continue
        base = str(env).strip().lower()
        if "-" in base:
            base = base.split("-")[0]
        
        if base in seen_bases:
            continue
        seen_bases.add(base)

        suffix = str(suffixes.get(base, "")).strip().lower()
        full_name = f"{base}-{suffix}" if suffix else base
        
        entry = {
            "base": base,
            "suffix": suffix,
            "full": full_name
        }
        result.append(entry)
    return result
