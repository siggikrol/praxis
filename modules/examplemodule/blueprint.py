from flask import Blueprint, render_template, redirect, url_for, flash
from .forms import ExampleForm
from .service import process_form_data

bp = Blueprint(
    "examplemodule",
    __name__,
    url_prefix="/examplemodule",
    template_folder="templates/examplemodule",
)


@bp.get("/")
def home():
    """Landing page."""
    return render_template("home.html")


@bp.route("/form", methods=["GET", "POST"])
def form():
    """Example form handler."""
    form = ExampleForm()
    if form.validate_on_submit():
        result = process_form_data(
            form.name.data,
            form.count.data,
            form.mode.data,
        )
        flash("Form submitted successfully.", "success")
        return render_template(
            "form.html",
            form=form,
            result=result,
        )
    return render_template("form.html", form=form, result=None)
