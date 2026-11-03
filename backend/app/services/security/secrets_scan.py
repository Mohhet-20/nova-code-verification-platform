"""Dedicated secrets detection. Matched values are NEVER stored, returned, logged or exported: a finding carries a rule, a location, the length and a masked preview.

A detector cannot guarantee that every credential is found (see docs/architecture/SECURITY.md). Nothing is revoked, rotated, transmitted or tested.
"""
from __future__ import annotations

import hashlib
import math
import os
import re
from dataclasses import dataclass
from pathlib import Path

import pathspec

from app.services.analysis.config import looks_generated
from app.services.analysis.redaction import TOKEN_PATTERNS, scrub
from app.services.security.codescan import read_regular
from app.services.security.inventory import PRUNE_DIRS

VERSION = "1.0.0"
EXTRA_PATTERNS: dict[str, re.Pattern[str]] = {
    "npm access token": re.compile(r"\bnpm_[A-Za-z0-9]{36}\b"),
    "PyPI upload token": re.compile(r"\bpypi-AgEIcHlwaS5vcmc[A-Za-z0-9_-]{40,}\b"),
    "SendGrid API key": re.compile(r"\bSG\.[A-Za-z0-9_-]{22}\.[A-Za-z0-9_-]{43}\b"),
    "credentials in URL": re.compile(r"\b[a-z][a-z0-9+.-]{1,12}://[^/\s:@'\"]{1,64}:([^/\s:@'\"]{6,})@[^/\s'\"]{3,}"),
}
KNOWN = {**TOKEN_PATTERNS, **EXTRA_PATTERNS}
SEVERITY_BY_LABEL = {"private key block": "critical"}
GENERIC = re.compile(
    r"(?i)(?P<name>[\w.-]*(?:pass(?:word|wd)?|secret|api[_-]?key|apikey|access[_-]?key|private[_-]?key|auth[_-]?token|token|credential)[\w.-]*)\s*(?::|=|=>|:=)\s*(?P<q>['\"]?)(?P<val>[^\s'\",;#]{8,})(?P=q)")
PLACEHOLDER = re.compile(r"(?i)(example|sample|dummy|fake|placeholder|changeme|change_me|your[_-]|xxxx|\*\*\*\*|<[^>]+>|\$\{|\{\{|%\(|todo|redacted|notasecret|test|none|null|true|false)")
TEXT_SUFFIX = {".py", ".js", ".ts", ".tsx", ".jsx", ".json", ".yml", ".yaml", ".toml", ".ini", ".cfg", ".conf", ".env", ".properties", ".sh", ".bash", ".txt", ".md", ".cs", ".java",
               ".go", ".rb", ".php", ".xml", ".tf", ".gradle", ".html", ".sql", ".ps1", ".bat", ""}
SKIP_NAMES = {"package-lock.json", "npm-shrinkwrap.json", "poetry.lock", "uv.lock", "Pipfile.lock", "yarn.lock", "pnpm-lock.yaml", "Cargo.lock"}


@dataclass
class SecretHit:
    rule_id: str
    label: str
    rel_path: str
    line: int
    length: int
    severity: str
    confidence: str
    preview: str
    value_hash: str  # salted, truncated: identity across line moves; not reversible for a high-entropy value
    explanation: str


def entropy(s: str) -> float:
    if not s:
        return 0.0
    counts: dict[str, int] = {}
    for ch in s:
        counts[ch] = counts.get(ch, 0) + 1
    return -sum(c / len(s) * math.log2(c / len(s)) for c in counts.values())


def _mask(line: str, start: int, end: int, keep: int = 0) -> str:
    value = line[start:end]
    masked = value[:keep] + "*" * max(4, min(12, len(value) - keep))
    out = line[:start] + masked + line[end:]
    a = max(0, start - 40)
    return scrub(out[a:a + 120].strip(), 160)


def scan_text(rel_path: str, text: str, salt: str) -> list[SecretHit]:
    hits: list[SecretHit] = []
    seen: set[tuple[int, int, int]] = set()
    is_example = bool(re.search(r"(example|sample|template|\.dist$)", rel_path, re.IGNORECASE))
    for ln, line in enumerate(text.splitlines(), 1):
        if len(line) > 2000:
            line = line[:2000]
        for label, pat in KNOWN.items():
            for m in pat.finditer(line):
                span = m.span(1) if pat.groups else m.span()
                if (ln, *span) in seen:
                    continue
                seen.add((ln, *span))
                value = line[span[0]:span[1]]
                doc_example = value.upper().endswith("EXAMPLE") or bool(PLACEHOLDER.search(value))
                hits.append(SecretHit("SEC-SECRET-001", label, rel_path, ln, len(value), SEVERITY_BY_LABEL.get(label, "high"), "low" if doc_example or is_example else "high",
                                      _mask(line, span[0], span[1], keep=4 if label in ("AWS access key id", "GitHub token", "Slack token") else 0),
                                      hashlib.sha256(f"{salt}|{value}".encode()).hexdigest()[:24],
                                      f"A value with the shape of a {label} appears in the file." + (" It looks like a documentation or placeholder value." if doc_example else "")))
        for m in GENERIC.finditer(line):
            span = m.span("val")
            if any(s[0] == ln and s[1] <= span[0] < s[2] for s in seen):
                continue
            value = m.group("val")
            if PLACEHOLDER.search(value) or value.startswith(("os.", "env", "get", "self.", "config", "settings")) or "(" in value or "[" in value:
                continue
            ent = entropy(value)
            if ent < 3.0 or (value.isalpha() and ent < 3.6) or value.lower() == value and value.isalpha():
                continue
            seen.add((ln, *span))
            confidence = "medium" if ent >= 3.5 and any(c.isdigit() for c in value) and len(value) >= 12 and not is_example else "low"
            hits.append(SecretHit("SEC-SECRET-002", "credential-like assignment", rel_path, ln, len(value), "medium" if confidence == "medium" else "low", confidence,
                                  _mask(line, span[0], span[1]), hashlib.sha256(f"{salt}|{value}".encode()).hexdigest()[:24],
                                  f"`{m.group('name')[:60]}` is assigned a high-entropy literal ({len(value)} characters)."))
    return hits


@dataclass
class SecretsResult:
    hits: list[SecretHit]
    scanned: int
    skipped: dict[str, int]
    excluded_by_config: list[str]
    problems: list[str]
    truncated: bool


def scan_repository(root: Path, salt: str, *, max_files: int, max_file_bytes: int, exclude_globs: list[str]) -> SecretsResult:
    spec = pathspec.GitIgnoreSpec.from_lines(exclude_globs) if exclude_globs else None
    hits: list[SecretHit] = []
    skipped: dict[str, int] = {}
    excluded: list[str] = []
    problems: list[str] = []
    scanned = 0
    truncated = False
    root_real = os.path.realpath(root)
    for dirpath, dirnames, filenames in os.walk(root, followlinks=False):
        dirnames[:] = sorted(d for d in dirnames if d not in PRUNE_DIRS and not os.path.islink(os.path.join(dirpath, d)))
        for fn in sorted(filenames):
            full = os.path.join(dirpath, fn)
            rel = os.path.relpath(full, root).replace(os.sep, "/")
            suffix = os.path.splitext(fn)[1].lower()
            if fn in SKIP_NAMES or looks_generated(rel, "") or suffix not in TEXT_SUFFIX and not fn.startswith(".env"):
                skipped["generated_or_binary_type"] = skipped.get("generated_or_binary_type", 0) + 1
                continue
            if spec is not None and spec.match_file(rel):
                excluded.append(rel)
                continue
            if os.path.islink(full) or not os.path.realpath(full).startswith(root_real + os.sep):
                skipped["symlink"] = skipped.get("symlink", 0) + 1
                continue
            if scanned >= max_files:
                truncated = True
                break
            data, status = read_regular(root, rel, max_file_bytes)
            if data is None:
                skipped[status] = skipped.get(status, 0) + 1
                continue
            if b"\x00" in data[:4096]:
                skipped["binary"] = skipped.get("binary", 0) + 1
                continue
            scanned += 1
            try:
                hits.extend(scan_text(rel, data.decode("utf-8", errors="replace"), salt))
            except Exception as exc:  # noqa: BLE001 - a detector bug must never leak the file text
                problems.append(f"{rel}: secrets rules failed ({type(exc).__name__})")
        if truncated:
            break
    if truncated:
        problems.append(f"file limit {max_files} reached; files beyond it were not scanned")
    return SecretsResult(hits, scanned, skipped, sorted(excluded), problems, truncated)
