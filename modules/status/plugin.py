"""
Pluggable module registration for the Status page.
Kept minimal and generic to avoid coupling with the rest of the app.
"""
from .blueprint import bp as status_bp

class Plugin:
    title = "Status & Diagnostics"
    icon = "fa-solid fa-gauge-high"
    category = "Tools"
    group = "Status & Diagnostics"
    description = "Operational health, cache diagnostics, and runtime status"
    home_endpoint = "status.index"


def register(app):
    """Called by the app's module loader. Registers the blueprint."""
    try:
        app.register_blueprint(status_bp)
        app.logger.info("Status module registered successfully")

        return {
            "title": Plugin.title,
            "icon": Plugin.icon,
            "category": Plugin.category,
            "group": Plugin.group,
            "description": Plugin.description,
            "home_endpoint": Plugin.home_endpoint,
        }

    except Exception as exc:
        app.logger.warning("Status module failed to register: %s", exc)
        return None
