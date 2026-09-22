# modules/wizard_unified/plugin.py

from engine.wizards.unified.unified import bp as engine_bp, wizard_bp
from .blueprint import bp as ui_bp


class Plugin:
    title = "Combined architecture"
    icon = "fa-solid fa-puzzle-piece"
    category = "Wizards"
    home_endpoint = "unified.env_home"
    description = "Unified wizard combining existing architecture and service configurations"
    suffix = "apps"


def register(app):
    """Register unified wizard blueprints and plugin metadata."""
    # Register UI landing blueprint
    app.register_blueprint(ui_bp)

    # Register engine entry blueprint (release selection, env_home)
    app.register_blueprint(engine_bp)

    # Register actual wizard flow blueprint
    app.register_blueprint(wizard_bp)

    # Register plugin metadata in registry
    plugin_dict = {
        "title": Plugin.title,
        "icon": Plugin.icon,
        "category": Plugin.category,
        "description": Plugin.description,
        "home_endpoint": Plugin.home_endpoint,
        "suffix": Plugin.suffix,
    }

    app.extensions.setdefault("plugins", {})
    app.extensions["plugins"]["unified"] = plugin_dict

    return plugin_dict
