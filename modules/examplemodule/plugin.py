from .blueprint import bp


class Plugin:
    title = "Example Module"
    icon = "fa-solid fa-flask"
    category = "Examples"
    home_endpoint = "examplemodule.home"
    suffix = "example"
    description = "Demonstration of a module with WTForms."


def register(app):
    """Register blueprint and expose plugin metadata (template for new modules)."""
    if bp.name not in app.blueprints:
        app.register_blueprint(bp)

    plugin_dict = {
        "title": Plugin.title,
        "icon": Plugin.icon,
        "category": Plugin.category,
        "description": Plugin.description,
        "home_endpoint": Plugin.home_endpoint,
        "suffix": Plugin.suffix,
    }

    app.extensions.setdefault("plugins", {})
    app.extensions["plugins"]["examplemodule"] = plugin_dict

    app.logger.info("examplemodule: registered blueprint and plugin metadata")
    return plugin_dict
