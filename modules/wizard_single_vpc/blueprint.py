from flask import Blueprint, render_template, url_for, redirect

bp = Blueprint(
    "wizard_single_vpc",
    __name__,
    url_prefix="/wizard/single-vpc",
    template_folder="templates",
)


@bp.get("/")
def env_home():
    """Landing page for the Single-VPC wizard (frontpage card points here)."""
    # This now resolves to:
    # modules/wizard_single_vpc/templates/wizard_single_vpc/home.html
    return render_template("wizard_single_vpc/home.html")


@bp.get("/start")
def start():
    """Forward into the REAL wizard engine (engine/wizards)."""
    return redirect(url_for("single-vpc.env_home"))
