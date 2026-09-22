import os

from flask import request, session
from flask_wtf import FlaskForm
from wtforms import SelectField, FormField
from wtforms.validators import DataRequired, NumberRange
from engine.wizards.factory.utils import _key
from engine.wizards.constants.redis_constants import (
    REDIS_NODE_TYPES,
    REDIS_NODE_TYPE_GROUPS,
    REDIS_DEFAULTS,
    REDIS_VALIDATION_LIMITS,
)
from engine.wizards.constants.project_constants import ENV_DEFAULT_ENV_TYPES
from engine.wizards.forms.ux import get_env_key, get_env_label
from .fields import NumberInputField
from services.aws_instance_types.redis_node_types import get_redis_node_type_options


def _wizard_env_slug() -> str:
    try:
        bp = (request.blueprint or "").strip()
    except RuntimeError:
        return ""
    if bp.endswith("_wizard"):
        return bp[: -len("_wizard")]
    return bp


def _selected_aws_region(default: str = "us-east-1") -> str:
    try:
        env_slug = _wizard_env_slug()
        if env_slug:
            common_cfg = session.get(_key(env_slug, "common"), {}) or {}
            region = (common_cfg.get("aws_region") or "").strip()
            if region:
                return region
    except Exception:
        pass
    return (os.getenv("AWS_DEFAULT_REGION") or default).strip() or default


class RedisEnvForm(FlaskForm):
    """Redis settings per environment."""
    class Meta:
        csrf = False

    redis_instance_type = SelectField(
        "Redis Instance Type",
        choices=REDIS_NODE_TYPES,
        validators=[DataRequired()],
        default=REDIS_DEFAULTS["instance_type"],
    )

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        region = _selected_aws_region()
        groups, choices, meta = get_redis_node_type_options(
            region=region,
            fallback_groups=REDIS_NODE_TYPE_GROUPS,
            fallback_choices=REDIS_NODE_TYPES,
        )
        self._redis_node_types_meta = meta
        current = str(self.redis_instance_type.data or "").strip()
        available = {str(value) for value, _label in (choices or [])}
        missing = (
            [(current, f"{current} (saved selection)")]
            if current and current not in available
            else []
        )
        self.redis_instance_type.choices = missing + list(choices or [])
        self.redis_instance_type.option_groups = (
            ([("Saved selection", missing)] if missing else [])
            + list(groups or [])
        )
    redis_cluster_size = NumberInputField(
        "Cluster Size",
        validators=[
            DataRequired(),
            NumberRange(min=REDIS_VALIDATION_LIMITS["cluster_size_min"]),
        ],
        default=REDIS_DEFAULTS["cluster_size"],
    )
    redis_cluster_shard = NumberInputField(
        "Shards",
        validators=[
            DataRequired(),
            NumberRange(min=REDIS_VALIDATION_LIMITS["shards_min"]),
        ],
        default=REDIS_DEFAULTS["shards"],
    )
    redis_cluster_replica = NumberInputField(
        "Replicas per Shard",
        validators=[
            DataRequired(),
            NumberRange(min=REDIS_VALIDATION_LIMITS["replicas_min"]),
        ],
        default=REDIS_DEFAULTS["replicas"],
    )


class RedisSettingsForm(FlaskForm):
    """Top-level Redis settings wrapper — adds per-environment subforms."""

    @classmethod
    def for_envs(cls, env_types):
        env_types = env_types or ENV_DEFAULT_ENV_TYPES
        attrs = {
            get_env_key(env): FormField(RedisEnvForm, description=f"Redis settings for {get_env_label(env)}")
            for env in env_types
        }
        return type("RedisSettingsFormDynamic", (cls,), attrs)

# Backward compatibility
RedisForm = RedisSettingsForm

# Helper for wizard dynamic form registration
RedisSettingsFormDynamic = RedisSettingsForm.for_envs
