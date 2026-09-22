from flask import Blueprint
import os
from engine.wizards.plugin import register_wizard_step

# Module metadata
TITLE = "PC Source"
CATEGORY = "Wizards"
ICON = "fa-solid fa-server"
DESCRIPTION = "PC Source step for Unified Wizard"
SUFFIX = None

def register(app):
    """
    Register blueprint for template resolution.
    Ensures pc_source.html overrides wizard step template.
    """
    bp = Blueprint(
        "pc_source_ext",
        __name__,
        template_folder="templates/pc_source",   # <-- FIXED
        static_folder=None,
    )

    app.register_blueprint(bp)

    # Ensure Jinja loader knows about this directory
    template_path = os.path.join(os.path.dirname(__file__), "templates", "pc_source")
    if template_path not in app.jinja_loader.searchpath:
        app.jinja_loader.searchpath.append(template_path)
        app.logger.info(f"[pc_source] Added template path: {template_path}")

    # Register template so resolve_template() picks it up for the step key.
    register_wizard_step(
        key="pc_source",
        form_class="modules.wizard_ext.pc_source.forms.PCSourceForm",
        template="pc_source/pc_source.html",
    )

    app.logger.info("Registered module 'pc_source_ext' via register(app)")
    return bp
