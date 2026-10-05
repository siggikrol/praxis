from .blueprint import bp
from .documentation_cache import start_documentation_cache_refresher


def register(app):
    app.register_blueprint(bp)
    start_documentation_cache_refresher(app)
    return {'title': 'Modules & Stacks', 'icon': 'fa-solid fa-layer-group', 'category': 'Terraform',
            'home_endpoint': 'terraform_stacks.index',
            'description': 'Register Git modules and configure versioned infrastructure stacks.'}
