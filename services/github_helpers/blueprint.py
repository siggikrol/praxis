from __future__ import annotations
import base64, os, html, os.path as _p
from flask import (
    Blueprint, current_app, render_template_string, session,
    Response, stream_with_context, request, url_for, render_template, redirect
)
from services.github_helpers.github_api import (
    get_repo,
    get_branch,
    create_repo,
    update_repo,
    ensure_branch_from,
    wait_for_branch,
    wait_for_commit,
    set_repo_custom_properties,
    create_git_blob,
    create_git_tree,
    create_git_commit,
    update_git_ref
)
from services.github_auth import get_github_app_auth
from engine.wizards.factory.utils import _key
from services.spec_fingerprint import spec_fingerprint
from .forms import (
    DEFAULT_HARBOR_REGISTRY_PROJECT,
    DEFAULT_PC_VERSION,
    PCDeployWorkflowForm,
)

# --------------------------------------------------------
# 🔥 ONE UNIVERSAL BLUEPRINT FOR ALL ENVIRONMENTS
# --------------------------------------------------------
github_bp = Blueprint(
    "github",
    __name__,
    url_prefix="/github",
    template_folder="templates"
)


# --------------------------------------------------------
# Utility: extract env slug **only from POST/GET values**
# (Referer fallback removed because it causes wrong slugs across workers)
# --------------------------------------------------------
def get_env_slug_from_request() -> str:
    slug = request.values.get("env_slug", "").strip()
    return slug


def _spec_hash(spec_yaml: str) -> str:
    return spec_fingerprint(spec_yaml)


def _core_validate_gate_ok(env_slug: str) -> bool:
    from services.wizard_workspaces import current_owner, get_workspace_for_user

    workspace_id = str(session.get(f"{env_slug}:workspace_id") or "").strip()
    workspace = (
        get_workspace_for_user(current_owner(session), workspace_id)
        if workspace_id else None
    )
    if not workspace or workspace.get("access") not in {"owner", "editor"}:
        return False
    spec_yaml = session.get(f"{env_slug}:spec_yaml")
    if not isinstance(spec_yaml, str) or not spec_yaml:
        return False
    approved = str(session.get(f"{env_slug}:core_validate_ok_spec_hash") or "").strip()
    return bool(approved and approved == _spec_hash(spec_yaml))


def _core_validate_gate_redirect(env_slug: str):
    return redirect(
        url_for(
            f"{env_slug}_wizard.wizard_finish",
            core_validate_error=(
                "Run Praxis Core validation successfully before continuing to GitHub repo creation."
            ),
        )
    )


def _default_workflow_values() -> dict[str, str]:
    return {
        "harbor_registry_project": DEFAULT_HARBOR_REGISTRY_PROJECT,
        "pc_version": DEFAULT_PC_VERSION,
    }


def _store_workflow_values(env_slug: str, values: dict[str, str]) -> None:
    session["github_workflow"] = values
    session["github_env_slug"] = env_slug
    session.modified = True


def _render_workflow_page(
    form: PCDeployWorkflowForm,
    env_slug: str,
    back_url: str,
    is_prod: bool,
    prod_confirm_error: str | None = None,
    spec_yaml: str | None = None,
    repo_name=None,
):
    return render_template(
        "github_workflow.html",
        form=form,
        env_slug=env_slug,
        back_url=back_url,
        is_prod=is_prod,
        prod_confirm_error=prod_confirm_error,
        prod_locked_values=_default_workflow_values(),
        spec_yaml=spec_yaml,
        repo_name=repo_name,
    )


# --------------------------------------------------------
# Workflow route for GitHub repo creation
# --------------------------------------------------------
@github_bp.route("/workflow", methods=["GET", "POST"])
def github_workflow():
    form = PCDeployWorkflowForm()
    is_prod = bool(current_app.config.get("RUNTIME_ENV_IS_PROD"))

    if request.method == "GET":
        env_slug = (request.args.get("env_slug") or "").strip()
        if not env_slug:
            return "Missing env_slug", 400

        if not _core_validate_gate_ok(env_slug):
            return _core_validate_gate_redirect(env_slug)

        spec_yaml = session.get(_key(env_slug, "spec_yaml"))

        back_url = url_for(f"{env_slug}_wizard.wizard_finish")

        # --------------------------------------------------------
        # 🔥 BUILD REPO NAME FOR FRONTEND CONFIRMATION
        # --------------------------------------------------------
        repo_cfg = session.get(_key(env_slug, "repository_settings"), {}) or {}
        new_prefix_raw = (repo_cfg.get("new_prefix") or "").strip()

        import re
        def _slugify(s: str) -> str:
            return re.sub(r"[^a-z0-9-]+", "-", (s or "").strip().lower()).strip("-")

        repo_name = _slugify(new_prefix_raw) if new_prefix_raw else ""

        if is_prod:
            _store_workflow_values(env_slug, _default_workflow_values())
            session["github_prod_confirmed_env_slug"] = ""
            session.modified = True

            return _render_workflow_page(
                form,
                env_slug,
                back_url,
                is_prod=True,
                spec_yaml=spec_yaml,
                repo_name=repo_name,
            )

        session.pop("github_prod_confirmed_env_slug", None)

        # Carry the exact image coordinates used by the successful Core
        # validation into seed-repository creation.
        harbor_project = str(
            session.get(f"{env_slug}:core_validate_harbor_project")
            or DEFAULT_HARBOR_REGISTRY_PROJECT
        ).strip()
        pc_version = str(
            session.get(f"{env_slug}:core_validate_pc_version") or DEFAULT_PC_VERSION
        ).strip()
        form.harbor_registry_project.data = harbor_project
        form.pc_version.data = pc_version
        _store_workflow_values(
            env_slug,
            {
                "harbor_registry_project": harbor_project,
                "pc_version": pc_version,
            },
        )

        return _render_workflow_page(
            form,
            env_slug,
            back_url,
            is_prod=False,
            spec_yaml=spec_yaml,
            repo_name=repo_name,
        )

    # POST
    env_slug = (request.form.get("env_slug") or session.get("github_env_slug") or "").strip()
    if not env_slug:
        return "Missing env_slug", 400
    if not _core_validate_gate_ok(env_slug):
        return _core_validate_gate_redirect(env_slug)
    back_url = url_for(f"{env_slug}_wizard.wizard_finish")

    spec_yaml = session.get(_key(env_slug, "spec_yaml"))

    if is_prod:
        _store_workflow_values(env_slug, _default_workflow_values())
        confirmed = (request.form.get("prod_confirm") or "").strip().lower() in ("1", "true", "on", "yes")
        if not confirmed:
            session["github_prod_confirmed_env_slug"] = ""
            session.modified = True
            return _render_workflow_page(
                form,
                env_slug,
                back_url,
                is_prod=True,
                prod_confirm_error="Please confirm before creating a production repository.",
                spec_yaml=spec_yaml,
            )
        session["github_prod_confirmed_env_slug"] = env_slug
        session.modified = True
        return render_template("github_forward.html")

    if form.validate_on_submit():
        session.pop("github_prod_confirmed_env_slug", None)
        _store_workflow_values(
            env_slug,
            {
                "harbor_registry_project": form.harbor_registry_project.data,
                "pc_version": form.pc_version.data,
            },
        )
        return render_template("github_forward.html")

    # Invalid POST
    return _render_workflow_page(form, env_slug, back_url, is_prod=False, spec_yaml=spec_yaml)

# --------------------------------------------------------
# Main route for GitHub repo creation
# --------------------------------------------------------
@github_bp.route("/init", methods=["POST"])
def github_init():

    # Always explicit, never infer from Referer
    env_slug = get_env_slug_from_request()

    if not env_slug:
        current_app.logger.error(
            "github_init: missing env_slug in POST. form=%s headers=%s",
            dict(request.form),
            dict(request.headers)
        )
        return "Missing env_slug for GitHub operation", 400
    if not _core_validate_gate_ok(env_slug):
        return _core_validate_gate_redirect(env_slug)

    if current_app.config.get("RUNTIME_ENV_IS_PROD"):
        confirmed_env = (session.get("github_prod_confirmed_env_slug") or "").strip()
        if confirmed_env != env_slug:
            return "Production confirmation required. Return to the previous step.", 400
        session.pop("github_prod_confirmed_env_slug", None)
        session.modified = True

    gh_app = get_github_app_auth(current_app.config)

    if not gh_app:
        page = render_template_string(
            "{% extends 'base.html' %}{% block content %}"
            "<div class='card shadow-sm border-0'><div class='card-body'>"
            "<h5 class='card-title mb-2'>GitHub App Authentication not configured</h5>"
            "<p class='text-muted mb-0'>Set GitHub App credentials in app config.</p>"
            "</div></div>{% endblock %}"
        )
        return page, 400

    # --------------------------------------------------------
    # FIXED: Correct Back / Fail links
    # --------------------------------------------------------
    shell = render_template_string(
        "{% extends 'base.html' %}{% block content %}"
        "<div class='card shadow-sm border-0'><div class='card-body'>"
        "<div class='d-flex align-items-center gap-2 mb-2'>"
        "<div class='spinner-border spinner-border-sm text-primary' role='status'></div>"
        "<h5 class='card-title m-0'>Preparing repository</h5></div>"
        "<ul id='log' class='list-unstyled small mb-3'></ul>"

        "<div id='done' class='d-none'><div class='alert alert-success'>Completed.</div>"
        "<a class='btn btn-primary' href='{{ url_for(env_slug ~ \".env_home\") }}'>Back</a></div>"

        "<div id='fail' class='d-none'><div class='alert alert-danger'>Failed. See details above.</div>"
        "<a class='btn btn-outline-secondary' href='{{ url_for(env_slug ~ \".env_home\") }}'>Back</a></div>"

        "</div></div>"
        "<script>(function(){const log=document.getElementById('log');"
        "window._append=s=>{const li=document.createElement('li');li.className='mb-1';li.textContent=s;log.appendChild(li);window.scrollTo(0,document.body.scrollHeight)};"
        "window._done=()=>document.getElementById('done').classList.remove('d-none');"
        "window._fail=()=>document.getElementById('fail').classList.remove('d-none');})();</script>"
        "{% endblock %}",
        env_slug=env_slug,
    )

    # --------------------------------------------------------
    # Everything else unchanged (streaming logic, API calls, etc.)
    # --------------------------------------------------------
    def step(s): return f"<script>_append('{html.escape(str(s))}')</script>\n"

    def _slugify(s: str) -> str:
        import re
        return re.sub(r"[^a-z0-9-]+", "-", (s or "").strip().lower()).strip("-")

    def _key(k): return f"{env_slug}:{k}"

    repo_cfg = session.get(_key("repository_settings"), {}) or {}
    proj_cfg = session.get(_key("project_settings"), {}) or {}
    common = session.get(_key("common"), {}) or {}
    pcw_cfg = session.get("github_workflow", {}) or {}
    full_yaml = session.get(_key("spec_yaml"))

    new_prefix_raw = (repo_cfg.get("new_prefix") or "").strip()
    repo_description = (repo_cfg.get("repo_description") or "").strip()
    owner = os.environ.get("PS_GITHUB_OWNER", (proj_cfg.get("workspace") or "")).strip()

    SPEC_FILE_REL = "spec/spec.yaml"
    SEED_REPO_DIR = current_app.config.get("SEED_REPO_DIR", "seed_repo")
    README_TPL = _p.join(SEED_REPO_DIR, "README.md.j2")
    WF_TPL_CANDIDATES = [
        _p.join(SEED_REPO_DIR, ".github", "workflows", "pc-deploy.yaml.j2"),
        _p.join(SEED_REPO_DIR, ".github", "workflows", "pc-deploy.yml.j2"),
    ]
    WF_TPL = next((p for p in WF_TPL_CANDIDATES if _p.exists(_p.join(current_app.root_path, p))), WF_TPL_CANDIDATES[-1])
    WF_OUT_PATH = ".github/workflows/pc-deploy.yaml" if WF_TPL.endswith(".yaml.j2") else ".github/workflows/pc-deploy.yml"

    slug_py = env_slug.replace("-", "_")
    script = f"environments/{slug_py}_setup.py"
    #script = (pcw_cfg.get("script") or "environments/multi_vpc_setup.py").strip()
    sit_script = None
    spec_file = (pcw_cfg.get("spec_file") or "env_templates/spec.yaml").strip()
    repo_path = (pcw_cfg.get("repo_path") or "${{ github.workspace }}/spec").strip()
    if current_app.config.get("RUNTIME_ENV_IS_PROD"):
        harbor_proj = DEFAULT_HARBOR_REGISTRY_PROJECT
        pc_version = DEFAULT_PC_VERSION
    else:
        harbor_proj = (pcw_cfg.get("harbor_registry_project") or DEFAULT_HARBOR_REGISTRY_PROJECT).strip()
        pc_version = (pcw_cfg.get("pc_version") or DEFAULT_PC_VERSION).strip()


    def _collect_seed_files():
        import os
        app_root = current_app.root_path
        seed_abs = _p.join(app_root, SEED_REPO_DIR)
        EXCLUDE_DIRS = {".git", ".svn", ".hg", "__pycache__"}
        EXCLUDE_FILES = {".DS_Store"}
        collected = []
        for root, dirs, files in os.walk(seed_abs):
            dirs[:] = [d for d in dirs if d not in EXCLUDE_DIRS]
            rel_dir = os.path.relpath(root, seed_abs)
            rel_dir = "" if rel_dir == "." else rel_dir
            for fname in sorted(files):
                # Skip templates; rendered versions are added separately below.
                if fname.endswith(".j2"): continue
                if fname in EXCLUDE_FILES: continue
                src_abs = _p.join(root, fname)
                repo_rel = _p.join(rel_dir, fname).replace("\\", "/") if rel_dir else fname
                with open(src_abs, "rb") as fh:
                    data = fh.read()
                try:
                    content = data.decode("utf-8")
                    encoding = "utf-8"
                except UnicodeDecodeError:
                    content = base64.b64encode(data).decode("ascii")
                    encoding = "base64"
                collected.append({
                    "path": repo_rel,
                    "mode": "100644",
                    "type": "blob",
                    "content": content,
                    "encoding": encoding
                })
        return collected

    def _materialize_binary_blobs(owner, repo, tree_items):
        materialized = []
        for item in tree_items:
            if item.get("encoding") != "base64":
                text_item = dict(item)
                text_item.pop("encoding", None)
                materialized.append(text_item)
                continue

            blob_json = create_git_blob(owner, repo, item["content"], encoding="base64")
            materialized.append({
                "path": item["path"],
                "mode": item["mode"],
                "type": item["type"],
                "sha": blob_json["sha"],
            })
        return materialized

    from jinja2 import Template

    def _render_seed_template(abs_path, ctx):
        with open(abs_path, "r", encoding="utf-8") as fh:
            tpl_text = fh.read()
        return Template(tpl_text).render(**ctx)

    def run():
        yield shell
        try:
            yield step("Validating inputs…")
            if not new_prefix_raw: raise RuntimeError("Missing repository_settings.new_prefix")
            if not owner: raise RuntimeError("Missing owner (project_settings.workspace or PS_GITHUB_OWNER)")
            if not full_yaml: raise RuntimeError("Spec YAML not found in session. Visit Finish first.")

            new_prefix = _slugify(new_prefix_raw)
            repo_slug = new_prefix

            yield step(f"Ensuring repository {owner}/{repo_slug} …")
            repo_json = get_repo(owner, repo_slug)
            if not repo_json:
                repo_json = create_repo(
                    owner, repo_slug,
                    description=repo_description,
                    private=current_app.config.get("GITHUB_DEFAULT_PRIVATE", True),
                )
                yield step("Repository created. Waiting for GitHub to initialize...")

                # Consistent Check: Wait for the default branch (usually 'main') to appear
                default_branch = repo_json.get("default_branch") or "main"
                if not wait_for_branch(owner, repo_slug, default_branch):
                    raise RuntimeError(f"GitHub failed to initialize branch '{default_branch}' within timeout.")
                if not wait_for_commit(owner, repo_slug, default_branch):
                    raise RuntimeError(f"GitHub commit tree not ready for branch '{default_branch}'.")
                yield step("GitHub initialization complete.")
            else:
                yield step("Repository exists.")
            ## Set custom property
            try:
                yield step("Setting custom property... ")
                set_repo_custom_properties(owner, repo_slug, {"tool": "praxis"})
                yield step("Custom property is set.")
            except RuntimeError as e:
                yield step(f"Warning: Failed to set custom property: {e}")
            default_branch = repo_json.get("default_branch") or current_app.config.get("PS_GITHUB_DEFAULT_BRANCH", "main")
            target_branch = default_branch

            yield step(f"Ensuring branch {target_branch} …")
            ensure_branch_from(owner, repo_slug, target_branch, default_branch)
            if not wait_for_branch(owner, repo_slug, target_branch):
                raise RuntimeError(f"GitHub failed to initialize branch '{target_branch}' within timeout.")
            if not wait_for_commit(owner, repo_slug, target_branch):
                raise RuntimeError(f"GitHub commit tree not ready for branch '{target_branch}'.")
            yield step("Branch ready.")

            yield step(f"Collecting {SEED_REPO_DIR} files …")
            files_to_commit = _collect_seed_files()
            yield step(f"Collected {len(files_to_commit)} seed files.")

            # Rendered spec.yaml
            files_to_commit.append({
                "path": SPEC_FILE_REL,
                "mode": "100644",
                "type": "blob",
                "content": full_yaml
            })

            wf_ctx = {
                "script": script,
                "sit_script": sit_script,
                "spec_file": spec_file,
                "repo_path": repo_path,
                "harbor_proj": harbor_proj,
                "harbor_registry_project": harbor_proj,   # optional alias
                "pc_version": pc_version,
            }

            yield step(f"Rendering {WF_OUT_PATH} …")
            pc_deploy_yaml = _render_seed_template(_p.join(current_app.root_path, WF_TPL), wf_ctx)
            files_to_commit.append({
                "path": WF_OUT_PATH,
                "mode": "100644",
                "type": "blob",
                "content": pc_deploy_yaml
            })

            docs_url = current_app.config.get("DOCS_URL") or ""
            readme_ctx = {
                "repo_slug": repo_slug, "owner": owner,
                "default_branch": default_branch, "target_branch": target_branch,
                "environment": (session.get(_key("common"), {}) or {}).get("environment", ""),
                "env_types": (session.get(_key("project_settings"), {}) or {}).get("environment_type") or [],
                "deployments": (session.get(_key("project_settings"), {}) or {}).get("deployments") or [],
                "aws_region": (session.get(_key("common"), {}) or {}).get("aws_region", ""),
                "domain_name": (session.get(_key("common"), {}) or {}).get("domain_name", ""),
                "spec_path": SPEC_FILE_REL, "workflow_path": WF_OUT_PATH,
                "docs_url": docs_url, "app_version": current_app.config.get("APP_VERSION") or "dev",
                "pc_version": pc_version,
            }
            yield step("Rendering README.md …")
            readme_md = _render_seed_template(_p.join(current_app.root_path, README_TPL), readme_ctx)
            files_to_commit.append({
                "path": "README.md",
                "mode": "100644",
                "type": "blob",
                "content": readme_md
            })

            yield step(f"Creating single commit on branch {target_branch} …")
            branch_info = get_branch(owner, repo_slug, target_branch)
            if not branch_info:
                raise RuntimeError(f"Could not fetch branch info for '{target_branch}'")

            parent_sha = branch_info["commit"]["sha"]
            base_tree_sha = branch_info["commit"]["commit"]["tree"]["sha"]

            yield step("Creating Git tree …")
            files_to_commit = _materialize_binary_blobs(owner, repo_slug, files_to_commit)
            tree_json = create_git_tree(owner, repo_slug, files_to_commit, base_tree_sha=base_tree_sha)
            new_tree_sha = tree_json["sha"]

            yield step("Creating Git commit …")
            commit_json = create_git_commit(owner, repo_slug, f"Initialize repository for {new_prefix}", new_tree_sha, [parent_sha])
            new_commit_sha = commit_json["sha"]

            yield step("Updating branch reference …")
            update_git_ref(owner, repo_slug, f"heads/{target_branch}", new_commit_sha)

            if target_branch != default_branch:
                yield step(f"Setting {target_branch} as the default branch…")
                update_repo(owner, repo_slug, default_branch=target_branch)
                yield step(f"Default branch is now {target_branch}.")

            yield step(f"Done → {owner}/{repo_slug}@{target_branch}")
            yield "<script>_done()</script>"
        except Exception as e:
            yield step(f"Error: {html.escape(str(e))}")
            yield "<script>_fail()</script>"

    resp = Response(stream_with_context(run()), mimetype="text/html")
    resp.headers["X-Accel-Buffering"] = "no"
    resp.headers["Cache-Control"] = "no-cache"
    return resp
