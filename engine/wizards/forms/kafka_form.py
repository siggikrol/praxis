import os

from flask import request, session
from flask_wtf import FlaskForm
from wtforms import SelectField
from wtforms.validators import DataRequired, NumberRange

from .fields import NumberInputField
from engine.wizards.factory.utils import _key
from engine.wizards.constants.kafka_constants import (
    KAFKA_BOOL_CHOICES,
    KAFKA_BROKER_NODE_INSTANCE_TYPE_CHOICES,
    KAFKA_BROKER_NODE_INSTANCE_TYPE_GROUPS,
    KAFKA_DEFAULTS,
    KAFKA_ENCRYPTION_IN_TRANSIT_CLIENT_BROKER_CHOICES,
    KAFKA_VERSION_CHOICES,
    KAFKA_VERSION_GROUPS,
    KAFKA_VALIDATION_LIMITS,
)
from services.aws_instance_types.kafka_catalog import (
    get_kafka_broker_node_instance_type_options,
    get_kafka_version_options,
)


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


class KafkaForm(FlaskForm):
    kafka_version = SelectField(
        "Kafka Version",
        choices=KAFKA_VERSION_CHOICES,
        validators=[DataRequired(message="Kafka Version is required")],
        default=KAFKA_DEFAULTS["kafka_version"],
        description="Kafka engine version for the MSK cluster.",
    )

    kafka_number_of_broker_nodes = NumberInputField(
        "Number of Broker Nodes",
        validators=[
            DataRequired(),
            NumberRange(min=KAFKA_VALIDATION_LIMITS["broker_nodes_min"]),
        ],
        default=KAFKA_DEFAULTS["kafka_number_of_broker_nodes"],
        description="Total broker node count for the cluster.",
    )

    kafka_scaling_max_capacity = NumberInputField(
        "Scaling Max Capacity",
        validators=[
            DataRequired(),
            NumberRange(min=KAFKA_VALIDATION_LIMITS["scaling_capacity_min"]),
        ],
        default=KAFKA_DEFAULTS["kafka_scaling_max_capacity"],
        description="Maximum scaling units for broker storage.",
    )

    kafka_broker_node_instance_type = SelectField(
        "Broker Node Instance Type",
        choices=KAFKA_BROKER_NODE_INSTANCE_TYPE_CHOICES,
        validators=[DataRequired()],
        default=KAFKA_DEFAULTS["kafka_broker_node_instance_type"],
    )

    kafka_encryption_in_transit_client_broker = SelectField(
        "Client-Broker Transit Encryption",
        choices=KAFKA_ENCRYPTION_IN_TRANSIT_CLIENT_BROKER_CHOICES,
        validators=[DataRequired()],
        default=KAFKA_DEFAULTS["kafka_encryption_in_transit_client_broker"],
    )

    kafka_encryption_in_transit_in_cluster = SelectField(
        "In-Cluster Transit Encryption",
        choices=KAFKA_BOOL_CHOICES,
        validators=[DataRequired()],
        default=KAFKA_DEFAULTS["kafka_encryption_in_transit_in_cluster"],
    )

    kafka_jmx_exporter_enabled = SelectField(
        "Enable JMX Exporter",
        choices=KAFKA_BOOL_CHOICES,
        validators=[DataRequired()],
        default=KAFKA_DEFAULTS["kafka_jmx_exporter_enabled"],
    )

    kafka_node_exporter_enabled = SelectField(
        "Enable Node Exporter",
        choices=KAFKA_BOOL_CHOICES,
        validators=[DataRequired()],
        default=KAFKA_DEFAULTS["kafka_node_exporter_enabled"],
    )

    kafka_cloudwatch_logs_enabled = SelectField(
        "Enable CloudWatch Logs",
        choices=KAFKA_BOOL_CHOICES,
        validators=[DataRequired()],
        default=KAFKA_DEFAULTS["kafka_cloudwatch_logs_enabled"],
    )

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        region = _selected_aws_region()

        version_groups, version_choices, version_meta = get_kafka_version_options(
            region=region,
            fallback_groups=KAFKA_VERSION_GROUPS,
            fallback_choices=KAFKA_VERSION_CHOICES,
        )
        broker_groups, broker_choices, broker_meta = get_kafka_broker_node_instance_type_options(
            region=region,
            fallback_groups=KAFKA_BROKER_NODE_INSTANCE_TYPE_GROUPS,
            fallback_choices=KAFKA_BROKER_NODE_INSTANCE_TYPE_CHOICES,
        )
        self._kafka_versions_meta = version_meta
        self._kafka_broker_node_types_meta = broker_meta

        self.kafka_version.option_groups = version_groups
        self.kafka_version.choices = version_choices
        self.kafka_broker_node_instance_type.option_groups = broker_groups
        self.kafka_broker_node_instance_type.choices = broker_choices

        self._ensure_current_choice(self.kafka_version)
        self._ensure_current_choice(self.kafka_broker_node_instance_type)

    @staticmethod
    def _ensure_current_choice(field) -> None:
        current = str(field.data or "").strip()
        if not current:
            return
        present = {str(v) for v, _ in (field.choices or [])}
        if current in present:
            return
        field.choices = [(current, current)] + list(field.choices or [])
