"""Defence in depth: findings are built without secret values, and every free-text field is scrubbed anyway."""
from __future__ import annotations

import re

REDACTED = "[REDACTED]"

# Shapes of well-known credentials. Matching text is replaced wholesale.
TOKEN_PATTERNS: dict[str, re.Pattern[str]] = {
    "AWS access key id": re.compile(r"\b(?:AKIA|ASIA)[0-9A-Z]{16}\b"),
    "GitHub token": re.compile(r"\bgh[pousr]_[A-Za-z0-9]{30,}\b"),
    "Slack token": re.compile(r"\bxox[abprs]-[A-Za-z0-9-]{10,}\b"),
    "Stripe live key": re.compile(r"\b[sr]k_live_[A-Za-z0-9]{16,}\b"),
    "Google API key": re.compile(r"\bAIza[0-9A-Za-z_-]{35}\b"),
    "private key block": re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----"),
    "JSON web token": re.compile(r"\beyJ[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}\b"),
}
_ASSIGNMENT = re.compile(
    r"(?i)\b((?:pass(?:word|wd)?|secret|token|api[_-]?key|auth|credential)[\w-]*\s*[:=]\s*)(['\"]?)[^\s'\",;]{4,}\2")


def scrub(text: str, limit: int = 1000) -> str:
    for pattern in TOKEN_PATTERNS.values():
        text = pattern.sub(REDACTED, text)
    text = _ASSIGNMENT.sub(lambda m: f"{m.group(1)}{REDACTED}", text)
    return text if len(text) <= limit else text[: limit - 1] + "…"
