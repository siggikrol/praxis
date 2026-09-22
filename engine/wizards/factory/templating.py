# modules/wizards/factory/templating.py
from flask import current_app
from .utils import _template_exists


def resolve_template(key: str) -> str:
    """
    Resolve the most appropriate template for a wizard step.

    Order of resolution:
      1. Module-registered template (via register_wizard_step)
      2. Wizard-scoped candidate locations
      3. Default shared wizard template
    """

    # --- Step 1: try registry-based template (lazy import to avoid circular deps) ---
    try:
        from importlib import import_module
        plugin_mod = import_module("engine.wizards.plugin")
        get_wizard_template = getattr(plugin_mod, "get_wizard_template", None)
        tmpl = None

        if get_wizard_template:
            tmpl = get_wizard_template(key)
            current_app.logger.info("🧭 resolve_template(): registry[%s] = %s", key, tmpl)

            if tmpl and _template_exists(tmpl):
                current_app.logger.info("✅ resolve_template(): found existing template %s", tmpl)
                return tmpl
            else:
                current_app.logger.debug("resolve_template(): registry template %s not found in FS (normal fallback)", tmpl)
        else:
            current_app.logger.debug("resolve_template(): no registry template for %s (normal)", key)

    except Exception as e:
        current_app.logger.debug("resolve_template(): registry exception (fallback ok): %s", e)


    # --- Step 2: fallback candidates ---
    candidates = [
        f"wizards/steps/{key}.html",
        f"steps/{key}.html",
        "wizards/wizard_step.html",
        "wizard_step.html",
    ]

    for name in candidates:
        if _template_exists(name):
            current_app.logger.info("ℹ️ resolve_template(): using fallback %s", name)
            return name

    # --- Step 3: final fallback ---
    current_app.logger.warning("⚠️ resolve_template(): no template found; using wizard_step.html")
    return "wizard_step.html"
