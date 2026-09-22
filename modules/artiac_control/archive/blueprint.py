# Archive views owned by the unified Artiac Operations module.
import logging
import os
import hashlib
from datetime import datetime, timezone
from flask import (
    Blueprint,
    render_template,
    request,
    abort,
    redirect,
    url_for,
    Response,
    session,
)
from werkzeug.exceptions import BadRequest

from .forms.filter_form import IacArtifactFilterForm
from botocore.exceptions import ClientError
from .service.s3_client import (
    list_archives,
    list_modules,
    fetch_archive_bytes,
    fetch_archive_metadata,
    get_bucket_prefix,
    validate_archive_key,
    clear_archive_cache,
    get_archive_cache_info,
)
from .service.artiac_status import fetch_artiac_status
from .service.zip_viewer import (
    list_zip_files,
    read_zip_file,
    read_zip_file_bytes,
    validate_zip_archive,
)
from .service.archive_inspector import inspect_terraform_archive
from modules.releases_explorer.service.diff import generate_combined_diff

bp = Blueprint(
    "iac_artifacts_explorer",
    __name__,
    url_prefix="/iac-artifacts",
    template_folder="templates",
)

log = logging.getLogger("modules.artiac_control.archive")


def _validated_archive_key(value: str | None) -> str:
    try:
        return validate_archive_key(value or "")
    except ValueError as exc:
        raise BadRequest(description=str(exc)) from exc


def _versions_match(left: object, right: object) -> bool:
    def normalized(value: object) -> str:
        text = str(value or "").strip()
        text = text[1:] if text[:1].lower() == "v" else text
        # Artiac archive names may append dependency/build identity while the
        # status file reports the logical module version.
        return text.split("+", 1)[0]

    return bool(normalized(left)) and normalized(left) == normalized(right)


def _semantic_version_key(value: object) -> tuple:
    """Sort common SemVer values numerically, independent of upload time."""
    text = str(value or "").strip()
    text = text[1:] if text[:1].lower() == "v" else text
    logical = text.split("+", 1)[0]
    if not logical or len(logical) > 256:
        return (-1, -1, -1, 0, ())
    core, separator, prerelease = logical.partition("-")
    numbers = core.split(".")
    if len(numbers) != 3 or any(not part.isascii() or not part.isdigit() for part in numbers):
        return (-1, -1, -1, 0, ())
    if separator and (
        not prerelease
        or any(char not in "0123456789ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz.-" for char in prerelease)
        or any(not part for part in prerelease.split("."))
    ):
        return (-1, -1, -1, 0, ())
    major, minor, patch = numbers
    # A stable release sorts after prereleases with the same numeric version.
    prerelease_key = tuple(
        (0, int(part)) if part.isdigit() else (1, part.lower())
        for part in prerelease.split(".")
        if part
    )
    try:
        numeric_version = (int(major), int(minor), int(patch))
    except ValueError:
        return (-1, -1, -1, 0, ())
    return (*numeric_version, 0 if separator else 1, prerelease_key)


def _archive_identity(key: str) -> dict[str, str]:
    prefix = str(get_bucket_prefix().get("prefix") or "").strip("/")
    relative = key[len(prefix) + 1:] if prefix and key.startswith(f"{prefix}/") else key
    module, filename = relative.split("/", 1)
    version = filename[len(module) + 1:-4] if filename.startswith(f"{module}-") else ""
    return {"module": module, "version": version, "filename": filename}


def _archive_module_navigation(
    archives: list[dict[str, object]],
    current_key: str,
) -> dict[str, dict[str, object] | None]:
    """Find adjacent modules and select their highest available version."""
    current_module = _archive_identity(current_key)["module"]
    representative_by_module = _highest_archive_by_module(archives)
    modules = sorted(representative_by_module, key=str.casefold)
    if current_module not in representative_by_module:
        return {"previous": None, "next": None}
    index = modules.index(current_module)
    previous_module = modules[index - 1] if index > 0 else None
    next_module = modules[index + 1] if index + 1 < len(modules) else None
    return {
        "previous": representative_by_module.get(previous_module) if previous_module else None,
        "next": representative_by_module.get(next_module) if next_module else None,
    }


def _highest_archive_by_module(
    archives: list[dict[str, object]],
) -> dict[str, dict[str, object]]:
    """Choose the highest semantic archive available for every module."""
    representative_by_module: dict[str, dict[str, object]] = {}
    for archive in archives:
        module = str(archive.get("module") or "")
        if not module:
            continue
        existing = representative_by_module.get(module)
        candidate_key = (
            _semantic_version_key(archive.get("version")),
            str(archive.get("filename") or ""),
        )
        existing_key = (
            _semantic_version_key(existing.get("version")),
            str(existing.get("filename") or ""),
        ) if existing else None
        if existing_key is None or candidate_key > existing_key:
            representative_by_module[module] = archive
    return representative_by_module


def _matching_spacelift_url(
    modules: list[dict[str, str]],
    module_name: str,
) -> str:
    """Return the console URL whose registry source names this archive module."""
    for module in modules:
        source_parts = str(module.get("source") or "").removeprefix("https://").split("/")
        if (
            len(source_parts) == 4
            and source_parts[0] == "spacelift.io"
            and source_parts[2] == module_name
            and module.get("source_url")
        ):
            return str(module["source_url"])
    return ""


def _annotate_archive_status(
    archives: list[dict[str, object]],
    status_by_module: dict[str, dict],
) -> None:
    for archive in archives:
        status = status_by_module.get(str(archive.get("module") or ""))
        is_current = bool(status) and _versions_match(archive.get("version"), status.get("version"))
        archive["is_current"] = is_current
        archive["archive_state"] = "current" if is_current else "historical"
        archive["current_status"] = status if is_current else None


def _parse_date_filter(value: str | None, *, end_of_day: bool = False) -> datetime | None:
    text = str(value or "").strip()
    if not text:
        return None
    try:
        parsed = datetime.strptime(text, "%Y-%m-%d").replace(tzinfo=timezone.utc)
        if end_of_day:
            parsed = parsed.replace(hour=23, minute=59, second=59, microsecond=999999)
        return parsed
    except ValueError:
        raise BadRequest(description="Archive dates must use YYYY-MM-DD.") from None


@bp.get("/")
def home():
    return redirect(url_for("iac_artifacts_explorer.list_view"))


@bp.get("/list")
def list_view():
    form = IacArtifactFilterForm(request.args)
    bucket_cfg = get_bucket_prefix()
    page = max(int(request.args.get("page", "1")), 1)
    per_page = int(os.getenv("PS_IAC_ARCHIVE_PAGE_SIZE", "50"))
    selected_a = request.args.get("a")
    selected_b = request.args.get("b")
    if selected_a:
        selected_a = _validated_archive_key(selected_a)
    if selected_b:
        selected_b = _validated_archive_key(selected_b)
    module_name = (request.args.get("module") or "").strip() or None
    status_payload = fetch_artiac_status()
    status_by_module = status_payload.get("status_by_module") or {}
    status_error = None if status_payload.get("ok") else status_payload.get("error")
    filter_values = {
        "q": (request.args.get("q") or "").strip(),
        "state": (request.args.get("state") or "").strip().lower(),
        "status": (request.args.get("status") or "").strip().lower(),
        "published_from": (request.args.get("published_from") or "").strip(),
        "published_to": (request.args.get("published_to") or "").strip(),
    }
    published_from = _parse_date_filter(filter_values["published_from"])
    published_to = _parse_date_filter(filter_values["published_to"], end_of_day=True)
    if published_from and published_to and published_from > published_to:
        abort(400, description="Published-from must be before published-to.")
    force_refresh = request.args.get("refresh", "").strip().lower() in {"1", "true", "yes", "on"}
    if force_refresh:
        clear_archive_cache()
    def classify_module(name: str) -> tuple[str, str]:
        if name.startswith("praxis_"):
            if "-" in name:
                return "praxis_custom", "PRAXIS Custom Wrappers"
            return "praxis_generic", "PRAXIS Generic Wrappers"
        return "custom_builds", "Custom Build Bundle Wrappers"

    truthy = {"1", "true", "yes", "on"}
    cat_flags = {
        "praxis_generic": (request.args.getlist("cat_praxis_generic") or ["1"])[-1].lower() in truthy,
        "praxis_custom": (request.args.getlist("cat_praxis_custom") or ["1"])[-1].lower() in truthy,
        "custom_builds": (request.args.getlist("cat_custom_builds") or ["1"])[-1].lower() in truthy,
    }
    preserved_params = {
        "cat_praxis_generic": "1" if cat_flags["praxis_generic"] else "0",
        "cat_praxis_custom": "1" if cat_flags["praxis_custom"] else "0",
        "cat_custom_builds": "1" if cat_flags["custom_builds"] else "0",
        **filter_values,
    }

    section_order = [
        ("praxis_generic", "PRAXIS Generic Wrappers"),
        ("praxis_custom", "PRAXIS Custom Wrappers"),
        ("custom_builds", "Custom Build Bundle Wrappers"),
    ]

    try:
        if module_name and not force_refresh:
            # Prefix Pushdown: only fetch archives for the selected module
            all_archives = list_archives(module=module_name)
        else:
            all_archives = list_archives()
    except ClientError as exc:
        log.error("Archive list failed: %s", exc)
        return render_template(
            "iac_artifacts_explorer/list.html",
            archives=[],
            modules=[],
            module_sections=[],
            cat_flags=cat_flags,
            form=form,
            error="Unable to list archives from S3. Check bucket permissions.",
            bucket_cfg=bucket_cfg,
            status_by_module=status_by_module,
            status_error=status_error,
            page=page,
            total_pages=1,
            selected_a=selected_a,
            selected_b=selected_b,
            catalog_summary={"archives": 0, "modules": 0, "current": 0, "current_failures": 0, "missing_current": 0},
            cache_info=get_archive_cache_info(),
            catalog_warnings={
                "missing_current": [],
                "orphan_modules": [],
                "duplicate_versions": [],
                "highest_version_mismatches": [],
            },
            filter_values=filter_values,
            preserved_params=preserved_params,
        )

    filtered_archives = [
        a
        for a in all_archives
        if cat_flags.get(classify_module(a.get("module") or "")[0])
    ]
    _annotate_archive_status(all_archives, status_by_module)
    archive_versions: dict[tuple[str, str], int] = {}
    highest_by_module: dict[str, dict[str, object]] = {}
    for item in all_archives:
        module = str(item.get("module") or "")
        version_key = (module, str(item.get("version") or ""))
        archive_versions[version_key] = archive_versions.get(version_key, 0) + 1
        current_highest = highest_by_module.get(module)
        candidate_key = (
            _semantic_version_key(item.get("version")),
            item.get("modified") or 0,
        )
        current_key = (
            _semantic_version_key(current_highest.get("version")),
            current_highest.get("modified") or 0,
        ) if current_highest else None
        if current_key is None or candidate_key > current_key:
            highest_by_module[module] = item
    current_archive_modules = {
        str(item.get("module") or "") for item in all_archives if item.get("is_current")
    }
    status_modules = set(status_by_module)
    catalog_summary = {
        "archives": len(all_archives),
        "modules": len({str(item.get("module") or "") for item in all_archives}),
        "current": len(current_archive_modules),
        "current_failures": sum(
            1
            for module in current_archive_modules
            if str((status_by_module.get(module) or {}).get("status") or "").lower() != "ok"
        ),
        "missing_current": len(status_modules - current_archive_modules),
    }
    cache_info = get_archive_cache_info()
    catalog_warnings = {
        "missing_current": [
            {"module": module, "version": (status_by_module.get(module) or {}).get("version")}
            for module in sorted(status_modules - current_archive_modules)
        ],
        "orphan_modules": sorted(
            {str(item.get("module") or "") for item in all_archives} - status_modules
        ),
        "duplicate_versions": [
            {"module": module, "version": version, "count": count}
            for (module, version), count in sorted(archive_versions.items())
            if count > 1
        ],
        "highest_version_mismatches": [
            {
                "module": module,
                "highest_version": highest.get("version"),
                "current_version": (status_by_module.get(module) or {}).get("version"),
            }
            for module, highest in sorted(highest_by_module.items())
            if module in status_by_module
            and not _versions_match(highest.get("version"), status_by_module[module].get("version"))
        ],
    }
    query = filter_values["q"].lower()
    if query:
        filtered_archives = [
            item for item in filtered_archives
            if query in str(item.get("module") or "").lower()
            or query in str(item.get("filename") or "").lower()
        ]
    if filter_values["state"] in {"current", "historical"}:
        filtered_archives = [
            item for item in filtered_archives
            if item.get("archive_state") == filter_values["state"]
        ]
    if filter_values["status"]:
        filtered_archives = [
            item for item in filtered_archives
            if (
                str((item.get("current_status") or {}).get("status") or "").lower()
                == filter_values["status"]
            )
        ]
    if published_from:
        filtered_archives = [
            item for item in filtered_archives
            if item.get("modified") and item["modified"] >= published_from
        ]
    if published_to:
        filtered_archives = [
            item for item in filtered_archives
            if item.get("modified") and item["modified"] <= published_to
        ]
    archives = list(filtered_archives)
    if module_name:
        archives = [a for a in archives if a["module"] == module_name]
        # We still need the full list of modules for the navigation
        all_module_names = list_modules()
    else:
        # Default view: show only the latest archive per module.
        latest_by_module = {}
        for item in filtered_archives:
            mod = item.get("module") or "unknown"
            current = latest_by_module.get(mod)
            if not current or (item.get("modified") or 0) > (current.get("modified") or 0):
                latest_by_module[mod] = item
        archives = sorted(latest_by_module.values(), key=lambda a: a.get("module") or "")
        all_module_names = sorted(latest_by_module.keys())

    version = (form.version.data or "").strip()
    if version:
        archives = [a for a in archives if version in a["filename"]]

    # Use the full list of modules (either from list_modules or from the 'latest' map)
    modules = [m for m in all_module_names if cat_flags.get(classify_module(m)[0])]
    grouped_modules = {key: [] for key, _ in section_order}
    for name in modules:
        key, _ = classify_module(name)
        grouped_modules[key].append(name)
    modules = []
    for key, _ in section_order:
        modules.extend(sorted(grouped_modules.get(key, [])))

    module_groups = None
    module_sections = []
    if not module_name:
        grouped = {}
        for item in filtered_archives:
            mod = item.get("module") or "unknown"
            grouped.setdefault(mod, []).append(item)
        for mod, items in grouped.items():
            grouped[mod] = sorted(
                items,
                key=lambda a: (bool(a.get("is_current")), a.get("modified") or 0),
                reverse=True,
            )

        total_pages = max((len(modules) + per_page - 1) // per_page, 1)
        start = (page - 1) * per_page
        end = start + per_page
        page_modules = modules[start:end]
        page_by_section = {key: [] for key, _ in section_order}
        for name in page_modules:
            key, _ = classify_module(name)
            page_by_section[key].append(name)
        for key, title in section_order:
            if page_by_section.get(key):
                module_sections.append({"title": title, "modules": page_by_section[key]})
        module_groups = {m: grouped[m] for m in page_modules}
        archives = [grouped[m][0] for m in page_modules if grouped.get(m)]
    else:
        total_pages = max((len(archives) + per_page - 1) // per_page, 1)
        start = (page - 1) * per_page
        end = start + per_page
        archives = archives[start:end]

    return render_template(
        "iac_artifacts_explorer/list.html",
        archives=archives,
        module_groups=module_groups,
        module_sections=module_sections,
        modules=modules,
        cat_flags=cat_flags,
        form=form,
        bucket_cfg=bucket_cfg,
        page=page,
        total_pages=total_pages,
        selected_a=selected_a,
        selected_b=selected_b,
        status_by_module=status_by_module,
        status_error=status_error,
        catalog_summary=catalog_summary,
        cache_info=cache_info,
        catalog_warnings=catalog_warnings,
        filter_values=filter_values,
        preserved_params=preserved_params,
    )


@bp.get("/view")
def view_archive():
    key = _validated_archive_key(request.args.get("key"))

    bucket_cfg = get_bucket_prefix()
    sort_key = request.args.get("sort", "name")
    archive_identity = _archive_identity(key)
    try:
        catalog_archives = list_archives()
        archive_navigation = _archive_module_navigation(catalog_archives, key)
    except ClientError as exc:
        log.warning("Archive navigation unavailable: %s", exc)
        catalog_archives = []
        archive_navigation = {"previous": None, "next": None}
    module_archives = sorted(
        [
            archive
            for archive in catalog_archives
            if str(archive.get("module") or "") == archive_identity["module"]
        ],
        key=lambda archive: (
            _semantic_version_key(archive.get("version")),
            str(archive.get("filename") or ""),
        ),
        reverse=True,
    )
    module_choices = sorted(
        _highest_archive_by_module(catalog_archives).values(),
        key=lambda archive: str(archive.get("module") or "").casefold(),
    )
    current_version_key = _semantic_version_key(archive_identity["version"])
    comparison_versions = [
        archive
        for archive in module_archives
        if archive.get("key") != key
        and _semantic_version_key(archive.get("version")) < current_version_key
    ]
    try:
        meta = fetch_archive_metadata(key)
        data = fetch_archive_bytes(key)
        inspection = inspect_terraform_archive(data)
        inspection["current_spacelift_url"] = _matching_spacelift_url(
            inspection.get("modules", []),
            archive_identity["module"],
        )
        meta["sha256"] = hashlib.sha256(data).hexdigest()
        cache_key = f"{key}:{meta.get('modified')}:{meta.get('size')}"
        files = list_zip_files(data, cache_key=cache_key)
    except ClientError as exc:
        log.error("Archive fetch failed: %s", exc)
        return render_template(
            "iac_artifacts_explorer/view.html",
            key=key,
            files=[],
            selected_file=None,
            file_preview=None,
            preview_error="Unable to access this archive. Check S3 permissions.",
            metadata={},
            bucket_cfg=bucket_cfg,
            total_files=0,
            total_size=0,
            inspection={},
            archive_navigation=archive_navigation,
            archive_identity=archive_identity,
            module_choices=module_choices,
            module_archives=module_archives,
            comparison_versions=comparison_versions,
        )
    except ValueError as exc:
        abort(413, description=str(exc))

    if sort_key == "size":
        files = sorted(files, key=lambda f: f.get("size") or 0, reverse=True)
    else:
        files = sorted(files, key=lambda f: f.get("name") or "")

    total_size = sum(f.get("size") or 0 for f in files)

    selected_file = request.args.get("file")
    file_preview = None
    preview_error = None
    preview_lang = "language-plaintext"
    if selected_file:
        try:
            file_preview = read_zip_file(data, selected_file)
            ext = selected_file.rsplit(".", 1)[-1].lower() if "." in selected_file else ""
            preview_lang = {
                "tf": "language-hcl",
                "hcl": "language-hcl",
                "yaml": "language-yaml",
                "yml": "language-yaml",
                "json": "language-json",
                "md": "language-markdown",
                "txt": "language-plaintext",
            }.get(ext, "language-plaintext")
        except Exception as exc:
            preview_error = str(exc)

    return render_template(
        "iac_artifacts_explorer/view.html",
        key=key,
        files=files,
        selected_file=selected_file,
        file_preview=file_preview,
        preview_error=preview_error,
        metadata=meta,
        bucket_cfg=bucket_cfg,
        total_files=len(files),
        total_size=total_size,
        sort_key=sort_key,
        preview_lang=preview_lang,
        inspection=inspection,
        archive_navigation=archive_navigation,
        archive_identity=archive_identity,
        module_choices=module_choices,
        module_archives=module_archives,
        comparison_versions=comparison_versions,
    )


@bp.get("/download")
def download_archive():
    key = _validated_archive_key(request.args.get("key"))

    try:
        data = fetch_archive_bytes(key)
        validate_zip_archive(data)
    except ClientError as exc:
        log.error("Archive download failed: %s", exc)
        abort(403)
    except ValueError as exc:
        abort(413, description=str(exc))
    filename = key.split("/")[-1]

    user = session.get("user_id") or "unknown"
    log.info("Archive download user=%s key=%s", user, key)
    return Response(
        data,
        mimetype="application/zip",
        headers={"Content-Disposition": f"attachment; filename={filename}"},
    )


@bp.get("/download-file")
def download_file():
    key = _validated_archive_key(request.args.get("key"))
    filename = request.args.get("file")
    if not filename:
        abort(400)

    try:
        data = fetch_archive_bytes(key)
        file_bytes = read_zip_file_bytes(data, filename)
    except ClientError as exc:
        log.error("Archive download failed: %s", exc)
        abort(403)
    except ValueError as exc:
        abort(413, description=str(exc))
    except Exception as exc:
        log.error("Archive file download failed: %s", exc)
        abort(404)

    safe_name = filename.split("/")[-1]
    user = session.get("user_id") or "unknown"
    log.info("Archive file download user=%s key=%s file=%s", user, key, filename)
    return Response(
        file_bytes,
        mimetype="application/octet-stream",
        headers={"Content-Disposition": f"attachment; filename={safe_name}"},
    )


@bp.get("/raw")
def raw_file():
    key = _validated_archive_key(request.args.get("key"))
    filename = request.args.get("file")
    if not filename:
        abort(400)

    try:
        data = fetch_archive_bytes(key)
        file_bytes = read_zip_file_bytes(data, filename)
    except ClientError as exc:
        log.error("Archive raw failed: %s", exc)
        abort(403)
    except ValueError as exc:
        abort(413, description=str(exc))
    except Exception as exc:
        log.error("Archive raw failed: %s", exc)
        abort(404)

    return Response(
        file_bytes,
        mimetype="text/plain",
    )


@bp.get("/compare")
def compare_archives():
    key_a = _validated_archive_key(request.args.get("a"))
    key_b = _validated_archive_key(request.args.get("b"))

    try:
        bytes_a = fetch_archive_bytes(key_a)
        bytes_b = fetch_archive_bytes(key_b)
        metadata_a = fetch_archive_metadata(key_a)
        metadata_b = fetch_archive_metadata(key_b)
        validate_zip_archive(bytes_a)
        validate_zip_archive(bytes_b)
    except ClientError as exc:
        log.error("Archive compare fetch failed: %s", exc)
        abort(403)
    except ValueError as exc:
        abort(413, description=str(exc))

    files_a = list_zip_files(bytes_a)
    files_b = list_zip_files(bytes_b)

    names_a = {f["name"] for f in files_a}
    names_b = {f["name"] for f in files_b}
    common_files = sorted(names_a & names_b)
    added_files = sorted(names_b - names_a)
    removed_files = sorted(names_a - names_b)
    modified_files = []
    unchanged_files = []
    file_info_a = {item["name"]: item for item in files_a}
    file_info_b = {item["name"]: item for item in files_b}
    for filename in common_files:
        left = file_info_a[filename]
        right = file_info_b[filename]
        if left.get("size") == right.get("size") and left.get("crc") == right.get("crc"):
            unchanged_files.append(filename)
        else:
            modified_files.append(filename)
    all_files = sorted(names_a | names_b)
    file_states = {
        **{name: "added" for name in added_files},
        **{name: "removed" for name in removed_files},
        **{name: "modified" for name in modified_files},
        **{name: "unchanged" for name in unchanged_files},
    }
    comparison_summary = {
        "added": len(added_files),
        "removed": len(removed_files),
        "modified": len(modified_files),
        "unchanged": len(unchanged_files),
        "total": len(all_files),
    }

    selected_file = request.args.get("file")
    changed_files = modified_files + added_files + removed_files
    if not selected_file or selected_file not in all_files:
        selected_file = (
            "main.tf"
            if "main.tf" in changed_files
            else (changed_files[0] if changed_files else (all_files[0] if all_files else None))
        )

    diff_rows = []
    diff_summary = "The archives contain no files."
    if selected_file:
        a_text = read_zip_file(bytes_a, selected_file) if selected_file in names_a else ""
        b_text = read_zip_file(bytes_b, selected_file) if selected_file in names_b else ""
        diff_rows, diff_summary = generate_combined_diff(a_text, b_text)

    return render_template(
        "iac_artifacts_explorer/compare.html",
        key_a=key_a,
        key_b=key_b,
        all_files=all_files,
        file_states=file_states,
        selected_file=selected_file,
        diff_rows=diff_rows,
        diff_summary=diff_summary,
        comparison_summary=comparison_summary,
        identity_a=_archive_identity(key_a),
        identity_b=_archive_identity(key_b),
        metadata_a=metadata_a,
        metadata_b=metadata_b,
    )
