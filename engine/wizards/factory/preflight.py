from flask import request


def _preflight_done(env_slug: str) -> bool:
    """Determine if the preflight phase is marked as done."""
    return request.args.get("preflight") == "ok"


def with_preflight_ok(url: str) -> str:
    """Append preflight flag to URLs where required."""
    sep = "&" if "?" in url else "?"
    return f"{url}{sep}preflight=ok"
