from __future__ import annotations

import os
from functools import lru_cache
from typing import Dict, List

import yaml
from flask import current_app

from .provider_types import PCSections


@lru_cache(maxsize=1)
def _load_profiles() -> Dict[str, Dict[str, List[str] | Dict[str, List[str]]]]:
    """
    Loads profile definitions from YAML if present.

    Structure:
      profiles:
        multi-vpc:
          bootstrap:
            - bootstrap-ilp
          deployment:
            - application-ilp
          iac:
            - alb
          helm:
            - helm-monitoring
    """
    # Prefer the bundled config next to this module; fall back to app-level config.
    module_root = os.path.dirname(__file__)
    candidates = [
        os.path.join(module_root, "config", "pc_whitelist_profiles.yaml"),
    ]
    try:
        candidates.append(os.path.join(current_app.root_path, "config", "pc_whitelist_profiles.yaml"))
    except Exception:
        # In case this is called outside an app context.
        pass

    cfg_path = next((p for p in candidates if p and os.path.exists(p)), None)

    if not cfg_path:
        return {}

    try:
        current_app.logger.info("[pc_source] Loading profile defaults from %s", cfg_path)
    except Exception:
        pass

    with open(cfg_path, "r", encoding="utf-8") as fh:
        data = yaml.safe_load(fh) or {}

    profiles = data.get("profiles") or {}
    result: Dict[str, Dict[str, List[str]]] = {}
    for name, sections in profiles.items():
        if not isinstance(sections, dict):
            continue
        normalized: Dict[str, List[str] | Dict[str, List[str]]] = {}
        for section_name in (
            "bootstrap",
            "deployment",
            "iac",
            "helm",
            "helm_iac_cds",
            "helm_app_cds",
            "helm_bootstrap_cds",
        ):
            values = sections.get(section_name) or []
            if isinstance(values, list):
                normalized[section_name] = [str(v) for v in values]
        blacklist = sections.get("blacklist") or {}
        if isinstance(blacklist, dict):
            normalized["blacklist"] = {
                "substrings": [str(v) for v in (blacklist.get("substrings") or []) if isinstance(v, str)],
                "exact": [str(v) for v in (blacklist.get("exact") or []) if isinstance(v, str)],
            }
        result[str(name)] = normalized
    return result


def get_profile_defaults(profile_name: str | None, branch: str | None = None) -> PCSections:
    """
    Returns default selections for a given profile name.

    Special handling:
      - None / "custom": no defaults
      - "full": use all current GitHub directories
    """
    from .structure_cache import get_pc_source_sections  # local import to avoid cycles

    if not profile_name or profile_name == "custom":
        return {
            "bootstrap": [],
            "deployment": [],
            "iac": [],
            "helm": [],
            "helm_iac_cds": [],
            "helm_app_cds": [],
            "helm_bootstrap_cds": [],
        }

    if profile_name in ("full", "unified"):
        sections, _meta = get_pc_source_sections(branch=branch, refresh_async_if_stale=False)
        return sections

    profiles = _load_profiles()
    base = profiles.get(profile_name, {})
    defaults = {
        "bootstrap": base.get("bootstrap", []),
        "deployment": base.get("deployment", []),
        "iac": base.get("iac", []),
        "helm": base.get("helm", []),
        "helm_iac_cds": base.get("helm_iac_cds", []),
        "helm_app_cds": base.get("helm_app_cds", []),
        "helm_bootstrap_cds": base.get("helm_bootstrap_cds", []),
    }

    # Dynamic RGS profile: auto-pick any matching entries; allow explicit YAML to override.
    if profile_name == "rgs":
        sections, _meta = get_pc_source_sections(branch=branch, refresh_async_if_stale=False)

        def _pick(section_name: str) -> list[str]:
            return [
                n for n in sections.get(section_name, [])
                if "rgs" in n.lower() or "rng" in n.lower()
            ]

        if not defaults["bootstrap"]:
            defaults["bootstrap"] = _pick("bootstrap")
        if not defaults["helm_bootstrap_cds"]:
            defaults["helm_bootstrap_cds"] = _pick("helm_bootstrap_cds")
        if not defaults["deployment"]:
            defaults["deployment"] = _pick("deployment")
        if not defaults["helm_app_cds"]:
            defaults["helm_app_cds"] = _pick("helm_app_cds")
        # IAC is intentionally left as-is (currently correct), and helm/helm_iac_cds are skipped.

    # Dynamic Catalyst profile: select all, except exclude any IAC entries containing "rgs".
    if profile_name == "catalyst":
        sections, _meta = get_pc_source_sections(branch=branch, refresh_async_if_stale=False)
        blacklist = base.get("blacklist") if isinstance(base, dict) else {}
        deny_substrings = tuple((blacklist or {}).get("substrings") or [])
        deny_exact = set((blacklist or {}).get("exact") or [])

        def _allowed(name: str) -> bool:
            lower = name.lower()
            if lower in deny_exact:
                return False
            return not any(s in lower for s in deny_substrings)

        def _filtered(key: str) -> list[str]:
            return [n for n in sections.get(key, []) if isinstance(n, str) and _allowed(n)]

        if blacklist:
            return {
                "bootstrap": _filtered("bootstrap"),
                "deployment": _filtered("deployment"),
                "iac": _filtered("iac"),
                "helm": _filtered("helm"),
                "helm_iac_cds": _filtered("helm_iac_cds"),
                "helm_app_cds": _filtered("helm_app_cds"),
                "helm_bootstrap_cds": _filtered("helm_bootstrap_cds"),
            }

    return defaults
