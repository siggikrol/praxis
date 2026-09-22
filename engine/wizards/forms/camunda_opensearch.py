from flask import current_app, request, session
from flask_wtf import FlaskForm
from wtforms import BooleanField, FormField, SelectField, SelectMultipleField, StringField, TextAreaField
from wtforms.widgets import CheckboxInput, ListWidget

from engine.wizards.camunda_opensearch import DEFAULTS, INTEGER_BOUNDS, LOG_TYPES, normalize, validate_config
from engine.wizards.camunda_opensearch import SHARED_INPUTS, effective_config, migrate_form_state
from engine.wizards.factory.utils import get_env_types_from_session
from engine.wizards.forms.fields import NumberInputField
from engine.wizards.forms.ux import get_env_key, get_env_label


GROUPS = (
    ("Camunda deployment", ("cluster_target", "eks_camunda_namespace", "eks_camunda_zeebe_service_account", "eks_camunda_optimize_service_account")),
    ("Domain and capacity", ("opensearch_domain_name", "opensearch_engine_version", "opensearch_instance_type", "opensearch_instance_count", "opensearch_master_type", "opensearch_master_count")),
    ("Storage", ("opensearch_ebs_volume_type", "opensearch_ebs_volume_size", "opensearch_ebs_iops", "opensearch_ebs_throughput")),
    ("Custom endpoint", ("opensearch_custom_endpoint_enabled", "opensearch_custom_endpoint", "opensearch_custom_endpoint_certificate_id", "opensearch_master_user_secret_name")),
    ("Network access", ("opensearch_additional_cidr_blocks",)),
    ("Account and advanced settings", ("opensearch_create_iam_service_linked_role", "opensearch_service_linked_role_propagation_delay", "opensearch_enabled_logs", "opensearch_auto_tune_desired_state", "opensearch_auto_tune_rollback_on_disable", "opensearch_off_peak_window_start_time_hours", "opensearch_off_peak_window_start_time_minutes")),
)


class CamundaEnvironmentForm(FlaskForm):
    class Meta:
        csrf = False

    groups = GROUPS
    multi = False
    override_shared_settings = BooleanField(
        "Override shared settings for this environment", default=False,
        description="Use only when this environment needs different deployment or engine settings.",
    )
    eks_camunda_namespace = StringField("Camunda namespace", default=DEFAULTS["eks_camunda_namespace"], description="Use the namespace from the selected Camunda deployment.")
    eks_camunda_zeebe_service_account = StringField("Zeebe service account", default=DEFAULTS["eks_camunda_zeebe_service_account"], description="Must match the rendered application chart; used for IAM trust.")
    eks_camunda_optimize_service_account = StringField("Optimize service account", default=DEFAULTS["eks_camunda_optimize_service_account"], description="Must match the rendered application chart; used for IAM trust.")
    opensearch_domain_name = StringField("OpenSearch domain name", description="3–28 lowercase letters, digits or hyphens, starting with a letter. Unique in this account and region.")
    opensearch_engine_version = SelectField("Engine version", default=DEFAULTS["opensearch_engine_version"], choices=[], validate_choice=False)
    opensearch_instance_type = SelectField("Data instance type", default=DEFAULTS["opensearch_instance_type"], choices=[], validate_choice=False)
    opensearch_master_type = SelectField("Master instance type", default=DEFAULTS["opensearch_master_type"], choices=[], validate_choice=False)
    opensearch_ebs_volume_type = SelectField("Volume type", choices=[(v, v) for v in ("gp3", "gp2", "io1", "standard")], default="gp3")
    opensearch_custom_endpoint_enabled = BooleanField("Use a custom endpoint", default=False)
    opensearch_custom_endpoint = StringField("Custom endpoint hostname", description="Required with a custom endpoint. The module finds the Route53 zone by removing the first hostname label, then creates the CNAME. That zone must already exist.")
    opensearch_custom_endpoint_certificate_id = StringField("ACM certificate ID", description="Required with a custom endpoint. The certificate stack exposes cert_arn; use its certificate UUID in the same account and region, covering this hostname. Automatic stack-output wiring is not implemented yet.")
    opensearch_master_user_secret_name = StringField("Master-user secret reference", description="This module reads an existing Secrets Manager secret containing username/password; it does not create it. Confirm the owning stack. Include its generated six-character suffix; never paste credentials.")
    opensearch_create_iam_service_linked_role = SelectField("OpenSearch service-linked role", choices=[("", "Choose role ownership"), ("false", "Reuse the account's existing role"), ("true", "This stack creates the account's role")], description="Check the target account. Only one stack should own creation of this account-level role.")
    opensearch_service_linked_role_propagation_delay = StringField("Role propagation delay", default="120s", description="Used when this stack creates the role.")
    opensearch_cidr_blocks = TextAreaField("Legacy CIDR override — clear after review", description="Move only approved extra worker ranges to Additional worker CIDRs, then clear this old override. Network subnet CIDRs are supplied automatically.")
    opensearch_additional_cidr_blocks = TextAreaField("Additional worker CIDRs", default="10.0.0.0/8", description="Network stack private subnet CIDRs are included automatically. Add only approved extra networks that need access, such as the Spacelift worker network. One CIDR per line.")
    opensearch_enabled_logs = SelectMultipleField("Enabled logs", choices=[(v, v) for v in LOG_TYPES], default=list(LOG_TYPES), option_widget=CheckboxInput(), widget=ListWidget(prefix_label=False))
    opensearch_auto_tune_desired_state = SelectField("Auto-tune", choices=[(v, v) for v in ("DISABLED", "ENABLED")], default="DISABLED")
    opensearch_auto_tune_rollback_on_disable = SelectField("Auto-tune rollback", choices=[(v, v) for v in ("NO_ROLLBACK", "DEFAULT_ROLLBACK")], default="NO_ROLLBACK")

    def __init__(self, *args, **kwargs):
        data = dict(kwargs.get("data") or {})
        # FormField supplies child values as keyword arguments, standalone forms
        # can supply a data mapping. Normalize both without touching POST data.
        for key in ("opensearch_cidr_blocks", "opensearch_additional_cidr_blocks", "opensearch_create_iam_service_linked_role"):
            if key in kwargs:
                data[key] = kwargs.pop(key)
        if isinstance(data.get("opensearch_cidr_blocks"), list):
            data["opensearch_cidr_blocks"] = "\n".join(data["opensearch_cidr_blocks"])
        if isinstance(data.get("opensearch_additional_cidr_blocks"), list):
            data["opensearch_additional_cidr_blocks"] = "\n".join(data["opensearch_additional_cidr_blocks"])
        if type(data.get("opensearch_create_iam_service_linked_role")) is bool:
            data["opensearch_create_iam_service_linked_role"] = str(data["opensearch_create_iam_service_linked_role"]).lower()
        kwargs["data"] = data
        super().__init__(*args, **kwargs)

for _name, (_minimum, _maximum) in INTEGER_BOUNDS.items():
    _label = {
        "opensearch_instance_count": "Data instance count",
        "opensearch_master_count": "Master instance count",
        "opensearch_ebs_volume_size": "Volume size (GiB)",
        "opensearch_ebs_iops": "Volume IOPS",
        "opensearch_ebs_throughput": "Volume throughput (MiB/s)",
        "opensearch_off_peak_window_start_time_hours": "Off-peak start hour (UTC)",
        "opensearch_off_peak_window_start_time_minutes": "Off-peak start minute",
    }[_name]
    setattr(CamundaEnvironmentForm, _name, NumberInputField(_label, default=DEFAULTS[_name], render_kw={"min": _minimum, **({"max": _maximum} if _maximum is not None else {})}))


class CamundaOpenSearchForm(FlaskForm):
    shared_inputs = SHARED_INPUTS
    _environment_keys = ()

    def __init__(self, *args, **kwargs):
        kwargs["data"] = migrate_form_state(kwargs.get("data"), self._environment_keys)
        super().__init__(*args, **kwargs)

    def validate(self, extra_validators=None):
        valid = super().validate(extra_validators)
        for env in self._environment_keys:
            target = self[env].form
            values = effective_config(self.data, env)
            errors = validate_config(values, target.multi)
            if target.opensearch_cidr_blocks.data:
                errors["opensearch_cidr_blocks"] = ["Review the legacy override, move only additional approved worker ranges, and clear this field."]
            for name, message in getattr(target, "catalog_errors", {}).items():
                errors.setdefault(name, []).append(message)
            for name, messages in errors.items():
                field = self[name] if name in SHARED_INPUTS and not target.override_shared_settings.data else target[name]
                field.errors = list(dict.fromkeys([*field.errors, *messages]))
                valid = False
        return valid

    @classmethod
    def for_envs(cls, envs, *, multi=False):
        attrs = {"multi": multi}
        if multi:
            attrs["cluster_target"] = SelectField("EKS cluster hosting Camunda", choices=[("", "Choose the application cluster"), ("ilp", "ILP"), ("cgs", "CGS"), ("pmv", "PMV")])
        env_form = type("CamundaTargetForm", (CamundaEnvironmentForm,), attrs)
        fields = {
            get_env_key(env): FormField(env_form, label=get_env_label(env)) for env in envs
        }
        fields["_env_fields_resolved"] = True
        fields["_environment_keys"] = tuple(get_env_key(env) for env in envs)
        return type("CamundaOpenSearchFormDynamic", (cls,), fields)

    @classmethod
    def for_wizard(cls, env_slug):
        return cls.for_envs(get_env_types_from_session(env_slug), multi=env_slug == "multi-vpc")


for _name in SHARED_INPUTS:
    setattr(CamundaOpenSearchForm, _name, getattr(CamundaEnvironmentForm, _name))


def seed_camunda_form(env_slug, form):
    common = session.get(f"{env_slug}:common") or {}
    prefix = str(common.get("environment") or "").strip()
    domain = str(common.get("domain_name") or "").strip()
    from services.aws_instance_types.opensearch_versions import get_version_options
    versions, form.version_meta = get_version_options(
        common.get("aws_region"),
        force=request.method == "POST" and request.form.get("opensearch_catalog_refresh") == "versions",
        refresh_async=not current_app.testing,
    )
    form.supported_versions = versions
    def version_choices(field):
        field.choices = [(value, value) for value in versions]
        if field.data and field.data not in versions:
            field.choices.append((field.data, f"{field.data} (not verified in this region)"))
    version_choices(form.opensearch_engine_version)
    for field in form:
        if field.type != "FormField":
            continue
        env = field.short_name
        target = field.form
        version_choices(target.opensearch_engine_version)
        saved_references = (session.get(f"{env_slug}:camunda_opensearch") or {}).get(env, {})
        for name in ("opensearch_master_user_secret_name", "opensearch_custom_endpoint_certificate_id"):
            target[name].data = saved_references.get(name, "")
        # Preview only: never store a second EKS_CLUSTER_NAME in this card.
        cluster = target.cluster_target.data if target.multi else ""
        target.cluster_name_suffix = f"{prefix}-{env}" if prefix else ""
        if not prefix:
            target.cluster_preview = "Complete Common Settings first"
        elif target.multi and not cluster:
            target.cluster_preview = "Choose the application cluster"
        else:
            target.cluster_preview = f"{cluster + '-' if cluster else ''}{prefix}-{env}"
        target.shared_values = {key: common.get(key, "") for key in ("aws_region", "customer", "product", "support_organization")}
        configure_catalog(form, target, env, common.get("aws_region"))
        # Suggestions apply only to an unsaved GET; explicit POST data is validated as entered.
        if request.method != "GET":
            continue
        saved = (session.get(f"{env_slug}:camunda_opensearch") or {}).get(env, {})
        if prefix and "opensearch_domain_name" not in saved:
            candidate = f"{prefix}-{env}-os"
            if len(candidate) <= 28:
                target.opensearch_domain_name.data = candidate
        if prefix and domain and not saved.get("opensearch_custom_endpoint"):
            from engine.wizards.camunda_opensearch import suggested_custom_endpoint
            target.opensearch_custom_endpoint.data = suggested_custom_endpoint(common, env)


def normalize_payload(payload):
    result = {key: payload[key] for key in SHARED_INPUTS if key in payload}
    for env, values in payload.items():
        if not isinstance(values, dict):
            continue
        values = normalize(values)
        if not values.get("override_shared_settings"):
            values = {key: value for key, value in values.items() if key not in SHARED_INPUTS}
        result[env] = values
    return result


def configure_catalog(form, target, env, region):
    from services.aws_instance_types.opensearch_catalog import get_catalog, catalog_meta

    version = effective_config(form.data, env).get("opensearch_engine_version", "3.3")
    force = request.method == "POST" and request.form.get("opensearch_catalog_refresh") == "1"
    options = {"force": force, "refresh_async": not current_app.testing}
    rows, stale = get_catalog(region, version, **options)
    target.catalog_meta = catalog_meta(region, version)
    target.catalog_version = version
    target.catalog_errors = {}
    if getattr(form, "supported_versions", []) and not form.version_meta.stale and version not in form.supported_versions:
        target.catalog_errors["opensearch_engine_version"] = "Select an AWS-supported OpenSearch version for this region."

    for role, type_name, count_name in (
        ("data", "opensearch_instance_type", "opensearch_instance_count"),
        ("master", "opensearch_master_type", "opensearch_master_count"),
    ):
        field = target[type_name]
        supported = sorted({row["InstanceType"] for row in rows or []
                            if role in row.get("InstanceRole", []) and row.get("InstanceType")})
        field.choices = [(value, value) for value in supported]
        if field.data and field.data not in supported:
            field.choices.append((field.data, f"{field.data} (not verified for this region/version)"))
            if rows and not stale:
                target.catalog_errors[type_name] = "Select an AWS-supported instance type for this region, version and node role."
        if not field.data:
            continue
        limits, limits_stale = get_catalog(region, version, field.data, **options)
        bounds = (limits or {}).get(role, {}).get("InstanceLimits", {}).get("InstanceCountLimits", {})
        count = target[count_name]
        if not bounds or limits_stale:
            continue
        minimum, maximum = bounds.get("MinimumInstanceCount"), bounds.get("MaximumInstanceCount")
        count.render_kw = dict(count.render_kw or {})
        for key, value in (("min", minimum), ("max", maximum)):
            if value is not None:
                count.render_kw[key] = value
        if isinstance(count.data, (int, float)) and (
            minimum is not None and count.data < minimum or
            maximum is not None and count.data > maximum
        ):
            target.catalog_errors[count_name] = f"AWS allows {minimum}–{maximum} {role} instances for this type and version."
