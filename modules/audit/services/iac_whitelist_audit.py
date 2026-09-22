# envs/services/iac_whitelist_audit.py
from __future__ import annotations
import base64, json, os, re, runpy, shutil, subprocess, tempfile, urllib.request, urllib.error
from pathlib import Path
from typing import Dict, List, Mapping, Iterable, Set, TypedDict, Optional
import logging
from urllib.error import HTTPError, URLError

from services.github_helpers.github_api import get_token

log = logging.getLogger("praxis.iac")

def _redact(s: str | None) -> str:
    if not s:
        return ""
    s = re.sub(r'(?<=://)([^:@/]+)(?::[^@/]+)?@', r'\1:***@', s)
    s = re.sub(r'(Authorization:\s*)(Basic|Bearer)\s+[A-Za-z0-9+/=._\-]+', r'\1\2 ***', s)
    return s


MODULE_NAME_RE = re.compile(r'(?m)^\s*module\s+"(?P<name>[^"]+)"')

class AuditResult(TypedDict):
    discovered: Dict[str, List[str]]
    declared: Dict[str, List[str]]
    missing_buckets: List[str]
    extra_buckets: List[str]
    per_folder: Dict[str, Dict[str, List[str]]]

# ---------------------------- GitHub API export -----------------------------

def _gh_req_json(url: str, token: str) -> dict:
    req = urllib.request.Request(
        url,
        headers={
            "Accept": "application/vnd.github+json",
            "Authorization": f"Bearer {token}",
            "X-GitHub-Api-Version": "2022-11-28",
            "User-Agent": "iac-whitelist-auditor",
        },
    )
    with urllib.request.urlopen(req, timeout=30) as resp:
        return json.loads(resp.read().decode("utf-8"))

def _gh_get_raw(owner: str, repo: str, ref: str, path: str, token: str) -> bytes:
    raw_url = f"https://raw.githubusercontent.com/{owner}/{repo}/{ref}/{path}"
    req = urllib.request.Request(
        raw_url,
        headers={"Authorization": f"Bearer {token}", "User-Agent": "iac-whitelist-auditor"},
    )
    with urllib.request.urlopen(req, timeout=30) as resp:
        return resp.read()

def export_from_github_api(owner: str, repo: str, branch: str) -> Path:
    token = get_token()
    if not token:
        raise RuntimeError("GitHub authentication token is required for GitHub API export")

    tmp = Path(tempfile.mkdtemp(prefix="iac_audit_gh_"))
    try:
        repo_url = f"https://api.github.com/repos/{owner}/{repo}"
        meta = _gh_req_json(repo_url, token)
        default_branch = meta.get("default_branch") or "main"

        def _fetch_tree(ref: str) -> dict:
            url = f"https://api.github.com/repos/{owner}/{repo}/git/trees/{ref}?recursive=1"
            return _gh_req_json(url, token)

        tried = []
        try:
            tree = _fetch_tree(branch)
        except (HTTPError, URLError) as e1:
            tried.append((branch, str(e1)))
            if branch != default_branch:
                try:
                    tree = _fetch_tree(default_branch)
                except (HTTPError, URLError) as e2:
                    tried.append((default_branch, str(e2)))
                    raise RuntimeError(
                        "Failed to list repo tree.\n" + "\n".join(f"{ref}: {err}" for ref, err in tried)
                    )
            else:
                raise

        blobs = [t["path"] for t in tree.get("tree", []) if t.get("type") == "blob"]
        main_paths = [p for p in blobs if p.endswith("/main.tf") or p == "main.tf"]

        for rel in main_paths:
            contents_url = f"https://api.github.com/repos/{owner}/{repo}/contents/{rel}?ref={branch}"
            content_bytes = None
            try:
                meta = _gh_req_json(contents_url, token)
                data = meta.get("content")
                if data and meta.get("encoding") == "base64":
                    content_bytes = base64.b64decode(data)
            except (HTTPError, URLError):
                pass  # fallback to raw below

            if content_bytes is None:
                try:
                    content_bytes = _gh_get_raw(owner, repo, branch, rel, token)
                except (HTTPError, URLError):
                    if branch != default_branch:
                        content_bytes = _gh_get_raw(owner, repo, default_branch, rel, token)
                    else:
                        raise

            dest = tmp / rel
            dest.parent.mkdir(parents=True, exist_ok=True)
            dest.write_bytes(content_bytes)

        return tmp
    except Exception:
        shutil.rmtree(tmp, ignore_errors=True)
        raise

# ---------------------------- Git-based export (kept as-is; unused on distroless) -----------------------------

def _run(cmd: list[str], cwd: Path | None = None) -> subprocess.CompletedProcess:
    res = subprocess.run(cmd, cwd=cwd, check=False, capture_output=True, text=True)
    if res.returncode != 0:
        log.error("[_run] FAILED (%s)\nstdout:\n%s\nstderr:\n%s",
                  res.returncode, _redact(res.stdout), _redact(res.stderr))
        raise subprocess.CalledProcessError(res.returncode, cmd, output=res.stdout, stderr=res.stderr)
    return res


def export_from_git_url(git_url: str, branch: str) -> Path:
    """
    Clone via HTTPS using HTTP Basic with x-access-token:<PAT>,
    no credentials in URL, and shallow depth.
    """
    # If env provided an owner/repo but git_url is malformed, repair it
    owner = os.environ.get("PS_GITHUB_OWNER")
    repo  = os.environ.get("PS_GITHUB_REPO")
    if (not git_url) or re.match(r"^https://github\.com/[^/]+/[^/]+\.git$", git_url) is None:
        if owner and repo:
            fixed = f"https://github.com/{owner}/{repo}.git"
            log.warning("[export_from_git_url] git_url looked malformed (%s). Using %s",
                        _redact(git_url), _redact(fixed))
            git_url = fixed

    token = get_token()
    tmp = Path(tempfile.mkdtemp(prefix="iac_audit_giturl_"))

    # Build git command: HTTP Basic "x-access-token:<PAT/IAT>"
    if token:
        # Both PAT and IAT (Installation Access Token) work with x-access-token
        basic = base64.b64encode(f"x-access-token:{token}".encode("utf-8")).decode("ascii")
        cmd = [
            "git",
            "-c", f"http.extraHeader=Authorization: Basic {basic}",
            "-c", "credential.helper=",
            "-c", "core.askpass=",
            "-c", "transfer.fsckobjects=true",
            "clone", "--depth", "1", "--branch", branch, git_url, str(tmp),
        ]
        redacted_cmd = [
            "git",
            "-c", "http.extraHeader=Authorization: Basic ***",
            "-c", "credential.helper=",
            "-c", "core.askpass=",
            "clone", "--depth", "1", "--branch", branch, _redact(git_url), str(tmp),
        ]
    else:
        cmd = ["git", "clone", "--depth", "1", "--branch", branch, git_url, str(tmp)]
        redacted_cmd = ["git", "clone", "--depth", "1", "--branch", branch, _redact(git_url), str(tmp)]

    log.info("[export_from_git_url] Cloning (branch=%s): %s", branch, " ".join(redacted_cmd))
    res = subprocess.run(cmd, capture_output=True, text=True)
    if res.returncode != 0:
        err = _redact((res.stderr or "") + (res.stdout or ""))
        shutil.rmtree(tmp, ignore_errors=True)
        log.error("[_run] FAILED (%s)\n%s", res.returncode, err)
        raise RuntimeError(f"git clone failed: {err}")
    log.info("[export_from_git_url] Clone completed → %s", tmp)
    return tmp



def export_from_local_repo(repo: Path, branch: str) -> Path:
    repo = repo.resolve()
    if not (repo / ".git").exists():
        raise RuntimeError(f"Not a git repository: {repo}")
    tmp = Path(tempfile.mkdtemp(prefix="iac_audit_local_"))
    try:
        out = _run(["git", "-C", str(repo), "ls-tree", "-r", "--name-only", branch])
        files = [ln.strip() for ln in out.stdout.splitlines() if ln.strip()]
        for rel in files:
            dest = tmp / rel
            dest.parent.mkdir(parents=True, exist_ok=True)
            try:
                blob = _run(["git", "-C", str(repo), "show", f"{branch}:{rel}"]).stdout
            except subprocess.CalledProcessError:
                continue
            dest.write_text(blob, encoding="utf-8")
        return tmp
    except Exception:
        shutil.rmtree(tmp, ignore_errors=True)
        raise

# ---------------------------- Smart selector -----------------------------

def export_repo_to_tmp(
    repo: Optional[Path] = None,
    git_url: Optional[str] = None,
    branch: str = "main",
    gh_owner: Optional[str] = None,
    gh_repo: Optional[str] = None,
) -> Path:
    """Export the repository either using git clone (preferred) or GitHub API (fallback)."""

    git_present = shutil.which("git") is not None
    log.info(
        "[export_repo_to_tmp] git_present=%s, git_url=%s, gh_owner=%s, gh_repo=%s, branch=%s",
        git_present, _redact(git_url), gh_owner, gh_repo, branch
    )

    if git_present and (git_url or repo):
        log.info("[export_repo_to_tmp] Using GIT CLONE (--depth 1) for branch=%s url=%s",branch, _redact(git_url) if git_url else repo)
        return export_from_git_url(git_url, branch) if git_url else export_from_local_repo(repo, branch)

    if gh_owner and gh_repo:
        log.warning("[export_repo_to_tmp] Falling back to GitHub API for repo=%s/%s (branch=%s)", gh_owner, gh_repo, branch)
        return export_from_github_api(gh_owner, gh_repo, branch)

    log.error("[export_repo_to_tmp] Missing parameters. Provide git_url/repo or gh_owner/gh_repo.")
    raise RuntimeError("Provide either repo/git_url or (gh_owner & gh_repo) for GitHub API export.")

# ---------------------------- Core auditor funcs -----------------------------

def discover_folders_and_modules(iac_root: Path) -> Dict[str, List[str]]:
    out: Dict[str, List[str]] = {}
    for main in sorted(iac_root.rglob("main.tf")):
        folder = main.parent.name
        text = main.read_text(encoding="utf-8", errors="ignore")
        mods = [m.group("name") for m in MODULE_NAME_RE.finditer(text)]
        out[folder] = sorted(set(mods))
    return out

def load_buckets_from_py(py_path: Path, var_name: str = "IAC_WHITELIST_BUCKETS") -> Dict[str, List[str]]:
    ns = runpy.run_path(str(py_path))
    if var_name not in ns:
        raise KeyError(f"{var_name} not found in {py_path}")
    raw = ns[var_name]
    if not isinstance(raw, (dict, Mapping)):
        raise TypeError(f"{var_name} must be a Mapping[str, Iterable[str]]")
    return {k: sorted({str(x) for x in v}) for k, v in raw.items()}

def diff(discovered: Dict[str,List[str]], declared: Dict[str,List[str]]) -> AuditResult:
    folders_disk: Set[str] = set(discovered.keys())
    folders_decl: Set[str] = set(declared.keys())
    missing_buckets = sorted(folders_disk - folders_decl)
    extra_buckets   = sorted(folders_decl - folders_disk)
    per_folder: Dict[str, Dict[str, List[str]]] = {}
    for folder in sorted(folders_disk & folders_decl):
        set_disk = set(discovered[folder])
        set_decl = set(declared[folder])
        per_folder[folder] = {
            "missing": sorted(set_disk - set_decl),
            "extra":   sorted(set_decl - set_disk),
        }
    return {
        "discovered": discovered,
        "declared": declared,
        "missing_buckets": missing_buckets,
        "extra_buckets": extra_buckets,
        "per_folder": per_folder,
    }

def render_python_variable(mapping: Dict[str, List[str]], var_name: str = "IAC_WHITELIST_BUCKETS") -> str:
    lines = ['from typing import Mapping, Iterable', '', f'{var_name}: Mapping[str, Iterable[str]] = {{']
    for folder, mods in sorted(mapping.items()):
        items = ", ".join(f'"{m}"' for m in mods)
        lines.append(f'    "{folder}": [{items}],')
    lines.append("}")
    return "\n".join(lines)

def write_back_to_py_var(py_path: Path, new_mapping: Dict[str, List[str]], var_name: str = "IAC_WHITELIST_BUCKETS") -> None:
    src = py_path.read_text(encoding="utf-8")
    new_block = render_python_variable(new_mapping, var_name)
    import regex as rx
    pattern = rx.compile(rf'(?ms)^\s*{rx.escape(var_name)}\s*:\s*Mapping\[.*?\]\s*=\s*\{{.*?\}}\s*')
    if pattern.search(src):
        updated = pattern.sub(new_block, src)
    else:
        updated = src.rstrip() + "\n\n" + new_block + "\n"
    py_path.write_text(updated, encoding="utf-8")
