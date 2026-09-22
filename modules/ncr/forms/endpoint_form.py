from wtforms import Form, HiddenField, StringField, TextAreaField
from wtforms.validators import DataRequired, Optional, ValidationError

from .validators import validate_host_list


def _trim(value):
    return value.strip() if isinstance(value, str) else value

class EndpointForm(Form):
    namespace     = StringField(
        "Namespace",
        validators=[DataRequired(message="Namespace is required for each endpoint.")],
        filters=[_trim],
        render_kw={"placeholder": "cw"},
    )
    name          = StringField(
        "Name",
        validators=[DataRequired(message="Name is required for each endpoint.")],
        filters=[_trim],
        render_kw={"placeholder": "camunda-webapp"},
    )
    gateways      = TextAreaField(
        "Gateways",
        validators=[DataRequired(message="At least one gateway is required for each endpoint.")],
        filters=[_trim],
        description="One per line",
        render_kw={"placeholder": "camunda-webapp"},
    )
    host_template = HiddenField(validators=[Optional()])
    hosts         = TextAreaField(
        "Hosts",
        validators=[validate_host_list],
        filters=[_trim],
        description="One per line",
        render_kw={"placeholder": "camunda.int.ak-ctlst-staging.example.com"},
    )

    def validate_hosts(self, field):
        if (self.host_template.data or "").strip():
            return
        if not (field.data or "").strip():
            raise ValidationError("At least one host is required for a custom endpoint.")
