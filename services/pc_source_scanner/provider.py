from __future__ import annotations

import os
from typing import List, Optional
from urllib.parse import quote

from flask import current_app

from services.github_helpers.github_api import (
    _raise_for_status,
    _request,
    list_directory,
    list_branches,
    get_token,
)
from .cache import DEFAULT_TTL_SECONDS, get_cached, set_cached
from .provider_types import PCSections
# Dynamic defaults: prefer GitHub discovery; optional whitelist to prune noisy entries.
HELM_APP_CDS_ALLOWED: tuple[str, ...] = ()
HELM_BOOTSTRAP_CDS_ALLOWED: tuple[str, ...] = ()

# Empty tuple means no static allowlist; uses GitHub discovery
HELM_IAC_CDS_ALLOWED: tuple[str, ...] = ()


def _get_owner_repo_token() -> tuple[str, str, Optional[str]]:
    """
    Reads the GitHub owner and repo from environment variables first, with
    Flask config used as a fallback where applicable.

    The GitHub token is not read directly from env/config here. It is obtained
    by calling `get_token()`, which uses the configured GitHub App
    authentication flow. Ensure the GitHub App settings required by
    `get_token()` are configured for token retrieval to succeed.
    """
    # Environment variables first (correct behaviour in your setup)
    owner = (os.getenv("PS_GITHUB_OWNER") or current_app.config.get("PS_GITHUB_OWNER", "")).strip()
    repo = (
        os.getenv("PC_GENERIC_REPO")
        or os.getenv("PS_GITHUB_REPO")
        or current_app.config.get("PC_GENERIC_REPO")
        or "devops-praxis-core"
    ).strip()

    if not owner:
        raise RuntimeError("PS_GITHUB_OWNER is not configured")
    if not repo:
        raise RuntimeError("PC_GENERIC_REPO / PS_GITHUB_REPO is not configured")

    token = get_token()
    return owner, repo, token


def _cached(key: str, builder, *, ttl: int = DEFAULT_TTL_SECONDS, force: bool = False):
    """
    Lightweight shared cache across requests to avoid repeated GitHub calls.

    force=True bypasses the cache and always calls builder(), while still updating the cache.
    """
    if not force:
        cached = get_cached(key)
        if cached is not None:
            return cached
    val = builder()
    set_cached(key, val, ttl)
    return val


def _fetch_section(section: str, path: str, branch: Optional[str] = None) -> list[str]:
    owner, repo, token = _get_owner_repo_token()

    ref_qs = f"?ref={quote(branch)}" if branch else ""
    api_path = f"/repos/{owner}/{repo}/contents/{path}{ref_qs}"
    current_app.logger.info(f"[PC Source] Fetching {api_path}")

    resp = _request("GET", api_path, token)

    current_app.logger.info(
        f"[PC Source] GitHub status={resp.status_code} path={api_path}"
    )

    if resp.status_code == 404:
        raise RuntimeError(
            f"PC Source path not found: {path} (branch={branch or 'default'})"
        )

    _raise_for_status(resp)

    try:
        items = resp.json()
        names = [i["name"] for i in items if i.get("type") == "dir"]
        current_app.logger.info(f"[PC Source] Found dirs for {section}: {names}")
        return names
    except Exception as exc:
        raise RuntimeError(f"Invalid GitHub response for PC Source path {path}: {exc}") from exc


def _fetch_section_with_children(
    section: str,
    path: str,
    allowed_fallback: tuple[str, ...] | None = None,
    branch: Optional[str] = None,
) -> list[str]:
    """
    Fetch directories under a path and include one level of child directories.
    Example result: ["helm-app-db-resource-deployment/ilp", "helm-cert-manager"].
    Does not recurse deeper than one level.
    """
    owner, repo, token = _get_owner_repo_token()
    try:
        parents = list_directory(owner, repo, path, token, ref=branch)
    except Exception as exc:
        raise RuntimeError(f"Failed to list PC Source section {section} ({path}): {exc}") from exc

    results: list[str] = []
    child_errors: list[str] = []
    for parent in parents:
        try:
            children = list_directory(owner, repo, f"{path}/{parent}", token, ref=branch)
        except Exception as exc:
            child_errors.append(f"{parent}: {exc}")
            continue

        if children:
            results.extend([f"{parent}/{c}" for c in children])
        else:
            results.append(parent)

    if child_errors:
        raise RuntimeError(
            f"Failed to expand PC Source section {section}: " + "; ".join(child_errors)
        )

    current_app.logger.info(
        "[PC Source] Found nested dirs for %s: %s", section, results
    )
    # If nothing was discovered (GitHub down / path mismatch), fall back to allowed list
    if not results and allowed_fallback:
        current_app.logger.info(
            "[PC Source] Using allowed_fallback for %s (%d entries)",
            section,
            len(allowed_fallback),
        )
        return list(allowed_fallback)
    return results


def _filter_allowed(items: list[str], allowed: tuple[str, ...]) -> list[str]:
    allowed_set = set(allowed)
    return sorted([i for i in items if i in allowed_set])



def get_bootstrap_dirs(branch: Optional[str] = None, *, force_refresh: bool = False) -> List[str]:
    cache_key = f"bootstrap_dirs:{branch or 'default'}"
    return _cached(cache_key, lambda: _fetch_section("bootstrap", "SSA/bootstrap", branch=branch), force=force_refresh)


def get_deployment_dirs(branch: Optional[str] = None, *, force_refresh: bool = False) -> List[str]:
    cache_key = f"deployment_dirs:{branch or 'default'}"
    return _cached(cache_key, lambda: _fetch_section("deployment", "SSA/deployment", branch=branch), force=force_refresh)


def get_iac_dirs(branch: Optional[str] = None, *, force_refresh: bool = False) -> List[str]:
    cache_key = f"iac_dirs:{branch or 'default'}"
    return _cached(cache_key, lambda: _fetch_section("iac", "SSA/iac", branch=branch), force=force_refresh)


def get_helm_dirs(branch: Optional[str] = None, *, force_refresh: bool = False) -> List[str]:
    cache_key = f"helm_dirs:{branch or 'default'}"
    return _cached(cache_key, lambda: _fetch_section("helm", "SSA/helm", branch=branch), force=force_refresh)


def get_helm_iac_cds_dirs(branch: Optional[str] = None, *, force_refresh: bool = False) -> List[str]:
    def builder() -> List[str]:
        # Discover helm dirs and expand env-specific subfolders where present.
        owner, repo, token = _get_owner_repo_token()
        parents = list_directory(owner, repo, "SSA/helm", token, ref=branch)

        allowed_suffixes = {"ilp", "cgs", "pmv", "rgs", "rng"}
        results: list[str] = []
        child_errors: list[str] = []

        for parent in parents:
            # Check all parent folders for env-specific children.
            try:
                children = list_directory(owner, repo, f"SSA/helm/{parent}", token, ref=branch)
            except Exception as exc:
                child_errors.append(f"{parent}: {exc}")
                continue

            env_children = [c for c in children if c.lower() in allowed_suffixes]
            if env_children:
                results.extend([f"{parent}/{c}" for c in env_children])
                continue

            # Default: use the parent folder as-is
            results.append(parent)

        if child_errors:
            raise RuntimeError(
                "Failed to expand PC Source section helm_iac_cds: " + "; ".join(child_errors)
            )

        # If nothing was collected, fall back to the original nested listing
        if not results:
            results = _fetch_section_with_children("helm_iac_cds", "SSA/helm", branch=branch)

        return sorted(results)

    cache_key = f"helm_iac_cds_dirs:{branch or 'default'}"
    return _cached(cache_key, builder, force=force_refresh)


def get_helm_app_cds_dirs(branch: Optional[str] = None, *, force_refresh: bool = False) -> List[str]:
    cache_key = f"helm_app_cds_dirs:{branch or 'default'}"
    return _cached(
        cache_key,
        lambda: list(HELM_APP_CDS_ALLOWED) if HELM_APP_CDS_ALLOWED else get_deployment_dirs(branch=branch),
        force=force_refresh,
    )


def get_helm_bootstrap_cds_dirs(branch: Optional[str] = None, *, force_refresh: bool = False) -> List[str]:
    cache_key = f"helm_bootstrap_cds_dirs:{branch or 'default'}"
    return _cached(
        cache_key,
        lambda: list(HELM_BOOTSTRAP_CDS_ALLOWED) if HELM_BOOTSTRAP_CDS_ALLOWED else get_bootstrap_dirs(branch=branch),
        force=force_refresh,
    )


def get_all_sections(branch: Optional[str] = None, *, force_refresh: bool = False) -> PCSections:
    """
    Returns all sections as a single structure.
    """
    return {
        "bootstrap": get_bootstrap_dirs(branch=branch, force_refresh=force_refresh),
        "deployment": get_deployment_dirs(branch=branch, force_refresh=force_refresh),
        "iac": get_iac_dirs(branch=branch, force_refresh=force_refresh),
        "helm": get_helm_dirs(branch=branch, force_refresh=force_refresh),
        "helm_iac_cds": get_helm_iac_cds_dirs(branch=branch, force_refresh=force_refresh),
        "helm_app_cds": get_helm_app_cds_dirs(branch=branch, force_refresh=force_refresh),
        "helm_bootstrap_cds": get_helm_bootstrap_cds_dirs(branch=branch, force_refresh=force_refresh),
    }


def get_repo_branches(*, force_refresh: bool = False) -> List[str]:
    cache_key = "pc_source_branches"
    def builder() -> List[str]:
        owner, repo, token = _get_owner_repo_token()
        return list_branches(owner, repo, token)
    return _cached(cache_key, builder, force=force_refresh)
