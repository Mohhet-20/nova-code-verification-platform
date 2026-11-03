"""Connections, change requests, revision-bound workflows and external status reporting."""
from __future__ import annotations

import hashlib
from datetime import timedelta
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.core.config import Settings
from app.core.errors import NovaError
from app.db.base import utcnow
from app.models import ChangeRequest, CiConnection, CiStatusReport, CiWorkflowRun, CvRun, Repository
from app.security.auth import RequestContext
from app.security.paths import resolve_repository_root
from app.services import audit, git
from app.services.analysis.redaction import scrub
from app.services.cicd import providers, readiness
from app.services.cicd.errors import CiConflict, CiForbidden, CiInvalid, CiNotFound, ProviderError
from app.services.review import gitops
from app.services.verification import policy as cv_policy
from app.services.verification import service as cv_service

HEX = set("0123456789abcdef")
TRANSPORT: providers.Transport | None = None  # tests replace this: it is the single external boundary


def transport_for(settings: Settings) -> providers.Transport:
    return TRANSPORT or providers.HttpTransport(settings.ci_github_api_base)


def provider_for(settings: Settings, name: str) -> providers.Provider:
    return providers.get_provider(name, settings.ci_github_api_base)


# ------------------------------------------------------------------------------------------------ connections
def get_connection(db: Session, repo_id: str, cid: str) -> CiConnection:
    c = db.scalar(select(CiConnection).where(CiConnection.id == cid, CiConnection.repository_id == repo_id))
    if c is None:
        raise CiNotFound("connection_not_found", "Provider connection not found.")
    return c


def connection_out(c: CiConnection) -> dict[str, Any]:
    return {"id": c.id, "provider": c.provider, "external_repo": c.external_repo, "default_branch": c.default_branch, "enabled": c.enabled,
            "enabled_events": c.enabled_events, "status": c.status if c.enabled else "disabled", "status_context": c.status_context,
            "token_env": c.token_env, "token_configured": bool(providers.resolve_secret(c.token_env)),
            "webhook_secret_env": c.webhook_secret_env, "webhook_secret_configured": bool(providers.resolve_secret(c.webhook_secret_env)),
            "capabilities": {"webhooks": c.provider == "github", "status_reporting": c.provider == "github", "file_listing": c.provider == "github", "local_branches": c.provider == "local"},
            "last_validated_at": c.last_validated_at, "last_sync_at": c.last_sync_at, "last_error": c.last_error, "created_at": c.created_at, "updated_at": c.updated_at}


def _need_owner(ctx: RequestContext) -> None:
    if ctx.role not in readiness.ADMIN_ROLES:
        raise CiForbidden("connection_admin_required", "Only a workspace owner may change provider connections.")


def upsert_connection(db: Session, ctx: RequestContext, repo: Repository, *, provider: str, external_repo: str, default_branch: str, enabled_events: list[str],
                      token_env: str, webhook_secret_env: str, status_context: str, enabled: bool, settings: Settings) -> CiConnection:
    _need_owner(ctx)
    provider_for(settings, provider)  # rejects unsupported providers
    if provider == "github" and not providers.EXTERNAL_REPO.match(external_repo):
        raise CiInvalid("invalid_external_repo", "external_repo must look like 'owner/name'.")
    if provider == "local":
        external_repo = ""
        if not repo.is_git:
            raise CiInvalid("not_a_git_repository", "The local provider needs a Git repository.")
    for label, name in (("token_env", token_env), ("webhook_secret_env", webhook_secret_env)):
        if name and not providers.ENV_NAME.match(name):
            raise CiInvalid("invalid_env_name", f"{label} must be the NAME of an environment variable (e.g. NOVA_GITHUB_TOKEN), never the secret itself.")
    if not providers.CONTEXT.match(status_context):
        raise CiInvalid("invalid_status_context", "The status context must start with 'nova/' so unrelated checks are never overwritten.")
    bad = [e for e in enabled_events if e not in ("pull_request",)]
    if bad:
        raise CiInvalid("unsupported_event", f"Unsupported event(s): {', '.join(bad)}. Supported: pull_request.")
    if not providers.REF.match(default_branch or "x"):
        raise CiInvalid("invalid_branch", "default_branch is not a valid branch name.")
    c = db.scalar(select(CiConnection).where(CiConnection.repository_id == repo.id, CiConnection.provider == provider, CiConnection.external_repo == external_repo))
    changed_identity = False
    if c is None:
        c = CiConnection(repository_id=repo.id, provider=provider, external_repo=external_repo, created_by=ctx.user.id)
        db.add(c)
    else:
        changed_identity = (c.token_env, c.webhook_secret_env) != (token_env, webhook_secret_env)
    c.default_branch, c.enabled_events, c.token_env, c.webhook_secret_env = default_branch or "main", sorted(set(enabled_events)), token_env, webhook_secret_env
    c.status_context, c.enabled, c.updated_at = status_context, enabled, utcnow()
    if provider == "local":
        c.status, c.last_validated_at = "validated", utcnow()
    elif c.id is None or changed_identity or c.status != "validated":
        c.status = "unconfigured"  # any change to where credentials come from must be re-validated
    audit.record(db, workspace_id=ctx.workspace.id, actor_user_id=ctx.user.id, action="ci.connection_saved", entity_type="ci_connection",
                 entity_id=c.id or "new", details={"repository_id": repo.id, "provider": provider, "external_repo": external_repo})
    try:
        db.commit()
    except IntegrityError:
        db.rollback()
        raise CiConflict("connection_exists", "That connection already exists.") from None
    return c


def validate_connection(db: Session, ctx: RequestContext, settings: Settings, repo: Repository, c: CiConnection) -> CiConnection:
    _need_owner(ctx)
    prov = provider_for(settings, c.provider)
    token = providers.resolve_secret(c.token_env)
    try:
        info = prov.validate_connection(c.external_repo, token, transport_for(settings), settings.ci_http_timeout_seconds, settings.ci_http_max_response_bytes)
        c.status, c.last_error, c.last_validated_at = "validated", "", utcnow()
        if info.get("default_branch") and c.provider == "github" and c.default_branch == "main":
            c.default_branch = info["default_branch"]
    except ProviderError as exc:
        c.status, c.last_error = ("unconfigured" if exc.error_class == "token_missing" else "error"), scrub(f"{exc.error_class}: {exc.message}", 400)
    c.updated_at = utcnow()
    audit.record(db, workspace_id=ctx.workspace.id, actor_user_id=ctx.user.id, action="ci.connection_validated", entity_type="ci_connection", entity_id=c.id,
                 details={"repository_id": repo.id, "provider": c.provider, "status": c.status})
    db.commit()
    return c


# ------------------------------------------------------------------------------------------------ change requests
def get_cr(db: Session, repo_id: str, crid: str) -> ChangeRequest:
    cr = db.scalar(select(ChangeRequest).where(ChangeRequest.id == crid, ChangeRequest.repository_id == repo_id))
    if cr is None:
        raise CiNotFound("change_request_not_found", "Change request not found.")
    return cr


def get_wf(db: Session, repo_id: str, wid: str) -> CiWorkflowRun:
    wf = db.scalar(select(CiWorkflowRun).where(CiWorkflowRun.id == wid, CiWorkflowRun.repository_id == repo_id))
    if wf is None:
        raise CiNotFound("workflow_not_found", "Workflow run not found.")
    return wf


def latest_wf(db: Session, cr: ChangeRequest, head_only: bool = False) -> CiWorkflowRun | None:
    stmt = select(CiWorkflowRun).where(CiWorkflowRun.change_request_id == cr.id)
    if head_only:
        stmt = stmt.where(CiWorkflowRun.head_sha == cr.head_sha)
    return db.scalar(stmt.order_by(CiWorkflowRun.created_at.desc(), CiWorkflowRun.id.desc()).limit(1))


def _changed_files_git(root: Any, settings: Settings, base: str | None, head: str) -> tuple[list[dict[str, str]], bool, str]:
    if not base:
        return [], False, "unknown"
    res = git.run_git(root, ["--literal-pathspecs", "diff", "--name-status", "-z", "--no-renames", base, head], settings.git_timeout_seconds, settings.git_max_output_bytes)
    if not res.ok:
        return [], False, "unknown"
    parts = [p for p in res.stdout.split("\0") if p]
    names = {"A": "added", "M": "modified", "D": "deleted", "T": "type_changed"}
    files = [{"path": parts[i + 1][:1024], "status": names.get(parts[i][:1], "modified")} for i in range(0, len(parts) - 1, 2)]
    cap = settings.ci_max_changed_files
    return files[:cap], len(files) > cap or res.truncated, "git"


def upsert_cr(db: Session, repo: Repository, conn: CiConnection | None, n: providers.NormalizedChangeRequest, settings: Settings, provider: str) -> tuple[ChangeRequest, str]:
    """Create or update a change request. Returns (row, outcome): created | updated | head_changed | stale_event."""
    cr = db.scalar(select(ChangeRequest).where(ChangeRequest.repository_id == repo.id, ChangeRequest.provider == provider, ChangeRequest.external_id == n.external_id))
    outcome = "updated"
    if cr is None:
        cr = ChangeRequest(repository_id=repo.id, connection_id=conn.id if conn else None, provider=provider, external_id=n.external_id, head_changed_at=utcnow())
        db.add(cr)
        outcome = "created"
    else:
        if n.updated_at and cr.last_event_at and n.updated_at.replace(tzinfo=None) < cr.last_event_at.replace(tzinfo=None):
            return cr, "stale_event"
        if cr.head_sha and cr.head_sha != n.head_sha:
            outcome = "head_changed"
            cr.head_changed_at = utcnow()
    cr.source_branch, cr.target_branch, cr.head_sha, cr.base_sha = n.source_branch, n.target_branch, n.head_sha, n.base_sha
    cr.title, cr.description, cr.author, cr.state = n.title, n.description, n.author, n.state
    if n.updated_at:
        cr.last_event_at = n.updated_at
    cr.updated_at = utcnow()
    try:
        root = resolve_repository_root(repo.root_path, settings.registration_base_dirs)
        files, trunc, src = _changed_files_git(root, settings, n.base_sha, n.head_sha)
    except NovaError:
        files, trunc, src = [], False, "unknown"
    if src == "git" or outcome in ("created", "head_changed"):
        cr.changed_files, cr.files_truncated, cr.files_source = files, trunc, src
        cr.notes = [] if src == "git" else ["Changed files are not available locally; fetch them from the provider with a configured token."]
    db.flush()
    return cr, outcome


def sync_local(db: Session, ctx: RequestContext, settings: Settings, repo: Repository, conn: CiConnection) -> dict[str, Any]:
    ctx.require_write()
    if conn.provider != "local":
        raise CiInvalid("not_local", "Only the local provider synchronizes branches from Git.")
    root = resolve_repository_root(repo.root_path, settings.registration_base_dirs)
    gitops.require_git(root, settings)
    res = git.run_git(root, ["for-each-ref", "--format=%(refname:short) %(objectname)", "refs/heads"], settings.git_timeout_seconds, settings.git_max_output_bytes)
    if not res.ok:
        raise CiConflict("git_unavailable", "Git could not list branches.")
    heads = {}
    for line in res.stdout.splitlines()[:200]:
        name, _, sha = line.rpartition(" ")
        if name and len(sha) == 40 and set(sha) <= HEX and providers.REF.match(name):
            heads[name] = sha
    target = conn.default_branch
    if target not in heads:
        raise CiInvalid("default_branch_missing", f"The default branch '{target[:60]}' does not exist locally.")
    seen: set[str] = set()
    created = updated = head_changed = 0
    for name, sha in sorted(heads.items()):
        if name == target:
            continue
        mb = git.run_git(root, ["merge-base", heads[target], sha], settings.git_timeout_seconds, 1000)
        base = mb.stdout.strip() if mb.ok and len(mb.stdout.strip()) == 40 else heads[target]
        cr, outcome = upsert_cr(db, repo, conn, providers.NormalizedChangeRequest(name, name, target, sha, base, f"Branch {name}", "", "", "open"), settings, "local")
        seen.add(cr.id)
        created += outcome == "created"
        updated += outcome == "updated"
        head_changed += outcome == "head_changed"
    closed = 0
    for cr in db.scalars(select(ChangeRequest).where(ChangeRequest.repository_id == repo.id, ChangeRequest.provider == "local", ChangeRequest.state == "open")):
        if cr.id not in seen:
            cr.state, cr.notes, closed = "closed", [*cr.notes, "The branch no longer exists locally."], closed + 1
    conn.last_sync_at = utcnow()
    audit.record(db, workspace_id=ctx.workspace.id, actor_user_id=ctx.user.id, action="ci.local_sync", entity_type="ci_connection", entity_id=conn.id,
                 details={"created": created, "updated": updated, "head_changed": head_changed, "closed": closed})
    db.commit()
    return {"created": created, "updated": updated, "head_changed": head_changed, "closed": closed, "branches": len(seen)}


def fetch_provider_files(db: Session, ctx: RequestContext, settings: Settings, repo: Repository, cr: ChangeRequest) -> ChangeRequest:
    ctx.require_write()
    conn = db.get(CiConnection, cr.connection_id) if cr.connection_id else None
    if conn is None or conn.provider == "local":
        raise CiInvalid("no_provider", "This change request has no external provider to fetch files from.")
    prov = provider_for(settings, conn.provider)
    try:
        files, trunc = prov.list_changed_files(conn.external_repo, cr.external_id, providers.resolve_secret(conn.token_env), transport_for(settings),
                                               settings.ci_http_timeout_seconds, settings.ci_http_max_response_bytes, settings.ci_max_changed_files)
    except ProviderError as exc:
        raise CiConflict("provider_error", scrub(f"{exc.error_class}: {exc.message}", 300), {"retryable": exc.retryable}) from None
    cr.changed_files, cr.files_truncated, cr.files_source, cr.notes = files, trunc, "provider", []
    db.commit()
    return cr


def cr_out(db: Session, cr: ChangeRequest, detail: bool = False) -> dict[str, Any]:
    wf = latest_wf(db, cr)
    cur = latest_wf(db, cr, head_only=True)
    out = {"id": cr.id, "provider": cr.provider, "external_id": cr.external_id, "source_branch": cr.source_branch, "target_branch": cr.target_branch,
           "head_sha": cr.head_sha, "base_sha": cr.base_sha, "title": cr.title, "author": cr.author, "state": cr.state, "files_count": len(cr.changed_files or []),
           "files_source": cr.files_source, "files_truncated": cr.files_truncated, "head_changed_at": cr.head_changed_at, "updated_at": cr.updated_at,
           "verification": {"latest_run_id": wf.id if wf else None, "latest_head_sha": wf.head_sha if wf else None, "current_revision_verified": cur is not None and cur.status == "completed",
                            "stale": bool(wf and wf.head_sha != cr.head_sha and cur is None),
                            "gate_outcome": cur.gate_outcome if cur else None, "readiness": cur.readiness if cur else None}}
    if detail:
        out.update({"description": cr.description, "changed_files": cr.changed_files, "notes": cr.notes, "connection_id": cr.connection_id})
    return out


# ------------------------------------------------------------------------------------------------ workflows
def _key(cr: ChangeRequest, mode: str, cvv: int, rv: int) -> str:
    return "wf:" + hashlib.sha256(f"{cr.id}|{cr.head_sha}|{cr.base_sha}|{mode}|{cvv}|{rv}".encode()).hexdigest()[:40]


def ensure_awaiting(db: Session, repo: Repository, cr: ChangeRequest) -> CiWorkflowRun | None:
    """Event-driven record that the current head needs verification. Idempotent per (change request, head). Never executes anything."""
    if cr.state != "open" or not cr.head_sha:
        return None
    key = f"event:{cr.id}:{cr.head_sha}"
    wf = db.scalar(select(CiWorkflowRun).where(CiWorkflowRun.repository_id == repo.id, CiWorkflowRun.idempotency_key == key))
    if wf is None:
        wf = CiWorkflowRun(repository_id=repo.id, change_request_id=cr.id, head_sha=cr.head_sha, base_sha=cr.base_sha, mode="focused", trigger="event", idempotency_key=key,
                           status="awaiting")
        db.add(wf)
        db.flush()
    return wf


def start_workflow(db: Session, ctx: RequestContext, settings: Settings, repo: Repository, cr: ChangeRequest, *, mode: str | None, confirm: bool,
                   force: bool = False) -> tuple[CiWorkflowRun, bool]:
    ctx.require_write()
    if not confirm:
        raise CiInvalid("confirmation_required", "Starting verification runs the repository's tests. Send confirm=true to start it.")
    if cr.state != "open":
        raise CiConflict("change_request_not_open", f"The change request is {cr.state}; only open change requests are verified.")
    cvv, rules, _ = cv_policy.current(db, repo.id)
    mode = mode or rules["default_mode"]
    rv = readiness.current(db, repo.id)[0]
    base_key = _key(cr, mode, cvv, rv)
    existing = db.scalar(select(CiWorkflowRun).where(CiWorkflowRun.repository_id == repo.id, CiWorkflowRun.idempotency_key == base_key))
    if existing is not None and not force and existing.status in ("queued", "running", "completed"):
        return refresh(db, settings, repo, existing), True
    awaiting = db.scalar(select(CiWorkflowRun).where(CiWorkflowRun.change_request_id == cr.id, CiWorkflowRun.head_sha == cr.head_sha, CiWorkflowRun.status == "awaiting"))
    try:
        run = cv_service.start(db, ctx, settings, repo, source="revisions", mode=mode, base=cr.base_sha, head=cr.head_sha, paths=None, patch=None, coverage=False,
                               baseline_id=None, confirm=True, link=None)
    except NovaError as exc:
        code = getattr(exc, "code", "")
        if code == "run_in_progress":
            raise CiConflict("verification_in_progress", "Another verification run is active for this repository. Wait for it to finish.") from None
        if code in ("git_unavailable",) or "resolve to a commit" in str(exc) or "valid revision" in str(exc):
            raise CiInvalid("revision_unavailable", "The base or head revision is not available in the local repository (deleted branch, force-push, or not fetched).") from None
        raise
    n = db.scalar(select(func.count()).select_from(CiWorkflowRun).where(CiWorkflowRun.repository_id == repo.id, CiWorkflowRun.idempotency_key.like(base_key + "%"))) or 0
    wf = awaiting or CiWorkflowRun(repository_id=repo.id, change_request_id=cr.id, head_sha=cr.head_sha, base_sha=cr.base_sha, idempotency_key=base_key if n == 0 else f"{base_key}#{n}")
    wf.mode, wf.status, wf.cv_run_id, wf.requested_by = mode, "queued", run.id, ctx.user.id
    wf.head_sha, wf.base_sha = cr.head_sha, cr.base_sha
    if awaiting is None:
        wf.trigger = "manual"
        db.add(wf)
    audit.record(db, workspace_id=ctx.workspace.id, actor_user_id=ctx.user.id, action="ci.workflow_started", entity_type="ci_workflow_run", entity_id=wf.id or "new",
                 details={"repository_id": repo.id, "change_request_id": cr.id, "head_sha": cr.head_sha, "mode": mode})
    db.commit()
    return wf, False


def refresh(db: Session, settings: Settings, repo: Repository, wf: CiWorkflowRun) -> CiWorkflowRun:
    """Bring the workflow in line with its verification run and (once finished) evaluate readiness. Idempotent."""
    if not wf.cv_run_id:
        return wf
    run = db.get(CvRun, wf.cv_run_id)
    if run is None:
        return wf
    wf.gate_outcome = run.gate_outcome
    if run.status in ("queued", "running"):
        wf.status = run.status
    else:
        wf.status = "completed" if run.status == "completed" else "cancelled" if run.status == "cancelled" else "error"
        wf.finished_at = wf.finished_at or run.finished_at or utcnow()
        wf.error = run.reason if wf.status == "error" else wf.error
        if wf.readiness is None:
            evaluate(db, settings, repo, wf)
    db.commit()
    return wf


def evaluate(db: Session, settings: Settings, repo: Repository, wf: CiWorkflowRun) -> dict[str, Any]:
    cr = db.get(ChangeRequest, wf.change_request_id)
    assert cr is not None
    version, rules, _ = readiness.current(db, repo.id)
    decision = readiness.evaluate(db, settings, repo, cr, wf, rules, version)
    wf.readiness, wf.readiness_decision, wf.readiness_policy_version, wf.evaluated_at = decision["outcome"], decision, version, utcnow()
    db.commit()
    return decision


def finalize(db: Session, settings: Settings, repo: Repository, wf_id: str) -> None:
    """Background-job tail: refresh, then record/send the external status when a provider connection exists."""
    wf = db.get(CiWorkflowRun, wf_id)
    if wf is None:
        return
    refresh(db, settings, repo, wf)
    cr = db.get(ChangeRequest, wf.change_request_id)
    if cr is not None and cr.connection_id and wf.status in ("completed", "error", "cancelled"):
        conn = db.get(CiConnection, cr.connection_id)
        if conn is not None and conn.provider != "local" and conn.enabled:
            report_status(db, settings, repo, conn, cr, wf)


def wf_out(db: Session, wf: CiWorkflowRun) -> dict[str, Any]:
    cr = db.get(ChangeRequest, wf.change_request_id)
    stale = bool(cr and cr.head_sha != wf.head_sha)
    eff = "BLOCKED" if stale and wf.readiness else wf.readiness
    return {"id": wf.id, "change_request_id": wf.change_request_id, "head_sha": wf.head_sha, "base_sha": wf.base_sha, "mode": wf.mode, "trigger": wf.trigger, "status": wf.status,
            "cv_run_id": wf.cv_run_id, "gate_outcome": wf.gate_outcome, "readiness": wf.readiness, "readiness_effective": eff, "stale": stale,
            "current_head_sha": cr.head_sha if cr else None, "readiness_policy_version": wf.readiness_policy_version, "evaluated_at": wf.evaluated_at,
            "error": wf.error, "requested_by": wf.requested_by, "created_at": wf.created_at, "finished_at": wf.finished_at}


# ------------------------------------------------------------------------------------------------ external status reporting
def provider_state(wf: CiWorkflowRun, cr: ChangeRequest) -> tuple[str, str]:
    """Conservative mapping. 'success' only for READY + gate PASS on the exact current head; BLOCKED/INCOMPLETE never become success."""
    short = wf.head_sha[:8]
    if wf.status in ("queued", "running", "awaiting"):
        return "pending", f"NOVA verification {wf.status} at {short}"
    if wf.readiness == "READY" and wf.gate_outcome == "PASS" and wf.head_sha == cr.head_sha:
        return "success", f"NOVA: ready (gate PASS) at {short}"
    if wf.readiness == "NOT_READY" or wf.gate_outcome == "FAIL":
        return "failure", f"NOVA: not ready (gate {wf.gate_outcome}) at {short}"
    if wf.readiness == "INCOMPLETE" and wf.gate_outcome != "FAIL":
        return "pending", f"NOVA: incomplete (gate {wf.gate_outcome}) at {short}"
    return "error", f"NOVA: {wf.readiness or 'unevaluated'} (gate {wf.gate_outcome}) at {short}"


def report_out(r: CiStatusReport) -> dict[str, Any]:
    return {"id": r.id, "workflow_run_id": r.workflow_run_id, "provider": r.provider, "head_sha": r.head_sha, "context": r.context, "state": r.state, "description": r.description,
            "nova_gate": r.nova_gate, "nova_readiness": r.nova_readiness, "status": r.status, "attempts": r.attempts, "error_class": r.error_class, "error": r.error,
            "retryable": r.status == "retry_scheduled", "last_error": r.error, "sha": r.head_sha, "response_meta": r.response_meta, "next_retry_at": r.next_retry_at, "created_at": r.created_at, "updated_at": r.updated_at}


def report_status(db: Session, settings: Settings, repo: Repository, conn: CiConnection, cr: ChangeRequest, wf: CiWorkflowRun, *, existing: CiStatusReport | None = None) -> CiStatusReport:
    state, desc = provider_state(wf, cr)
    key = f"{wf.id}:{state}:{conn.status_context}"
    rep = existing or db.scalar(select(CiStatusReport).where(CiStatusReport.workflow_run_id == wf.id, CiStatusReport.idempotency_key == key))
    if rep is not None and rep.status == "sent":
        return rep  # replays never send twice
    if rep is None:
        rep = CiStatusReport(repository_id=repo.id, workflow_run_id=wf.id, connection_id=conn.id, provider=conn.provider, head_sha=wf.head_sha, context=conn.status_context,
                             state=state, description=desc[:140], nova_gate=wf.gate_outcome, nova_readiness=wf.readiness, idempotency_key=key)
        db.add(rep)
    if wf.head_sha != cr.head_sha:
        rep.status, rep.error = "skipped_stale", "The change request moved to a newer commit; this revision is no longer reported."
        db.commit()
        return rep
    prov = provider_for(settings, conn.provider)
    token = providers.resolve_secret(conn.token_env)
    if not (prov.supports_status_reporting and conn.status == "validated" and token and conn.enabled):
        rep.status = "unconfigured"
        rep.error = "Reporting intent recorded; nothing was sent because the connection is not validated or has no token available."
        db.commit()
        return rep
    rep.attempts = (rep.attempts or 0) + 1
    try:
        meta = prov.report_status(conn.external_repo, wf.head_sha, state, desc, conn.status_context, token, transport_for(settings), settings.ci_http_timeout_seconds,
                                  settings.ci_http_max_response_bytes, settings.ci_github_api_base)
        rep.status, rep.response_meta, rep.error, rep.error_class, rep.next_retry_at = "sent", meta, "", "", None
    except ProviderError as exc:
        rep.error_class, rep.error = exc.error_class, scrub(exc.message, 300)
        rep.response_meta = {"http_status": exc.status}
        if exc.retryable and rep.attempts < settings.ci_status_max_attempts:
            rep.status = "retry_scheduled"
            rep.next_retry_at = utcnow() + timedelta(seconds=min(3600, settings.ci_retry_base_seconds * 2 ** (rep.attempts - 1)))
        else:
            rep.status = "failed_permanent"
            rep.next_retry_at = None
            if exc.retryable:
                rep.error = "Retries exhausted: " + rep.error
    rep.updated_at = utcnow()
    db.commit()
    return rep


def retry_report(db: Session, ctx: RequestContext, settings: Settings, repo: Repository, rep: CiStatusReport) -> CiStatusReport:
    ctx.require_write()
    if rep.status not in ("retry_scheduled", "unconfigured"):
        raise CiConflict("not_retryable", f"A status report in state '{rep.status}' cannot be retried.")
    if rep.status == "retry_scheduled" and rep.next_retry_at and utcnow().replace(tzinfo=None) < rep.next_retry_at.replace(tzinfo=None):
        raise CiConflict("retry_too_soon", "The backoff period has not elapsed yet.", {"next_retry_at": rep.next_retry_at.isoformat()})
    wf = get_wf(db, repo.id, rep.workflow_run_id)
    cr = db.get(ChangeRequest, wf.change_request_id)
    conn = db.get(CiConnection, rep.connection_id) if rep.connection_id else None
    if conn is None or cr is None:
        raise CiConflict("connection_missing", "The provider connection no longer exists.")
    rep.status = "pending"
    audit.record(db, workspace_id=ctx.workspace.id, actor_user_id=ctx.user.id, action="ci.status_retry", entity_type="ci_status_report", entity_id=rep.id, details={"workflow_run_id": wf.id})
    return report_status(db, settings, repo, conn, cr, wf, existing=rep)
