# envs/wizard/render.py
from __future__ import annotations
from flask import current_app

def template_exists(name: str) -> bool:
    try:
        current_app.jinja_loader.get_source(current_app.jinja_env, name)
        return True
    except Exception:
        return False
