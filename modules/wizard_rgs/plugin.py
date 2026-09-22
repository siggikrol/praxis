# modules/wizard_rgs/plugin.py

from engine.wizards.rgs.rgs import bp as engine_bp, wizard_bp
from .blueprint import bp as ui_bp

class Plugin:
    title = "RGS"
    icon = "fa-solid fa-gamepad"
    category = "Wizards"
    home_endpoint = "rgs.env_home"
    description = "RGS environment setup"
    suffix = "rgs"

def register(app):
    """Register engine + wizard + UI blueprints for the RGS wizard."""

    # 1) Engine blueprint
    if engine_bp.name not in app.blueprints:
        app.register_blueprint(engine_bp)

    # 2) Wizard blueprint (IMPORTANT!)
    if "rgs_wizard" not in app.blueprints:
        app.register_blueprint(wizard_bp)

    # 3) UI blueprint
    if ui_bp.name not in app.blueprints:
        app.register_blueprint(ui_bp)

    # 4) Register plugin definition
    plugin_dict = {
        "title": Plugin.title,
        "icon": Plugin.icon,
       "category": Plugin.category,
        "description": Plugin.description,
        "home_endpoint": Plugin.home_endpoint,
        "suffix": Plugin.suffix,
    }

    app.extensions.setdefault("plugins", {})
    app.extensions["plugins"]["rgs"] = plugin_dict

    return plugin_dict
