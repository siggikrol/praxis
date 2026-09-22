# forms/formkiq_form.py
from flask_wtf import FlaskForm
from wtforms import StringField, SelectField, FormField, BooleanField
from wtforms.validators import DataRequired, Optional, NumberRange, Regexp
from .fields import NumberInputField
from engine.wizards.constants.project_constants import ENV_DEFAULT_ENV_TYPES
from engine.wizards.forms.ux import get_env_key, get_env_label

# Simple pragmatic email regex (no external dependency)
_EMAIL_RE = r"^[^@\s]+@[^@\s]+\.[^@\s]+$"


class FormKiQEnvForm(FlaskForm):
    """
    Per-environment subform for FormKiQ (mirrors the Rancher pattern).
    Contains ONLY env-specific fields.
    """
    class Meta:
        csrf = False

    # Will be auto-filled to the environment name (uat/prod/staging/...)
    app_env = StringField(
        "App Environment",
        validators=[DataRequired(message="App Environment is required")],
        render_kw={"placeholder": "prod"},
        description="Environment label for this FormKiQ deployment (e.g., uat, prod, staging).",
    )

    fq_enable_doc_scan_events = SelectField(
        "Enable Pre-Deployment Task",
        choices=[("true", "true"), ("false", "false")],
        default="false",
        description="Enable FormKIQ Pre-Deployment Task",
    )

    fq_template = StringField(
        "Template URL",
        validators=[
            DataRequired(message="Template URL is required."),
            Regexp(r"^https?://.+", message="Must start with http:// or https:// and be a valid URL."),
        ],
        render_kw={
            "placeholder": "https://.../template.yaml",
            "pattern": r"https?://.+",
            "title": "Must start with http:// or https://",
            "required": True,
        },
        description="Full URL to the template file (must start with http:// or https://).",
    )
class FormkiqForm(FlaskForm):
    """
    Global (non-env) fields live here.
    Env-specific fields are provided via FormkiqForm.for_envs(...) as FormFields of FormKiQEnvForm,
    exactly like RancherSettingsForm.for_envs.
    """

    disable_section = BooleanField(
        "Exclude FormKiQ from spec",
        default=False,
        description="Skip FormKiQ configuration and omit it from the final spec.",
    )

    # GLOBAL (for all envs)
    fq_admin_email = StringField(
        "FormKiQ Admin Email",
        validators=[
            DataRequired(message="Email is required"),
            Regexp(_EMAIL_RE, message="Enter a valid email address"),
        ],
        render_kw={
            "placeholder": "devops@praxis.ca",
            "type": "email",
            "autocomplete": "email",
            "required": True,
            "pattern": _EMAIL_RE,
            "title": "Enter a valid email like name@example.com",
        },
        description="Admin email used by FormKiQ (global).",
    )

    fq_capacity_provider = StringField(
        "Capacity Provider",
        validators=[DataRequired(message="Capacity Provider is required")],
        default="FARGATE_SPOT",
        render_kw={"placeholder": "FARGATE_SPOT"},
        description="ECS capacity provider to use (default FARGATE_SPOT).",
    )

    fq_enable_public_url = SelectField(
        "Enable Public URL",
        choices=[("true", "true"), ("false", "false")],
        default="false",
        description="Whether to expose a public URL.",
    )

    fq_pwd_min = NumberInputField(
        "Password Minimum Length",
        validators=[Optional(), NumberRange(min=1)],
        default=8,
        description="Minimum number of characters for passwords.",
    )

    fq_pwd_lower_case = SelectField(
        "Require Lowercase?",
        choices=[("true", "true"), ("false", "false")],
        default="false",
    )

    fq_pwd_req_num = SelectField(
        "Require Number?",
        choices=[("true", "true"), ("false", "false")],
        default="false",
    )

    fq_pwd_req_sym = SelectField(
        "Require Symbol?",
        choices=[("true", "true"), ("false", "false")],
        default="false",
    )

    fq_pwd_req_upper_case = SelectField(
        "Require Uppercase?",
        choices=[("true", "true"), ("false", "false")],
        default="false",
    )
    # >>> IDENTICAL pattern to RancherSettingsForm.for_envs(...)
    @classmethod
    def for_envs(cls, env_types):
        """
        Build a dynamic form class with one FormKiQEnvForm per selected env.
        Also prefill each subform's `app_env` with the env name (uat/prod/staging/...).
        """
        env_types = env_types or ENV_DEFAULT_ENV_TYPES

        def _formfield_for(env) -> FormField:
            # Default dict sets initial data for nested fields
            return FormField(
                FormKiQEnvForm,
                description=f"FormKiQ settings for {get_env_label(env)}",
                default={"app_env": get_env_key(env)},
            )

        attrs = {get_env_key(env): _formfield_for(env) for env in env_types}
        return type("FormkiqFormDynamic", (cls,), attrs)

    def validate(self, extra_validators=None):
        # If the user chose to exclude FormKiQ, skip field validation entirely.
        if getattr(self, "disable_section", None) and self.disable_section.data:
            return True
        return super().validate(extra_validators=extra_validators)
