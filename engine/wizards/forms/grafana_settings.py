# engine/wizards/forms/grafana_settings.py
from __future__ import annotations
from flask_wtf import FlaskForm
from wtforms import SelectField, StringField, FormField
from wtforms.validators import DataRequired, Optional
from engine.wizards.constants.project_constants import ENV_DEFAULT_ENV_TYPES
from engine.wizards.forms.ux import get_env_key, get_env_label


# ---------------------------------------------------------------------------
# Per-environment subform
# ---------------------------------------------------------------------------
class GrafanaEnvForm(FlaskForm):
    """Grafana settings for a single environment."""
    class Meta:
        csrf = False

    grafana_cloud_setup = SelectField(
        "Grafana Cloud Setup",
        choices=[("true", "true"), ("false", "false")],
        validators=[DataRequired()],
        default="false",
        description="Enable or disable Grafana Cloud",
        render_kw={
            "data-bs-toggle": "tooltip",
            "data-bs-placement": "top",
            "title": "Enable or disable Grafana Cloud",
        },
    )

    grafana_external_id = StringField(
        "Grafana External ID",
        validators=[Optional()],
        render_kw={
            "placeholder": "12345",
            "data-required-when": "grafana_cloud_setup:true",
            "data-bs-toggle": "tooltip",
            "data-bs-placement": "top",
            "title": "External account ID for Grafana Cloud",
        },
        description="External account ID for Grafana Cloud",
    )

    def validate(self, **kwargs):
        ok = super().validate(**kwargs)
        if (self.grafana_cloud_setup.data or "").strip().lower() == "true":
            if not (self.grafana_external_id.data or "").strip():
                self.grafana_external_id.errors.append(
                    "Grafana External ID is required when Grafana Cloud is enabled."
                )
                ok = False
        return ok


# ---------------------------------------------------------------------------
# Top-level multi-environment wrapper
# ---------------------------------------------------------------------------
class GrafanaSettingsForm(FlaskForm):
    """Top-level Grafana dynamic form (acts like Redis / Aurora / FormKiQ)."""

    @classmethod
    def for_envs(cls, env_types):
        env_types = env_types or ENV_DEFAULT_ENV_TYPES

        # Build attributes dynamically: one subform per environment
        attrs = {
            get_env_key(env): FormField(
                GrafanaEnvForm,
                description=f"Grafana settings for {get_env_label(env)}",
            )
            for env in env_types
        }

        # Build a new form class type with those fields
        return type("GrafanaSettingsFormDynamic", (cls,), attrs)


# Backward compatibility alias (mirrors RedisForm pattern)
GrafanaSettingsFormDynamic = GrafanaSettingsForm.for_envs
