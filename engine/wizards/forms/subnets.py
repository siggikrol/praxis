# forms/subnets.py
import ipaddress
from flask_wtf import FlaskForm
from wtforms import StringField, FormField
from wtforms.validators import DataRequired, ValidationError
from engine.wizards.constants.project_constants import ENV_DEFAULT_ENV_TYPES
from constants import SUBNET_DEPLOYMENTS

AWS_MIN_PREFIX = 16
AWS_MAX_PREFIX = 28


from engine.wizards.forms.ux import get_env_key, get_env_label


class SubnetForm(FlaskForm):
    class Meta:
        csrf = False

    vpc_cidr_block = StringField(
        "VPC CIDR Block",
        validators=[DataRequired()],
        render_kw={
            "placeholder": "10.223.40.0/21",
            "pattern": r"^((25[0-5]|2[0-4]\d|1?\d{1,2})\.){3}(25[0-5]|2[0-4]\d|1?\d{1,2})/(3[0-2]|[12]?\d)$",
            "title": "Enter a valid IPv4 CIDR, e.g. 10.223.40.0/21",
        },
    )

    def validate_vpc_cidr_block(self, field):
        raw = (field.data or "").strip()
        try:
            # Require true network address (no host bits set).
            net = ipaddress.ip_network(raw, strict=True)
        except ValueError as e:
            msg = str(e)
            if "has host bits set" in msg:
                raise ValidationError("CIDR must be the network address (no host bits). Example: 10.223.40.0/21.")
            raise ValidationError("Invalid CIDR block. Example format: 10.223.40.0/21.")

        if net.version != 4:
            raise ValidationError("Only IPv4 CIDRs are allowed.")

        if not (AWS_MIN_PREFIX <= net.prefixlen <= AWS_MAX_PREFIX):
            raise ValidationError(f"CIDR size must be between /{AWS_MIN_PREFIX} and /{AWS_MAX_PREFIX}.")


class SubnetsForm(FlaskForm):
    @classmethod
    def for_envs(cls, env_types):
        env_types = env_types or ENV_DEFAULT_ENV_TYPES
        attrs = {}
        for env in env_types:
            for dep in SUBNET_DEPLOYMENTS:
                key = get_env_key(env)
                name = f"{key}_{dep}"
                attrs[name] = FormField(
                    SubnetForm,
                    description=f"{get_env_label(env)} / {dep.upper()}",
                )
        return type("SubnetsFormDynamic", (cls,), attrs)

    def validate(self, **kwargs):
        ok = super().validate(**kwargs)
        nets = []
        for name, field in self._fields.items():
            sub = getattr(field, "form", None)
            if sub is None or not hasattr(sub, "vpc_cidr_block"):
                continue

            cidr = (sub.vpc_cidr_block.data or "").strip()
            try:
                net = ipaddress.ip_network(cidr, strict=True)
            except Exception:
                # Field-level validator already recorded the error.
                continue

            for other in nets:
                if net.overlaps(other):
                    sub.vpc_cidr_block.errors.append(f"Overlaps {other}.")
                    ok = False
            nets.append(net)

        return ok
