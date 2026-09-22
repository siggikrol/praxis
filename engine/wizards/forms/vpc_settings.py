from __future__ import annotations

from flask import request, session
from flask_wtf import FlaskForm
from wtforms import FormField, SelectField, SelectMultipleField, StringField
from wtforms.validators import DataRequired, Regexp
from wtforms.widgets import CheckboxInput, ListWidget

from constants import AWS_REGIONS
from engine.wizards.constants.project_constants import ENV_DEFAULT_ENV_TYPES
from engine.wizards.constants.vpc_constants import (
    AZ_ZONE_IDS_BY_REGION,
    VPC_AZ_PROFILES,
    VPC_AZ_PROFILE_CHOICES,
    default_vpc_profile_for_region,
)
from engine.wizards.factory.utils import _key
from engine.wizards.forms.ux import get_env_key, get_env_label


def _wizard_env_slug() -> str:
    try:
        bp = (request.blueprint or "").strip()
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
            region = (common_cfg.get("aws_region") or "").strip()
            if region:
                return region
    except Exception:
        pass
    return default


def _vpc_endpoint_service_region(service_name: str) -> str:
    parts = (service_name or "").strip().split(".")
    if len(parts) >= 5 and parts[:3] == ["com", "amazonaws", "vpce"]:
        return parts[3]
    return ""


class VpcEnvForm(FlaskForm):
    class Meta:
        csrf = False

    vpc_az_profile = SelectField(
        "Region/VPC Profile",
        choices=VPC_AZ_PROFILE_CHOICES,
        default="custom",
        description="Pick a preset to auto-fill TGW/VPCE and AZs, or choose Custom.",
    )
    custom_aws_region = SelectField(
        "Custom AWS Region",
        choices=[],
        description="Used only when Region/VPC Profile is set to Custom.",
    )

    transit_gateway_id = StringField(
        "Transit Gateway ID",
        validators=[DataRequired(), Regexp(r"^tgw-[0-9A-Fa-f]+$")],
        render_kw={"placeholder": "tgw-04005a7f93411ea02"},
        description="AWS Transit Gateway identifier",
    )
    vpc_endpoint_service = StringField(
        "VPC Endpoint Service",
        validators=[
            DataRequired(),
            Regexp(r"^com\.amazonaws\.vpce\.[a-z0-9\-]+\.vpce-svc-[0-9A-Fa-f]+$"),
        ],
        render_kw={"placeholder": "com.amazonaws.vpce.us-east-1.vpce-svc-..."},
        description="Service name for VPC endpoint",
    )

    zones = SelectMultipleField(
        "Availability Zones",
        choices=[],
        option_widget=CheckboxInput(),
        widget=ListWidget(prefix_label=False),
        description="Pick exactly 3 zones for subnet placement",
    )

    zone_ids = SelectMultipleField(
        "Filter AZ Zone IDs",
        choices=[],
        option_widget=CheckboxInput(),
        widget=ListWidget(prefix_label=False),
        description="Internal AZ IDs to include (account-stable).",
    )

    def __init__(self, *args, aws_region: str | None = None, **kwargs):
        aws_region = (aws_region or "").strip() or _selected_aws_region()
        initial_data = kwargs.get("data")
        super().__init__(*args, **kwargs)

        supported_custom_regions = [code for code, _ in AWS_REGIONS]
        self.custom_aws_region.choices = list(AWS_REGIONS)

        selected_custom_region = (
            self.custom_aws_region.data or aws_region or "us-east-1"
        ).strip() or "us-east-1"
        if supported_custom_regions:
            if selected_custom_region not in supported_custom_regions:
                selected_custom_region = (
                    "us-east-1"
                    if "us-east-1" in supported_custom_regions
                    else supported_custom_regions[0]
                )
            self.custom_aws_region.data = selected_custom_region
        self._custom_region = selected_custom_region
        profile_key = (self.vpc_az_profile.data or "").lower()
        explicit_profile = bool(self.vpc_az_profile.raw_data) or (
            isinstance(initial_data, dict)
            and bool((initial_data.get("vpc_az_profile") or "").strip())
        )
        default_profile_key = default_vpc_profile_for_region(selected_custom_region)
        if not explicit_profile and profile_key == "custom" and default_profile_key:
            profile_key = default_profile_key
            self.vpc_az_profile.data = default_profile_key
        profile = VPC_AZ_PROFILES.get(profile_key)
        region = profile["aws_region"] if profile else self._custom_region
        self._aws_region = (region or "us-east-1").strip()

        suffixes = ["a", "b", "c", "d", "e", "f"]
        zone_name_choices = [f"{self._aws_region}{suffix}" for suffix in suffixes]
        self.zones.choices = [(zone, zone) for zone in zone_name_choices]

        zone_id_choices = AZ_ZONE_IDS_BY_REGION.get(self._aws_region, [])
        self.zone_ids.choices = [(zone_id, zone_id) for zone_id in zone_id_choices]

        self.zones.render_kw = dict(
            self.zones.render_kw or {},
            **{
                "data-aws-region": self._aws_region,
                "data-custom-region": self._custom_region,
                "data-suffixes": ",".join(suffixes),
                "data-field-name": self.zones.name,
            },
        )
        self.zone_ids.render_kw = dict(
            self.zone_ids.render_kw or {},
            **{
                "data-field-name": self.zone_ids.name,
            },
        )

        if profile:
            if not (self.transit_gateway_id.data or "").strip():
                self.transit_gateway_id.data = profile["transit_gateway_id"]
            if not (self.vpc_endpoint_service.data or "").strip():
                self.vpc_endpoint_service.data = profile["vpc_endpoint_service"]
            if not (self.zones.data or []):
                self.zones.data = profile.get("az_names", [])
            if not (self.zone_ids.data or []):
                self.zone_ids.data = profile.get("az_zone_ids", [])

        if not (self.zones.data or []):
            self.zones.data = [f"{self._aws_region}{suffix}" for suffix in ("a", "b", "c")]

    def validate(self, **kwargs):
        profile_key = (self.vpc_az_profile.data or "").lower()
        profile = VPC_AZ_PROFILES.get(profile_key)
        expected_region = (
            profile.get("aws_region", self._aws_region) if profile else self._aws_region
        )
        submitted_endpoint_service = (self.vpc_endpoint_service.data or "").strip()

        if profile:
            self.transit_gateway_id.data = profile["transit_gateway_id"]
            self.vpc_endpoint_service.data = profile["vpc_endpoint_service"]
            self.zones.data = profile.get("az_names", [])
            self.zone_ids.data = profile.get("az_zone_ids", [])

        ok = super().validate(**kwargs)

        if len(self.zones.data or []) != 3:
            self.zones.errors.append("Select exactly 3 availability zones.")
            ok = False

        endpoint_region = _vpc_endpoint_service_region(submitted_endpoint_service)
        if endpoint_region and endpoint_region != expected_region:
            self.vpc_endpoint_service.errors.append(
                "VPC Endpoint Service region must match the selected VPC region "
                f"({expected_region}). Endpoint services cannot be used across AWS regions."
            )
            ok = False

        if profile:
            bad = [
                zone
                for zone in (self.zones.data or [])
                if not str(zone).startswith(expected_region)
            ]
            if bad:
                self.zones.errors.append(f"Preset expects region {expected_region}.")
                ok = False

        return ok


class VpcSettingsForm(FlaskForm):
    @classmethod
    def for_envs(cls, env_types):
        env_types = env_types or ENV_DEFAULT_ENV_TYPES
        attrs = {
            get_env_key(env): FormField(VpcEnvForm, description=f"VPC settings for {get_env_label(env)}")
            for env in env_types
        }
        return type("VpcSettingsFormDynamic", (cls,), attrs)


VpcSettingsFormDynamic = VpcSettingsForm.for_envs
