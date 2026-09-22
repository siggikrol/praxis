from flask_wtf import FlaskForm
from wtforms import StringField, IntegerField, SelectField, SubmitField
from wtforms.validators import DataRequired, Length, NumberRange


class ExampleForm(FlaskForm):
    """Example WTForm demonstrating text, number, and select inputs."""
    
    name = StringField(
        "Name",
        validators=[
            DataRequired(),
            Length(min=2, max=64)
        ]
    )

    count = IntegerField(
        "Count",
        validators=[
            DataRequired(),
            NumberRange(min=1, max=100)
        ]
    )

    mode = SelectField(
        "Mode",
        choices=[
            ("fast", "Fast"),
            ("safe", "Safe"),
            ("debug", "Debug"),
        ],
        validators=[DataRequired()]
    )

    submit = SubmitField("Submit")
