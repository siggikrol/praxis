# Generic configuration constants for Kafka (MSK) form.

KAFKA_BOOL_CHOICES = [("true", "true"), ("false", "false")]

KAFKA_VERSION_CHOICES = [
    ("3.8.0", "3.8.0"),
    ("3.7.1", "3.7.1"),
    ("3.6.0", "3.6.0"),
]

KAFKA_VERSION_GROUPS = [
    ("Kafka Versions", KAFKA_VERSION_CHOICES),
]

KAFKA_ENCRYPTION_IN_TRANSIT_CLIENT_BROKER_CHOICES = [
    ("TLS_PLAINTEXT", "TLS_PLAINTEXT"),
    ("TLS", "TLS"),
    ("PLAINTEXT", "PLAINTEXT"),
]

KAFKA_BROKER_NODE_INSTANCE_TYPE_CHOICES = [
    ("kafka.m5.large", "kafka.m5.large"),
    ("kafka.m5.xlarge", "kafka.m5.xlarge"),
    ("kafka.m5.2xlarge", "kafka.m5.2xlarge"),
    ("kafka.m7g.large", "kafka.m7g.large"),
    ("kafka.m7g.xlarge", "kafka.m7g.xlarge"),
    ("kafka.m7g.2xlarge", "kafka.m7g.2xlarge"),
]

KAFKA_BROKER_NODE_INSTANCE_TYPE_GROUPS = [
    ("Broker Node Instance Types", KAFKA_BROKER_NODE_INSTANCE_TYPE_CHOICES),
]

KAFKA_DEFAULTS = {
    "kafka_version": "3.8.0",
    "kafka_number_of_broker_nodes": 3,
    "kafka_scaling_max_capacity": 250,
    "kafka_broker_node_instance_type": "kafka.m7g.large",
    "kafka_encryption_in_transit_client_broker": "TLS_PLAINTEXT",
    "kafka_encryption_in_transit_in_cluster": "true",
    "kafka_jmx_exporter_enabled": "false",
    "kafka_node_exporter_enabled": "false",
    "kafka_cloudwatch_logs_enabled": "false",
}

KAFKA_VALIDATION_LIMITS = {
    "broker_nodes_min": 1,
    "scaling_capacity_min": 1,
}
