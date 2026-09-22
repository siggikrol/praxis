"""
Singleton registry for wizard step plugins.
Used by dynamic modules like praxisrelease, iac, helm, etc.
Ensures consistent shared state across all imports.
"""

import importlib
import sys
from typing import Any, Dict

# ---------------------------------------------------------------------
# 🔁 Singleton enforcement
# ---------------------------------------------------------------------
if "engine.wizards.registry" in sys.modules:
    # Reuse existing instance if already imported under canonical name
    existing = sys.modules["engine.wizards.registry"]
    _registry = getattr(existing, "_registry", {})
else:
    _registry: Dict[str, Dict[str, str]] = {}

# Expose globally shared reference
sys.modules["engine.wizards.registry"] = sys.modules[__name__]


# ---------------------------------------------------------------------
# 🧱 Core registry functions
# ---------------------------------------------------------------------
def register_wizard_step(key: str, form_class: str, template: str) -> None:
    """Register a wizard step plugin."""
    _registry[key] = {"form_class": form_class, "template": template}


def get_wizard_step(key: str) -> Any:
    """Return the form class for a registered wizard step."""
    info = _registry.get(key)
    if not info:
        raise KeyError(f"Wizard step '{key}' not registered")
    module_path, class_name = info["form_class"].rsplit(".", 1)
    module = importlib.import_module(module_path)
    return getattr(module, class_name)


def get_wizard_template(key: str) -> str:
    """Return the Jinja2 template path for a registered wizard step."""
    info = _registry.get(key)
    if not info:
        raise KeyError(f"Wizard step '{key}' not registered")
    return info["template"]


# ---------------------------------------------------------------------
# 🧩 Optional: Debugging helper
# ---------------------------------------------------------------------
def list_registered_steps() -> Dict[str, Dict[str, str]]:
    """Return a copy of the current registry for diagnostics."""
    return dict(_registry)
