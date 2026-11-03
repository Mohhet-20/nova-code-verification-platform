"""External issue links. Linking or creating is always an explicit, authorized call; nothing is auto-published. NOVA never writes back from
inbound events, so there is no bidirectional sync loop. Creation is at most once per NOVA resource per tracker (unique index) and external
ids are unique per installation (duplicate prevention)."""
from __future__ import annotations

from typing import Any

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.core.config import Settings
from app.core.errors import ConflictError, NotFoundError, ValidationFailedError
from app.db.base import utcnow
from app.models import ExternalLink, IntegrationInstallation, WorkflowInstance
from app.services.governance.common import emit
from app.services.integrations import service as svc
from app.services.integrations.netguard import HttpClient
from app.services.integrations.registry import AdapterError, Unsupported

NOVA_TYPES = ("workflow",)


def _check_resource(db: Session, workspace_id: str, nova_type: str, nova_id: str) -> None:
    if nova_type not in NOVA_TYPES:
        raise ValidationFailedError("Unsupported NOVA resource type.", {"allowed": list(NOVA_TYPES)})
    w = db.get(WorkflowInstance, nova_id)
    if w is None or w.workspace_id != workspace_id:
        raise NotFoundError("NOVA resource not found.")


def _tracker(db: Session, workspace_id: str, installation_id: str) -> IntegrationInstallation:
    inst = svc.get_installation(db, workspace_id, installation_id)
    if "issue_tracker" not in svc.adapter_or_404(inst.integration_type).capabilities:
        raise ValidationFailedError("This integration is not an issue tracker.")
    if not inst.enabled:
        raise ConflictError("The integration is disabled.")
    return inst


def _map(code: str) -> Exception:
    return ConflictError("The issue tracker request failed.", {"category": code})


def search(db: Session, settings: Settings, workspace_id: str, installation_id: str, query: str, http: HttpClient | None = None) -> list[dict[str, Any]]:
    inst = _tracker(db, workspace_id, installation_id)
    try:
        return svc.adapter_or_404(inst.integration_type).search_issues(inst.config, svc.token_for(db, settings, inst), http or svc.http_client(settings), query)
    except AdapterError as exc:
        raise _map(exc.category) from None
    except Unsupported:
        raise ValidationFailedError("This operation is not supported by the integration.") from None


def link(db: Session, settings: Settings, workspace_id: str, actor: str, installation_id: str, nova_type: str, nova_id: str, external_id: str,
         http: HttpClient | None = None) -> ExternalLink:
    inst = _tracker(db, workspace_id, installation_id)
    _check_resource(db, workspace_id, nova_type, nova_id)
    try:
        issue = svc.adapter_or_404(inst.integration_type).get_issue(inst.config, svc.token_for(db, settings, inst), http or svc.http_client(settings), external_id)
    except AdapterError as exc:
        raise _map(exc.category) from None
    return _persist(db, workspace_id, actor, inst, nova_type, nova_id, issue, "linked")


def _persist(db: Session, workspace_id: str, actor: str, inst: IntegrationInstallation, nova_type: str, nova_id: str, issue: dict[str, Any], origin: str) -> ExternalLink:
    if db.scalar(select(ExternalLink).where(ExternalLink.installation_id == inst.id, ExternalLink.external_id == issue["external_id"], ExternalLink.nova_id == nova_id)):
        raise ConflictError("This issue is already linked to this resource.")
    row = ExternalLink(workspace_id=workspace_id, installation_id=inst.id, nova_type=nova_type, nova_id=nova_id, external_id=issue["external_id"], external_key=issue["key"],
                       url=issue["url"], title=issue["title"], external_state=issue["state"], origin=origin, last_synced_at=utcnow(), created_by=actor)
    db.add(row)
    try:
        db.flush()
    except IntegrityError:
        db.rollback()
        raise ConflictError("A duplicate link was prevented.") from None
    emit(db, workspace_id=workspace_id, actor=actor, action=f"integration.issue_{origin}", entity_type="integration", entity_id=inst.id, details={"nova_type": nova_type, "external": issue["key"]})
    return row


def create(db: Session, settings: Settings, workspace_id: str, actor: str, installation_id: str, nova_type: str, nova_id: str, title: str, body: str,
           http: HttpClient | None = None) -> ExternalLink:
    inst = _tracker(db, workspace_id, installation_id)
    _check_resource(db, workspace_id, nova_type, nova_id)
    if db.scalar(select(ExternalLink).where(ExternalLink.installation_id == inst.id, ExternalLink.nova_type == nova_type, ExternalLink.nova_id == nova_id, ExternalLink.origin == "created")):
        raise ConflictError("An issue was already created for this resource in this tracker.")
    try:
        issue = svc.adapter_or_404(inst.integration_type).create_issue(inst.config, svc.token_for(db, settings, inst), http or svc.http_client(settings), title, body)
    except Unsupported:
        raise ValidationFailedError("Issue creation is not enabled for this integration.") from None
    except AdapterError as exc:
        raise _map(exc.category) from None
    return _persist(db, workspace_id, actor, inst, nova_type, nova_id, issue, "created")


def serialize(link_: ExternalLink) -> dict[str, Any]:
    return {"id": link_.id, "installation_id": link_.installation_id, "nova_type": link_.nova_type, "nova_id": link_.nova_id, "external_id": link_.external_id,
            "key": link_.external_key, "url": link_.url, "title": link_.title, "state": link_.external_state, "origin": link_.origin, "created_at": link_.created_at}
