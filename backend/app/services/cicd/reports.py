"""Unified review report and release-evidence report, built *only* from persisted records, so the same records always give the same bytes.

``sha256`` covers the canonical JSON body (not the generation time). Output is sanitized (secrets scrubbed, repository root removed),
size-bounded, and never implies a check ran when it did not.
"""
from __future__ import annotations

import hashlib
import json
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.config import Settings
from app.models import (
    ChangeRequest,
    CiReport,
    CiStatusReport,
    CiWorkflowRun,
    CodeReview,
    CvCheck,
    CvRun,
    Repository,
)
from app.security.auth import RequestContext
from app.services import audit
from app.services.analysis.redaction import scrub
from app.services.cicd import service
from app.services.cicd.errors import CiInvalid

SCHEMA = "nova.ci.report/1"
LIST_CAPS = ("changed_files", "findings", "regressions", "status_reports", "checks")


def _sev(rule_id: str) -> tuple[str, str, str, str]:
    try:
        from app.services.analysis.catalog import rule_index
        spec = rule_index()[rule_id][1]
        return spec.severity, spec.title, spec.description, spec.remediation
    except Exception:  # noqa: BLE001
        return "unknown", rule_id, "", ""


def _sanitize(obj: Any, root: str) -> Any:
    if isinstance(obj, str):
        s = obj.replace(root, "<repo>") if root else obj
        return scrub(s, 2000)
    if isinstance(obj, dict):
        return {k: _sanitize(v, root) for k, v in obj.items()}
    if isinstance(obj, list):
        return [_sanitize(v, root) for v in obj]
    return obj


def collect(db: Session, settings: Settings, repo: Repository, cr: ChangeRequest, wf: CiWorkflowRun | None, kind: str) -> dict[str, Any]:
    run = db.get(CvRun, wf.cv_run_id) if wf and wf.cv_run_id else None
    stale = bool(wf and wf.head_sha != cr.head_sha)
    gaps: list[str] = []
    if wf is None:
        gaps.append("No verification workflow exists for this change request. Nothing has been verified.")
    elif stale:
        gaps.append(f"The latest verification is for {wf.head_sha[:8]} but the change request is now at {cr.head_sha[:8]}; its results do not apply to the current revision.")
    if wf is not None and run is None:
        gaps.append("The workflow has no verification run yet (awaiting an authorized start).")
    if run is not None and run.status != "completed":
        gaps.append(f"The verification run is {run.status}; there is no complete evidence.")
    checks: list[dict[str, Any]] = []
    findings: list[dict[str, Any]] = []
    if run is not None:
        for c in db.scalars(select(CvCheck).where(CvCheck.run_id == run.id).order_by(CvCheck.ordinal)):
            checks.append({"check_id": c.check_id, "tier": c.tier, "required": c.required, "status": c.status, "reason": c.reason, "detail": str(c.result.get("detail") or ""),
                           "started_at": c.started_at.isoformat() if c.started_at else None, "finished_at": c.finished_at.isoformat() if c.finished_at else None})
            if c.status in ("blocked", "error", "incomplete", "cancelled", "not_applicable"):
                gaps.append(f"Check '{c.check_id}' is {c.status}: {str(c.result.get('detail') or 'no detail')[:200]}")
        seen: set[tuple[str, str, str]] = set()
        for f in (run.static_result or {}).get("new_findings", []):
            if not isinstance(f, dict):
                continue
            k = (str(f.get("rule_id")), str(f.get("rel_path")), str(f.get("scope")))
            if k in seen:
                continue
            seen.add(k)
            sev, title, why, fix = _sev(k[0])
            findings.append({"source": "static_analysis", "rule_id": k[0], "title": title, "severity": sev, "location": {"path": k[1], "scope": k[2]},
                             "explanation": why, "remediation": fix or "See the rule documentation.", "evidence": {"type": "verification_run", "id": run.id}})
        order = {"critical": 0, "high": 1, "medium": 2, "low": 3, "info": 4, "unknown": 5}
        findings.sort(key=lambda f: (order.get(f["severity"], 5), f["location"]["path"], f["rule_id"]))
    regression = (run.regression if run else None) or {}
    reviews = [{"id": r.id, "status": r.status, "risk_category": r.risk_category, "risk_score": r.risk_score, "files_total": r.files_total, "findings_total": r.findings_total}
               for r in db.scalars(select(CodeReview).where(CodeReview.repository_id == repo.id, CodeReview.head_sha == cr.head_sha, CodeReview.base_sha == cr.base_sha)
                                   .order_by(CodeReview.created_at.desc()).limit(5))] if cr.base_sha else []
    if not reviews:
        gaps.append("No Phase 07 code review exists for this exact base/head pair, so no change-risk assessment is included.")
    if cr.files_source == "unknown":
        gaps.append("The changed-file list is unknown (not available locally and not fetched from the provider).")
    reports = list(db.scalars(select(CiStatusReport).where(CiStatusReport.workflow_run_id == wf.id).order_by(CiStatusReport.created_at))) if wf else []
    body: dict[str, Any] = {
        "schema": SCHEMA, "kind": kind,
        "repository": {"id": repo.id, "name": repo.name},
        "revision": {"head_sha": cr.head_sha, "base_sha": cr.base_sha, "evidence_head_sha": wf.head_sha if wf else None, "evidence_is_current": bool(wf and not stale)},
        "change_request": {"id": cr.id, "provider": cr.provider, "external_id": cr.external_id, "source_branch": cr.source_branch, "target_branch": cr.target_branch,
                           "title": cr.title, "author": cr.author, "state": cr.state, "files_source": cr.files_source},
        "changed_files": (cr.changed_files or [])[: settings.ci_max_changed_files],
        "verification": None if wf is None else {"workflow_run_id": wf.id, "status": wf.status, "mode": wf.mode, "trigger": wf.trigger, "cv_run_id": wf.cv_run_id,
                                                  "created_at": wf.created_at.isoformat(), "finished_at": wf.finished_at.isoformat() if wf.finished_at else None,
                                                  "policy_snapshot": run.policy_snapshot if run else None, "baseline_id": run.baseline_id if run else None},
        "checks": checks,
        "quality_gate": (run.gate_decision if run else None) or None,
        "findings": findings,
        "security_analysis": _security_section(db, repo, cr),
        "regressions": {"counts": regression.get("counts"), "available": regression.get("available"), "baseline": regression.get("baseline"),
                        "items": (regression.get("items") or [])[:100]},
        "affected_tests": {"targets": ((run.plan or {}).get("test_targets") if run else None), "selection": (((run.plan or {}).get("impact") or {}).get("selection") if run else None)},
        "related_reviews": reviews,
        "release_readiness": None if wf is None or wf.readiness is None else {"outcome": "BLOCKED" if stale else wf.readiness, "recorded_outcome": wf.readiness,
                                                                              "stale": stale, "decision": wf.readiness_decision},
        "external_status": [{"provider": r.provider, "state": r.state, "status": r.status, "head_sha": r.head_sha, "context": r.context, "attempts": r.attempts, "error_class": r.error_class}
                            for r in reports],
        "gaps": gaps,
        "limitations": ["Readiness and gate results are decision support, not proof of correctness.", "Only Python/pytest verification is supported.",
                        "Tests are bound to the checked-out revision; NOVA never checks out other revisions."],
    }
    return body


def _canonical(body: dict[str, Any]) -> str:
    return json.dumps(body, sort_keys=True, separators=(",", ":"), default=str)


def _fit(body: dict[str, Any], limit: int) -> tuple[dict[str, Any], bool]:
    truncated = False
    while len(_canonical(body).encode()) > limit:
        key = max((k for k in LIST_CAPS if isinstance(body.get(k), list) and len(body[k]) > 1), key=lambda k: len(_canonical(body[k])), default=None)
        if key is None:
            reg = body.get("regressions") or {}
            if isinstance(reg.get("items"), list) and len(reg["items"]) > 1:
                reg["items"] = reg["items"][: len(reg["items"]) // 2]
            else:
                break
        else:
            body[key] = body[key][: max(1, len(body[key]) // 2)]
        truncated = True
    if truncated:
        body["truncated"] = True
        body["gaps"] = [*body["gaps"], "This report was truncated to fit the size limit; some entries are omitted."]
    return body, truncated


def _m(s: Any) -> str:
    return str(s if s is not None else "").replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;").replace("|", "\\|").replace("`", "'").replace("[", "\\[").replace("]", "\\]").replace("(", "\\(").replace(")", "\\)").replace("\n", " ").replace("\r", " ")[:300]


def markdown(body: dict[str, Any]) -> str:
    rev, cr = body["revision"], body["change_request"]
    L = [f"# NOVA {'release evidence' if body['kind'] == 'release' else 'pull-request review'} report", "",
         f"- Repository: **{_m(body['repository']['name'])}**", f"- Change request: {_m(cr['provider'])} `{_m(cr['external_id'])}` - {_m(cr['title'])} ({_m(cr['state'])})",
         f"- Branches: `{_m(cr['source_branch'])}` into `{_m(cr['target_branch'])}`", f"- Head revision: `{_m(rev['head_sha'])}`  Base: `{_m(rev['base_sha'])}`",
         f"- Evidence is for the current revision: **{'yes' if rev['evidence_is_current'] else 'NO'}**", ""]
    rr = body.get("release_readiness")
    L += ["## Release readiness", "", f"**{_m(rr['outcome']) if rr else 'Not evaluated'}**" + (" (stale: the head changed after this evidence)" if rr and rr["stale"] else ""), ""]
    if rr:
        for r in rr["decision"].get("rules", []):
            L.append(f"- {_m(r['id'])}: **{_m(r['status'])}** - {_m(r['detail'])}")
        L += ["", f"_{_m(rr['decision'].get('meaning'))}_", ""]
    g = body.get("quality_gate")
    L += ["## Quality gate", "", f"**{_m(g['outcome']) if g else 'No decision'}**" + (f" - {_m(g.get('reason'))}" if g else ""), ""]
    L += ["## Checks", ""] + ([f"- {_m(c['check_id'])} ({'required' if c['required'] else 'optional'}): **{_m(c['status'])}** - {_m(c['detail'])}" for c in body["checks"]] or ["- none recorded"]) + [""]
    L += ["## Findings", ""] + ([f"- [{_m(f['severity'])}] {_m(f['title'])} (`{_m(f['rule_id'])}`) in `{_m(f['location']['path'])}`: {_m(f['remediation'])}" for f in body["findings"]] or ["- no new static-analysis findings were recorded (this is not a security conclusion)"]) + [""]
    c = (body["regressions"] or {}).get("counts")
    L += ["## Regressions", "", (", ".join(f"{k}: {v}" for k, v in c.items()) if c else "No regression analysis is available."), ""]
    L += ["## Changed files", ""] + ([f"- {_m(f['status'])} `{_m(f['path'])}`" for f in body["changed_files"]] or ["- unknown or none"]) + [""]
    L += ["## Gaps and missing evidence", ""] + ([f"- {_m(x)}" for x in body["gaps"]] or ["- none recorded"]) + [""]
    L += ["## Limitations", ""] + [f"- {_m(x)}" for x in body["limitations"]] + [""]
    L += [f"_Security analysis performed: {'yes' if body['security_analysis']['performed'] else 'no'}. {_m(body['security_analysis']['note'])}_", ""]
    return "\n".join(L)


def _security_section(db: Session, repo: Repository, cr: ChangeRequest) -> dict[str, Any]:
    from app.models import SecScan

    sc = db.scalar(select(SecScan).where(SecScan.repository_id == repo.id, SecScan.status == "completed", SecScan.head_sha == cr.head_sha, SecScan.dirty.is_(False))
                   .order_by(SecScan.created_at.desc(), SecScan.id.desc()).limit(1))
    if sc is None:
        return {"performed": False, "note": "No completed security scan of a clean working tree exists for this exact revision. Absence of findings above is not a security conclusion."}
    st = (sc.summary or {}).get("state")
    return {"performed": True, "scan_id": sc.id, "state": st, "open_by_severity": (sc.summary or {}).get("open_by_severity", {}), "incomplete_reasons": (sc.summary or {}).get("incomplete_reasons", [])[:5],
            "note": ("A security scan covered this revision; detection is best-effort and not exhaustive." if st == "complete" else
                     "A security scan covered this revision but is PARTIAL; absence of findings is not a result.")}


def generate(db: Session, ctx: RequestContext, settings: Settings, repo: Repository, cr: ChangeRequest, wf: CiWorkflowRun | None, kind: str) -> CiReport:
    ctx.require_write()
    if kind not in ("review", "release"):
        raise CiInvalid("invalid_kind", "kind must be 'review' or 'release'.")
    if kind == "release" and wf is None:
        raise CiInvalid("no_workflow", "A release report needs a verification workflow; none exists, so there is no evidence to report.")
    if wf is not None and wf.status in ("queued", "running"):
        service.refresh(db, settings, repo, wf)
    body = collect(db, settings, repo, cr, wf, kind)
    body = _sanitize(body, repo.root_path)
    body, truncated = _fit(body, settings.ci_report_max_bytes)
    canon = _canonical(body)
    rep = CiReport(repository_id=repo.id, change_request_id=cr.id, workflow_run_id=wf.id if wf else None, kind=kind, head_sha=cr.head_sha, sha256=hashlib.sha256(canon.encode()).hexdigest(),
                   body=json.loads(canon), markdown=markdown(body), size_bytes=len(canon.encode()), truncated=truncated, created_by=ctx.user.id)
    db.add(rep)
    audit.record(db, workspace_id=ctx.workspace.id, actor_user_id=ctx.user.id, action="ci.report_generated", entity_type="ci_report", entity_id=rep.id or "new",
                 details={"repository_id": repo.id, "kind": kind, "head_sha": cr.head_sha, "sha256": rep.sha256[:16]})
    db.commit()
    return rep


def report_out(r: CiReport, full: bool = False) -> dict[str, Any]:
    out = {"id": r.id, "kind": r.kind, "change_request_id": r.change_request_id, "workflow_run_id": r.workflow_run_id, "head_sha": r.head_sha, "sha256": r.sha256,
           "size_bytes": r.size_bytes, "truncated": r.truncated, "created_at": r.created_at}
    if full:
        out.update({"body": r.body, "markdown": r.markdown})
    return out
