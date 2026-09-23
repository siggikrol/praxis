from .blueprint import bp


def register(app):
    app.register_blueprint(bp)
    from .testing import start_reconciler
    start_reconciler()
    return {
        "title": "Terraform Module Builder", "icon": "fa-solid fa-cubes", "category": "Terraform",
        "home_endpoint": "terraform_module_builder.index",
        "description": "Browse the Terraform Registry, create wrappers, and adapt examples.",
    }
