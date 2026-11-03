"""Review findings: Phase 04 analyzers run on the reviewed content (not duplicated), plus a few evidence-backed change checks.

Identity: analyzer findings keep the Phase 04 fingerprint, so the same construct is one finding whether it came from this
review's analyzer run or from the stored static-analysis findings. Heuristic findings hash (rule, path, scope, discriminator);
line numbers are never part of an identity.
"""
from __future__ import annotations

import hashlib
import re
from collections import Counter
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from app.core.config import Settings
from app.services.analysis import catalog, fingerprint
from app.services.analysis import config as cfg_mod
from app.services.analysis.analyzers.py_facts import collect_facts
from app.services.analysis.model import RawFinding, RuleContext
from app.services.analysis.orchestrator import parse_python
from app.services.analysis.redaction import scrub

SEV_RANK = {"critical": 0, "high": 1, "medium": 2, "low": 3, "info": 4}
CONF_RANK = {"high": 0, "medium": 1, "low": 2}
CATEGORY = {"correctness": "correctness_defect", "security": "security", "reliability": "reliability",
            "maintainability": "maintainability", "complexity": "maintainability", "performance": "performance"}
CAT_PRIORITY = {"security": 0, "correctness_defect": 1, "reliability": 2, "performance": 3, "testing_gap": 4,
                "maintainability": 5, "informational": 6}
AUTH_RE = re.compile(r"(?i)(?:^|[/_.-])(auth\w*|login|logout|permission\w*|passw\w*|token\w*|session\w*|acl|rbac|oauth\w*|credential\w*|"
                     r"secret\w*|jwt|csrf)(?:$|[/_.-])")
AUTH_NAME_RE = re.compile(r"(?i)(auth|login|logout|permission|passw|token|session|acl|rbac|oauth|credential|secret|jwt|csrf|role|is_admin)")
DATA_RE = re.compile(r"(?i)(?:^|/)(migrations?|alembic|versions)/|(?:^|/)(models?|schema|schemas|entities)(?:\.py|/)")


@dataclass
class Draft:
    """A finding before persistence."""
    fingerprint: str
    rule_id: str
    analyzer_id: str
    rel_path: str
    start_line: int | None
    end_line: int | None
    scope: str
    severity: str
    category: str
    title: str
    explanation: str
    evidence: str
    remediation: str
    limitations: str
    confidence: str
    actionable: bool
    source: str
    related: dict[str, Any] = field(default_factory=dict)


@dataclass
class AnalyzerOutcome:
    findings: dict[str, list[tuple[str, RawFinding]]] = field(default_factory=dict)  # path -> [(fingerprint, raw)]
    analyzed: set[str] = field(default_factory=set)
    evaluated_rules: set[str] = field(default_factory=set)
    errors: list[str] = field(default_factory=list)
    skipped: list[str] = field(default_factory=list)
    config: cfg_mod.EffectiveConfig | None = None


def analyze(root: Path, files: list[tuple[str, bytes]]) -> AnalyzerOutcome:
    """Run every enabled Python rule on the given (path, content) pairs. A failing rule is reported, never hidden."""
    out = AnalyzerOutcome()
    try:
        config = cfg_mod.load_for_repository(root)
    except Exception as exc:  # invalid repo config must not stop the review, but must be visible
        config = cfg_mod.defaults()
        out.errors.append(f"The repository's analysis configuration could not be used ({type(exc).__name__}); defaults were applied.")
    out.config = config
    failed: Counter[str] = Counter()
    for rel, data in files:
        if config.is_excluded(rel):
            out.skipped.append(f"{rel}: excluded by the repository's analysis configuration")
            continue
        parsed, reason = parse_python(data, rel)
        if parsed is None:
            out.skipped.append(f"{rel}: {reason}")
            continue
        facts = collect_facts(parsed.tree)
        raw: list[RawFinding] = []
        for analyzer in catalog.ANALYZERS:
            if "Python" not in analyzer.languages:
                continue
            enabled = config.enabled_options([r.spec.id for r in analyzer.rules])
            if not enabled:
                continue
            res = analyzer.run(lambda options, p=parsed, f=facts: RuleContext(p, options, f), enabled)
            raw.extend(res.findings)
            bad = {rid for rid, _ in res.errors}
            out.evaluated_rules.update(rid for rid in enabled if rid not in bad)
            for rid, exc_name in res.errors:
                failed[f"{analyzer.id}/{rid}: {exc_name}"] += 1
        raw.sort(key=lambda r: (r.line, r.col or 0, r.rule_id, r.discriminator))
        ordinals: Counter[tuple[str, str, str]] = Counter()
        keyed: list[tuple[str, RawFinding]] = []
        for r in raw:
            key = (r.rule_id, r.scope, r.discriminator)
            keyed.append((fingerprint.make(r.rule_id, rel, r.scope, r.discriminator, ordinals[key]), r))
            ordinals[key] += 1
        out.findings[rel] = keyed
        out.analyzed.add(rel)
    for msg, n in sorted(failed.items()):
        out.errors.append(f"Analyzer rule failed on {n} file(s): {msg}")
    return out


def _intersects(r: RawFinding, ranges: list[list[int]]) -> bool:
    end = max(r.end_line, r.line)
    return any(a <= end and b >= r.line for a, b in ranges)


def drafts_from_analyzers(outcome: AnalyzerOutcome, ranges_by_path: dict[str, list[list[int]]], added: set[str],
                          stored: dict[str, Any]) -> tuple[list[Draft], int, list[str]]:
    """Findings on changed lines (all lines for added files). Returns (drafts, resolved_by_change, [fingerprints of pre-existing])."""
    index = catalog.rule_index()
    config = outcome.config or cfg_mod.defaults()
    drafts: list[Draft] = []
    preexisting: list[str] = []
    for rel, keyed in outcome.findings.items():
        for fp, r in keyed:
            if rel not in added and not _intersects(r, ranges_by_path.get(rel, [])):
                continue
            analyzer, spec = index[r.rule_id]
            sev = config.rules[r.rule_id].severity or r.severity or spec.severity
            st = stored.get(fp)
            related: dict[str, Any] = {}
            ev = scrub(r.evidence, 900)
            if st is not None:
                related["finding_ids"] = [st.id]
                related["preexisting"] = True
                preexisting.append(fp)
                ev += f" This construct was already reported by static analysis (status {st.status}); the change touches it."
            drafts.append(Draft(fp, r.rule_id, analyzer.id, rel, r.line, max(r.end_line, r.line), r.scope[:300], sev,
                                CATEGORY.get(spec.category, "maintainability"), spec.title[:200], scrub(r.explanation, 1900), ev[:1500],
                                scrub(spec.remediation, 1000), scrub(spec.limitations, 600), r.confidence or spec.confidence,
                                sev != "info", "analyzer", related))
    resolved = 0
    seen = {fp for keyed in outcome.findings.values() for fp, _ in keyed}
    for fp, st in stored.items():
        if st.rel_path in outcome.analyzed and st.rule_id in outcome.evaluated_rules and fp not in seen and st.status != "resolved":
            resolved += 1
    return drafts, resolved, preexisting


def _fp(*parts: str) -> str:
    return hashlib.sha256("\x00".join(parts).encode("utf-8", "replace")).hexdigest()[:40]


def _params(sig: str) -> list[str]:
    inner = sig[sig.index("(") + 1: sig.rindex(")")] if "(" in sig and ")" in sig else ""
    return [p.strip() for p in inner.split(",") if p.strip()]


def breaking_change(old: str, new: str) -> str | None:
    """Reason a signature change can break existing callers, or None when it is only additive."""
    o, n = _params(old), _params(new)
    name = lambda p: p.lstrip("*").split("=")[0]  # noqa: E731
    on, nn = [name(p) for p in o if p not in ("/", "*")], [name(p) for p in n if p not in ("/", "*")]
    removed = [x for x in on if x not in nn]
    if removed:
        return "parameter(s) removed or renamed: " + ", ".join(removed[:5])
    new_required = [name(p) for p in n if "=" not in p and not p.startswith("*") and p not in ("/", "*") and name(p) not in on]
    if new_required:
        return "new required parameter(s): " + ", ".join(new_required[:5])
    common = [x for x in on if x in nn]
    if common != [x for x in nn if x in common]:
        return "positional order of parameters changed"
    return None


def heuristic_drafts(per_file: list[dict[str, Any]], importers_of: dict[str, list[str]], tests_state: dict[str, Any]) -> list[Draft]:
    """Checks that need only the diff plus index/test data. ``per_file`` entries: path, change_type, is_test, symbols (ChangedSym list)."""
    out: list[Draft] = []
    for f in per_file:
        path, ctype, syms = f["path"], f["change_type"], f["symbols"]
        if f["is_test"]:
            continue
        imps = sorted(set(importers_of.get(path, [])))
        for s in syms:
            if not s.public or s.kind == "module":
                continue
            if s.change_kind == "removed" or (s.change_kind == "modified" and s.signature_old and s.signature_new
                                               and s.signature_old != s.signature_new):
                reason = "was removed" if s.change_kind == "removed" else breaking_change(s.signature_old or "", s.signature_new or "")
                if reason is None:
                    continue
                if s.change_kind != "removed":
                    reason = f"signature changed incompatibly: {reason}"
                conf = "medium" if imps else "low"
                who = (f"{len(imps)} file(s) import this module ({', '.join(imps[:3])}{'…' if len(imps) > 3 else ''})" if imps
                       else "no importing file was found in the index (the index may be incomplete or stale)")
                disc = f"{s.name}:{'removed' if s.change_kind == 'removed' else 'signature'}"
                out.append(Draft(_fp("REV-API-001", path, s.name, disc), "REV-API-001", "review", path, s.start, s.end, s.name, "medium",
                                 "reliability", "Public interface changed incompatibly" if s.change_kind != "removed" else "Public symbol removed",
                                 f"The public symbol {s.name} {reason}. Callers written against the previous interface may fail.",
                                 f"Previous: {s.signature_old}. New: {s.signature_new or 'n/a'}. {who}.",
                                 "Keep the previous signature (or add the new parameters with defaults), or update and test every caller. "
                                 "If the break is intended, record it in the change description.",
                                 "Name-based import data cannot prove that a given caller uses this symbol; dynamic use is not visible.",
                                 conf, True, "heuristic", {}))
        if ctype in ("deleted", "renamed"):
            gone = f.get("old_path") or path
            imps = sorted(set(importers_of.get(gone, [])))
            if imps:
                out.append(Draft(_fp("REV-DEL-001", gone, "", "importers"), "REV-DEL-001", "review", gone, None, None, "", "medium",
                                 "reliability", "Removed module is still imported" if ctype == "deleted" else "Renamed module is still imported by its old path",
                                 f"{gone} was {ctype}, but {len(imps)} indexed file(s) import it: {', '.join(imps[:5])}{'…' if len(imps) > 5 else ''}.",
                                 "Imports come from the repository index; files that are updated in this same change may no longer import it.",
                                 "Update the importing modules (or restore/re-export the module) and run their tests.",
                                 "The index may predate this change; an importer that was edited here may already be fixed.",
                                 "high" if not any(i in f.get("changed_paths", set()) for i in imps) else "medium", True, "heuristic", {}))
        if tests_state.get("discovered") and ctype != "deleted" and f["language"] == "Python":
            changed_code = [s for s in syms if s.change_kind in ("added", "modified") and s.kind != "module"]
            if changed_code and not tests_state["associated"].get(path) and not tests_state["any_test_changed"]:
                names = ", ".join(sorted(s.name for s in changed_code)[:4])
                out.append(Draft(_fp("REV-TEST-001", path, "", "no-tests"), "REV-TEST-001", "review", path, None, None, "", "low",
                                 "testing_gap", "Changed code has no associated tests",
                                 f"{path} changed ({names}{'…' if len(changed_code) > 4 else ''}), no discovered test imports this file, and no test file changed in this review.",
                                 "Based on the latest static test discovery (imports only); nothing was executed.",
                                 "Add tests for the changed behavior (see the test recommendations) and run them through the approved execution workflow.",
                                 "Tests may reach this code indirectly or under a different import path; discovery is static.",
                                 "medium", True, "heuristic", {}))
        hits = AUTH_RE.search(path) is not None or any(AUTH_NAME_RE.search(s.name) for s in syms if s.kind != "module")
        if hits and syms:
            out.append(Draft(_fp("REV-INFO-001", path, "", "auth-area"), "REV-INFO-001", "review", path, None, None, "", "info",
                             "informational", "Change touches authentication or access-control related code",
                             f"{path} or its changed symbols match authentication/authorization naming. This is an observation about where to look, not a finding that the change is wrong.",
                             "Matched on file path and symbol names only.",
                             "Have a reviewer confirm that access checks, error paths and token/session handling are unchanged in intent.",
                             "Name matching cannot tell whether security behavior changed.", "medium", False, "heuristic", {}))
        if DATA_RE.search(path):
            out.append(Draft(_fp("REV-INFO-002", path, "", "data-model"), "REV-INFO-002", "review", path, None, None, "", "info",
                             "informational", "Change touches a database model or migration",
                             f"{path} looks like a model/schema/migration file. Schema changes are hard to roll back and affect running data.",
                             "Matched on the file path only.",
                             "Confirm the migration is reversible and compatible with data and code deployed before it.",
                             "Path matching cannot tell whether the schema actually changed.", "medium", False, "heuristic", {}))
    return out


def prioritize(drafts: list[Draft], settings: Settings) -> tuple[list[Draft], dict[str, Any]]:
    """Deduplicate by fingerprint, apply the confidence threshold and the per-file / per-review bounds."""
    by_fp: dict[str, Draft] = {}
    dup = 0
    for d in drafts:
        cur = by_fp.get(d.fingerprint)
        if cur is None:
            by_fp[d.fingerprint] = d
            continue
        dup += 1
        if (SEV_RANK[d.severity], CONF_RANK[d.confidence]) < (SEV_RANK[cur.severity], CONF_RANK[cur.confidence]):
            d.related = {**cur.related, **d.related}
            by_fp[d.fingerprint] = d
        else:
            cur.related = {**d.related, **cur.related}
    floor = CONF_RANK.get(settings.review_min_confidence, 1)
    kept: list[Draft] = []
    below = 0
    for d in by_fp.values():
        if CONF_RANK[d.confidence] > floor and d.severity not in ("critical", "high"):
            below += 1
            continue
        kept.append(d)
    kept.sort(key=lambda d: (SEV_RANK[d.severity], CONF_RANK[d.confidence], CAT_PRIORITY.get(d.category, 9), d.rel_path, d.start_line or 0, d.fingerprint))
    per_file: Counter[str] = Counter()
    final: list[Draft] = []
    omitted_file = omitted_total = 0
    for d in kept:
        if len(final) >= settings.review_max_findings:
            omitted_total += 1
            continue
        if per_file[d.rel_path] >= settings.review_max_findings_per_file:
            omitted_file += 1
            continue
        per_file[d.rel_path] += 1
        final.append(d)
    return final, {"duplicates_merged": dup, "below_confidence_threshold": below, "omitted_per_file_limit": omitted_file,
                   "omitted_review_limit": omitted_total, "confidence_threshold": settings.review_min_confidence}
