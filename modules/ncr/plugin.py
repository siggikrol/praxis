from .blueprint import bp as ncr_bp

class Plugin:
    title = "Network Change Request"
    icon = "fa-solid fa-network-wired"
    category = "Tools"
    description = "Generate NCR documents (Ingress + Egress)"
    home_endpoint = "ncr.ingress"   # first page

def register(app):
    if ncr_bp.name not in app.blueprints:
        app.register_blueprint(ncr_bp)

    return {
        "title": Plugin.title,
        "icon": Plugin.icon,
        "category": Plugin.category,
        "description": Plugin.description,
        "home_endpoint": Plugin.home_endpoint,
    }
