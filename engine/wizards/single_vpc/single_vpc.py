"""
Single-VPC Wizard blueprint setup.
Preserves the original step order and form sequence from the legacy setup.
Adds dynamic form resolution based on Project Settings selection.
"""

import logging
from flask import Blueprint, abort, redirect, url_for, session, render_template, request, current_app
from engine.wizards.factory import create_wizard_blueprint
from engine.wizards.components import active_steps
from engine.wizards.factory.utils import get_env_types_from_session
from engine.wizards.constants.project_constants import ENV_DEFAULT_ENV_TYPES
from modules.praxisrelease.forms import PraxisReleaseForm
from modules.praxisrelease.service import get_release_source

# --- Core wizard forms ---
from engine.wizards.forms import (
    SingleVpcProjectSettingsForm,
    RepositorySettingsForm,
    SpaceliftSettingsForm,
    ReplacementsForm,
    CommonSettingsForm,
    RancherSettingsForm,
    VpcSettingsForm,
    DefaultSubnetsForm,
    EksSettingsForm,
    AuroraSettingsForm,
    RedisForm,
    AlbForm,
    FormkiqForm,
    VaultDatabaseForm,
)

# --- Whitelist form (dynamic GitHub-driven) ---
from modules.wizard_ext.pc_source.forms import PCSourceForm

# --- Helper to resolve env types dynamically from session ---
def _get_env_types():
    """Return selected environment types from session or defaults."""
    return get_env_types_from_session("single-vpc", ENV_DEFAULT_ENV_TYPES)


# --- Dynamic helpers for multi-env forms ---
def _dynamic_spacelift_form():
    return SpaceliftSettingsForm.for_envs(_get_env_types())

def _dynamic_replacements_form():
    return ReplacementsForm.for_envs(_get_env_types())

def _dynamic_rancher_form():
    env_types = _get_env_types()
    try:
        maybe_dynamic = RancherSettingsForm.for_envs(env_types)
        if getattr(maybe_dynamic, "__name__", "").endswith("Dynamic"):
            return maybe_dynamic
        return maybe_dynamic
    except RecursionError:
        return RancherSettingsForm
    except Exception:
        return RancherSettingsForm

def _dynamic_subnets_form():
    return DefaultSubnetsForm.for_envs(_get_env_types())

def _dynamic_eks_form():
    return EksSettingsForm.for_envs(_get_env_types())

def _dynamic_aurora_form():
    return AuroraSettingsForm.for_envs(_get_env_types())

#def _dynamic_sumologic_form():
#   return SumologicForm.for_envs(_get_env_types())

def _dynamic_formkiq_form():
    return FormkiqForm.for_envs(_get_env_types())

def _dynamic_redis_form():
    return RedisForm.for_envs(_get_env_types())

def _dynamic_vault_form():
    return VaultDatabaseForm.for_envs(_get_env_types())

# ---------------------------------------------------------------------------
# 1. Standalone Release Selection Blueprint
# ---------------------------------------------------------------------------
bp = Blueprint("single-vpc", __name__, url_prefix="/single-vpc")

@bp.route("/release", methods=["GET", "POST"])
def release_selection():
    """
    Standalone pre-wizard release selection page for SINGLE-VPC.
    - POST with reload: stay on this page, just refresh releases list.
    - POST normal submit: save selection, go to single-vpc wizard.
    """
    form = PraxisReleaseForm(wizard_slug="single-vpc")

    # 1) Customer changed → JS posts with reload=1 → just refresh releases list
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
            form_action=url_for("single-vpc.release_selection"),
            wizard_slug="single-vpc",
            release_source=get_release_source(),
        )

    # 2) Normal submit → go to FIRST wizard step
    if form.validate_on_submit():
        selection = {
            "customer": form.customer.data,
            "release_key": form.release_key.data,
        }
        if request.form.get("edit") == "1":
            from services.wizard_workspaces import change_workspace_release
            try:
                _, target_step = change_workspace_release(
                    session, "single-vpc", selection,
                    return_step=request.form.get("return_step", ""),
                    valid_steps=[key for key, _ in active_steps("single-vpc", WIZARD_STEPS, session)],
                )
            except PermissionError:
                abort(403)
        else:
            from services.wizard_workspaces import start_new_workspace
            session["release_selection"] = selection
            start_new_workspace(session, "single-vpc")
            target_step = "project_settings"
        logging.info(
            f"Saved release_selection to session: {session['release_selection']}"
        )
        # keep your current URL pattern: /single-vpc/single-vpc/wizard/project_settings
        return redirect(
            url_for("single-vpc_wizard.wizard_step", step=target_step)
        )


    # 3) Initial GET render
    return render_template(
        "praxisrelease/wizard.html",
        form=form,
        form_action=url_for("single-vpc.release_selection"),
        wizard_slug="single-vpc",
        release_source=get_release_source(),
    )



@bp.route("/")
def env_home():
    """Entry page — redirect to release selector first."""
    return redirect(url_for("single-vpc.release_selection"))


# ---------------------------------------------------------------------------
# 2. Main Wizard Flow (unchanged order except removed release_selection)
# ---------------------------------------------------------------------------
WIZARD_STEPS = [
    ("project_settings", SingleVpcProjectSettingsForm),
    ("repository_settings", RepositorySettingsForm),
    ("spacelift_settings", lambda: _dynamic_spacelift_form()),
    ("replacements", lambda: _dynamic_replacements_form()),
    ("pc_source", PCSourceForm.for_profile("catalyst")),
    ("common", CommonSettingsForm),
    ("rancher", lambda: _dynamic_rancher_form()),
    ("vpc", VpcSettingsForm),
    ("subnets", lambda: _dynamic_subnets_form()),
    ("eks", lambda: _dynamic_eks_form()),
    ("aurora", lambda: _dynamic_aurora_form()),
    ("redis", lambda: _dynamic_redis_form()),
    ("alb", AlbForm),
    ("formkiq", lambda: _dynamic_formkiq_form()),
    ("vault_database", lambda: _dynamic_vault_form()),
]


# ---------------------------------------------------------------------------
# 3. Register Wizard Blueprint Nested Under Single-VPC
# ---------------------------------------------------------------------------
wizard_bp = create_wizard_blueprint("single-vpc", WIZARD_STEPS)
