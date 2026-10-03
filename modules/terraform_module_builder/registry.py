"""Read-only, bounded access to public Registry metadata and versioned GitHub sources."""
import copy
import io
import posixpath
import re
import stat
import time
import zipfile
from urllib.parse import parse_qs, quote, urlparse

import requests
import hcl2

BASE = "https://registry.terraform.io/v1/modules"
IDENTIFIER = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]*/[A-Za-z0-9][A-Za-z0-9_-]*/(?:aws|google)$")
VERSION = re.compile(r"^\d+\.\d+\.\d+(?:[-+][A-Za-z0-9.-]+)?$")
_CACHE = {}


class RegistryError(ValueError):
    pass


def module_address(value):
    value = str(value or "").strip().rstrip("/")
    if value.startswith("https://registry.terraform.io/modules/"):
        parts = urlparse(value).path.split("/")
        value = "/".join(parts[2:5])
    if not IDENTIFIER.fullmatch(value):
        raise RegistryError("Choose an AWS or Google Cloud module using namespace/name/provider or its Registry URL.")
    return value


def version_number(value):
    if not VERSION.fullmatch(str(value or "")):
        raise RegistryError("Choose a published module version.")
    return value


def _get(url, *, params=None, limit=3_000_000):
    try:
        with requests.get(url, params=params, timeout=(5, 25), stream=True,
                          allow_redirects=False, headers={"User-Agent": "Praxis-terraform-module-builder/1.0"}) as response:
            if response.status_code == 429:
                raise RegistryError("The source is rate limiting requests. Please try again shortly.")
            if response.status_code not in (200, 204):
                raise RegistryError(f"The source returned HTTP {response.status_code}. Check the module and version, then try again.")
            chunks, size = [], 0
            for chunk in response.iter_content(65536):
                size += len(chunk)
                if size > limit:
                    raise RegistryError("This source exceeds the import size limit.")
                chunks.append(chunk)
            return b"".join(chunks), response.headers
    except requests.RequestException as exc:
        raise RegistryError("Cannot reach the public module source. Your saved drafts are still available; please retry.") from exc


def _json(path, params=None):
    import json
    cache_key = (path, tuple(sorted((params or {}).items())))
    cached = _CACHE.get(cache_key)
    if cached and cached[0] > time.monotonic():
        return copy.deepcopy(cached[1])
    body, _ = _get(BASE + path, params=params)
    try:
        data = json.loads(body)
    except (ValueError, UnicodeError) as exc:
        raise RegistryError("The Registry returned unreadable metadata.") from exc
    if not isinstance(data, dict) or data.get("errors"):
        raise RegistryError("The Registry could not find this module or version.")
    if len(_CACHE) >= 100:
        _CACHE.clear()
    _CACHE[cache_key] = (time.monotonic() + 300, data)
    return copy.deepcopy(data)


def search(query="", offset=0, provider="aws"):
    if provider not in ("aws", "google"):
        raise RegistryError("Choose AWS or Google Cloud.")
    params = {"provider": provider, "limit": 12, "offset": max(0, min(offset, 10000))}
    if query:
        params["q"] = query[:200]
    return _json("/search" if query else "", params)


def details(address, version=None):
    address = module_address(address)
    suffix = "/" + version_number(version) if version else ""
    data = _json("/" + address + suffix)
    if data.get("provider") not in ("aws", "google") or not isinstance(data.get("root"), dict):
        raise RegistryError("This module does not have usable AWS or Google Cloud metadata.")
    version_number(data.get("version"))
    data["address"] = address
    data["registry_url"] = f"https://registry.terraform.io/modules/{address}/{data['version']}"
    return data


def available_versions(address):
    """Return published versions newest-first and the Registry's latest release."""
    data = details(address)
    latest = version_number(data.get("version"))
    published = data.get("versions", [])
    if not isinstance(published, list):
        raise RegistryError("The Registry returned an unreadable version list.")
    versions = []
    for value in reversed(published):
        try:
            value = version_number(value)
        except RegistryError:
            continue
        if value not in versions:
            versions.append(value)
    if latest in versions:
        versions.remove(latest)
    versions.insert(0, latest)
    return versions, latest


def import_example(module, example_path):
    """Import text files only, never extract a downloaded archive onto disk."""
    if example_path not in {entry["path"] for entry in module.get("examples", [])}:
        raise RegistryError("Choose an example from the selected module version.")
    _, headers = _get(f"{BASE}/{module_address(module['address'])}/{version_number(module['version'])}/download")
    source = headers.get("X-Terraform-Get", "")
    parsed = urlparse(source.removeprefix("git::"))
    if parsed.scheme != "https" or parsed.hostname != "github.com" or parsed.port or parsed.username:
        raise RegistryError("Example import currently supports GitHub-hosted Registry modules. You can still create a wrapper.")
    match = re.fullmatch(r"/([\w.-]+)/([\w.-]+?)(?:\.git)?(?://(.+))?", parsed.path)
    ref = parse_qs(parsed.query).get("ref", [""])[0]
    if not match or not ref or not re.fullmatch(r"[\w./-]+", ref):
        raise RegistryError("The Registry did not provide a versioned GitHub source.")
    owner, repo, subdir = match.groups()
    package_root = posixpath.normpath(subdir or ".")
    example_root = posixpath.normpath(posixpath.join(package_root, example_path))
    if package_root.startswith("..") or example_root.startswith("..") or example_root.startswith("/"):
        raise RegistryError("Invalid example path.")
    body, _ = _get(f"https://codeload.github.com/{owner}/{repo}/zip/{quote(ref, safe='')}", limit=12_000_000)
    files, warnings, licenses = {}, [], {}
    try:
        archive = zipfile.ZipFile(io.BytesIO(body))
        entries = archive.infolist()
        if len(entries) > 5000 or sum(item.file_size for item in entries) > 32_000_000:
            raise RegistryError("The repository is too large to import as an example.")
        for item in entries:
            parts = item.filename.split("/")
            if ".." in parts or item.filename.startswith("/") or "\\" in item.filename:
                raise RegistryError("The repository contains an unsafe file path.")
            path = "/".join(parts[1:])
            relative = path[len(example_root)+1:] if path.startswith(example_root+"/") else ""
            license_file = "/" not in path and path.upper().startswith(("LICENSE", "NOTICE", "COPYING"))
            if item.is_dir() or not (relative or license_file):
                continue
            if stat.S_ISLNK(item.external_attr >> 16):
                warnings.append(f"Skipped symbolic link: {path}")
                continue
            if item.file_size > 1_000_000:
                raise RegistryError("An example file exceeds the 1 MB limit.")
            try:
                text = archive.read(item).decode("utf-8").replace("\r\n", "\n").replace("\r", "\n")
                if "\x00" in text:
                    raise UnicodeError()
            except UnicodeError:
                warnings.append(f"Skipped binary file: {path}")
                continue
            if license_file:
                licenses["UPSTREAM-" + path] = text
            elif relative:
                if relative in files:
                    raise RegistryError("The example contains duplicate paths.")
                if relative.endswith(".tf"):
                    # Local module references must survive moving the example out of its repository.
                    def rewrite(match):
                        target = posixpath.normpath(posixpath.join(posixpath.dirname(path), match[2]))
                        root_prefix = "" if package_root == "." else package_root + "/"
                        if target == example_root or target.startswith(example_root + "/"):
                            return match[0]
                        if target == package_root or (package_root == "." and target == "."):
                            suffix = ""
                        elif not target.startswith("..") and (not root_prefix or target.startswith(root_prefix)):
                            suffix = "//" + target[len(root_prefix):]
                        else:
                            warnings.append(f"{relative}: local module source {match[2]} needs adjustment.")
                            return match[0]
                        return f'{match[1]}"{module["address"]}{suffix}"\n  version = "{module["version"]}"'
                    # Only rewrite source attributes of module blocks, never resource file sources.
                    try:
                        tree = hcl2.parses(text)
                        replacements = []
                        for block in tree.find_data("block"):
                            if str(block.children[0].children[0]) != "module":
                                continue
                            block_body = next(c for c in block.children if getattr(c, "data", None) == "body")
                            for attr in block_body.children:
                                if getattr(attr, "data", None) == "attribute" and str(attr.children[0].children[0]) == "source":
                                    start, end = attr.meta.start_pos, attr.meta.end_pos
                                    replacement = re.sub(r'(\bsource\s*=\s*)"(\.\.?/[^"\n]*)"', rewrite, text[start:end])
                                    replacements.append((start, end, replacement))
                        for start, end, replacement in sorted(replacements, reverse=True):
                            text = text[:start] + replacement + text[end:]
                    except Exception as exc:
                        raise RegistryError(f"{relative}: could not parse the example's Terraform syntax.") from exc
                    if "../" in text or "./" in text:
                        warnings.append(f"{relative}: review remaining relative file references.")
                files[relative] = text
        files.update(licenses)
    except (zipfile.BadZipFile, RuntimeError, OSError) as exc:
        raise RegistryError("The versioned repository archive could not be read.") from exc
    if not any(name.endswith((".tf", ".tf.json")) for name in files):
        raise RegistryError("No Terraform files were found in this example.")
    warnings.append("Review the example's providers, additional resources, and existing values before using it.")
    return files, list(dict.fromkeys(warnings)), {"download_source": source, "revision": ref, "example_path": example_path}
