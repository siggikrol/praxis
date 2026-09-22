import ipaddress
from wtforms.validators import ValidationError
from modules.ncr.constants import HOSTNAME_PATTERN

def validate_host_list(form, field):
    for lineno, s in enumerate((field.data or "").splitlines(), start=1):
        s = s.strip()
        if not s:
            continue
        if not HOSTNAME_PATTERN.match(s):
            raise ValidationError(f"Invalid host on line {lineno}: {s}")

def validate_cidr_list(form, field):
    for lineno, s in enumerate((field.data or "").splitlines(), start=1):
        s = s.strip()
        if not s:
            continue
        try:
            ipaddress.ip_network(s, strict=False)
        except Exception:
            raise ValidationError(f"Invalid CIDR/IP on line {lineno}: {s}")
