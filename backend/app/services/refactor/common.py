"""Shared building blocks of the refactoring subsystem: the workspace view of a repository, the edit model and findings.

Everything here reads files from disk through the Phase 08 path rules (no symlinks, repository-relative, allow-listed types) and never
writes. The *plan* an engine returns is plain data (JSON-serialisable) so it can be stored, reviewed and re-checked later.
"""
from __future__ import annotations

import hashlib
import re
from collections.abc import Iterable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from app.core.config import Settings
from app.services.analysis.redaction import scrub
from app.services.repair import patch as patchlib

PLAN_VERSION = "1.0"
PARSER_NAME = {"python": "Python", "javascript": "JavaScript", "jsx": "JSX", "typescript": "TypeScript", "tsx": "TSX"}


def parser_for(language: str) -> object | None:
    """tree-sitter parser for a lower-case language id (the index stores display names such as ``TSX``)."""
    from app.services.intelligence.parser_registry import get_parser

    return get_parser(PARSER_NAME.get(language, language))
SEVERITIES = ("blocker", "warning", "info")


@dataclass
class Edit:
    rel_path: str
    line: int  # 1-based
    col: int  # 0-based character offset
    end_col: int
    old: str
    new: str
    kind: str  # definition | reference | import_name | module_attribute | export_string | string_annotation | star_import_use | ...
    confidence: str = "certain"  # certain: resolved by scope analysis; inferred: resolved by a weaker rule (always listed for review)
    note: str = ""

    @property
    def key(self) -> tuple[str, int, int, int]:
        return (self.rel_path, self.line, self.col, self.end_col)


@dataclass
class Finding:
    severity: str  # blocker | warning | info
    code: str
    message: str
    locations: list[dict[str, Any]] = field(default_factory=list)

    def as_dict(self) -> dict[str, Any]:
        return {"severity": self.severity, "code": self.code, "message": scrub(self.message, 500), "locations": self.locations[:20]}


@dataclass
class PlanResult:
    edits: list[Edit] = field(default_factory=list)
    findings: list[Finding] = field(default_factory=list)
    resolution: dict[str, Any] = field(default_factory=dict)
    side_effects: list[str] = field(default_factory=list)
    limitations: list[str] = field(default_factory=list)
    candidate_edits: list[Edit] = field(default_factory=list)  # shown for review when the plan cannot be applied
    extra: dict[str, Any] = field(default_factory=dict)  # engine specific output (extraction details, suggestions, ...)
    rewrites: dict[str, str] = field(default_factory=dict)  # whole-file results for block edits (extract function): rel_path -> new text
    block_edits: list[dict[str, Any]] = field(default_factory=list)  # reviewable description of those multi-line edits
    complete: bool = True

    def add(self, severity: str, code: str, message: str, locations: list[dict[str, Any]] | None = None) -> None:
        self.findings.append(Finding(severity, code, message, locations or []))

    @property
    def blockers(self) -> list[Finding]:
        return [f for f in self.findings if f.severity == "blocker"]


class Workspace:
    """A read-only view of one repository: which files the index knows and what is on disk right now."""

    def __init__(self, root: Path, settings: Settings, indexed: dict[str, tuple[str, str]], index_state: str = "unknown",
                 index_notes: list[str] | None = None) -> None:
        self.root = root
        self.settings = settings
        self.indexed = indexed  # rel_path -> (language, sha256 at index time)
        self.index_state = index_state
        self.index_notes = index_notes or []
        self._cache: dict[str, tuple[str, str] | None] = {}
        self.unreadable: dict[str, str] = {}

    def paths(self, languages: Iterable[str]) -> list[str]:
        langs = set(languages)
        out = sorted(p for p, (lang, _) in self.indexed.items() if lang in langs)
        return out[: self.settings.refactor_max_scan_files]

    def read(self, rel: str) -> tuple[str, str] | None:
        """(text, sha256 of the bytes on disk) or None when the file cannot be read safely."""
        if rel in self._cache:
            return self._cache[rel]
        out: tuple[str, str] | None = None
        try:
            target = patchlib.check_path(self.root, rel, self.settings)
            text, data = patchlib.read_text_file(target, rel, self.settings)
            out = (text, patchlib.sha256_bytes(data))
        except patchlib.PatchError as exc:
            self.unreadable[rel] = exc.code
        except OSError:
            self.unreadable[rel] = "unreadable"
        self._cache[rel] = out
        return out

    def stale_paths(self, rels: Iterable[str]) -> list[str]:
        out = []
        for r in rels:
            got = self.read(r)
            idx = self.indexed.get(r)
            if got is not None and idx is not None and idx[1] != got[1]:
                out.append(r)
        return sorted(out)


# ------------------------------------------------------------------------------------------------ edits


class EditError(Exception):
    def __init__(self, code: str, message: str, details: list[dict[str, Any]] | None = None) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.details = details or []


def find_conflicts(edits: list[Edit]) -> list[Finding]:
    """Overlapping or contradictory edits. Identical duplicates are merged by the caller, not reported."""
    out: list[Finding] = []
    by_file: dict[str, list[Edit]] = {}
    for e in edits:
        by_file.setdefault(e.rel_path, []).append(e)
    for rel, es in by_file.items():
        es.sort(key=lambda e: (e.line, e.col, e.end_col))
        for a, b in zip(es, es[1:], strict=False):
            if a.line == b.line and b.col < a.end_col:
                same = (a.col, a.end_col, a.new) == (b.col, b.end_col, b.new)
                if not same:
                    out.append(Finding("blocker", "overlapping_edits", f"Two edits overlap in {rel} on line {a.line}.",
                                       [{"path": rel, "line": a.line}]))
    return out


def dedupe(edits: list[Edit]) -> list[Edit]:
    seen: dict[tuple[str, int, int, int], Edit] = {}
    for e in edits:
        prev = seen.get(e.key)
        if prev is None:
            seen[e.key] = e
        elif prev.new != e.new:
            # contradictory edits at one location: keep both so find_conflicts reports them
            seen[(*e.key[:3], e.end_col + 100000 + len(seen))] = e
    return sorted(seen.values(), key=lambda e: (e.rel_path, e.line, e.col))


def apply_edits(source: str, edits: list[Edit]) -> str:
    """Apply single-line edits whose ``old`` text must still be at the recorded position. Raises ``EditError`` otherwise."""
    lines = source.split("\n")
    by_line: dict[int, list[Edit]] = {}
    for e in edits:
        by_line.setdefault(e.line, []).append(e)
    for ln, es in by_line.items():
        if ln < 1 or ln > len(lines):
            raise EditError("stale", f"Line {ln} no longer exists.", [{"line": ln}])
        text = lines[ln - 1]
        for e in sorted(es, key=lambda x: x.col, reverse=True):
            if text[e.col:e.end_col] != e.old:
                raise EditError("stale", f"The text at {e.rel_path}:{ln} is no longer {e.old!r}.", [{"path": e.rel_path, "line": ln}])
            text = text[:e.col] + e.new + text[e.end_col:]
        lines[ln - 1] = text
    return "\n".join(lines)


def context_of(source_lines: list[str], line: int, limit: int = 200) -> str:
    if 1 <= line <= len(source_lines):
        return scrub(source_lines[line - 1].rstrip("\r"), limit)
    return ""


def edit_dict(e: Edit, source_lines: list[str]) -> dict[str, Any]:
    return {"id": hashlib.sha256(f"{e.rel_path}:{e.line}:{e.col}:{e.end_col}:{e.new}".encode()).hexdigest()[:12], "rel_path": e.rel_path, "line": e.line,
            "col": e.col, "end_col": e.end_col, "old": e.old, "new": e.new, "kind": e.kind, "confidence": e.confidence, "note": e.note,
            "context": context_of(source_lines, e.line)}


IDENT = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")


def loc(rel: str, line: int, col: int | None = None) -> dict[str, Any]:
    d: dict[str, Any] = {"path": rel, "line": line}
    if col is not None:
        d["col"] = col
    return d
