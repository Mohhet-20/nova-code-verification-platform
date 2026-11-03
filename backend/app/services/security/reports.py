"""Security reports (JSON, Markdown, HTML, CSV). Built only from stored scan data; secrets appear only as the redacted preview.

A report states whether the scan was complete. Zero findings from a partial scan is reported as "no findings recorded; scan incomplete", never as clean.
"""
from __future__ import annotations

import csv
import hashlib
import html
import io
import json
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.config import Settings
from app.db.base import utcnow
from app.models import Repository, SecComponent, SecEvaluation, SecFinding, SecReport, SecScan
from app.security.auth import RequestContext
from app.services import audit
from app.services.analysis.redaction import scrub
from app.services.security.errors import SecInvalid, SecNotFound

SCHEMA = "nova.security.report/1"
FORMATS = ("json", "markdown", "html", "csv")
SEV_ORDER = {"critical": 0, "high": 1, "medium": 2, "low": 3, "info": 4}
LIMITS = ["Detection is best-effort and not exhaustive: absence of a finding is not proof of absence of a vulnerability.",
          "Advisory matching covers only the sources listed and only Python and npm manifests; unsupported manifests are listed as coverage gaps.",
          "Findings describe observed patterns and advisory matches; exploitability and impact are not assessed.",
          "NOVA does not test, revoke or rotate any credential."]


def _cell(v: Any) -> str:
    s = "" if v is None else str(v)
    s = s.replace("\r", " ").replace("\n", " ")
    return "'" + s if s[:1] in ("=", "+", "-", "@", "\t") else s


def _md(v: Any) -> str:
    s = "" if v is None else str(v).replace("\r", " ").replace("\n", " ")
    for ch in ("\\", "|", "[", "]", "(", ")", "<", ">", "`", "*", "_"):
        s = s.replace(ch, "\\" + ch)
    return s


def _rows(db: Session, repo_id: str, scan: SecScan) -> list[SecFinding]:
    rows = list(db.scalars(select(SecFinding).where(SecFinding.repository_id == repo_id, SecFinding.last_seen_scan_id == scan.id)))
    rows.sort(key=lambda f: ((f.priority or {}).get("bucket", "P4"), SEV_ORDER.get(f.severity, 9), f.rel_path or "", f.line or 0, f.id))
    return rows


def build(db: Session, repo: Repository, scan: SecScan) -> dict[str, Any]:
    findings = _rows(db, repo.id, scan)
    comps = list(db.scalars(select(SecComponent).where(SecComponent.scan_id == scan.id).order_by(SecComponent.ecosystem, SecComponent.name, SecComponent.version)))
    ev = db.scalar(select(SecEvaluation).where(SecEvaluation.scan_id == scan.id).order_by(SecEvaluation.created_at.desc(), SecEvaluation.id.desc()).limit(1))
    summ = scan.summary or {}
    state = summ.get("state", "unknown")
    if scan.status != "completed":
        headline = "The scan did not complete; there are no results to interpret."
    elif not findings:
        headline = ("No findings were recorded by a complete scan of the supported inputs. This is not a guarantee of security." if state == "complete"
                    else "No findings were recorded, but the scan is INCOMPLETE; absence of findings is not a result.")
    else:
        headline = f"{len(findings)} finding(s) recorded." + (" The scan is INCOMPLETE." if state != "complete" else "")
    return {
        "schema": SCHEMA, "generated_at": utcnow().isoformat(), "repository": {"id": repo.id, "name": repo.name},
        "scan": {"id": scan.id, "status": scan.status, "state": state, "scan_types": scan.scan_types, "head_sha": scan.head_sha, "dirty": scan.dirty, "finished_at": scan.finished_at.isoformat() if scan.finished_at else None,
                 "incomplete_reasons": summ.get("incomplete_reasons", []), "tool_versions": scan.tool_versions, "inventory_digest": scan.inventory_digest},
        "headline": headline, "limitations": LIMITS, "coverage": scan.coverage or {},
        "policy_evaluation": ({"status": ev.status, "policy_version": ev.policy_version, "explanation": (ev.decision or {}).get("explanation"), "evaluated_at": ev.created_at.isoformat(),
                               "missing_data": (ev.decision or {}).get("missing_data", []), "exceptions_applied": (ev.decision or {}).get("exceptions_applied", [])} if ev else None),
        "summary": {"findings": len(findings), "open_by_severity": summ.get("open_by_severity", {}), "components": len(comps)},
        "findings": [{"id": f.id, "kind": f.kind, "rule_id": f.rule_id, "title": f.title, "severity": f.severity, "confidence": f.confidence, "evidence_quality": f.evidence_quality, "status": f.status,
                      "path": f.rel_path, "line": f.line, "priority": {"bucket": (f.priority or {}).get("bucket"), "score": (f.priority or {}).get("score"), "model": (f.priority or {}).get("model")},
                      "preview": (f.evidence or {}).get("preview"), "match_status": (f.evidence or {}).get("match_status"), "advisory_ids": (f.evidence or {}).get("advisory_ids"),
                      "sources": [{k: s.get(k) for k in ("provider", "advisory_id", "url", "retrieved_at")} for s in (f.evidence or {}).get("sources", [])], "remediation": f.remediation}
                     for f in findings],
        "components": [{"ecosystem": c.ecosystem, "name": c.name, "version": c.version, "version_state": c.version_state, "relationship": c.relationship_kind, "scope": c.scope,
                        "assessment": (c.assessment or {}).get("status")} for c in comps],
    }


def render_json(d: dict[str, Any]) -> str:
    return json.dumps(d, indent=2, sort_keys=True, default=str)


def render_markdown(d: dict[str, Any]) -> str:
    s = d["scan"]
    o = [f"# Security report: {_md(d['repository']['name'])}", "", f"**{_md(d['headline'])}**", "", f"- Scan: `{s['id']}` ({_md(s['status'])}, {_md(s['state'])})",
         f"- Revision: {_md(s['head_sha'] or 'unknown')}{' (uncommitted changes present)' if s['dirty'] else ''}", f"- Scan types: {_md(', '.join(s['scan_types']))}"]
    if d["policy_evaluation"]:
        pe = d["policy_evaluation"]
        o.append(f"- Policy v{pe['policy_version']}: **{_md(pe['status'].upper())}**: {_md(pe['explanation'])}")
    if s["incomplete_reasons"]:
        o += ["", "## Coverage gaps", *[f"- {_md(r)}" for r in s["incomplete_reasons"]]]
    o += ["", "## Findings", ""]
    if d["findings"]:
        o += ["| Priority | Severity | Rule | Location | Status | Title |", "|---|---|---|---|---|---|"]
        for f in d["findings"]:
            loc = f"{f['path'] or ''}:{f['line']}" if f["line"] else (f["path"] or "")
            o.append(f"| {_md(f['priority']['bucket'])} | {_md(f['severity'])} | {_md(f['rule_id'])} | {_md(loc)} | {_md(f['status'])} | {_md(f['title'])}{_md(' ' + f['preview']) if f['preview'] else ''} |")
    else:
        o.append("_No findings recorded._ " + _md(d["headline"]))
    o += ["", "## Dependencies", f"{d['summary']['components']} component(s) inventoried."]
    o += ["", "## Limitations", *[f"- {_md(x)}" for x in d["limitations"]], ""]
    return "\n".join(o)


def render_html(d: dict[str, Any]) -> str:
    e = html.escape
    s = d["scan"]
    rows = "".join(f"<tr><td>{e(str(f['priority']['bucket']))}</td><td>{e(f['severity'])}</td><td>{e(f['rule_id'])}</td><td>{e(str(f['path'] or ''))}{(':' + str(f['line'])) if f['line'] else ''}</td>"
                   f"<td>{e(f['status'])}</td><td>{e(f['title'])} {e(f['preview'] or '')}</td></tr>" for f in d["findings"])
    gaps = "".join(f"<li>{e(r)}</li>" for r in s["incomplete_reasons"])
    lim = "".join(f"<li>{e(x)}</li>" for x in d["limitations"])
    pe = d["policy_evaluation"]
    return ("<!doctype html><html lang=\"en\"><head><meta charset=\"utf-8\"><meta http-equiv=\"Content-Security-Policy\" content=\"default-src 'none'; style-src 'unsafe-inline'\">"
            f"<title>Security report: {e(d['repository']['name'])}</title><style>body{{font:14px system-ui;margin:2rem;max-width:70rem}}td,th{{border:1px solid #888;padding:4px 8px;text-align:left}}table{{border-collapse:collapse}}</style></head><body>"
            f"<h1>Security report: {e(d['repository']['name'])}</h1><p><strong>{e(d['headline'])}</strong></p>"
            f"<p>Scan {e(s['id'])} ({e(s['status'])}, {e(s['state'])}); revision {e(str(s['head_sha'] or 'unknown'))}</p>"
            + (f"<p>Policy v{pe['policy_version']}: <strong>{e(pe['status'].upper())}</strong>: {e(str(pe['explanation']))}</p>" if pe else "")
            + (f"<h2>Coverage gaps</h2><ul>{gaps}</ul>" if gaps else "") + "<h2>Findings</h2>"
            + (f"<table><tr><th>Priority</th><th>Severity</th><th>Rule</th><th>Location</th><th>Status</th><th>Title</th></tr>{rows}</table>" if rows else f"<p>No findings recorded. {e(d['headline'])}</p>")
            + f"<h2>Limitations</h2><ul>{lim}</ul></body></html>")


def render_csv(d: dict[str, Any]) -> str:
    buf = io.StringIO()
    w = csv.writer(buf, lineterminator="\n")
    w.writerow(["id", "kind", "rule_id", "severity", "confidence", "status", "priority", "path", "line", "title", "preview", "advisory_ids", "scan_state"])
    for f in d["findings"]:
        w.writerow([_cell(x) for x in (f["id"], f["kind"], f["rule_id"], f["severity"], f["confidence"], f["status"], f["priority"]["bucket"], f["path"], f["line"], f["title"], f["preview"],
                                       ";".join(f["advisory_ids"] or []), d["scan"]["state"])])
    return buf.getvalue()


def generate(db: Session, ctx: RequestContext, settings: Settings, repo: Repository, scan: SecScan, fmt: str) -> SecReport:
    ctx.require_write()
    if fmt not in FORMATS:
        raise SecInvalid("invalid_format", f"Format must be one of {', '.join(FORMATS)}.")
    d = build(db, repo, scan)
    body = scrub({"json": render_json, "markdown": render_markdown, "html": render_html, "csv": render_csv}[fmt](d), settings.security_report_max_bytes + 10)
    raw = body.encode("utf-8")
    truncated = False
    if len(raw) > settings.security_report_max_bytes:
        raw, truncated = raw[: settings.security_report_max_bytes], True
        body = raw.decode("utf-8", "ignore") + ("\n" if fmt != "json" else "") + "\n[report truncated at the size limit]\n"
    r = SecReport(repository_id=repo.id, scan_id=scan.id, format=fmt, sha256=hashlib.sha256(body.encode()).hexdigest(), size_bytes=len(body.encode()), truncated=truncated, body=body, created_by=ctx.user.id)
    db.add(r)
    db.flush()
    audit.record(db, workspace_id=ctx.workspace.id, actor_user_id=ctx.user.id, action="security.report_generated", entity_type="sec_report", entity_id=r.id,
                 details={"repository_id": repo.id, "scan_id": scan.id, "format": fmt, "size": r.size_bytes, "truncated": truncated})
    db.commit()
    return r


def get(db: Session, repo_id: str, rid: str) -> SecReport:
    r = db.scalar(select(SecReport).where(SecReport.id == rid, SecReport.repository_id == repo_id))
    if r is None:
        raise SecNotFound("report_not_found", "Report not found.")
    return r


def out(r: SecReport, body: bool = False) -> dict[str, Any]:
    o = {"id": r.id, "scan_id": r.scan_id, "format": r.format, "sha256": r.sha256, "size_bytes": r.size_bytes, "truncated": r.truncated, "created_at": r.created_at}
    if body:
        o["body"] = r.body
    return o
