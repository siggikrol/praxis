# modules/releases_explorer/forms/filter_form.py
from flask_wtf import FlaskForm
from wtforms import StringField
from wtforms.validators import Optional

class ReleaseFilterForm(FlaskForm):
    """Basic filter form for releases."""
    customer = StringField(validators=[Optional()])
    module = StringField(validators=[Optional()])
    release_type = StringField(validators=[Optional()])
    version = StringField(validators=[Optional()])
    query = StringField(validators=[Optional()])
    latest = StringField(validators=[Optional()])
    state = StringField(validators=[Optional()])
    published_from = StringField(validators=[Optional()])
    published_to = StringField(validators=[Optional()])
