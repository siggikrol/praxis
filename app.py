#!/usr/bin/env python3
from __future__ import annotations

import logging
import os
import sys
import time
from datetime import datetime
from urllib.parse import urlencode

from flask import Flask, abort, flash, render_template, session, redirect, request, url_for, current_app
from flask_session import Session
from flask_wtf import CSRFProtect
from werkzeug.middleware.proxy_fix import ProxyFix
from werkzeug.routing import BuildError
from jinja2 import ChoiceLoader, FileSystemLoader
from services.github_helpers.blueprint import github_bp

# --- Import path bootstrap ----------------------------------------------------
PROJECT_ROOT = os.path.dirname(os.path.abspath(__file__))
if PROJECT_ROOT not in sys.path:
    sys.path.insert(0, PROJECT_ROOT)
# -----------------------------------------------------------------------------

# Logging
from logs.logging_setup import configure_logging  # noqa: E402
from logs.request_logging import init_request_logging  # noqa: E402

configure_logging()
_APP_START_MONOTONIC = time.perf_counter()

# Blueprints
from auth import bp as auth_bp  # noqa: E402

# --- Autoload pluggable modules from ./modules/*/plugin.py --------------------
import importlib  # noqa: E402
from pathlib import Path  # noqa: E402


def load_plugins(app: Flask) -> None:
    """
    Autoload modules under ./modules/<pkg>/plugin.py.

    Module conventions:
      - EITHER expose a 'register(app)' function
      - OR expose a Flask Blueprint named 'bp'

    Optional allowlist:
      - env ENABLED_MODULES="status,wizards"  (if unset → load all)
    """

    modules_dir = Path(__file__).parent / "modules"
    if not modules_dir.exists():
        app.logger.info("No modules directory found; skipping plugin load")
        return

    allowlist = {
        s.strip()
        for s in os.environ.get("ENABLED_MODULES", "").split(",")
        if s.strip()
    } or None  # if empty, load all



    app.logger.info("Autoloading plugins from %s", modules_dir)

    for pkg_dir in sorted(p.name for p in modules_dir.iterdir() if (p / "__init__.py").exists()):
        
        if pkg_dir == "github_helpers":
            app.logger.info("Skipping github_helpers (now a service)")
            continue

        # 🔥 exclude github_helpers because it's service
        if pkg_dir == "github_helpers":
            app.logger.info("Skipping module '%s' (treated as service, not plugin)", pkg_dir)
            continue

        # existing allowlist logic
        if allowlist and pkg_dir not in allowlist:
            app.logger.info("Skipping module '%s' (not in ENABLED_MODULES)", pkg_dir)
            continue

        plugin_mod = f"modules.{pkg_dir}.plugin"
        plugin_started = time.perf_counter()
        try:
            app.logger.info("Importing plugin: %s", plugin_mod)
            mod = importlib.import_module(plugin_mod)
        except ModuleNotFoundError:
            app.logger.info("Module '%s' has no plugin.py; skipping", pkg_dir)
            continue
        except Exception as e:
            app.logger.exception("Failed to import %s: %s", plugin_mod, e)
            continue

        try:
            register = getattr(mod, "register", None)
            if callable(register):
                result = register(app)
                app.logger.info(
                    "Registered module '%s' via register(app) in %.1fms",
                    pkg_dir, (time.perf_counter() - plugin_started) * 1000,
                )

                # NEW: collect optional homepage links
                if isinstance(result, dict) and result.get("home_endpoint"):
                    result.setdefault("category", "Wizards")
                    app.home_links.append(result)

                continue
            
        except Exception as e:
            app.logger.exception("Error registering module '%s': %s", pkg_dir, e)
            continue

        try:
            bp = getattr(mod, "bp", None)
            if bp is not None:
                # Only register if this blueprint name doesn't already exist
                if bp.name not in app.blueprints:
                    app.register_blueprint(bp)
                    app.logger.info("Registered module '%s' via blueprint 'bp'", pkg_dir)
                else:
                    app.logger.info(
                        "Skipping duplicate blueprint '%s' (already registered by %s)",
                        bp.name,
                        app.blueprints[bp.name],
                    )
            else:
                app.logger.debug("Module '%s' has no blueprint or register() function", pkg_dir)
        except Exception as e:
            app.logger.exception("Error attaching blueprint for '%s': %s", pkg_dir, e)
# ------------------------------------------------------------------------------


app = Flask(__name__)
app.home_links = []


def _normalize_runtime_env(raw_value: str | None) -> str:
    """Normalize runtime environment labels for UI and behavior toggles."""
    value = (raw_value or "").strip().lower()
    aliases = {
        "production": "prod",
        "stage": "staging",
        "stg": "staging",
    }
    value = aliases.get(value, value)
    return value or "unknown"

module_template_path = os.path.join(app.root_path, "engine", "wizards", "templates")
if os.path.isdir(module_template_path):
    app.jinja_loader = ChoiceLoader([
        app.jinja_loader,
        FileSystemLoader(module_template_path),
    ])


# Make has_endpoint always available to templates
def has_endpoint(name: str) -> bool:
    try:
        url_for(name)
        return True
    except BuildError:
        return False
    except Exception:
        return False

app.jinja_env.globals.update(has_endpoint=has_endpoint)

# ---- Core config -------------------------------------------------------------
# Require a real secret; no debug fallback to avoid weak cookies in any environment.
app.secret_key = os.environ.get("FLASK_SECRET")
if not app.secret_key:
    raise RuntimeError("FLASK_SECRET must be set")

# Auth always enabled (no environment override)
app.config["AUTH_ENABLED"] = True

# CSRF is enforced globally; individual handlers can opt out explicitly if needed.
app.config["WTF_CSRF_CHECK_DEFAULT"] = True
# In proxied/TLS-terminated environments, rely on token rather than Referer.
app.config["WTF_CSRF_SSL_STRICT"] = False

# App meta / docs
app.config["APP_VERSION"] = os.environ.get("APP_VERSION") or os.environ.get("K_REVISION") or "dev"
app.config["DOCS_URL"] = os.environ.get("DOCS_URL", "")
runtime_env_raw = (
    os.environ.get("PS_RUNTIME_ENV")
    or os.environ.get("APP_ENV")
    or os.environ.get("ENVIRONMENT")
    or os.environ.get("FLASK_ENV")
)
app.config["RUNTIME_ENV"] = _normalize_runtime_env(runtime_env_raw)
app.config["RUNTIME_ENV_IS_PROD"] = app.config["RUNTIME_ENV"] == "prod"

# GitHub App
app.config["SECRET_KEY"] = os.environ.get("SECRET_KEY") or os.environ.get("FLASK_SECRET")
app.config["GITHUB_APP_ID"] = os.environ.get("GITHUB_APP_ID")
app.config["GITHUB_INSTALLATION_ID"] = os.environ.get("GITHUB_INSTALLATION_ID")
app.config["GITHUB_PRIVATE_KEY"] = os.environ.get("GITHUB_PRIVATE_KEY")
app.config["GITHUB_DEFAULT_PRIVATE"] = (os.environ.get("GITHUB_PRIVATE") or "1") in ("1", "true", "True")

# Azure AD (optional; used by auth flows)
app.config["CLIENT_ID"] = os.getenv("CLIENT_ID")
app.config["CLIENT_SECRET"] = os.getenv("CLIENT_SECRET")
app.config["TENANT_ID"] = os.getenv("TENANT_ID")
if app.config.get("TENANT_ID"):
    app.config["AUTHORITY"] = f"https://login.microsoftonline.com/{app.config['TENANT_ID']}"
app.config["REDIRECT_PATH"] = "/auth/aad_callback"
app.config["SCOPE"] = ["User.Read"]

# ---- Sessions: filesystem storage -------------------------------------------
app.config.update(
    SESSION_TYPE="filesystem",
    # Keep session state (including legacy drafts awaiting SQLite migration)
    # on the same persistent cache volume as the application databases.
    SESSION_FILE_DIR=os.environ.get(
        "SESSION_FILE_DIR",
        "/tmp/praxis-cache/flask_sessions",
    ),
    SESSION_PERMANENT=False,
    SESSION_USE_SIGNER=True,
    SESSION_KEY_PREFIX="ps_",
    SESSION_COOKIE_NAME="ps_session",
    SESSION_COOKIE_SAMESITE="Lax",
    SESSION_COOKIE_HTTPONLY=True,
    SESSION_COOKIE_SECURE=os.getenv("FLASK_SECURE_COOKIES", "0" if app.debug else "1").lower() in {"1", "true", "yes"},
)
os.makedirs(app.config["SESSION_FILE_DIR"], exist_ok=True)  # ensure writable
Session(app)

# ---- CSRF init ---------------------------------------------------------------
csrf = CSRFProtect()
csrf.init_app(app)

# ---- Logging / middleware ----------------------------------------------------
init_request_logging(app)
logging.getLogger("app").info("startup", extra={"version": app.config.get("APP_VERSION") or "dev"})
app.wsgi_app = ProxyFix(app.wsgi_app, x_for=1, x_proto=1, x_host=1, x_port=1)
app.config["PREFERRED_URL_SCHEME"] = "https"

# --- Register core blueprint(s) ----------------------------------------------
app.register_blueprint(auth_bp)
app.register_blueprint(github_bp)

# ---- Load pluggable modules AFTER core config/session ------------------------
load_plugins(app)

# Validate key caches at startup so first-page status checks are less likely to block
# on expensive refreshes (configurable via PS_VALIDATE_CACHES_ON_STARTUP* env vars).
from services.cache_startup_validator import validate_caches_on_startup  # noqa: E402

validate_caches_on_startup(app)

# --- Background refreshers ---------------------------------------------------
# Keep dynamic AWS-backed option lists (like EC2 instance types) reasonably fresh without
# requiring app restarts. Safe to call in each gunicorn worker; refresh uses a file lock.
from services.aws_instance_types.refresher import start_aws_catalog_refresher  # noqa: E402

start_aws_catalog_refresher()

# Keep PC Source whitelist discovery reasonably fresh (GitHub folder structure cached on disk).
from services.pc_source_scanner.refresher import start_pc_source_refresher  # noqa: E402

start_pc_source_refresher()

# --- Auth guard ---------------------------------------------------------------
def _auth_is_public(path: str) -> bool:
    return path.startswith("/static") or path.startswith("/healthz") or path.startswith("/auth")


@app.before_request
def _require_login():
    if not app.config.get("AUTH_ENABLED", False):
        return None
    path = (request.path or "/")
    if _auth_is_public(path):
        return None
    if session.get("user"):
        try:
            from services.session_activity import touch_active_session

            touch_active_session(
                session_dir=app.config.get("SESSION_FILE_DIR", "/tmp/flask_sessions"),
                sid=getattr(session, "sid", None) or request.cookies.get(app.config.get("SESSION_COOKIE_NAME", "ps_session")),
                user=str(session.get("user") or ""),
                auth_method=str(session.get("auth_method") or ""),
                login_ts=session.get("login_ts"),
                force=False,
            )
        except Exception:
            logging.getLogger("app").debug("Session activity touch failed", exc_info=True)
        return None
    nxt = request.full_path if request.query_string else request.path
    return redirect(url_for("auth.login") + "?" + urlencode({"next": nxt}), code=302)

# --- Routes -------------------------------------------------------------------
def _category_home_links(category: str):
    items = [link for link in app.home_links if link.get("category") == category]
    if category != "Wizards":
        return items

    wizard_order = {
        "multi-vpc.env_home": 0,
        "single-vpc.env_home": 1,
        "rgs.env_home": 2,
        "loyalty.env_home": 3,
        "unified.env_home": 4,
    }
    return sorted(
        items,
        key=lambda link: (
            wizard_order.get(link.get("home_endpoint"), len(wizard_order)),
            link.get("title", ""),
        ),
    )


@app.route("/")
def index():
    current_app.logger.debug("Registered blueprints: %s", list(app.blueprints.keys()))
    praxis_core_version = (
        (os.getenv("PS_CORE_VALIDATE_DEFAULT_PC_VERSION") or "1.3.1").strip()
        or "1.3.1"
    )
    if session.get("user"):
        current_hour = datetime.now().hour
        home_greeting = (
            "Good morning" if current_hour < 12
            else "Good afternoon" if current_hour < 18
            else "Good evening"
        )
        readiness_home = None
        gh_quick = None
        spacelift_quick = None
        aws_catalog_quick = None
        pc_source_quick = None
        core_validate_quick = None
        confluence_quick = None
        try:
            from modules.status.service import (
                cached_github_status,
                cached_spacelift_status,
                quick_aws_catalog_status,
                quick_pc_source_status,
            )

            gh_quick = cached_github_status()
            spacelift_quick = cached_spacelift_status()
            aws_catalog_quick = quick_aws_catalog_status()
            pc_source_quick = quick_pc_source_status()
        except Exception:
            pass
        try:
            from engine.wizards.factory.core_validate_jobs import feature_state
            from modules.spec_validator.confluence import load_config

            core_validate_quick = feature_state()
            confluence_quick = load_config().as_dict()
        except Exception:
            pass
        try:
            from modules.environment_readiness.store import home_snapshot

            readiness_home = home_snapshot(str(session.get("user") or ""))
        except Exception:
            current_app.logger.exception("Readiness home snapshot unavailable")
        return render_template(
            "index.html",
            readiness_home=readiness_home,
            home_greeting=home_greeting,
            gh_quick=gh_quick,
            spacelift_quick=spacelift_quick,
            aws_catalog_quick=aws_catalog_quick,
            pc_source_quick=pc_source_quick,
            core_validate_quick=core_validate_quick,
            confluence_quick=confluence_quick,
            praxis_core_version=praxis_core_version,
        )
    return redirect(url_for("auth.login", next="/"), code=302)

@app.route("/wizards")
def wizards():
    from datetime import datetime
    from engine.wizards.factory.core_validate_jobs import get_job
    from engine.wizards.factory.views import _core_validate_outcome, _spec_hash
    from services.wizard_workspaces import (
        current_owner,
        ensure_active_workspace,
        list_workspaces,
        sync_workspace,
        workspace_environment_names,
        workspace_release_details,
    )

    owner = current_owner(session)
    # Adopt pre-workspace browser sessions without losing their current work.
    for env_slug in ("multi-vpc", "single-vpc", "rgs", "loyalty", "unified"):
        prefix = f"{env_slug}:"
        if not any(key.startswith(prefix) for key in session.keys()):
            continue
        workspace = ensure_active_workspace(session, env_slug)
        if workspace.get("access") == "viewer":
            continue
        spec_yaml = str(session.get(f"{env_slug}:spec_yaml") or "")
        sync_workspace(
            workspace_id=str(workspace["id"]),
            owner=str(workspace["owner"]),
            env_slug=env_slug,
            session_obj=session,
            current_step="finish" if spec_yaml else str(workspace.get("current_step") or "project_settings"),
            spec_yaml=spec_yaml,
            latest_job_id=str(session.get(f"{env_slug}:core_validate_latest_job_id") or ""),
        )
    owned_workspaces, shared_workspaces = list_workspaces(owner)
    from modules.environment_readiness.store import setups_for_workspaces
    from services.wizard_workspaces import annotate_workspace_origins

    visible_workspaces = owned_workspaces + shared_workspaces
    annotate_workspace_origins(
        visible_workspaces,
        setups_for_workspaces([str(item["id"]) for item in visible_workspaces]),
    )

    def _decorate(workspace):
        item = dict(workspace)
        item["environment_names"] = workspace_environment_names(item)
        item["release"] = workspace_release_details(item)
        job_id = str(item.get("latest_job_id") or "").strip()
        job = get_job(job_id) if job_id else None
        item["validation"] = _core_validate_outcome(
            job,
            _spec_hash(str(item.get("spec_yaml") or "")),
        )
        try:
            item["updated_label"] = datetime.fromtimestamp(
                float(item.get("updated_at") or 0)
            ).strftime("%Y-%m-%d %H:%M")
        except Exception:
            item["updated_label"] = "-"
        return item

    return render_template(
        "category.html",
        category="Wizards",
        category_items=_category_home_links("Wizards"),
        subtitle="Guided, step-by-step flows for provisioning and setup.",
        show_checklist=True,
        owned_workspaces=[_decorate(item) for item in owned_workspaces],
        shared_workspaces=[_decorate(item) for item in shared_workspaces],
    )


@app.get("/wizards/workspaces/<workspace_id>/resume")
def resume_wizard_workspace(workspace_id):
    from services.wizard_workspaces import (
        current_owner,
        get_workspace_for_user,
        load_workspace_state,
    )

    workspace = get_workspace_for_user(current_owner(session), workspace_id)
    if not workspace:
        # A readiness revision may still reference a workspace the owner just
        # deleted. Repair that stale cross-store link instead of leaving a dead
        # Continue URL behind.
        from modules.environment_readiness.store import unlink_wizard_workspace

        setup_id = unlink_wizard_workspace(
            workspace_id, wizard_owner=current_owner(session)
        )
        if setup_id:
            flash(
                "That wizard workspace was removed. You can start it again from this setup.",
                "warning",
            )
            return redirect(
                url_for("environment_readiness.setup_detail", setup_id=setup_id)
            )
        abort(404)
    load_workspace_state(workspace, session)
    env_slug = str(workspace["env_slug"])
    if workspace.get("access") == "viewer" or workspace.get("current_step") == "finish":
        return redirect(url_for(f"{env_slug}_wizard.wizard_finish"))
    # Workspaces saved on retired 4.0 wizard cards should resume at the next
    # meaningful active step rather than falling back to the start.
    current_step = str(workspace.get("current_step") or "project_settings")
    current_step = {
        "jinja2": "pc_source",
        "grafana": "vpc",
    }.get(current_step, current_step)
    return redirect(
        url_for(
            f"{env_slug}_wizard.wizard_step",
            step=current_step,
        )
    )


@app.get("/wizards/workspaces/<workspace_id>/release")
def change_wizard_workspace_release(workspace_id):
    """Open release selection for a saved setup without losing its state."""
    from services.wizard_workspaces import (
        current_owner,
        get_workspace_for_user,
        load_workspace_state,
    )

    workspace = get_workspace_for_user(current_owner(session), workspace_id)
    if not workspace:
        abort(404)
    if workspace.get("access") == "viewer":
        abort(403)

    env_slug = str(workspace.get("env_slug") or "")
    if env_slug not in {"multi-vpc", "single-vpc", "rgs", "loyalty", "unified"}:
        abort(404)

    load_workspace_state(workspace, session)
    return redirect(
        url_for(
            f"{env_slug}.release_selection",
            edit="1",
            return_step=str(workspace.get("current_step") or "project_settings"),
        )
    )


@app.get("/wizards/workspaces/<workspace_id>/finish")
def open_wizard_workspace_finish(workspace_id):
    from services.wizard_workspaces import (
        current_owner,
        get_workspace_for_user,
        load_workspace_state,
    )

    workspace = get_workspace_for_user(current_owner(session), workspace_id)
    if not workspace:
        abort(404)
    load_workspace_state(workspace, session)
    return redirect(url_for(f"{workspace['env_slug']}_wizard.wizard_finish"))


@app.post("/wizards/workspaces/<workspace_id>/delete")
def delete_wizard_workspace(workspace_id):
    from services.wizard_workspaces import (
        current_owner,
        delete_workspace,
        get_workspace_for_user,
    )

    owner = current_owner(session)
    workspace = get_workspace_for_user(owner, workspace_id)
    if not workspace or workspace.get("owner") != owner:
        abort(404)
    if not delete_workspace(owner=owner, workspace_id=workspace_id):
        abort(404)

    from modules.environment_readiness.store import unlink_wizard_workspace

    unlink_wizard_workspace(workspace_id, wizard_owner=owner)

    env_slug = str(workspace.get("env_slug") or "")
    if session.get(f"{env_slug}:workspace_id") == workspace_id:
        for key in list(session.keys()):
            if key.startswith(f"{env_slug}:"):
                session.pop(key, None)
        session.modified = True

    flash(f"Removed setup {workspace.get('title') or workspace_id}.", "success")
    return redirect(url_for("wizards"))


@app.route("/wizards/workspaces/<workspace_id>/sharing", methods=["GET", "POST"])
def wizard_workspace_sharing(workspace_id):
    from services.wizard_workspaces import (
        current_owner,
        get_workspace_for_user,
        list_shares,
        set_share,
    )

    owner = current_owner(session)
    workspace = get_workspace_for_user(owner, workspace_id)
    if not workspace or workspace.get("owner") != owner:
        abort(404)
    if request.method == "POST":
        if set_share(
            owner=owner,
            workspace_id=workspace_id,
            shared_with=request.form.get("shared_with", ""),
            permission=request.form.get("permission", "viewer"),
        ):
            flash("Workspace access updated.", "success")
        else:
            flash("Enter another user and select a valid permission.", "warning")
        return redirect(url_for("wizard_workspace_sharing", workspace_id=workspace_id))
    return render_template(
        "wizard_workspace_sharing.html",
        workspace=workspace,
        shares=list_shares(owner, workspace_id),
    )


@app.post("/wizards/workspaces/<workspace_id>/sharing/<path:shared_with>/remove")
def remove_wizard_workspace_share(workspace_id, shared_with):
    from services.wizard_workspaces import current_owner, remove_share

    if not remove_share(
        owner=current_owner(session),
        workspace_id=workspace_id,
        shared_with=shared_with,
    ):
        abort(404)
    flash("Workspace access removed.", "success")
    return redirect(url_for("wizard_workspace_sharing", workspace_id=workspace_id))

@app.route("/tools")
def tools():
    return render_template(
        "category.html",
        category="Tools",
        category_items=_category_home_links("Tools"),
        subtitle="Targeted utilities and explorers for quick operational tasks.",
        show_checklist=False,
    )

@app.route("/info")
def info():
    return render_template("info.html")


@app.get("/healthz")
def healthz():
    return "ok", 200

# Security headers
@app.after_request
def _set_security_headers(resp):
    resp.headers.setdefault("X-Content-Type-Options", "nosniff")
    resp.headers.setdefault("X-Frame-Options", "SAMEORIGIN")
    resp.headers.setdefault("Referrer-Policy", "no-referrer")
    return resp

# Template context
@app.context_processor
def inject_nav_flags():
    return {"auth_enabled": app.config.get("AUTH_ENABLED", False), "current_user": session.get("user")}


@app.context_processor
def inject_meta():
    v = os.environ.get("APP_VERSION") or current_app.config.get("APP_VERSION") or "dev"
    return {
        "APP_VERSION": v,
        "app_version": v,
        "runtime_env": current_app.config.get("RUNTIME_ENV", "unknown"),
        "runtime_env_is_prod": bool(current_app.config.get("RUNTIME_ENV_IS_PROD")),
    }

@app.context_processor
def inject_home_links():
    return {"home_links": app.home_links}


logging.getLogger("app").info(
    "studio_ready",
    extra={
        "duration_ms": round((time.perf_counter() - _APP_START_MONOTONIC) * 1000, 1),
        "plugins": len(app.home_links),
        "pid": os.getpid(),
    },
)


if __name__ == "__main__":
    os.chdir(PROJECT_ROOT)
    app.run(host="0.0.0.0", port=5000)
