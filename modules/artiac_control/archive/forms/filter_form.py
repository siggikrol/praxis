# Archive filtering for Artiac Operations.
from flask_wtf import FlaskForm
from wtforms import StringField
from wtforms.validators import Optional


class IacArtifactFilterForm(FlaskForm):
    """Basic filter form for IAC archives."""

    module = StringField(validators=[Optional()])
    version = StringField(validators=[Optional()])
