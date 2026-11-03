"""Phase 16 integration API (v1): catalog, installations, credentials, inbound events, CI evidence, issue links, webhook subscriptions and
deliveries, extensions, jobs and health. Every handler derives identity and permissions server-side; request models forbid unknown fields
(no mass assignment); secrets are returned exactly once at creation/rotation and never again."""
from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, Header, Query, Request
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.core.config import Settings, get_settings
from app.core.errors import NotFoundError, PayloadTooLargeError, ValidationFailedError
from app.db.session import get_db
from app.models import (
    CiEvidenceRun,
    ExternalLink,
    IntegrationEvent,
    IntegrationInstallation,
    IntegrationJob,
    OutboxEvent,
    WebhookDelivery,
    WebhookDeliveryAttempt,
    WebhookSubscription,
)
from app.schemas.common import Page, make_page
from app.security.auth import RequestContext, get_context
from app.security.authz import Principal
from app.services.integrations import extensions as ext
from app.services.integrations import idempotency, inbound, jobs, links, outbox, registry
from app.services.integrations import secrets as sec
from app.services.integrations import service as svc

router = APIRouter(prefix="/integrations", tags=["integrations"])
inbound_router = APIRouter(prefix="/integrations", tags=["integrations-inbound"])
Dict = dict[str, Any]


class _In(BaseModel):
    model_config = ConfigDict(extra="forbid")


class InstallationCreate(_In):
    integration_type: str = Field(min_length=1, max_length=40)
    name: str = Field(min_length=1, max_length=120)
    config: Dict = Field(default_factory=dict)
    credential: str | None = Field(default=None, max_length=sec.MAX_SECRET_LEN)
    credential_ref: str | None = Field(default=None, max_length=80)


class InstallationUpdate(_In):
    name: str | None = Field(default=None, min_length=1, max_length=120)
    config: Dict | None = None


class CredentialReplace(_In):
    credential: str | None = Field(default=None, max_length=sec.MAX_SECRET_LEN)
    credential_ref: str | None = Field(default=None, max_length=80)


class SubscriptionCreate(_In):
    name: str = Field(min_length=1, max_length=120)
    url: str = Field(min_length=8, max_length=500)
    events: list[str] = Field(min_length=1, max_length=len(outbox.ALLOWED_EVENTS))


class SubscriptionUpdate(_In):
    name: str | None = Field(default=None, min_length=1, max_length=120)
    url: str | None = Field(default=None, min_length=8, max_length=500)
    events: list[str] | None = Field(default=None, min_length=1, max_length=len(outbox.ALLOWED_EVENTS))
    active: bool | None = None


class LinkCreate(_In):
    installation_id: str = Field(min_length=1, max_length=32)
    nova_type: str = Field(min_length=1, max_length=40)
    nova_id: str = Field(min_length=1, max_length=32)
    external_id: str = Field(min_length=1, max_length=20)


class IssueCreate(_In):
    installation_id: str = Field(min_length=1, max_length=32)
    nova_type: str = Field(min_length=1, max_length=40)
    nova_id: str = Field(min_length=1, max_length=32)
    title: str = Field(min_length=1, max_length=200)
    body: str = Field(default="", max_length=4000)


class StatusPublish(_In):
    external_repo: str = Field(min_length=3, max_length=201)
    sha: str = Field(pattern="^[0-9a-f]{40}$")
    state: str = Field(pattern="^(success|failure|error|pending)$")
    description: str = Field(default="", max_length=140)


class ManifestCheck(_In):
    manifest: Dict


def P(ctx: RequestContext = Depends(get_context), db: Session = Depends(get_db)) -> Principal:  # noqa: N802
    return Principal(db, ctx)


def _page(items: list[Any], total: int, page: int, size: int) -> Page[Any]:
    return make_page(items, total, page, size)


def _inst(db: Session, p: Principal, installation_id: str) -> IntegrationInstallation:
    return svc.get_installation(db, p.workspace_id, installation_id)


# ------------------------------------------------------------------------------------------------ catalog / health
@router.get("/catalog")
def catalog(p: Principal = Depends(P)) -> Dict:
    p.require("integration.view")
    return {"items": [a.manifest() for a in registry.ADAPTERS.values()], "events": list(outbox.ALLOWED_EVENTS)}


@router.get("/health")
def health(p: Principal = Depends(P), db: Session = Depends(get_db), settings: Settings = Depends(get_settings)) -> Dict:
    p.require("integration.view")
    insts = list(db.scalars(select(IntegrationInstallation).where(IntegrationInstallation.workspace_id == p.workspace_id, IntegrationInstallation.removed_at.is_(None))))
    by: dict[str, int] = {}
    for i in insts:
        h = svc.health_state(i)
        by[h] = by.get(h, 0) + 1
    return {"installations": by, "total": len(insts), "secrets": sec.encryption_status(settings) | {"stored_credentials_available": sec.encryption_status(settings)["configured"]},
            "outbound_queue": outbox.queue_stats(db), "jobs": jobs.stats(db), "worker_enabled": settings.integration_worker_enabled,
            "private_destinations_allowed": settings.integration_allow_private_destinations}


# ------------------------------------------------------------------------------------------------ installations
@router.get("/installations")
def list_installations(page: int = Query(1, ge=1), page_size: int = Query(25, ge=1, le=100), p: Principal = Depends(P), db: Session = Depends(get_db)) -> Page[Any]:
    p.require("integration.view")
    rows, total = svc.list_installations(db, p.workspace_id, page, page_size)
    return _page([svc.serialize(r) for r in rows], total, page, page_size)


@router.post("/installations", status_code=201)
def create_installation(body: InstallationCreate, p: Principal = Depends(P), db: Session = Depends(get_db), settings: Settings = Depends(get_settings)) -> Dict:
    p.require("integration.manage")
    inst, whs = svc.create(db, settings, workspace_id=p.workspace_id, actor=p.user_id, integration_type=body.integration_type, name=body.name.strip(), config=body.config,
                           credential=body.credential, credential_ref=body.credential_ref)
    db.commit()
    out = svc.serialize(inst)
    out["inbound_path"] = f"/api/v1/integrations/inbound/{inst.id}" if whs else None
    out["webhook_secret"] = whs  # shown exactly once
    return out


@router.get("/installations/{installation_id}")
def get_installation(installation_id: str, p: Principal = Depends(P), db: Session = Depends(get_db)) -> Dict:
    p.require("integration.view")
    inst = _inst(db, p, installation_id)
    out = svc.serialize(inst)
    out["inbound_path"] = f"/api/v1/integrations/inbound/{inst.id}" if out["requires_webhook_secret"] else None
    return out


@router.put("/installations/{installation_id}")
def update_installation(installation_id: str, body: InstallationUpdate, p: Principal = Depends(P), db: Session = Depends(get_db)) -> Dict:
    p.require("integration.manage")
    inst = svc.update(db, p.workspace_id, p.user_id, _inst(db, p, installation_id), name=body.name.strip() if body.name else None, config=body.config)
    db.commit()
    return svc.serialize(inst)


@router.post("/installations/{installation_id}/test")
def test_installation(installation_id: str, p: Principal = Depends(P), db: Session = Depends(get_db), settings: Settings = Depends(get_settings)) -> Dict:
    p.require("integration.manage")
    res = svc.test(db, settings, p.user_id, _inst(db, p, installation_id))
    db.commit()
    return res


@router.post("/installations/{installation_id}/enable")
def enable_installation(installation_id: str, p: Principal = Depends(P), db: Session = Depends(get_db)) -> Dict:
    p.require("integration.manage")
    inst = svc.set_enabled(db, p.user_id, _inst(db, p, installation_id), True)
    db.commit()
    return svc.serialize(inst)


@router.post("/installations/{installation_id}/disable")
def disable_installation(installation_id: str, p: Principal = Depends(P), db: Session = Depends(get_db)) -> Dict:
    p.require("integration.manage")
    inst = svc.set_enabled(db, p.user_id, _inst(db, p, installation_id), False)
    db.commit()
    return svc.serialize(inst)


@router.post("/installations/{installation_id}/credential")
def replace_credential(installation_id: str, body: CredentialReplace, p: Principal = Depends(P), db: Session = Depends(get_db), settings: Settings = Depends(get_settings)) -> Dict:
    p.require("integration.manage")
    inst = svc.rotate_credential(db, settings, p.user_id, _inst(db, p, installation_id), body.credential, body.credential_ref)
    db.commit()
    return svc.serialize(inst)


@router.post("/installations/{installation_id}/webhook-secret/rotate")
def rotate_webhook_secret(installation_id: str, p: Principal = Depends(P), db: Session = Depends(get_db), settings: Settings = Depends(get_settings)) -> Dict:
    p.require("integration.manage")
    plain = svc.rotate_webhook_secret(db, settings, p.user_id, _inst(db, p, installation_id))
    db.commit()
    return {"webhook_secret": plain}


@router.post("/installations/{installation_id}/revoke")
def revoke_installation(installation_id: str, p: Principal = Depends(P), db: Session = Depends(get_db)) -> Dict:
    p.require("integration.manage")
    svc.revoke(db, p.user_id, _inst(db, p, installation_id))
    db.commit()
    return {"revoked": True}


@router.post("/installations/{installation_id}/publish-status")
def publish_status(installation_id: str, body: StatusPublish, p: Principal = Depends(P), db: Session = Depends(get_db), settings: Settings = Depends(get_settings)) -> Dict:
    p.require("integration.manage")
    out = svc.publish_status(db, settings, p.user_id, _inst(db, p, installation_id), external_repo=body.external_repo, sha=body.sha, state=body.state, description=body.description)
    db.commit()
    return out


@router.post("/installations/{installation_id}/check", status_code=202)
def queue_check(installation_id: str, p: Principal = Depends(P), db: Session = Depends(get_db)) -> Dict:
    p.require("integration.manage")
    inst = _inst(db, p, installation_id)
    j = jobs.enqueue(db, workspace_id=p.workspace_id, actor=p.user_id, operation="connection_check", installation_id=inst.id)
    db.commit()
    return jobs.serialize(j)


@router.get("/installations/{installation_id}/events")
def installation_events(installation_id: str, status: str | None = Query(None, pattern="^(accepted|processed|ignored|failed)$"), page: int = Query(1, ge=1),
                        page_size: int = Query(25, ge=1, le=100), p: Principal = Depends(P), db: Session = Depends(get_db)) -> Page[Any]:
    p.require("integration.view")
    inst = _inst(db, p, installation_id)
    q = select(IntegrationEvent).where(IntegrationEvent.installation_id == inst.id)
    if status:
        q = q.where(IntegrationEvent.status == status)
    total = db.scalar(select(func.count()).select_from(q.subquery())) or 0
    rows = db.scalars(q.order_by(IntegrationEvent.received_at.desc(), IntegrationEvent.id).offset((page - 1) * page_size).limit(page_size))
    return _page([{"id": e.id, "delivery_id": e.delivery_id, "event_type": e.event_type, "action": e.action, "status": e.status, "external_ref": e.external_ref,
                   "repository_id": e.repository_id, "error_category": e.error_category, "received_at": e.received_at, "processed_at": e.processed_at} for e in rows], total, page, page_size)


# ------------------------------------------------------------------------------------------------ inbound (signature-authenticated, no bearer)
@inbound_router.post("/inbound/{installation_id}")
async def inbound_webhook(installation_id: str, request: Request, db: Session = Depends(get_db), settings: Settings = Depends(get_settings)) -> Dict:
    limit = settings.integration_inbound_max_bytes
    declared = request.headers.get("content-length")
    if declared and declared.isdigit() and int(declared) > limit:
        raise PayloadTooLargeError("The webhook body exceeds the size limit.")
    chunks: list[bytes] = []
    size = 0
    async for chunk in request.stream():
        size += len(chunk)
        if size > limit:
            raise PayloadTooLargeError("The webhook body exceeds the size limit.")
        chunks.append(chunk)
    res = inbound.receive(db, settings, installation_id, dict(request.headers), b"".join(chunks))
    db.commit()
    return res


# ------------------------------------------------------------------------------------------------ CI evidence
@router.get("/ci-evidence")
def ci_evidence(repository_id: str | None = Query(None, max_length=32), head_sha: str | None = Query(None, pattern="^[0-9a-f]{40}$"), status: str | None = Query(None, pattern="^(pending|success|failed|cancelled|unknown)$"),
                page: int = Query(1, ge=1), page_size: int = Query(25, ge=1, le=100), p: Principal = Depends(P), db: Session = Depends(get_db)) -> Dict:
    p.require("integration.view")
    q = select(CiEvidenceRun).where(CiEvidenceRun.workspace_id == p.workspace_id)
    if repository_id:
        q = q.where(CiEvidenceRun.repository_id == repository_id)
    if head_sha:
        q = q.where(CiEvidenceRun.head_sha == head_sha)
    if status:
        q = q.where(CiEvidenceRun.status == status)
    total = db.scalar(select(func.count()).select_from(q.subquery())) or 0
    configured = db.scalar(select(func.count()).select_from(IntegrationInstallation).where(IntegrationInstallation.workspace_id == p.workspace_id, IntegrationInstallation.removed_at.is_(None),
                                                                                          IntegrationInstallation.integration_type.in_(("github", "local-test")))) or 0
    rows = db.scalars(q.order_by(CiEvidenceRun.received_at.desc(), CiEvidenceRun.id).offset((page - 1) * page_size).limit(page_size))
    items = [{"id": r.id, "installation_id": r.installation_id, "repository_id": r.repository_id, "source": r.source, "external_run_id": r.external_run_id, "name": r.name, "head_sha": r.head_sha,
              "status": r.status, "external_url": r.external_url, "started_at": r.started_at, "completed_at": r.completed_at, "received_at": r.received_at} for r in rows]
    return {**_page(items, total, page, page_size).model_dump(), "configured": configured > 0}


# ------------------------------------------------------------------------------------------------ external issue links
@router.get("/installations/{installation_id}/issues/search")
def search_issues(installation_id: str, q: str = Query(min_length=1, max_length=100), p: Principal = Depends(P), db: Session = Depends(get_db), settings: Settings = Depends(get_settings)) -> Dict:
    p.require("integration.link_issue")
    return {"items": links.search(db, settings, p.workspace_id, installation_id, q)}


@router.get("/links")
def list_links(nova_type: str = Query(max_length=40), nova_id: str = Query(max_length=32), p: Principal = Depends(P), db: Session = Depends(get_db)) -> Dict:
    p.require("integration.view")
    rows = db.scalars(select(ExternalLink).where(ExternalLink.workspace_id == p.workspace_id, ExternalLink.nova_type == nova_type, ExternalLink.nova_id == nova_id).order_by(ExternalLink.created_at, ExternalLink.id))
    return {"items": [links.serialize(r) for r in rows]}


@router.post("/links", status_code=201)
def link_issue(body: LinkCreate, p: Principal = Depends(P), db: Session = Depends(get_db), settings: Settings = Depends(get_settings)) -> Dict:
    p.require("integration.link_issue")
    row = links.link(db, settings, p.workspace_id, p.user_id, body.installation_id, body.nova_type, body.nova_id, body.external_id)
    db.commit()
    return links.serialize(row)


@router.post("/issues", status_code=201)
def create_issue(body: IssueCreate, idempotency_key: str | None = Header(default=None, alias="Idempotency-Key"), p: Principal = Depends(P), db: Session = Depends(get_db),
                 settings: Settings = Depends(get_settings)) -> Any:
    p.require("integration.manage")
    from fastapi.responses import JSONResponse
    rec, replay_body, replay_status = idempotency.begin(db, p.workspace_id, p.user_id, "POST /integrations/issues", idempotency_key, body.model_dump())
    if replay_body is not None:
        db.commit()
        return JSONResponse(replay_body, status_code=replay_status or 201, headers={"Idempotent-Replay": "true"})
    try:
        row = links.create(db, settings, p.workspace_id, p.user_id, body.installation_id, body.nova_type, body.nova_id, body.title, body.body)
    except Exception:
        db.rollback()  # a failed first attempt must not pin the key; a retry with the same key may try again
        raise
    out = links.serialize(row)
    from fastapi.encoders import jsonable_encoder
    idempotency.complete(rec, 201, jsonable_encoder(out))
    db.commit()
    return out


# ------------------------------------------------------------------------------------------------ webhook subscriptions & deliveries
@router.get("/webhook-subscriptions")
def list_subscriptions(page: int = Query(1, ge=1), page_size: int = Query(25, ge=1, le=100), p: Principal = Depends(P), db: Session = Depends(get_db)) -> Page[Any]:
    p.require("webhook.manage")
    q = select(WebhookSubscription).where(WebhookSubscription.workspace_id == p.workspace_id, WebhookSubscription.removed_at.is_(None))
    total = db.scalar(select(func.count()).select_from(q.subquery())) or 0
    rows = db.scalars(q.order_by(WebhookSubscription.created_at, WebhookSubscription.id).offset((page - 1) * page_size).limit(page_size))
    return _page([outbox.serialize_subscription(s) for s in rows], total, page, page_size)


@router.post("/webhook-subscriptions", status_code=201)
def create_subscription(body: SubscriptionCreate, p: Principal = Depends(P), db: Session = Depends(get_db), settings: Settings = Depends(get_settings)) -> Dict:
    p.require("webhook.manage")
    sub, plain = outbox.create_subscription(db, settings, workspace_id=p.workspace_id, actor=p.user_id, name=body.name.strip(), url=body.url.strip(), events=body.events)
    db.commit()
    return {**outbox.serialize_subscription(sub), "secret": plain}  # shown exactly once


@router.put("/webhook-subscriptions/{sub_id}")
def update_subscription(sub_id: str, body: SubscriptionUpdate, p: Principal = Depends(P), db: Session = Depends(get_db), settings: Settings = Depends(get_settings)) -> Dict:
    from app.services.governance.common import emit
    p.require("webhook.manage")
    sub = outbox.get_subscription(db, p.workspace_id, sub_id)
    if body.events is not None:
        bad = [e for e in body.events if e not in outbox.ALLOWED_EVENTS]
        if bad:
            raise ValidationFailedError("Unsupported event types.", {"rejected": bad[:5]})
        sub.events = sorted(set(body.events))
    if body.url is not None:
        outbox.validate_subscription_url(settings, body.url.strip())
        sub.url = body.url.strip()
    if body.name is not None:
        sub.name = body.name.strip()
    if body.active is not None:
        sub.active = body.active
        sub.disabled_reason = None if body.active else "disabled_by_admin"
        if body.active:
            sub.consecutive_failures = 0
    emit(db, workspace_id=p.workspace_id, actor=p.user_id, action="webhook.subscription_updated", entity_type="webhook_subscription", entity_id=sub.id)
    db.commit()
    return outbox.serialize_subscription(sub)


@router.post("/webhook-subscriptions/{sub_id}/rotate-secret")
def rotate_subscription_secret(sub_id: str, p: Principal = Depends(P), db: Session = Depends(get_db), settings: Settings = Depends(get_settings)) -> Dict:
    p.require("webhook.manage")
    plain = outbox.rotate_secret(db, settings, p.user_id, outbox.get_subscription(db, p.workspace_id, sub_id))
    db.commit()
    return {"secret": plain}


@router.post("/webhook-subscriptions/{sub_id}/remove")
def remove_subscription(sub_id: str, p: Principal = Depends(P), db: Session = Depends(get_db)) -> Dict:
    from app.db.base import utcnow
    from app.services.governance.common import emit
    p.require("webhook.manage")
    sub = outbox.get_subscription(db, p.workspace_id, sub_id)
    sub.removed_at, sub.active = utcnow(), False
    sec.revoke_owner(db, "subscription", sub.id)
    emit(db, workspace_id=p.workspace_id, actor=p.user_id, action="webhook.subscription_removed", entity_type="webhook_subscription", entity_id=sub.id)
    db.commit()
    return {"removed": True}


@router.get("/webhook-deliveries")
def list_deliveries(subscription_id: str | None = Query(None, max_length=32), state: str | None = Query(None, pattern="^(pending|in_progress|retrying|succeeded|failed)$"),
                    page: int = Query(1, ge=1), page_size: int = Query(25, ge=1, le=100), p: Principal = Depends(P), db: Session = Depends(get_db)) -> Page[Any]:
    p.require("webhook.manage")
    q = select(WebhookDelivery, OutboxEvent.event_type).join(OutboxEvent, OutboxEvent.id == WebhookDelivery.event_id).where(WebhookDelivery.workspace_id == p.workspace_id)
    if subscription_id:
        q = q.where(WebhookDelivery.subscription_id == subscription_id)
    if state:
        q = q.where(WebhookDelivery.state == state)
    total = db.scalar(select(func.count()).select_from(q.subquery())) or 0
    rows = db.execute(q.order_by(WebhookDelivery.created_at.desc(), WebhookDelivery.id).offset((page - 1) * page_size).limit(page_size)).all()
    return _page([_delivery(d, et) for d, et in rows], total, page, page_size)


def _delivery(d: WebhookDelivery, event_type: str) -> Dict:
    return {"id": d.id, "subscription_id": d.subscription_id, "event_id": d.event_id, "event_type": event_type, "state": d.state, "attempts": d.attempts, "next_attempt_at": d.next_attempt_at,
            "last_status_code": d.last_status_code, "last_error_category": d.last_error_category, "last_attempt_at": d.last_attempt_at, "completed_at": d.completed_at,
            "replayed_by": d.replayed_by, "created_at": d.created_at}


def _get_delivery(db: Session, p: Principal, delivery_id: str) -> WebhookDelivery:
    d = db.get(WebhookDelivery, delivery_id)
    if d is None or d.workspace_id != p.workspace_id:
        raise NotFoundError("Delivery not found.")
    return d


@router.get("/webhook-deliveries/{delivery_id}")
def get_delivery(delivery_id: str, p: Principal = Depends(P), db: Session = Depends(get_db)) -> Dict:
    p.require("webhook.manage")
    d = _get_delivery(db, p, delivery_id)
    ev = db.get(OutboxEvent, d.event_id)
    atts = db.scalars(select(WebhookDeliveryAttempt).where(WebhookDeliveryAttempt.delivery_id == d.id).order_by(WebhookDeliveryAttempt.attempt_no))
    return {**_delivery(d, ev.event_type if ev else ""), "envelope": outbox.envelope(ev) if ev else None,
            "attempts_detail": [{"attempt_no": a.attempt_no, "started_at": a.started_at, "duration_ms": a.duration_ms, "status_code": a.status_code,
                                 "error_category": a.error_category, "response_excerpt": a.response_excerpt} for a in atts]}


@router.post("/webhook-deliveries/{delivery_id}/replay")
def replay_delivery(delivery_id: str, p: Principal = Depends(P), db: Session = Depends(get_db)) -> Dict:
    p.require("webhook.manage")
    d = outbox.replay(db, p.user_id, _get_delivery(db, p, delivery_id))
    db.commit()
    return _delivery(d, "")


# ------------------------------------------------------------------------------------------------ worker / jobs
@router.post("/worker/run")
def run_worker(p: Principal = Depends(P), db: Session = Depends(get_db), settings: Settings = Depends(get_settings)) -> Dict:
    """Process one bounded batch of due deliveries and jobs now (also what the optional background loop calls)."""
    p.require("integration.manage")
    return {"deliveries": outbox.run_due(db, settings), "jobs": jobs.run_due(db, settings)}


@router.get("/jobs")
def list_jobs(state: str | None = Query(None, pattern="^(queued|running|succeeded|failed|dead)$"), page: int = Query(1, ge=1), page_size: int = Query(25, ge=1, le=100),
              p: Principal = Depends(P), db: Session = Depends(get_db)) -> Page[Any]:
    p.require("integration.view")
    q = select(IntegrationJob).where(IntegrationJob.workspace_id == p.workspace_id)
    if state:
        q = q.where(IntegrationJob.state == state)
    total = db.scalar(select(func.count()).select_from(q.subquery())) or 0
    rows = db.scalars(q.order_by(IntegrationJob.created_at.desc(), IntegrationJob.id).offset((page - 1) * page_size).limit(page_size))
    return _page([jobs.serialize(j) for j in rows], total, page, page_size)


# ------------------------------------------------------------------------------------------------ extensions
@router.get("/extensions")
def list_extensions(p: Principal = Depends(P), db: Session = Depends(get_db)) -> Dict:
    p.require("integration.view")
    return {"items": ext.list_extensions(db, p.workspace_id), "external_code_supported": False}


@router.post("/extensions/validate")
def validate_extension(body: ManifestCheck, p: Principal = Depends(P)) -> Dict:
    p.require("extension.manage")
    return ext.validate_manifest(body.manifest)


@router.post("/extensions/{ext_id}/enable")
def enable_extension(ext_id: str, p: Principal = Depends(P), db: Session = Depends(get_db)) -> Dict:
    p.require("extension.manage")
    out = ext.set_enabled(db, p.workspace_id, p.user_id, ext_id, True)
    db.commit()
    return out


@router.post("/extensions/{ext_id}/disable")
def disable_extension(ext_id: str, p: Principal = Depends(P), db: Session = Depends(get_db)) -> Dict:
    p.require("extension.manage")
    out = ext.set_enabled(db, p.workspace_id, p.user_id, ext_id, False)
    db.commit()
    return out
