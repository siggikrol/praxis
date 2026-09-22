from __future__ import annotations

import json
import logging
import os
import random
import re
import threading
import tempfile
import time
from dataclasses import dataclass
from typing import Any, Iterable

try:
    import fcntl  # type: ignore
except Exception:  # pragma: no cover (non-POSIX)
    fcntl = None  # type: ignore


logger = logging.getLogger(__name__)

DEFAULT_TTL_SECONDS = 24 * 60 * 60
DEFAULT_CACHE_DIR = "/tmp/praxis-cache"
DEFAULT_REGION = "us-east-1"
_SAFE_BASENAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,200}$")


@dataclass(frozen=True)
class AwsCatalogMeta:
    catalog: str
    region: str
    cache_path: str
    generated_at: float | None
    stale: bool
    count: int
    source: str  # aws_cache | fallback
    error: str | None = None


class BackgroundRefreshRegistry:
    """
    In-process background refresh thread registry.

    This prevents a single worker process from spawning multiple refresh threads for
    the same logical task (e.g. "eks:eu-central-1").
    """

    _threads: dict[str, threading.Thread] = {}
    _lock = threading.Lock()

    @classmethod
    def spawn(cls, name: str, target) -> bool:
        with cls._lock:
            cls._threads = {key: thread for key, thread in cls._threads.items() if thread.is_alive()}
            existing = cls._threads.get(name)
            if existing and existing.is_alive():
                return False
            t = threading.Thread(target=target, name=name, daemon=True)
            cls._threads[name] = t
            t.start()
            return True


def normalize_region(region: str | None) -> str:
    r = (region or "").strip()
    return r or (os.getenv("AWS_DEFAULT_REGION") or DEFAULT_REGION)


def ttl_seconds(env_var: str, default: int = DEFAULT_TTL_SECONDS) -> int:
    raw = os.getenv(env_var)
    if not raw:
        return default
    try:
        v = int(raw)
    except Exception:
        return default
    return v if v > 0 else default


def _normalize_path(path: str) -> str:
    raw = (path or "").strip()
    if not raw:
        raise ValueError("path is required")
    if "\x00" in raw:
        raise ValueError("path contains null byte")
    return os.path.abspath(os.path.normpath(raw))


def _allowed_roots() -> tuple[str, ...]:
    """
    Return canonical filesystem roots that this process is allowed to read/write.

    Keeping file operations under these roots prevents path-injection style writes
    to arbitrary locations while still allowing normal cache/session operation.
    """
    roots = [
        DEFAULT_CACHE_DIR,
        tempfile.gettempdir(),
        "/tmp",
        "/var/tmp",
    ]
    # Deployment-controlled cache/session locations used across the app.
    env_dir_vars = (
        "PS_EKS_CLUSTER_VERSIONS_CACHE_DIR",
        "PS_EKS_INSTANCE_TYPES_CACHE_DIR",
        "PS_EKS_AMI_CACHE_DIR",
        "PS_AURORA_ENGINE_VERSIONS_CACHE_DIR",
        "PS_AURORA_INSTANCE_CLASSES_CACHE_DIR",
        "PS_REDIS_NODE_TYPES_CACHE_DIR",
        "PS_KAFKA_CATALOG_CACHE_DIR",
        "PS_OPENSEARCH_CATALOG_CACHE_DIR",
        "PS_AWS_CATALOG_JOB_DIR",
        "PS_PC_SOURCE_CACHE_DIR",
        "PS_SPACELIFT_STATUS_CACHE_DIR",
        "PS_SPACELIFT_OPTIONS_CACHE_DIR",
        "PS_CACHE_DIR",
        "SESSION_FILE_DIR",
    )
    # File-path env vars; we allow their parent directories.
    env_file_vars = (
        "PS_ACTIVE_SESSIONS_REGISTRY",
        "PS_VALIDATE_CACHES_ON_STARTUP_LOCK_PATH",
        "PS_VALIDATE_CACHES_ON_STARTUP_DONE_PATH",
    )
    for name in env_dir_vars:
        raw = (os.getenv(name) or "").strip()
        if raw:
            roots.append(raw)
    for name in env_file_vars:
        raw = (os.getenv(name) or "").strip()
        if raw:
            roots.append(os.path.dirname(raw) or raw)

    out: list[str] = []
    for root in roots:
        try:
            normalized = _normalize_path(root)
        except Exception:
            continue
        if normalized == os.path.sep:
            # Do not allow "/" as a root; that would effectively disable path scoping.
            continue
        if normalized not in out:
            out.append(normalized)
    return tuple(out)


def _is_within_any_root(path: str, roots: tuple[str, ...]) -> bool:
    for root in roots:
        try:
            if os.path.commonpath([path, root]) == root:
                return True
        except Exception:
            continue
    return False


def _match_allowed_root(path: str) -> str | None:
    normalized = _normalize_path(path)
    roots = tuple(sorted(_allowed_roots(), key=len, reverse=True))
    for root in roots:
        root_norm = _normalize_path(root)
        prefix = root_norm if root_norm.endswith(os.sep) else (root_norm + os.sep)
        if normalized == root_norm or normalized.startswith(prefix):
            return root_norm
    return None


def _assert_allowed_path(path: str) -> str:
    normalized = _normalize_path(path)
    if _match_allowed_root(normalized):
        return normalized
    raise ValueError(f"path outside allowed roots: {normalized!r}")


def _validate_io_target(path: str, *, require_lock_suffix: bool = False) -> str:
    target = _assert_allowed_path(path)
    matched_root = _match_allowed_root(target)
    if not matched_root:
        raise ValueError(f"path outside allowed roots: {target!r}")

    rel = os.path.relpath(target, matched_root)
    if rel in ("", ".", ".."):
        raise ValueError(f"invalid relative path: {rel!r}")
    parts = rel.split(os.sep)
    for part in parts:
        if part in ("", ".", "..") or not _SAFE_BASENAME_RE.fullmatch(part):
            raise ValueError(f"unsafe path component: {part!r}")

    base = parts[-1]
    if require_lock_suffix and not base.endswith(".lock"):
        raise ValueError(f"lock path must end with .lock: {base!r}")
    if not require_lock_suffix and not base.endswith(".json"):
        raise ValueError(f"cache path must end with .json: {base!r}")

    safe_target = os.path.join(matched_root, *parts)
    return _normalize_path(safe_target)


def cache_dir(env_var: str, default: str = DEFAULT_CACHE_DIR) -> str:
    """
    Return a normalized cache directory path.

    The directory root comes from the given environment variable or the provided
    default. The result is stripped of any trailing slash and converted to an
    absolute path to avoid ambiguity.
    """
    base = (os.getenv(env_var) or default).rstrip("/")
    return _normalize_path(base)


def cache_paths(*, cache_dir_path: str, cache_stem: str, region: str) -> tuple[str, str]:
    """
    Build cache and lock file paths under the given cache_dir_path.

    The region is converted into a filesystem-safe token, and the resulting
    paths are ensured to reside within the cache_dir_path root.
    """
    # Tokenize cache stem/region into filesystem-safe components.
    safe_stem = re.sub(r"[^a-z0-9_.-]+", "-", (cache_stem or "").strip().lower()) or "cache"
    safe_region = re.sub(r"[^a-z0-9-]+", "-", region.strip().lower()) or "unknown"
    cache_root = _normalize_path(cache_dir_path)
    path = os.path.join(cache_root, f"{safe_stem}.{safe_region}.json")
    return path, path + ".lock"


def validate_payload(payload):
    """Reject malformed/empty catalog snapshots before replacing a good file."""
    import math
    if not isinstance(payload, dict):
        raise ValueError('Cache payload must be an object')
    if 'generated_at' in payload:
        timestamp = payload['generated_at']
        if type(timestamp) not in (int, float) or not math.isfinite(timestamp) or timestamp <= 0:
            raise ValueError('Invalid cache timestamp')
    if 'groups' in payload:
        groups = payload['groups']
        if not isinstance(groups, list) or not groups or not all(
            isinstance(g, (list, tuple)) and len(g) == 2 and isinstance(g[0], str)
            and isinstance(g[1], list) and g[1] and all(isinstance(v, str) and v for v in g[1])
            for g in groups
        ):
            raise ValueError('AWS returned an empty or malformed catalog')
    if 'ami_id' in payload and not re.fullmatch(r'ami-[a-f0-9]+', str(payload['ami_id'])):
        raise ValueError('Invalid AMI ID')
    if 'data' in payload and 'version' in payload and 'instance_type' in payload:
        if payload.get('schema') != 1:
            raise ValueError('Unsupported OpenSearch cache schema')
        data = payload['data']
        if payload['instance_type']:
            if not isinstance(data, dict) or not data or not all(isinstance(v, dict) for v in data.values()):
                raise ValueError('Invalid OpenSearch limits')
        elif not isinstance(data, list) or not data or not all(
            isinstance(row, dict) and isinstance(row.get('InstanceType'), str)
            and isinstance(row.get('InstanceRole'), list)
            and all(isinstance(role, str) for role in row['InstanceRole']) for row in data
        ):
            raise ValueError('Invalid OpenSearch instance catalog')


def read_json(path: str) -> dict[str, Any] | None:
    try:
        target_path = _validate_io_target(path)
        _assert_allowed_path(target_path)
        # lgtm [py/path-injection]
        with open(target_path, "r", encoding="utf-8") as fh:
            payload = json.load(fh)
            validate_payload(payload)
            return payload
    except FileNotFoundError:
        return None
    except Exception as exc:
        logger.warning("AWS catalog: unable to read cache %s: %s", path, exc)
        return None


def read_json_with_meta(path: str) -> dict[str, Any]:
    """
    Read a JSON cache file with basic filesystem metadata under validated path rules.

    Returns:
      {
        "exists": bool,
        "size_bytes": int | None,
        "mtime_epoch": float | None,
        "payload": dict | None,
        "error": str | None,
      }
    """
    out: dict[str, Any] = {
        "exists": False,
        "size_bytes": None,
        "mtime_epoch": None,
        "payload": None,
        "error": None,
    }
    try:
        target_path = _validate_io_target(path)
        _assert_allowed_path(target_path)
    except Exception:
        out["error"] = "invalid path"
        return out

    try:
        # lgtm [py/path-injection]
        st = os.stat(target_path)
        out["exists"] = True
        out["size_bytes"] = int(st.st_size)
        out["mtime_epoch"] = float(st.st_mtime)
    except FileNotFoundError:
        return out
    except Exception as exc:
        out["error"] = str(exc)
        return out

    try:
        # lgtm [py/path-injection]
        with open(target_path, "r", encoding="utf-8") as fh:
            payload = json.load(fh)
        if isinstance(payload, dict):
            out["payload"] = payload
        else:
            out["error"] = "invalid payload"
    except Exception as exc:
        out["error"] = str(exc)
    return out


def write_json_atomic(path: str, data: dict[str, Any]) -> None:
    """
    Atomically write JSON data to a normalized file path.

    Callers are responsible for choosing an appropriate cache directory; this
    helper guarantees normalized filesystem operations and atomic replace.
    """
    validate_payload(data)
    target_path = _validate_io_target(path)
    _assert_allowed_path(target_path)
    target_dir = os.path.dirname(target_path)
    base_name = os.path.basename(target_path)
    if not base_name:
        raise ValueError(f"invalid cache file path: {path!r}")
    # lgtm [py/path-injection]
    os.makedirs(target_dir, exist_ok=True)
    tmp = os.path.join(
        target_dir,
        f".{base_name}.tmp.{os.getpid()}.{int(time.time() * 1000)}.{random.randint(1000, 9999)}",
    )
    if not (
        tmp.startswith(target_dir + os.sep)
        and _is_within_any_root(tmp, _allowed_roots())
    ):
        raise ValueError(f"unsafe temp file path: {tmp!r}")
    # lgtm [py/path-injection]
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(data, fh, indent=2, sort_keys=True)
    if not (tmp.startswith(target_dir + os.sep) and target_path.startswith(target_dir + os.sep)):
        raise ValueError("unsafe atomic replace paths")
    # lgtm [py/path-injection]
    os.replace(tmp, target_path)


def payload_generated_at(payload: dict[str, Any] | None) -> float | None:
    if not isinstance(payload, dict):
        return None
    val = payload.get("generated_at")
    if isinstance(val, (int, float)) and val > 0:
        return float(val)
    return None


def is_stale(generated_at: float | None, ttl: int) -> bool:
    if not generated_at:
        return True
    return (time.time() - generated_at) >= float(ttl)


def acquire_lock(lock_path: str, *, blocking: bool) -> Any | None:
    if fcntl is None:
        return None
    try:
        requested = _validate_io_target(lock_path, require_lock_suffix=True)
        _assert_allowed_path(requested)
    except Exception as exc:
        logger.warning("Cache lock unavailable path=%s: %s", lock_path, exc)
        return None
    try:
        lock_dir = os.path.dirname(requested)
        _assert_allowed_path(lock_dir)
        # lgtm [py/path-injection]
        os.makedirs(lock_dir, exist_ok=True)
        # lgtm [py/path-injection]
        fh = open(requested, "a", encoding="utf-8")
    except Exception as exc:
        logger.warning("Cache lock unavailable path=%s: %s", requested, exc)
        return None
    try:
        flags = fcntl.LOCK_EX
        if not blocking:
            flags |= fcntl.LOCK_NB
        fcntl.flock(fh.fileno(), flags)
        return fh
    except BlockingIOError:
        try:
            fh.close()
        except Exception:
            pass
        return None
    except Exception:
        try:
            fh.close()
        except Exception:
            pass
        return None


def release_lock(fh: Any | None) -> None:
    if fh is None or fcntl is None:
        return
    try:
        fcntl.flock(fh.fileno(), fcntl.LOCK_UN)
    except Exception:
        pass
    try:
        fh.close()
    except Exception:
        pass


_SIZE_RANK: dict[str, int] = {
    "nano": 0,
    "micro": 1,
    "small": 2,
    "medium": 3,
    "large": 4,
    "xlarge": 100 + 1,
}


def _size_rank(size: str) -> int:
    size = (size or "").strip().lower()
    if size in _SIZE_RANK:
        return _SIZE_RANK[size]
    m = re.fullmatch(r"(\d+)xlarge", size)
    if m:
        return 100 + int(m.group(1))
    return 1000


def family_and_size(value: str) -> tuple[str, str]:
    """
    Extract (family, size) from:
      - EC2 instance types: m7i.large
      - RDS classes: db.r7g.8xlarge
      - ElastiCache node types: cache.r7g.large
    """
    parts = [p.strip() for p in (value or "").split(".") if p.strip()]
    if len(parts) >= 3 and parts[0] in ("db", "cache"):
        return parts[1].lower(), parts[2].lower()
    if len(parts) == 2 and parts[0] in ("db", "cache"):
        # e.g. "db.serverless" (Aurora Serverless v2)
        return parts[1].lower(), ""
    if len(parts) == 2:
        return parts[0].lower(), parts[1].lower()
    if len(parts) == 1:
        return parts[0].lower(), ""
    return (value or "").strip().lower(), ""


def default_group_for_family(family: str) -> str:
    fam = (family or "").strip().lower()
    if fam.startswith("t"):
        return "Burstable"
    if fam.startswith(("m", "a")):
        return "General Purpose"
    if fam.startswith(("c", "hpc")):
        return "Compute Optimized"
    if fam.startswith(("r", "x", "u", "z")):
        return "Memory Optimized"
    if fam.startswith(("inf", "trn", "dl", "f")):
        return "GPU / Accelerated"
    if fam.startswith(("g", "p")):
        return "GPU / Accelerated"
    if fam.startswith(("i", "d", "h", "im", "is")):
        return "Storage Optimized"
    return "Other"


def should_skip_value(value: str) -> bool:
    v = (value or "").strip().lower()
    if not v:
        return True
    if v.endswith(".metal") or ".metal-" in v:
        return True
    if v.startswith("mac"):
        return True
    return False


def build_optgroups(
    values: Iterable[str],
    *,
    group_order: list[str],
    group_for_family=default_group_for_family,
) -> list[tuple[str, list[tuple[str, str]]]]:
    buckets: dict[str, list[str]] = {}
    for raw in values:
        v = str(raw).strip()
        if should_skip_value(v):
            continue
        fam, size = family_and_size(v)
        group = group_for_family(fam)
        buckets.setdefault(group, []).append(v)

    out: list[tuple[str, list[tuple[str, str]]]] = []
    for label in group_order:
        items = buckets.get(label) or []
        if not items:
            continue
        # Stable ordering and de-dupe.
        unique = sorted(set(items), key=lambda it: (family_and_size(it)[0], _size_rank(family_and_size(it)[1]), it))
        out.append((label, [(x, x) for x in unique]))
    return out


def flatten_choices(groups: list[tuple[str, list[tuple[str, str]]]]) -> list[tuple[str, str]]:
    return [opt for _label, opts in groups for opt in opts]
