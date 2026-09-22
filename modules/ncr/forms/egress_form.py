from flask_wtf import FlaskForm
from wtforms import BooleanField, SelectMultipleField, TextAreaField
from wtforms.validators import Optional
from modules.ncr.networking import load_profile


def EgressForm(*args, **kwargs):
    """Construct choices from current content; saved/submitted values still take precedence."""
    config = load_profile('catalyst-common')['profile']['builder']
    fields = {}
    for rule in config['egress_rules'].values():
        name = rule['field']
        label = config['labels'][name]
        if rule['kind'] == 'flag':
            fields[name] = BooleanField(label)
        elif rule['kind'] == 'list':
            choices = config['partner_choices' if name == 'partners' else 'east_west_choices']
            fields[name] = SelectMultipleField(label, choices=[tuple(row) for row in choices])
        else:
            fields[name] = TextAreaField(label, validators=[Optional()])
    return type('ConfiguredEgressForm', (FlaskForm,), fields)(*args, **kwargs)
