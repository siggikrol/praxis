# Read-only S3 archive client for Artiac Operations.
import os
import logging
import time
import boto3
from botocore.exceptions import ClientError

REGION = os.getenv("AWS_DEFAULT_REGION", "us-east-1")
BUCKET = (
    os.getenv("PS_IAC_ARCHIVE_S3_BUCKET")
    or os.getenv("ARTIAC_S3_BUCKET")
    or os.getenv("S3_BUCKET")
)
PREFIX = (
    os.getenv("PS_IAC_ARCHIVE_S3_PREFIX")
    or os.getenv("ARTIAC_S3_PREFIX")
    or os.getenv("S3_PREFIX")
    or "terraform-module-archive"
).strip("/")

log = logging.getLogger("modules.artiac_control.archive.s3")

CACHE_TTL_SECONDS = int(os.getenv("PS_IAC_ARCHIVE_CACHE_TTL", "120"))
_cache = {"expires_at": 0.0, "refreshed_at": 0.0, "archives": []}


_MODULE_CHARS = frozenset(
    "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789_.-"
)


def _is_supported_version(value: str) -> bool:
    """Validate an archive SemVer-shaped value without regex backtracking."""
    if not value or len(value) > 256:
        return False
    core = value.split("+", 1)[0].split("-", 1)[0]
    numbers = core.split(".")
    return bool(
        len(numbers) == 3
        and all(part and part.isascii() and part.isdigit() for part in numbers)
    )


def _is_module_name(value: str) -> bool:
    return bool(value and value[0].isalnum() and value[0].isascii() and all(
        char in _MODULE_CHARS for char in value
    ))


def _version_from_filename(module: str, filename: str) -> str:
    if not filename:
        return ""
    base = filename[:-4] if filename.endswith(".zip") else filename
    if module and base.startswith(f"{module}-"):
        candidate = base[len(module) + 1 :]
        return candidate if _is_supported_version(candidate) else ""
    parts = base.rsplit("-", 1)
    if len(parts) == 2 and _is_supported_version(parts[1]):
        return parts[1]
    return ""


def _s3():
    return boto3.client("s3", region_name=REGION)


def _full_prefix() -> str:
    return f"{PREFIX}/" if PREFIX else ""


def validate_archive_key(key: str) -> str:
    """Return a normalized archive key constrained to the configured catalog."""
    clean = str(key or "").strip()
    prefix = _full_prefix()
    if not clean or "\\" in clean or clean.startswith("/"):
        raise ValueError("Invalid archive key")
    if prefix and not clean.startswith(prefix):
        raise ValueError("Archive key is outside the configured prefix")
    relative = clean[len(prefix):] if prefix else clean
    parts = relative.split("/")
    if len(parts) != 2 or any(part in ("", ".", "..") for part in parts):
        raise ValueError("Archive key must use module/filename.zip structure")
    module, filename = parts
    if not _is_module_name(module):
        raise ValueError("Invalid archive module name")
    if not filename.endswith(".zip") or not filename.startswith(f"{module}-"):
        raise ValueError("Invalid archive filename")
    if not _version_from_filename(module, filename):
        raise ValueError("Archive filename does not contain a supported version")
    return clean


def clear_archive_cache() -> None:
    _cache.update({"expires_at": 0.0, "refreshed_at": 0.0, "archives": []})


def get_archive_cache_info() -> dict[str, object]:
    return {
        "refreshed_at": _cache.get("refreshed_at") or None,
        "expires_at": _cache.get("expires_at") or None,
        "ttl_seconds": CACHE_TTL_SECONDS,
        "cached_count": len(_cache.get("archives") or []),
    }


def list_modules():
    """List module names from S3 prefixes efficiently."""
    if not BUCKET:
        return []

    client = _s3()
    prefix = _full_prefix()
    base_prefix = prefix.rstrip("/")

    try:
        paginator = client.get_paginator("list_objects_v2")
        pages = paginator.paginate(Bucket=BUCKET, Prefix=prefix, Delimiter="/")

        modules = set()
        for page in pages:
            # Folders are in CommonPrefixes
            for cp in page.get("CommonPrefixes", []):
                full_cp = (cp.get("Prefix") or "").rstrip("/")
                rel_cp = full_cp
                if base_prefix and full_cp.startswith(base_prefix):
                    rel_cp = full_cp[len(base_prefix):].lstrip("/")
                if rel_cp:
                    modules.add(rel_cp)
            # Files directly under prefix
            if page.get("Contents"):
                for obj in page["Contents"]:
                    if obj["Key"] == prefix:
                        continue
                    rel_key = obj["Key"][len(prefix):]
                    if "/" not in rel_key and rel_key.endswith(".zip"):
                        modules.add("unknown")
                        break
        return sorted(list(modules))
    except ClientError as e:
        log.error("S3 list modules error: %s", e)
        return []


def list_archives(module=None) -> list[dict[str, object]]:
    if not BUCKET:
        log.warning("PS_IAC_ARCHIVE_S3_BUCKET is not configured")
        return []

    now = time.time()
    # Use cache if we want all modules and it's fresh
    if not module and _cache["archives"] and now < _cache["expires_at"]:
        log.info("Using cached IAC archive list (%ds TTL)", CACHE_TTL_SECONDS)
        return list(_cache["archives"])

    # If module is specified, check if we have it in cache already
    if module and _cache["archives"] and now < _cache["expires_at"]:
        log.info("Filtering from cached IAC archive list for module=%s", module)
        return [a for a in _cache["archives"] if a["module"] == module]

    client = _s3()
    base_prefix = _full_prefix()

    # Prefix Pushdown
    search_prefix = base_prefix
    if module and module != "unknown":
        search_prefix = f"{base_prefix}{module}/"

    log.info("Listing IAC archives bucket=%s prefix=%s", BUCKET, search_prefix or "(none)")

    try:
        paginator = client.get_paginator("list_objects_v2")
        pages = paginator.paginate(Bucket=BUCKET, Prefix=search_prefix)
    except ClientError as exc:
        log.error("S3 list error: %s", exc)
        return []

    archives = []
    total_objects = 0
    for page in pages:
        contents = page.get("Contents", [])
        total_objects += len(contents)
        for obj in contents:
            key = obj["Key"]
            if not key.endswith(".zip"):
                continue

            rel_key = key[len(base_prefix):] if key.startswith(base_prefix) else key
            if "/" in rel_key:
                actual_module, filename = rel_key.split("/", 1)
            else:
                actual_module, filename = "unknown", rel_key

            if module and actual_module != module:
                continue

            archives.append({
                "module": actual_module,
                "filename": filename,
                "version": _version_from_filename(actual_module, filename),
                "key": key,
                "modified": obj.get("LastModified"),
                "size": obj.get("Size"),
            })

    log.info("S3 list complete: %d objects, %d zip archives", total_objects, len(archives))

    archives = sorted(archives, key=lambda a: a.get("modified") or 0, reverse=True)

    # Only update global cache if we fetched EVERYTHING
    if not module:
        _cache["archives"] = list(archives)
        _cache["refreshed_at"] = time.time()
        _cache["expires_at"] = time.time() + CACHE_TTL_SECONDS

    return archives


def fetch_archive_bytes(key: str) -> bytes:
    if not BUCKET:
        raise RuntimeError("PS_IAC_ARCHIVE_S3_BUCKET is not configured")

    key = validate_archive_key(key)
    client = _s3()
    try:
        obj = client.get_object(Bucket=BUCKET, Key=key)
        return obj["Body"].read()
    except ClientError as exc:
        log.error("S3 fetch error: %s", exc)
        raise


def fetch_archive_metadata(key: str) -> dict[str, object]:
    if not BUCKET:
        raise RuntimeError("PS_IAC_ARCHIVE_S3_BUCKET is not configured")

    key = validate_archive_key(key)
    client = _s3()
    try:
        obj = client.head_object(Bucket=BUCKET, Key=key)
        return {
            "size": obj.get("ContentLength"),
            "modified": obj.get("LastModified"),
            "etag": str(obj.get("ETag") or "").strip('"'),
            "content_type": obj.get("ContentType"),
        }
    except ClientError as exc:
        log.error("S3 head error: %s", exc)
        raise


def get_bucket_prefix() -> dict[str, str]:
    return {
        "bucket": BUCKET or "",
        "prefix": PREFIX or "",
    }
