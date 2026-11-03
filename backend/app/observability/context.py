"""Request/job correlation context and identifier validation. Incoming identifiers are untrusted input, never identities."""
from __future__ import annotations

import re
import secrets
from contextvars import ContextVar
from dataclasses import dataclass

REQUEST_ID = ContextVar[str | None]("nova_request_id", default=None)
TRACE_ID = ContextVar[str | None]("nova_trace_id", default=None)
SPAN_ID = ContextVar[str | None]("nova_span_id", default=None)
JOB_ID = ContextVar[str | None]("nova_job_id", default=None)
JOB_TYPE = ContextVar[str | None]("nova_job_type", default=None)

_REQ = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._\-]{7,63}$")
_TRACEPARENT = re.compile(r"^00-([0-9a-f]{32})-([0-9a-f]{16})-([0-9a-f]{2})$")


def new_request_id() -> str:
    return "req_" + secrets.token_hex(8)


def valid_request_id(value: str | None) -> str | None:
    """Accept only a short, plain token; anything else (control characters, newlines, spaces, long values) is discarded."""
    return value if value and _REQ.fullmatch(value) else None


def parse_traceparent(value: str | None) -> tuple[str, str, bool] | None:
    """W3C trace context: (trace_id, parent_span_id, sampled). All-zero ids are invalid."""
    if not value:
        return None
    m = _TRACEPARENT.fullmatch(value.strip())
    if not m or set(m[1]) == {"0"} or set(m[2]) == {"0"}:
        return None
    return m[1], m[2], bool(int(m[3], 16) & 1)


def format_traceparent(trace_id: str, span_id: str, sampled: bool) -> str:
    return f"00-{trace_id}-{span_id}-{'01' if sampled else '00'}"


@dataclass(frozen=True)
class Snapshot:
    request_id: str | None
    trace_id: str | None
    span_id: str | None


def snapshot() -> Snapshot:
    return Snapshot(REQUEST_ID.get(), TRACE_ID.get(), SPAN_ID.get())
