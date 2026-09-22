# modules/status/blueprint.py
from __future__ import annotations

import os
import json
import time
from datetime import datetime, timezone

import requests
from flask import Blueprint, current_app, render_template, session, request, redirect, url_for, Response
from services.aws_instance_types.common import read_json as _safe_read_json
from services.session_activity import list_active_sessions
from services.aws_instance_types.registry import BY_KEY, requested_catalogs, refresh_region
from services.github_helpers.github_api import get_token

from .service import (
    collect_env_flags,
    collect_app_meta,
    count_server_side_sessions,
    check_spacelift,
    check_s3_access,
    collect_modules_status,
    check_artiac_status,
    refresh_spacelift_status_cache,
)

# IMPORTANT: tell Flask where this module's templates live
bp = Blueprint(
    "status",
    __name__,
    url_prefix="/status",
    template_folder="templates",
)

_github_status_cache: dict[str, object] = {"expires_at": 0.0, "payload": None}

def _github_rate_limit():
    """
    Fetch GitHub rate-limit. Uses App Auth.
    Keep this resilient: short timeout and never raise.
    """
    token = get_token()
    try:
        ttl = int(os.getenv("PS_GITHUB_STATUS_TTL_SECONDS", "30"))
    except Exception:
        ttl = 30
    now = time.time()
    if ttl > 0 and now < float(_github_status_cache.get("expires_at", 0.0)):
        cached = _github_status_cache.get("payload")
        if isinstance(cached, dict):
            return dict(cached)

    headers = {"Accept": "application/vnd.github+json"}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    try:
        resp = requests.get("https://api.github.com/rate_limit", headers=headers, timeout=2.5)
        data = resp.json() if resp.ok else {"error": f"{resp.status_code} {resp.text[:120]}"}
    except Exception as e:
        data = {"error": str(e)}

    core = (data or {}).get("resources", {}).get("core", {})
    search = (data or {}).get("resources", {}).get("search", {})
    graphql = (data or {}).get("resources", {}).get("graphql", {})

    def _fmt_reset(epoch):
        try:
            return datetime.fromtimestamp(epoch, tz=timezone.utc).isoformat()
        except Exception:
            return None

    payload = {
        "raw": data,
        "core": {
            "limit": core.get("limit"),
            "remaining": core.get("remaining"),
            "reset": core.get("reset"),
            "reset_iso": _fmt_reset(core.get("reset")),
        },
        "search": {
            "limit": search.get("limit"),
            "remaining": search.get("remaining"),
            "reset": search.get("reset"),
            "reset_iso": _fmt_reset(search.get("reset")),
        },
        "graphql": {
            "limit": graphql.get("limit"),
            "remaining": graphql.get("remaining"),
            "reset": graphql.get("reset"),
            "reset_iso": _fmt_reset(graphql.get("reset")),
        },
        "token_present": bool(token),
    }
    if ttl > 0:
        _github_status_cache["payload"] = dict(payload)
        _github_status_cache["expires_at"] = now + ttl
    return payload


@bp.get("/")
def index():
    # Status landing page: a simple index of available status dashboards.
    return render_template("status/index.html")


@bp.get("/app")
def app_status():
    gh = _github_rate_limit()
    user = session.get("user")
    sid = getattr(session, "sid", None)
    login_ts = session.get("login_ts")
    login_ts_iso = None
    try:
        if isinstance(login_ts, (int, float)):
            login_ts_iso = datetime.fromtimestamp(float(login_ts), tz=timezone.utc).isoformat()
    except Exception:
        login_ts_iso = None
    session_meta = {
        "sid_short": (str(sid)[:8] if sid else None),
        "auth_method": session.get("auth_method") or "unknown",
        "login_ts": login_ts if isinstance(login_ts, (int, float)) else None,
        "login_ts_iso": login_ts_iso,
    }
    app_meta = collect_app_meta(current_app)
    session_stats = count_server_side_sessions(current_app.config.get("SESSION_FILE_DIR", "/tmp/flask_sessions"))
    active_sessions = list_active_sessions(session_dir=current_app.config.get("SESSION_FILE_DIR", "/tmp/flask_sessions"))
    env_flags = collect_env_flags()
    s3 = check_s3_access()
    modules = collect_modules_status(current_app)
    artiac = check_artiac_status()

    flags = {
        "AUTH_ENABLED": bool(current_app.config.get("AUTH_ENABLED")),
    }

    # Render directly from this blueprint's template_folder
    return render_template(
        "status/app.html",
        gh=gh,
        user=user,
        session_meta=session_meta,
        active_sessions=active_sessions,
        flags=flags,
        app_meta=app_meta,
        session_stats=session_stats,
        env_flags=env_flags,
        s3=s3,
        modules=modules,
        artiac=artiac,
    )


@bp.get("/spacelift-cache")
def spacelift_cache():
    spacelift = check_spacelift()
    last_spacelift_refresh = session.pop("status:spacelift_last_refresh", None)
    return render_template(
        "status/spacelift_cache.html",
        spacelift=spacelift,
        last_spacelift_refresh=last_spacelift_refresh,
    )


@bp.post("/spacelift/refresh")
def spacelift_refresh():
    result = refresh_spacelift_status_cache()
    session["status:spacelift_last_refresh"] = {
        "ts_epoch": time.time(),
        "ok": bool(result.get("ok")),
        "error": result.get("error"),
        "generated_at": result.get("generated_at"),
        "generated_at_iso": result.get("generated_at_iso"),
    }
    session.modified = True
    return_to = (request.form.get("return_to") or "").strip().lower()
    if return_to == "app":
        return redirect(url_for("status.app_status"))
    return redirect(url_for("status.spacelift_cache"))


@bp.get("/aws-catalogs")
def aws_catalogs():
    from .service import available_aws_regions, collect_aws_catalog_cache_status

    import re

    region = (request.args.get("region") or "").strip()
    if region and not re.fullmatch(r"[a-z0-9-]+", region):
        region = ""

    data = collect_aws_catalog_cache_status(focus_region=region or None)
    all_regions = available_aws_regions("ec2")
    if not all_regions:
        # Fallback: at least show configured regions.
        all_regions = sorted(set((data.get("config") or {}).get("refresh_regions") or []))
    last_refresh = session.pop("status:aws_catalog_last_refresh", None)
    return render_template(
        "status/aws_catalogs.html",
        data=data,
        last_refresh=last_refresh,
        all_regions=all_regions,
        selected_region=region or None,
    )

@bp.get("/aws-catalogs/partials/caches")
def aws_catalogs_partials_caches():
    from .service import collect_aws_catalog_cache_status

    import re

    region = (request.args.get("region") or "").strip()
    if region and not re.fullmatch(r"[a-z0-9-]+", region):
        region = ""
    data = collect_aws_catalog_cache_status(focus_region=region or None)
    return render_template(
        "status/aws_catalogs/_caches.html",
        data=data,
        selected_region=region or None,
    )


@bp.get("/aws-catalogs/partials/previews")
def aws_catalogs_partials_previews():
    from .service import collect_aws_catalog_cache_status

    import re

    region = (request.args.get("region") or "").strip()
    if region and not re.fullmatch(r"[a-z0-9-]+", region):
        region = ""
    data = collect_aws_catalog_cache_status(focus_region=region or None)
    return render_template(
        "status/aws_catalogs/_previews.html",
        data=data,
        selected_region=region or None,
    )


@bp.post("/aws-catalogs/jobs")
def aws_catalogs_job_start():
    """
    Start an async refresh job (returns quickly; work is done in a background thread).
    """
    from .aws_catalog_jobs import start_refresh_job

    catalog = (request.form.get("catalog") or "").strip().lower()
    region = (request.form.get("region") or "").strip()
    engine = (request.form.get("engine") or "").strip()



    def _split_csv(raw: str) -> list[str]:
        return [p.strip() for p in (raw or "").split(",") if p.strip()]

    # Regions: if no explicit region provided for all/blank, use configured.
    if not region and catalog in ("", "all"):
        raw_regions = (
            os.getenv("PS_AWS_CATALOG_REFRESH_REGIONS")
            or os.getenv("PS_EKS_INSTANCE_TYPES_REFRESH_REGIONS")
            or os.getenv("AWS_DEFAULT_REGION")
            or "us-east-1"
        )
        regions = _split_csv(raw_regions) or ["us-east-1"]
    else:
        region = region or (os.getenv("AWS_DEFAULT_REGION", "us-east-1").strip() or "us-east-1")
        regions = [region]

    # Light validation to avoid writing weird filenames / confusing jobs.
    import re

    regions = [r for r in regions if re.fullmatch(r"[a-z0-9-]+", r)]
    if not regions:
        return {"ok": False, "error": "No valid regions provided"}, 400
    if catalog not in (*BY_KEY, '', 'all', 'kafka'):
        return {"ok": False, "error": f"Unknown catalog: {catalog!r}"}, 400

    job = start_refresh_job(catalog=catalog or "all", regions=regions, engine=engine)
    # For callers, provide the poll URL.
    job_id = job.get("job_id")
    if not job_id:
        return job, 429
    job["poll_url"] = url_for("status.aws_catalogs_job_status", job_id=job_id)
    return job, 202


@bp.get("/aws-catalogs/jobs/<job_id>.json")
def aws_catalogs_job_status(job_id: str):
    from .aws_catalog_jobs import get_job

    import re

    if not re.fullmatch(r"[a-f0-9]{32}", (job_id or "").strip()):
        return {"ok": False, "error": "invalid job id"}, 400
    job = get_job(job_id)
    if not job:
        return {"ok": False, "error": "job not found"}, 404
    return job, 200


@bp.post("/aws-catalogs/refresh")
def aws_catalogs_refresh():
    job, status = aws_catalogs_job_start()
    session['status:aws_catalog_last_refresh'] = job
    return redirect(url_for('status.aws_catalogs'))


@bp.get("/aws-catalogs/json")
def aws_catalogs_json():
    """
    Return the raw cached JSON for a given catalog/region/engine.
    """
    catalog = (request.args.get("catalog") or "").strip().lower()
    region = (request.args.get("region") or "").strip()
    engine = (request.args.get("engine") or "").strip()

    if catalog not in BY_KEY:
        return {"ok": False, "error": "Unknown catalog"}, 400
    if not region:
        return {"ok": False, "error": "region is required"}, 400

    import re

    if not re.fullmatch(r"[a-z0-9-]+", region):
        return {"ok": False, "error": "invalid region"}, 400

    # Resolve target from status metadata rather than building an arbitrary path from request args.
    from .service import collect_aws_catalog_cache_status

    requested_engine = (engine or os.getenv("PS_AURORA_ENGINE", "aurora-postgresql") or "aurora-postgresql").strip()
    requested_engine_safe = re.sub(r"[^a-z0-9-]+", "-", requested_engine.lower()) or "unknown"

    data = collect_aws_catalog_cache_status(focus_region=region)
    entries = data.get("entries") if isinstance(data, dict) else None
    if not isinstance(entries, list):
        return {"ok": False, "error": "unable to resolve cache metadata"}, 500

    target = None
    for e in entries:
        if not isinstance(e, dict):
            continue
        if str(e.get("catalog") or "") != catalog:
            continue
        if str(e.get("region") or "") != region:
            continue
        if catalog in ("opensearch", "opensearch_limits", "eks_ami") and e.get("engine") != (engine or ("3.3" if catalog.startswith("opensearch") else BY_KEY[catalog].engines()[0])):
            continue
        if catalog in ("aurora", "aurora_versions"):
            if e.get("engine") != requested_engine:
                continue
        target = e
        break

    if not target:
        return {"ok": False, "error": "cache not found"}, 404

    path = str(target.get("cache_path") or "")
    payload = _safe_read_json(path)
    if payload is None:
        return {"ok": False, "error": "cache not found"}, 404

    text = json.dumps(payload, indent=2, sort_keys=True)
    return Response(text, mimetype="application/json")


@bp.get("/pc-source-cache")
def pc_source_cache():
    from .service import collect_pc_source_cache_status

    import re

    branch = (request.args.get("branch") or "").strip()
    if branch and len(branch) > 200:
        branch = branch[:200]
    if branch and branch != "default" and not re.fullmatch(r"[A-Za-z0-9._/-]+", branch):
        branch = ""

    data = collect_pc_source_cache_status(focus_branch=branch if branch else None)
    all_data = collect_pc_source_cache_status(focus_branch=None)

    github_owner = (
        os.getenv("PS_GITHUB_OWNER")
        or current_app.config.get("PS_GITHUB_OWNER")
        or ""
    ).strip()
    github_repo = (
        os.getenv("PC_GENERIC_REPO")
        or os.getenv("PS_GITHUB_REPO")
        or current_app.config.get("PC_GENERIC_REPO")
        or current_app.config.get("PS_GITHUB_REPO")
        or "devops-praxis-core"
    ).strip()
    github_url = f"https://github.com/{github_owner}/{github_repo}" if github_owner and github_repo else None

    all_branches = sorted(
        {str(e.get("branch_display")) for e in (all_data.get("entries") or []) if e.get("branch_display")},
        key=lambda b: (0 if b == "default" else 1, b.lower()),
    )

    return render_template(
        "status/pc_source_cache.html",
        data=data,
        all_branches=all_branches,
        selected_branch=(branch or None),
        github_owner=github_owner,
        github_repo=github_repo,
        github_url=github_url,
    )


@bp.get("/pc-source-cache/partials/caches")
def pc_source_cache_partials_caches():
    from .service import collect_pc_source_cache_status

    import re

    branch = (request.args.get("branch") or "").strip()
    if branch and len(branch) > 200:
        branch = branch[:200]
    if branch and branch != "default" and not re.fullmatch(r"[A-Za-z0-9._/-]+", branch):
        branch = ""

    data = collect_pc_source_cache_status(focus_branch=branch if branch else None)
    return render_template(
        "status/pc_source_cache/_caches.html",
        data=data,
        selected_branch=(branch or None),
    )


@bp.get("/pc-source-cache/partials/previews")
def pc_source_cache_partials_previews():
    from .service import collect_pc_source_cache_status

    import re

    branch = (request.args.get("branch") or "").strip()
    if branch and len(branch) > 200:
        branch = branch[:200]
    if branch and branch != "default" and not re.fullmatch(r"[A-Za-z0-9._/-]+", branch):
        branch = ""

    data = collect_pc_source_cache_status(focus_branch=branch if branch else None)
    return render_template(
        "status/pc_source_cache/_previews.html",
        data=data,
        selected_branch=(branch or None),
    )


@bp.post("/pc-source-cache/jobs")
def pc_source_cache_job_start():
    """
    Start an async refresh job (returns quickly; work is done in a background thread).
    """
    from .pc_source_jobs import start_refresh_job

    import re

    branch = (request.form.get("branch") or "").strip()
    if branch and len(branch) > 200:
        return {"ok": False, "error": "branch too long"}, 400
    if branch and branch != "default" and not re.fullmatch(r"[A-Za-z0-9._/-]+", branch):
        return {"ok": False, "error": f"invalid branch: {branch!r}"}, 400

    if branch:
        branches = [None] if branch.lower() == "default" else [branch]
    else:
        raw = (os.getenv("PS_PC_SOURCE_REFRESH_BRANCHES") or "").strip()
        if not raw:
            branches = [None]
        else:
            parts = [p.strip() for p in raw.split(",") if p.strip()]
            branches = []
            for p in parts:
                if p.lower() == "default":
                    branches.append(None)
                else:
                    branches.append(p)
            # De-dupe while keeping order.
            seen: set[str] = set()
            uniq: list[str | None] = []
            for b in branches:
                key = (b or "default").lower()
                if key in seen:
                    continue
                seen.add(key)
                uniq.append(b)
            branches = uniq or [None]

    job = start_refresh_job(branches=branches)
    job_id = job.get("job_id")
    if not job_id:
        return job, 429
    job["poll_url"] = url_for("status.pc_source_cache_job_status", job_id=job_id)
    return job, 202


@bp.post("/pc-source-cache/clear")
def pc_source_cache_clear():
    """Delete the cached PC Source inventory for one explicitly selected branch."""
    import re

    branch = (request.form.get("branch") or "").strip()
    confirmed = (request.form.get("confirm") or "").strip().lower()
    if not branch:
        return {"ok": False, "error": "branch is required"}, 400
    if len(branch) > 200:
        return {"ok": False, "error": "branch too long"}, 400
    if branch != "default" and not re.fullmatch(r"[A-Za-z0-9._/-]+", branch):
        return {"ok": False, "error": "invalid branch"}, 400
    if confirmed != "delete":
        return {"ok": False, "error": "cache deletion was not confirmed"}, 400

    from services.pc_source_scanner.structure_cache import clear_pc_source_sections

    clear_pc_source_sections(None if branch.lower() == "default" else branch)
    return redirect(url_for("status.pc_source_cache", branch=branch, cache_cleared="1"))


@bp.get("/pc-source-cache/jobs/<job_id>.json")
def pc_source_cache_job_status(job_id: str):
    from .pc_source_jobs import get_job

    import re

    if not re.fullmatch(r"[a-f0-9]{32}", (job_id or "").strip()):
        return {"ok": False, "error": "invalid job id"}, 400
    job = get_job(job_id)
    if not job:
        return {"ok": False, "error": "job not found"}, 404
    return job, 200


@bp.get("/pc-source-cache/json")
def pc_source_cache_json():
    """
    Return the raw cached JSON for a given branch.
    """
    branch = (request.args.get("branch") or "").strip()
    if not branch:
        return {"ok": False, "error": "branch is required"}, 400

    import re

    if branch != "default" and not re.fullmatch(r"[A-Za-z0-9._/-]+", branch):
        return {"ok": False, "error": "invalid branch"}, 400

    from .service import collect_pc_source_cache_status

    data = collect_pc_source_cache_status(focus_branch=branch)
    entries = data.get("entries") if isinstance(data, dict) else None
    if not isinstance(entries, list):
        return {"ok": False, "error": "unable to resolve cache metadata"}, 500

    target = None
    for e in entries:
        if isinstance(e, dict) and str(e.get("branch_display") or "") == branch:
            target = e
            break
    if not target:
        return {"ok": False, "error": "cache not found"}, 404

    path = str(target.get("cache_path") or "")
    payload = _safe_read_json(path)
    if payload is None:
        return {"ok": False, "error": "cache not found"}, 404

    text = json.dumps(payload, indent=2, sort_keys=True)
    return Response(text, mimetype="application/json")


@bp.get("/aws-catalogs/iam-policy.json")
def aws_catalogs_iam_policy():
    from services.aws_instance_types.registry import iam_policy
    return iam_policy()
