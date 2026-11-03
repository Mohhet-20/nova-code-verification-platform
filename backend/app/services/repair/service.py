"""Repair investigation and patch lifecycle: create, evidence, proposal, validate, approve, apply. Verification is in ``verify``."""
from __future__ import annotations

import hashlib
import logging
import threading
from pathlib import Path, PurePosixPath
from typing import Any

from sqlalchemy import select, update
from sqlalchemy.orm import Session

from app.core.config import Settings
from app.core.errors import NotFoundError
from app.db.base import utcnow
from app.models import (
    FailureReport,
    Finding,
    Investigation,
    RepairInvestigation,
    RepairPatch,
    Repository,
    ReviewFinding,
)
from app.security.auth import RequestContext
from app.security.paths import resolve_repository_root
from app.services import audit
from app.services.analysis.redaction import scrub
from app.services.repair import ai, autofix, evidence, proposal
from app.services.repair import patch as patchlib
from app.services.repair.errors import RepairConflict, RepairInvalid, from_patch_error
from app.services.review import findings as review_findings
from app.services.review import gitops

log = logging.getLogger("nova.repair")

INV_TRANSITIONS: dict[str, set[str]] = {
    "open": {"analyzed", "closed"},
    "analyzed": {"analyzed", "proposed", "patched", "closed"},
    "proposed": {"analyzed", "proposed", "patched", "closed"},
    "patched": {"analyzed", "proposed", "patched", "applied", "closed"},
    "applied": {"applied", "verified", "verification_failed", "closed"},
    "verification_failed": {"verification_failed", "analyzed", "proposed", "patched", "verified", "closed"},
    "verified": {"verified", "closed"},
    "closed": {"open"},
}
PATCH_TRANSITIONS: dict[str, set[str]] = {
    "draft": {"validated", "invalid", "rejected"},
    "invalid": {"validated", "invalid", "rejected"},
    "validated": {"validated", "invalid", "awaiting_approval", "rejected"},
    "awaiting_approval": {"awaiting_approval", "validated", "invalid", "approved", "rejected"},
    "approved": {"approved", "invalid", "applying", "rejected"},
    "applying": {"applied", "approved", "invalid"},
    "applied": {"applied", "verified", "verification_failed"},
    "verification_failed": {"verification_failed", "verified"},
    "verified": {"verified", "verification_failed"},
    "rejected": set(),
}
APPLY_LOCKS: dict[str, threading.Lock] = {}
_LOCKS_GUARD = threading.Lock()


def _repo_lock(repo_id: str) -> threading.Lock:
    with _LOCKS_GUARD:
        return APPLY_LOCKS.setdefault(repo_id, threading.Lock())


def move_inv(inv: RepairInvestigation, new: str) -> None:
    if new not in INV_TRANSITIONS.get(inv.status, set()):
        raise RepairConflict("invalid_state_transition", f"An investigation that is '{inv.status}' cannot become '{new}'.",
                             {"from": inv.status, "to": new, "allowed": sorted(INV_TRANSITIONS.get(inv.status, set()))})
    inv.status = new
    if new == "closed":
        inv.closed_at = utcnow()
    elif inv.closed_at is not None and new == "open":
        inv.closed_at = None


def move_patch(p: RepairPatch, new: str) -> None:
    if new not in PATCH_TRANSITIONS.get(p.status, set()):
        raise RepairConflict("invalid_state_transition", f"A patch that is '{p.status}' cannot become '{new}'.",
                             {"from": p.status, "to": new, "allowed": sorted(PATCH_TRANSITIONS.get(p.status, set()))})
    p.status = new


def _audit(db: Session, ctx: RequestContext, action: str, etype: str, eid: str, repo: Repository, inv_id: str, **details: Any) -> None:
    audit.record(db, workspace_id=ctx.workspace.id, actor_user_id=ctx.user.id, action=action, entity_type=etype, entity_id=eid,
                 details={"repository_id": repo.id, "investigation_id": inv_id, **details})


# ------------------------------------------------------------------------------------------------ lookup

def get_investigation(db: Session, repo: Repository, inv_id: str) -> RepairInvestigation:
    inv = db.scalar(select(RepairInvestigation).where(RepairInvestigation.id == inv_id, RepairInvestigation.repository_id == repo.id))
    if inv is None:
        raise NotFoundError("Repair investigation not found.")
    return inv


def get_patch(db: Session, repo: Repository, inv: RepairInvestigation, patch_id: str) -> RepairPatch:
    p = db.scalar(select(RepairPatch).where(RepairPatch.id == patch_id, RepairPatch.investigation_id == inv.id, RepairPatch.repository_id == repo.id))
    if p is None:
        raise NotFoundError("Patch not found.")
    return p


# ------------------------------------------------------------------------------------------------ investigations

def create_investigation(db: Session, ctx: RequestContext, settings: Settings, repo: Repository, *, title: str, description: str, severity: str,
                         failure_id: str | None, finding_id: str | None, review_finding_id: str | None, debug_investigation_id: str | None,
                         hint_files: list[str]) -> RepairInvestigation:
    sources = [x for x in (failure_id, finding_id, review_finding_id, debug_investigation_id) if x]
    if len(sources) > 1:
        raise RepairInvalid("multiple_sources", "Provide at most one source: a failure, a finding, a review finding or a debugging investigation.")
    kind = "manual"
    failure = finding = rfinding = dbg = None
    if failure_id:
        failure = db.scalar(select(FailureReport).where(FailureReport.id == failure_id, FailureReport.repository_id == repo.id))
        kind = "failure"
    elif finding_id:
        finding = db.scalar(select(Finding).where(Finding.id == finding_id, Finding.repository_id == repo.id))
        kind = "finding"
    elif review_finding_id:
        rfinding = db.scalar(select(ReviewFinding).where(ReviewFinding.id == review_finding_id, ReviewFinding.repository_id == repo.id))
        kind = "review_finding"
    elif debug_investigation_id:
        dbg = db.scalar(select(Investigation).where(Investigation.id == debug_investigation_id, Investigation.repository_id == repo.id))
        kind = "debug_investigation"
    if kind != "manual" and not (failure or finding or rfinding or dbg):
        raise NotFoundError("The referenced source was not found in this repository.")
    root = resolve_repository_root(repo.root_path, settings.registration_base_dirs)
    clean_hints: list[str] = []
    for h in hint_files:
        p = PurePosixPath(h)
        if not h or "\\" in h or h.startswith("/") or ".." in p.parts or "\x00" in h or str(p) != h:
            raise RepairInvalid("unsafe_path", "A file hint must be a normalized repository-relative path.", [{"field": "hint_files", "message": h[:120]}])
        clean_hints.append(h)
    del root
    inv = RepairInvestigation(repository_id=repo.id, title=scrub(title.strip(), 200), description=scrub(description.strip(), 4000), severity=severity,
                              source_kind=kind, failure_id=failure.id if failure else None, finding_id=finding.id if finding else None,
                              review_finding_id=rfinding.id if rfinding else None, debug_investigation_id=dbg.id if dbg else None,
                              hint_files=clean_hints[:20], created_by=ctx.user.id)
    db.add(inv)
    db.flush()
    _audit(db, ctx, "repair.created", "repair_investigation", inv.id, repo, inv.id, source=kind, source_id=failure_id or finding_id or review_finding_id or debug_investigation_id)
    db.commit()
    return inv


def refresh_evidence(db: Session, ctx: RequestContext, settings: Settings, repo: Repository, inv: RepairInvestigation) -> dict[str, Any]:
    if inv.status == "closed":
        raise RepairConflict("investigation_closed", "The investigation is closed; reopen it to refresh evidence.")
    ev = evidence.assemble(db, settings, repo, inv)
    if inv.status == "open":
        move_inv(inv, "analyzed")
    _audit(db, ctx, "repair.evidence_assembled", "repair_investigation", inv.id, repo, inv.id, state=ev["state"],
           hypotheses=len(ev["hypotheses"]), facts=len(ev["facts"]))
    db.commit()
    return ev


def generate_proposal(db: Session, ctx: RequestContext, settings: Settings, repo: Repository, inv: RepairInvestigation) -> dict[str, Any]:
    if inv.status == "closed":
        raise RepairConflict("investigation_closed", "The investigation is closed; reopen it first.")
    if inv.status in ("applied", "verified"):
        raise RepairConflict("invalid_state_transition", "A patch is already applied; generate a new proposal only after verification fails or the investigation is reopened.")
    doc = proposal.generate(db, settings, repo, inv)
    if inv.status == "open":
        move_inv(inv, "analyzed")
    if inv.status in ("analyzed", "proposed", "verification_failed"):
        move_inv(inv, "proposed")
    _audit(db, ctx, "repair.proposal_generated", "repair_investigation", inv.id, repo, inv.id, state=doc["state"], patch_available=doc["patch_available"],
           ai_assisted=False)
    db.commit()
    return doc


def set_investigation_status(db: Session, ctx: RequestContext, repo: Repository, inv: RepairInvestigation, status: str) -> RepairInvestigation:
    if status not in ("open", "closed"):
        raise RepairInvalid("invalid_status", "Only 'open' (reopen) and 'closed' can be set directly.")
    if status == "open" and inv.status != "closed":
        raise RepairConflict("invalid_state_transition", "Only a closed investigation can be reopened.")
    old = inv.status
    move_inv(inv, status if status == "closed" else "open")
    if status == "open":
        inv.status = "analyzed" if inv.evidence else "open"
    _audit(db, ctx, "repair.status_changed", "repair_investigation", inv.id, repo, inv.id, from_status=old, to_status=inv.status)
    db.commit()
    return inv


# ------------------------------------------------------------------------------------------------ patches

def _allowed(inv: RepairInvestigation, p: RepairPatch | None = None, extra: list[str] | None = None) -> set[str]:
    return set(inv.relevant_files) | set(extra if extra is not None else (p.extra_files if p else []))


def _bind(root: Path, settings: Settings, diff_text: str) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    """Parse and bind a diff to the files on disk now: returns (file records, originals). Raises PatchError for structural problems."""
    fps = patchlib.parse(diff_text, settings.repair_max_patch_bytes, settings.repair_max_files)
    files: list[dict[str, Any]] = []
    originals: dict[str, Any] = {}
    for fp in fps:
        target = patchlib.check_path(root, fp.path, settings)
        if fp.creates:
            files.append({"path": fp.path, "change_type": "create", "base_sha256": None, "result_sha256": None, "added": fp.added, "removed": 0})
            originals[fp.path] = None
        else:
            text, data = patchlib.read_text_file(target, fp.path, settings)
            files.append({"path": fp.path, "change_type": "modify", "base_sha256": patchlib.sha256_bytes(data), "result_sha256": None,
                          "added": fp.added, "removed": fp.removed})
            originals[fp.path] = text
    return files, originals


def create_patch(db: Session, ctx: RequestContext, settings: Settings, repo: Repository, inv: RepairInvestigation, *, source: str,
                 title: str | None, diff: str | None, extra_files: list[str], rule_id: str | None, rel_path: str | None,
                 line: int | None) -> RepairPatch:
    if inv.status in ("closed", "applied", "verified"):
        raise RepairConflict("invalid_state_transition", f"A new patch cannot be created while the investigation is '{inv.status}'.")
    pending = db.scalar(select(RepairPatch.id).where(RepairPatch.investigation_id == inv.id,
                                                      RepairPatch.status.in_(("applying", "applied"))).limit(1))
    if pending:
        raise RepairConflict("patch_already_applied", "Another patch for this investigation is applied or being applied.")
    root = resolve_repository_root(repo.root_path, settings.registration_base_dirs)
    rule = None
    summary = ""
    ai_provenance: dict[str, Any] | None = None
    if source == "generated":
        fix = proposal.fix_for(db, settings, repo, inv, rule_id, rel_path, line)
        diff_text, rule = fix.diff, fix.rule_id
        title = title or f"{autofix.RULES[fix.rule_id]['title']} in {fix.rel_path}:{fix.line}"
        summary = f"Deterministic fix for {fix.rule_id} at {fix.rel_path}:{fix.line}. Verified by re-analysis; behavior is not verified until tests pass."
        if fix.rel_path not in inv.relevant_files:
            extra_files = list(dict.fromkeys([*extra_files, fix.rel_path]))
    elif source == "ai_assisted":
        proposal_ai = ai.propose(db, settings, repo, inv)  # Claude proposes; everything below (binding, validation, approval) still applies
        diff_text, title, summary = proposal_ai.diff, title or proposal_ai.title, proposal_ai.summary
        ai_provenance = proposal_ai.provenance
    else:
        if not diff:
            raise RepairInvalid("invalid_patch", "A manual patch needs the unified diff text.")
        diff_text = diff.replace("\r\n", "\n") if "\r\n" in diff and all(x.endswith("\r") for x in diff.split("\n")[:-1] if x) else diff
        title = title or "Manual patch"
        summary = "Patch supplied by the requester."
    if not diff_text.endswith("\n"):
        diff_text += "\n"
    for e in extra_files:
        patchlib.check_path(root, e, settings)
    try:
        files, originals = _bind(root, settings, diff_text)
    except patchlib.PatchError as exc:
        raise from_patch_error(exc) from exc
    try:
        head = gitops.require_git(root, settings)
    except Exception:  # noqa: BLE001 - Git is optional; the content hashes are the real binding
        head = None
    p = RepairPatch(investigation_id=inv.id, repository_id=repo.id, source=source, rule_id=rule, title=scrub(title, 200), summary=summary[:1000],
                    status="draft", diff_text=diff_text, patch_sha256=hashlib.sha256(diff_text.encode("utf-8")).hexdigest(), files=files,
                    originals=originals, base_head=head, extra_files=extra_files, validation={}, created_by=ctx.user.id)
    db.add(p)
    db.flush()
    if inv.status in ("analyzed", "proposed", "verification_failed", "open"):
        if inv.status == "open":
            move_inv(inv, "analyzed")
        move_inv(inv, "patched")
    elif inv.status == "patched":
        move_inv(inv, "patched")
    _audit(db, ctx, "repair.patch_created", "repair_patch", p.id, repo, inv.id, source=source, sha256=p.patch_sha256[:16],
           files=[f["path"] for f in files], rule_id=rule, **({"ai": ai_provenance} if ai_provenance else {}))
    db.commit()
    return p


def _integrity(p: RepairPatch) -> None:
    if hashlib.sha256(p.diff_text.encode("utf-8")).hexdigest() != p.patch_sha256:
        raise RepairConflict("patch_tampered", "The stored patch no longer matches its recorded hash. It was changed after it was created and will not be used.",
                             {"expected": p.patch_sha256[:16]})


def _static_check(root: Path, results: list[patchlib.FileResult]) -> dict[str, Any]:
    """New findings the patched Python files would have compared with the originals (read-only; in memory)."""
    new_found: list[dict[str, str]] = []
    resolved = 0
    try:
        for r in results:
            if not r.path.endswith(".py"):
                continue
            after = review_findings.analyze(root, [(r.path, r.result.encode("utf-8"))])
            before = review_findings.analyze(root, [(r.path, (r.original or "").encode("utf-8"))]) if r.original is not None else None
            counts_before: dict[tuple[str, str, str], int] = {}
            for _, x in (before.findings.get(r.path, []) if before else []):
                k = (x.rule_id, x.scope, x.discriminator)
                counts_before[k] = counts_before.get(k, 0) + 1
            counts_after: dict[tuple[str, str, str], int] = {}
            for _, x in after.findings.get(r.path, []):
                k = (x.rule_id, x.scope, x.discriminator)
                counts_after[k] = counts_after.get(k, 0) + 1
            for k, n in counts_after.items():
                if n > counts_before.get(k, 0):
                    new_found.append({"rel_path": r.path, "rule_id": k[0], "scope": k[1]})
            resolved += sum(max(0, n - counts_after.get(k, 0)) for k, n in counts_before.items())
        return {"available": True, "new_findings": new_found[:20], "resolved_findings": resolved}
    except Exception:  # noqa: BLE001 - an analyzer problem must not block validation, but is reported
        return {"available": False, "new_findings": [], "resolved_findings": 0, "note": "Static analysis could not be run on the patched files."}


static_check = _static_check  # shared with the refactoring subsystem


def validate(db: Session, ctx: RequestContext | None, settings: Settings, repo: Repository, inv: RepairInvestigation, p: RepairPatch) -> RepairPatch:
    """Check the patch against the current files without touching them. Moves the patch to validated or invalid."""
    if p.status in ("applying", "applied", "verification_failed", "verified", "rejected"):
        raise RepairConflict("invalid_state_transition", f"A patch that is '{p.status}' cannot be validated.")
    _integrity(p)
    root = resolve_repository_root(repo.root_path, settings.registration_base_dirs)
    tmp_base = Path(settings.artifact_dir) / "repair-tmp"
    bases = {f["path"]: f["base_sha256"] for f in p.files}
    checks = ["hash_matches", "paths_inside_repository", "no_symlinks", "allowed_file_types", "files_expected_for_investigation",
              "context_matches_exactly", "source_unchanged_since_creation", "result_parses"]
    now = utcnow()
    try:
        results = patchlib.validate_isolated(root, settings, tmp_base, p.diff_text, _allowed(inv, p), bases)
    except patchlib.PatchError as exc:
        p.validation = {"ok": False, "checked_at": now.isoformat(), "errors": [{"code": exc.code, "message": exc.message, "details": exc.details[:5]}],
                        "stale": exc.code == "stale", "working_tree_modified": False, "checks_attempted": checks}
        p.validated_at = now
        if p.status != "invalid":
            move_patch(p, "invalid")
        if ctx:
            _audit(db, ctx, "repair.patch_validated", "repair_patch", p.id, repo, inv.id, ok=False, code=exc.code)
        db.commit()
        return p
    for f in p.files:
        r = next(x for x in results if x.path == f["path"])
        f["result_sha256"] = r.result_sha256
    p.files = [dict(f) for f in p.files]  # reassign so the JSON change is persisted
    static = _static_check(root, results)
    warnings = [w for r in results for w in r.warnings]
    if static["new_findings"]:
        warnings.append(f"The patch would introduce {len(static['new_findings'])} new static-analysis finding(s).")
    p.validation = {"ok": True, "checked_at": now.isoformat(), "errors": [], "warnings": warnings, "stale": False, "working_tree_modified": False,
                    "checks_passed": checks, "static_check": static, "files": [{"path": r.path, "change_type": r.change_type, "added": r.added,
                                                                               "removed": r.removed} for r in results]}
    p.validated_at = now
    if p.status in ("draft", "invalid", "validated", "awaiting_approval"):
        move_patch(p, "validated")
    if ctx:
        _audit(db, ctx, "repair.patch_validated", "repair_patch", p.id, repo, inv.id, ok=True, files=len(results))
    db.commit()
    return p


def submit(db: Session, ctx: RequestContext, settings: Settings, repo: Repository, inv: RepairInvestigation, p: RepairPatch) -> RepairPatch:
    ctx.require_write()
    if p.status == "awaiting_approval":
        raise RepairConflict("already_submitted", "The patch is already awaiting approval.")
    if p.status != "validated":
        raise RepairConflict("invalid_state_transition", f"Only a validated patch can be submitted for approval (this one is '{p.status}').")
    validate(db, ctx, settings, repo, inv, p)
    if p.status != "validated":
        raise RepairConflict("stale_source" if p.validation.get("stale") else "validation_failed",
                             "The patch no longer validates; it was not submitted.", p.validation.get("errors"))
    move_patch(p, "awaiting_approval")
    p.submitted_at = utcnow()
    _audit(db, ctx, "repair.patch_submitted", "repair_patch", p.id, repo, inv.id)
    db.commit()
    return p


def approve(db: Session, ctx: RequestContext, settings: Settings, repo: Repository, inv: RepairInvestigation, p: RepairPatch) -> RepairPatch:
    ctx.require_write()
    if p.status == "approved":
        raise RepairConflict("already_approved", "The patch is already approved.")
    if p.status != "awaiting_approval":
        raise RepairConflict("invalid_state_transition", f"Only a patch awaiting approval can be approved (this one is '{p.status}').")
    validate(db, ctx, settings, repo, inv, p)  # approval is only valid for content that is still current
    if p.status != "validated":
        raise RepairConflict("stale_source" if p.validation.get("stale") else "validation_failed",
                             "The patch no longer validates against the current files, so it was not approved. Revalidate and resubmit.",
                             p.validation.get("errors"))
    p.status = "awaiting_approval"  # validate() moved it to validated; approval continues from the submitted state
    move_patch(p, "approved")
    p.approved_by, p.approved_at = ctx.user.id, utcnow()
    _audit(db, ctx, "repair.patch_approved", "repair_patch", p.id, repo, inv.id, sha256=p.patch_sha256[:16])
    db.commit()
    return p


def reject(db: Session, ctx: RequestContext, repo: Repository, inv: RepairInvestigation, p: RepairPatch, reason: str) -> RepairPatch:
    ctx.require_write()
    if len(reason.strip()) < 5:
        raise RepairInvalid("reason_required", "A rejection needs a reason of at least 5 characters.")
    if p.status == "rejected":
        raise RepairConflict("already_rejected", "The patch is already rejected.")
    move_patch(p, "rejected")
    p.rejected_by, p.rejected_at, p.reject_reason = ctx.user.id, utcnow(), scrub(reason.strip(), 500)
    _audit(db, ctx, "repair.patch_rejected", "repair_patch", p.id, repo, inv.id, reason=p.reject_reason)
    db.commit()
    return p


def recovery(p: RepairPatch, results: list[patchlib.FileResult] | None = None) -> dict[str, Any]:
    reverse = ""
    if results:
        reverse = patchlib.reverse_diff(results)
    created = [f["path"] for f in p.files if f["change_type"] == "create"]
    return {
        "originals_retained": True,
        "files": [{"path": f["path"], "change_type": f["change_type"], "before_sha256": f["base_sha256"], "after_sha256": f["result_sha256"]} for f in p.files],
        "reverse_diff": reverse[:20000],
        "instructions": [
            "NOVA did not commit anything. To undo this patch, restore the original content of the files listed above.",
            "If the files are tracked by Git: review `git diff`, then `git checkout -- <path>` restores a modified file (this discards other uncommitted edits to it).",
            "Without Git: apply the reverse diff shown here (`git apply -R` with the original patch, or `patch -R -p1`).",
            *( [f"Created files to delete by hand: {', '.join(created)}."] if created else [] ),
            "The original text of each patched file is retained by NOVA with this patch for audit.",
        ],
    }


def apply(db: Session, ctx: RequestContext, settings: Settings, repo: Repository, inv: RepairInvestigation, p: RepairPatch, confirm: bool) -> RepairPatch:
    ctx.require_write()
    if not confirm:
        raise RepairInvalid("confirmation_required", "Applying a patch changes files in the repository. Send confirm=true to proceed.")
    if p.status in ("applied", "verification_failed", "verified"):
        raise RepairConflict("already_applied", "This patch has already been applied.")
    if p.status == "applying":
        raise RepairConflict("apply_in_progress", "This patch is being applied by another request.")
    if p.status != "approved":
        raise RepairConflict("approval_required", f"A patch must be approved before it can be applied (this one is '{p.status}').")
    _integrity(p)
    root = resolve_repository_root(repo.root_path, settings.registration_base_dirs)
    # claim the patch atomically; only one request can move approved -> applying
    claimed = db.execute(update(RepairPatch).where(RepairPatch.id == p.id, RepairPatch.status == "approved").values(status="applying")).rowcount
    db.commit()
    db.refresh(p)
    if claimed != 1:
        raise RepairConflict("apply_in_progress", "This patch is being applied by another request, or is no longer approved.")
    lock = _repo_lock(repo.id)
    if not lock.acquire(timeout=30):
        p.status = "approved"
        db.commit()
        raise RepairConflict("apply_in_progress", "Another patch is being applied in this repository. Try again shortly.")
    results: list[patchlib.FileResult] = []
    try:
        bases = {f["path"]: f["base_sha256"] for f in p.files}
        results = patchlib.build(root, settings, p.diff_text, _allowed(inv, p), bases)
        for f in p.files:
            expected = f.get("result_sha256")
            r = next(x for x in results if x.path == f["path"])
            if expected and r.result_sha256 != expected:
                raise patchlib.PatchError("patch_tampered", f"The result for {f['path']!r} differs from what was validated.")
        patchlib.write_results(root, settings, results)
    except patchlib.PatchError as exc:
        p.status = "invalid" if exc.code in ("stale", "conflict", "result_invalid", "patch_tampered") else "approved"
        p.validation = {**p.validation, "ok": False, "stale": exc.code == "stale",
                        "errors": [{"code": exc.code, "message": exc.message, "details": exc.details[:5]}], "checked_at": utcnow().isoformat()}
        p.apply_result = {"ok": False, "error": exc.code, "message": exc.message, "files_changed": False}
        _audit(db, ctx, "repair.patch_apply_refused", "repair_patch", p.id, repo, inv.id, code=exc.code)
        db.commit()
        if exc.code == "patch_tampered":
            raise RepairConflict("patch_tampered", exc.message) from exc
        raise from_patch_error(exc) from exc
    except Exception as exc:
        log.warning("repair_apply_failed", extra={"patch_id": p.id, "error": type(exc).__name__})
        patchlib.restore(root, settings, results)
        p.status = "approved"
        p.apply_result = {"ok": False, "error": "apply_failed", "message": "Writing failed; any files already written were restored.", "files_changed": False}
        _audit(db, ctx, "repair.patch_apply_failed", "repair_patch", p.id, repo, inv.id, error=type(exc).__name__)
        db.commit()
        raise RepairConflict("apply_failed", "Writing the files failed and the original content was restored. Nothing was changed.") from exc
    finally:
        lock.release()
    p.status = "applied"
    p.applied_by, p.applied_at = ctx.user.id, utcnow()
    p.apply_result = {"ok": True, "files_changed": True, **recovery(p, results)}
    if inv.status in ("patched", "verification_failed", "analyzed", "proposed"):
        if inv.status in ("analyzed", "proposed"):
            move_inv(inv, "patched")
        move_inv(inv, "applied")
    _audit(db, ctx, "repair.patch_applied", "repair_patch", p.id, repo, inv.id, sha256=p.patch_sha256[:16],
           files=[{"path": f["path"], "before": (f["base_sha256"] or "")[:12], "after": (f["result_sha256"] or "")[:12]} for f in p.files])
    db.commit()
    return p


def rollback_guidance(db: Session, settings: Settings, repo: Repository, p: RepairPatch) -> dict[str, Any]:
    root = resolve_repository_root(repo.root_path, settings.registration_base_dirs)
    results = []
    for f in p.files:
        original = p.originals.get(f["path"])
        try:
            current = patchlib.read_text_file(patchlib.check_path(root, f["path"], settings), f["path"], settings)[0]
        except patchlib.PatchError:
            current = None
        if current is not None and f["change_type"] == "modify":
            results.append(patchlib.FileResult(f["path"], "modify", f["base_sha256"], f["result_sha256"] or "", f["added"], f["removed"], original, current))
    out = recovery(p, results)
    out["applied"] = p.status in ("applied", "verification_failed", "verified")
    out["current_matches_patched_result"] = all(
        (patchlib.sha256_text(r.result) == next(f for f in p.files if f["path"] == r.path)["result_sha256"]) for r in results) if results else None
    return out
