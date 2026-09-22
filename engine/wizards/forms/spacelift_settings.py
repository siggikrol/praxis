# forms/spacelift_settings.py
from functools import lru_cache
import os
import re
import time
from datetime import datetime, timezone

from flask import current_app, request, session
from flask_wtf import FlaskForm
from wtforms import FormField, SelectField, StringField
from wtforms.validators import DataRequired, Optional
from engine.wizards.constants.project_constants import ENV_DEFAULT_ENV_TYPES
from engine.wizards.forms.ux import get_env_key, get_env_label
from services.spacelift_api import list_aws_integrations, list_spaces
from engine.wizards.factory.utils import _key
from services.aws_instance_types.common import (
    acquire_lock,
    is_stale,
    payload_generated_at,
    read_json,
    release_lock,
    write_json_atomic,
)


_SPACELIFT_OPTIONS_CACHE_EXPIRES_AT: float = 0.0
_SPACELIFT_OPTIONS_CACHE_SCHEMA = 2
_LAST_AWS_OPTIONS_SOURCE = "unknown"
_LAST_SPACE_OPTIONS_SOURCE = "unknown"
_LAST_AWS_ACCOUNT_BY_INTEGRATION: dict[str, str] = {}


def ensure_spacelift_options_cache(force: bool = False) -> bool:
    """
    Ensure Spacelift option caches are refreshed periodically.

    The dropdown option lists are cached in-process to avoid hammering Spacelift on every
    page view. Without a TTL, new spaces/integrations won't appear until the web process
    is restarted.

    Controlled via `PS_SPACELIFT_OPTIONS_TTL` (seconds). Defaults to 300.
    Set to 0 to effectively disable caching (always clears before use).

    Returns True if caches were cleared.
    """
    global _SPACELIFT_OPTIONS_CACHE_EXPIRES_AT

    try:
        ttl = int(os.getenv("PS_SPACELIFT_OPTIONS_TTL", "300"))
    except Exception:
        ttl = 300

    now = time.time()
    expired = now >= _SPACELIFT_OPTIONS_CACHE_EXPIRES_AT
    cleared = False
    if force or ttl <= 0 or expired:
        _aws_integration_options.cache_clear()
        _space_options.cache_clear()
        if force:
            _drop_spacelift_options_cache("spacelift-aws-integrations")
            _drop_spacelift_options_cache("spacelift-spaces")
        _SPACELIFT_OPTIONS_CACHE_EXPIRES_AT = (now + ttl) if ttl > 0 else 0.0
        cleared = True

    # Warm once after clear so the next form render can use cache metadata consistently.
    if cleared and ttl > 0:
        try:
            _aws_integration_options("")
            _space_options("")
        except Exception as exc:
            current_app.logger.warning("Spacelift: cache warmup failed: %s", exc)

    return cleared


def spacelift_bootstrap_options() -> tuple[list[tuple[str, str]], list[tuple[str, str]]]:
    """Return the same cached integration and space choices used by the wizard."""
    ensure_spacelift_options_cache(force=False)
    return list(_aws_integration_options("")), list(_sanitize_spaces(_space_options("")))


def _spacelift_options_ttl_seconds() -> int:
    try:
        ttl = int(os.getenv("PS_SPACELIFT_OPTIONS_TTL", "300"))
    except Exception:
        ttl = 300
    return max(0, ttl)


def _fmt_utc(epoch: float | int | None) -> str | None:
    try:
        if epoch is None:
            return None
        return datetime.fromtimestamp(float(epoch), tz=timezone.utc).isoformat()
    except Exception:
        return None


def _spacelift_options_cache_dir() -> str:
    return (
        os.getenv("PS_SPACELIFT_OPTIONS_CACHE_DIR")
        or os.getenv("PS_CACHE_DIR")
        or "/tmp/praxis-cache"
    ).rstrip("/")


def _spacelift_account_token() -> str:
    raw = (
        os.getenv("SPACELIFT_ACCOUNT_NAME")
        or os.getenv("SPACELIFT_ORG")
        or "default"
    )
    return re.sub(r"[^a-z0-9-]+", "-", (raw or "").strip().lower()) or "default"


def _spacelift_options_cache_paths(kind: str) -> tuple[str, str]:
    safe_kind = re.sub(r"[^a-z0-9-]+", "-", (kind or "").strip().lower()) or "spacelift-options"
    path = os.path.join(_spacelift_options_cache_dir(), f"{safe_kind}.{_spacelift_account_token()}.json")
    return path, f"{path}.lock"


def _account_id_from_role_arn(role_arn: str) -> str:
    match = re.match(r"^arn:aws:iam::(\d{12}):role/.+$", (role_arn or "").strip())
    return match.group(1) if match else ""


def _normalize_options(raw: object) -> list[tuple[str, str]]:
    out: list[tuple[str, str]] = []
    for item in (raw or []):
        if not isinstance(item, (list, tuple)) or len(item) < 2:
            continue
        value = str(item[0] or "").strip()
        label = str(item[1] or "").strip()
        if not value:
            continue
        out.append((value, label or value))
    return out


def _filter_options(options: list[tuple[str, str]], filter_prefix: str = "") -> list[tuple[str, str]]:
    token = (filter_prefix or "").strip().lower()
    if not token:
        return list(options)
    filtered: list[tuple[str, str]] = []
    for value, label in options:
        v = (value or "").lower()
        l = (label or "").lower()
        if token in v or token in l:
            filtered.append((value, label))
    return filtered


def _account_from_integration_map(integration_id: str) -> str:
    integration_id = (integration_id or "").strip()
    if not integration_id:
        return ""
    account_id = _LAST_AWS_ACCOUNT_BY_INTEGRATION.get(integration_id) or ""
    if account_id:
        return account_id
    lowered = integration_id.lower()
    for key, value in _LAST_AWS_ACCOUNT_BY_INTEGRATION.items():
        if key.lower() == lowered:
            return value
    return ""


def get_aws_integration_account_id(integration_id: str) -> str:
    """
    Resolve AWS account ID for a Spacelift AWS integration ID.

    Resolution order:
      1) current worker memory map
      2) disk cache payload (if fresh)
      3) refresh integration options (cache/live)
    """
    integration_id = (integration_id or "").strip()
    if not integration_id:
        return ""

    account_id = _account_from_integration_map(integration_id)
    if account_id:
        return account_id

    _read_spacelift_options_cache("spacelift-aws-integrations")
    account_id = _account_from_integration_map(integration_id)
    if account_id:
        return account_id

    try:
        _aws_integration_options("")
    except Exception:
        return ""
    return _account_from_integration_map(integration_id)


def get_cached_aws_account_choices() -> list[tuple[str, str]]:
    """Return account IDs already discovered by the wizard's Spacelift cache, without a live API call."""
    options = _read_spacelift_options_cache("spacelift-aws-integrations") or []
    labels_by_integration = dict(options)
    account_labels: dict[str, list[str]] = {}
    for integration_id, account_id in _LAST_AWS_ACCOUNT_BY_INTEGRATION.items():
        account_id = str(account_id or "").strip()
        if not re.fullmatch(r"\d{12}", account_id):
            continue
        label = labels_by_integration.get(integration_id) or integration_id
        account_labels.setdefault(account_id, []).append(label)
    return [
        (account_id, f"{account_id} — {', '.join(sorted(set(labels)))}")
        for account_id, labels in sorted(account_labels.items())
    ]


def _read_spacelift_options_cache(kind: str) -> list[tuple[str, str]] | None:
    global _LAST_AWS_ACCOUNT_BY_INTEGRATION
    ttl = _spacelift_options_ttl_seconds()
    if ttl <= 0:
        return None
    cache_path, _lock_path = _spacelift_options_cache_paths(kind)
    payload = read_json(cache_path) or {}
    if payload.get("schema") != _SPACELIFT_OPTIONS_CACHE_SCHEMA:
        return None
    generated_at = payload_generated_at(payload)
    if is_stale(generated_at, ttl):
        return None
    if kind == "spacelift-aws-integrations":
        raw_accounts = payload.get("integration_accounts")
        if not isinstance(raw_accounts, dict):
            return None
        if isinstance(raw_accounts, dict):
            _LAST_AWS_ACCOUNT_BY_INTEGRATION = {
                str(k).strip(): str(v).strip()
                for k, v in raw_accounts.items()
                if str(k).strip() and str(v).strip()
            }
    options = _normalize_options(payload.get("options"))
    if not options:
        return None
    return options


def _write_spacelift_options_cache(
    kind: str,
    options: list[tuple[str, str]],
    *,
    integration_accounts: dict[str, str] | None = None,
) -> None:
    if not options:
        return
    cache_path, lock_path = _spacelift_options_cache_paths(kind)
    lock_fh = acquire_lock(lock_path, blocking=True)
    try:
        payload = {
            "schema": _SPACELIFT_OPTIONS_CACHE_SCHEMA,
            "kind": kind,
            "generated_at": time.time(),
            "options": [[value, label] for value, label in options],
        }
        if kind == "spacelift-aws-integrations" and isinstance(integration_accounts, dict):
            payload["integration_accounts"] = {
                str(k).strip(): str(v).strip()
                for k, v in integration_accounts.items()
                if str(k).strip() and str(v).strip()
            }
        write_json_atomic(
            cache_path,
            payload,
        )
    except Exception as exc:
        current_app.logger.warning("Spacelift: unable to write %s cache: %s", kind, exc)
    finally:
        release_lock(lock_fh)


def _drop_spacelift_options_cache(kind: str) -> None:
    cache_path, lock_path = _spacelift_options_cache_paths(kind)
    lock_fh = acquire_lock(lock_path, blocking=True)
    try:
        if os.path.exists(cache_path):
            os.remove(cache_path)
    except Exception as exc:
        current_app.logger.warning("Spacelift: unable to clear %s cache: %s", kind, exc)
    finally:
        release_lock(lock_fh)


def _spacelift_options_cache_status() -> dict:
    ttl = _spacelift_options_ttl_seconds()
    expires_at = _SPACELIFT_OPTIONS_CACHE_EXPIRES_AT
    now = time.time()
    stale = ttl <= 0 or expires_at <= 0 or now >= expires_at
    return {
        "ttl_seconds": ttl,
        "expires_at": expires_at if expires_at > 0 else None,
        "expires_at_iso": _fmt_utc(expires_at if expires_at > 0 else None),
        "stale": stale,
    }


def _repo_prefix_token() -> str:
    try:
        env_slug = (request.view_args or {}).get("env_slug")
    except RuntimeError:
        return ""
    if not env_slug:
        return ""
    repo_cfg = session.get(_key(env_slug, "repository_settings"), {}) or {}
    prefix = (repo_cfg.get("new_prefix") or "").strip().lower()
    if not prefix:
        return ""
    return prefix.split("-")[0]


def _wizard_env_slug() -> str:
    try:
        bp = (request.blueprint or "").strip()
    except RuntimeError:
        return ""
    if bp.endswith("_wizard"):
        return bp[: -len("_wizard")]
    return bp

def _sanitize_spaces(space_opts: list[tuple[str, str]]) -> list[tuple[str, str]]:
    return [
        opt for opt in space_opts
        if opt[0].lower() != "root"
        and opt[1].lower() != "root"
        and not opt[1].lower().startswith("root (")
    ]

def _count_matches(options: list[tuple[str, str]], token: str) -> int:
    tok = (token or "").strip().lower()
    if not tok:
        return len(options)
    hits = 0
    for value, label in options:
        v = (value or "").lower()
        l = (label or "").lower()
        if tok in v or tok in l:
            hits += 1
    return hits


@lru_cache(maxsize=4)
def _aws_integration_options(filter_prefix: str = "") -> list[tuple[str, str]]:
    global _LAST_AWS_OPTIONS_SOURCE
    global _LAST_AWS_ACCOUNT_BY_INTEGRATION
    cached = _read_spacelift_options_cache("spacelift-aws-integrations")
    if cached is not None:
        _LAST_AWS_OPTIONS_SOURCE = "spacelift_options_cache"
        return _filter_options(cached, filter_prefix)

    try:
        current_app.logger.info("Spacelift: fetching AWS integrations")
        items = list_aws_integrations()
    except Exception as exc:
        current_app.logger.warning("Spacelift: unable to load AWS integrations: %s", exc)
        _LAST_AWS_OPTIONS_SOURCE = "spacelift_live_error"
        return []
    current_app.logger.info("Spacelift: AWS integrations fetched count=%d", len(items))
    options: list[tuple[str, str]] = []
    account_by_integration: dict[str, str] = {}
    for item in items:
        if not isinstance(item, dict):
            continue
        value = str(item.get("id") or "").strip()
        name = str(item.get("name") or "").strip() or value
        if not value:
            continue
        if value.lower() == "root" or name.lower() == "root":
            continue
        account_id = _account_id_from_role_arn(str(item.get("roleArn") or ""))
        if account_id:
            account_by_integration[value] = account_id
        label = f"{name} ({value})" if name and name != value else value
        options.append((value, label))
    options = sorted(options, key=lambda opt: opt[1])
    _LAST_AWS_ACCOUNT_BY_INTEGRATION = dict(account_by_integration)
    _write_spacelift_options_cache(
        "spacelift-aws-integrations",
        options,
        integration_accounts=account_by_integration,
    )
    _LAST_AWS_OPTIONS_SOURCE = "spacelift_live"
    current_app.logger.info("Spacelift: AWS integrations options count=%d", len(options))
    return _filter_options(options, filter_prefix)


@lru_cache(maxsize=4)
def _space_options(filter_prefix: str = "") -> list[tuple[str, str]]:
    global _LAST_SPACE_OPTIONS_SOURCE
    cached = _read_spacelift_options_cache("spacelift-spaces")
    if cached is not None:
        _LAST_SPACE_OPTIONS_SOURCE = "spacelift_options_cache"
        return _filter_options(cached, filter_prefix)

    try:
        current_app.logger.info("Spacelift: fetching spaces")
        items = list_spaces()
    except Exception as exc:
        current_app.logger.warning("Spacelift: unable to load spaces: %s", exc)
        _LAST_SPACE_OPTIONS_SOURCE = "spacelift_live_error"
        return []
    current_app.logger.info("Spacelift: spaces fetched count=%d", len(items))
    options: list[tuple[str, str]] = []
    for item in items:
        if not isinstance(item, dict):
            continue
        value = item.get("id") or ""
        name = item.get("name") or value
        if not value:
            continue
        label = f"{name} ({value})" if name and name != value else value
        options.append((value, label))
    options = sorted(options, key=lambda opt: opt[1])
    _write_spacelift_options_cache("spacelift-spaces", options)
    _LAST_SPACE_OPTIONS_SOURCE = "spacelift_live"
    current_app.logger.info("Spacelift: spaces options count=%d", len(options))
    return _filter_options(options, filter_prefix)

class SpaceliftEnvForm(FlaskForm):
    class Meta:
        csrf = False   # ← IMPORTANT: disable CSRF for nested form

    aws_integration_id = SelectField(
        "AWS Integration ID",
        validators=[
            DataRequired(message="AWS Integration ID is required."),
        ],
        render_kw={
            "required": True,
        },
        description="Exact AWS integration ID in Spacelift (e.g., 'PRAXIS-Catalyst-Sandbox').",
    )

    spacelift_space_id = SelectField(
        "Spacelift Space ID",
        validators=[
            DataRequired(message="Spacelift Space ID is required."),
        ],
        render_kw={
            "required": True,
        },
        description="Exact Spacelift space ID (slug). Example: 'praxis-catalyst-sandbox'.",
    )

    description = StringField(
        "Description",
        validators=[Optional()],
        render_kw={"placeholder": "PRAXIS PGP Catalyst Sandbox default Setup"},
        description="Human-readable description for this Spacelift environment (optional).",
    )

class SpaceliftSettingsForm(FlaskForm):
    @classmethod
    def for_envs(cls, env_types):
        env_types = env_types or ENV_DEFAULT_ENV_TYPES
        
        # Build a subclass with a FormField per env
        attrs = {
            get_env_key(env): FormField(SpaceliftEnvForm, description=f"Spacelift settings for {get_env_label(env)}")
            for env in env_types
        }

        def _init(self, *args, **kwargs):
            super(SpaceliftSettingsForm, self).__init__(*args, **kwargs)
            repo_prefix = _repo_prefix_token()
            # Server-side filtering is confusing because the UI already has a client-side
            # filter box. Always load the full option list and let the user filter.
            aws_before = _aws_integration_options.cache_info()
            space_before = _space_options.cache_info()
            aws_opts = _aws_integration_options("")
            space_opts = _sanitize_spaces(_space_options(""))
            aws_after = _aws_integration_options.cache_info()
            space_after = _space_options.cache_info()
            from_live = (aws_after.misses > aws_before.misses) or (space_after.misses > space_before.misses)
            refreshed = False
            if not aws_opts and not space_opts:
                current_app.logger.info("Spacelift: empty cache detected; refreshing once")
                _aws_integration_options.cache_clear()
                _space_options.cache_clear()
                refreshed = True
                aws_before = _aws_integration_options.cache_info()
                space_before = _space_options.cache_info()
                aws_opts = _aws_integration_options("")
                space_opts = _sanitize_spaces(_space_options(""))
                aws_after = _aws_integration_options.cache_info()
                space_after = _space_options.cache_info()
                from_live = (aws_after.misses > aws_before.misses) or (space_after.misses > space_before.misses)

            aws_match = _count_matches(aws_opts, repo_prefix)
            space_match = _count_matches(space_opts, repo_prefix)
            fallback = bool(repo_prefix) and (aws_match == 0 or space_match == 0)
            cache_meta = _spacelift_options_cache_status()
            source = "spacelift_options_cache"
            if from_live:
                live_sources = {_LAST_AWS_OPTIONS_SOURCE, _LAST_SPACE_OPTIONS_SOURCE}
                if "spacelift_live" in live_sources:
                    source = "spacelift_live"
                elif "spacelift_live_error" in live_sources:
                    source = "spacelift_live_error"

            current_app.logger.info(
                "Spacelift: applying options aws=%d spaces=%d filter=%s aws_match=%d space_match=%d refreshed=%s fallback=%s source=%s stale=%s aws_source=%s space_source=%s",
                len(aws_opts),
                len(space_opts),
                repo_prefix or "",
                aws_match,
                space_match,
                refreshed,
                fallback,
                source,
                cache_meta["stale"],
                _LAST_AWS_OPTIONS_SOURCE,
                _LAST_SPACE_OPTIONS_SOURCE,
            )
            self._spacelift_meta = {
                "filter_prefix": repo_prefix or "",
                "aws_count": len(aws_opts),
                "space_count": len(space_opts),
                "aws_match": aws_match,
                "space_match": space_match,
                "refreshed": refreshed,
                "fallback": fallback,
                "empty": not aws_opts and not space_opts,
                "source": source,
                "stale": cache_meta["stale"],
                "ttl_seconds": cache_meta["ttl_seconds"],
                "expires_at": cache_meta["expires_at"],
                "expires_at_iso": cache_meta["expires_at_iso"],
                "cache_kind": "worker+disk",
            }
            env_slug = _wizard_env_slug()
            if env_slug:
                # Cleanup legacy internal session artifact so it never leaks into spec output.
                legacy_key = _key(env_slug, "spacelift_aws_accounts")
                if legacy_key in session:
                    session.pop(legacy_key, None)
                    session.modified = True
            for _, field in self._fields.items():
                sub = getattr(field, "form", None)
                if not sub:
                    continue
                aws_field = getattr(sub, "aws_integration_id", None)
                space_field = getattr(sub, "spacelift_space_id", None)
                if aws_field is not None:
                    aws_field.choices = [("", "Select an option…")] + list(aws_opts)
                if space_field is not None:
                    space_field.choices = [("", "Select an option…")] + list(space_opts)

        dyn = type("SpaceliftSettingsFormDynamic", (cls,), attrs)
        dyn.__init__ = _init
        return dyn
