# forms/alb_settings.py
from flask_wtf import FlaskForm
from wtforms import TextAreaField
from wtforms.validators import DataRequired
from constants import DEFAULT_EXTERNAL_ALB_ALLOWED_IPS

class AlbSettingsForm(FlaskForm):
    external_alb_allowed_ips = TextAreaField(
        "External ALB Allowed IPs",
        validators=[DataRequired()],
        description="One CIDR per line (or comma-separated). Will be wrapped under description “PRAXIS-EU”.",
        render_kw={
            "rows": 5,
            "placeholder": "82.117.198.222/32\n212.30.199.42/32\n…"
        },
    )

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        # If blank, seed with our defaults (newline separated)
        val = (self.external_alb_allowed_ips.data or "").strip()
        if not val:
            self.external_alb_allowed_ips.data = "\n".join(DEFAULT_EXTERNAL_ALB_ALLOWED_IPS)
