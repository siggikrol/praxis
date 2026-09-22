# forms/wizard/rancher_settings.py
from flask import request
from flask_wtf import FlaskForm
from wtforms import StringField, SelectField, FormField, TextAreaField, BooleanField
from wtforms.validators import DataRequired, ValidationError
from constants import (
    AWS_REGIONS,
    RANCHER_EKS_CLUSTERS,
    RANCHER_GITREPO_AUTH_TYPE,
    ENV_DEFAULT_ENV_TYPES,
    RANCHER_GITHUB_AUTH,
    RANCHER_PREFIXES,
    RANCHER_CLUSTER_TO_RANCHER_PREFIX,
    RANCHER_CLUSTER_TO_REGION,
    ENV_TYPE,
    MONITORING_TYPES,
)
from engine.wizards.forms.ux import get_env_key, get_env_label

class RancherEnvForm(FlaskForm):
    class Meta:
        csrf = False

    rancher_cluster_name = SelectField(
        "Rancher Cluster Name",
        choices=RANCHER_EKS_CLUSTERS,
        validators=[DataRequired()],
        default=RANCHER_EKS_CLUSTERS[0][0],
        description="Rancher EKS cluster name",
    )
    rancher_aws_region = SelectField(
        "AWS Region",
        choices=AWS_REGIONS,
        validators=[DataRequired()],
        default=AWS_REGIONS[0][0],
        description="Auto-set from selected cluster",
    )
    rancher_gitrepo_auth_type = SelectField(
        "Git Repo Auth Type",
        choices=RANCHER_GITREPO_AUTH_TYPE,
        validators=[DataRequired()],
        default="ssh",
        description="Auth type used for Rancher GitRepo access.",
    )
    rancher_github_auth = SelectField(
        "GitHub Auth Type",
        choices=RANCHER_GITHUB_AUTH,
        validators=[DataRequired()],
        default=RANCHER_GITHUB_AUTH[0][0],
        description="Rancher GitHub auth config name.",
    )
    helm_git_branch = StringField(
        "Helm Git Branch",
        validators=[DataRequired()],
        render_kw={"placeholder": "main"},
        description="Base Helm charts branch per env (dev/uat/prod).",
    )
    helm_app_git_branch = StringField(
        "Helm App Git Branch",
        validators=[DataRequired()],
        render_kw={"placeholder": "main"},
        description="Application Helm charts branch per env.",
    )
    helm_bootstrap_git_branch = StringField(
        "Helm Bootstrap Git Branch",
        validators=[DataRequired()],
        render_kw={"placeholder": "main"},
        description="Bootstrap repo branch used for Rancher bootstrap.",
    )

    istio = BooleanField(
        "Enable Istio",
        default=False,
        description="Controls Istio label for cluster resources.",
    )
    vault = BooleanField(
        "Enable Vault",
        default=False,
        description="Controls Vault label for cluster resources.",
    )
    aws_account_alias = StringField(
        "AWS Account Alias",
        validators=[DataRequired()],
        render_kw={"placeholder": "praxis-catalyst-sandbox"},
        description="AWS account alias used in Rancher labels; inherited from the selected Spacelift AWS integration.",
    )
    rancherPrefix = SelectField(
        "Rancher Prefix",
        choices=RANCHER_PREFIXES,
        validators=[DataRequired()],
        default=RANCHER_PREFIXES[0][0],
        description="Auto-set from selected cluster",
    )
    monitoring_type = SelectField(
        "Monitoring Type",
        choices=MONITORING_TYPES,
        validators=[DataRequired()],
        default=MONITORING_TYPES[0][0],
        description="Select monitoring deployment type Grafana OSS/Cloud",
    )
    multi_mesh = BooleanField(
        "Enable Multi Mesh",
        default=False,
        description="Controls multi-mesh label for cluster resources.",
    )
    dev_tools = BooleanField(
        "Dev Tools",
        default=False,
        description="Use dev tools labels for mock/3rd-party tooling.",
    )
    grafana_cloud_setup = BooleanField(
        "Enable Grafana Cloud",
        default=False,
        description=(
            "Sets the Grafana Cloud boolean required by the IAM roles module. "
            "This does not deploy Grafana from Studio."
        ),
    )
    env_type = SelectField(
        "Environment Type",
        choices=ENV_TYPE,
        validators=[DataRequired()],
        default=ENV_TYPE[0][0],
        description="Environment tier for labels (dev/staging/uat/etc).",
    )

    # Additional label fields (map to BASE_LABELS/EXTRA_LABELS in module)
    domain_name = StringField(
        "Domain Name",
        validators=[DataRequired()],
        render_kw={"placeholder": "example.com"},
        description="Base DNS suffix used for labels.",
    )
    common_env_name = StringField(
        "Common Environment Name",
        validators=[DataRequired()],
        render_kw={"placeholder": "nikola-apps"},
        description="Shared environment name used for labels.",
    )
    product = StringField(
        "Product",
        validators=[DataRequired()],
        render_kw={"placeholder": "PGP"},
        description="Product tag used for labels.",
    )
    customer = StringField(
        "Customer",
        validators=[DataRequired()],
        render_kw={"placeholder": "PRAXIS"},
        description="Customer tag used for labels.",
    )
    support_organization = StringField(
        "Support Organization",
        validators=[DataRequired()],
        render_kw={"placeholder": "PRAXIS"},
        description="Support org tag used for labels.",
    )

    # Settings flags (map to module boolean inputs) rendered as toggles
    fetch_waf = BooleanField("Fetch WAF", default=False, description="Lookup WAF details.")
    fetch_cert = BooleanField("Fetch Certificate", default=True, description="Lookup ACM certificate details.")
    fetch_vault_db = BooleanField("Fetch Vault DB", default=False, description="Lookup Vault DB details.")
    enable_meta = BooleanField("Enable Meta (labels/annotations)", default=True, description="Merge labels/annotations.")
    enable_kubeconfig_secret = BooleanField("Enable Kubeconfig Secret", default=False, description="Write kubeconfig to Secrets Manager.")
    include_rabbit_labels = BooleanField("Include Rabbit Labels", default=False, description="Add RabbitMQ annotation.")
    include_vault_labels = BooleanField("Include Vault Labels", default=False, description="Add Vault annotation.")
    include_rds_labels = BooleanField("Include RDS Labels", default=False, description="Add RDS annotation.")
    include_rds_endpoint = BooleanField("Include RDS Endpoint", default=False, description="Fetch and add RDS endpoint annotation.")
    include_vault_rds_labels = BooleanField("Include Vault RDS Labels", default=False, description="Add Vault RDS annotation.")
    include_redis_labels = BooleanField("Include Redis Labels", default=False, description="Add Redis annotation.")
    include_waf_labels = BooleanField("Include WAF Labels", default=False, description="Add WAF label if fetched.")

    # Optional annotations (free-form key:value, one per line)
    annotations = TextAreaField(
        "Annotations",
        description="Enter key:value pairs, one per line (e.g. runbook:https://wiki/... , oncall:pagerduty:team)",
        render_kw={
            "rows": 4,
            "placeholder": "runbook:https://wiki.example.com/runbooks/eks-imported-cluster\n"
                           "observability_dashboard:https://grafana.example.com/d/.../cluster\n"
                           "oncall_rotation:pagerduty:praxis-rancher-rotation"
        },
    )
    extra_labels = TextAreaField(
        "Extra Labels",
        description="Enter key:value pairs, one per line (e.g. owner:platform, cost_center:1234).",
        render_kw={
            "rows": 4,
            "placeholder": "owner:platform\ncost_center:1234",
        },
    )

    # Helpers for templating sections
    label_field_names = (
        "domain_name",
        "common_env_name",
        "env_type",
        "product",
        "customer",
        "support_organization",
        "aws_account_alias",
        "rancherPrefix",
        "monitoring_type",
    )
    settings_field_names = (
        "istio",
        "vault",
        "multi_mesh",
        "dev_tools",
        "grafana_cloud_setup",
        "fetch_waf",
        "fetch_cert",
        "fetch_vault_db",
        "enable_meta",
        "enable_kubeconfig_secret",
        "include_rabbit_labels",
        "include_vault_labels",
        "include_rds_labels",
        "include_rds_endpoint",
        "include_vault_rds_labels",
        "include_redis_labels",
        "include_waf_labels",
    )
    annotations_field_name = "annotations"

    def _apply_cluster_mappings(self):
        cluster = (self.rancher_cluster_name.data or "").strip()
        region = RANCHER_CLUSTER_TO_REGION.get(cluster)
        if region:
            self.rancher_aws_region.data = region
        prefix = RANCHER_CLUSTER_TO_RANCHER_PREFIX.get(cluster)
        if prefix:
            self.rancherPrefix.data = prefix

    def validate(self, **kwargs):
        self._apply_cluster_mappings()
        return super().validate(**kwargs)

    def _validate_kv_lines(self, field, *, label: str):
        raw = (field.data or "").strip()
        if not raw:
            return
        bad_lines = []
        for idx, line in enumerate(raw.splitlines(), start=1):
            line = line.strip()
            if not line:
                continue
            if ":" not in line:
                bad_lines.append(idx)
                continue
            key, value = line.split(":", 1)
            if not key.strip() or not value.strip():
                bad_lines.append(idx)
        if bad_lines:
            lines = ", ".join(str(i) for i in bad_lines)
            raise ValidationError(
                f"{label} must be key:value pairs, one per line. Fix line(s): {lines}."
            )

    def validate_annotations(self, field):
        self._validate_kv_lines(field, label="Annotations")

    def validate_extra_labels(self, field):
        self._validate_kv_lines(field, label="Extra Labels")


class RancherSettingsForm(FlaskForm):
    @staticmethod
    def _wizard_env_slug() -> str:
        try:
            bp = (request.blueprint or "").strip()
        except RuntimeError:
            return ""
        if bp.endswith("_wizard"):
            return bp[: -len("_wizard")]
        return bp

    @classmethod
    def for_envs(cls, env_types):
        env_types = env_types or ENV_DEFAULT_ENV_TYPES
        is_multi_vpc = cls._wizard_env_slug() == "multi-vpc"

        class RancherEnvFormDynamic(RancherEnvForm):
            multi_mesh = BooleanField(
                "Enable Multi Mesh",
                default=is_multi_vpc,
                description="Controls multi-mesh label for cluster resources.",
            )

        attrs = {
            get_env_key(env): FormField(
                RancherEnvFormDynamic,
                description=f"Rancher settings for {get_env_label(env)}",
            )
            for env in env_types
        }

        def _init(self, *args, **kwargs):
            common_defaults = kwargs.pop("common_defaults", {}) or {}
            super(RancherSettingsForm, self).__init__(*args, **kwargs)

            for env_key, field in self._fields.items():
                if not isinstance(field, FormField) or not hasattr(field, "form"):
                    continue
                sub = field.form
                env_name = str(env_key).strip().lower()

                for name in ("helm_git_branch", "helm_app_git_branch", "helm_bootstrap_git_branch"):
                    fld = getattr(sub, name, None)
                    if not fld:
                        continue
                    val = (fld.data or "").strip()
                    if not val:
                        fld.data = env_name

                # Align env_type to the project settings environment selection.
                env_field = getattr(sub, "env_type", None)
                if env_field is not None:
                    # Extract the base environment name (e.g., 'staging' from 'staging-c')
                    # This ensures labels.env_type in Rancher stays without the suffix.
                    base_env_name = env_name.split("-")[0] if "-" in env_name else env_name
                    
                    choices = list(env_field.choices or [])
                    choice_values = [c[0] for c in choices]
                    if base_env_name not in choice_values:
                        choices.insert(0, (base_env_name, base_env_name))
                        env_field.choices = choices
                    env_field.data = base_env_name

                # inherit common settings into label fields if empty
                if hasattr(sub, "domain_name") and not sub.domain_name.data:
                    sub.domain_name.data = common_defaults.get("domain_name", "")
                if hasattr(sub, "common_env_name") and not sub.common_env_name.data:
                    sub.common_env_name.data = common_defaults.get("environment", "")
                if hasattr(sub, "product") and not sub.product.data:
                    sub.product.data = common_defaults.get("product", "")
                if hasattr(sub, "customer") and not sub.customer.data:
                    sub.customer.data = common_defaults.get("customer", "")
                if hasattr(sub, "support_organization") and not sub.support_organization.data:
                    sub.support_organization.data = common_defaults.get("support_organization", "")

                if hasattr(sub, "_apply_cluster_mappings"):
                    sub._apply_cluster_mappings()

        dyn = type("RancherSettingsFormDynamic", (cls,), attrs)
        dyn.__init__ = _init
        return dyn
