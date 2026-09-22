# modules/praxisrelease/plugin.py
from __future__ import annotations

from flask import (
    Blueprint, render_template, request, redirect, url_for, session, current_app
)
from modules.praxisrelease.forms import PraxisReleaseForm
from modules.praxisrelease.service import (
    fetch_release_manifest,
    get_release_source,
    BUCKET,
)
import yaml

bp = Blueprint(
    "praxisrelease",
    __name__,
    url_prefix="/praxisrelease",
    template_folder="templates",
)


def _find_wizard_step_endpoint(env_slug: str) -> str:
    """
    Always return the wizard_step endpoint for a blueprint named:
      {env_slug}_wizard
    This matches how create_wizard_blueprint() names it.
    """

    # 1) The blueprint for every wizard is ALWAYS <slug>_wizard
    bp_name = f"{env_slug}_wizard"

    # 2) The wizard step endpoint is ALWAYS <bp_name>.wizard_step
    endpoint = f"{bp_name}.wizard_step"

    # 3) Validate it actually exists
    if endpoint not in current_app.view_functions:
        raise RuntimeError(
            f"wizard_step endpoint not found: '{endpoint}' "
            f"(expected blueprint name '{bp_name}')"
        )

    return endpoint


# ---------------------------------------------------------------------------
# Release selection (main view)
# ---------------------------------------------------------------------------
@bp.route("/", methods=["GET", "POST"])
def release_home():
    """
    Main release selection screen.
    - POST with 'reload' -> repopulate releases for selected customer, re-render.
    - POST valid submit -> save selection, redirect to first wizard step.
    - GET -> render form with initial choices.
    """
    form = PraxisReleaseForm(
        wizard_slug=request.values.get("wizard_slug") or request.values.get("env_slug")
    )

    # 1) Handle explicit reload action
    if request.method == "POST" and "reload" in request.form:
        customer = request.form.get("customer") or ""
        form.customer.data = customer
        try:
            form.set_release_choices(customer)
        except Exception as e:
            current_app.logger.warning(
                f"Reload failed for customer={customer}: {e}"
            )
            form.release_key.choices = [("", f"Error: {e}")]
        else:
            # Keep the selected release in sync with the refreshed choices.
            release_values = [val for val, _ in form.release_key.choices]
            if form.release_key.data not in release_values and release_values:
                form.release_key.data = release_values[0]
        return render_template(
            "praxisrelease/wizard.html",
            form=form,
            release_source=get_release_source(),
        )

    # 2) Regular submit -> pick a wizard + jump
    if form.validate_on_submit():
        session["release_selection"] = {
            "customer": form.customer.data,
            "release_key": form.release_key.data,
        }
        current_app.logger.info(
            f"Saved release_selection → {session['release_selection']}"
        )

        # a) Respect explicit wizard_slug if provided
        env_slug = request.form.get("wizard_slug")

        # b) Otherwise auto-pick the first available wizard
        if not env_slug:
            for candidate in ["multi-vpc", "single-vpc", "rgs", "loyalty"]:
                try:
                    _ = _find_wizard_step_endpoint(candidate)
                    env_slug = candidate
                    break
                except RuntimeError:
                    continue

        if not env_slug:
            raise RuntimeError(
                "No wizard_step endpoints available "
                "(tried: multi-vpc, single-vpc, rgs, loyalty)"
            )

        endpoint = _find_wizard_step_endpoint(env_slug)
        return redirect(url_for(endpoint, step="project_settings"))

    # 3) GET or initial render: hydrate choices
    if not form.customer.choices:
        # __init__ already pulls customers; nothing to do here
        pass

    customer = form.customer.data or (
        form.customer.choices[0][0] if form.customer.choices else ""
    )
    if customer and not form.release_key.choices:
        try:
            form.set_release_choices(customer)
        except Exception as e:
            current_app.logger.warning(
                f"Initial load failed for customer={customer}: {e}"
            )
            form.release_key.choices = [("", f"Error: {e}")]

    return render_template(
        "praxisrelease/wizard.html",
        form=form,
        release_source=get_release_source(),
    )

# ---------------------------------------------------------------------------
# Examine release contents (optional)
# ---------------------------------------------------------------------------
@bp.route("/examine", methods=["GET"])
def examine_release():
    """
    Display deployment.yaml of a selected tarball. Safe to keep even if the UI link is disabled.
    """
    customer_raw = request.args.get("customer")
    key = request.args.get("release_key")
    release_page = request.args.get("release_page") or "1"
    # NEW: remember which wizard invoked this (single-vpc, multi-vpc, etc.)
    wizard_slug = request.args.get("wizard_slug") or request.args.get("env_slug") or ""

    manifest = None
    error = None
    customer = None if customer_raw == "all" else customer_raw
    try:
        manifest = fetch_release_manifest(BUCKET, key, customer)
    except Exception as e:
        error = str(e)

    return render_template(
        "praxisrelease/examine.html",
        customer=customer_raw or "",
        release_key=key,
        manifest_yaml=yaml.dump(manifest or {}, sort_keys=False),
        error=error,
        wizard_slug=wizard_slug,   # ← pass through
        release_page=release_page,
    )


def register(app):
    """Register blueprint and (safely) expose our templates globally if needed."""
    import os
    import jinja2

    # 1) Always register the blueprint
    app.register_blueprint(bp)

    # 2) Safely ensure our templates path is visible to Jinja,
    #    regardless of whether the loader is a FileSystemLoader or ChoiceLoader.
    template_path = os.path.join(app.root_path, "modules", "praxisrelease", "templates")
    loader = app.jinja_loader

    try:
        if isinstance(loader, jinja2.ChoiceLoader):
            # Collect all existing search paths to avoid duplicates
            existing_paths = []
            for l in loader.loaders:
                if hasattr(l, "searchpath"):
                    existing_paths.extend(l.searchpath)

            if template_path not in existing_paths:
                app.logger.info(
                    "PRAXISRelease: extending ChoiceLoader with %s", template_path
                )
                app.jinja_loader = jinja2.ChoiceLoader(
                    list(loader.loaders) + [jinja2.FileSystemLoader(template_path)]
                )

        elif hasattr(loader, "searchpath"):
            # Simple FileSystemLoader case
            if template_path not in loader.searchpath:
                app.logger.info(
                    "PRAXISRelease: appending %s to jinja_loader.searchpath", template_path
                )
                loader.searchpath.append(template_path)

        else:
            # Unknown loader type – don't crash the app
            app.logger.warning(
                "PRAXISRelease: unknown jinja_loader type %r; leaving loader unchanged",
                type(loader),
            )

    except Exception as e:
        # Never let template-loader tweaking kill plugin registration
        app.logger.warning(
            "PRAXISRelease: blueprint registered, but failed to tweak jinja_loader: %s",
            e,
            exc_info=True,
        )
    else:
        app.logger.info(
            "PRAXISRelease: blueprint registered and templates path ensured: %s",
            template_path,
        )
