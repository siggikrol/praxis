from flask_wtf import FlaskForm
from wtforms import SelectMultipleField
from wtforms.widgets import ListWidget, CheckboxInput
from constants import AZ_NAMES

class AvailabilityZonesForm(FlaskForm):
    zones = SelectMultipleField(
        "Availability Zones",
        choices=[(z, z) for z in AZ_NAMES],
        option_widget=CheckboxInput(),
        widget=ListWidget(prefix_label=False),
        default=AZ_NAMES,
        description="Availability zones for subnet placement",
    )
