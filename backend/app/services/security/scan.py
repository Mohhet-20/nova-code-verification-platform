"""Security scan orchestration: inventory -> advisory matching -> code rules -> secrets -> finding lifecycle reconciliation.

A scan only reads files. It never executes, imports or installs anything from the scanned repository, and never makes a network request unless an
advisory source was explicitly enabled.
"""
from __future__ import annotations

import fnmatch
import hashlib
import os
import time
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.config import Settings
from app.db.base import utcnow
from app.models import Repository, SecComponent, SecEvent, SecFinding, SecScan, SecSuppression
from app.security.auth import RequestContext
from app.security.paths import resolve_repository_root
from app.services import audit
from app.services.analysis import catalog
from app.services.analysis.redaction import scrub
from app.services.cicd import providers as cip
from app.services.review import gitops
from app.services.security import advisories as adv
from app.services.security import codescan, inventory, policy, priority, secrets_scan
from app.services.security.errors import SecConflict, SecInvalid
from app.services.verification import changes

SCAN_TYPES = ("dependencies", "code", "secrets")
PY_ALIASES = {"pyyaml": "yaml", "pillow": "PIL", "beautifulsoup4": "bs4", "scikit-learn": "sklearn", "opencv-python": "cv2", "python-dateutil": "dateutil", "pyjwt": "jwt",
              "protobuf": "google", "attrs": "attr", "msgpack-python": "msgpack", "pycryptodome": "Crypto", "python-dotenv": "dotenv", "psycopg2-binary": "psycopg2"}
OSV_TRANSPORT: cip.Transport | None = None  # tests inject a mock


def _aware(d: datetime | None) -> datetime | None:
    return d if d is None or d.tzinfo else d.replace(tzinfo=UTC)


def event(db: Session, repo_id: str, etype: str, eid: str, action: str, *, frm: str | None = None, to: str | None = None, reason: str = "", actor: str | None = None,
          details: dict[str, Any] | None = None) -> None:
    db.add(SecEvent(repository_id=repo_id, entity_type=etype, entity_id=eid, action=action, from_status=frm, to_status=to, reason=scrub(reason, 500), actor_user_id=actor, details=details or {}))


# ------------------------------------------------------------------------------------------------ start
def start(db: Session, ctx: RequestContext, settings: Settings, repo: Repository, scan_types: list[str], options: dict[str, Any]) -> SecScan:
    ctx.require_write()
    types = [t for t in SCAN_TYPES if t in scan_types]
    if not types or set(scan_types) - set(SCAN_TYPES):
        raise SecInvalid("invalid_scan_types", f"scan_types must be a non-empty subset of {', '.join(SCAN_TYPES)}.")
    globs = options.get("exclude_globs") or []
    if not isinstance(globs, list) or len(globs) > 50 or any(not isinstance(g, str) or not g or len(g) > 200 or g.startswith("/") or ".." in g.split("/") for g in globs):
        raise SecInvalid("invalid_exclusions", "exclude_globs must be up to 50 relative glob patterns.")
    resolve_repository_root(repo.root_path, settings.registration_base_dirs)  # raises when the repository is not inside the permitted roots
    active = db.scalar(select(SecScan.id).where(SecScan.repository_id == repo.id, SecScan.status.in_(("queued", "running"))).limit(1))
    if active:
        raise SecConflict("scan_in_progress", "A security scan is already queued or running for this repository.", {"scan_id": active})
    scan = SecScan(repository_id=repo.id, status="queued", scan_types=types, options={"exclude_globs": globs, "include_dev": bool(options.get("include_dev", True))}, requested_by=ctx.user.id)
    db.add(scan)
    db.flush()
    audit.record(db, workspace_id=ctx.workspace.id, actor_user_id=ctx.user.id, action="security.scan_started", entity_type="sec_scan", entity_id=scan.id,
                 details={"repository_id": repo.id, "scan_types": types, "exclusions": len(globs)})
    db.commit()
    return scan


def recover_interrupted(db: Session) -> int:
    n = 0
    for s in db.scalars(select(SecScan).where(SecScan.status.in_(("queued", "running")))):
        s.status, s.error, s.finished_at = "failed", "The server restarted before the scan finished.", utcnow()
        n += 1
    db.commit()
    return n


def run_job(session_factory: Callable[[], Session], settings: Settings, scan_id: str) -> None:
    with session_factory() as db:
        scan = db.get(SecScan, scan_id)
        if scan is None or scan.status != "queued":
            return
        scan.status, scan.started_at = "running", utcnow()
        db.commit()
        try:
            _execute(db, settings, scan)
        except Exception as exc:  # noqa: BLE001 - any failure must end the scan visibly, without leaking source text
            db.rollback()
            scan = db.get(SecScan, scan_id)
            if scan is not None:
                scan.status, scan.error, scan.finished_at = "failed", scrub(f"The scan failed ({type(exc).__name__}).", 300), utcnow()
                scan.summary = {"state": "failed", "incomplete_reasons": ["the scan did not finish"], "headline": "The scan failed; there are no results to interpret."}
                db.commit()


# ------------------------------------------------------------------------------------------------ helpers
def _fp(*parts: Any) -> str:
    return hashlib.sha256("|".join(str(p) for p in parts).encode()).hexdigest()[:32]


def _cwe(refs: tuple[str, ...]) -> list[str]:
    return [f"https://cwe.mitre.org/data/definitions/{r.split('-')[1]}.html" for r in refs if r.startswith("CWE-")]


def _walk_py(root: Path, max_files: int) -> tuple[list[str], bool]:
    out: list[str] = []
    root_real = os.path.realpath(root)
    for dirpath, dirnames, filenames in os.walk(root, followlinks=False):
        dirnames[:] = sorted(d for d in dirnames if d not in inventory.PRUNE_DIRS and not os.path.islink(os.path.join(dirpath, d)))
        for fn in sorted(filenames):
            if not fn.endswith(".py"):
                continue
            full = os.path.join(dirpath, fn)
            if os.path.islink(full) or not os.path.realpath(full).startswith(root_real + os.sep):
                continue
            out.append(os.path.relpath(full, root).replace(os.sep, "/"))
            if len(out) >= max_files:
                return out, True
    return out, False


def _glob_hit(rel: str, glob: str) -> bool:
    return fnmatch.fnmatchcase(rel, glob) or (glob.endswith("/**") and rel.startswith(glob[:-2])) or (not glob.startswith("**") and fnmatch.fnmatchcase(rel, "**/" + glob))


# ------------------------------------------------------------------------------------------------ execution
def _execute(db: Session, settings: Settings, scan: SecScan) -> None:
    repo = db.get(Repository, scan.repository_id)
    assert repo is not None
    root = resolve_repository_root(repo.root_path, settings.registration_base_dirs)
    deadline = time.monotonic() + settings.security_time_limit_seconds
    reasons: list[str] = []
    cand: list[dict[str, Any]] = []
    cov: dict[str, Any] = {}
    imports: set[str] = set()
    try:
        fp, fp_warn, head = changes.tree_fingerprint(root, settings)
        scan.head_sha, scan.source_digest = head, fp
        if head:
            st = gitops._g(root, settings, ["status", "--porcelain=v1", "--untracked-files=no"])
            scan.dirty = bool(st.ok and st.stdout.strip())
        if fp_warn:
            reasons.extend(fp_warn)
    except Exception:  # noqa: BLE001 - revision identity is best effort; its absence is recorded, not hidden
        reasons.append("the repository revision could not be determined")
    _, rules, _ = policy.current(db, repo.id)
    w = priority.weights(rules.get("priority_weights"))
    workspace_id = repo.workspace_id
    summary: dict[str, Any] = {}

    if "code" in scan.scan_types:
        files, trunc = _walk_py(root, settings.security_max_files)
        n_ok = n_skip = 0
        problems: list[str] = []
        timed_out = False
        for rel in files:
            if time.monotonic() > deadline:
                timed_out = True
                break
            data, status = codescan.read_regular(root, rel, settings.security_max_file_bytes)
            if data is None:
                n_skip += 1
                problems.append(f"{rel}: not analysed ({status})")
                continue
            hits, problem, mods = codescan.scan_source(rel, data)
            imports |= mods
            if problem:
                problems.append(problem)
                if "not analysed" in problem:
                    n_skip += 1
                    continue
            n_ok += 1
            counter: dict[tuple[str, str, str, str], int] = {}
            index = catalog.rule_index()
            for h in hits:
                spec = index[h.rule_id][1]
                k = (h.rule_id, h.rel_path, h.scope, h.discriminator)
                counter[k] = counter.get(k, 0) + 1
                tainted = h.rule_id in ("PY-SEC-007", "PY-SEC-008", "PY-SEC-012") or (h.rule_id == "PY-SEC-006" and "tainted" in h.discriminator)
                request_reach = tainted and ("request data" in h.explanation or "web route" in h.explanation)
                cand.append({
                    "kind": "code", "rule_id": h.rule_id, "category": "security", "title": spec.title, "description": scrub(h.explanation, 700), "severity": h.severity, "confidence": h.confidence,
                    "evidence_quality": "dataflow" if tainted else ("heuristic" if h.rule_id in ("PY-SEC-010", "PY-SEC-014") else "syntactic"), "rel_path": h.rel_path, "line": h.line,
                    "end_line": h.end_line, "scope": h.scope, "component_key": None,
                    "evidence": {"detail": scrub(h.evidence, 400), "rule_version": spec.version, "why_dangerous": spec.description, "how_to_investigate": spec.limitations,
                                 "assessment": ("untrusted-data flow observed inside one function; exploitability is not established" if tainted else
                                                "a risky syntactic pattern is present; whether it is exploitable depends on where the data comes from and is not established"),
                                 "reachability": "request_reachable" if request_reach else "unknown"},
                    "remediation": spec.remediation, "references": _cwe(spec.references), "fingerprint": _fp("code", h.rule_id, h.rel_path, h.scope, h.discriminator, counter[k]),
                    "scope_kind": "runtime"})
        cov["code"] = {"files_found": len(files), "analysed": n_ok, "skipped": n_skip, "truncated": trunc, "timed_out": timed_out, "problems": problems[:50], "languages": ["Python"],
                       "unsupported": "JavaScript/TypeScript and other languages are not analysed by the security rules"}
        if trunc:
            reasons.append(f"code scan: file limit {settings.security_max_files} reached")
        if timed_out:
            reasons.append("code scan: time limit reached before all files were analysed")
        if n_skip:
            reasons.append(f"code scan: {n_skip} Python file(s) could not be analysed")

    if "secrets" in scan.scan_types and time.monotonic() <= deadline:
        res = secrets_scan.scan_repository(root, repo.id, max_files=settings.security_max_files, max_file_bytes=settings.security_max_file_bytes, exclude_globs=scan.options.get("exclude_globs") or [])
        for h in res.hits:
            cand.append({
                "kind": "secret", "rule_id": h.rule_id, "category": "secrets", "title": f"Possible secret: {h.label}", "description": h.explanation, "severity": h.severity, "confidence": h.confidence,
                "evidence_quality": "syntactic" if h.rule_id == "SEC-SECRET-001" else "heuristic", "rel_path": h.rel_path, "line": h.line, "end_line": h.line, "scope": "", "component_key": None,
                "evidence": {"preview": h.preview, "length": h.length, "label": h.label, "value_stored": False, "rule_version": secrets_scan.VERSION,
                             "assessment": "a value with this shape was found; NOVA did not test whether it is a live credential"},
                "remediation": "Revoke and rotate the credential if it is real, remove it from the repository history, and load it from the environment or a secrets manager. NOVA never rotates or tests credentials.",
                "references": ["https://cwe.mitre.org/data/definitions/798.html"], "fingerprint": _fp("secret", h.rule_id, h.rel_path, h.value_hash), "scope_kind": "runtime"})
        cov["secrets"] = {"scanned": res.scanned, "skipped": res.skipped, "excluded_by_configuration": res.excluded_by_config[:200], "excluded_count": len(res.excluded_by_config),
                          "exclusion_patterns": scan.options.get("exclude_globs") or [], "problems": res.problems, "truncated": res.truncated,
                          "limitation": "Best-effort detection: secrets in unusual formats, encoded values, binary files and files over the size limit are not found."}
        if res.excluded_by_config:
            reasons.append(f"secrets scan: {len(res.excluded_by_config)} file(s) were excluded by configuration (results for them are unknown)")
        if res.truncated:
            reasons.append("secrets scan: file limit reached")
        reasons.extend(res.problems[:3])

    if "dependencies" in scan.scan_types and time.monotonic() <= deadline:
        inv = inventory.build(root, settings.security_max_files, settings.security_max_manifest_bytes, settings.security_max_components)
        scan.inventory_digest = inv.digest
        comps: list[SecComponent] = []
        for c in inv.components:
            if not scan.options.get("include_dev", True) and c.scope == "dev":
                continue
            row = SecComponent(scan_id=scan.id, repository_id=repo.id, ecosystem=c.ecosystem, name=c.name, version=c.version, version_state=c.version_state, relationship_kind=c.relationship,
                               scope=c.scope, manifests=c.manifests, declared=c.declared[:20], paths=c.paths, license=c.license, notes=c.notes[:10])
            comps.append(row)
            db.add(row)
        db.flush()
        pkgs = sorted({(c.ecosystem, c.name) for c in comps})
        results = [p.fetch(pkgs) for p in adv.configured_providers(db, settings, workspace_id, OSV_TRANSPORT)] if pkgs else []
        for r in results:
            if r.name != "workspace-store" and r.records:
                adv.persist(db, workspace_id, r.records)
        matches, assess = adv.match(comps, results)
        by_key: dict[tuple[str, str, str], SecComponent] = {(c.ecosystem, c.name, c.version or ""): c for c in comps}
        status_counts: dict[str, int] = {}
        for key, a in assess.items():
            if key in by_key:
                by_key[key].assessment = a
            status_counts[a["status"]] = status_counts.get(a["status"], 0) + 1
        # the workspace store answers "ok" even when it is empty; only a source that can actually hold advisories counts as having data
        ok_providers = [r for r in results if r.state == "ok" and (r.name != "workspace-store" or r.records)]
        unassessed = status_counts.get("unassessed", 0)
        imported_lower = {m.lower() for m in imports}
        for m in matches:
            comp = by_key.get((m.ecosystem, m.name, m.version or ""))
            sev = m.severity()
            unspecified = sev is None
            alias_mod = PY_ALIASES.get(m.name, m.name.replace("-", "_"))
            reach = "imported" if (m.ecosystem == "PyPI" and (alias_mod.lower() in imported_lower or m.name.replace("-", "_").lower() in imported_lower)) else "unknown"
            ids = m.ids
            fixed = m.fixed()
            sources = [{"provider": r.provider, "advisory_id": r.advisory_id, "url": next((u for u in r.references if "advisories" in u or "osv.dev" in u or "nvd" in u), (r.references[:1] or [None])[0]),
                        "published": r.published, "modified": r.modified, "retrieved_at": (_aware(r.retrieved_at).isoformat() if r.retrieved_at else None), "severity": r.severity,
                        "scoring": {"system": r.scoring_system, "vector": r.score} if r.score else None, "uncertainty": r.uncertainty} for r in m.records]
            summary_text = next((r.summary for r in m.records if r.summary), "")
            cand.append({
                "kind": "dependency", "rule_id": "DEP-ADVISORY" if m.status == "confirmed" else "DEP-ADVISORY-POSSIBLE", "category": "dependency",
                "title": f"{m.name} {m.version or '(unpinned)'}: {ids[0]}" + (" (possible)" if m.status == "possible" else ""),
                "description": scrub(f"{summary_text} {m.reason}.".strip(), 700), "severity": sev or "medium", "confidence": "high" if m.status == "confirmed" else "low",
                "evidence_quality": "exact_version" if m.status == "confirmed" else "heuristic", "rel_path": (comp.manifests[0]["path"] if comp and comp.manifests else None), "line": None, "end_line": None,
                "scope": "", "component_key": f"{m.ecosystem}:{m.name}",
                "evidence": {"match_status": m.status, "reason": m.reason, "version": m.version, "ecosystem": m.ecosystem, "package": m.name, "advisory_ids": ids, "sources": sources,
                             "fixed_versions": fixed, "severity_source": "provided" if not unspecified else "unspecified", "reachability": reach, "paths": (comp.paths if comp else []),
                             "relationship": comp.relationship_kind if comp else None, "assessment": "advisory data matched the installed version; exploitability in this repository is not established"
                             if m.status == "confirmed" else "an advisory exists for this package but the installed version could not be evaluated"},
                "remediation": (f"Upgrade {m.name} to {fixed[0]} or later (verify compatibility)." if fixed else "No fixed version is recorded in the queried sources; review the advisory references."),
                "references": [u for r in m.records for u in r.references][:10], "fingerprint": _fp("dep", m.ecosystem, m.name, m.version, min(ids), m.status),
                "scope_kind": comp.scope if comp else "unknown", "fix_available": bool(fixed)})
        cov["inventory"] = inv.coverage()
        cov["advisories"] = {"providers": adv.summarize_providers(results), "providers_ok": len(ok_providers), "sources": [r.name for r in ok_providers], "components": len(comps),
                             "unassessed": unassessed, "status_counts": status_counts, "matches": len(matches)}
        summary["components"] = len(comps)
        if inv.truncated:
            reasons.append("dependency inventory: limits reached")
        bad_files = [f for f in inv.files if f.status in ("malformed", "too_large", "skipped")]
        unsupported = [f for f in inv.files if f.status == "unsupported"]
        if bad_files:
            reasons.append(f"dependency inventory: {len(bad_files)} manifest file(s) could not be parsed")
        if unsupported:
            reasons.append(f"dependency inventory: {len(unsupported)} unsupported manifest(s) not covered ({', '.join(f.path for f in unsupported[:3])})")
        if not ok_providers and pkgs:
            reasons.append("advisory matching: no advisory source provided data (configure an advisory directory, enable OSV, or import advisories)")
        elif unassessed:
            reasons.append(f"advisory matching: {unassessed} component(s) could not be assessed")
        for r in results:
            if r.state in ("unavailable", "error"):
                reasons.append(f"advisory source {r.name}: {r.state} ({scrub(r.detail, 100)})")
    elif "dependencies" in scan.scan_types:
        reasons.append("dependency analysis was not reached before the time limit")

    if len(cand) > settings.security_max_findings:
        cand.sort(key=lambda c: (priority.score(c, w)["score"]), reverse=True)
        reasons.append(f"finding limit {settings.security_max_findings} reached; {len(cand) - settings.security_max_findings} lower-priority finding(s) were not recorded")
        cand = cand[: settings.security_max_findings]

    seen_ids = _reconcile(db, repo, scan, cand, w, set(scan.scan_types), reasons)
    sev_counts: dict[str, int] = {}
    cat_counts: dict[str, int] = {}
    status_counts2: dict[str, int] = {}
    for f in db.scalars(select(SecFinding).where(SecFinding.id.in_(seen_ids))) if seen_ids else []:
        sev_counts[f.severity] = sev_counts.get(f.severity, 0) + (1 if f.status == "open" else 0)
        cat_counts[f.category] = cat_counts.get(f.category, 0) + (1 if f.status == "open" else 0)
        status_counts2[f.status] = status_counts2.get(f.status, 0) + 1
    state = "complete" if not reasons else "partial"
    scan.summary = {**(scan.summary or {}), **summary, "state": state, "incomplete_reasons": reasons[:30], "findings_total": len(seen_ids), "open_by_severity": sev_counts, "open_by_category": cat_counts,
                    "by_status": status_counts2,
                    "headline": ("No findings were recorded by a complete scan of the supported inputs. This is not a guarantee of security." if state == "complete" and not seen_ids else
                                 "No findings were recorded, but the scan is PARTIAL: absence of findings is not a result." if not seen_ids else f"{sum(sev_counts.values())} open finding(s)." + (" The scan is PARTIAL." if state == "partial" else ""))}
    scan.coverage = cov
    scan.tool_versions = {"inventory": "1", "advisory_matching": "1", "secrets": secrets_scan.VERSION, "priority": priority.MODEL, **codescan.tool_versions()}
    scan.status, scan.finished_at = "completed", utcnow()
    audit.record(db, workspace_id=workspace_id, actor_user_id=scan.requested_by, action="security.scan_completed", entity_type="sec_scan", entity_id=scan.id,
                 details={"repository_id": repo.id, "state": state, "findings": len(seen_ids)})
    db.commit()


def _suppression_match(sups: list[SecSuppression], c: dict[str, Any]) -> SecSuppression | None:
    for s in sups:
        if s.kind == "finding" and s.fingerprint == c["fingerprint"]:
            return s
        if s.kind == "rule_in_path" and s.rule_id == c["rule_id"] and c.get("rel_path") and s.path_glob and _glob_hit(c["rel_path"], s.path_glob):
            return s
    return None


def _reconcile(db: Session, repo: Repository, scan: SecScan, cand: list[dict[str, Any]], w: dict[str, Any], ran: set[str], reasons: list[str]) -> list[str]:
    now = utcnow()
    kinds_complete = {"code": "code" in ran and not any(r.startswith("code scan") for r in reasons), "secret": "secrets" in ran and not any(r.startswith("secrets scan") for r in reasons),
                      "dependency": "dependencies" in ran and not any(r.startswith(("dependency", "advisory")) and "could not be assessed" not in r for r in reasons)}
    sups = [s for s in db.scalars(select(SecSuppression).where(SecSuppression.repository_id == repo.id, SecSuppression.revoked_at.is_(None)))
            if s.expires_at is None or _aware(s.expires_at) > _aware(now)]  # type: ignore[operator]
    # One finding per fingerprint: the same secret value repeated in a file is a single finding, at its first occurrence.
    # Without this the second candidate would be inserted again, hit the unique constraint and fail the whole scan.
    unique: dict[str, dict[str, Any]] = {}
    for c in cand:
        unique.setdefault(c["fingerprint"], c)
    cand = list(unique.values())
    existing = {f.fingerprint: f for f in db.scalars(select(SecFinding).where(SecFinding.repository_id == repo.id))}
    seen: list[str] = []
    suppressed_new: dict[str, int] = {}
    for c in cand:
        f = existing.get(c["fingerprint"])
        sup = _suppression_match(sups, c)
        pr = priority.score({**c, "reachability": c["evidence"].get("reachability", "unknown"), "fix_available": c.get("fix_available", False)}, w)
        if f is None:
            f = SecFinding(repository_id=repo.id, fingerprint=c["fingerprint"], kind=c["kind"], rule_id=c["rule_id"], category=c["category"], title=c["title"][:200],
                           description=c["description"][:1500], severity=c["severity"], confidence=c["confidence"], evidence_quality=c["evidence_quality"], rel_path=c.get("rel_path"),
                           line=c.get("line"), end_line=c.get("end_line"), scope=c.get("scope", "")[:200], component_key=c.get("component_key"), evidence=c["evidence"],
                           remediation=c["remediation"][:1500], references=c.get("references", []), status="open", priority=pr, first_seen_scan_id=scan.id, last_seen_scan_id=scan.id,
                           first_seen_at=now, last_seen_at=now)
            db.add(f)
            db.flush()
            event(db, repo.id, "finding", f.id, "detected", to="open", details={"scan_id": scan.id, "rule_id": f.rule_id})
        else:
            f.title, f.description, f.severity, f.confidence, f.evidence_quality = c["title"][:200], c["description"][:1500], c["severity"], c["confidence"], c["evidence_quality"]
            f.rel_path, f.line, f.end_line, f.evidence, f.priority = c.get("rel_path"), c.get("line"), c.get("end_line"), c["evidence"], pr
            f.remediation, f.references = c["remediation"][:1500], c.get("references", [])
            f.last_seen_scan_id, f.last_seen_at = scan.id, now
            if f.status == "resolved":
                f.status, f.resolution_reason = "open", None
                event(db, repo.id, "finding", f.id, "reopened", frm="resolved", to="open", reason="detected again", details={"scan_id": scan.id})
        if sup is not None and f.status in ("open", "suppressed"):
            if f.status != "suppressed":
                event(db, repo.id, "finding", f.id, "suppressed", frm=f.status, to="suppressed", reason=sup.reason, details={"suppression_id": sup.id, "scan_id": scan.id})
            f.status, f.suppression_id, f.resolution_reason = "suppressed", sup.id, f"suppressed: {sup.reason}"[:300]
            suppressed_new[sup.id] = suppressed_new.get(sup.id, 0) + 1
        elif sup is None and f.status == "suppressed":
            f.status, f.suppression_id, f.resolution_reason = "open", None, None
            event(db, repo.id, "finding", f.id, "unsuppressed", frm="suppressed", to="open", reason="the suppression was revoked or expired", details={"scan_id": scan.id})
        seen.append(f.id)
    seen_set = set(seen)
    for f in existing.values():
        if f.id in seen_set or f.status not in ("open", "suppressed") or not kinds_complete.get(f.kind):
            continue
        f.status, f.resolution_reason = "resolved", "not detected in a complete scan"
        event(db, repo.id, "finding", f.id, "resolved", frm="open", to="resolved", reason="not detected in a complete scan", details={"scan_id": scan.id})
    scan.summary = {**(scan.summary or {}), "suppressions_applied": suppressed_new}
    db.flush()
    return seen
