"""
Pluggable module registration for the Artiac control page.
"""
from .blueprint import bp as artiac_bp
from .archive.blueprint import bp as archives_bp


class Plugin:
    title = "Artiac Operations"
    icon = "fa-solid fa-boxes-stacked"
    category = "Tools"
    group = "Artiac"
    description = "Explore Praxis Terraform/OpenTofu modules built by Artiac, monitor publishing health, and inspect versioned archives"
    home_endpoint = "artiac_control.index"


def register(app):
    """Called by the app's module loader. Registers the blueprint."""
    try:
        if artiac_bp.name not in app.blueprints:
            app.register_blueprint(artiac_bp)
        if archives_bp.name not in app.blueprints:
            app.register_blueprint(archives_bp)
        app.logger.info("Artiac Operations module registered successfully")

        return {
            "title": Plugin.title,
            "icon": Plugin.icon,
            "category": Plugin.category,
            "group": Plugin.group,
            "description": Plugin.description,
            "home_endpoint": Plugin.home_endpoint,
        }

    except Exception as exc:
        app.logger.warning("Artiac control module failed to register: %s", exc)
        return None
