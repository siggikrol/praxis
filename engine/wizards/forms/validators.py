# forms/validators.py
import json
import ipaddress
from wtforms.validators import ValidationError

def is_json(msg="Must be valid JSON"):
    def _v(form, field):
        data = (field.data or "").strip()
        if not data:
            return
        try:
            json.loads(data)
        except Exception:
            raise ValidationError(msg)
    return _v

def validate_cidr(form, field):
    try:
        ipaddress.ip_network(field.data, strict=False)
    except Exception:
        raise ValidationError("Invalid CIDR block. Example: 10.223.40.0/21")
