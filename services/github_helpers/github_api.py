# github_api.py
import base64
import json
import os
import time
from typing import Any, Dict, Optional, List
from urllib.parse import quote

import requests
from flask import current_app
from services.github_auth import get_github_app_auth


def _compute_api_base() -> str:
    """
    Resolves the GitHub REST API base.
    Priority:
      1) GITHUB_API (full base URL, e.g. https://api.github.com or https://ghe.example.com/api/v3)
      2) GITHUB_HOST (host only; builds https://<host>/api/v3)
      3) Public GitHub (https://api.github.com)
    """
    api = os.getenv("GITHUB_API")
    if api:
        return api.rstrip("/")
    host = os.getenv("GITHUB_HOST")
    if host:
        host = host.strip().rstrip("/")
        if host.startswith("http://") or host.startswith("https://"):
            return f"{host}/api/v3"
        return f"https://{host}/api/v3"
    return "https://api.github.com"


API = _compute_api_base()
HTTP_TIMEOUT = float(os.getenv("GITHUB_HTTP_TIMEOUT", "30"))  # seconds


def _headers(token: str, scheme: str = "Bearer") -> Dict[str, str]:
    """
    Builds Authorization headers
    """
    return {
        "Authorization": f"{scheme} {token}",
        "Accept": "application/vnd.github+json",
        "User-Agent": "praxis/1.0",
    }


def _api(path: str) -> str:
    return f"{API}{path}"



def _request(method: str, path: str, token: Optional[str] = None, **kw) -> requests.Response:
    """
    Issues a request to the GitHub API.

    If token is not provided, attempts to obtain one using get_token().

    Surfaces GitHub error messages with status, URL, and message body.
    """
    if not token:
        token = get_token()

    if not token:
        raise RuntimeError("GitHub authentication token is missing (no GitHub App configured)")

    url = _api(path)

    headers = kw.pop("headers", {})
    hdrs = _headers(token, "Bearer")
    hdrs.update(headers)

    try:
        resp = requests.request(method, url, headers=hdrs, timeout=HTTP_TIMEOUT, **kw)
    except requests.RequestException as e:
        raise RuntimeError(f"GitHub request error: {e}")

    return resp


def _raise_for_status(resp: requests.Response) -> None:
    """
    Raises a RuntimeError with a concise message including GitHub's error payload.
    """
    if 200 <= resp.status_code < 300:
        return

    detail = ""
    try:
        data = resp.json()

        # GitHub error responses usually include "message" and sometimes "errors"
        msg = data.get("message")
        errs = data.get("errors")

        if isinstance(errs, list) and errs:
            detail = f"{msg} :: {json.dumps(errs, ensure_ascii=False)}" if msg else json.dumps(errs, ensure_ascii=False)
        else:
            detail = msg or resp.text

    except Exception:
        detail = resp.text

    raise RuntimeError(f"GitHub API {resp.status_code} {resp.url} :: {detail}")


def get_repo(owner: str, repo: str, token: Optional[str] = None) -> Optional[Dict[str, Any]]:
    """
    Returns repository JSON or None if not found.
    """
    r = _request("GET", f"/repos/{owner}/{repo}", token)

    if r.status_code == 404:
        return None

    _raise_for_status(r)
    return r.json()


def create_repo(
        owner: str,
        repo: str,
        token: Optional[str] = None,
        description: str = "",
        private: bool = True,
        default_branch: Optional[str] = None,
) -> Dict[str, Any]:
    """
    Creates a repository under an organization.

    auto_init=True creates an initial commit on the default branch.
    """

    payload = {
        "name": repo,
        "description": description or "",
        "private": bool(private),
        "auto_init": True,
        "has_issues": True,
        "has_wiki": False,
    }

    if default_branch:
        payload["default_branch"] = default_branch

    r = _request("POST", f"/orgs/{owner}/repos", token, json=payload)

    _raise_for_status(r)
    return r.json()


def update_repo(owner: str, repo: str, token: Optional[str] = None, **settings) -> Dict[str, Any]:
    """
    Updates repository settings (e.g., default_branch, description, etc.)
    Example: update_repo(owner, repo, default_branch="main")
    """
    r = _request("PATCH", f"/repos/{owner}/{repo}", token, json=settings)
    _raise_for_status(r)
    return r.json()


def get_branch(owner: str, repo: str, branch: str, token: Optional[str] = None) -> Optional[Dict[str, Any]]:
    """
    Returns branch JSON or None if not found.
    """
    r = _request("GET", f"/repos/{owner}/{repo}/branches/{branch}", token)

    if r.status_code == 404:
        return None

    _raise_for_status(r)
    return r.json()


def wait_for_branch(owner: str, repo: str, branch: str, token: Optional[str] = None, timeout: int = 30) -> bool:
    """
    Polls GitHub until the specified branch exists or timeout is reached.
    """
    start_time = time.time()
    while time.time() - start_time < timeout:
        if get_branch(owner, repo, branch, token):
            return True
        time.sleep(2)
    return False

def wait_for_commit(owner: str, repo: str, branch: str, token: Optional[str] = None, timeout: int = 30) -> bool:
    """
    Waits until the branch HEAD commit SHA is available.
    This avoids GitHub API race conditions after repo creation.
    """
    start_time = time.time()

    while time.time() - start_time < timeout:
        branch_info = get_branch(owner, repo, branch, token)

        if branch_info and branch_info.get("commit", {}).get("sha"):
            return True

        time.sleep(2)

    return False

def ensure_branch_from(owner: str, repo: str, new_branch: str, from_branch: str, token: Optional[str] = None) -> None:
    """
    Ensures that 'new_branch' exists by creating it from 'from_branch' HEAD if missing.
    """
    if get_branch(owner, repo, new_branch, token):
        return

    base = get_branch(owner, repo, from_branch, token)

    if not base:
        raise RuntimeError(f"Base branch '{from_branch}' not found")

    sha = base["commit"]["sha"]

    r = _request(
        "POST",
        f"/repos/{owner}/{repo}/git/refs",
        token,
        json={"ref": f"refs/heads/{new_branch}", "sha": sha},
    )

    _raise_for_status(r)


def create_git_tree(
    owner: str,
    repo: str,
    tree_items: List[Dict[str, str]],
    base_tree_sha: Optional[str] = None,
    token: Optional[str] = None
) -> Dict[str, Any]:
    """
    Creates a new tree object for a repository.
    tree_items should be a list of objects like:
    {
        "path": "file.rb",
        "mode": "100644",
        "type": "blob",
        "content": "..."
    }
    """
    payload = {"tree": tree_items}
    if base_tree_sha:
        payload["base_tree"] = base_tree_sha

    r = _request("POST", f"/repos/{owner}/{repo}/git/trees", token, json=payload)
    _raise_for_status(r)
    return r.json()


def create_git_blob(
    owner: str,
    repo: str,
    content: str,
    encoding: str = "utf-8",
    token: Optional[str] = None
) -> Dict[str, Any]:
    """
    Creates a Git blob and returns its JSON payload.
    Use encoding="base64" for binary content.
    """
    payload = {"content": content, "encoding": encoding}
    r = _request("POST", f"/repos/{owner}/{repo}/git/blobs", token, json=payload)
    _raise_for_status(r)
    return r.json()


def create_git_commit(
    owner: str,
    repo: str,
    message: str,
    tree_sha: str,
    parent_shas: List[str],
    token: Optional[str] = None
) -> Dict[str, Any]:
    """
    Creates a new Git commit.
    """
    payload = {
        "message": message,
        "tree": tree_sha,
        "parents": parent_shas
    }
    r = _request("POST", f"/repos/{owner}/{repo}/git/commits", token, json=payload)
    _raise_for_status(r)
    return r.json()


def update_git_ref(
    owner: str,
    repo: str,
    ref: str,
    commit_sha: str,
    token: Optional[str] = None
) -> Dict[str, Any]:
    """
    Updates a Git reference (e.g., heads/main).
    """
    path_ref = ref
    if path_ref.startswith("refs/"):
        path_ref = path_ref[5:]

    payload = {"sha": commit_sha, "force": False}
    r = _request("PATCH", f"/repos/{owner}/{repo}/git/refs/{path_ref}", token, json=payload)
    _raise_for_status(r)
    return r.json()


# services/github_helpers/github_api.py

def get_token() -> Optional[str]:
    """
    Public helper to get a GitHub token.
    Leverages the auth service's internal caching.
    """
    try:
        gh_app = get_github_app_auth(current_app.config)
        if gh_app:
            return gh_app.get_token()
    except Exception as e:
        current_app.logger.exception("Failed to get GitHub App token")
        raise RuntimeError(f"Failed to get GitHub App token: {e}") from e
    return None


def upsert_file(
        owner: str,
        repo: str,
        path: str,
        content: str,
        message: str,
        token: Optional[str] = None,
        branch: str = "main",
        author: Optional[Dict[str, str]] = None,
        committer: Optional[Dict[str, str]] = None,
) -> Dict[str, Any]:
    """
    Creates or updates a file. Handles eventual consistency with a single retry on 409.
    """

    def _do_put(current_sha: Optional[str] = None):
        payload = {
            "message": message,
            "content": base64.b64encode(content.encode("utf-8")).decode("ascii"),
            "branch": branch,
        }
        if current_sha:
            payload["sha"] = current_sha
        if author: payload["author"] = author
        if committer: payload["committer"] = committer

        return _request("PUT", f"/repos/{owner}/{repo}/contents/{path}", token, json=payload)

    # 1. Get current state
    get_url = f"/repos/{owner}/{repo}/contents/{path}?ref={branch}"
    gr = _request("GET", get_url, token)

    sha = None
    if gr.status_code == 200:
        sha = gr.json().get("sha")
    elif gr.status_code != 404:
        _raise_for_status(gr)

    # 2. Attempt update
    pr = _do_put(sha)

    # 3. Standard Retry Strategy: Handle 409 Conflict once
    if pr.status_code == 409:
        time.sleep(2)  # Wait for GitHub state to settle

        # Re-fetch latest SHA
        gr_retry = _request("GET", get_url, token)
        if gr_retry.status_code == 200:
            sha = gr_retry.json().get("sha")
            pr = _do_put(sha)  # Final attempt

    _raise_for_status(pr)
    return pr.json()

# for dynamic list of whitelists
def list_directory(owner: str, repo: str, path: str, token: Optional[str] = None, ref: Optional[str] = None) -> List[
    str]:
    """
    Returns a sorted list of directory names under the given path using the
    GitHub Contents API.

    Example path: "SSA/bootstrap"
    """
    ref_qs = f"?ref={quote(ref)}" if ref else ""
    resp = _request("GET", f"/repos/{owner}/{repo}/contents/{path}{ref_qs}", token)
    _raise_for_status(resp)

    data = resp.json()
    if not isinstance(data, list):
        return []

    dirs = [
        item.get("name", "")
        for item in data
        if isinstance(item, dict) and item.get("type") == "dir"
    ]
    return sorted([d for d in dirs if d], key=str.lower)


def list_branches(owner: str, repo: str, token: Optional[str] = None) -> List[str]:
    """
    Returns a sorted list of branch names for a repository.
    """
    branches: List[str] = []
    page = 1
    per_page = 100
    while True:
        resp = _request("GET", f"/repos/{owner}/{repo}/branches?per_page={per_page}&page={page}", token)
        _raise_for_status(resp)
        data = resp.json()
        if not isinstance(data, list) or not data:
            break
        branches.extend(
            item.get("name", "")
            for item in data
            if isinstance(item, dict) and item.get("name")
        )
        if len(data) < per_page:
            break
        page += 1
    return sorted(set([b for b in branches if b]), key=str.lower)


def set_repo_custom_properties(owner: str, repo: str, properties: Dict[str, Any], token: Optional[str] = None) -> None:
    """
    Sets custom properties for a repository.
    """
    payload = {
        "properties": [
            {"property_name": k, "value": v} for k, v in properties.items()
        ]
    }
    path = f"/repos/{owner}/{repo}/properties/values"
    r = _request("PATCH", path, token, json=payload)

    _raise_for_status(r)
