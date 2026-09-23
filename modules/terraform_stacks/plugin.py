from .blueprint import bp


def register(app):
    app.register_blueprint(bp)
    return {'title': 'Modules & Stacks', 'icon': 'fa-solid fa-layer-group', 'category': 'Terraform',
            'home_endpoint': 'terraform_stacks.index',
            'description': 'Register Git modules and configure versioned infrastructure stacks.'}
