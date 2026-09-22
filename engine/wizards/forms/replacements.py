# forms/replacements.py
import re
from flask_wtf import FlaskForm
from wtforms import StringField, SelectField, FormField
from wtforms.validators import DataRequired, Regexp
from engine.wizards.constants.replacements_constants import (
    RANCHER_CONTEXT_CHOICES,
    RANCHER_CONTEXT_TO_INTEGRATION,
    RANCHER_WORKER_POOL_ID_DEFAULT,
    rancher_worker_pool_id_for_context,
)
from engine.wizards.constants.project_constants import (
    ENV_DEFAULT_ENV_TYPES,
)
from engine.wizards.forms.ux import get_env_key, get_env_label

PLATFORM_COMPONENT_REPLACEMENT_HELP = (
    "Controls whether Praxis deploys this platform component. "
    "Enable only for Multi-VPC environments; keep disabled for Single-VPC."
)


class ReplacementsEnvForm(FlaskForm):
    class Meta:
        csrf = False

    ssa_rancher_context = SelectField(
        "Rancher Context",
        choices=RANCHER_CONTEXT_CHOICES,
        validators=[DataRequired(message="Select a Rancher context.")],
        default="",
    )
    ssa_rancher_cloud_integration = StringField(
        "Rancher AWS Cloud Integration",
        validators=[DataRequired(), Regexp(r"^[A-Z0-9]+$")],
        render_kw={"placeholder": "01J6C65MK08K6V5BYCBBZ3PWVF"},
        description="Auto-filled from context"
    )
    ssa_rancher_worker_pool_id = StringField(
        "Rancher Worker Pool ID",
        validators=[DataRequired(), Regexp(r"^[A-Z0-9]+$")],
        render_kw={"placeholder": RANCHER_WORKER_POOL_ID_DEFAULT},
        description="Auto-filled from context"
    )
    ssa_iac_git_branch = StringField(
        "IaC Git Branch",
        validators=[DataRequired(), Regexp(r"^[A-Za-z0-9._/-]+$")],
        render_kw={"placeholder": "dev"},
    )

    def _apply_context_mapping(self):
        ctx = (self.ssa_rancher_context.data or "").strip()
        mapped = RANCHER_CONTEXT_TO_INTEGRATION.get(ctx)
        self.ssa_rancher_cloud_integration.data = mapped or ""
        self.ssa_rancher_worker_pool_id.data = (
            rancher_worker_pool_id_for_context(ctx) if mapped else ""
        )

    def validate(self, **kwargs):
        self._apply_context_mapping()
        return super().validate(**kwargs)

class ReplacementsForm(FlaskForm):
    # NOTE: real regex + hints injected in __init__ based on expected_suffix
    ssa_prefix = StringField(
        "SSA Prefix",
        validators=[DataRequired()],  # Regexp set in __init__
        filters=[lambda s: (s or "").strip().lower()],
        render_kw={"placeholder": "<customer>-ctlst"},
        description="Only the base prefix (e.g. 'acme-ctlst'). SSA will append environment/stage automatically."
    )

    #sa_iac_git_branch = StringField(
    #    "IaC Git Branch", validators=[DataRequired()],
    #    render_kw={"placeholder":"main"}, default="main"
    #)
    ssa_iac_git_repo   = StringField(
        "IaC Git Folder",   validators=[DataRequired()],
        render_kw={"placeholder":"ssa-catalyst-sandbox-iac"}
    )
    ssa_helm_context   = StringField(
        "Helm Context",   validators=[DataRequired()],
        render_kw={"placeholder":"harbor_helm_auth"}, default="harbor_helm_auth"
    )
    ssa_shared_vault_context = StringField(
        "Shared Vault Context", validators=[DataRequired()],
        render_kw={"placeholder":"shared_vault_approle_auth"}, default="shared_vault_approle_auth"
    )
    protect_stack_delete = SelectField(
        "Protect Stack Delete", choices=[("true","true"),("false","false")], default="true"
    )
    istio = SelectField(
        "Enable Istio",
        choices=[("true", "true"), ("false", "false")],
        default="false",
        description=PLATFORM_COMPONENT_REPLACEMENT_HELP,
    )
    vault = SelectField(
        "Enable Vault",
        choices=[("true", "true"), ("false", "false")],
        default="false",
        description=PLATFORM_COMPONENT_REPLACEMENT_HELP,
    )

    @classmethod
    def for_envs(cls, env_types, *, platform_components_enabled=False):
        env_types = env_types or ENV_DEFAULT_ENV_TYPES

        attrs = {
            get_env_key(env): FormField(ReplacementsEnvForm, description=f"Replacements for {get_env_label(env)}")
            for env in env_types
        }
        attrs["_env_fields_resolved"] = True
        attrs["_platform_components_enabled"] = platform_components_enabled
        return type("ReplacementsFormDynamic", (cls,), attrs)

    # NEW: suffix-aware validation + hints
    def __init__(self, *args, expected_suffix: str = "ctlst", **kwargs):
        submitted_data = args[0] if args else kwargs.get("formdata")
        saved_data = kwargs.get("data")
        super().__init__(*args, **kwargs)

        # Keep these inherited fields in the common section while allowing
        # topology-specific defaults on dynamically generated form classes.
        if getattr(self, "_platform_components_enabled", False) and submitted_data is None:
            if not isinstance(saved_data, dict) or "istio" not in saved_data:
                self.istio.data = "true"
            if not isinstance(saved_data, dict) or "vault" not in saved_data:
                self.vault.data = "true"

        suffix = (expected_suffix or "ctlst").strip().lower()
        pattern = rf"^[a-z]+-{re.escape(suffix)}$"

        self.ssa_prefix.validators = [
            DataRequired(message="SSA Prefix is required."),
            Regexp(
                pattern,
                message=f"Use <customer>-{suffix} only (lowercase letters). "
                        "Do NOT append -uat/-staging/-prod/etc."
            ),
        ]
        rk = self.ssa_prefix.render_kw = dict(self.ssa_prefix.render_kw or {})
        rk.update({
            "placeholder": f"<customer>-{suffix}",
            "pattern": pattern,
            "title": (
                f"Lowercase letters only followed by '-{suffix}' "
                f"(e.g. acme-{suffix}). Do NOT add -uat/-prod/-staging."
            ),
            "required": True,
        })
        self.ssa_prefix.description = (
            f"Only the base prefix (e.g. 'acme-{suffix}'). "
            "SSA will append environment/stage automatically."
        )
