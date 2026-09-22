# modules/releases_explorer/plugin.py
from .blueprint import bp


class Plugin:
    title = "Releases Explorer"
    icon = "fa-solid fa-box-archive"
    category = "Tools"
    group = "Releases"
    home_endpoint = "releases_explorer.home"
    description = "Browse, inspect and compare release archives"


def register(app):
    """Register blueprint and expose module metadata."""
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
