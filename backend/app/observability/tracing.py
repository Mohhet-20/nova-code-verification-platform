"""Minimal W3C-compatible tracer: sampled spans in a bounded ring, optional fail-safe exporter. Attributes are allow-listed and scrubbed."""
from __future__ import annotations

import logging
import secrets
import threading
import time
from collections import deque
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

from app.observability import context as ctx
from app.observability.redact import clean_str

log = logging.getLogger("nova.tracing")
CONFIG: dict[str, Any] = {"sample_ratio": 1.0, "ring_size": 2000, "exporter": None}
SPANS: deque[dict[str, Any]] = deque(maxlen=2000)
_EXPORT_Q: deque[dict[str, Any]] = deque(maxlen=1000)
_LOCK = threading.Lock()
ALLOWED_ATTRS = {"http.method", "http.route", "http.status_code", "job.kind", "job.id", "job.outcome", "job.retries", "error.class", "workflow", "component", "db.system", "result", "count", "parent.request_id", "queue.delay_ms"}
MAX_ATTRS = 16


def configure(sample_ratio: float = 1.0, ring_size: int = 2000, exporter: Any = None) -> None:
    global SPANS
    CONFIG.update(sample_ratio=max(0.0, min(1.0, sample_ratio)), ring_size=ring_size, exporter=exporter)
    if SPANS.maxlen != ring_size:
        SPANS = deque(SPANS, maxlen=ring_size)


def _sampled(trace_id: str, parent_sampled: bool | None) -> bool:
    ratio = CONFIG["sample_ratio"]
    if parent_sampled is False or ratio <= 0:
        return False
    return ratio >= 1.0 or int(trace_id[-8:], 16) / 0xFFFFFFFF < ratio


@dataclass
class Span:
    name: str
    trace_id: str
    span_id: str
    parent_id: str | None
    sampled: bool
    start: float = field(default_factory=time.time)
    attrs: dict[str, Any] = field(default_factory=dict)
    status: str = "ok"
    _t0: float = field(default_factory=time.perf_counter)

    def set(self, key: str, value: Any) -> None:
        if key in ALLOWED_ATTRS and len(self.attrs) < MAX_ATTRS:
            self.attrs[key] = clean_str(value, 200) if isinstance(value, str) else value

    def error(self, exc: BaseException) -> None:
        self.status = "error"
        self.set("error.class", type(exc).__name__)


@contextmanager
def span(name: str, *, remote_parent: tuple[str, str, bool] | None = None, **attrs: Any) -> Iterator[Span]:
    """Open a span. `remote_parent` (an incoming traceparent) only links trace ids for correlation; it is never an identity."""
    cur_trace, cur_span = ctx.TRACE_ID.get(), ctx.SPAN_ID.get()
    if cur_trace:
        trace_id, parent = cur_trace, cur_span
        sampled = True
    elif remote_parent:
        trace_id, parent, sampled = remote_parent[0], remote_parent[1], remote_parent[2]
    else:
        trace_id, parent, sampled = secrets.token_hex(16), None, True
    if not cur_trace:
        sampled = _sampled(trace_id, remote_parent[2] if remote_parent else None)
    sp = Span(name[:80], trace_id, secrets.token_hex(8), parent, sampled)
    for k, v in attrs.items():
        sp.set(k.replace("_", ".", 1) if k not in ALLOWED_ATTRS else k, v)
    t1, t2 = ctx.TRACE_ID.set(trace_id), ctx.SPAN_ID.set(sp.span_id)
    try:
        yield sp
    except BaseException as exc:
        sp.error(exc)
        raise
    finally:
        ctx.TRACE_ID.reset(t1)
        ctx.SPAN_ID.reset(t2)
        try:
            _finish(sp)
        except Exception:  # noqa: BLE001 - telemetry must never mask the original exception
            log.debug("span finish failed", exc_info=False)


def _finish(sp: Span) -> None:
    if not sp.sampled:
        return
    rec = {
        "trace_id": sp.trace_id, "span_id": sp.span_id, "parent_id": sp.parent_id, "name": sp.name,
        "start": datetime.fromtimestamp(sp.start, UTC).isoformat(), "duration_ms": round((time.perf_counter() - sp._t0) * 1000, 3),
        "status": sp.status, "attributes": sp.attrs, "request_id": ctx.REQUEST_ID.get(),
    }
    with _LOCK:
        if len(SPANS) == SPANS.maxlen:
            from app.observability.metrics import TELEMETRY_DROPPED
            TELEMETRY_DROPPED.inc(signal="spans")
        SPANS.append(rec)
        if CONFIG["exporter"] is not None:
            _EXPORT_Q.append(rec)


def flush_exporter() -> dict[str, Any]:
    """Hand buffered spans to the optional exporter callable. Never raises; failures are counted and the batch is dropped (bounded)."""
    exp = CONFIG["exporter"]
    if exp is None:
        return {"enabled": False, "exported": 0, "failed": 0}
    with _LOCK:
        batch = list(_EXPORT_Q)
        _EXPORT_Q.clear()
    if not batch:
        return {"enabled": True, "exported": 0, "failed": 0}
    try:
        exp(batch)
        return {"enabled": True, "exported": len(batch), "failed": 0}
    except Exception as exc:  # noqa: BLE001
        from app.observability.metrics import TELEMETRY_DROPPED
        TELEMETRY_DROPPED.inc(len(batch), signal="export")
        log.warning("span_export_failed", extra={"error_class": type(exc).__name__})
        return {"enabled": True, "exported": 0, "failed": len(batch)}


def recent(limit: int = 100, trace_id: str | None = None, request_id: str | None = None) -> list[dict[str, Any]]:
    with _LOCK:
        items = [s for s in SPANS if (not trace_id or s["trace_id"] == trace_id) and (not request_id or s["request_id"] == request_id)]
    return items[-limit:][::-1]
