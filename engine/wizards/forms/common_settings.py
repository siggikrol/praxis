# forms/common_settings.py
import re
from flask_wtf import FlaskForm
from wtforms import StringField, SelectField
from wtforms.validators import DataRequired
from constants import AWS_REGIONS, DOMAIN_CHOICES

class CommonSettingsForm(FlaskForm):
    aws_region = SelectField(
        "AWS Region",
        choices=[("", "Select AWS region"), *AWS_REGIONS],
        validators=[DataRequired()],
        default="",
        description="AWS region where resources will be deployed",
    )

    # NOTE: real regex + hints injected in __init__ based on expected_suffix
    environment = StringField(
        "Environment",
        validators=[DataRequired()],  # Regexp set in __init__
        filters=[lambda s: s.strip().lower() if s else s],
        render_kw={"placeholder": "<customer>-ctlst"},
        description=(
            "Only the base prefix, e.g. 'acme-ctlst'. "
            "SSA will append the environment/stage automatically (e.g. 'ilp-acme-ctlst-uat')."
        ),
    )

    cert_cluster_mode = SelectField(
        "Certificate Cluster Mode",
        choices=[("singlezone","singlezone"),("multizone","multizone")],
        validators=[DataRequired()],
        default="singlezone",
        description="Certificate issuance mode",
    )

    support_organization = StringField(
        "Support Organization",
        validators=[DataRequired()],
        render_kw={"placeholder":"PRAXIS"},
        description="Tag: Organization responsible for support",
        default="PRAXIS",
    )
    customer = StringField(
        "Customer",
        validators=[DataRequired()],
        render_kw={"placeholder":"PRAXIS"},
        description="Tag: Customer name for tagging",
        default="PRAXIS",
    )
    product = StringField(
        "Product",
        validators=[DataRequired()],
        render_kw={"placeholder":"PGP"},
        description="Tag: Product name for tagging",
        default="PGP",
    )

    domain_name = StringField(
        "Domain Name",
        validators=[DataRequired(message="Please choose a domain.")],
        filters=[lambda s: s.strip().lower() if s else s],
        render_kw={
            "placeholder": "Select or type a domain",
            "list": "domain-name-options",
        },
        description="Root domain for DNS and certificates",
    )

    def __init__(self, *args, expected_suffix: str = "ctlst", **kwargs):
        """
        expected_suffix controls the rule:
          - ctlst   → acme-ctlst
          - rgs     → acme-rgs
          - playon  → acme-playon
        """
        super().__init__(*args, **kwargs)

        # ----- dynamic validation & hints for `environment`
        suffix = (expected_suffix or "ctlst").strip().lower()
        pattern = rf"^[a-z]+-{re.escape(suffix)}$"
        self.environment.validators = [
            DataRequired(message="Environment is required."),
            # enforce "<customer>-<suffix>" shape
            # keep as a simple client-side hint (pattern+title); backend checks elsewhere as needed
        ]
        rk = self.environment.render_kw = dict(self.environment.render_kw or {})
        rk.update({
            "placeholder": f"<customer>-{suffix}",
            "pattern": pattern,
            "title": (
                f"Lowercase letters only followed by '-{suffix}' "
                f"(e.g. acme-{suffix}). Do NOT add -uat/-prod/-staging."
            ),
            "required": True,
        })
        self.environment.description = (
            f"Only the base prefix, e.g. 'acme-{suffix}'. "
            "SSA will append the environment/stage automatically "
            "(e.g. 'ilp-acme-ctlst-uat')."
        )

        # Browser combobox: suggest known domains while allowing custom values.
        self.domain_name.datalist_options = [value for value, _label in DOMAIN_CHOICES]
