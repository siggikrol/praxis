# forms/mkodo_form.py
from flask_wtf import FlaskForm
from wtforms import StringField, SelectField, BooleanField
from wtforms.validators import Optional

class MkodoForm(FlaskForm):
    disable_section = BooleanField(
        "Exclude Mkodo from spec",
        default=False,
        description="Skip Mkodo configuration and omit it from the final spec.",
    )

    enabled = SelectField(
        "Enable Mkodo?",
        choices=[("true","true"),("false","false")],
        default="true",
    )
    mkodo_basepoint = StringField(
        "Basepoint URL",
        validators=[Optional()],
        render_kw={"placeholder":"gateway.us-east.mkodo-stage.net"},
    )
    mkodo_cors_allow_domain = StringField(
        "CORS Allow Domain",
        validators=[Optional()],
    )

    def validate(self, extra_validators=None):
        # If excluded, allow submit without validating other fields.
        if getattr(self, "disable_section", None) and self.disable_section.data:
            return True
        return super().validate(extra_validators=extra_validators)
