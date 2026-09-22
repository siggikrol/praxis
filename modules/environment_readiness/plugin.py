from .blueprint import bp


class Plugin:
    title = "Praxis Foundation Readiness"
    icon = "fa-solid fa-clipboard-check"
    category = "Tools"
    group = "Planning"
    description = "Confirm the prerequisites required for DevOps to start foundation provisioning with Praxis"
    home_endpoint = "environment_readiness.index"


def register(app):
    if bp.name not in app.blueprints:
        app.register_blueprint(bp)
    return {
        "title": Plugin.title,
        "icon": Plugin.icon,
        "category": Plugin.category,
        "group": Plugin.group,
        "description": Plugin.description,
        "home_endpoint": Plugin.home_endpoint,
    }
