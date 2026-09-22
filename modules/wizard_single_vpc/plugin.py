# modules/wizard_single_vpc/plugin.py

from engine.wizards.single_vpc.single_vpc import bp as engine_bp, wizard_bp
from .blueprint import bp as ui_bp


class Plugin:
    title = "Catalyst Single-VPC"
    icon = "fa-solid fa-diagram-project"
    category = "Wizards"
    home_endpoint = "single-vpc.env_home"   # NOTE: NOT wizard_single_vpc.env_home
    description = "Single VPC Catalyst wizard"
    suffix = "ctlst"


def register(app):
    """Register engine + wizard + UI blueprints for the Single-VPC wizard."""

    # 1) Engine blueprint: /single-vpc/, /single-vpc/release, /single-vpc/wizard/...
    if engine_bp.name not in app.blueprints:
        app.register_blueprint(engine_bp)

    # 2) Wizard blueprint: /single-vpc/wizard/<step>
    if "single-vpc_wizard" not in app.blueprints:
        app.register_blueprint(wizard_bp)

    # 3) UI landing page: /wizard/single-vpc/
    if ui_bp.name not in app.blueprints:
        app.register_blueprint(ui_bp)

    # 4) REGISTER PLUGIN in registry
    plugin_dict = {
        "title": Plugin.title,
        "icon": Plugin.icon,
        "category": Plugin.category,
        "description": Plugin.description,
        "home_endpoint": Plugin.home_endpoint,
        "suffix": Plugin.suffix,
    }

    app.extensions.setdefault("plugins", {})
    app.extensions["plugins"]["single-vpc"] = plugin_dict

    return plugin_dict
