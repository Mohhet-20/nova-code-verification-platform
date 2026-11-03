"""Redaction and bounding for anything that leaves the process through logs, spans, events or reports."""
from __future__ import annotations

import re
from typing import Any

SENSITIVE_KEY = re.compile(r"(authori[sz]ation|cookie|passw|secret|token|api[-_]?key|credential|private[-_]?key|signature|session)", re.I)
REDACTED = "[REDACTED]"
MAX_DEPTH = 4


def clip(text: str, limit: int) -> str:
    return text if len(text) <= limit else text[: max(0, limit - 1)] + "…"


def clean_str(value: str, limit: int = 1000) -> str:
    """Pattern-scrub a string (tokens, key=value secrets), strip control characters (log injection) and bound its length."""
    from app.services.analysis.redaction import scrub

    value = re.sub(r"[\x00-\x08\x0b-\x1f\x7f]", " ", value.replace("\r", " ").replace("\n", "\\n"))
    return scrub(clip(value, limit * 2), limit)


def clean(value: Any, limit: int = 1000, depth: int = 0) -> Any:
    if isinstance(value, str):
        return clean_str(value, limit)
    if isinstance(value, bool | int | float) or value is None:
        return value
    if depth >= MAX_DEPTH:
        return clean_str(str(value), 100)
    if isinstance(value, dict):
        return {str(k)[:60]: (REDACTED if SENSITIVE_KEY.search(str(k)) else clean(v, limit, depth + 1)) for k, v in list(value.items())[:50]}
    if isinstance(value, list | tuple | set):
        return [clean(v, limit, depth + 1) for v in list(value)[:50]]
    return clean_str(str(value), limit)
