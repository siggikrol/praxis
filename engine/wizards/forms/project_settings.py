import re
from flask_wtf import FlaskForm
from wtforms import StringField, SelectMultipleField, TextAreaField
from wtforms.validators import DataRequired, Regexp, Optional, Length
from wtforms.widgets import ListWidget, CheckboxInput, HiddenInput
from engine.wizards.constants.project_constants import (
    UNIFIED_ENV_TYPES_CHOICES,
    PROJECT_DEPLOYMENT_CHOICES,
    PROJECT_DEPLOYMENT_DEFAULTS,
    SINGLE_VPC_DEPLOYMENT_DEFAULTS,
    PROJECT_DEFAULT_SUFFIX,
    PROJECT_ENVIRONMENT_HELP,
)


class EnvironmentDetailsForm(FlaskForm):
    environment_name = StringField("Environment name", validators=[Optional(), Length(max=120)],
        description="A display name for this environment. The AWS resource prefix is configured under Cloud.")
    environment_description = TextAreaField("Description", validators=[Optional(), Length(max=1000)])
    environment_owner = StringField("Owner / team", validators=[Optional(), Length(max=200)])


class ProjectSettingsForm(EnvironmentDetailsForm):
    """Used by the Multi-VPC wizard to select deployment stacks and environment types."""

    deployments = SelectMultipleField(
        "Deployments",
        choices=PROJECT_DEPLOYMENT_CHOICES,
        option_widget=CheckboxInput(),
        widget=ListWidget(prefix_label=False),
        default=PROJECT_DEPLOYMENT_DEFAULTS,
        description="Select which deployment modules to include",
    )

    environment_type = SelectMultipleField(
        "Environment Types",
        choices=UNIFIED_ENV_TYPES_CHOICES,
        option_widget=CheckboxInput(),
        widget=ListWidget(prefix_label=False),
        default=[],
        validators=[DataRequired(message="Select at least one environment type")],
        description="Select environment stages to provision",
    )

    environment_suffixes = StringField(widget=HiddenInput())

# Used by the Loyalty wizard
class DefaultProjectSettingsForm(EnvironmentDetailsForm):
    """Loyalty project settings (environment selection only)."""

#    environment_prefix = StringField(
#        "Environment Prefix",
#        validators=[DataRequired()],
#        render_kw={"placeholder": f"<customer>-{PROJECT_DEFAULT_SUFFIX}"},
#        description=PROJECT_ENVIRONMENT_HELP,
#        filters=[lambda s: s.strip().lower() if s else s],
#    )

    environment_type = SelectMultipleField(
        "Environment Types",
        choices=UNIFIED_ENV_TYPES_CHOICES,
        option_widget=CheckboxInput(),
        widget=ListWidget(prefix_label=False),
        default=[],
        validators=[DataRequired(message="Select at least one environment type")],
        description="Select environment stages to provision",
    )

    environment_suffixes = StringField(widget=HiddenInput())


class UnifiedProjectSettingsForm(EnvironmentDetailsForm):
    """Used by Unified wizard"""

    environment_type = SelectMultipleField(
        "Environment Types",
        choices=UNIFIED_ENV_TYPES_CHOICES,
        option_widget=CheckboxInput(),
        widget=ListWidget(prefix_label=False),
        default=[],
        validators=[DataRequired(message="Select at least one environment type")],
        description="Select environment stages to provision",
    )

    environment_suffixes = StringField(widget=HiddenInput())

    deployments = SelectMultipleField(
        "Deployments",
        choices=PROJECT_DEPLOYMENT_CHOICES,
        option_widget=CheckboxInput(),
        widget=ListWidget(prefix_label=False),
        default=[],
        validators=[DataRequired(message="Select at least one deployment")],
        description="Select which deployment modules to include",
    )


class SingleVpcProjectSettingsForm(EnvironmentDetailsForm):
    """Single-VPC variant of unified project settings with catalyst defaults."""

    environment_type = SelectMultipleField(
        "Environment Types",
        choices=UNIFIED_ENV_TYPES_CHOICES,
        option_widget=CheckboxInput(),
        widget=ListWidget(prefix_label=False),
        default=[],
        validators=[DataRequired(message="Select at least one environment type")],
        description="Select environment stages to provision",
    )

    environment_suffixes = StringField(widget=HiddenInput())

    deployments = SelectMultipleField(
        "Deployments",
        choices=PROJECT_DEPLOYMENT_CHOICES,
        option_widget=CheckboxInput(),
        widget=ListWidget(prefix_label=False),
        default=SINGLE_VPC_DEPLOYMENT_DEFAULTS,
        validators=[DataRequired(message="Select at least one deployment")],
        description="Select which deployment modules to include",
    )


class RgsProjectSettingsForm(EnvironmentDetailsForm):
    """RGS variant of Unified project settings (deployments locked to rgs)."""

    environment_type = SelectMultipleField(
        "Environment Types",
        choices=UNIFIED_ENV_TYPES_CHOICES,
        option_widget=CheckboxInput(),
        widget=ListWidget(prefix_label=False),
        default=[],
        validators=[DataRequired(message="Select at least one environment type")],
        description="Select environment stages to provision",
    )

    environment_suffixes = StringField(widget=HiddenInput())

    deployments = SelectMultipleField(
        "Deployments",
        choices=[("rgs", "rgs"), ("common", "common"), ("rmq", "rmq")],
        option_widget=CheckboxInput(),
        widget=ListWidget(prefix_label=False),
        default=["rgs", "common", "rmq"],
        validators=[DataRequired(message="Select at least one deployment")],
        description="Select which deployment modules to include",
    )
