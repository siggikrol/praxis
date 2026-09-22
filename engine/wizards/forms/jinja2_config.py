# forms/jinja2_config.py
from flask_wtf import FlaskForm
from wtforms import SelectMultipleField
from wtforms.widgets import ListWidget, CheckboxInput

class Jinja2Form(FlaskForm):
    exclude_files = SelectMultipleField(
        "Exclude Files",
        choices=[
            (
                "helm-monitoring/grafana/values.yaml",
                "helm-monitoring/grafana/values.yaml"
            )
        ],
        option_widget=CheckboxInput(),
        widget=ListWidget(prefix_label=False),
        default=["helm-monitoring/grafana/values.yaml"],
        description="File paths to exclude from Jinja2 templating",
    )
