# modules/wizard_multi_vpc/plugin.py

from engine.wizards.multi_vpc.multi_vpc import bp as engine_bp, wizard_bp
from .blueprint import bp as ui_bp

class Plugin:
    title = "Multi-VPC"
    icon = "fa-solid fa-sitemap"
    category = "Wizards"
    home_endpoint = "multi-vpc.env_home"      
    description = "Isolated networks for an AWS environment"
    suffix = "ctlst"                      


def register(app):
    """
    Register engine + wizard + UI blueprints for the MULTI-VPC wizard.
    """

    # 1) Engine blueprint (release page + core wizard)
    if engine_bp.name not in app.blueprints:
        app.register_blueprint(engine_bp)

    # 2) WIZARD blueprint (creates /multi-vpc/wizard/... routes)
    # create_wizard_blueprint("multi-vpc") always names it "multi-vpc_wizard"
    if "multi-vpc_wizard" not in app.blueprints:
        app.register_blueprint(wizard_bp)

    # 3) UI (landing) blueprint: /wizard/multi-vpc/
    if ui_bp.name not in app.blueprints:
        app.register_blueprint(ui_bp)

    # 4) Register plugin metadata (needed by wizard_step + sidebar)
    plugin_dict = {
        "title": Plugin.title,
        "icon": Plugin.icon,
        "category": Plugin.category,
        "description": Plugin.description,
        "home_endpoint": Plugin.home_endpoint,
        "suffix": Plugin.suffix,
    }

    app.extensions.setdefault("plugins", {})
    app.extensions["plugins"]["multi-vpc"] = plugin_dict

    return plugin_dict
