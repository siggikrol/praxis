# modules/releases_explorer/service/s3_client.py
import os
import boto3
from botocore.exceptions import ClientError
import logging
import re
import time

PROVIDER = os.getenv("PS_RELEASES_PROVIDER", "s3").lower()
BUCKET = os.getenv("PS_RELEASES_S3_BUCKET")
REGION = os.getenv("AWS_DEFAULT_REGION", "us-east-1")
PREFIX = (os.getenv("PS_RELEASES_S3_PREFIX") or "bundles").strip("/")

log = logging.getLogger("modules.releases_explorer.s3")
try:
    CACHE_TTL_SECONDS = max(1, int(os.getenv("PS_RELEASES_CACHE_TTL", "120")))
except ValueError:
    CACHE_TTL_SECONDS = 120
_cache = {"expires_at": 0.0, "refreshed_at": 0.0, "releases": []}
_PATH_PART_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]*$")
_FILENAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.+@-]*$")
_RELEASE_IDENTITY_RE = re.compile(
    r"^(?P<release_type>.+?)-v?(?P<version>(?:\d+(?:\.\d+)+(?:[-+][0-9A-Za-z.-]+)?)|(?:\d{4}-\d{2}(?:-\d{2})?))$",
    re.IGNORECASE,
)


def _s3():
    """Return boto3 S3 client."""
    return boto3.client("s3", region_name=REGION)


def _full_prefix() -> str:
    return f"{PREFIX}/" if PREFIX else ""


def validate_release_key(key: str) -> str:
    """Constrain release access to a valid archive under the configured prefix."""
    clean = str(key or "").strip()
    prefix = _full_prefix()
    if not clean or clean.startswith("/") or "\\" in clean:
        raise ValueError("Invalid release key")
    if prefix and not clean.startswith(prefix):
        raise ValueError("Release key is outside the configured prefix")
    relative = clean[len(prefix):] if prefix else clean
    parts = relative.split("/")
    if not 1 <= len(parts) <= 3 or any(part in {"", ".", ".."} for part in parts):
        raise ValueError("Release key has an unexpected path structure")
    if not relative.endswith(".tar.gz"):
        raise ValueError("Release key must identify a .tar.gz archive")
    path_parts = parts[:-1]
    filename = parts[-1][:-7]
    if any(not _PATH_PART_RE.fullmatch(part) for part in path_parts):
        raise ValueError("Release key contains an invalid customer or module")
    if not filename or not _FILENAME_RE.fullmatch(filename):
        raise ValueError("Release archive filename is invalid")
    return clean


def release_identity_from_filename(filename: str) -> dict[str, str]:
    """Derive an open-ended type and version from the archive filename."""
    name = str(filename or "").strip()
    stem = name[:-7] if name.lower().endswith(".tar.gz") else name
    match = _RELEASE_IDENTITY_RE.fullmatch(stem)
    if not match:
        return {"release_type": stem.lower(), "version": ""}
    return {
        "release_type": match.group("release_type").strip().lower(),
        "version": match.group("version"),
    }


def clear_release_cache() -> None:
    _cache.update({"expires_at": 0.0, "refreshed_at": 0.0, "releases": []})


def get_release_cache_info() -> dict[str, object]:
    return {
        "refreshed_at": _cache.get("refreshed_at") or None,
        "expires_at": _cache.get("expires_at") or None,
        "ttl_seconds": CACHE_TTL_SECONDS,
        "cached_count": len(_cache.get("releases") or []),
    }


def get_release_source() -> dict[str, str]:
    return {
        "bucket": BUCKET or "",
        "prefix": PREFIX or "",
        "region": REGION or "",
    }


def list_customers():
    """List customer names from S3 prefixes efficiently."""
    if PROVIDER != "s3" or not BUCKET:
        return []

    client = _s3()
    prefix = _full_prefix()

    try:
        paginator = client.get_paginator("list_objects_v2")
        pages = paginator.paginate(Bucket=BUCKET, Prefix=prefix, Delimiter="/")

        customers = set()
        for page in pages:
            # Folders are in CommonPrefixes
            for cp in page.get("CommonPrefixes", []):
                full_cp = cp.get("Prefix")
                if not full_cp:
                    continue
                full_cp = full_cp.rstrip("/")
                if not full_cp.startswith(prefix):
                    continue
                rel_cp = full_cp[len(prefix):].strip("/")
                if rel_cp:
                    customers.add(rel_cp)
            # For consistency with list_releases, if there are files directly in prefix, they are "unknown"
            if page.get("Contents"):
                for obj in page["Contents"]:
                    if obj["Key"] == prefix:
                        continue
                    rel_key = obj["Key"][len(prefix):]
                    if "/" not in rel_key and rel_key.endswith(".tar.gz"):
                        customers.add("unknown")
                        break
        return sorted(list(customers))
    except ClientError as e:
        log.error("S3 list customers error: %s", e)
        return []


def list_releases(customer=None, *, force_refresh: bool = False):
    """List releases and detect customers from prefixes."""
    if PROVIDER != "s3":
        return []
    if not BUCKET:
        log.warning("PS_RELEASES_S3_BUCKET is not configured")
        return []

    client = _s3()
    base_prefix = _full_prefix()
    now = time.time()
    if not force_refresh and _cache["releases"] and now < _cache["expires_at"]:
        if customer:
            return [r for r in _cache["releases"] if r["customer"] == customer]
        return list(_cache["releases"])

    # Prefix Pushdown: if customer is specified, narrow the S3 search
    search_prefix = base_prefix
    if customer and customer != "unknown":
        search_prefix = f"{base_prefix}{customer}/"
        log.info("Using S3 prefix pushdown for customer %r: %s", customer, search_prefix)
    elif customer == "unknown":
        log.info('Customer filter "unknown" selected — prefix pushdown disabled; performing base-prefix S3 scan')
        log.info("Using S3 prefix: %s", search_prefix)
    else:
        log.info("No customer selected — performing full S3 scan")
        log.info("Using S3 prefix: %s", search_prefix)

    try:
        paginator = client.get_paginator("list_objects_v2")
        pages = paginator.paginate(Bucket=BUCKET, Prefix=search_prefix)
    except ClientError as e:
        log.error("S3 list error: %s", e)
        return []

    releases = []
    for page in pages:
        for obj in page.get("Contents", []):
            key = obj["Key"]
            if base_prefix and not key.startswith(base_prefix):
                continue

            # Detect customer prefix (folder)
            rel_key = key[len(base_prefix):] if base_prefix and key.startswith(base_prefix) else key
            if not rel_key or rel_key.endswith("/"):
                continue

            parts = rel_key.split("/")
            if len(parts) >= 3:
                # new structure: customer/module/filename
                actual_customer = parts[0]
                module = parts[1]
                filename = "/".join(parts[2:])
            elif len(parts) == 2:
                # legacy structure: folder/filename 
                # We treat the folder as the customer for consistency and filtering
                actual_customer = parts[0]
                module = parts[0]
                filename = parts[1]
            else:
                # Files directly in the root of the prefix
                actual_customer = "unknown"
                module = "unknown"
                filename = parts[0]

            # If we were searching for a specific customer but got something else
            # (only possible if we are not using pushdown or if prefix matched partially)
            if customer and actual_customer != customer:
                continue

            if not filename.endswith(".tar.gz"):
                continue
            try:
                validate_release_key(key)
            except ValueError:
                log.warning("Skipping release object with unexpected key: %s", key)
                continue

            identity = release_identity_from_filename(filename)
            releases.append({
                "customer": actual_customer,
                "module": module,
                "filename": filename,
                "release_type": identity["release_type"],
                "version": identity["version"],
                "key": key,
                "modified": obj["LastModified"],
                "size": obj["Size"],
            })

    releases = sorted(releases, key=lambda r: r["modified"], reverse=True)
    if not customer:
        _cache["releases"] = list(releases)
        _cache["refreshed_at"] = time.time()
        _cache["expires_at"] = time.time() + CACHE_TTL_SECONDS
    return releases


def fetch_release_bytes(key: str) -> bytes:
    """Fetch tar.gz bytes from S3."""
    key = validate_release_key(key)
    client = _s3()

    try:
        obj = client.get_object(Bucket=BUCKET, Key=key)
        return obj["Body"].read()
    except ClientError as e:
        log.error("S3 fetch error: %s", e)
        raise


def fetch_release_metadata(key: str) -> dict[str, object]:
    key = validate_release_key(key)
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
        log.error("S3 release metadata error: %s", exc)
        raise
