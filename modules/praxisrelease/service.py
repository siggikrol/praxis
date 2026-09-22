# modules/praxisrelease/service.py

import os
import io
import re
import tarfile
import yaml
import boto3
from botocore.exceptions import ClientError


# ---------------------------------------------------------------------------
# Environment configuration
# ---------------------------------------------------------------------------
PROVIDER = os.getenv("PS_RELEASES_PROVIDER", "s3").lower()
BUCKET = os.getenv("PS_RELEASES_S3_BUCKET")
REGION = os.getenv("AWS_DEFAULT_REGION", "us-east-1")
PREFIX = (os.getenv("PS_RELEASES_S3_PREFIX") or "bundles").strip("/")
_TAR_GZ_SUFFIX = ".tar.gz"
_NATURAL_CHUNK_RE = re.compile(r"(\d+)")
WIZARD_RELEASE_PREFIXES = {
    "rgs": ("rgs-",),
    "single-vpc": ("catalyst-",),
    "multi-vpc": ("catalyst-",),
    "loyalty": ("loyalty-", "playon-"),
}


def _make_s3_client():
    """
    Create an S3 client using the standard AWS_* environment variables.
    No manual endpoint override — lets boto3 resolve the correct regional endpoint.
    """
    session = boto3.session.Session(region_name=REGION)
    return session.client("s3")


s3 = None


def _s3():
    """Create a client only when a configured S3 operation is requested."""
    global s3
    if PROVIDER != "s3" or not BUCKET:
        raise RuntimeError("S3 releases are not configured")
    if s3 is None:
        s3 = _make_s3_client()
    return s3


def _full_prefix() -> str:
    return f"{PREFIX}/" if PREFIX else ""


def get_release_prefix() -> str:
    return _full_prefix()


def get_release_source() -> dict[str, str]:
    return {
        "bucket": BUCKET or "",
        "prefix": PREFIX or "",
    }


def _release_basename(path: str) -> str:
    return os.path.basename((path or "").rstrip("/"))


def _release_stem(path: str) -> str:
    name = _release_basename(path)
    if name.endswith(_TAR_GZ_SUFFIX):
        return name[: -len(_TAR_GZ_SUFFIX)]
    return name


def _natural_sort_key(value: str) -> tuple[tuple[int, int | str], ...]:
    chunks = []
    for chunk in _NATURAL_CHUNK_RE.split((value or "").lower()):
        if not chunk:
            continue
        if chunk.isdigit():
            chunks.append((0, int(chunk)))
        else:
            chunks.append((1, chunk))
    return tuple(chunks)


def sort_release_names(names) -> list[str]:
    return sorted(
        names,
        key=lambda name: (
            _natural_sort_key(_release_stem(name)),
            _natural_sort_key(name),
        ),
        reverse=True,
    )


def sort_release_objects(releases) -> list[dict]:
    return sorted(
        releases,
        key=lambda release: (
            _natural_sort_key(_release_stem(release.get("key", ""))),
            _natural_sort_key(release.get("key", "")),
        ),
        reverse=True,
    )


def filter_release_names_for_wizard(
    names, wizard_slug: str | None = None
) -> list[str]:
    allowed_prefixes = WIZARD_RELEASE_PREFIXES.get((wizard_slug or "").strip().lower())
    names = list(names)
    if not allowed_prefixes:
        return names

    return [
        name
        for name in names
        if _release_basename(name).lower().startswith(allowed_prefixes)
    ]


def filter_release_objects_for_wizard(
    releases, wizard_slug: str | None = None
) -> list[dict]:
    allowed_prefixes = WIZARD_RELEASE_PREFIXES.get((wizard_slug or "").strip().lower())
    releases = list(releases)
    if not allowed_prefixes:
        return releases

    return [
        release
        for release in releases
        if _release_basename(release.get("key", "")).lower().startswith(
            allowed_prefixes
        )
    ]


# ---------------------------------------------------------------------------
# Listing helpers
# ---------------------------------------------------------------------------
def list_customers(bucket: str = BUCKET) -> list[str]:
    """Return all customer folders under the configured prefix."""
    if PROVIDER != "s3" or not BUCKET:
        return []
    customers = set()
    base_prefix = _full_prefix()
    try:
        paginator = _s3().get_paginator("list_objects_v2")
        for page in paginator.paginate(Bucket=bucket, Prefix=base_prefix, Delimiter="/"):
            for cp in page.get("CommonPrefixes", []):
                name = cp["Prefix"].rstrip("/")
                if base_prefix:
                    name = name[len(base_prefix):] if name.startswith(base_prefix) else name
                if name:
                    customers.add(name)
    except ClientError as e:
        raise RuntimeError(f"Unable to list customers in {bucket}: {e}")
    return sorted(customers)


def list_customer_releases(bucket: str = BUCKET, customer_prefix: str = "") -> list[str]:
    """List all .tar.gz releases under a customer's prefix."""
    if PROVIDER != "s3" or not BUCKET:
        return []
    releases = []
    base_prefix = _full_prefix()
    customer_prefix = (customer_prefix or "").strip().strip("/")
    full_prefix = f"{base_prefix}{customer_prefix}/" if customer_prefix else base_prefix
    try:
        paginator = _s3().get_paginator("list_objects_v2")
        for page in paginator.paginate(Bucket=bucket, Prefix=full_prefix):
            for obj in page.get("Contents", []):
                key = obj["Key"]
                if key.endswith(".tar.gz"):
                    releases.append(os.path.basename(key))
    except ClientError as e:
        raise RuntimeError(f"Unable to list releases for {customer_prefix}: {e}")
    return sort_release_names(releases)


def list_all_release_keys(bucket: str = BUCKET) -> list[str]:
    """List full S3 keys for all .tar.gz releases under the base prefix."""
    if PROVIDER != "s3" or not BUCKET:
        return []
    base_prefix = _full_prefix()
    releases = []
    try:
        paginator = _s3().get_paginator("list_objects_v2")
        for page in paginator.paginate(Bucket=bucket, Prefix=base_prefix):
            for obj in page.get("Contents", []):
                key = obj["Key"]
                if key.endswith(".tar.gz"):
                    releases.append(key)
    except ClientError as e:
        raise RuntimeError(f"Unable to list releases in {bucket}: {e}")
    return sort_release_names(releases)


def list_all_release_objects(bucket: str = BUCKET) -> list[dict]:
    """List all .tar.gz release objects under the base prefix."""
    if PROVIDER != "s3" or not BUCKET:
        return []
    base_prefix = _full_prefix()
    releases = []
    try:
        paginator = _s3().get_paginator("list_objects_v2")
        for page in paginator.paginate(Bucket=bucket, Prefix=base_prefix):
            for obj in page.get("Contents", []):
                key = obj["Key"]
                if key.endswith(".tar.gz"):
                    releases.append({
                        "key": key,
                        "modified": obj.get("LastModified"),
                    })
    except ClientError as e:
        raise RuntimeError(f"Unable to list releases in {bucket}: {e}")
    return sort_release_objects(releases)


# --- Backward compatibility alias ---
def list_releases_for_customer(customer: str) -> list[str]:
    """Alias for list_customer_releases to preserve compatibility with plugin.py"""
    return list_customer_releases(BUCKET, customer)


# ---------------------------------------------------------------------------
# Manifest loader
# ---------------------------------------------------------------------------
def fetch_release_manifest(bucket, key, customer=None):
    """Fetch deployment.yaml from the given .tar.gz in S3."""
    if PROVIDER != "s3" or not BUCKET:
        raise RuntimeError("S3 releases are not configured")
    if bucket != BUCKET:
        raise RuntimeError(f"Access to bucket '{bucket}' is not allowed")

    # Normalize and constrain key to the provided customer prefix.
    key = (key or "").lstrip("/")
    if ".." in key.split("/"):
        raise RuntimeError("Invalid key")

    base_prefix = _full_prefix()
    if customer:
        customer = customer.strip().strip("/")
        prefix = f"{base_prefix}{customer}/"
        if not key.startswith(prefix):
            key = prefix + key
        if not key.startswith(prefix):
            raise RuntimeError("Requested key is outside the selected customer scope")
    elif base_prefix and not key.startswith(base_prefix):
        key = base_prefix + key

    s3_client = _s3()

    try:
        obj = s3_client.get_object(Bucket=bucket, Key=key)
    except Exception as e:
        raise RuntimeError(f"Failed to fetch release manifest from s3://{bucket}/{key}: {e}")

    body = io.BytesIO(obj["Body"].read())
    try:
        with tarfile.open(fileobj=body, mode="r:gz") as tar:
            # Look for deployment.yaml anywhere in the archive
            member = next((m for m in tar.getmembers() if m.name.endswith("deployment.yaml")), None)
            if not member:
                raise RuntimeError(f"Archive {key} has no deployment.yaml file")

            f = tar.extractfile(member)
            return yaml.safe_load(f.read()) if f else None

    except tarfile.ReadError:
        raise RuntimeError(f"File {key} is not a valid tar.gz archive")


# ---------------------------------------------------------------------------
# Manifest utility
# ---------------------------------------------------------------------------
def get_versions_for_section(section: str, manifest: dict) -> dict:
    """Return a flattened dict of version numbers for a given section."""
    section_data = manifest.get(section, {})
    result = {}
    for k, v in section_data.items():
        if isinstance(v, dict):
            for subk, subv in v.items():
                result[f"{k}.{subk}"] = subv
        else:
            result[k] = v
    return result
