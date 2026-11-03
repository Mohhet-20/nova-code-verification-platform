"""Safe remediation proposals for security findings.

Nothing here edits the repository. A proposal is a unified diff that is handed to the existing repair workflow (investigation -> patch ->
validation -> explicit approval -> apply with backup/rollback guidance). The security re-check below runs in memory on the post-patch text.
A proposal never claims the change preserves behavior.
"""
from __future__ import annotations

import difflib
import re
from pathlib import Path
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.config import Settings
from app.db.base import utcnow
from app.models import RepairPatch, Repository, SecAdvisory, SecComponent, SecFinding, SecRemediation
from app.security.auth import RequestContext
from app.security.paths import resolve_repository_root
from app.services import audit
from app.services.repair import service as repair
from app.services.security import codescan, versions
from app.services.security.errors import SecConflict, SecNotFound
from app.services.security.scan import event

BEHAVIOR = "NOVA does not verify that this change preserves behavior. Review the diff, run your tests and, for dependencies, regenerate and review the lockfile."
LOCK_NOTE = "Lockfiles are not modified. Regenerate them with your package manager after review; NOVA never installs packages or runs lifecycle scripts."


def _diff(rel: str, old: str, new: str) -> str:
    return "".join(difflib.unified_diff(old.splitlines(keepends=True), new.splitlines(keepends=True), f"a/{rel}", f"b/{rel}", n=3))


def _read(root: Path, rel: str, settings: Settings) -> str | None:
    data, status = codescan.read_regular(root, rel, settings.security_max_file_bytes)
    if data is None:
        return None
    try:
        return data.decode("utf-8")
    except UnicodeDecodeError:
        return None


def get(db: Session, repo_id: str, rid: str) -> SecRemediation:
    r = db.scalar(select(SecRemediation).where(SecRemediation.id == rid, SecRemediation.repository_id == repo_id))
    if r is None:
        raise SecNotFound("remediation_not_found", "Remediation not found.")
    return r


def out(r: SecRemediation, detail: bool = True) -> dict[str, Any]:
    o = {"id": r.id, "finding_id": r.finding_id, "kind": r.kind, "status": r.status, "proposal": r.proposal, "investigation_id": r.investigation_id, "patch_id": r.patch_id,
         "created_at": r.created_at, "updated_at": r.updated_at}
    if detail:
        o |= {"diff_text": r.diff_text, "validation": r.validation}
    return o


def _fixed_candidates(db: Session, workspace_id: str, eco: str, pkg: str, installed: str, fixed: list[str]) -> list[str]:
    """Fixed versions above the installed one that no known advisory still lists as affected, ascending."""
    rows = list(db.scalars(select(SecAdvisory).where(SecAdvisory.workspace_id == workspace_id, SecAdvisory.ecosystem == eco, SecAdvisory.package == pkg)))
    out_: list[str] = []
    for fv in sorted(set(fixed), key=lambda v: (versions.parse(eco, v) is None, versions.parse(eco, v) or 0)):  # type: ignore[arg-type, return-value]
        if versions.parse(eco, fv) is None or (versions.compare(eco, fv, installed) or 0) <= 0:
            continue
        if any(versions.affected(eco, fv, a.ranges or [], a.versions or []) is True for a in rows):
            continue
        out_.append(fv)
    return out_


_REQ = re.compile(r"^(?P<name>[A-Za-z0-9][A-Za-z0-9._-]*)(?P<extras>\[[^\]]*\])?\s*(?P<op>==|>=)\s*(?P<ver>[A-Za-z0-9.!+*_-]+)(?P<tail>\s*(?:;.*|#.*)?)$")


def _edit_requirements(text: str, name: str, new: str) -> tuple[str | None, str]:
    from app.services.security.inventory import norm_name

    lines = text.split("\n")
    hits = []
    for i, ln in enumerate(lines):
        m = _REQ.match(ln.rstrip("\r"))
        if m and norm_name("PyPI", m["name"]) == name:
            hits.append((i, m))
    if len(hits) != 1:
        return None, f"expected exactly one simple '==' or '>=' requirement for {name}; found {len(hits)}"
    i, m = hits[0]
    if "*" in m["ver"]:
        return None, "the requirement uses a wildcard version"
    cr = "\r" if lines[i].endswith("\r") else ""
    lines[i] = f"{m['name']}{m['extras'] or ''}{m['op']}{new}{m['tail']}{cr}"
    return "\n".join(lines), f"{m['name']}{m['op']}{m['ver']} -> {m['name']}{m['op']}{new}"


def _edit_package_json(text: str, name: str, new: str) -> tuple[str | None, str]:
    pat = re.compile(r'("' + re.escape(name) + r'"\s*:\s*")(?P<pre>[\^~]|>=)?(?P<ver>\d+\.\d+\.\d+)(")')
    hits = list(pat.finditer(text))
    if len(hits) != 1:
        return None, f"expected exactly one simple version specifier for {name} in package.json; found {len(hits)}"
    m = hits[0]
    pre = m["pre"] or ""
    return text[: m.start()] + m[1] + pre + new + m[4] + text[m.end():], f"{name}: {pre}{m['ver']} -> {pre}{new}"


def _cannot(reason: str, **extra: Any) -> dict[str, Any]:
    return {"supported": False, "reason": reason, "behavior": BEHAVIOR, **extra}


def _propose_dependency(db: Session, repo: Repository, settings: Settings, root: Path, f: SecFinding) -> tuple[dict[str, Any], str]:
    ev = f.evidence or {}
    eco, pkg, ver = ev.get("ecosystem"), ev.get("package"), ev.get("version")
    if ev.get("match_status") != "confirmed":
        return _cannot("Only advisory matches confirmed against an exact installed version are eligible; this match is only possible."), ""
    if not ver or not eco or not pkg:
        return _cannot("The installed version is unknown."), ""
    cands = _fixed_candidates(db, repo.workspace_id, eco, pkg, ver, ev.get("fixed_versions") or [])
    if not cands:
        return _cannot("The advisory data names no fixed version above the installed one that is itself free of known advisories."), ""
    target = cands[0]
    comp = db.scalar(select(SecComponent).where(SecComponent.scan_id == f.last_seen_scan_id, SecComponent.ecosystem == eco, SecComponent.name == pkg, SecComponent.version == ver))
    if comp is None:
        return _cannot("The component is not part of the latest scan."), ""
    if comp.relationship_kind != "direct":
        return _cannot(f"{pkg} is a transitive dependency. Update the direct dependency that brings it in (paths: {comp.paths[:2]}) and regenerate the lockfile.", paths=comp.paths[:3]), ""
    manifests = [m["path"] for m in comp.manifests if m["path"].endswith(("requirements.txt", ".in", "package.json")) or m["path"].rsplit("/", 1)[-1].startswith("requirements")]
    editable = [m for m in manifests if not m.endswith("lock.json")]
    if len(editable) != 1:
        return _cannot("The dependency is declared in " + (f"{len(editable)} editable manifests" if editable else "a manifest NOVA does not edit (pyproject.toml, lockfiles)") + "; update it manually."), ""
    rel = editable[0]
    text = _read(root, rel, settings)
    if text is None:
        return _cannot(f"{rel} could not be read as text."), ""
    new_text, change = (_edit_package_json if rel.endswith("package.json") else _edit_requirements)(text, pkg, target)
    if new_text is None:
        return _cannot(change), ""
    pc = versions.parse(eco, target)
    breaking = versions.major(eco, target) != versions.major(eco, ver)
    constraint_ok = None
    for d in comp.declared:
        if d.startswith(rel + ":"):
            constraint_ok = True
    proposal = {"supported": True, "kind": "dependency", "package": pkg, "ecosystem": eco, "from_version": ver, "to_version": target, "manifest": rel, "change": change,
                "major_version_change": breaking, "prerelease_target": bool(pc is not None and versions.is_prerelease(eco, target)),
                "advisory_ids": ev.get("advisory_ids"), "basis": "fixed version recorded in advisory data; the lowest fixed version above the installed one that no known advisory lists as affected",
                "alternatives": cands[1:4], "caveats": [LOCK_NOTE, BEHAVIOR, *(["The upgrade crosses a major version and may include breaking changes."] if breaking else [])],
                "declared_constraint_checked": constraint_ok is not None}
    return proposal, _diff(rel, text, new_text)


def _propose_code(root: Path, settings: Settings, f: SecFinding) -> tuple[dict[str, Any], str]:
    rel = f.rel_path or ""
    text = _read(root, rel, settings)
    if text is None or not f.line:
        return _cannot("The source file could not be read."), ""
    lines = text.split("\n")
    if not 1 <= f.line <= len(lines):
        return _cannot("The finding location is outside the file (the file changed since the scan)."), ""
    ln = lines[f.line - 1]
    plan: tuple[str, str] | None = None
    if f.rule_id == "PY-SEC-005" and ln.count("yaml.load(") == 1 and "Loader" not in ln:
        plan = (ln.replace("yaml.load(", "yaml.safe_load("), "yaml.load -> yaml.safe_load")
    elif f.rule_id == "PY-SEC-013" and len(re.findall(r"\bdebug\s*=\s*True\b", ln)) == 1:
        plan = (re.sub(r"\bdebug(\s*=\s*)True\b", r"debug\1False", ln), "debug=True -> debug=False")
    if plan is None:
        return _cannot(f"{f.rule_id} at this location is outside the narrow patterns NOVA rewrites automatically; fix it manually following the remediation guidance."), ""
    lines[f.line - 1] = plan[0]
    new_text = "\n".join(lines)
    hits, _, _ = codescan.scan_source(rel, new_text.encode())
    before, _, _ = codescan.scan_source(rel, text.encode())

    def keyset(hs: list[codescan.CodeHit]) -> dict[tuple[str, str, str], int]:
        d: dict[tuple[str, str, str], int] = {}
        for h in hs:
            k = (h.rule_id, h.scope, h.discriminator)
            d[k] = d.get(k, 0) + 1
        return d

    kb, ka = keyset(before), keyset(hits)
    new = [k for k, n in ka.items() if n > kb.get(k, 0)]
    if new:
        return _cannot("The rewrite would introduce a new security finding; not proposed."), ""
    side = ["yaml.safe_load rejects YAML tags that construct arbitrary Python objects; documents relying on them will fail to load."] if f.rule_id == "PY-SEC-005" else [
        "Behavior that depended on debug mode (reloader, interactive debugger, verbose errors) changes."]
    return {"supported": True, "kind": "code", "rule_id": f.rule_id, "rel_path": rel, "line": f.line, "change": plan[1], "side_effects": side, "caveats": [BEHAVIOR],
            "basis": "deterministic single-line rewrite, re-checked with the security rules"}, _diff(rel, text, new_text)


def propose(db: Session, ctx: RequestContext, settings: Settings, repo: Repository, f: SecFinding) -> SecRemediation:
    ctx.require_write()
    if f.status not in ("open", "accepted_risk"):
        raise SecConflict("finding_not_open", f"A {f.status} finding has nothing to remediate.")
    dup = db.scalar(select(SecRemediation.id).where(SecRemediation.finding_id == f.id, SecRemediation.status.in_(("proposed", "validated", "submitted"))).limit(1))
    if dup:
        raise SecConflict("remediation_exists", "An active remediation proposal already exists for this finding.", {"remediation_id": dup})
    root = resolve_repository_root(repo.root_path, settings.registration_base_dirs)
    if f.kind == "secret":
        proposal, diff = _cannot("Secrets are never rewritten automatically: revoke and rotate the credential, then remove it from history and configuration."), ""
        kind = "code"
    elif f.kind == "dependency":
        proposal, diff = _propose_dependency(db, repo, settings, root, f)
        kind = "dependency"
    else:
        proposal, diff = _propose_code(root, settings, f)
        kind = "code"
    r = SecRemediation(repository_id=repo.id, finding_id=f.id, kind=kind, status="proposed" if proposal.get("supported") else "cannot_fix", proposal=proposal, diff_text=diff, created_by=ctx.user.id)
    db.add(r)
    db.flush()
    event(db, repo.id, "finding", f.id, "remediation_proposed", reason=r.status, actor=ctx.user.id, details={"remediation_id": r.id})
    audit.record(db, workspace_id=ctx.workspace.id, actor_user_id=ctx.user.id, action="security.remediation_proposed", entity_type="sec_remediation", entity_id=r.id,
                 details={"repository_id": repo.id, "finding_id": f.id, "status": r.status, "kind": kind})
    db.commit()
    return r


def submit(db: Session, ctx: RequestContext, settings: Settings, repo: Repository, r: SecRemediation, f: SecFinding) -> SecRemediation:
    """Create a repair investigation + patch from the proposal and validate it. Approval and application stay with the repair workflow."""
    ctx.require_write()
    if r.status != "proposed" or not r.diff_text:
        raise SecConflict("not_submittable", f"A remediation that is '{r.status}' cannot be submitted.")
    p = r.proposal
    path = p.get("manifest") or p.get("rel_path")
    inv = repair.create_investigation(db, ctx, settings, repo, title=f"Security: {f.title}"[:200], description=f"{f.description}\n\nProposed change: {p.get('change')}\n{BEHAVIOR}",
                                      severity=f.severity if f.severity in ("low", "medium", "high", "critical") else "medium", failure_id=None, finding_id=None,
                                      review_finding_id=None, debug_investigation_id=None, hint_files=[path])
    r.investigation_id = inv.id
    try:
        patch = repair.create_patch(db, ctx, settings, repo, inv, source="manual", title=f"Security fix: {p.get('change')}"[:200], diff=r.diff_text, extra_files=[path], rule_id=None,
                                    rel_path=None, line=None)
    except Exception as exc:  # noqa: BLE001 - the original files were never touched; record why the patch could not be created
        r.status, r.validation = "validation_failed", {"ok": False, "stage": "patch_creation", "error": getattr(exc, "message", None) or type(exc).__name__, "files_modified": False}
        r.updated_at = utcnow()
        db.commit()
        return r
    r.patch_id = patch.id
    patch = repair.validate(db, ctx, settings, repo, inv, patch)
    v = dict(patch.validation or {})
    recheck = _recheck(repo, settings, patch, f)
    ok = bool(v.get("ok")) and recheck["ok"]
    r.validation = {"ok": ok, "repair_validation": {"ok": v.get("ok"), "errors": v.get("errors", []), "warnings": v.get("warnings", [])}, "security_recheck": recheck,
                    "files_modified": False, "note": "Validation never writes to the repository. Applying requires explicit approval in the repair workflow."}
    r.status = "submitted" if ok else "validation_failed"
    r.updated_at = utcnow()
    event(db, repo.id, "finding", f.id, "remediation_submitted" if ok else "remediation_validation_failed", actor=ctx.user.id, details={"remediation_id": r.id, "patch_id": patch.id})
    audit.record(db, workspace_id=ctx.workspace.id, actor_user_id=ctx.user.id, action="security.remediation_submitted", entity_type="sec_remediation", entity_id=r.id,
                 details={"repository_id": repo.id, "ok": ok, "patch_id": patch.id, "investigation_id": inv.id})
    db.commit()
    return r


def _recheck(repo: Repository, settings: Settings, patch: RepairPatch, f: SecFinding) -> dict[str, Any]:
    """In-memory: for a code finding, the target rule must be gone from the patched text and nothing new introduced."""
    if f.kind != "code":
        return {"ok": True, "note": "Dependency changes are not re-scanned until the next security scan after the change is applied and the lockfile regenerated."}
    root = resolve_repository_root(repo.root_path, settings.registration_base_dirs)
    text = _read(root, f.rel_path or "", settings)
    from app.services.repair import patch as patchlib

    try:
        results = patchlib.validate_isolated(root, settings, Path(settings.artifact_dir) / "repair-tmp", patch.diff_text, {f.rel_path or ""}, {x["path"]: x["base_sha256"] for x in patch.files})
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "note": f"The patched text could not be produced ({type(exc).__name__})."}
    res = next((r for r in results if r.path == f.rel_path), None)
    if res is None or text is None:
        return {"ok": False, "note": "The patched file was not produced."}
    after, _, _ = codescan.scan_source(f.rel_path or "", res.result.encode())
    still = [h for h in after if h.rule_id == f.rule_id and h.scope == f.scope]
    before, _, _ = codescan.scan_source(f.rel_path or "", text.encode())
    kb = {(h.rule_id, h.scope, h.discriminator) for h in before}
    new = [h.rule_id for h in after if (h.rule_id, h.scope, h.discriminator) not in kb]
    return {"ok": not new and len(still) < len([h for h in before if h.rule_id == f.rule_id and h.scope == f.scope]), "target_rule_remaining": len(still), "new_findings": new[:10]}


def sync(db: Session, repo: Repository, r: SecRemediation) -> SecRemediation:
    """Mirror the repair patch state so the workspace shows approval/application progress."""
    if r.patch_id:
        patch = db.get(RepairPatch, r.patch_id)
        if patch is not None and patch.repository_id == repo.id:
            r.proposal = {**r.proposal, "repair_patch_status": patch.status}
    return r
