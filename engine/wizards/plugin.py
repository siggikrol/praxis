# modules/wizards/plugin.py
"""
Wizard step registry and blueprint registration
Used both by the wizards module itself and external modules like praxisrelease.
"""

import importlib
from typing import Any, Dict

# Internal registry
_registry: Dict[str, Dict[str, str]] = {}


def register_wizard_step(step_key: str, form_class: str = "", template_path: str = "") -> None:
    _registry[step_key] = {"form_class": form_class, "template": template_path}


def get_wizard_step(step_key: str) -> Any:
    info = _registry.get(step_key)
    if not info:
        raise KeyError(f"Wizard step '{step_key}' not registered")
    module_path, class_name = info["form_class"].rsplit(".", 1)
    module = importlib.import_module(module_path)
    return getattr(module, class_name)


def get_wizard_template(step_key: str) -> str:
    info = _registry.get(step_key)
    if not info:
        raise KeyError(f"Wizard step '{step_key}' not registered")
    return info["template"]


# REMOVE ALL AUTO BLUEPRINT REGISTRATION
def register(app):
    """Engine initialisation only."""
    app.logger.info("Wizard engine initialised (no auto blueprints).")
