# envs/wizard/utils.py
from __future__ import annotations
import json
from urllib.parse import urlparse, parse_qsl, urlencode, urlunparse
from flask import current_app, session, request
from datetime import datetime
from typing import Any

def ns_key(env_slug: str, prefix: str) -> str:
    return f"{env_slug}:{prefix}"

def prune(obj: Any):
    if isinstance(obj, dict):
        return {k: prune(v) for k, v in obj.items() if k != "csrf_token"}
    if isinstance(obj, list):
        return [prune(x) for x in obj]
    return obj

def with_preflight_ok(url: str) -> str:
    if not url:
        return url
    p = urlparse(url)
    if p.netloc:
        return url
    q = dict(parse_qsl(p.query))
    q["preflight"] = "ok"
    return urlunparse(p._replace(query=urlencode(q, doseq=True)))

def log_wizard(env_slug: str, event: str, **fields) -> None:
    fields.setdefault("env", env_slug)
    fields.setdefault("ts", datetime.utcnow().isoformat())
    msg = "wizard_event " + " ".join(f"{k}={repr(v)}" for k, v in sorted(fields.items()))
    current_app.logger.info(msg)

def get_step_index(wizard_steps: list[tuple[str, Any]], step: str) -> int:
    for i, (k, _) in enumerate(wizard_steps):
        if k == step:
            return i
    return -1

def progress_steps(wizard_steps, exclude: set[str]) -> list[str]:
    return [slug for slug, _ in wizard_steps if slug not in exclude]

def parse_json_minified(s: str) -> str:
    if not isinstance(s, str): return s
    try:
        obj = json.loads(s)
        return json.dumps(obj, separators=(",", ":"))
    except Exception:
        return s.replace("\r", "").replace("\n", "")
