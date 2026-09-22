import logging
import time
import uuid
from flask import g, request, session

log = logging.getLogger("app.request")

# Headers commonly used for propagated request ids
_REQ_ID_HEADERS = ("X-Request-Id", "X-Request-ID", "X-Correlation-Id", "X-Amzn-Trace-Id")

def _read_request_id() -> str:
    for h in _REQ_ID_HEADERS:
        v = request.headers.get(h)
        if v:
            return v.strip()
    return uuid.uuid4().hex

def init_request_logging(app):
    @app.before_request
    def _start_timer():
        g._start = time.perf_counter()
        g.request_id = _read_request_id()

    @app.after_request
    def _access_log(response):
        try:
            duration = int((time.perf_counter() - getattr(g, "_start", time.perf_counter())) * 1000)
            ua = request.headers.get("User-Agent", "")
            # Keep UA short to avoid bloating logs
            ua = (ua[:180] + "…") if len(ua) > 180 else ua
            user = session.get("user") if session else None
            extra = {
                "request_id": getattr(g, "request_id", None),
                "method": request.method,
                "path": request.path,
                "status": response.status_code,
                "duration_ms": duration,
                "remote_addr": request.headers.get("X-Forwarded-For", request.remote_addr),
                "endpoint": request.endpoint,
                "blueprint": request.blueprint,
                "user_agent": ua,
                "user": user,
                "user_id": user,   # surface in JSON logs via logging formatter
            }
            log.info("access", extra=extra)
            # Echo request id back to clients to aid correlation
            response.headers["X-Request-Id"] = extra["request_id"]
        finally:
            return response

    @app.errorhandler(Exception)
    def _log_exception(err):
        # Let Flask render the error, but log with stack
        log.exception(
            "unhandled_exception",
            extra={
                "request_id": getattr(g, "request_id", None),
                "user": session.get("user") if session else None,
                "user_id": session.get("user") if session else None,
            },
        )
        return err, getattr(err, "code", 500)
