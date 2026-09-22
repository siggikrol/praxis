# modules/releases_explorer/blueprint.py
import logging
import os
import hashlib
import re
from datetime import datetime, timezone
from urllib.parse import quote
from flask import (
    Blueprint,
    render_template,
    request,
    abort,
    redirect,
    url_for,
    Response,
)
from werkzeug.exceptions import BadRequest
from .forms.filter_form import ReleaseFilterForm
from .service.s3_client import (
    list_releases,
    fetch_release_bytes,
    fetch_release_metadata,
    validate_release_key,
    clear_release_cache,
    get_release_cache_info,
    get_release_source,
)
from .service.parser import extract_files, validate_release_archive
from .service.diff import generate_combined_diff


bp = Blueprint(
    "releases_explorer",
    __name__,
    url_prefix="/releases",
    template_folder="templates"
)


log = logging.getLogger("modules.releases_explorer")


def _parse_date_filter(value: str | None, *, end_of_day: bool = False) -> datetime | None:
    text = str(value or "").strip()
    if not text:
        return None
    try:
        parsed = datetime.strptime(text, "%Y-%m-%d").replace(tzinfo=timezone.utc)
    except ValueError:
        abort(400, description="Release dates must use YYYY-MM-DD.")
    if end_of_day:
        parsed = parsed.replace(hour=23, minute=59, second=59, microsecond=999999)
    return parsed


def _release_datetime(value) -> datetime | None:
    if isinstance(value, datetime):
        return value if value.tzinfo else value.replace(tzinfo=timezone.utc)
    if isinstance(value, str):
        try:
            parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
            return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)
        except ValueError:
            return None
    return None


def _validated_release_key(value: str | None) -> str:
    try:
        return validate_release_key(value or "")
    except ValueError as exc:
        raise BadRequest(description=str(exc)) from exc


def _release_type_color(value: object) -> int:
    """Return a stable hue for any current or future release type."""
    digest = hashlib.sha256(str(value or "unknown").lower().encode("utf-8")).digest()
    return int.from_bytes(digest[:2], "big") % 360


def _natural_version_key(value: object) -> tuple:
    return tuple(
        (0, int(part)) if part.isdigit() else (1, part.lower())
        for part in re.split(r"(\d+)", str(value or ""))
        if part
    )


# ------------------------
# Home
# ------------------------
@bp.get("/")
def home():
    return redirect(url_for("releases_explorer.list_view"))


# ------------------------
# Release List
# ------------------------
@bp.get("/list")
def list_view():
    filters = {
        "customer": (request.args.get("customer") or "").strip(),
        "module": (request.args.get("module") or "").strip(),
        "release_type": (request.args.get("release_type") or "").strip().lower(),
        "version": (request.args.get("version") or "").strip(),
        "query": (request.args.get("query") or "").strip(),
        "state": (request.args.get("state") or "").strip().lower(),
        "published_from": (request.args.get("published_from") or "").strip(),
        "published_to": (request.args.get("published_to") or "").strip(),
    }
    # Preserve the old ?latest=1 link contract while presenting a clearer state filter.
    if not filters["state"] and (request.args.get("latest") or "").lower() in {
        "1", "true", "yes", "on"
    }:
        filters["state"] = "latest"
    published_from = _parse_date_filter(filters["published_from"])
    published_to = _parse_date_filter(filters["published_to"], end_of_day=True)
    if published_from and published_to and published_from > published_to:
        abort(400, description="Published-from must be before published-to.")

    force_refresh = (request.args.get("refresh") or "").strip().lower() in {
        "1", "true", "yes", "on"
    }
    if force_refresh:
        clear_release_cache()
    releases_all = list_releases(force_refresh=force_refresh)
    customers = sorted({str(r.get("customer") or "") for r in releases_all if r.get("customer")})
    modules = sorted({str(r.get("module") or "") for r in releases_all if r.get("module")})
    release_types = sorted(
        {str(r.get("release_type") or "") for r in releases_all if r.get("release_type")}
    )
    latest_keys = {}
    for release in releases_all:
        group_key = (
            release.get("customer"),
            release.get("module"),
            release.get("release_type"),
        )
        latest_keys.setdefault(group_key, release.get("key"))

    # Filtering
    releases = list(releases_all)
    if filters["customer"]:
        releases = [r for r in releases if r.get("customer") == filters["customer"]]
    if filters["module"]:
        releases = [r for r in releases if r.get("module") == filters["module"]]
    if filters["release_type"]:
        releases = [
            r for r in releases
            if r.get("release_type") == filters["release_type"]
        ]
    if filters["version"]:
        releases = [
            r for r in releases
            if filters["version"].lower() in (r.get("filename") or "").lower()
        ]
    if filters["query"]:
        query_lower = filters["query"].lower()
        releases = [
            r for r in releases
            if query_lower in (r.get("filename") or "").lower()
            or query_lower in (r.get("customer") or "").lower()
            or query_lower in (r.get("key") or "").lower()
        ]
    if filters["state"] == "latest":
        releases = [
            r for r in releases
            if latest_keys.get(
                (r.get("customer"), r.get("module"), r.get("release_type"))
            ) == r.get("key")
        ]
    elif filters["state"] == "historical":
        releases = [
            r for r in releases
            if latest_keys.get(
                (r.get("customer"), r.get("module"), r.get("release_type"))
            ) != r.get("key")
        ]
    if published_from:
        releases = [
            r for r in releases
            if (_release_datetime(r.get("modified")) or datetime.min.replace(tzinfo=timezone.utc))
            >= published_from
        ]
    if published_to:
        releases = [
            r for r in releases
            if (_release_datetime(r.get("modified")) or datetime.max.replace(tzinfo=timezone.utc))
            <= published_to
        ]
    for release in releases:
        release["is_latest_upload"] = (
            latest_keys.get(
                (
                    release.get("customer"),
                    release.get("module"),
                    release.get("release_type"),
                )
            )
            == release.get("key")
        )
        release["release_type_color"] = _release_type_color(
            release.get("release_type")
        )

    sort_key = (request.args.get("sort") or "published").strip().lower()
    sort_order = (request.args.get("order") or "desc").strip().lower()
    sorters = {
        "customer": lambda item: str(item.get("customer") or "").lower(),
        "type": lambda item: str(item.get("release_type") or "").lower(),
        "filename": lambda item: str(item.get("filename") or "").lower(),
        "version": lambda item: _natural_version_key(item.get("version")),
        "published": lambda item: (
            _release_datetime(item.get("modified"))
            or datetime.min.replace(tzinfo=timezone.utc)
        ).timestamp(),
        "size": lambda item: int(item.get("size") or 0),
        "state": lambda item: bool(item.get("is_latest_upload")),
    }
    if sort_key not in sorters:
        sort_key = "published"
    if sort_order not in {"asc", "desc"}:
        sort_order = "desc"
    releases = sorted(
        releases,
        key=sorters[sort_key],
        reverse=sort_order == "desc",
    )

    # Calculate previous releases for "Compare Prev" button
    prev_by_key = {}
    per_group = {}
    for r in releases_all:
        group_key = (r["customer"], r["module"], r.get("release_type"))
        per_group.setdefault(group_key, []).append(r)

    for items in per_group.values():
        # Already sorted by modified DESC, but let's be explicit
        items_sorted = sorted(items, key=lambda r: r["modified"], reverse=True)
        for idx in range(1, len(items_sorted)):
            prev_by_key[items_sorted[idx - 1]["key"]] = items_sorted[idx]["key"]

    form = ReleaseFilterForm(request.args)

    a = request.args.get("a")
    b = request.args.get("b")
    if a:
        a = _validated_release_key(a)
    if b:
        b = _validated_release_key(b)
    page = max(int(request.args.get("page", "1")), 1)
    per_page = max(int(os.getenv("PS_RELEASES_PAGE_SIZE", "50")), 1)
    total_results = len(releases)
    total_pages = max((total_results + per_page - 1) // per_page, 1)
    page = min(page, total_pages)
    start = (page - 1) * per_page
    releases = releases[start:start + per_page]
    summary = {
        "releases": len(releases_all),
        "customers": len(customers),
        "modules": len({(r.get("customer"), r.get("module")) for r in releases_all}),
        "types": len(release_types),
        "latest": len(latest_keys),
        "visible": total_results,
    }

    return render_template(
        "releases_explorer/list.html",
        releases=releases,
        customers=customers,
        modules=modules,
        release_types=release_types,
        form=form,
        selected_a=a,
        selected_b=b,
        prev_by_key=prev_by_key,
        filters=filters,
        summary=summary,
        page=page,
        total_pages=total_pages,
        cache_info=get_release_cache_info(),
        sort_key=sort_key,
        sort_order=sort_order,
    )

# ------------------------
# View single release
# ------------------------
@bp.get("/view")
def view_release():
    key = _validated_release_key(request.args.get("key"))
    try:
        data = fetch_release_bytes(key)
        safety = validate_release_archive(data)
        files = extract_files(data)
        metadata = fetch_release_metadata(key)
        metadata["sha256"] = hashlib.sha256(data).hexdigest()
    except ValueError as exc:
        abort(413, description=str(exc))

    source = get_release_source()
    bucket = source.get("bucket") or ""
    region = source.get("region") or "us-east-1"
    prefix = source.get("prefix") or ""
    s3_bucket_url = ""
    s3_object_url = ""
    if bucket:
        encoded_prefix = quote(f"{prefix}/" if prefix else "", safe="/")
        encoded_key = quote(key, safe="/")
        s3_bucket_url = (
            f"https://s3.console.aws.amazon.com/s3/buckets/{quote(bucket, safe='')}"
            f"?region={quote(region, safe='')}&prefix={encoded_prefix}"
        )
        s3_object_url = (
            f"https://s3.console.aws.amazon.com/s3/object/{quote(bucket, safe='')}"
            f"?region={quote(region, safe='')}&prefix={encoded_key}"
        )

    return render_template(
        "releases_explorer/view.html",
        key=key,
        files=files,
        metadata=metadata,
        safety=safety,
        catalog_repo_url=(
            os.getenv("PS_RELEASES_CATALOG_REPO_URL")
            or ""
        ).strip(),
        s3_bucket_url=s3_bucket_url,
        s3_object_url=s3_object_url,
        release_source=source,
    )


# ------------------------
# Download release archive
# ------------------------
@bp.get("/download")
def download_release():
    key = _validated_release_key(request.args.get("key"))
    try:
        data = fetch_release_bytes(key)
        validate_release_archive(data)
    except ValueError as exc:
        abort(413, description=str(exc))
    filename = key.split("/")[-1] or "release.tar.gz"
    return Response(
        data,
        mimetype="application/gzip",
        headers={"Content-Disposition": f"attachment; filename={filename}"},
    )


# ------------------------
# Compare two releases
# ------------------------
@bp.get("/compare")
def compare_releases():
    key_a = _validated_release_key(request.args.get("a"))
    key_b = _validated_release_key(request.args.get("b"))
    try:
        bytes_a = fetch_release_bytes(key_a)
        bytes_b = fetch_release_bytes(key_b)
        validate_release_archive(bytes_a)
        validate_release_archive(bytes_b)
        files_a = extract_files(bytes_a)
        files_b = extract_files(bytes_b)
        metadata_a = fetch_release_metadata(key_a)
        metadata_b = fetch_release_metadata(key_b)
    except ValueError as exc:
        abort(413, description=str(exc))

    names_a = set(files_a)
    names_b = set(files_b)
    all_files = sorted(names_a | names_b)
    added_files = sorted(names_b - names_a)
    removed_files = sorted(names_a - names_b)
    common_files = sorted(names_a & names_b)
    modified_files = sorted(
        filename for filename in common_files
        if files_a[filename] != files_b[filename]
    )
    unchanged_files = sorted(set(common_files) - set(modified_files))
    file_states = {
        **{name: "added" for name in added_files},
        **{name: "removed" for name in removed_files},
        **{name: "modified" for name in modified_files},
        **{name: "unchanged" for name in unchanged_files},
    }
    comparison_summary = {
        "total": len(all_files),
        "added": len(added_files),
        "removed": len(removed_files),
        "modified": len(modified_files),
        "unchanged": len(unchanged_files),
    }

    # default file = deployment.yaml if exists
    selected_file = request.args.get("file")
    changed_files = modified_files + added_files + removed_files
    if not selected_file or selected_file not in all_files:
        selected_file = (
            "deployment.yaml"
            if "deployment.yaml" in changed_files
            else (changed_files[0] if changed_files else (all_files[0] if all_files else None))
        )

    diff_rows = []
    summary = "The releases contain no supported YAML or text files."
    if selected_file:
        diff_rows, summary = generate_combined_diff(
            files_a.get(selected_file, ""),
            files_b.get(selected_file, ""),
        )

    return render_template(
        "releases_explorer/compare.html",
        key_a=key_a,
        key_b=key_b,
        all_files=all_files,
        file_states=file_states,
        common_files=common_files,
        selected_file=selected_file,
        diff_rows=diff_rows,
        summary=summary,
        comparison_summary=comparison_summary,
        metadata_a=metadata_a,
        metadata_b=metadata_b,
    )
