from __future__ import annotations

import hashlib
import json

import yaml


def spec_fingerprint(spec_yaml: str) -> str:
    """Hash YAML meaning rather than comments, whitespace, or key ordering."""
    if not isinstance(spec_yaml, str) or not spec_yaml.strip():
        return ""
    try:
        parsed = yaml.safe_load(spec_yaml)
        canonical = json.dumps(
            parsed,
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=False,
        )
    except (yaml.YAMLError, TypeError, ValueError):
        canonical = spec_yaml
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()
