"""Read-only Terraform/OpenTofu metadata extraction from module archives."""
from __future__ import annotations

import json
import re
from io import BytesIO
from urllib.parse import quote, urlsplit, urlunsplit
from zipfile import ZipFile

from .zip_viewer import MAX_PREVIEW_BYTES, validate_zip_archive

_BLOCK_RE = re.compile(r'\b(variable|output|module)\s+"([^"]+)"\s*\{', re.MULTILINE)
_PROVIDER_RE = re.compile(r'\brequired_providers\s*\{', re.MULTILINE)
_PROVIDER_NAME_RE = re.compile(r"(?m)^\s*([A-Za-z0-9_-]+)\s*=")
_REQUIRED_VERSION_RE = re.compile(r'\brequired_version\s*=\s*"([^"]+)"')
_SOURCE_RE = re.compile(r'\bsource\s*=\s*"([^"]+)"')
_DEFAULT_RE = re.compile(r"(?m)^\s*default\s*=")
_SPACELIFT_MODULE_SOURCE_RE = re.compile(
    r"^spacelift\.io/"
    r"(?P<organization>[A-Za-z0-9_-]+)/"
    r"(?P<module>[A-Za-z0-9_-]+)/"
    r"(?P<provider>[A-Za-z0-9_-]+)$"
)


def _safe_repository_url(value: object) -> str:
    """Convert common Git remotes to credential-free browser URLs."""
    remote = str(value or "").strip()
    if not remote:
        return ""
    scp_style = re.fullmatch(r"(?:[^@/\s]+@)?([^:/\s]+):(.+)", remote)
    if scp_style and "://" not in remote:
        host, path = scp_style.groups()
        return f"https://{host}/{path.removesuffix('.git').lstrip('/')}"
    candidate = remote if "://" in remote else f"https://{remote}"
    try:
        parsed = urlsplit(candidate)
    except ValueError:
        return ""
    if parsed.scheme not in {"http", "https", "ssh", "git"} or not parsed.hostname:
        return ""
    path = parsed.path.removesuffix(".git")
    return urlunsplit(("https", parsed.hostname, path, "", ""))


def _repository_browser_url(repo_url: str, ref: str) -> str:
    if not repo_url:
        return ""
    parsed = urlsplit(repo_url)
    selected_ref = (ref or "main").strip()
    encoded_ref = quote(selected_ref, safe="")
    if parsed.hostname == "bitbucket.org":
        return f"{repo_url.rstrip('/')}/src/{encoded_ref}/"
    if parsed.hostname == "github.com" and selected_ref:
        return f"{repo_url.rstrip('/')}/tree/{encoded_ref}"
    return repo_url


def _repository_ssh_url(remote: str, repo_url: str) -> str:
    raw = (remote or "").strip()
    if re.fullmatch(r"[^@/\s]+@[^:/\s]+:.+", raw):
        return raw
    parsed = urlsplit(repo_url)
    if parsed.hostname in {"bitbucket.org", "github.com"} and parsed.path:
        path = parsed.path.strip("/")
        return f"git@{parsed.hostname}:{path}.git"
    return raw


def _contract_source(document: object) -> dict[str, str]:
    if not isinstance(document, dict):
        return {}
    wrapper = document.get("wrapper")
    source = wrapper.get("source") if isinstance(wrapper, dict) else None
    if not isinstance(source, dict):
        return {}
    repo = str(source.get("repo") or "").strip()
    ref = str(source.get("ref") or "").strip()
    repo_url = _safe_repository_url(repo)
    return {
        "type": str(source.get("type") or "").strip(),
        "repo": repo,
        "repo_url": repo_url,
        "browser_url": _repository_browser_url(repo_url, ref),
        "ssh_url": _repository_ssh_url(repo, repo_url),
        "ref": ref,
        "commit": str(source.get("commit") or "").strip(),
    }


def _module_source_url(source: str) -> str:
    """Convert a registry source into its Spacelift console module URL."""
    value = (source or "").strip().removeprefix("https://")
    match = _SPACELIFT_MODULE_SOURCE_RE.fullmatch(value)
    if not match:
        return ""
    organization = match.group("organization").lower()
    module_slug = f"terraform-{match.group('provider')}-{match.group('module')}"
    return f"https://{organization}.app.spacelift.io/module/{module_slug}"


def _block_body(text: str, start: int) -> str:
    depth = 0
    for index in range(start, len(text)):
        if text[index] == "{":
            depth += 1
        elif text[index] == "}":
            depth -= 1
            if depth == 0:
                return text[start + 1:index]
    return text[start + 1:]


def inspect_terraform_archive(data: bytes) -> dict[str, object]:
    safety = validate_zip_archive(data)
    providers: set[str] = set()
    required_versions: set[str] = set()
    variables: list[dict[str, object]] = []
    outputs: set[str] = set()
    modules: list[dict[str, str]] = []
    source: dict[str, str] = {}

    with ZipFile(BytesIO(data)) as archive:
        for info in archive.infolist():
            if info.is_dir():
                continue
            if info.filename.lower().endswith("contract.json") and not source:
                with archive.open(info) as handle:
                    raw_contract = handle.read()
                try:
                    source = _contract_source(json.loads(raw_contract.decode("utf-8")))
                except (UnicodeDecodeError, json.JSONDecodeError):
                    source = {}
            if not info.filename.lower().endswith((".tf", ".hcl")):
                continue
            with archive.open(info) as handle:
                raw = handle.read(MAX_PREVIEW_BYTES)
            text = raw.decode("utf-8", errors="replace")
            required_versions.update(_REQUIRED_VERSION_RE.findall(text))
            for provider_block in _PROVIDER_RE.finditer(text):
                body = _block_body(text, provider_block.end() - 1)
                providers.update(_PROVIDER_NAME_RE.findall(body))
            for match in _BLOCK_RE.finditer(text):
                kind, name = match.groups()
                body = _block_body(text, match.end() - 1)
                if kind == "variable":
                    variables.append({"name": name, "required": not bool(_DEFAULT_RE.search(body))})
                elif kind == "output":
                    outputs.add(name)
                else:
                    source_match = _SOURCE_RE.search(body)
                    module_source = source_match.group(1) if source_match else ""
                    modules.append(
                        {
                            "name": name,
                            "source": module_source,
                            "source_url": _module_source_url(module_source),
                        }
                    )

    variables.sort(key=lambda item: str(item["name"]))
    modules.sort(key=lambda item: item["name"])
    return {
        "providers": sorted(providers),
        "required_versions": sorted(required_versions),
        "variables": variables,
        "required_variables": sum(1 for item in variables if item["required"]),
        "optional_variables": sum(1 for item in variables if not item["required"]),
        "outputs": sorted(outputs),
        "modules": modules,
        "source": source,
        "safety": safety,
    }
