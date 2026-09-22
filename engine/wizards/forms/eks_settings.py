import os

from flask import request, session
from flask_wtf import FlaskForm
from wtforms import (
    BooleanField,
    FieldList,
    FormField,
    HiddenField,
    SelectField,
    SelectMultipleField,
    StringField,
    TextAreaField,
)
from wtforms.validators import DataRequired, NumberRange, Optional
from .fields import NumberInputField
from engine.wizards.factory.utils import _key
from engine.wizards.constants.eks_constants import (
    EKS_DEFAULTS,
    EKS_CLUSTER_VERSION_GROUPS,
    EKS_CLUSTER_VERSIONS,
    EKS_INSTANCE_TYPES,
    EKS_INSTANCE_TYPE_GROUPS,
    EKS_TAINT_EFFECTS,
    EKS_VALIDATION_LIMITS,
)
from engine.wizards.constants.project_constants import ENV_DEFAULT_ENV_TYPES
from engine.wizards.forms.ux import get_env_key, get_env_label
from services.aws_instance_types.eks_ami import get_eks_ami_id
from services.aws_instance_types.eks_cluster_versions import get_eks_cluster_version_options
from services.aws_instance_types.eks_instance_types import get_eks_instance_type_options


def _split_instance_types(raw: str) -> list[str]:
    if isinstance(raw, (list, tuple, set)):
        return [str(v).strip() for v in raw if str(v).strip()]
    if not isinstance(raw, str):
        return []
    parts = [p.strip() for p in raw.replace("\n", ",").split(",")]
    return [p for p in parts if p]


def _preserve_dynamic_choices(field, groups, choices) -> None:
    """Keep saved/submitted values selectable when a live catalog changes."""
    current = field.data or []
    if not isinstance(current, (list, tuple, set)):
        current = [current]
    current = [str(value).strip() for value in current if str(value).strip()]
    available = {str(value) for value, _label in (choices or [])}
    missing = [(value, f"{value} (saved selection)") for value in current if value not in available]
    field.choices = missing + list(choices or [])
    field.option_groups = (
        ([("Saved selection", missing)] if missing else [])
        + list(groups or [])
    )


def _parse_kv_lines(raw: str, parts: int) -> tuple[list[tuple[str, ...]], list[str]]:
    if not isinstance(raw, str) or not raw.strip():
        return [], []
    rows = []
    errors = []
    for line in raw.splitlines():
        line = line.strip()
        if not line:
            continue
        segs = [s.strip() for s in line.split(":")]
        segs = [s for s in segs if s != ""]
        if len(segs) != parts:
            errors.append(f"Invalid line: '{line}'")
            continue
        rows.append(tuple(segs))
    return rows, errors


TAINTS_HELP_TEXT = (
    "Format: key: value: EFFECT (e.g. dedicated: vault: NO_SCHEDULE). "
    "One taint per line. Allowed effects: NO_SCHEDULE, PREFER_NO_SCHEDULE, NO_EXECUTE."
)

TAINTS_RENDER_KW = {
    "placeholder": "key: value: EFFECT",
    "rows": 3,
}

SINGLE_VPC_REQUIRED_NODE_SG_RULES = (
    {
        "name": "vault_access",
        "description": "Cluster to node - Vault Access",
        "type": "ingress",
        "protocol": "tcp",
        "from_port": 8080,
        "to_port": 8080,
        "source_type": "source_cluster_security_group",
    },
    {
        "name": "ingress_15012",
        "description": "Istio xDS",
        "type": "ingress",
        "protocol": "tcp",
        "from_port": 15012,
        "to_port": 15012,
        "source_type": "source_cluster_security_group",
    },
    {
        "name": "ingress_15017",
        "description": "Istio sidecar injector webhook",
        "type": "ingress",
        "protocol": "tcp",
        "from_port": 15017,
        "to_port": 15017,
        "source_type": "source_cluster_security_group",
    },
)

MULTI_VPC_EKS_CLUSTERS = ("ilp", "cgs", "pmv")


def required_node_sg_rule_form_defaults() -> list[dict]:
    """Return fresh WTForms data for the node SG rules required on EKS clusters."""
    return [
        {
            **rule,
            "source_values": "",
            "source_security_group_id": "",
        }
        for rule in SINGLE_VPC_REQUIRED_NODE_SG_RULES
    ]


def _wizard_slug_from_blueprint(blueprint_name: str | None) -> str:
    bp = (blueprint_name or "").strip()
    # Nested blueprints are reported as "parent.child_wizard". Only the
    # child name identifies the wizard slug used by the form logic.
    bp = bp.rsplit(".", 1)[-1]
    if bp.endswith("_wizard"):
        return bp[: -len("_wizard")]
    return bp


def _wizard_env_slug() -> str:
    try:
        return _wizard_slug_from_blueprint(request.blueprint)
    except RuntimeError:
        return ""


def _selected_aws_region(default: str = "us-east-1") -> str:
    """
    Resolve region from the wizard's Common step (session), falling back to env/default.
    """
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


def _selected_eks_cluster_version(default: str | None = None) -> str:
    fallback = (default or EKS_DEFAULTS["cluster_version"] or "").strip()
    try:
        posted = (request.form.get("eks_cluster_version") or "").strip()
        if posted:
            return posted
        env_slug = _wizard_env_slug()
        if env_slug:
            eks_cfg = session.get(_key(env_slug, "eks"), {}) or {}
            saved = (eks_cfg.get("eks_cluster_version") or "").strip()
            if saved:
                return saved
    except Exception:
        pass
    return fallback


def _selected_ami_type(default: str | None = None) -> str:
    fallback = (default or EKS_DEFAULTS["ami_type"] or "").strip()
    try:
        posted = (request.form.get("ami_type") or "").strip()
        if posted:
            return posted
        env_slug = _wizard_env_slug()
        if env_slug:
            eks_cfg = session.get(_key(env_slug, "eks"), {}) or {}
            saved = (eks_cfg.get("ami_type") or "").strip()
            if saved:
                return saved
    except Exception:
        pass
    return fallback


class ManagedNodeGroupForm(FlaskForm):
    class Meta:
        csrf = False

    name = StringField("Name", validators=[DataRequired()])
    instance_types = SelectMultipleField(
        "Instance Types",
        choices=[],
        validators=[DataRequired()],
        description="Select one or more instance types.",
        render_kw={"data-eks-instance-select": "1"},
    )
    min_size = NumberInputField(
        "Min Size", validators=[DataRequired(), NumberRange(min=0)]
    )
    max_size = NumberInputField(
        "Max Size", validators=[DataRequired(), NumberRange(min=0)]
    )
    desired_size = NumberInputField(
        "Desired Size", validators=[DataRequired(), NumberRange(min=0)]
    )
    capacity_type = SelectField(
        "Capacity Type",
        choices=[("", "Default"), ("ON_DEMAND", "ON_DEMAND"), ("SPOT", "SPOT")],
        default="",
    )
    disk_size = NumberInputField(
        "Disk Size (GiB)", validators=[Optional(), NumberRange(min=1)]
    )
    iam_role_use_name_prefix = BooleanField(
        "Use IAM Role Name Prefix",
        default=False,
        description="When enabled, Terraform may add a generated suffix to the node group IAM role name.",
    )
    ami_id = StringField(
        "AMI ID Override",
        validators=[Optional()],
        description=(
            "Optional custom AMI ID for this node group. Leave blank to use the "
            "default node group AMI ID."
        ),
    )
    labels = TextAreaField(
        "Labels",
        description="One per line: key: value",
    )
    taints = TextAreaField(
        "Taints",
        description=TAINTS_HELP_TEXT,
        render_kw=TAINTS_RENDER_KW,
    )


class NodeSecurityGroupRuleForm(FlaskForm):
    class Meta:
        csrf = False

    name = StringField("Rule Name", validators=[DataRequired()])
    description = StringField("Description", validators=[Optional()])
    type = SelectField(
        "Direction",
        choices=[("ingress", "ingress"), ("egress", "egress")],
        default="ingress",
        validators=[DataRequired()],
    )
    protocol = SelectField(
        "Protocol",
        choices=[("tcp", "tcp"), ("udp", "udp"), ("icmp", "icmp"), ("-1", "-1 (all)")],
        default="tcp",
        validators=[DataRequired()],
    )
    from_port = NumberInputField(
        "From Port",
        validators=[DataRequired(), NumberRange(min=0, max=65535)],
    )
    to_port = NumberInputField(
        "To Port",
        validators=[DataRequired(), NumberRange(min=0, max=65535)],
    )
    source_type = SelectField(
        "Source Type",
        choices=[
            ("source_cluster_security_group", "Cluster Security Group"),
            ("self", "Self"),
            ("source_node_security_group", "Node Security Group"),
            ("cidr_blocks", "CIDR Blocks"),
            ("ipv6_cidr_blocks", "IPv6 CIDR Blocks"),
            ("prefix_list_ids", "Prefix List IDs"),
            ("source_security_group_id", "Security Group ID"),
        ],
        default="source_cluster_security_group",
        validators=[DataRequired()],
    )
    source_values = TextAreaField(
        "Source Values",
        validators=[Optional()],
        description="Use newline/comma separated values for CIDR/IPv6 CIDR/Prefix List types.",
        render_kw={"rows": 3},
    )
    source_security_group_id = StringField(
        "Source Security Group ID",
        validators=[Optional()],
        description="Required only when Source Type = Security Group ID.",
    )


class EksEnvForm(FlaskForm):
    class Meta:
        csrf = False

    default_node_group_enabled = BooleanField(
        "Enable Default Node Group", default=EKS_DEFAULTS["default_node_group"]["enabled"]
    )
    default_node_group_name = StringField(
        "Name", default=EKS_DEFAULTS["default_node_group"]["name"]
    )
    default_node_group_instance_types = SelectMultipleField(
        "Instance Types",
        choices=[],
        default=[EKS_DEFAULTS["default_node_group"]["instance_types"]],
        description="Select one or more instance types.",
        render_kw={"data-eks-instance-select": "1"},
    )
    default_node_group_min_size = NumberInputField(
        "Min Size",
        validators=[Optional(), NumberRange(min=0)],
        default=EKS_DEFAULTS["default_node_group"]["min_size"],
    )
    default_node_group_max_size = NumberInputField(
        "Max Size",
        validators=[Optional(), NumberRange(min=0)],
        default=EKS_DEFAULTS["default_node_group"]["max_size"],
    )
    default_node_group_desired_size = NumberInputField(
        "Desired Size",
        validators=[Optional(), NumberRange(min=0)],
        default=EKS_DEFAULTS["default_node_group"]["desired_size"],
    )
    default_node_group_capacity_type = SelectField(
        "Capacity Type",
        choices=[("", "Default"), ("ON_DEMAND", "ON_DEMAND"), ("SPOT", "SPOT")],
        default="",
    )
    default_node_group_disk_size = NumberInputField(
        "Disk Size (GiB)", validators=[Optional(), NumberRange(min=1)]
    )
    default_node_group_iam_role_use_name_prefix = BooleanField(
        "Use IAM Role Name Prefix",
        default=False,
        description="When enabled, Terraform may add a generated suffix to the default node group IAM role name.",
    )
    default_node_group_ami_id = StringField(
        "AMI ID",
        validators=[DataRequired()],
        description=(
            "Required region-specific EKS optimized AMI ID inherited by additional "
            "node groups unless they override it."
        ),
    )
    default_node_group_ami_auto = HiddenField(default="")
    default_node_group_labels = TextAreaField(
        "Labels",
        description="One per line: key: value",
    )
    default_node_group_taints = TextAreaField(
        "Taints",
        description=TAINTS_HELP_TEXT,
        render_kw=TAINTS_RENDER_KW,
    )

    additional_node_groups = FieldList(FormField(ManagedNodeGroupForm), min_entries=0)
    additional_node_security_group_rules = FieldList(FormField(NodeSecurityGroupRuleForm), min_entries=0)
    bypass_node_security_group_rule_setup = BooleanField(
        "I would like to bypass the security rule setup"
    )
    bypass_node_security_group_rule_setup_acknowledged = HiddenField(default="")

    def __init__(self, *args, **kwargs):
        initial_data = kwargs.get("data")
        super().__init__(*args, **kwargs)

        # Seed new Single-VPC and Multi-VPC EKS cards with the required Vault
        # and Istio rules. Preserve an explicitly saved list (including an
        # intentionally empty/bypassed list) when revisiting the step.
        should_seed_rules = (
            _wizard_env_slug() in {"single-vpc", "multi-vpc"}
            and (not isinstance(initial_data, dict) or "additional_node_security_group_rules" not in initial_data)
            and not self.is_submitted()
            and not self.additional_node_security_group_rules.entries
        )
        if should_seed_rules:
            for rule in required_node_sg_rule_form_defaults():
                self.additional_node_security_group_rules.append_entry(rule)

        region = _selected_aws_region()
        groups, choices, meta = get_eks_instance_type_options(
            region=region,
            fallback_groups=EKS_INSTANCE_TYPE_GROUPS,
            fallback_choices=EKS_INSTANCE_TYPES,
        )
        ami_id, ami_meta = get_eks_ami_id(
            region=region,
            cluster_version=_selected_eks_cluster_version(),
            ami_type=_selected_ami_type(),
        )
        # Optional: templates can introspect this for debugging.
        self._eks_instance_types_meta = meta
        self._eks_ami_meta = ami_meta

        if ami_id:
            # Track the last automatically resolved value. This lets a new
            # recommended AMI replace an older automatic value while preserving
            # a value that the user deliberately changed.
            default_ami = self.default_node_group_ami_id
            auto_ami = self.default_node_group_ami_auto
            current = str(default_ami.data or "").strip()
            previous_auto = str(auto_ami.data or "").strip()
            if not current or not previous_auto or current == previous_auto:
                default_ami.data = ami_id
            auto_ami.data = ami_id
            rk = default_ami.render_kw = dict(default_ami.render_kw or {})
            rk.setdefault("placeholder", ami_id)
            for group in self.additional_node_groups:
                override = group.form.ami_id
                override_rk = override.render_kw = dict(override.render_kw or {})
                override_rk.setdefault("placeholder", ami_id)

        _preserve_dynamic_choices(
            self.default_node_group_instance_types,
            groups,
            choices,
        )
        for group in self.additional_node_groups:
            _preserve_dynamic_choices(group.form.instance_types, groups, choices)

    def _validate_sizes(self, mn, mx, ds, target_field):
        if mn is None or mx is None or ds is None:
            target_field.errors.append("Min, Max, and Desired are required.")
            return False
        if not (mn <= ds <= mx):
            target_field.errors.append("Desired must be between Min and Max.")
            return False
        return True

    def _validate_kv_fields(self, raw, expected_parts, field, label):
        rows, errors = _parse_kv_lines(raw, expected_parts)
        if errors:
            field.errors.append(f"{label} must be formatted as {expected_parts} colon-separated values.")
            return False
        return True

    def _validate_taints(self, raw, field):
        rows, errors = _parse_kv_lines(raw, 3)
        if errors:
            field.errors.append(
                "Taints must use three colon-separated values per line: "
                "key: value: EFFECT. Example: dedicated: vault: NO_SCHEDULE. "
                "Do not use key:value only."
            )
            return False
        valid = True
        for _, _, effect in rows:
            if effect not in EKS_TAINT_EFFECTS:
                field.errors.append(
                    f"Invalid taint effect '{effect}'. Use one of: {', '.join(EKS_TAINT_EFFECTS)}. "
                    "Example: dedicated: vault: NO_SCHEDULE."
                )
                valid = False
        return valid

    def _parse_list_values(self, raw):
        if not isinstance(raw, str):
            return []
        parts = [p.strip() for p in raw.replace("\n", ",").split(",")]
        return [p for p in parts if p]

    def missing_single_vpc_required_node_sg_rules(self) -> list[dict]:
        """Return required single-VPC node SG rules not present with the expected shape."""
        configured = []
        for rule in self.additional_node_security_group_rules or []:
            form = rule.form
            configured.append(
                {
                    "name": (form.name.data or "").strip(),
                    "type": (form.type.data or "").strip(),
                    "protocol": (form.protocol.data or "").strip(),
                    "from_port": form.from_port.data,
                    "to_port": form.to_port.data,
                    "source_type": (form.source_type.data or "").strip(),
                }
            )

        def _matches(required: dict, candidate: dict) -> bool:
            return all(
                candidate.get(field) == required[field]
                for field in ("name", "type", "protocol", "from_port", "to_port", "source_type")
            )

        return [
            required
            for required in SINGLE_VPC_REQUIRED_NODE_SG_RULES
            if not any(_matches(required, candidate) for candidate in configured)
        ]

    def has_acknowledged_node_sg_rule_bypass(self) -> bool:
        if not bool(self.bypass_node_security_group_rule_setup.data):
            return False
        acknowledged = str(
            self.bypass_node_security_group_rule_setup_acknowledged.data or ""
        ).strip().lower()
        return acknowledged in {"1", "true", "yes", "on"}

    def validate(self, **kwargs):
        valid = super().validate(**kwargs)

        default_enabled = bool(self.default_node_group_enabled.data)
        default_name = (self.default_node_group_name.data or "").strip()
        default_types_raw = self.default_node_group_instance_types.data or ""
        default_types = _split_instance_types(default_types_raw)
        default_min = self.default_node_group_min_size.data
        default_max = self.default_node_group_max_size.data
        default_desired = self.default_node_group_desired_size.data

        if default_enabled:
            if not default_name:
                self.default_node_group_name.errors.append("Name is required.")
                valid = False
            if not default_types:
                self.default_node_group_instance_types.errors.append(
                    "At least one instance type is required."
                )
                valid = False
            if not self._validate_sizes(default_min, default_max, default_desired, self.default_node_group_desired_size):
                valid = False
            if not self._validate_kv_fields(self.default_node_group_labels.data, 2, self.default_node_group_labels, "Labels"):
                valid = False
            if not self._validate_taints(self.default_node_group_taints.data, self.default_node_group_taints):
                valid = False

        # Additional node groups validation
        additional_groups = self.additional_node_groups or []
        for group in additional_groups:
            g = group.form
            g_types = _split_instance_types(g.instance_types.data or "")
            if not g_types:
                g.instance_types.errors.append("At least one instance type is required.")
                valid = False
            if not self._validate_sizes(g.min_size.data, g.max_size.data, g.desired_size.data, g.desired_size):
                valid = False
            if not self._validate_kv_fields(g.labels.data, 2, g.labels, "Labels"):
                valid = False
            if not self._validate_taints(g.taints.data, g.taints):
                valid = False

        # Default disabled requires at least one additional group
        if not default_enabled:
            named_groups = [
                (group.form.name.data or "").strip()
                for group in additional_groups
                if (group.form.name.data or "").strip()
            ]
            if not named_groups:
                self.default_node_group_enabled.errors.append(
                    "Disable default node group requires at least one additional node group."
                )
                valid = False

        # Unique names across default + additional
        names = []
        if default_enabled and default_name:
            names.append(("default", default_name, self.default_node_group_name))
        for idx, group in enumerate(additional_groups):
            gname = (group.form.name.data or "").strip()
            if gname:
                names.append((f"additional[{idx}]", gname, group.form.name))

        seen = {}
        for _, name, field in names:
            key = name.lower()
            if key in seen:
                field.errors.append("Node group names must be unique.")
                seen[key].errors.append("Node group names must be unique.")
                valid = False
            else:
                seen[key] = field

        # Additional SG rules validation
        sg_rules = self.additional_node_security_group_rules or []
        sg_seen = {}
        list_source_types = {"cidr_blocks", "ipv6_cidr_blocks", "prefix_list_ids"}
        for idx, rule in enumerate(sg_rules):
            r = rule.form
            name = (r.name.data or "").strip()
            source_type = (r.source_type.data or "").strip()
            values = self._parse_list_values(r.source_values.data or "")
            sg_id = (r.source_security_group_id.data or "").strip()

            if r.from_port.data is None or r.to_port.data is None:
                # Attach errors to the specific missing fields so the UI can highlight them correctly.
                if r.from_port.data is None:
                    r.from_port.errors.append("From Port is required.")
                if r.to_port.data is None:
                    r.to_port.errors.append("To Port is required.")
                valid = False
            elif r.from_port.data > r.to_port.data:
                # Attach ordering errors to both fields since both values participate in the validation.
                msg = "To Port must be greater than or equal to From Port."
                r.from_port.errors.append(msg)
                r.to_port.errors.append(msg)
                valid = False

            if source_type in list_source_types and not values:
                r.source_values.errors.append("Source Values are required for the selected Source Type.")
                valid = False

            if source_type == "source_security_group_id" and not sg_id:
                r.source_security_group_id.errors.append(
                    "Source Security Group ID is required for the selected Source Type."
                )
                valid = False

            if name:
                key = name.lower()
                if key in sg_seen:
                    r.name.errors.append("Rule names must be unique.")
                    sg_seen[key].errors.append("Rule names must be unique.")
                    valid = False
                else:
                    sg_seen[key] = r.name
            else:
                r.name.errors.append("Rule Name is required.")
                valid = False

        if _wizard_env_slug() == "single-vpc":
            missing_required_rules = self.missing_single_vpc_required_node_sg_rules()
            if missing_required_rules and not self.has_acknowledged_node_sg_rule_bypass():
                if self.bypass_node_security_group_rule_setup.data:
                    self.bypass_node_security_group_rule_setup.errors.append(
                        "Confirm the bypass warning before continuing without the required security group rules."
                    )
                else:
                    self.additional_node_security_group_rules.errors.append(
                        "Single-VPC requires the Vault and Istio node security group rules shown below."
                    )
                valid = False

        return valid


class EksSettingsForm(FlaskForm):
    eks_cluster_version = SelectField(
        "EKS Version",
        choices=EKS_CLUSTER_VERSIONS,
        default=EKS_DEFAULTS["cluster_version"],
    )
    eks_ssh_allow_access_from_cidrs = StringField(
        "SSH Access CIDRs", default=EKS_DEFAULTS["ssh_allow_access_from_cidrs"]
    )
    eks_endpoint_private_access = BooleanField(
        "Private API Endpoint", default=EKS_DEFAULTS["endpoint_private_access"]
    )
    eks_endpoint_public_access = BooleanField(
        "Public API Endpoint", default=EKS_DEFAULTS["endpoint_public_access"]
    )
    eks_endpoint_public_access_cidrs = StringField(
        "Public Access CIDRs", default=EKS_DEFAULTS["endpoint_public_access_cidrs"]
    )
    eks_api_allow_access_from_cidrs = StringField(
        "EKS API Allow Access CIDRs", default=EKS_DEFAULTS["api_allow_access_from_cidrs"]
    )
    eks_systems_administrators_role = StringField(
        "Systems Administrators Role", default=EKS_DEFAULTS["systems_admin_role"]
    )
    ami_type = SelectField(
        "AMI Type",
        choices=[("amazon-linux-2023", "amazon-linux-2023"), ("bottlerocket", "bottlerocket")],
        default=EKS_DEFAULTS["ami_type"],
    )
    enable_load_balancer_controller_irsa = SelectField(
        "Create AWS Load Balancer Controller IRSA Role",
        choices=[("true", "true"), ("false", "false")],
        default="false",
        description="Whether to create AWS Load Balancer Controller IRSA Role",
    )
    enable_cluster_autoscaler_irsa = SelectField(
        "Create AutoScale IRSA Role",
        choices=[("true", "true"), ("false", "false")],
        default="false",
        description="Whether to create AutoScaler IRSA Role",
    )
    enable_external_secrets_irsa = SelectField(
        "Create External Secrets IRSA Role",
        choices=[("true", "true"), ("false", "false")],
        default="false",
        description="Whether to create External Secrets IRSA Role",
    )
    enable_external_dns_irsa = SelectField(
        "Create External DNS IRSA Role",
        choices=[("true", "true"), ("false", "false")],
        default="false",
        description="Whether to create External DNS IRSA Role",
    )
    enable_rancher_access = SelectField(
        "Rancher User Access",
        choices=[("true", "true"), ("false", "false")],
        default="false",
        description="Allow Rancher user access to be able to import eks cluster into rancher instance",
    )
    def __init__(self, *args, **kwargs):
        initial_data = kwargs.get("data")
        super().__init__(*args, **kwargs)
        region = _selected_aws_region()
        groups, choices, meta = get_eks_cluster_version_options(
            region=region,
            fallback_groups=EKS_CLUSTER_VERSION_GROUPS,
            fallback_choices=EKS_CLUSTER_VERSIONS,
        )
        self._eks_cluster_versions_meta = meta
        saved_version = (
            str(initial_data.get("eks_cluster_version") or "").strip()
            if isinstance(initial_data, dict)
            else ""
        )
        if not self.is_submitted() and not saved_version:
            allowed = {v for v, _ in (choices or [])}
            desired_default = str(EKS_DEFAULTS.get("cluster_version") or "").strip()
            if desired_default and desired_default not in allowed:
                desired_default = ""
            if not desired_default:
                if groups:
                    # Prefer the newest Standard Support version when possible.
                    for label, opts in groups:
                        if label == "Standard Support" and opts:
                            desired_default = str(opts[0][0])
                            break
            if not desired_default and choices:
                desired_default = str(choices[0][0])
            if desired_default:
                self.eks_cluster_version.data = desired_default
        _preserve_dynamic_choices(self.eks_cluster_version, groups, choices)

    @classmethod
    def for_envs(cls, env_types, clusters=None):
        env_types = env_types or ENV_DEFAULT_ENV_TYPES

        if clusters:
            attrs = {
                f"{get_env_key(env)}_{cluster}": FormField(
                    EksEnvForm,
                    description=f"EKS settings for {get_env_label(env)} / {cluster.upper()}",
                )
                for env in env_types
                for cluster in clusters
            }
        else:
            attrs = {
                get_env_key(env): FormField(EksEnvForm, description=f"EKS settings for {get_env_label(env)}")
                for env in env_types
            }
        attrs["_env_fields_resolved"] = True
        return type("EksSettingsFormDynamic", (cls,), attrs)
