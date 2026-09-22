# modules/wizard_unified/blueprint.py

from flask import Blueprint, render_template, url_for, redirect

bp = Blueprint(
    "wizard_unified",
    __name__,
    url_prefix="/wizard/unified",
    template_folder="templates",
)


@bp.get("/")
def env_home():
    """Landing page for the Unified wizard (frontpage card points here)."""
    return render_template("wizard_unified/home.html")


@bp.get("/start")
def start():
    """Forward into the unified wizard engine."""
    return redirect(url_for("unified.env_home"))
