"""Test recommendations derived from the actual change. They are suggestions only: nothing here executes or generates a test."""
from __future__ import annotations

import hashlib
import re
from typing import Any

from app.services.review.findings import Draft, breaking_change

PRIORITY_RANK = {"high": 0, "medium": 1, "low": 2}
VALIDATION_RE = re.compile(r"(?i)(valid|check|verify|sanit|ensure|auth|permission|allow|deny|login|token|parse)")
NOT_EXECUTED = "Recommended only. Nothing was run; use the Tests & coverage workspace to plan, review and approve execution."


def _id(*parts: str) -> str:
    return hashlib.sha256("\x00".join(parts).encode()).hexdigest()[:16]


def _existing(path: str, tests_state: dict[str, Any], changed_tests: set[str]) -> dict[str, Any]:
    if not tests_state.get("discovered"):
        return {"status": "unknown", "tests": [], "changed_in_review": False,
                "note": "Tests have not been discovered, so existing tests cannot be determined."}
    tests = tests_state["associated"].get(path, [])
    if not tests:
        return {"status": "none_found", "tests": [], "changed_in_review": False,
                "note": "No discovered test imports this file (static discovery; indirect use is possible)."}
    return {"status": "imported_by_tests", "tests": tests[:5], "changed_in_review": any(t in changed_tests for t in tests),
            "note": "These tests import the file; that does not show they exercise the changed behavior."}


def build(per_file: list[dict[str, Any]], drafts: list[Draft], impact: dict[str, Any], tests_state: dict[str, Any],
          coverage_pct: dict[str, float], importers_of: dict[str, list[str]], changed_tests: set[str], limit: int) -> tuple[list[dict[str, Any]], int]:
    items: dict[str, dict[str, Any]] = {}

    def add(path: str, symbol: str | None, behavior: str, reason: str, source: str, evidence: list[str], priority: str, confidence: str,
            kind: str | None = None) -> None:
        rid = _id(path, symbol or "", behavior)
        if rid in items:
            return
        ex = _existing(path, tests_state, changed_tests)
        if source == "changed_symbol" and ex["status"] == "none_found" and priority == "medium":
            priority = "high"
        cov = coverage_pct.get(path)
        if cov is not None:
            evidence = [*evidence, f"File-level coverage was {cov:.0f}% in the latest measured run, which may predate this change."]
        items[rid] = {"id": rid, "target": {"rel_path": path, "symbol": symbol, "kind": kind}, "behavior": behavior, "reason": reason,
                      "evidence_source": source, "evidence": evidence[:5], "priority": priority, "confidence": confidence,
                      "existing_tests": ex, "status": "recommended_only", "note": NOT_EXECUTED}

    for f in per_file:
        if f["is_test"] or f["change_type"] == "unsupported":
            continue
        path = f["path"]
        for s in f["symbols"]:
            if s.kind == "module":
                continue
            where = f"{path}:{s.start}" if s.start else path
            if s.change_kind == "removed":
                imps = importers_of.get(f.get("old_path") or path, [])
                add(path, s.name, f"Confirm nothing still depends on the removed {s.kind} {s.name}.",
                    "A symbol was removed in this change.", "removed_symbol",
                    [f"{s.name} no longer exists in the new version.", *(["Importing files: " + ", ".join(imps[:3])] if imps else [])],
                    "high" if s.public else "medium", "medium", s.kind)
                continue
            sig_break = (s.change_kind == "modified" and s.signature_old and s.signature_new and s.signature_old != s.signature_new
                         and breaking_change(s.signature_old, s.signature_new))
            if sig_break:
                add(path, s.name, f"Verify existing callers still work with the new signature of {s.name}.",
                    f"The signature changed incompatibly ({sig_break}).", "signature_change",
                    [f"Previous: {s.signature_old}", f"New: {s.signature_new}"], "high" if s.public else "medium", "high", s.kind)
            if VALIDATION_RE.search(s.name):
                add(path, s.name, f"Verify that invalid input or unauthorized use is rejected by {s.name}.",
                    "The changed function's name indicates validation, parsing or access control.", "changed_symbol",
                    [f"{s.change_kind} at {where}"], "high", "low", s.kind)
            if s.handlers_old is not None and s.handlers_new is not None and s.handlers_old != s.handlers_new:
                add(path, s.name, f"Verify the error paths of {s.name}: expected exceptions are raised or handled.",
                    "The try/except/raise structure of the function changed.", "changed_symbol",
                    [f"try/except/raise nodes: {s.handlers_old} before, {s.handlers_new} after"], "medium", "medium", s.kind)
            if s.complexity and s.complexity >= 10:
                add(path, s.name, f"Cover each branch of {s.name} (cyclomatic complexity {s.complexity}).",
                    "A complex function was changed.", "changed_symbol", [f"{s.change_kind} at {where}"], "medium", "medium", s.kind)
            if s.change_kind == "added":
                add(path, s.name, f"Verify typical results and failure behavior of the new {s.kind} {s.name}.",
                    "New code was added.", "changed_symbol", [f"added at {where}"], "medium", "medium", s.kind)
            elif s.change_kind == "modified":
                add(path, s.name, f"Verify the changed behavior of {s.name} and that previously supported inputs still work.",
                    "Existing code was modified.", "changed_symbol", [f"modified at {where}"], "medium", "medium", s.kind)
    for d in drafts:
        if d.source == "analyzer" and d.severity in ("critical", "high", "medium") and d.rel_path:
            add(d.rel_path, d.scope or None, f"Add a test that would fail because of: {d.title}.",
                f"{d.rule_id} reported this on changed lines.", "finding", [d.explanation[:200]], "high" if d.severity != "medium" else "medium",
                d.confidence)
    for fl in impact.get("failures", []):
        for p in fl["files"][:1]:
            add(p, None, f"Rerun {fl['test_id'] or 'the failing test'} through the approved verification workflow after this change.",
                "An open recorded failure involves a changed file.", "failure", [f"Failure {fl['id']}: {fl['summary']}"], "high", "high")
    ordered = sorted(items.values(), key=lambda r: (PRIORITY_RANK[r["priority"]], r["target"]["rel_path"], r["target"]["symbol"] or "", r["id"]))
    return ordered[:limit], max(0, len(ordered) - limit)
