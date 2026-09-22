from flask import request, url_for, current_app, session
from engine.wizards.presentation import STAGES, LABELS, stage_for, environment_review
from .utils import _get_step_index

# Keep this for parity with the old factory; adjust if you want to hide steps.
EXCLUDE_FROM_PROGRESS: set[str] = set()


def _friendly_step_label(step_slug: str) -> str:
    if step_slug in LABELS:
        return LABELS[step_slug]
    parts = [part for part in str(step_slug or "").split("_") if part]
    if not parts:
        return "Step"

    special = {
        "alb": "ALB",
        "eks": "EKS",
        "opensearch": "OpenSearch",
        "pc": "PC",
        "rgs": "RGS",
        "vpc": "VPC",
    }
    return " ".join(special.get(part, part.capitalize()) for part in parts)


def inject_wizard_progress(env_slug: str, wizard_steps: list[tuple[str, object]]) -> dict:
    """
    Injects wizard navigation context for templates (Next/Prev + step list).
    Auto-detects nested or flat blueprint endpoint naming.
    """
    steps = [slug for slug, _ in wizard_steps if slug not in EXCLUDE_FROM_PROGRESS]
    step_labels = [_friendly_step_label(slug) for slug in steps]

    _path = (request.path or "").rstrip("/")
    _seg = _path.split("/")[-1] if _path else ""
    current = (request.view_args or {}).get("step") or _seg

    try:
        idx = steps.index(current)
    except ValueError:
        idx = -1

    # Detect correct endpoint naming (handles nested blueprints)
    endpoint_base = f"{env_slug}_wizard.wizard_step"


    # Build navigation URLs safely
    step_urls = [url_for(endpoint_base, step=slug) for slug in steps]
    prev_url = url_for(endpoint_base, step=steps[idx - 1]) if idx > 0 else None
    next_url = (
        url_for(endpoint_base, step=steps[idx + 1])
        if 0 <= idx < len(steps) - 1
        else None
    )

    # Try to build the home URL for this environment
    home_url = None
    try:
        home_url = url_for(f"{env_slug}.env_home")
    except Exception as e:
        current_app.logger.warning(f"inject_wizard_progress: could not resolve home URL for {env_slug}: {e}")

    release_change_url = url_for(
        f"{env_slug}.release_selection",
        edit="1",
        return_step=current if current in steps else steps[0],
    )

    stage_links = []
    for stage in STAGES:
        members = [(slug, label, url) for slug, label, url in zip(steps, step_labels, step_urls) if stage_for(slug) == stage]
        target = url_for(f"{env_slug}_wizard.wizard_finish") if stage == "Review" else (members[0][2] if members else None)
        stage_links.append({"name": stage, "url": target, "members": members})
    return {
        "praxis_stages": stage_links,
        "praxis_stage": stage_for(current),
        "praxis_step_title": _friendly_step_label(current),
        "environment_review": environment_review(env_slug, session),
        # Used by wizard_step.html
        "progress_steps": steps,
        "progress_step_labels": step_labels,
        "progress_step_urls": step_urls,
        "progress_current": current,
        "progress_index": idx,
        "prev_url": prev_url,
        "next_url": next_url,
        "home_url": home_url,
        "release_change_url": release_change_url,
        # Utility for programmatic index lookup
        "get_step_index": lambda s: _get_step_index(wizard_steps, s),
    }
