from __future__ import annotations

import html
import os
import re
from dataclasses import dataclass
from typing import Any
from urllib.parse import urljoin

import requests


class ConfluenceError(RuntimeError):
    pass


@dataclass(frozen=True)
class ConfluenceConfig:
    base_url: str
    space_key: str
    root_page_id: str
    user_email: str
    api_token: str

    @property
    def missing_fields(self) -> list[str]:
        required = {
            "PS_SPEC_WORKBENCH_CONFLUENCE_BASE_URL": self.base_url,
            "PS_SPEC_WORKBENCH_CONFLUENCE_SPACE_KEY": self.space_key,
            "PS_SPEC_WORKBENCH_CONFLUENCE_USER_EMAIL": self.user_email,
            "PS_SPEC_WORKBENCH_CONFLUENCE_API_TOKEN": self.api_token,
        }
        return [name for name, value in required.items() if not str(value or "").strip()]

    @property
    def enabled(self) -> bool:
        return not self.missing_fields

    @property
    def reason(self) -> str:
        if self.enabled:
            return ""
        return "Missing Confluence config: " + ", ".join(self.missing_fields)

    def as_dict(self) -> dict[str, Any]:
        return {
            "base_url": self.base_url,
            "space_key": self.space_key,
            "root_page_id": self.root_page_id,
            "enabled": self.enabled,
            "reason": self.reason,
            "missing_fields": self.missing_fields,
        }


def load_config() -> ConfluenceConfig:
    return ConfluenceConfig(
        base_url=(os.getenv("PS_SPEC_WORKBENCH_CONFLUENCE_BASE_URL") or "").strip().rstrip("/"),
        space_key=(os.getenv("PS_SPEC_WORKBENCH_CONFLUENCE_SPACE_KEY") or "").strip(),
        root_page_id=(os.getenv("PS_SPEC_WORKBENCH_CONFLUENCE_ROOT_PAGE_ID") or "").strip(),
        user_email=(os.getenv("PS_SPEC_WORKBENCH_CONFLUENCE_USER_EMAIL") or "").strip(),
        api_token=(os.getenv("PS_SPEC_WORKBENCH_CONFLUENCE_API_TOKEN") or "").strip(),
    )


def _slugify_title(title: str) -> str:
    slug = re.sub(r"[^a-z0-9]+", "-", (title or "").strip().lower()).strip("-")
    return slug or "spec"


def _page_web_url(base_url: str, payload: dict[str, Any]) -> str:
    links = payload.get("_links")
    if not isinstance(links, dict):
        return ""
    webui = str(links.get("webui") or "").strip()
    if not webui:
        return ""
    if webui.startswith("http://") or webui.startswith("https://"):
        return webui
    return urljoin(base_url.rstrip("/") + "/", webui.lstrip("/"))


def _render_yaml_block(spec_yaml: str) -> str:
    escaped = html.escape(spec_yaml or "")
    return f"<pre><code>{escaped}</code></pre>"


def _render_metadata_table(rows: list[tuple[str, str]]) -> str:
    table_rows = []
    for label, value in rows:
        table_rows.append(
            "<tr>"
            f"<th><p>{html.escape(label)}</p></th>"
            f"<td><p>{html.escape(value)}</p></td>"
            "</tr>"
        )
    return "<table><tbody>" + "".join(table_rows) + "</tbody></table>"


def _architecture_metadata_rows(draft: dict[str, Any]) -> list[tuple[str, str]]:
    metadata = draft.get("architecture_metadata") or {}
    labels = [
        ("architect", "Architect"),
        ("customer", "Customer"),
        ("environments", "Target environments"),
        ("purpose", "POC / architecture purpose"),
        ("status", "Decision status"),
        ("ticket", "Related ticket"),
    ]
    rows = [("Workbench mode", "Environment prerequisite" if draft.get("mode") == "prerequisite" else "Playground")]
    rows.extend((label, str(metadata.get(key) or "-")) for key, label in labels)
    if draft.get("source_template_key"):
        rows.append(("Starter template", str(draft.get("source_template_key"))))
    if draft.get("source_draft_id"):
        rows.append(("Forked from draft", str(draft.get("source_draft_id"))))
    return rows


def _render_prerequisite_report(draft: dict[str, Any], validation: dict[str, Any]) -> str:
    yaml_text = str(validation.get("spec_yaml") or "")
    unresolved = len(set(re.findall(r"__[A-Z0-9_]+__", yaml_text)))
    rows = _architecture_metadata_rows(draft) + [
        ("YAML syntax", "Passed"),
        ("Praxis Core validation", "Passed"),
        ("Validated content hash", str(validation.get("content_hash") or "")),
        ("Unresolved placeholders", str(unresolved)),
    ]
    return "<h2>Environment Prerequisite Report</h2>" + _render_metadata_table(rows)


def _render_parent_page_body(
    *,
    draft: dict[str, Any],
    validation: dict[str, Any],
    revision_number: int,
    published_by: str,
) -> str:
    metadata = _render_metadata_table(
        [
            ("Status", "Published from Praxis"),
            ("Latest revision", f"Rev {revision_number:03d}"),
            ("Published by", published_by),
            ("Validated hash", str(validation.get("content_hash") or "")),
            ("Harbor project", str(validation.get("harbor_registry_project") or "")),
            ("Praxis Core version", str(validation.get("pc_version") or "")),
        ]
    )
    note = (
        "<p><em>"
        "This page is managed by Praxis. Child pages are immutable published revisions."
        "</em></p>"
    )
    return (
        f"<h1>{html.escape(str(draft.get('title') or 'Untitled Spec'))}</h1>"
        f"{_render_prerequisite_report(draft, validation)}"
        "<h2>Publication Metadata</h2>"
        f"{metadata}"
        "<h2>Current Editable Spec</h2>"
        f"{_render_yaml_block(str(draft.get('working_yaml') or ''))}"
        f"{note}"
    )


def _render_revision_page_body(
    *,
    draft: dict[str, Any],
    validation: dict[str, Any],
    revision_number: int,
    published_by: str,
) -> str:
    metadata = _render_metadata_table(
        [
            ("Spec", str(draft.get("title") or "")),
            ("Revision", f"Rev {revision_number:03d}"),
            ("Published by", published_by),
            ("Validated hash", str(validation.get("content_hash") or "")),
            ("Harbor project", str(validation.get("harbor_registry_project") or "")),
            ("Praxis Core version", str(validation.get("pc_version") or "")),
            ("Validation job", str(validation.get("job_id") or "")),
        ]
    )
    return (
        f"<h1>{html.escape(str(draft.get('title') or 'Untitled Spec'))} - Rev {revision_number:03d}</h1>"
        f"{_render_prerequisite_report(draft, validation)}"
        "<h2>Revision Metadata</h2>"
        f"{metadata}"
        "<h2>YAML Snapshot</h2>"
        f"{_render_yaml_block(str(validation.get('spec_yaml') or ''))}"
    )


def _safe_json(resp: requests.Response) -> Any:
    try:
        return resp.json()
    except Exception:
        return None


def _raise_for_status(resp: requests.Response) -> None:
    if 200 <= resp.status_code < 300:
        return
    payload = _safe_json(resp)
    detail = ""
    if isinstance(payload, dict):
        detail = str(
            payload.get("message")
            or payload.get("error")
            or payload.get("reason")
            or payload
        )
    elif payload is not None:
        detail = str(payload)
    else:
        detail = resp.text.strip()
    raise ConfluenceError(f"Confluence API {resp.status_code}: {detail or 'request failed'}")


class ConfluenceClient:
    def __init__(self, config: ConfluenceConfig, *, timeout: float = 20.0) -> None:
        self.config = config
        self.timeout = timeout
        self.session = requests.Session()
        self.session.auth = (config.user_email, config.api_token)
        self.session.headers.update(
            {
                "Accept": "application/json",
                "User-Agent": "praxis/spec-workbench",
            }
        )

    def _api_url(self, path: str) -> str:
        return f"{self.config.base_url}{path}"

    def _request(self, method: str, path: str, **kwargs) -> requests.Response:
        try:
            resp = self.session.request(
                method,
                self._api_url(path),
                timeout=self.timeout,
                **kwargs,
            )
        except requests.RequestException as exc:
            raise ConfluenceError(f"Confluence request error: {exc}") from exc
        _raise_for_status(resp)
        return resp

    def get_space(self) -> dict[str, Any]:
        resp = self._request(
            "GET",
            "/wiki/api/v2/spaces",
            params={"keys": self.config.space_key},
        )
        payload = _safe_json(resp)
        results = payload.get("results") if isinstance(payload, dict) else None
        if not isinstance(results, list) or not results:
            raise ConfluenceError(f"Confluence space not found for key {self.config.space_key!r}")
        space = results[0]
        if not isinstance(space, dict):
            raise ConfluenceError("Invalid Confluence space response")
        return space

    def get_page(self, page_id: str) -> dict[str, Any]:
        resp = self._request("GET", f"/wiki/api/v2/pages/{page_id}")
        payload = _safe_json(resp)
        if not isinstance(payload, dict):
            raise ConfluenceError("Invalid Confluence page response")
        return payload

    def create_page(
        self,
        *,
        space_id: str,
        parent_id: str,
        title: str,
        body_html: str,
    ) -> dict[str, Any]:
        resp = self._request(
            "POST",
            "/wiki/api/v2/pages",
            json={
                "spaceId": str(space_id),
                "status": "current",
                "title": title,
                "parentId": str(parent_id),
                "body": {
                    "representation": "storage",
                    "value": body_html,
                },
            },
            headers={"Content-Type": "application/json"},
        )
        payload = _safe_json(resp)
        if not isinstance(payload, dict):
            raise ConfluenceError("Invalid create-page response")
        return payload

    def update_page(
        self,
        *,
        page_id: str,
        title: str,
        body_html: str,
        version_number: int,
        version_message: str,
    ) -> dict[str, Any]:
        resp = self._request(
            "PUT",
            f"/wiki/api/v2/pages/{page_id}",
            json={
                "id": str(page_id),
                "status": "current",
                "title": title,
                "body": {
                    "representation": "storage",
                    "value": body_html,
                },
                "version": {
                    "number": int(version_number),
                    "message": version_message,
                },
            },
            headers={"Content-Type": "application/json"},
        )
        payload = _safe_json(resp)
        if not isinstance(payload, dict):
            raise ConfluenceError("Invalid update-page response")
        return payload

    def upload_attachment(
        self,
        *,
        page_id: str,
        filename: str,
        content: bytes,
        comment: str,
    ) -> dict[str, Any]:
        headers = {"X-Atlassian-Token": "nocheck"}
        resp = self._request(
            "PUT",
            f"/wiki/rest/api/content/{page_id}/child/attachment",
            headers=headers,
            files={"file": (filename, content, "application/x-yaml")},
            data={"comment": comment},
        )
        payload = _safe_json(resp)
        if not isinstance(payload, dict):
            raise ConfluenceError("Invalid attachment response")
        return payload


def publish_revision(
    *,
    draft: dict[str, Any],
    validation: dict[str, Any],
    revision_number: int,
    published_by: str,
) -> dict[str, str]:
    config = load_config()
    if not config.enabled:
        raise ConfluenceError(config.reason or "Confluence is not configured")

    client = ConfluenceClient(config)
    space = client.get_space()
    space_id = str(space.get("id") or "").strip()
    if not space_id:
        raise ConfluenceError("Confluence space id missing from space lookup")

    parent_root_id = config.root_page_id or str(space.get("homepageId") or "").strip()
    if not parent_root_id:
        raise ConfluenceError(
            "Missing Confluence root page id and space homepage id was not returned"
        )

    parent_body = _render_parent_page_body(
        draft=draft,
        validation=validation,
        revision_number=revision_number,
        published_by=published_by,
    )

    parent_title = str(draft.get("title") or "Untitled Spec").strip() or "Untitled Spec"
    parent_page_id = str(draft.get("confluence_parent_page_id") or "").strip()
    parent_page: dict[str, Any]

    if parent_page_id:
        try:
            current_page = client.get_page(parent_page_id)
        except ConfluenceError as exc:
            if "Confluence API 404:" not in str(exc):
                raise
            current_page = {}
        if current_page:
            version = current_page.get("version")
            current_version = 1
            if isinstance(version, dict):
                current_version = int(version.get("number") or 1)
            parent_page = client.update_page(
                page_id=parent_page_id,
                title=parent_title,
                body_html=parent_body,
                version_number=current_version + 1,
                version_message=f"Published Rev {revision_number:03d}",
            )
        else:
            parent_page = client.create_page(
                space_id=space_id,
                parent_id=parent_root_id,
                title=parent_title,
                body_html=parent_body,
            )
    else:
        parent_page = client.create_page(
            space_id=space_id,
            parent_id=parent_root_id,
            title=parent_title,
            body_html=parent_body,
        )

    parent_id = str(parent_page.get("id") or "").strip()
    if not parent_id:
        raise ConfluenceError("Confluence parent page id missing after publish")

    revision_title = f"{parent_title} - Rev {revision_number:03d}"
    revision_body = _render_revision_page_body(
        draft=draft,
        validation=validation,
        revision_number=revision_number,
        published_by=published_by,
    )
    revision_page = client.create_page(
        space_id=space_id,
        parent_id=parent_id,
        title=revision_title,
        body_html=revision_body,
    )
    revision_page_id = str(revision_page.get("id") or "").strip()
    if not revision_page_id:
        raise ConfluenceError("Confluence revision page id missing after publish")

    spec_yaml = str(validation.get("spec_yaml") or "")
    slug = _slugify_title(parent_title)
    client.upload_attachment(
        page_id=parent_id,
        filename=f"{slug}-latest.yaml",
        content=spec_yaml.encode("utf-8"),
        comment=f"Latest published spec from Rev {revision_number:03d}",
    )
    client.upload_attachment(
        page_id=revision_page_id,
        filename=f"{slug}-rev-{revision_number:03d}.yaml",
        content=spec_yaml.encode("utf-8"),
        comment=f"Immutable spec snapshot for Rev {revision_number:03d}",
    )

    return {
        "confluence_parent_page_id": parent_id,
        "confluence_parent_page_url": _page_web_url(config.base_url, parent_page),
        "confluence_revision_page_id": revision_page_id,
        "confluence_revision_page_url": _page_web_url(config.base_url, revision_page),
    }
