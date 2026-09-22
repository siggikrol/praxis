"""YAML spec and exclusion configuration helpers."""

SPEC_SECTION_ORDER = [
    "common",
    "vpc",
    "database",
    "eks",
    "redis",
    "route53",
    "loadbalancer",
    "vault",
]

EXCLUDE_FROM_SPEC = {
    "csrf_token",
    "submit",
    "_csrf_token",
}

EXCLUDE_FROM_PROGRESS = {
    "csrf_token",
    "submit",
    "_csrf_token",
}
