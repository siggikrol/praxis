# engine/wizards/unified/unified.py

"""
Unified Wizard blueprint setup.

Includes all major steps from Multi-VPC, RGS, and Loyalty wizards.
Uses dynamic resolution for environment-dependent forms and shares
the same generic release selection pattern.
"""

import logging
from flask import Blueprint, abort, redirect, url_for, session, render_template, request, current_app

from engine.wizards.factory import create_wizard_blueprint
from engine.wizards.factory.utils import get_env_types_from_session
from engine.wizards.constants.project_constants import ENV_DEFAULT_ENV_TYPES
from modules.praxisrelease.forms import PraxisReleaseForm
from modules.praxisrelease.service import get_release_source
from modules.wizard_ext.pc_source.forms import PCSourceForm

# Core wizard forms
from engine.wizards.forms import (
    UnifiedProjectSettingsForm,
    RepositorySettingsForm,
    SpaceliftSettingsForm,
    ReplacementsForm,
    CommonSettingsForm,
    RancherSettingsForm,
    VpcSettingsForm,
    SubnetsForm,
    DefaultSubnetsForm,
    EksSettingsForm,
    AuroraSettingsForm,
    RedisForm,
    AlbForm,
    FormkiqForm,
    VaultDatabaseForm,
)

# ---------------------------------------------------------------------------
# Dynamic helpers
# ---------------------------------------------------------------------------

def _get_env_types():
    """Return selected environment types from session or defaults."""
    return get_env_types_from_session("unified", ENV_DEFAULT_ENV_TYPES)


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
    return SubnetsForm.for_envs(_get_env_types())


def _dynamic_default_subnets_form():
    return DefaultSubnetsForm.for_envs(_get_env_types())


def _dynamic_eks_form():
    return EksSettingsForm.for_envs(_get_env_types())


def _dynamic_aurora_form():
    return AuroraSettingsForm.for_envs(_get_env_types())


def _dynamic_redis_form():
    return RedisForm.for_envs(_get_env_types())


def _dynamic_formkiq_form():
    return FormkiqForm.for_envs(_get_env_types())


def _dynamic_vault_form():
    return VaultDatabaseForm.for_envs(_get_env_types())


# ---------------------------------------------------------------------------
# 1. Standalone Release Selection Blueprint
# ---------------------------------------------------------------------------

bp = Blueprint("unified", __name__, url_prefix="/unified")


@bp.route("/release", methods=["GET", "POST"])
def release_selection():
    """
    Standalone pre-wizard release selection page for the unified wizard.
    """
    form = PraxisReleaseForm(wizard_slug="unified")

    if request.method == "POST" and "reload" in request.form:
        customer = request.form.get("customer") or ""
        form.customer.data = customer
        try:
            form.set_release_choices(customer)
        except Exception as e:
            current_app.logger.warning(
                "Reload failed for customer=%s: %s", customer, e
            )
            form.release_key.choices = [("", f"Error: {e}")]

        return render_template(
            "praxisrelease/wizard.html",
            form=form,
            form_action=url_for("unified.release_selection"),
            wizard_slug="unified",
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
                    session, "unified", selection,
                    return_step=request.form.get("return_step", ""),
                    valid_steps=[key for key, _ in WIZARD_STEPS],
                )
            except PermissionError:
                abort(403)
        else:
            from services.wizard_workspaces import start_new_workspace
            session["release_selection"] = selection
            start_new_workspace(session, "unified")
            target_step = "project_settings"
        logging.info("Saved unified release_selection to session")

        return redirect(
            url_for("unified_wizard.wizard_step", step=target_step)
        )

    return render_template(
        "praxisrelease/wizard.html",
        form=form,
        form_action=url_for("unified.release_selection"),
        wizard_slug="unified",
        release_source=get_release_source(),
    )


@bp.route("/")
def env_home():
    """Entry page — redirect to unified release selector."""
    return redirect(url_for("unified.release_selection"))


# ---------------------------------------------------------------------------
# 2. Unified Main Wizard Flow
#    Combined components from multi-vpc, rgs, loyalty.
# ---------------------------------------------------------------------------

WIZARD_STEPS = [
    # Project settings variants
    ("project_settings", UnifiedProjectSettingsForm),

    # Repository + CI/CD basics
    ("repository_settings", RepositorySettingsForm),
    ("spacelift_settings", lambda: _dynamic_spacelift_form()),
    ("replacements", lambda: _dynamic_replacements_form()),

    # Helm whitelists for all three profiles
    ("pc_source", PCSourceForm.for_profile("unified")),

    # Shared common and platform settings
    ("common", CommonSettingsForm),
    ("rancher", lambda: _dynamic_rancher_form()),

    # Network and compute
    ("vpc", VpcSettingsForm),
    ("subnets", lambda: _dynamic_default_subnets_form()),
    ("eks", lambda: _dynamic_eks_form()),
    ("aurora", lambda: _dynamic_aurora_form()),
    ("redis", lambda: _dynamic_redis_form()),
    ("alb", AlbForm),

    # Application-level options from multi-vpc
    ("formkiq", lambda: _dynamic_formkiq_form()),
    ("vault_database", lambda: _dynamic_vault_form()),
]

# ---------------------------------------------------------------------------
# 3. Register Wizard Blueprint Nested Under Unified
# ---------------------------------------------------------------------------

wizard_bp = create_wizard_blueprint("unified", WIZARD_STEPS)
