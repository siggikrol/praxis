from flask import Blueprint, render_template, url_for, redirect

bp = Blueprint(
    "wizard_rgs",
    __name__,
    url_prefix="/wizard/rgs",
    template_folder="templates",
)


@bp.get("/")
def env_home():
    """Landing page for the rgs wizard (frontpage card points here)."""
    # This now resolves to:
    # modules/wizard_rgs/templates/wizard_rgs/home.html
    return render_template("wizard_rgs/home.html")


@bp.get("/start")
def start():
    """Forward into the REAL wizard engine (engine/wizards)."""
    return redirect(url_for("rgs.env_home"))
