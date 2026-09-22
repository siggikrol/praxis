# forms/wizards/alb_form.py
from __future__ import annotations
import re
from flask_wtf import FlaskForm
from wtforms import FieldList, FormField, HiddenField, StringField, TextAreaField
from wtforms.validators import DataRequired, Length, ValidationError
from constants import DEFAULT_EXTERNAL_ALB_ALLOWED_IPS

CIDR_RE = re.compile(r"^\d{1,3}(\.\d{1,3}){3}/\d{1,2}$")
ALB_GROUP_KEY = "ips"

def _parse_lines(raw: str) -> list[str]:
    if not raw:
        return []
    parts = [p.strip() for p in raw.replace(",", "\n").splitlines()]
    return [p for p in parts if p]

class _AlbIpGroupForm(FlaskForm):
    # 🔒 Important: disable nested CSRF; parent AlbForm handles it.
    class Meta:
        csrf = False

    group_key   = HiddenField(default=ALB_GROUP_KEY, validators=[DataRequired(), Length(max=64)])
    description = StringField("Description", validators=[DataRequired(), Length(max=128)],
                              description="Human-friendly label (shown in spec)")
    cidrs       = TextAreaField("CIDRs (one per line or comma-separated)",
                                description="Example: 203.0.113.10/32",
                                render_kw={"rows": 6})

    def validate_cidrs(self, field):
        ips = _parse_lines(field.data)
        if not ips:
            raise ValidationError("Provide at least one CIDR.")
        bad = [x for x in ips if not CIDR_RE.match(x)]
        if bad:
            raise ValidationError(f"Invalid CIDR(s): {', '.join(bad)}")

class AlbForm(FlaskForm):
    # A list of groups; each becomes an item at top-level external_alb_allowed_ips
    groups = FieldList(FormField(_AlbIpGroupForm), min_entries=1, label="Groups")

    def __init__(self, *args, **kwargs):
        formdata = kwargs.get("formdata")
        if formdata is None and args:
            formdata = args[0]
        is_bound = formdata is not None

        super().__init__(*args, **kwargs)
        self._normalize_entries(seed_defaults=not is_bound)

    def _normalize_entries(self, *, seed_defaults: bool) -> None:
        if len(self.groups.entries) == 0:
            self.groups.append_entry()

        for entry in self.groups.entries:
            entry.form.group_key.data = ALB_GROUP_KEY

        if not seed_defaults:
            return

        g0 = self.groups[0].form
        if not (g0.description.data or "").strip():
            g0.description.data = "PRAXIS-EU"

        defaults = DEFAULT_EXTERNAL_ALB_ALLOWED_IPS
        if isinstance(defaults, dict):
            seed = defaults.get(ALB_GROUP_KEY, [])
        else:
            seed = list(defaults)

        if not (g0.cidrs.data or "").strip():
            g0.cidrs.data = "\n".join(seed)

    def validate(self, **kwargs):
        self._normalize_entries(seed_defaults=False)
        return super().validate(**kwargs)
