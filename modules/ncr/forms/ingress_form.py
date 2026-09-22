from flask_wtf import FlaskForm
from wtforms import (
    StringField, TextAreaField, SelectField, FieldList, FormField
)
from wtforms.validators import DataRequired, Regexp, Optional
from modules.ncr.constants import (
    DOMAIN_PATTERN,
    ENV_NAME_REGEX,
    ENV_SEGMENT_PATTERN,
    PLATFORM_CHOICES,
    build_environment_slug,
    resolve_environment_fields,
)
from .endpoint_form import EndpointForm


def _normalize_value(value):
    return value.strip().lower() if isinstance(value, str) else value

class IngressForm(FlaskForm):
    platform = SelectField(
        "Platform",
        choices=PLATFORM_CHOICES,
        validators=[DataRequired()],
        default="catalyst",
    )

    environment_prefix = StringField(
        "Environment prefix",
        validators=[
            DataRequired(),
            Regexp(
                ENV_SEGMENT_PATTERN,
                message="Use lowercase letters, numbers, and hyphens in the environment prefix.",
            ),
        ],
        filters=[_normalize_value],
        render_kw={"placeholder": "ak"},
    )

    environment_suffix = StringField(
        "Environment suffix",
        validators=[
            DataRequired(),
            Regexp(
                ENV_SEGMENT_PATTERN,
                message="Use lowercase letters, numbers, and hyphens in the environment suffix.",
            ),
        ],
        filters=[_normalize_value],
        render_kw={"placeholder": "staging"},
    )

    environment = StringField(
        "Environment slug",
        validators=[Optional()],
        filters=[_normalize_value],
        render_kw={
            "placeholder": "ak-ctlst-staging",
            "readonly": True,
        },
    )

    domain = StringField(
        "Domain",
        validators=[DataRequired(), Regexp(DOMAIN_PATTERN)],
        render_kw={"placeholder": "example.com"},
    )

    outgoing_paysafe_allowed = SelectField(
        "",
        choices=[("unknown", "Unknown"), ("yes", "Yes"), ("no", "No")],
    )

    port_8080_cross_vpc = SelectField(
        "",
        choices=[("true", "Enabled"), ("false", "Disabled")],
    )

    eu_ngl_dev_ad_access = SelectField(
        "",
        choices=[("true", "Enabled"), ("false", "Disabled")],
    )

    ad_protocol_url = StringField(
        "AD Protocol URL",
        validators=[DataRequired()],
    )

    shared_vault_url = StringField(
        "Shared Vault URL",
        validators=[DataRequired()],
    )

    allow_ips = TextAreaField(
        "Allow IPs",
        validators=[Optional()],
    )

    endpoints = FieldList(FormField(EndpointForm), min_entries=0)

    def __init__(self, *args, **kwargs):
        from modules.ncr.networking import load_profile
        profile = load_profile("catalyst-common")["profile"]
        defaults = profile["defaults"]
        data = dict(kwargs.pop("data", None) or {})
        for field, value in {**profile['builder']['ingress_defaults'], "ad_protocol_url": defaults["ad_url"], "shared_vault_url": defaults["vault_url"],
                             "allow_ips": "\n".join(defaults["allow_ips"])}.items():
            data.setdefault(field, value)
        kwargs["data"] = data
        super().__init__(*args, **kwargs)
        for field, label in profile['builder']['ingress_labels'].items():
            self[field].label.text = label
        self._sync_environment_fields()

    def _sync_environment_fields(self):
        platform, prefix, suffix = resolve_environment_fields(
            self.platform.data,
            self.environment_prefix.data,
            self.environment_suffix.data,
            self.environment.data,
        )
        self.platform.data = platform
        self.environment_prefix.data = prefix
        self.environment_suffix.data = suffix
        self.environment.data = build_environment_slug(platform, prefix, suffix)

    def validate(self, extra_validators=None):
        self._sync_environment_fields()
        valid = super().validate(extra_validators=extra_validators)
        self._sync_environment_fields()
        if self.environment.data and not ENV_NAME_REGEX.match(self.environment.data):
            self.environment.errors.append(
                "Generated environment slug is invalid for the selected platform.",
            )
            return False
        return valid

    def seed_defaults(self, default_list):
        if self.endpoints.entries:
            return
        for ns, name, gws, tpl in default_list:
            self.endpoints.append_entry({
                "namespace": ns,
                "name": name,
                "gateways": gws,
                "hosts": "",
                "host_template": tpl,
            })
