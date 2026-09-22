from flask import Blueprint
import os
from jinja2 import FileSystemLoader, ChoiceLoader
from engine.wizards.plugin import register_wizard_step

TITLE = "Wizard Extensions"
CATEGORY = "Internal"
ICON = "fa-solid fa-puzzle-piece"
DESCRIPTION = "Extension components for Unified Wizard"
SUFFIX = None

def register(app):
    base_dir = os.path.dirname(__file__)

    # 1. Register lightweight blueprint
    bp = Blueprint(
        "wizard_ext",
        __name__,
        template_folder=None,   # extension modules supply their own template dirs
    )
    app.register_blueprint(bp)
    app.logger.info("wizard_ext: registered base blueprint")

    # 2. Add pc_source template directory to Jinja loader
    pc_source_templates = os.path.join(base_dir, "pc_source", "templates")
    if os.path.isdir(pc_source_templates):
        loader = FileSystemLoader(pc_source_templates)

        # Wrap existing choice loader correctly
        if isinstance(app.jinja_loader, ChoiceLoader):
            app.jinja_loader.loaders.insert(0, loader)
        else:
            # Flask sometimes uses a single loader before plugins modify it
            app.jinja_loader = ChoiceLoader([loader, app.jinja_loader])

        app.logger.info(f"wizard_ext: registered template path {pc_source_templates}")

        # Register pc_source step so templating picks the custom template
        register_wizard_step(
            "pc_source",
            "modules.wizard_ext.pc_source.forms.PCSourceForm",
            "pc_source/pc_source.html",
        )

    return bp
