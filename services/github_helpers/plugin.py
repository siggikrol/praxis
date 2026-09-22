# modules/github_helpers/plugin.py
from flask import Flask
from services.github_helpers.blueprint import github_bp


def register(app: Flask):
    """
    Register ONE shared GitHub helper blueprint for all wizards.
    No per-env dynamic registration needed.
    """
    app.register_blueprint(github_bp)
    app.logger.info("GitHub Helpers: registered global 'github' blueprint")