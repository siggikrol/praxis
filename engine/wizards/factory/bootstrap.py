from flask import Blueprint, session, abort
from .views import (
    wizard_step,
    wizard_finish,
    wizard_reset,
    wizard_core_validate_job_start,
    wizard_core_validate_job_page,
    wizard_core_validate_job_status,
)
from .progress import inject_wizard_progress
from .preflight import with_preflight_ok
from .utils import _get_step_index

# import constants needed by templates
from engine.wizards.constants.vpc_constants import (
    VPC_AZ_PROFILE_CHOICES,
    VPC_AZ_PROFILES,
    AZ_ZONE_IDS_BY_REGION,
)
from engine.wizards.constants.eks_constants import (
    EKS_DEFAULTS,
    EKS_VALIDATION_LIMITS,
)
from engine.wizards.constants.replacements_constants import (
    RANCHER_CONTEXT_CHOICES,
    RANCHER_CONTEXT_TO_INTEGRATION,
    RANCHER_WORKER_POOL_ID_DEFAULT,
    RANCHER_WORKER_POOL_ID_EU,
)

from constants import (
    RANCHER_CLUSTER_TO_REGION,
    RANCHER_CLUSTER_TO_RANCHER_PREFIX,
)

def create_wizard_blueprint(env_slug: str, wizard_steps: list[tuple[str, object]]):
    """Assemble and return a Flask Blueprint for a specific wizard."""
    bp = Blueprint(
        f"{env_slug}_wizard",
        __name__,
        url_prefix=f"/{env_slug}",
        template_folder="modules/wizards/templates",
    )

    def resolved_steps():
        from engine.wizards.components import active_steps
        try:
            from engine.wizards.presentation import ordered_steps
            return ordered_steps(active_steps(env_slug, wizard_steps, session))
        except ValueError as exc:
            abort(400, str(exc))

    # Jinja global helper for preflight flag
    bp.add_app_template_global(with_preflight_ok)

    # Inject wizard navigation progress
    @bp.context_processor
    def _inject_progress():
        return inject_wizard_progress(env_slug, resolved_steps())

    # Inject wizard-specific data for templates (blueprint-scoped).
    @bp.context_processor
    def inject_wizard_context():
        steps = resolved_steps()
        from engine.wizards.components import COMPONENTS
        active = {key for key, _ in steps}
        return {
            "included_component_labels": [component.label for component in COMPONENTS if component.key in active],
            "WIZARD_STEPS": steps,
            "get_step_index": lambda s: _get_step_index(steps, s),
        }

    # Inject constants for templates (app-wide).
    @bp.app_context_processor
    def inject_wizard_constants():
        return {
            "rancher_ctx_map": RANCHER_CONTEXT_TO_INTEGRATION,
            "rancher_worker_pool_id_default": RANCHER_WORKER_POOL_ID_DEFAULT,
            "rancher_worker_pool_id_eu": RANCHER_WORKER_POOL_ID_EU,
            "vpc_profiles": VPC_AZ_PROFILES,
            "az_ids_by_region": AZ_ZONE_IDS_BY_REGION,
            "rancher_cluster_region": RANCHER_CLUSTER_TO_REGION,
            "rancher_cluster_prefix": RANCHER_CLUSTER_TO_RANCHER_PREFIX,
        }

    # Wizard step route
    @bp.route("/wizard/<step>", methods=["GET", "POST"], endpoint="wizard_step")
    def _wizard_step(step):
        return wizard_step(env_slug, resolved_steps(), step)

    # Wizard finish route
    @bp.route("/wizard/finish", methods=["GET"], endpoint="wizard_finish")
    def _wizard_finish():
        return wizard_finish(env_slug)

    # Wizard reset route
    @bp.route("/wizard/reset", methods=["POST"], endpoint="wizard_reset")
    def _wizard_reset():
        return wizard_reset(env_slug)

    # Praxis Core runtime validation (dryrun) routes.
    @bp.route(
        "/wizard/core-validate/jobs",
        methods=["POST"],
        endpoint="wizard_core_validate_job_start",
    )
    def _wizard_core_validate_job_start():
        return wizard_core_validate_job_start(env_slug)

    @bp.route(
        "/wizard/core-validate/jobs/<job_id>",
        methods=["GET"],
        endpoint="wizard_core_validate_job_page",
    )
    def _wizard_core_validate_job_page(job_id):
        return wizard_core_validate_job_page(env_slug, job_id)

    @bp.route(
        "/wizard/core-validate/jobs/<job_id>.json",
        methods=["GET"],
        endpoint="wizard_core_validate_job_status",
    )
    def _wizard_core_validate_job_status(job_id):
        return wizard_core_validate_job_status(env_slug, job_id)
    
    return bp
