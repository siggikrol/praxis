import os

from flask import request, session
from flask_wtf import FlaskForm
from wtforms import StringField, SelectField, FieldList, FormField
from wtforms.validators import DataRequired, Optional
from engine.wizards.factory.utils import _key
from engine.wizards.constants.aurora_constants import (
    AURORA_ENGINE_CHOICES,
    get_aurora_engine_version_fallback_groups,
    get_aurora_engine_version_fallback_choices,
    AURORA_INSTANCE_CLASSES,
    AURORA_INSTANCE_CLASS_GROUPS,
    AURORA_PARAM_APPLY_METHODS,
    AURORA_DEFAULT_PARAMETERS,
    AURORA_DEFAULTS,
    get_aurora_engine_profile,
)
from engine.wizards.constants.project_constants import ENV_DEFAULT_ENV_TYPES
from engine.wizards.forms.ux import get_env_key, get_env_label
from services.aws_instance_types.aurora_instance_classes import get_aurora_instance_class_options
from services.aws_instance_types.aurora_engine_versions import get_aurora_engine_version_options

_AURORA_DEFAULT_ENGINE_BY_WIZARD = {
    "loyalty": "aurora-mysql",
}


def _wizard_env_slug() -> str:
    try:
        bp = (request.blueprint or "").strip()
    except RuntimeError:
        return ""
    if bp.endswith("_wizard"):
        return bp[: -len("_wizard")]
    return bp


def _request_method() -> str:
    try:
        return (request.method or "").strip().upper()
    except RuntimeError:
        return ""


def _default_aurora_engine(default: str = AURORA_DEFAULTS["engine"]) -> str:
    wizard_slug = _wizard_env_slug()
    preferred = _AURORA_DEFAULT_ENGINE_BY_WIZARD.get(wizard_slug, "")
    if preferred:
        return preferred
    return default


def _selected_aws_region(default: str = "us-east-1") -> str:
    try:
        env_slug = _wizard_env_slug()
        if env_slug:
            common_cfg = session.get(_key(env_slug, "common"), {}) or {}
            region = (common_cfg.get("aws_region") or "").strip()
            if region:
                return region
    except Exception:
        pass
    return (os.getenv("AWS_DEFAULT_REGION") or default).strip() or default


def _selected_aurora_engine(default: str | None = None) -> str:
    default = (default or _default_aurora_engine()).strip() or AURORA_DEFAULTS["engine"]
    try:
        if _request_method() == "POST":
            eng = (request.form.get("engine") or "").strip()
            if eng:
                return eng
    except Exception:
        pass
    try:
        env_slug = _wizard_env_slug()
        if env_slug:
            aurora_cfg = session.get(_key(env_slug, "aurora"), {}) or {}
            eng = (aurora_cfg.get("engine") or "").strip()
            if eng:
                return eng
    except Exception:
        pass
    return (os.getenv("PS_AURORA_ENGINE") or default).strip() or default


class AuroraEnvForm(FlaskForm):
    class Meta:
        csrf = False

    aurora_instance_class = SelectField(
        "Instance Class",
        choices=AURORA_INSTANCE_CLASSES,
        validators=[Optional()],
        description="Instance class per-environment",
    )

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        region = _selected_aws_region()
        engine = _selected_aurora_engine()
        groups, choices, meta = get_aurora_instance_class_options(
            region=region,
            engine=engine,
            fallback_groups=AURORA_INSTANCE_CLASS_GROUPS,
            fallback_choices=AURORA_INSTANCE_CLASSES,
        )
        self._aurora_instance_classes_meta = meta
        self.aurora_instance_class.option_groups = groups
        self.aurora_instance_class.choices = choices


class ParameterForm(FlaskForm):
    name = StringField("Parameter Name", validators=[DataRequired()], default=AURORA_DEFAULT_PARAMETERS[0]["name"])
    value = StringField("Value", validators=[DataRequired()], default=AURORA_DEFAULT_PARAMETERS[0]["value"])
    apply_method = SelectField(
        "Apply Method",
        choices=AURORA_PARAM_APPLY_METHODS,
        validators=[DataRequired()],
        default=AURORA_DEFAULT_PARAMETERS[0]["apply_method"],
    )


class AuroraSettingsForm(FlaskForm):
    engine = SelectField(
        "Engine", validators=[DataRequired()],
        choices=AURORA_ENGINE_CHOICES,
        default=AURORA_DEFAULTS["engine"],
        description="Database engine",
    )
    engine_version = SelectField(
        "Engine Version",
        choices=[],
        validators=[DataRequired()],
        default=AURORA_DEFAULTS["engine_version"],
    )
    database_username = StringField(
        "DB Username", validators=[DataRequired()],
        default=AURORA_DEFAULTS["database_username"],
    )

    create_db_cluster_parameter_group = SelectField(
        "Create Param Group",
        choices=[("true", "true"), ("false", "false")],
        validators=[DataRequired()],
        default=AURORA_DEFAULTS["create_param_group"],
    )
    db_cluster_parameter_group_use_name_prefix = SelectField(
        "Use Name Prefix for Param Group",
        choices=[("true", "true"), ("false", "false")],
        validators=[DataRequired()],
        default=AURORA_DEFAULTS["use_name_prefix"],
    )
    db_cluster_parameter_group_family = StringField(
        "Param Group Family",
        validators=[Optional()],
        default=AURORA_DEFAULTS["param_group_family"],
    )

    db_cluster_parameter_group_parameters = FieldList(
        FormField(ParameterForm),
        min_entries=0,
        default=[],
        description="List of DB cluster parameters to apply",
    )

    enabled_cloudwatch_logs_exports = StringField(
        "CloudWatch Logs Exports",
        validators=[Optional()],
        default=AURORA_DEFAULTS["enabled_logs_exports"],
    )
    enable_s3_export_lambda = SelectField(
        "Enable S3 Export Lambda",
        choices=[("true", "true"), ("false", "false")],
        validators=[DataRequired()],
        default=AURORA_DEFAULTS["enable_s3_export_lambda"],
    )

    def __init__(self, *args, **kwargs):
        initial_data = kwargs.get("data")
        if not isinstance(initial_data, dict):
            initial_data = {}

        def _has_initial(name: str) -> bool:
            return bool(str(initial_data.get(name) or "").strip())

        initial_parameters = initial_data.get("db_cluster_parameter_group_parameters")
        has_initial_parameters = isinstance(initial_parameters, list) and len(initial_parameters) > 0

        super().__init__(*args, **kwargs)
        method = _request_method()
        if method != "POST" and not _has_initial("engine"):
            self.engine.data = _selected_aurora_engine()

        selected_engine = (self.engine.data or _selected_aurora_engine()).strip() or AURORA_DEFAULTS["engine"]
        profile = get_aurora_engine_profile(selected_engine)
        self._aurora_default_parameter_count = len(profile["parameters"])

        region = _selected_aws_region()
        version_groups, version_choices, version_meta = get_aurora_engine_version_options(
            region=region,
            engine=selected_engine,
            fallback_groups=get_aurora_engine_version_fallback_groups(selected_engine),
            fallback_choices=get_aurora_engine_version_fallback_choices(selected_engine),
        )
        self._aurora_engine_versions_meta = version_meta
        self.engine_version.option_groups = version_groups
        self.engine_version.choices = version_choices

        if method != "POST" and not _has_initial("engine_version"):
            self.engine_version.data = profile["engine_version"]
        self._ensure_current_choice(self.engine_version)
        if method != "POST" and not _has_initial("db_cluster_parameter_group_family"):
            self.db_cluster_parameter_group_family.data = profile["param_group_family"]
        if method != "POST" and not _has_initial("enabled_cloudwatch_logs_exports"):
            self.enabled_cloudwatch_logs_exports.data = profile["enabled_logs_exports"]
        if method != "POST" and not has_initial_parameters and not self._has_parameter_group_values():
            self._replace_parameter_group_parameters(profile["parameters"])

    @staticmethod
    def _ensure_current_choice(field) -> None:
        current = str(field.data or "").strip()
        if not current:
            return
        present = {str(v) for v, _ in (field.choices or [])}
        if current in present:
            return
        field.choices = [(current, current)] + list(field.choices or [])

    def _has_parameter_group_values(self) -> bool:
        for entry in self.db_cluster_parameter_group_parameters.entries:
            sub = getattr(entry, "form", None)
            if not sub:
                continue
            name_field = getattr(sub, "name", None)
            value_field = getattr(sub, "value", None)
            name = (name_field.data or "").strip() if name_field else ""
            value = (value_field.data or "").strip() if value_field else ""
            if name or value:
                return True
        return False

    def _replace_parameter_group_parameters(self, parameters: list[dict]) -> None:
        while self.db_cluster_parameter_group_parameters.entries:
            self.db_cluster_parameter_group_parameters.pop_entry()
        for param in parameters or AURORA_DEFAULT_PARAMETERS:
            self.db_cluster_parameter_group_parameters.append_entry({
                "name": (param.get("name") or "").strip(),
                "value": (param.get("value") or "").strip(),
                "apply_method": (param.get("apply_method") or AURORA_PARAM_APPLY_METHODS[0][0]).strip(),
            })

    @classmethod
    def for_envs(cls, env_types):
        env_types = env_types or ENV_DEFAULT_ENV_TYPES

        def _env_field(env) -> FormField:
            return FormField(
                AuroraEnvForm,
                description=f"Aurora settings for {get_env_label(env)}",
            )

        attrs = {get_env_key(env): _env_field(env) for env in env_types}
        return type("AuroraSettingsFormDynamic", (cls,), attrs)
