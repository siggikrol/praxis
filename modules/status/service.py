from __future__ import annotations
import json
import os
import time
from typing import Dict, Any
from pathlib import Path
from datetime import datetime, timezone
import importlib
import re

import requests  # present in your requirements
import boto3
from botocore.config import Config
from botocore.exceptions import BotoCoreError, ClientError
from services.aws_instance_types.common import (
    acquire_lock as _acquire_lock,
    release_lock as _release_lock,
    read_json as _cache_read_json,
    read_json_with_meta as _cache_read_json_with_meta,
    write_json_atomic as _write_json_atomic,
)

QUICK_TTL_SECONDS = int(os.getenv("PS_QUICK_STATUS_TTL", "30"))
_quick_cache: Dict[str, Any] = {}


from services.github_auth import get_github_app_auth


def _quick_cached(name: str) -> Dict[str, Any] | None:
    entry = _quick_cache.get(name)
    if not entry:
        return None
    if time.time() >= entry.get("expires_at", 0):
        return None
    payload = entry.get("payload")
    return dict(payload) if isinstance(payload, dict) else payload


def _quick_store(name: str, payload: Dict[str, Any]) -> Dict[str, Any]:
    _quick_cache[name] = {
        "expires_at": time.time() + QUICK_TTL_SECONDS,
        "payload": dict(payload) if isinstance(payload, dict) else payload,
    }
    return payload


def read_github_rate_limit(
    token: str | None,
    api_base: str = "https://api.github.com",
    timeout: float = 5.0,
) -> Dict[str, Any]:
    """Query GitHub /rate_limit. Returns a dict suitable for templating."""
    out: Dict[str, Any] = {"ok": False, "source": f"{api_base}/rate_limit"}
    headers = {"Accept": "application/vnd.github+json"}
    if token:
        headers["Authorization"] = f"Bearer {token}"

    try:
        r = requests.get(f"{api_base.rstrip('/')}/rate_limit", headers=headers, timeout=timeout)
        out["status_code"] = r.status_code
        if r.ok:
            data = r.json()
            core = data.get("rate") or data.get("resources", {}).get("core", {})
            search = data.get("resources", {}).get("search", {})
            graph = data.get("resources", {}).get("graphql", {})
            out.update({
                "ok": True,
                "core_limit": core.get("limit"),
                "core_remaining": core.get("remaining"),
                "core_reset_epoch": core.get("reset"),
                "search_remaining": search.get("remaining"),
                "graphql_remaining": graph.get("remaining"),
            })
        else:
            out["error"] = f"HTTP {r.status_code}"
            try:
                out["body"] = r.json()
            except Exception:
                out["body"] = r.text[:500]
    except Exception as e:
        out["error"] = str(e)
    return out


def quick_github_status(token: str | None) -> Dict[str, Any]:
    cached = _quick_cached("github")
    if cached is not None:
        return cached

    from flask import current_app
    gh_app = get_github_app_auth(current_app.config) if current_app else None
    app_token = None
    if not token and gh_app:
        try:
            app_token = gh_app.get_token()
        except Exception:
            pass

    rl = read_github_rate_limit(token or app_token, timeout=2.0)
    payload = {
        "ok": bool(rl.get("ok")),
        "remaining": rl.get("core_remaining"),
        "limit": rl.get("core_limit"),
        "error": rl.get("error"),
        "token_present": bool(token or app_token),
        "auth_method": "app" if (app_token and not token) else "pat" if token else "none"
    }
    return _quick_store("github", payload)


def cached_github_status() -> Dict[str, Any]:
    """Return an already-cached GitHub result without authentication or network I/O."""
    from flask import current_app

    config = current_app.config
    configured = all(
        str(config.get(key) or "").strip()
        for key in ("GITHUB_APP_ID", "GITHUB_INSTALLATION_ID", "GITHUB_PRIVATE_KEY")
    )
    cached = _quick_cached("github")
    if cached is not None:
        cached["configured"] = configured
        return cached
    return {
        "ok": None,
        "source": "cache_only",
        "error": "Not checked yet" if configured else "GitHub App configuration incomplete",
        "configured": configured,
        "token_present": False,
        "auth_method": "app" if configured else "none",
    }


def count_server_side_sessions(session_dir: str) -> Dict[str, Any]:
    """Rough count of server-side sessions when SESSION_TYPE=filesystem."""
    p = Path(session_dir)
    if not p.exists():
        return {"ok": True, "count": 0, "dir": session_dir, "note": "session dir missing (no active sessions yet?)"}
    try:
        files = [f for f in p.glob("**/*") if f.is_file()]
        return {"ok": True, "count": len(files), "dir": session_dir}
    except Exception as e:
        return {"ok": False, "error": str(e), "dir": session_dir}

def collect_app_meta(app) -> Dict[str, Any]:
    """Non-sensitive app flags useful for debugging."""
    cfg = app.config
    return {
        "ok": True,
        "app_version": cfg.get("APP_VERSION", "dev"),
        "auth_enabled": cfg.get("AUTH_ENABLED", False),
        "session_type": cfg.get("SESSION_TYPE", ""),
        "session_dir": cfg.get("SESSION_FILE_DIR", ""),
        "github_owner": cfg.get("PS_GITHUB_OWNER"),
        "github_repo": cfg.get("PS_GITHUB_REPO"),
        "git_branch": cfg.get("PS_GIT_BRANCH"),
    }


def collect_modules_status(app) -> Dict[str, Any]:
    """Summarize module availability vs enabled list."""
    modules_dir = Path(app.root_path) / "modules"
    available = []
    for p in modules_dir.iterdir():
        if not (p / "__init__.py").exists():
            continue
        if p.name == "github_helpers":
            continue
        if (p / "plugin.py").exists():
            available.append(p.name)
    available = sorted(available)

    enabled_raw = os.getenv("ENABLED_MODULES", "").strip()
    enabled_list = [s.strip() for s in enabled_raw.split(",") if s.strip()]
    effective = enabled_list or available
    enabled_set = set(effective)

    enabled_details = []
    for name in available:
        detail = {
            "name": name,
            "title": name,
            "description": "",
            "category": "",
            "enabled": name in enabled_set,
        }
        try:
            mod = importlib.import_module(f"modules.{name}.plugin")
            plugin_cls = getattr(mod, "Plugin", None)
            if plugin_cls:
                detail["title"] = getattr(plugin_cls, "title", name) or name
                detail["description"] = getattr(plugin_cls, "description", "") or ""
                detail["category"] = getattr(plugin_cls, "category", "") or ""
        except Exception:
            pass
        enabled_details.append(detail)

    unknown_enabled = sorted([m for m in enabled_list if m not in set(available)])

    home_links = getattr(app, "home_links", []) or []
    plugin_registry = app.extensions.get("plugins", {}) or {}

    return {
        "ok": True,
        "available": available,
        "available_count": len(available),
        "enabled_raw": enabled_raw,
        "enabled": enabled_list,
        "enabled_count": len(effective),
        "enabled_effective": effective,
        "enabled_details": enabled_details,
        "unknown_enabled": unknown_enabled,
        "home_links_count": len(home_links),
        "wizard_plugins_count": len(plugin_registry),
    }


def _env_present(name: str) -> bool:
    return bool(os.getenv(name))


def collect_env_flags() -> Dict[str, Any]:
    """High-level env/config presence checks (no secrets)."""
    try:
        from flask import current_app
    except Exception:
        current_app = None

    def _env_or_cfg(name: str) -> str:
        env_val = os.getenv(name)
        if env_val:
            return env_val
        try:
            if current_app is not None:
                cfg_val = current_app.config.get(name)  # type: ignore[union-attr]
                if cfg_val:
                    return str(cfg_val)
        except Exception:
            pass
        return ""

    cfg_path = os.getenv("SPACELIFT_CONFIG") or ""
    cfg_info = _read_spacelift_config_summary(cfg_path)
    env_token = _env_present("SPACELIFT_API_KEY") or _env_present("SPACELIFT_TOKEN")
    env_keypair = _env_present("SPACELIFT_API_KEY_ID") and _env_present("SPACELIFT_API_KEY_SECRET")
    creds_present = bool(env_token or env_keypair or cfg_info.get("has_token") or cfg_info.get("has_keypair"))
    sources = []
    if env_token or env_keypair:
        sources.append("env")
    if cfg_info.get("has_token") or cfg_info.get("has_keypair"):
        sources.append("config")
    return {
        "ok": True,
        "aws_access_key_id": _env_present("AWS_ACCESS_KEY_ID"),
        "aws_secret_access_key": _env_present("AWS_SECRET_ACCESS_KEY"),
        "aws_default_region": os.getenv("AWS_DEFAULT_REGION") or "",
        "github_app_configured": _env_present("GITHUB_APP_ID") and _env_present("GITHUB_PRIVATE_KEY"),
        "spacelift_account": os.getenv("SPACELIFT_ACCOUNT_NAME") or os.getenv("SPACELIFT_ORG") or "",
        "spacelift_api_url": os.getenv("SPACELIFT_API_URL") or "",
        "spacelift_config_path": cfg_path,
        "spacelift_config": cfg_info,
        "spacelift_api_key": env_token,
        "spacelift_api_key_id": _env_present("SPACELIFT_API_KEY_ID"),
        "spacelift_api_key_secret": _env_present("SPACELIFT_API_KEY_SECRET"),
        "spacelift_creds_present": creds_present,
        "spacelift_creds_source": ", ".join(sources) if sources else "",
        "releases_s3_bucket": _env_or_cfg("PS_RELEASES_S3_BUCKET"),
        "iac_archive_s3_bucket": _env_or_cfg("PS_IAC_ARCHIVE_S3_BUCKET"),
    }


def check_spacelift() -> Dict[str, Any]:
    """Check Spacelift API connectivity (counts only), using a disk cache."""
    started = time.time()
    out: Dict[str, Any] = {
        "ok": False,
        "account": os.getenv("SPACELIFT_ACCOUNT_NAME") or os.getenv("SPACELIFT_ORG") or "",
        "api_url": os.getenv("SPACELIFT_API_URL") or "",
        "aws_integrations": None,
        "spaces": None,
    }
    ttl = _ttl_seconds("PS_SPACELIFT_STATUS_TTL_SECONDS", default=300)
    cache_dir = _cache_dir("PS_SPACELIFT_STATUS_CACHE_DIR", default="/tmp/praxis-cache")
    account_safe = _safe_token(out["account"] or "default")
    cache_path = os.path.join(cache_dir, f"spacelift_status.{account_safe}.json")
    lock_path = f"{cache_path}.lock"

    payload: Dict[str, Any] = {}
    lock_fh = _acquire_lock(lock_path, blocking=True)
    try:
        payload = _cache_read_json(cache_path) or {}
        generated_at = payload.get("generated_at") if isinstance(payload.get("generated_at"), (int, float)) else None
        stale = (generated_at is None) or ((time.time() - float(generated_at)) >= float(ttl))
        if payload and not stale:
            payload = dict(payload)
            payload["source"] = "spacelift_cache"
            payload["cache_path"] = cache_path
            payload["ttl_seconds"] = ttl
            payload["stale"] = False
            payload["generated_at_iso"] = _fmt_utc(generated_at)
            _log_spacelift_status(payload, started=started)
            return payload

        from services.spacelift_api import get_status_counts

        counts = get_status_counts()
        out.update({
            "ok": True,
            "aws_integrations": counts.get("aws_integrations"),
            "spaces": counts.get("spaces"),
        })
        out["generated_at"] = time.time()
        try:
            _write_json_atomic(cache_path, out)
        except Exception as exc:
            out["cache_write_error"] = str(exc)
    except Exception as e:
        # Fallback to stale cached data if available.
        if payload:
            cached_generated_at = payload.get("generated_at") if isinstance(payload.get("generated_at"), (int, float)) else None
            payload = dict(payload)
            payload["source"] = "spacelift_cache"
            payload["cache_path"] = cache_path
            payload["ttl_seconds"] = ttl
            payload["stale"] = True
            payload["generated_at_iso"] = _fmt_utc(cached_generated_at)
            payload["refresh_error"] = str(e)
            _log_spacelift_status(payload, started=started)
            return payload
        out["error"] = str(e)
        out["generated_at"] = time.time()
    finally:
        _release_lock(lock_fh)

    out["source"] = "spacelift_live"
    out["cache_path"] = cache_path
    out["ttl_seconds"] = ttl
    out["stale"] = False
    out["generated_at_iso"] = _fmt_utc(out.get("generated_at"))
    _log_spacelift_status(out, started=started)
    return out


def refresh_spacelift_status_cache() -> Dict[str, Any]:
    """Force-refresh Spacelift status cache and return the refreshed payload."""
    out: Dict[str, Any] = {
        "ok": False,
        "account": os.getenv("SPACELIFT_ACCOUNT_NAME") or os.getenv("SPACELIFT_ORG") or "",
        "api_url": os.getenv("SPACELIFT_API_URL") or "",
        "aws_integrations": None,
        "spaces": None,
    }
    ttl = _ttl_seconds("PS_SPACELIFT_STATUS_TTL_SECONDS", default=300)
    cache_dir = _cache_dir("PS_SPACELIFT_STATUS_CACHE_DIR", default="/tmp/praxis-cache")
    account_safe = _safe_token(out["account"] or "default")
    cache_path = os.path.join(cache_dir, f"spacelift_status.{account_safe}.json")
    lock_path = f"{cache_path}.lock"

    lock_fh = _acquire_lock(lock_path, blocking=True)
    try:
        from services.spacelift_api import get_status_counts

        counts = get_status_counts()
        out.update({
            "ok": True,
            "aws_integrations": counts.get("aws_integrations"),
            "spaces": counts.get("spaces"),
        })
        out["generated_at"] = time.time()
        try:
            _write_json_atomic(cache_path, out)
        except Exception as exc:
            out["cache_write_error"] = str(exc)
    except Exception as e:
        out["error"] = str(e)
        out["generated_at"] = time.time()
        try:
            _write_json_atomic(cache_path, out)
        except Exception:
            pass
    finally:
        _release_lock(lock_fh)

    out["source"] = "spacelift_live_refresh"
    out["cache_path"] = cache_path
    out["ttl_seconds"] = ttl
    out["stale"] = False
    out["generated_at_iso"] = _fmt_utc(out.get("generated_at"))
    return out


def check_spacelift_quick() -> Dict[str, Any]:
    cached = _quick_cached("spacelift")
    if cached is not None:
        return cached
    full = check_spacelift()
    payload = {
        "ok": bool(full.get("ok")),
        "account": full.get("account"),
        "api_url": full.get("api_url"),
        "spaces": full.get("spaces"),
        "error": full.get("error") or full.get("refresh_error"),
        "source": full.get("source"),
        "stale": full.get("stale"),
    }
    return _quick_store("spacelift", payload)


def cached_spacelift_status() -> Dict[str, Any]:
    """Read the Spacelift disk cache only; never refresh it from a request."""
    cached = _quick_cached("spacelift")
    if cached is not None:
        return cached
    account = os.getenv("SPACELIFT_ACCOUNT_NAME") or os.getenv("SPACELIFT_ORG") or ""
    ttl = _ttl_seconds("PS_SPACELIFT_STATUS_TTL_SECONDS", default=300)
    cache_dir = _cache_dir("PS_SPACELIFT_STATUS_CACHE_DIR", default="/tmp/praxis-cache")
    cache_path = os.path.join(cache_dir, f"spacelift_status.{_safe_token(account or 'default')}.json")
    payload = _cache_read_json(cache_path) or {}
    if not payload:
        return {
            "ok": None, "account": account, "source": "cache_only",
            "stale": True, "error": "Not checked yet",
        }
    generated_at = payload.get("generated_at") if isinstance(payload.get("generated_at"), (int, float)) else None
    stale = generated_at is None or (time.time() - float(generated_at)) >= float(ttl)
    result = {
        "ok": bool(payload.get("ok")),
        "account": payload.get("account") or account,
        "api_url": payload.get("api_url"),
        "spaces": payload.get("spaces"),
        "error": payload.get("error") or payload.get("refresh_error"),
        "source": "spacelift_cache",
        "stale": stale,
    }
    return _quick_store("spacelift", result)


def quick_aws_catalog_status() -> Dict[str, Any]:
    cached = _quick_cached("aws_catalogs")
    if cached is not None:
        return cached

    data = collect_aws_catalog_cache_status()
    entries = data.get("entries") if isinstance(data, dict) else None
    entries = entries if isinstance(entries, list) else []
    config = data.get("config") if isinstance(data, dict) else {}
    config = config if isinstance(config, dict) else {}
    configured_regions = [str(r).strip() for r in (config.get("refresh_regions") or []) if str(r).strip()]
    default_aurora_engine = str(config.get("default_aurora_engine") or "").strip()

    expected_entries = []
    for entry in entries:
        if not isinstance(entry, dict):
            continue
        region = str(entry.get("region") or "").strip()
        if configured_regions and region not in configured_regions:
            continue
        catalog = str(entry.get("catalog") or "").strip()
        engine = str(entry.get("engine") or "").strip()
        if catalog in ("aurora", "aurora_versions") and default_aurora_engine and engine and engine != default_aurora_engine:
            continue
        expected_entries.append(entry)
    existing_entries = [e for e in expected_entries if e.get("exists")]
    healthy_entries = [e for e in expected_entries if e.get("source") in ("aws_cache", "curated_ec2_filtered")]
    stale_entries = [e for e in healthy_entries if e.get("stale")]

    state = "missing"
    if healthy_entries and not stale_entries and len(healthy_entries) == len(expected_entries):
        state = "fresh"
    elif healthy_entries:
        state = "stale"
    elif existing_entries:
        state = "fallback"

    payload = {
        "ok": state == "fresh",
        "state": state,
        "label": {
            "fresh": "Fresh",
            "stale": "Stale",
            "fallback": "Fallback",
            "missing": "Missing",
        }.get(state, "Unknown"),
        "entries": len(expected_entries),
        "healthy_entries": len(healthy_entries),
        "stale_entries": len(stale_entries),
        "configured_regions": configured_regions,
        "refresher_enabled": bool(config.get("refresher_enabled")),
    }
    return _quick_store("aws_catalogs", payload)


def quick_pc_source_status() -> Dict[str, Any]:
    cached = _quick_cached("pc_source")
    if cached is not None:
        return cached

    data = collect_pc_source_cache_status()
    entries = data.get("entries") if isinstance(data, dict) else None
    entries = entries if isinstance(entries, list) else []

    expected_entries = [e for e in entries if isinstance(e, dict) and e.get("expected")]
    if not expected_entries:
        expected_entries = [e for e in entries if isinstance(e, dict)]

    existing_entries = [e for e in expected_entries if e.get("exists")]
    healthy_entries = [e for e in expected_entries if e.get("ok")]
    stale_entries = [e for e in healthy_entries if e.get("stale")]

    state = "missing"
    if healthy_entries and not stale_entries and len(healthy_entries) == len(expected_entries):
        state = "fresh"
    elif healthy_entries:
        state = "stale"
    elif existing_entries:
        state = "partial"

    payload = {
        "ok": state == "fresh",
        "state": state,
        "label": {
            "fresh": "Fresh",
            "stale": "Stale",
            "partial": "Partial",
            "missing": "Missing",
        }.get(state, "Unknown"),
        "entries": len(expected_entries),
        "healthy_entries": len(healthy_entries),
        "stale_entries": len(stale_entries),
        "refresh_branches": list((data.get("config") or {}).get("refresh_branches") or []),
        "refresher_enabled": bool((data.get("config") or {}).get("refresher_enabled")),
    }
    return _quick_store("pc_source", payload)


def check_s3_access() -> Dict[str, Any]:
    """Verify access to configured S3 buckets with prefix-scoped list calls."""
    buckets = []
    bucket_configs = (
        ("PS_RELEASES_S3_BUCKET", "PS_RELEASES_S3_PREFIX", "bundles"),
        ("PS_IAC_ARCHIVE_S3_BUCKET", "PS_IAC_ARCHIVE_S3_PREFIX", "terraform-module-archive"),
    )
    for bucket_env, prefix_env, default_prefix in bucket_configs:
        name = os.getenv(bucket_env)
        if name:
            buckets.append({
                "name": name,
                "env_key": bucket_env,
                "prefix": os.getenv(prefix_env) or default_prefix,
            })

    out: Dict[str, Any] = {"ok": True, "buckets": []}
    if not buckets:
        out["note"] = "No S3 buckets configured."
        return out

    cfg = Config(connect_timeout=2, read_timeout=3, retries={"max_attempts": 1})
    s3 = boto3.client("s3", config=cfg)

    for b in buckets:
        entry = {"name": b["name"], "env_key": b["env_key"], "ok": False}
        try:
            raw_prefix = b.get("prefix") or ""
            normalized_prefix = raw_prefix.strip().strip("/")
            list_kwargs = {"Bucket": b["name"], "MaxKeys": 1}
            if normalized_prefix:
                list_kwargs["Prefix"] = f"{normalized_prefix}/"
            s3.list_objects_v2(**list_kwargs)
            entry["ok"] = True
        except ClientError as e:
            entry["error"] = str(e)
            out["ok"] = False
        except BotoCoreError as e:
            entry["error"] = str(e)
            out["ok"] = False
        out["buckets"].append(entry)

    return out


def _read_spacelift_config_summary(path: str) -> Dict[str, Any]:
    out: Dict[str, Any] = {"path": path or "", "exists": False, "has_token": False, "has_keypair": False}
    if not path:
        return out
    p = Path(path)
    if not p.exists():
        return out
    out["exists"] = True
    try:
        text = p.read_text(encoding="utf-8", errors="ignore")
    except Exception as e:
        out["error"] = str(e)
        return out

    token_match = re.search(r'^\s*token\s*=\s*"([^"]+)"\s*$', text, re.MULTILINE)
    if token_match:
        out["has_token"] = True
        return out

    key_id_match = re.search(r"^\s*api_key_id\s*=\s*([^\s#]+)\s*$", text, re.MULTILINE)
    key_secret_match = re.search(r"^\s*api_key_secret\s*=\s*([^\s#]+)\s*$", text, re.MULTILINE)
    if key_id_match and key_secret_match:
        out["has_keypair"] = True
    return out


def check_artiac_status() -> Dict[str, Any]:
    out: Dict[str, Any] = {
        "ok": False,
        "status_code": None,
        "base_url": os.getenv("ARTIAC_STATUS_API_BASE") or "",
        "modules_count": None,
    }
    try:
        from modules.artiac_control.service import fetch_status

        status_code, payload = fetch_status()
        out["status_code"] = status_code
        if isinstance(payload, dict):
            out.update(payload)
            data = payload.get("data") if isinstance(payload, dict) else None
            if isinstance(data, dict):
                mods = data.get("modules") or (data.get("statusFile") or {}).get("modules") or []
                if isinstance(mods, list):
                    out["modules_count"] = len(mods)
    except Exception as e:
        out["error"] = str(e)
    return out


def _fmt_utc(epoch: float | int | None) -> str | None:
    try:
        if epoch is None:
            return None
        return datetime.fromtimestamp(float(epoch), tz=timezone.utc).isoformat()
    except Exception:
        return None


def _log_spacelift_status(payload: Dict[str, Any], *, started: float) -> None:
    try:
        from flask import current_app

        current_app.logger.info(
            "Status Spacelift source=%s stale=%s ok=%s duration_ms=%d cache=%s",
            payload.get("source"),
            payload.get("stale"),
            payload.get("ok"),
            int((time.time() - started) * 1000),
            payload.get("cache_path"),
        )
    except Exception:
        pass


def _safe_token(value: str) -> str:
    """
    Safe token for cache filenames (matches the service's behavior).
    """
    import re

    return re.sub(r"[^a-z0-9-]+", "-", (value or "").strip().lower()) or "unknown"


def _split_csv(raw: str) -> list[str]:
    return [p.strip() for p in (raw or "").split(",") if p.strip()]


def _truthy(raw: str | None) -> bool:
    s = (raw or "").strip().lower()
    return s in ("1", "true", "yes", "y", "on")


def _ttl_seconds(env_var: str, default: int = 24 * 60 * 60) -> int:
    raw = os.getenv(env_var, "").strip()
    if not raw:
        return default
    try:
        v = int(raw)
    except Exception:
        return default
    return v if v > 0 else default


def _cache_dir(env_var: str, default: str = "/tmp/praxis-cache") -> str:
    return (os.getenv(env_var) or default).rstrip("/")


def _configured_refresh_regions() -> list[str]:
    raw = (
        os.getenv("PS_AWS_CATALOG_REFRESH_REGIONS")
        or os.getenv("PS_EKS_INSTANCE_TYPES_REFRESH_REGIONS")
        or os.getenv("AWS_DEFAULT_REGION")
        or "us-east-1"
    )
    return _split_csv(raw) or ["us-east-1"]


def _configured_refresh_interval_seconds() -> int:
    raw = (
        os.getenv("PS_AWS_CATALOG_REFRESH_INTERVAL_SECONDS")
        or os.getenv("PS_EKS_INSTANCE_TYPES_REFRESH_INTERVAL_SECONDS")
        or ""
    ).strip()
    if not raw:
        return 3600
    try:
        v = int(raw)
    except Exception:
        return 3600
    return max(60, v)


def _configured_refresher_enabled() -> bool:
    enabled = os.getenv("PS_AWS_CATALOG_REFRESHER_ENABLED")
    if enabled is None:
        enabled = os.getenv("PS_EKS_INSTANCE_TYPES_REFRESHER_ENABLED", "1")
    return _truthy(enabled)

def available_aws_regions(service_name: str = "ec2") -> list[str]:
    """
    Best-effort list of AWS regions from local botocore metadata (no network).

    This is used only for the /status/aws-catalogs region dropdown. If botocore
    isn't available for some reason, callers can fall back to configured regions.
    """
    try:
        import botocore.session

        sess = botocore.session.get_session()
        regions = sess.get_available_regions(service_name)
        regions = [r.strip() for r in (regions or []) if isinstance(r, str) and r.strip()]
        return sorted(set(regions))
    except Exception:
        return []


def collect_aws_catalog_cache_status(*, focus_region: str | None = None) -> Dict[str, Any]:
    """
    Inspect on-disk AWS-backed option catalogs and return a summary structure.
    Used by the /status/aws-catalogs page.
    """
    if focus_region and not re.fullmatch(r"[a-z0-9-]+", focus_region):
        # Reject any region value containing unexpected characters to avoid
        # influencing filesystem paths with untrusted input.
        focus_region = None
    now = time.time()
    focus_region = (focus_region or "").strip() or None

    from services.aws_instance_types.registry import CATALOGS, BY_KEY, entries as registry_entries
    dirs = {catalog.key: _cache_dir(catalog.directory_env) for catalog in CATALOGS}
    ttls = {catalog.key: _ttl_seconds(catalog.ttl_env) for catalog in CATALOGS}
    default_engine = (os.getenv('PS_AURORA_ENGINE') or 'aurora-postgresql').strip()
    entries = [entry for entry in registry_entries([focus_region] if focus_region else _configured_refresh_regions())
               if not focus_region or entry['region'] == focus_region]

    # Hydrate metadata for rendering.
    out_entries: list[dict[str, Any]] = []
    for e in entries:
        catalog = e["catalog"]
        region = e["region"]
        engine = e.get("engine")
        engine_safe = e.get("engine_safe")
        cache_path = e["cache_path"]

        file_meta = _cache_read_json_with_meta(cache_path)
        exists = bool(file_meta.get("exists"))
        payload_raw = file_meta.get("payload")
        payload = payload_raw if isinstance(payload_raw, dict) else None
        read_err = file_meta.get("error") if isinstance(file_meta.get("error"), str) else None
        mtime_epoch = file_meta.get("mtime_epoch") if isinstance(file_meta.get("mtime_epoch"), (int, float)) else None
        size_bytes = file_meta.get("size_bytes") if isinstance(file_meta.get("size_bytes"), int) else None
        schema = payload.get("schema") if isinstance(payload, dict) else None
        generated_at = payload.get("generated_at") if isinstance(payload, dict) else None
        if not isinstance(generated_at, (int, float)):
            generated_at = None

        ttl = ttls.get(catalog, 24 * 60 * 60)
        age_seconds = (now - float(generated_at)) if generated_at else None
        stale = (age_seconds is None) or (age_seconds >= float(ttl))

        # Derive count and group summaries from payload if present.
        count = payload.get("count") if isinstance(payload, dict) else None
        if not isinstance(count, int):
            count = None

        groups_raw = payload.get("groups") if isinstance(payload, dict) else None
        refresh_state = _cache_read_json(cache_path + '.status.json') or {}
        read_err = read_err or refresh_state.get('error')
        if catalog == 'eks_ami' and (payload or {}).get('ami_id'):
            groups_raw = [['AMI', [payload['ami_id']]]]
            count = 1
        if catalog in ("opensearch", "opensearch_limits"):
            read_err = read_err or (_cache_read_json(cache_path + '.status.json') or {}).get('error')
            raw = (payload or {}).get('data')
            schema = 1 if raw else schema
            if catalog == 'opensearch' and isinstance(raw, list):
                groups_raw = [[role, [row['InstanceType'] for row in raw
                                     if role in row.get('InstanceRole', []) and row.get('InstanceType')]]
                              for role in ('data', 'master')]
                count = len(raw)
            elif isinstance(raw, dict):
                groups_raw = [[role, [f"{key}: {value}" for key, value in limits.get('InstanceLimits', {}).get('InstanceCountLimits', {}).items()]]
                              for role, limits in raw.items()]
                count = len(raw)
        group_summaries: list[dict[str, Any]] = []
        if isinstance(groups_raw, list):
            for g in groups_raw:
                if not (isinstance(g, (list, tuple)) and len(g) == 2 and isinstance(g[0], str) and isinstance(g[1], list)):
                    continue
                label = g[0]
                values = [str(v) for v in g[1] if str(v).strip()]
                group_summaries.append(
                    {
                        "label": label,
                        "count": len(values),
                        "sample": values[:15],
                    }
                )

        if count is None and group_summaries:
            count = sum(gs["count"] for gs in group_summaries if isinstance(gs.get("count"), int))

        # "ok" means: we could read and parse a payload.
        ok = bool(exists and payload and not file_meta.get("error"))

        # "source" should match wizard behavior: if the cache is valid and has groups,
        # the wizard uses it; otherwise it falls back to static constants.
        has_groups = bool(group_summaries)
        schema_ok = (schema == 1)
        source = "aws_cache" if (ok and schema_ok and has_groups) else "fallback"
        if catalog in ("opensearch", "opensearch_limits") and (payload or {}).get("data"):
            source = "aws_cache"
        if source == "aws_cache" and BY_KEY[catalog].provenance != "aws_cache":
            source = (payload or {}).get('source', 'curated_unverified')
        out_entries.append(
            {
                "catalog": catalog,
                "catalog_label": BY_KEY[catalog].label,
                "region": region,
                "engine": engine,
                "engine_safe": engine_safe,
                "cache_path": cache_path,
                "cache_dir": os.path.dirname(cache_path),
                "exists": exists,
                "size_bytes": size_bytes,
                "mtime_epoch": mtime_epoch,
                "mtime_iso": _fmt_utc(mtime_epoch),
                "schema": schema,
                "generated_at_epoch": generated_at,
                "generated_at_iso": _fmt_utc(generated_at),
                "age_seconds": age_seconds,
                "stale": stale,
                "ttl_seconds": ttl,
                "count": count,
                "group_summaries": group_summaries,
                "ok": ok,
                "source": source,
                "error": read_err,
                "last_attempt_iso": _fmt_utc(refresh_state.get("last_attempt_at")),
                "next_retry_iso": _fmt_utc(refresh_state.get("next_retry_at")) if refresh_state.get("error") else None,
                "iam_actions": BY_KEY[catalog].actions,
            }
        )

    # Stable ordering: catalog, region, engine.
    catalog_order = {catalog.key: index for index, catalog in enumerate(CATALOGS)}
    out_entries.sort(
        key=lambda x: (
            catalog_order.get(x.get("catalog"), 99),
            str(x.get("region") or ""),
            str(x.get("engine") or ""),
            str(x.get("cache_path") or ""),
        )
    )

    return {
        "ok": True,
        "now_epoch": now,
        "now_iso": _fmt_utc(now),
        "config": {
            "refresh_regions": _configured_refresh_regions(),
            "refresh_interval_seconds": _configured_refresh_interval_seconds(),
            "refresher_enabled": _configured_refresher_enabled(),
            "default_aurora_engine": default_engine,
            "catalog_labels": {catalog.key: catalog.label for catalog in CATALOGS},
            "cache_dirs": dirs,
            "ttls": ttls,
            "focus_region": focus_region,
        },
        "entries": out_entries,
    }


def _configured_pc_source_refresh_branches() -> list[str]:
    """
    Branches to refresh periodically / via "refresh configured".

    - If PS_PC_SOURCE_REFRESH_BRANCHES is set: comma-separated list.
      Use "default" to represent the repo default branch (no ref).
    - If unset: refresh only "default".
    """
    raw = (os.getenv("PS_PC_SOURCE_REFRESH_BRANCHES") or "").strip()
    if not raw:
        return ["default"]
    out: list[str] = []
    for part in raw.split(","):
        p = (part or "").strip()
        if not p or p.lower() == "default":
            out.append("default")
        else:
            out.append(p)
    # De-dupe while keeping order.
    seen: set[str] = set()
    uniq: list[str] = []
    for b in out:
        key = (b or "").strip().lower()
        if key in seen:
            continue
        seen.add(key)
        uniq.append(b)
    return uniq or ["default"]


def _configured_pc_source_refresh_interval_seconds() -> int:
    raw = (os.getenv("PS_PC_SOURCE_REFRESH_INTERVAL_SECONDS") or "").strip()
    if not raw:
        return 3600
    try:
        v = int(raw)
    except Exception:
        return 3600
    return max(60, v)


def _configured_pc_source_refresher_enabled() -> bool:
    enabled = os.getenv("PS_PC_SOURCE_REFRESHER_ENABLED", "1")
    return _truthy(enabled)


def collect_pc_source_cache_status(*, focus_branch: str | None = None) -> Dict[str, Any]:
    """
    Inspect on-disk PC Source folder-structure cache and return a summary structure.
    Used by the /status/pc-source-cache page.

    focus_branch:
      - None/"": show configured + cached branches
      - "default": show only default branch cache
      - "<branch-name>": show only that branch cache
    """
    now = time.time()
    focus_branch = (focus_branch or "").strip() or None

    cache_dir = _cache_dir("PS_PC_SOURCE_CACHE_DIR", default="/tmp/praxis-cache")
    ttl = _ttl_seconds("PS_PC_SOURCE_CACHE_TTL_SECONDS")
    refresh_branches = _configured_pc_source_refresh_branches()

    # Candidate cache paths: scanned (anything on disk) + expected configured/selected.
    scanned_paths: list[str] = []
    if cache_dir and os.path.isdir(cache_dir):
        try:
            names = sorted(os.listdir(cache_dir))
        except Exception:
            names = []
        for name in names:
            if not (name.startswith("pc_source_sections.") and name.endswith(".json")):
                continue
            scanned_paths.append(os.path.join(cache_dir, name))

    desired_branches = refresh_branches

    expected_by_path: dict[str, str] = {}
    expected_paths: list[str] = []
    for b in desired_branches:
        if not b:
            continue
        b_disp = "default" if b.lower() == "default" else b
        if focus_branch and b_disp != focus_branch:
            continue
        safe = _safe_token(b_disp)
        path = os.path.join(cache_dir, f"pc_source_sections.{safe}.json")
        expected_paths.append(path)
        expected_by_path[path] = b_disp

    all_paths = sorted(set(scanned_paths + expected_paths))

    out_entries: list[dict[str, Any]] = []
    section_order = [
        "bootstrap",
        "deployment",
        "iac",
        "helm",
        "helm_iac_cds",
        "helm_app_cds",
        "helm_bootstrap_cds",
    ]

    for path in all_paths:
        name = os.path.basename(path)
        token = None
        if name.startswith("pc_source_sections.") and name.endswith(".json"):
            token = name[len("pc_source_sections.") : -len(".json")]

        expected_branch = expected_by_path.get(path)
        file_meta = _cache_read_json_with_meta(path)
        exists = bool(file_meta.get("exists"))
        payload_raw = file_meta.get("payload")
        payload = payload_raw if isinstance(payload_raw, dict) else None
        read_err = file_meta.get("error") if isinstance(file_meta.get("error"), str) else None
        mtime_epoch = file_meta.get("mtime_epoch") if isinstance(file_meta.get("mtime_epoch"), (int, float)) else None
        size_bytes = file_meta.get("size_bytes") if isinstance(file_meta.get("size_bytes"), int) else None
        schema = payload.get("schema") if isinstance(payload, dict) else None
        generated_at = payload.get("generated_at") if isinstance(payload, dict) else None
        if not isinstance(generated_at, (int, float)):
            generated_at = None

        payload_branch = payload.get("branch") if isinstance(payload, dict) else None
        branch: str | None
        if isinstance(payload_branch, str) and payload_branch.strip():
            branch = payload_branch.strip()
        elif payload_branch is None and isinstance(payload, dict):
            branch = None  # default branch (no ref)
        elif expected_branch:
            branch = None if expected_branch == "default" else expected_branch
        else:
            # Best-effort fallback: filename token is safe/sanitized.
            branch = None if (token in (None, "", "default")) else str(token)

        branch_display = branch or "default"
        if focus_branch and branch_display != focus_branch:
            continue

        sections_raw = payload.get("sections") if isinstance(payload, dict) else None
        sections = sections_raw if isinstance(sections_raw, dict) else {}

        counts: dict[str, int] = {}
        section_summaries: list[dict[str, Any]] = []
        for sec in section_order:
            raw_vals = sections.get(sec)
            if isinstance(raw_vals, list):
                vals = [str(v) for v in raw_vals if str(v).strip()]
            else:
                vals = []
            counts[sec] = len(vals)
            section_summaries.append({"section": sec, "count": len(vals), "sample": vals[:15]})

        total = payload.get("total") if isinstance(payload, dict) else None
        if not isinstance(total, int):
            total = int(sum(counts.values()))

        payload_error = payload.get("error") if isinstance(payload, dict) else None
        error = None
        if isinstance(payload_error, str) and payload_error.strip():
            error = payload_error.strip()
        if read_err:
            error = read_err

        section_errors = payload.get("section_errors") if isinstance(payload, dict) else None
        if not isinstance(section_errors, dict):
            section_errors = None

        age_seconds = (now - float(generated_at)) if generated_at else None
        stale = (age_seconds is None) or (age_seconds >= float(ttl))

        ok = bool(exists and payload and not read_err and schema == 1 and isinstance(payload.get("sections"), dict))
        source = "pc_cache" if ok else "github"

        out_entries.append(
            {
                "branch": branch,
                "branch_display": branch_display,
                "cache_path": path,
                "cache_dir": os.path.dirname(path),
                "exists": exists,
                "size_bytes": size_bytes,
                "mtime_epoch": mtime_epoch,
                "mtime_iso": _fmt_utc(mtime_epoch),
                "schema": schema,
                "generated_at_epoch": generated_at,
                "generated_at_iso": _fmt_utc(generated_at),
                "age_seconds": age_seconds,
                "stale": stale,
                "ttl_seconds": ttl,
                "total": total,
                "counts": counts,
                "section_summaries": section_summaries,
                "ok": ok,
                "source": source,
                "error": error,
                "section_errors": section_errors,
                "expected": bool(expected_branch is not None),
            }
        )

    out_entries.sort(
        key=lambda x: (
            0 if (x.get("branch_display") == "default") else 1,
            str(x.get("branch_display") or "").lower(),
            str(x.get("cache_path") or ""),
        )
    )

    return {
        "ok": True,
        "now_epoch": now,
        "now_iso": _fmt_utc(now),
        "config": {
            "cache_dir": cache_dir,
            "ttl_seconds": ttl,
            "refresher_enabled": _configured_pc_source_refresher_enabled(),
            "refresh_interval_seconds": _configured_pc_source_refresh_interval_seconds(),
            "refresh_branches": refresh_branches,
            "focus_branch": focus_branch,
        },
        "entries": out_entries,
    }
