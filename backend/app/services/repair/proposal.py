"""Deterministic repair proposals. No AI provider is involved: every statement comes from stored evidence, the files on disk, or the
fixed rule table in ``autofix``. When no safe, verified change exists the result is a diagnostic recommendation, never a made-up patch."""
from __future__ import annotations

from typing import Any

from sqlalchemy.orm import Session

from app.core.config import Settings
from app.db.base import utcnow
from app.models import RepairInvestigation, Repository
from app.security.paths import resolve_repository_root
from app.services import repositories as repo_svc
from app.services.repair import autofix, evidence, selection
from app.services.repair import patch as patchlib
from app.services.repair.errors import RepairConflict, RepairInvalid
from app.services.review import impact

GENERATOR = "nova-deterministic-repair"
VERSION = "1.0"
GENERIC_BY_CATEGORY = {
    "assertion": "Compare the expected and actual values in the failing assertion with what the candidate code returns for that input, then decide whether the code or the test expectation is wrong.",
    "exception": "Find which input or state makes the candidate code raise the reported exception, and handle or prevent that case where it originates.",
    "collection_error": "Fix the import or syntax problem reported while collecting the test module before looking at behavior.",
    "test_error": "Check the test setup (fixtures, imports) for the reported error; the code under test may not be at fault.",
}


def _candidate_scopes(inv: RepairInvestigation) -> list[dict[str, Any]]:
    return [h for h in (inv.evidence.get("hypotheses") or [])[:3] if h["score"] >= inv.evidence.get("min_score", 2.0)]


def generate(db: Session, settings: Settings, repo: Repository, inv: RepairInvestigation) -> dict[str, Any]:
    if not inv.evidence:
        evidence.assemble(db, settings, repo, inv)
    ev = inv.evidence
    root = resolve_repository_root(repo.root_path, settings.registration_base_dirs)
    src = evidence.load_sources(db, repo, inv)
    finding, failure = src["finding"] or src["review_finding"], src["failure"]
    scopes = _candidate_scopes(inv)
    fixes: list[autofix.Fix] = []
    declined: list[str] = []
    for rel in inv.relevant_files:
        if not rel.endswith(".py"):
            continue
        try:
            text = patchlib.read_text_file(patchlib.check_path(root, rel, settings), rel, settings)[0]
        except patchlib.PatchError as exc:
            declined.append(f"{rel}: {exc.message}")
            continue
        wanted_rule = wanted_line = None
        if finding is not None and finding.rel_path == rel:
            wanted_rule, wanted_line = finding.rule_id, finding.start_line
        found, dec = autofix.propose(root, settings, rel, text, wanted_rule, wanted_line)
        declined.extend(dec)
        if finding is not None and finding.rel_path == rel:
            if not found and wanted_rule in autofix.RULES:
                declined.append(f"{rel}:{wanted_line} {wanted_rule}: the finding is no longer reported at that line (the file may have changed).")
            fixes.extend(found)
            continue
        for f in found:  # scope automatic changes to the ranked symbol(s); never edit unrelated code in the same file
            inside = any(s["rel_path"] == rel and s.get("symbol") and s["start_line"] and s["start_line"] <= f.line <= s["end_line"] for s in scopes)
            file_level_ok = f.rule_id in ("PY-COR-001", "PY-COR-002") and any(s["rel_path"] == rel and not s.get("symbol") for s in scopes)
            if inside or file_level_ok:
                fixes.append(f)
            else:
                declined.append(f"{rel}:{f.line} {f.rule_id}: outside the ranked candidate symbols, so it is not proposed for this investigation.")
    paths = sorted({f.rel_path for f in fixes}) or list(inv.relevant_files)
    tests = selection.select_tests(db, repo, inv, paths, None, failure) if _has_discovery(db, repo) else {
        "targets": [], "notes": ["Test discovery has not been run, so tests cannot be selected."]}
    snap = repo_svc.latest_snapshot(db, repo.id, completed_only=True)
    imp = impact.importers(db, snap.id if snap else None, paths) if snap else {}
    risks = [f"{len(v)} file(s) import {k}: {', '.join(v[:4])}{'…' if len(v) > 4 else ''}." for k, v in imp.items() if v]
    risks += [s for f in fixes for s in autofix.RULES[f.rule_id]["side_effects"]]
    root_cause = inv.root_cause or {}
    state = "ready" if fixes else ("diagnostic_only" if root_cause.get("state") == "ranked" or finding is not None else "insufficient_evidence")
    if fixes:
        strategy = " ".join(dict.fromkeys(autofix.RULES[f.rule_id]["strategy"] for f in fixes))
        expected = " ".join(dict.fromkeys(autofix.RULES[f.rule_id]["behavior"] for f in fixes))
    elif finding is not None:
        strategy = (finding.remediation or "").strip() or "Review the reported construct and correct it."
        expected = "The reported construct no longer triggers the finding."
    elif failure is not None and root_cause.get("state") == "ranked":
        strategy = GENERIC_BY_CATEGORY.get(failure.category, "Investigate the candidate location before changing code.")
        expected = "The failing test passes and previously passing tests keep passing."
    else:
        strategy = "No repair strategy can be recommended yet; gather the evidence listed under next steps."
        expected = "Unknown."
    doc: dict[str, Any] = {
        "generator": GENERATOR, "version": VERSION, "ai_assisted": False, "generated_at": utcnow().isoformat(), "state": state,
        "problem_statement": inv.title + (": " + inv.description.strip().replace("\n", " ")[:400] if inv.description.strip() else ""),
        "evidence": [f["id"] + " " + f["text"] for f in ev.get("facts", [])[:6]],
        "suspected_root_cause": {k: root_cause.get(k) for k in ("state", "kind", "summary", "confidence", "confidence_rationale", "note") if k in root_cause},
        "strategy": strategy,
        "files": [{"rel_path": h["rel_path"], "symbol": h.get("symbol")} for h in scopes] or [{"rel_path": r, "symbol": None} for r in inv.relevant_files],
        "expected_behavior": expected,
        "side_effects": list(dict.fromkeys(risks))[:8],
        "regression_risks": ["Behavior changes are not verified until the selected tests pass after the patch is applied."],
        "tests_to_run": tests["targets"], "test_selection_notes": tests["notes"],
        "confidence": {"level": root_cause.get("confidence", "low"),
                       "rationale": root_cause.get("confidence_rationale") or "No ranked root cause; see unresolved questions."},
        "patch_available": bool(fixes),
        "patch_reason": ("Verified deterministic fix(es): " + "; ".join(f"{f.rule_id} at {f.rel_path}:{f.line}" for f in fixes)
                         + ". Re-analysis confirms the targeted finding is gone and no rule gained findings.") if fixes else
                        ("No safe automatic patch: " + ("; ".join(declined[:3]) if declined else "no mechanical fix applies to the available evidence.")),
        "candidate_fixes": [f.info() for f in fixes],
        "limitations": ["Generated without an AI provider from deterministic rules and stored evidence.",
                        "A patch that passes re-analysis may still change behavior; only the selected tests can show it did not."] + evidence.LIMITATIONS[:1],
        "unresolved_questions": ev.get("unknowns", [])[:6],
        "next_steps": ev.get("next_steps", [])[:5],
    }
    inv.proposal = doc
    inv.proposal_generated_at = utcnow()
    return doc


def _has_discovery(db: Session, repo: Repository) -> bool:
    try:
        selection.known_ids(db, repo)
        return True
    except RepairConflict:
        return False


def fix_for(db: Session, settings: Settings, repo: Repository, inv: RepairInvestigation, rule_id: str | None, rel_path: str | None,
            line: int | None) -> autofix.Fix:
    """Return the one verified fix that matches a patch-generation request."""
    doc = inv.proposal or generate(db, settings, repo, inv)
    if not doc.get("patch_available"):
        raise RepairConflict("insufficient_evidence", doc.get("patch_reason") or "No patch can be generated from the available evidence.")
    root = resolve_repository_root(repo.root_path, settings.registration_base_dirs)
    matches: list[autofix.Fix] = []
    for c in doc["candidate_fixes"]:
        if (rule_id and c["rule_id"] != rule_id) or (rel_path and c["rel_path"] != rel_path) or (line and c["line"] != line):
            continue
        text = patchlib.read_text_file(patchlib.check_path(root, c["rel_path"], settings), c["rel_path"], settings)[0]
        found, _ = autofix.propose(root, settings, c["rel_path"], text, c["rule_id"], c["line"])
        matches.extend(f for f in found if f.line == c["line"])
    if not matches:
        raise RepairConflict("stale_source", "The proposal no longer matches the code on disk. Regenerate the proposal.")
    if len(matches) > 1:
        raise RepairInvalid("ambiguous_fix", "Several verified fixes are available; choose one by rule_id, rel_path and line.",
                            [{"rule_id": m.rule_id, "rel_path": m.rel_path, "line": m.line} for m in matches[:10]])
    return matches[0]
