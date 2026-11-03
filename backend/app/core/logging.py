"""Structured (JSON) logging with correlation fields, redaction and a bounded in-memory ring for the operations workspace.

Log records never contain file contents or secrets: extras pass through key-based and pattern-based redaction and every field is length-bounded.
"""
from __future__ import annotations

import json
import logging
import sys
from collections import deque
from datetime import UTC, datetime
from typing import Any

from app.observability import context as ctx
from app.observability.redact import SENSITIVE_KEY, clean, clean_str

_STANDARD = set(logging.LogRecord("", 0, "", 0, "", (), None).__dict__) | {"message", "asctime"}
_SETTINGS: dict[str, Any] = {"service": "nova-api", "environment": "development", "field_max": 1000}
LOG_RING: deque[dict[str, Any]] = deque(maxlen=2000)


def build_payload(record: logging.LogRecord, *, stack: bool = True) -> dict[str, Any]:
    msg = record.getMessage()
    payload: dict[str, Any] = {
        "ts": datetime.fromtimestamp(record.created, UTC).isoformat(),
        "level": record.levelname,
        "logger": record.name,
        "event": clean_str(str(record.msg) if isinstance(record.msg, str) and " " not in record.msg else msg, 120),
        "message": clean_str(msg, _SETTINGS["field_max"]),
        "service": _SETTINGS["service"],
        "environment": _SETTINGS["environment"],
    }
    for key, var in (("request_id", ctx.REQUEST_ID), ("trace_id", ctx.TRACE_ID), ("span_id", ctx.SPAN_ID), ("job_id", ctx.JOB_ID), ("job_type", ctx.JOB_TYPE)):
        if (val := var.get()) is not None:
            payload[key] = val
    for key, value in record.__dict__.items():
        if key in _STANDARD or key.startswith("_"):
            continue
        payload[key] = "[REDACTED]" if SENSITIVE_KEY.search(key) else clean(value, _SETTINGS["field_max"])
    if "ms" in payload and "duration_ms" not in payload:
        payload["duration_ms"] = payload["ms"]
    if record.exc_info and record.exc_info[0]:
        payload["error_class"] = payload.get("error_class") or record.exc_info[0].__name__
        payload["exc_type"] = record.exc_info[0].__name__
        if stack:  # stack traces stay in the internal log stream; the operations API never serves them
            payload["stack"] = clean_str(logging.Formatter().formatException(record.exc_info), 4000)
    elif "error_type" in payload and "error_class" not in payload:
        payload["error_class"] = payload["error_type"]
    return payload


class JsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        return json.dumps(build_payload(record), default=str)


class RingHandler(logging.Handler):
    """Keeps the most recent redacted records for the investigation view. Bounded; lost on restart."""

    def emit(self, record: logging.LogRecord) -> None:
        try:
            LOG_RING.append(build_payload(record, stack=False))
        except Exception:  # noqa: BLE001, S110 - logging must never raise into the application
            pass


def configure_logging(level: str = "INFO", as_json: bool = True, *, service: str = "nova-api", environment: str = "development", ring_size: int = 2000, field_max: int = 1000) -> None:
    global LOG_RING
    _SETTINGS.update(service=service, environment=environment, field_max=field_max)
    if LOG_RING.maxlen != ring_size:
        LOG_RING = deque(LOG_RING, maxlen=ring_size)
    handler = logging.StreamHandler(sys.stderr)
    handler.setFormatter(JsonFormatter() if as_json else logging.Formatter("%(levelname)s %(name)s: %(message)s"))
    root = logging.getLogger()
    root.handlers[:] = [handler, RingHandler()]
    root.setLevel(level.upper())
    # /ready inspects the schema revision on every probe; alembic would log two INFO lines each time. Migrations run in the release CLI, not here.
    logging.getLogger("alembic.runtime.migration").setLevel(logging.WARNING)
