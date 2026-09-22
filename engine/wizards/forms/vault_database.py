import os

from flask import request, session
from flask_wtf import FlaskForm
from wtforms import StringField, FieldList, FormField, SelectField, BooleanField
from wtforms.validators import DataRequired, NumberRange
from .fields import NumberInputField
from engine.wizards.factory.utils import _key
from engine.wizards.constants.project_constants import ENV_DEFAULT_ENV_TYPES
from engine.wizards.forms.ux import get_env_key, get_env_label
from engine.wizards.constants.vault_constants import (
    VAULT_DB_INSTANCE_TYPES,
    VAULT_DB_ENGINE_VERSION_GROUPS,
    VAULT_DB_ENGINE_VERSIONS,
    VAULT_DB_DEFAULTS,
    VAULT_DB_DEFAULT_PARAMETERS,
)
from services.aws_instance_types.aurora_engine_versions import (
    get_aurora_engine_version_options,
)
from services.aws_instance_types.aurora_instance_classes import (
    get_aurora_instance_class_options,
)

_VAULT_DB_ENGINE = "postgres"


def _wizard_env_slug() -> str:
    try:
        bp = (request.blueprint or "").strip().rsplit(".", 1)[-1]
    except RuntimeError:
        return ""
    if bp.endswith("_wizard"):
        return bp[: -len("_wizard")]
    return bp


def _selected_aws_region(default: str = "us-east-1") -> str:
    try:
        env_slug = _wizard_env_slug()
        if env_slug:
            common_cfg = session.get(_key(env_slug, "common"), {}) or {}
            region = str(common_cfg.get("aws_region") or "").strip()
            if region:
                return region
    except Exception:
        # Best-effort lookup: if request/session context is unavailable or malformed,
        # fall back to AWS_DEFAULT_REGION/default below.
        pass
    return (os.getenv("AWS_DEFAULT_REGION") or default).strip() or default


def _selected_engine_version() -> str:
    try:
        posted = str(request.form.get("vault_database_engine_version") or "").strip()
        if posted:
            return posted
        env_slug = _wizard_env_slug()
        if env_slug:
            saved = session.get(_key(env_slug, "vault_database"), {}) or {}
            version = str(saved.get("vault_database_engine_version") or "").strip()
            if version:
                return version
    except (RuntimeError, KeyError, TypeError, ValueError):
        # If request/session values are unavailable or malformed, use option fallbacks below.
        pass
    _groups, choices, _meta = get_aurora_engine_version_options(
        region=_selected_aws_region(),
        engine=_VAULT_DB_ENGINE,
        fallback_groups=VAULT_DB_ENGINE_VERSION_GROUPS,
        fallback_choices=VAULT_DB_ENGINE_VERSIONS,
        refresh_async_if_stale=False,
    )
    return str(choices[0][0]).strip() if choices else ""


class VaultDbParamForm(FlaskForm):
    name = StringField("Param Name", validators=[DataRequired()])
    value = StringField("Value", validators=[DataRequired()])
    apply_method = SelectField(
        "Apply Method",
        choices=[("pending-reboot", "pending-reboot"), ("immediate", "immediate")],
        default="pending-reboot",
)


def _preserve_dynamic_choice(field, groups, choices) -> None:
    current = str(field.data or "").strip()
    available = {str(value) for value, _label in (choices or [])}
    missing = [(current, f"{current} (saved selection)")] if current and current not in available else []
    field.choices = missing + list(choices or [])
    field.option_groups = (
        ([("Saved selection", missing)] if missing else [])
        + list(groups or [])
    )


class VaultEnvForm(FlaskForm):
    """Per-environment Vault database config (only instance type)."""

    class Meta:
        csrf = False

    vault_database_instance_type = SelectField(
        "Instance Type",
        choices=VAULT_DB_INSTANCE_TYPES,
        validators=[DataRequired()],
        default=VAULT_DB_DEFAULTS["instance_type"],
        description="DB instance class for this environment",
    )

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        engine_version = _selected_engine_version()
        groups, choices, meta = get_aurora_instance_class_options(
            region=_selected_aws_region(),
            engine=_VAULT_DB_ENGINE,
            engine_version=engine_version or None,
            fallback_choices=VAULT_DB_INSTANCE_TYPES,
            # Without a concrete AWS-advertised version, a PostgreSQL-wide
            # query is extremely large and may time out. Wait for versions.
            refresh_async_if_stale=bool(engine_version),
        )
        self._vault_database_instance_classes_meta = meta
        _preserve_dynamic_choice(
            self.vault_database_instance_type,
            groups,
            choices,
        )


class VaultDatabaseForm(FlaskForm):
    """Shared Vault database configuration + per-environment instance type."""

    disable_section = BooleanField(
        "Exclude Vault Database from spec",
        default=False,
        description="Skip Vault database configuration and omit it from the final spec.",
    )

    vault_rds_admin = StringField(
        "DB Admin User",
        validators=[DataRequired()],
        default=VAULT_DB_DEFAULTS["admin_user"],
        render_kw={"readonly": True},
    )

    vault_database_instance_pgroup_family = StringField(
        "Family",
        validators=[DataRequired()],
        default=VAULT_DB_DEFAULTS["family"],
        render_kw={"readonly": True},
    )

    vault_database_engine_version = SelectField(
        "Engine Version",
        choices=VAULT_DB_ENGINE_VERSIONS,
        validators=[DataRequired()],
        default=VAULT_DB_DEFAULTS["engine_version"],
    )

    vault_database_instance_allocated_storage = NumberInputField(
        "Allocated Storage (GiB)",
        validators=[DataRequired(), NumberRange(min=20)],
        default=VAULT_DB_DEFAULTS["allocated_storage"],
    )

    vault_database_instance_parameters = FieldList(
        FormField(VaultDbParamForm),
        min_entries=len(VAULT_DB_DEFAULT_PARAMETERS),
        default=VAULT_DB_DEFAULT_PARAMETERS,
        label="DB Cluster Params",
    )

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self._vault_default_parameter_count = len(VAULT_DB_DEFAULT_PARAMETERS)
        groups, choices, meta = get_aurora_engine_version_options(
            region=_selected_aws_region(),
            engine=_VAULT_DB_ENGINE,
            fallback_groups=VAULT_DB_ENGINE_VERSION_GROUPS,
            fallback_choices=VAULT_DB_ENGINE_VERSIONS,
        )
        self._vault_database_engine_versions_meta = meta
        _preserve_dynamic_choice(
            self.vault_database_engine_version,
            groups,
            choices,
        )

    @classmethod
    def for_envs(cls, env_types):
        """Generate dynamic Vault form with per-env instance type fields only."""
        env_types = env_types or ENV_DEFAULT_ENV_TYPES
        attrs = {
            get_env_key(env): FormField(VaultEnvForm, description=f"Vault DB config for {get_env_label(env)}")
            for env in env_types
        }
        return type("VaultDatabaseFormDynamic", (cls,), attrs)

    def validate(self, extra_validators=None):
        # If excluded, allow submit without validating required fields.
        if getattr(self, "disable_section", None) and self.disable_section.data:
            return True
        version = str(self.vault_database_engine_version.data or "").strip()
        major = version.split(".", 1)[0]
        if major.isdigit():
            # RDS parameter-group families must match the selected PostgreSQL major.
            self.vault_database_instance_pgroup_family.data = f"postgres{major}"
        return super().validate(extra_validators=extra_validators)
