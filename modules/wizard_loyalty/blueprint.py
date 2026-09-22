from flask import Blueprint, render_template, url_for, redirect

bp = Blueprint(
    "wizard_loyalty",
    __name__,
    url_prefix="/wizard/loyalty",
    template_folder="templates",
)


@bp.get("/")
def env_home():
    """Landing page for the loyalty wizard (frontpage card points here)."""
    # This now resolves to:
    # modules/wizard_loyalty/templates/wizard_loyalty/home.html
    return render_template("wizard_loyalty/home.html")


@bp.get("/start")
def start():
    """Forward into the REAL wizard engine (engine/wizards)."""
    return redirect(url_for("loyalty.env_home"))
