"""Deterministic, explainable change-risk model.

score = 100 * sum(weight_i * normalized_i over *available* factors) / sum(weight_i over *available* factors)

Every normalized value is in [0, 1] and saturates (``x / (x + k)``), so a large change cannot exceed 1 on any one factor and a
large repository is not penalised for its size: only the change itself and the measured neighbourhood of that change count.
A factor whose data does not exist (no index, no coverage run, no failure history ...) is *unavailable*: it contributes neither
risk nor safety, and the response says so. If less than half of the total weight is available, or nothing analysable changed,
the result is ``unknown``.

The score ranks review effort and regression exposure. It is NOT a probability of a defect and is not calibrated against
outcomes. Touching sensitive code raises exposure, it never marks the change defective.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

VERSION = "1.0"
WEIGHTS: dict[str, float] = {
    "change_size": 10, "files_changed": 5, "symbols_changed": 10, "public_interface": 12, "auth_sensitive": 10, "data_model": 8,
    "error_handling": 6, "complexity": 9, "existing_findings": 10, "new_findings": 12, "dependents": 8, "coverage": 10,
    "failure_history": 6, "test_results": 5,
}
LABELS = {
    "change_size": "Size of the change (added + removed lines in non-test code)",
    "files_changed": "Number of changed non-test code files",
    "symbols_changed": "Number of changed functions, methods and classes",
    "public_interface": "Public symbols removed or with a changed signature",
    "auth_sensitive": "Changes in authentication / authorization related code",
    "data_model": "Changes to database models or migrations",
    "error_handling": "Functions whose try/except/raise structure changed",
    "complexity": "Highest cyclomatic complexity among changed functions",
    "existing_findings": "Open static-analysis findings located in modified code",
    "new_findings": "Static-analysis findings in changed lines that are not already open findings",
    "dependents": "Files that depend on the changed files (bounded, from the index)",
    "coverage": "Measured test coverage of the changed source files (file level)",
    "failure_history": "Open recorded test failures that involve the changed files",
    "test_results": "Test results for this revision",
}
# category lower bounds (score >= bound)
THRESHOLDS = [("critical", 65.0), ("high", 40.0), ("moderate", 20.0), ("low", 0.0)]
MIN_AVAILABLE_SHARE = 0.5
SEVERITY_WEIGHT = {"critical": 8.0, "high": 4.0, "medium": 2.0, "low": 1.0, "info": 0.25}
DISCLAIMER = ("A heuristic ranking of review effort and regression exposure from the evidence listed below. It is not a probability "
              "that the change contains a defect, and a low score does not show the change is correct.")


def sat(x: float, k: float) -> float:
    return 0.0 if x <= 0 else x / (x + k)


@dataclass
class Inputs:
    code_files: int = 0                      # changed, non-test, parseable-or-known-code files
    analyzable_files: int = 0                # files whose content could be analysed at all
    lines_changed: int = 0
    symbols_changed: int = 0
    symbols_available: bool = True
    public_changes: list[str] = field(default_factory=list)
    auth_hits: list[str] = field(default_factory=list)
    data_hits: list[str] = field(default_factory=list)
    error_changes: list[str] = field(default_factory=list)
    max_complexity: int | None = None
    max_complexity_name: str | None = None
    existing_findings: list[tuple[str, str]] | None = None   # (severity, title); None = no analysis data
    new_findings: list[tuple[str, str]] | None = None       # None = analyzers did not run
    dependents: int | None = None                           # None = unknown
    dependents_incomplete: bool = False
    coverage_uncovered: float | None = None                 # mean uncovered fraction of covered changed files, None = no data
    coverage_note: str = ""
    failures: int | None = None                             # None = no failure history recorded
    notes: list[str] = field(default_factory=list)


def _factor(fid: str, status: str, raw: Any, norm: float | None, detail: str, evidence: list[str] | None = None) -> dict[str, Any]:
    w = WEIGHTS[fid]
    return {"id": fid, "label": LABELS[fid], "weight": w, "status": status, "raw": raw,
            "normalized": None if norm is None else round(norm, 4),
            "contribution": None if norm is None else round(w * norm, 3), "detail": detail, "evidence": (evidence or [])[:10]}


def _weighted_findings(items: list[tuple[str, str]]) -> float:
    return sum(SEVERITY_WEIGHT.get(sev, 0.0) for sev, _ in items)


def compute(inp: Inputs) -> dict[str, Any]:
    f: list[dict[str, Any]] = []
    unav = "unavailable"
    f.append(_factor("change_size", "measured", inp.lines_changed, sat(inp.lines_changed, 200),
                     f"{inp.lines_changed} changed lines in non-test code; saturates (x / (x + 200))."))
    f.append(_factor("files_changed", "measured", inp.code_files, sat(inp.code_files, 8),
                     f"{inp.code_files} changed non-test code file(s); saturates (x / (x + 8))."))
    if inp.symbols_available:
        f.append(_factor("symbols_changed", "measured", inp.symbols_changed, sat(inp.symbols_changed, 10),
                         f"{inp.symbols_changed} changed function/method/class symbol(s); saturates (x / (x + 10))."))
        f.append(_factor("public_interface", "measured", len(inp.public_changes), min(1.0, len(inp.public_changes) / 3),
                         f"{len(inp.public_changes)} public symbol(s) removed or with a changed signature; 3 or more is the maximum.",
                         sorted(inp.public_changes)))
        f.append(_factor("error_handling", "measured", len(inp.error_changes), sat(len(inp.error_changes), 3),
                         f"{len(inp.error_changes)} function(s) with a different count of try/except/raise nodes; saturates (x / (x + 3)).",
                         sorted(inp.error_changes)))
    else:
        for fid in ("symbols_changed", "public_interface", "error_handling"):
            f.append(_factor(fid, unav, None, None, "Changed symbols could not be determined for the changed files."))
    f.append(_factor("auth_sensitive", "measured", len(inp.auth_hits), min(1.0, len(inp.auth_hits) / 2),
                     "Matches on path or symbol names (auth, login, permission, token, session, password ...). "
                     "Name matching is a hint about where to look, not evidence of a flaw.", sorted(inp.auth_hits)))
    f.append(_factor("data_model", "measured", len(inp.data_hits), min(1.0, len(inp.data_hits) / 2),
                     "Changed files under migrations/ or alembic/, or named models/schema.", sorted(inp.data_hits)))
    if inp.max_complexity is None:
        f.append(_factor("complexity", unav, None, None, "No changed Python function with a measurable complexity."))
    else:
        f.append(_factor("complexity", "measured", inp.max_complexity, sat(inp.max_complexity, 10),
                         f"Highest cyclomatic complexity among changed functions is {inp.max_complexity}; saturates (x / (x + 10)).",
                         [inp.max_complexity_name or ""]))
    if inp.existing_findings is None:
        f.append(_factor("existing_findings", unav, None, None, "Static analysis has not been run for this repository."))
    else:
        w = _weighted_findings(inp.existing_findings)
        f.append(_factor("existing_findings", "measured", len(inp.existing_findings), sat(w, 6),
                         f"{len(inp.existing_findings)} open finding(s) in modified code (severity weights critical 8, high 4, medium 2, low 1, info 0.25; "
                         f"saturates w / (w + 6)).", [f"{s}: {t}" for s, t in inp.existing_findings]))
    if inp.new_findings is None:
        f.append(_factor("new_findings", unav, None, None, "Static analyzers could not be run on the changed files."))
    else:
        w = _weighted_findings(inp.new_findings)
        f.append(_factor("new_findings", "measured", len(inp.new_findings), sat(w, 6),
                         f"{len(inp.new_findings)} finding(s) on changed lines that are not already open findings (counted once; "
                         f"same weights).", [f"{s}: {t}" for s, t in inp.new_findings]))
    if inp.dependents is None:
        f.append(_factor("dependents", unav, None, None, "The dependency graph is missing or does not cover the changed files."))
    else:
        extra = " The list is incomplete (see impact limitations)." if inp.dependents_incomplete else ""
        f.append(_factor("dependents", "measured", inp.dependents, sat(inp.dependents, 10),
                         f"{inp.dependents} dependent file(s) within the traversal depth; saturates (x / (x + 10)).{extra}"))
    if inp.coverage_uncovered is None:
        f.append(_factor("coverage", unav, None, None, inp.coverage_note or "No measured coverage exists for the changed files."))
    else:
        f.append(_factor("coverage", "measured", round(inp.coverage_uncovered, 3), inp.coverage_uncovered,
                         "Mean uncovered fraction of changed files that have coverage data. " + inp.coverage_note))
    if inp.failures is None:
        f.append(_factor("failure_history", unav, None, None, "No test failures have been recorded for this repository."))
    else:
        f.append(_factor("failure_history", "measured", inp.failures, sat(inp.failures, 2),
                         f"{inp.failures} open recorded failure(s) with stack frames in the changed files; saturates (x / (x + 2))."))
    f.append(_factor("test_results", unav, None, None,
                     "NOVA does not link test runs to a revision, so no result can be attributed to this change."))

    total = sum(WEIGHTS.values())
    avail = sum(x["weight"] for x in f if x["status"] == "measured")
    share = avail / total
    notes = list(inp.notes)
    base: dict[str, Any] = {"version": VERSION, "weights": WEIGHTS, "thresholds": [{"category": c, "min_score": t} for c, t in THRESHOLDS],
                            "formula": "score = 100 * sum(weight * normalized) / sum(weight), over factors with status 'measured'",
                            "available_weight": avail, "total_weight": total, "evidence_share": round(share, 3), "factors": f,
                            "disclaimer": DISCLAIMER, "notes": notes}
    if inp.analyzable_files == 0 or inp.code_files == 0:
        return base | {"state": "unknown", "score": None, "category": "unknown",
                       "reason": "No analysable non-test code changed (documentation, binary, unsupported or test-only change), so no risk is estimated."}
    if share < MIN_AVAILABLE_SHARE:
        return base | {"state": "unknown", "score": None, "category": "unknown",
                       "reason": f"Only {share:.0%} of the model's evidence is available (minimum {MIN_AVAILABLE_SHARE:.0%})."}
    score = 100.0 * sum(x["contribution"] for x in f if x["status"] == "measured") / avail
    score = round(score, 1)
    category = next(c for c, t in THRESHOLDS if score >= t)
    return base | {"state": "ranked", "score": score, "category": category, "reason": None}
