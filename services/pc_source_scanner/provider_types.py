from __future__ import annotations

from typing import Dict, List, TypedDict


class PCSections(TypedDict):
    bootstrap: List[str]
    deployment: List[str]
    iac: List[str]
    helm: List[str]
    helm_iac_cds: List[str]
    helm_app_cds: List[str]
    helm_bootstrap_cds: List[str]
