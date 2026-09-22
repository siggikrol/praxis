from .blueprint import bp


class Plugin:
    title = "Spec Workbench"
    icon = "fa-solid fa-vial-circle-check"
    category = "Tools"
    group = "Validation"
    description = "Create draft specs, validate exact revisions, and publish approved revisions to Confluence"
    home_endpoint = "spec_validator.index"


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
