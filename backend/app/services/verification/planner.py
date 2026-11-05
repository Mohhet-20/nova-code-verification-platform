"""Impact-aware check planning. The plan is explainable: every check has a reason, a requirement level, a confidence and prerequisites.

Tiers: 0 syntax, 1 static analysis, 2 tests, 3 coverage. Modes widen the test scope only:
``focused`` (tests importing the changed modules, plus changed tests), ``standard`` (also tests of direct dependents), ``full`` (every discovered test).
Nothing outside this list can be planned: there is no way to name an arbitrary command.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.config import Settings
from app.models import Repository, TestFile
from app.services import repositories as repo_svc
from app.services.refactor import impact as refactor_impact
from app.services.testing import service as testing_service
from app.services.verification import changes
from app.services.verification.errors import CvInvalid

MODES = ("focused", "standard", "full")
CHECK_ORDER = ("syntax", "static_analysis", "tests", "coverage")
MODE_MEANING = {
    "focused": "Tests that import the changed modules, plus changed tests.",
    "standard": "Focused tests plus tests of the modules that directly import the changed ones.",
    "full": "Every discovered test.",
}


@dataclass
class PlannedCheck:
    check_id: str
    ordinal: int
    tier: int
    required: bool
    reason: str
    confidence: str
    prerequisites: list[str] = field(default_factory=list)
    status: str = "planned"  # planned | not_applicable | blocked
    result: dict[str, Any] = field(default_factory=dict)

    def public(self) -> dict[str, Any]:
        return {"check_id": self.check_id, "tier": self.tier, "required": self.required, "reason": self.reason, "confidence": self.confidence,
                "prerequisites": self.prerequisites, "status": self.status}


@dataclass
class Plan:
    mode: str
    checks: list[PlannedCheck]
    test_targets: list[str] | None  # None = every discovered test
    unverified_paths: list[str]
    notes: list[str]
    binding: list[str]
    impact: dict[str, Any]

    def check(self, cid: str) -> PlannedCheck | None:
        return next((c for c in self.checks if c.check_id == cid), None)

    def public(self) -> dict[str, Any]:
        return {"mode": self.mode, "mode_meaning": MODE_MEANING[self.mode], "checks": [c.public() for c in self.checks],
                "test_targets": self.test_targets, "unverified_paths": self.unverified_paths[:50], "notes": self.notes[:20],
                "binding_problems": self.binding, "impact": self.impact}


def _test_files(db: Session, repo_id: str) -> tuple[Any, dict[str, TestFile]]:
    d = testing_service.latest_discovery(db, repo_id)
    if d is None:
        return None, {}
    rows = {t.rel_path: t for t in db.scalars(select(TestFile).where(TestFile.discovery_id == d.id)) if t.parse_status == "ok" and t.test_count > 0}
    return d, rows


def plan(db: Session, settings: Settings, repo: Repository, root: Path, cs: changes.ChangeSet, mode: str, rules: dict[str, Any],
         coverage: bool) -> Plan:
    if mode not in MODES:
        raise CvInvalid("invalid_mode", f"mode must be one of {', '.join(MODES)}.")
    notes: list[str] = []
    py = [f for f in cs.files if f.is_python]
    unverified = [f.path for f in cs.files if not f.is_python and not f.is_doc]
    docs_only = bool(cs.files) and not py and not unverified
    checks: list[PlannedCheck] = []

    # tier 0: syntax
    syn = PlannedCheck("syntax", 0, 0, True, "Parse every changed Python file; a file that does not parse cannot be verified further.", "high")
    if not [f for f in py if f.new_text is not None]:
        syn.status, syn.result = "not_applicable", {"detail": "No changed Python file has content to parse."}
    checks.append(syn)

    # tier 1: static analysis (new findings against the previous content)
    st = PlannedCheck("static_analysis", 1, 1, bool(rules["static"]["enforce"]),
                      "Run the analyzers on the changed Python files and compare with their previous content.", "medium")
    live = [f for f in py if f.new_text is not None]
    if not live:
        st.status, st.result = "not_applicable", {"detail": "No changed Python file to analyse."}
    elif any(f.status == "modified" and f.old_text is None for f in live):
        st.status = "blocked"
        st.result = {"detail": "The previous content of a modified file is not known (manifest source or unreadable blob), so new findings cannot be separated from old ones."}
    checks.append(st)

    # tier 2: tests
    binding = changes.binding_problems(root, settings, cs)
    tests = PlannedCheck("tests", 2, 2, True, "", "medium", ["test discovery", "published index (standard mode only)"])
    disc, files = _test_files(db, repo.id)
    targets: list[str] | None = None
    impact_info: dict[str, Any] = {}
    changed_py_src = [f.path for f in py if f.path not in files]
    if docs_only:
        tests.status, tests.result = "not_applicable", {"detail": "Only documentation files changed; no test applies."}
        tests.reason = "Documentation-only change: nothing executable changed."
    elif disc is None:
        tests.status, tests.result = "blocked", {"detail": "Run test discovery first so NOVA knows which tests exist."}
        tests.reason = "Tests cannot be selected without a discovery run."
    elif binding:
        tests.status, tests.result = "blocked", {"detail": " ".join(binding)}
        tests.reason = "The tests run on the working tree, which does not match the requested change set."
    elif mode == "full":
        targets = None
        tests.reason = f"Run all {len(files)} discovered test file(s) (full mode)."
        tests.confidence = "high"
        if not files:
            tests.status, tests.result = "blocked", {"detail": "Discovery found no runnable test files."}
    else:
        sel: dict[str, str] = {}
        for p in (f.path for f in py if f.path in files):
            sel[p] = "the test file itself changed"
        for tf in files.values():
            for a in tf.associated_sources:
                if a.get("via") == "import" and a.get("path") in {f.path for f in py}:
                    sel.setdefault(tf.rel_path, f"imports changed module {a['path']}")
        if mode == "standard" and changed_py_src:
            snap = repo_svc.latest_snapshot(db, repo.id, completed_only=True)
            if snap is None:
                notes.append("Standard mode needs a published index to find dependents; only focused tests were selected.")
            else:
                try:
                    imp = refactor_impact.build(db, settings, repo, snap.id, {p: ("python", "", "") for p in changed_py_src}, True, "")
                    deps = imp.get("direct_dependents") or []
                    impact_info = {"direct_dependents": deps[:50], "snapshot_id": snap.id}
                    for tf in files.values():
                        for a in tf.associated_sources:
                            if a.get("via") == "import" and a.get("path") in deps:
                                sel.setdefault(tf.rel_path, f"imports {a['path']}, which depends on a changed module")
                except Exception:  # noqa: BLE001 - dependents are an enhancement; fall back to focused and say so
                    notes.append("Dependents could not be computed; only focused tests were selected.")
        targets = sorted(sel)
        impact_info["selection"] = {k: sel[k] for k in sorted(sel)[:100]}
        if targets:
            tests.reason = f"{len(targets)} test file(s) selected by static import association ({mode} mode). This is association, not proof that they exercise the change."
        else:
            tests.status = "blocked"
            if not cs.files:
                tests.result = {"detail": "No changes were detected against the comparison base, so there is nothing to verify. Use full mode to verify the whole repository."}
                tests.reason = "The change set is empty."
            else:
                tests.result = {"detail": "No discovered test imports the changed modules" + (f" ({', '.join(changed_py_src[:3])})" if changed_py_src else "") + ". Use full mode or add a test."}
                tests.reason = "No test could be linked to the change."
            if unverified and not py:
                tests.result = {"detail": "The changed files are not Python, and no verifier exists for them: " + ", ".join(unverified[:3])}
    checks.append(tests)

    # tier 3: coverage
    thresholds = rules["coverage"]["min_line_percent"] is not None or rules["coverage"]["max_drop_percent"] is not None
    if coverage or thresholds:
        cov = PlannedCheck("coverage", 3, 3, thresholds, "Measure line coverage during the test run (coverage.py, when available).", "high", ["tests"])
        if tests.status != "planned":
            cov.status, cov.result = "blocked" if tests.status == "blocked" else "not_applicable", {"detail": "Coverage needs a test run."}
        checks.append(cov)
    for i, c in enumerate(checks):
        c.ordinal = i
    return Plan(mode, checks, targets, unverified, notes, binding, impact_info)
