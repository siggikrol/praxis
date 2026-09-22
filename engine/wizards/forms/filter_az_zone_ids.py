from flask_wtf import FlaskForm
from wtforms import SelectMultipleField
from wtforms.widgets import ListWidget, CheckboxInput
from constants import AZ_ZONE_IDS

class FilterAzZoneIDsForm(FlaskForm):
    zone_ids = SelectMultipleField(
        "Filter AZ Zone IDs",
        choices=[(z, z) for z in AZ_ZONE_IDS],
        option_widget=CheckboxInput(),
        widget=ListWidget(prefix_label=False),
        default=AZ_ZONE_IDS,
        description="Specific availability zone IDs to include",
    )