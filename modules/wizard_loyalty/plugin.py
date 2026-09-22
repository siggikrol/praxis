from engine.wizards.loyalty.loyalty import bp as engine_bp, wizard_bp
from .blueprint import bp as ui_bp

class Plugin:
    title = "Application services"
    icon = "fa-solid fa-heart"
    category = "Wizards"
    home_endpoint = "loyalty.env_home"
    description = "Application services environment setup"
    suffix = "playon"

def register(app):

    # --- engine blueprint (loyalty routes) ---
    if engine_bp.name not in app.blueprints:
        app.register_blueprint(engine_bp)

    # --- wizard blueprint (loyalty.wizard_step etc.) ---
    if wizard_bp.name not in app.blueprints:
        app.register_blueprint(wizard_bp)

    # --- UI blueprint (/wizard/loyalty home) ---
    if ui_bp.name not in app.blueprints:
        app.register_blueprint(ui_bp)

    # --- plugin registry entry ---
    plugin_dict = {
        "title": Plugin.title,
        "icon": Plugin.icon,
        "category": Plugin.category,
        "description": Plugin.description,
        "home_endpoint": Plugin.home_endpoint,
        "suffix": Plugin.suffix,
    }

    app.extensions.setdefault("plugins", {})
    app.extensions["plugins"]["loyalty"] = plugin_dict

    return plugin_dict
