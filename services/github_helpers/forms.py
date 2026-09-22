import os

from flask_wtf import FlaskForm
from wtforms import StringField
from wtforms.validators import DataRequired

DEFAULT_HARBOR_REGISTRY_PROJECT = (
    (os.getenv("PS_CORE_VALIDATE_DEFAULT_HARBOR_PROJECT") or "betware-release").strip()
    or "betware-release"
)
DEFAULT_PC_VERSION = (
    (os.getenv("PS_CORE_VALIDATE_DEFAULT_PC_VERSION") or "1.3.1").strip()
    or "1.3.1"
)


class PCDeployWorkflowForm(FlaskForm):
    harbor_registry_project = StringField(
        "Harbor Registry Project",
        default=DEFAULT_HARBOR_REGISTRY_PROJECT,
        validators=[DataRequired()],
    )

    pc_version = StringField(
        "Praxis Core Version",
        default=DEFAULT_PC_VERSION,
        validators=[DataRequired()],
    )
