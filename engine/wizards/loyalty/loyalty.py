"""
Playon Wizard blueprint setup.
Preserves the original step order and form sequence from the legacy setup.
Adds dynamic form resolution based on Project Settings selection.
"""
import logging
from flask import Blueprint, abort, redirect, url_for, session, render_template, request, current_app
from engine.wizards.factory import create_wizard_blueprint
from engine.wizards.factory.utils import get_env_types_from_session
from engine.wizards.constants.project_constants import ENV_DEFAULT_ENV_TYPES
from modules.praxisrelease.forms import PraxisReleaseForm
from modules.praxisrelease.service import get_release_source

# --- Core wizard forms ---
# --- Core wizard forms ---
from engine.wizards.forms import (
    DefaultProjectSettingsForm,
    RepositorySettingsForm,
    SpaceliftSettingsForm,
    ReplacementsForm,
    CommonSettingsForm,
    RancherSettingsForm,
    VpcSettingsForm,
    DefaultSubnetsForm,
    EksSettingsForm,
    AuroraSettingsForm,
    KafkaForm,
)

# --- Whitelist form (dynamic GitHub-driven) ---
# from modules.wizard_ext.pc_source.forms import PCSourceForm


# --- Helper to resolve env types dynamically from session ---
def _get_env_types():
    """Return selected environment types from session or defaults."""
    return get_env_types_from_session("loyalty", ENV_DEFAULT_ENV_TYPES)


# --- Dynamic helpers for multi-env forms ---
def _dynamic_spacelift_form():
    """Create SpaceliftSettingsForm dynamically based on Project Settings selection."""
    return SpaceliftSettingsForm.for_envs(_get_env_types())


def _dynamic_replacements_form():
    """Create ReplacementsForm dynamically based on selected environment types."""
    return ReplacementsForm.for_envs(_get_env_types())


def _dynamic_rancher_form():
    """Safely create RancherSettingsForm only once to avoid recursion."""
    env_types = _get_env_types()
    form_cls = RancherSettingsForm
    try:
        maybe_dynamic = form_cls.for_envs(env_types)
        # Detect if it's already dynamic to prevent second .for_envs() call
        if getattr(maybe_dynamic, "__name__", "").endswith("Dynamic"):
            return maybe_dynamic
        return maybe_dynamic
    except RecursionError:
        # If for_envs itself loops, fallback to base
        return RancherSettingsForm
    except Exception:
        # If RancherSettingsForm has no for_envs()
        return RancherSettingsForm

def _dynamic_subnets_form():
    """Create SubnetsForm dynamically based on selected environment types."""
    env_types = _get_env_types()
    return DefaultSubnetsForm.for_envs(env_types)

def _dynamic_eks_form():
    """Create EKSSettingsForm dynamically based on selected environment types."""
    env_types = _get_env_types()
    return EksSettingsForm.for_envs(env_types)

def _dynamic_aurora_form():
    """Create AuroraSettingsForm dynamically based on selected environment types."""
    env_types = _get_env_types()
    return AuroraSettingsForm.for_envs(env_types)

# ---------------------------------------------------------------------------
# 1. Standalone Release Selection Blueprint
# ---------------------------------------------------------------------------
bp = Blueprint("loyalty", __name__, url_prefix="/loyalty")

@bp.route("/release", methods=["GET", "POST"])
def release_selection():
    """
    Standalone pre-wizard release selection page for LOYALTY.
    """
    form = PraxisReleaseForm(wizard_slug="loyalty")

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

        return render_template(
            "praxisrelease/wizard.html",
            form=form,
            form_action=url_for("loyalty.release_selection"),
            wizard_slug="loyalty",
            release_source=get_release_source(),
        )

    if form.validate_on_submit():
        selection = {
            "customer": form.customer.data,
            "release_key": form.release_key.data,
        }
        if request.form.get("edit") == "1":
            from services.wizard_workspaces import change_workspace_release
            try:
                _, target_step = change_workspace_release(
                    session, "loyalty", selection,
                    return_step=request.form.get("return_step", ""),
                    valid_steps=[key for key, _ in WIZARD_STEPS],
                )
            except PermissionError:
                abort(403)
        else:
            from services.wizard_workspaces import start_new_workspace
            session["release_selection"] = selection
            start_new_workspace(session, "loyalty")
            target_step = "project_settings"
        logging.info(
            f"Saved release_selection to session: {session['release_selection']}"
        )
        # loyalty wizard endpoints look like loyalty.wizard_step
        return redirect(url_for("loyalty_wizard.wizard_step", step=target_step))

    return render_template(
        "praxisrelease/wizard.html",
        form=form,
        form_action=url_for("loyalty.release_selection"),
        wizard_slug="loyalty",
        release_source=get_release_source(),
    )

@bp.route("/")
def env_home():
    """Entry page — redirect to release selector first."""
    return redirect(url_for("loyalty.release_selection"))

# ---------------------------------------------------------------------------
# 2. Main Wizard Flow (unchanged order except removed release_selection)
# ---------------------------------------------------------------------------
WIZARD_STEPS = [
    ("project_settings", DefaultProjectSettingsForm),
    ("repository_settings", RepositorySettingsForm),
    ("spacelift_settings", lambda: _dynamic_spacelift_form()),
    ("replacements", lambda: _dynamic_replacements_form()),
    #("pc_source", PCSourceForm.for_profile("loyalty")),
    ("common", CommonSettingsForm),
    ("rancher", lambda: _dynamic_rancher_form()),
    ("vpc", VpcSettingsForm),
    ("subnets", lambda: _dynamic_subnets_form()),
    ("eks", lambda: _dynamic_eks_form()),
    ("aurora", lambda: _dynamic_aurora_form()),
    ("kafka", KafkaForm),
]

# ---------------------------------------------------------------------------
# 3. Register Wizard Blueprint Nested Under Loyalty
# ---------------------------------------------------------------------------
wizard_bp = create_wizard_blueprint("loyalty", WIZARD_STEPS)
