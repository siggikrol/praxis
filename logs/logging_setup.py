import json
import logging
import os
import sys
from datetime import datetime
from typing import Optional

_DEFAULT_LEVEL = os.getenv("LOG_LEVEL", "INFO").upper()
_JSON = os.getenv("LOG_JSON", "1") not in ("0", "false", "False")
_LOG_PATH = os.getenv("LOG_PATH")  # if set, file logging with rotation can be added later

class JsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        ts = datetime.utcfromtimestamp(record.created).isoformat(timespec="milliseconds") + "Z"
        payload = {
            "ts": ts,
            "level": record.levelname,
            "logger": record.name,
            "msg": record.getMessage(),
        }
        # Include extras if present
        for k in ("request_id", "user_id", "user_email", "method", "path", "status",
                  "duration_ms", "remote_addr", "endpoint", "blueprint", "user_agent",
                  "ncr_action", "ncr_endpoints_before", "ncr_endpoints_after",
                  "ncr_focus_index", "ncr_removed", "ncr_notice", "ncr_valid",
                  "ncr_errors", "redirect_to"):
            v = getattr(record, k, None)
            if v is not None:
                payload[k] = v
        if record.exc_info:
            payload["exc_type"] = record.exc_info[0].__name__ if record.exc_info[0] else None
            payload["exc_text"] = self.formatException(record.exc_info)
        return json.dumps(payload, ensure_ascii=False)

class PlainFormatter(logging.Formatter):
    default_time_format = "%Y-%m-%dT%H:%M:%S"
    default_msec_format = "%s.%03d"

def configure_logging(level: Optional[str] = None, json_logs: Optional[bool] = None) -> None:
    lvl = getattr(logging, (level or _DEFAULT_LEVEL).upper(), logging.INFO)
    as_json = _JSON if json_logs is None else json_logs

    root = logging.getLogger()
    # Avoid duplicate handlers if reconfigured
    for h in list(root.handlers):
        root.removeHandler(h)

    handler = logging.StreamHandler(sys.stdout)
    handler.setLevel(lvl)
    handler.setFormatter(JsonFormatter() if as_json else PlainFormatter(
        fmt="%(asctime)s %(levelname)s %(name)s: %(message)s",
        datefmt="%Y-%m-%dT%H:%M:%S"
    ))

    root.setLevel(lvl)
    root.addHandler(handler)

    # Reduce noise from werkzeug unless full trace is desired
    logging.getLogger("werkzeug").setLevel(max(lvl, logging.WARNING))
