"""Installation lifecycle: create / update / test / enable / rotate / revoke. All operations are organization-scoped, audited and secret-free."""
from __future__ import annotations

import logging
from typing import Any

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.core.config import Settings
from app.core.errors import ConflictError, NotFoundError, NovaError, ValidationFailedError
from app.db.base import utcnow
from app.models import IntegrationInstallation, Repository
from app.observability import metrics as m
from app.services.governance.common import emit
from app.services.integrations import secrets as sec
from app.services.integrations.netguard import HttpClient, SafeHttpClient
from app.services.integrations.registry import Adapter, AdapterError, ConfigInvalid, Unsupported, get_adapter

log = logging.getLogger("nova.integrations")


class SecretsUnavailableError(NovaError):
    status_code = 503
    code = "secrets_unavailable"


def http_client(settings: Settings) -> HttpClient:
    return SafeHttpClient(allow_private=settings.integration_allow_private_destinations, timeout=settings.integration_http_timeout_seconds,
                          max_bytes=settings.integration_http_max_response_bytes)


def adapter_or_404(type_id: str) -> Adapter:
    try:
        return get_adapter(type_id)
    except Unsupported:
        raise NotFoundError("Unknown integration type.") from None


def get_installation(db: Session, workspace_id: str, installation_id: str) -> IntegrationInstallation:
    inst = db.get(IntegrationInstallation, installation_id)
    if inst is None or inst.workspace_id != workspace_id or inst.removed_at is not None:
        raise NotFoundError("Integration installation not found.")
    return inst


def _check_config(db: Session, workspace_id: str, adapter: Adapter, config: dict[str, Any]) -> dict[str, Any]:
    try:
        cfg = adapter.validate_config(config)
    except ConfigInvalid as exc:
        raise ValidationFailedError("The integration configuration is invalid.", {"problems": exc.problems}) from None
    for mp in cfg.get("repositories", []):
        repo = db.get(Repository, mp["repository_id"])
        if repo is None or repo.workspace_id != workspace_id:
            raise ValidationFailedError("A mapped repository does not exist in this organization.", {"repository_id": mp["repository_id"]})
    externals = [mp["external"].lower() for mp in cfg.get("repositories", [])]
    if len(externals) != len(set(externals)):
        raise ValidationFailedError("An external repository is mapped more than once.")
    return cfg


def _store_credential(db: Session, settings: Settings, inst: IntegrationInstallation, credential: str | None, ref: str | None, actor: str) -> None:
    if credential and ref:
        raise ValidationFailedError("Provide either a credential or a credential reference, not both.")
    if credential:
        try:
            row = sec.store(db, settings, workspace_id=inst.workspace_id, owner_type="installation", owner_id=inst.id, plaintext=credential, actor=actor)
        except sec.SecretsUnavailable as exc:
            raise SecretsUnavailableError(exc.reason) from None
        except ValueError:
            raise ValidationFailedError("The credential value is not acceptable.") from None
        inst.credential_ref = f"secret:{row.id}"
    elif ref:
        if not sec.valid_ref(ref) or ref.startswith("secret:"):
            raise ValidationFailedError("credential_ref must be env:NOVA_CRED_<NAME>.")
        sec.revoke_owner(db, "installation", inst.id)
        inst.credential_ref = ref


def _webhook_secret(db: Session, settings: Settings, inst: IntegrationInstallation, actor: str) -> str:
    plain = sec.new_webhook_secret()
    try:
        row = sec.store(db, settings, workspace_id=inst.workspace_id, owner_type="installation_webhook", owner_id=inst.id, plaintext=plain, actor=actor)
    except sec.SecretsUnavailable as exc:
        raise SecretsUnavailableError(exc.reason) from None
    inst.webhook_secret_id = row.id
    return plain


def create(db: Session, settings: Settings, *, workspace_id: str, actor: str, integration_type: str, name: str, config: dict[str, Any],
           credential: str | None, credential_ref: str | None) -> tuple[IntegrationInstallation, str | None]:
    ad = adapter_or_404(integration_type)
    cfg = _check_config(db, workspace_id, ad, config)
    if ad.requires_credential and not (credential or credential_ref):
        raise ValidationFailedError("This integration requires a credential.")
    if not ad.requires_credential and (credential or credential_ref):
        raise ValidationFailedError("This integration does not use a credential.")
    inst = IntegrationInstallation(workspace_id=workspace_id, integration_type=integration_type, name=name, config=cfg, enabled=False,
                                   requested_permissions=list(ad.permissions), created_by=actor, updated_by=actor)
    db.add(inst)
    try:
        db.flush()
    except IntegrityError:
        db.rollback()
        raise ConflictError("An integration with this name already exists.") from None
    _store_credential(db, settings, inst, credential, credential_ref, actor)
    whs = _webhook_secret(db, settings, inst, actor) if ad.requires_webhook_secret else None
    emit(db, workspace_id=workspace_id, actor=actor, action="integration.created", entity_type="integration", entity_id=inst.id, details={"type": integration_type, "name": name})
    return inst, whs


def update(db: Session, workspace_id: str, actor: str, inst: IntegrationInstallation, *, name: str | None, config: dict[str, Any] | None) -> IntegrationInstallation:
    if name is not None:
        inst.name = name
    if config is not None:
        inst.config = _check_config(db, workspace_id, adapter_or_404(inst.integration_type), config)
        inst.connection_status = "unknown"
    inst.updated_by = actor
    try:
        db.flush()
    except IntegrityError:
        db.rollback()
        raise ConflictError("An integration with this name already exists.") from None
    emit(db, workspace_id=workspace_id, actor=actor, action="integration.updated", entity_type="integration", entity_id=inst.id, details={"fields": [k for k, v in (("name", name), ("config", config)) if v is not None]})
    return inst


def _token(db: Session, settings: Settings, inst: IntegrationInstallation) -> str | None:
    ad = adapter_or_404(inst.integration_type)
    if not ad.requires_credential:
        return None
    try:
        return sec.resolve(db, settings, inst.workspace_id, inst.credential_ref)
    except sec.SecretsUnavailable as exc:
        raise SecretsUnavailableError(exc.reason) from None
    except sec.SecretMissing:
        raise AdapterError("credential_missing") from None


def token_for(db: Session, settings: Settings, inst: IntegrationInstallation) -> str | None:
    return _token(db, settings, inst)


def test(db: Session, settings: Settings, actor: str | None, inst: IntegrationInstallation, http: HttpClient | None = None) -> dict[str, Any]:
    """Connectivity test. The destination comes ONLY from the stored, validated config; the caller cannot supply a URL."""
    ad = adapter_or_404(inst.integration_type)
    now = utcnow()
    before = inst.connection_status
    inst.last_checked_at = now
    result: dict[str, Any]
    try:
        token = _token(db, settings, inst)
        detail = ad.test_connection(inst.config, token, http or http_client(settings))
        inst.connection_status, inst.last_success_at, inst.last_error_category = "connected", now, None
        inst.granted_permissions = [p for p in detail.get("granted", []) if isinstance(p, str)][:10] if "granted" in detail else inst.granted_permissions
        result = {"ok": True, "category": None, "detail": detail}
        m.INT_TESTS.inc(integration_type=inst.integration_type, result="ok")
    except Unsupported as exc:
        inst.last_error_category = "unsupported"
        result = {"ok": False, "category": "unsupported", "detail": {"operation": exc.operation}}
        m.INT_TESTS.inc(integration_type=inst.integration_type, result="unsupported")
    except AdapterError as exc:
        inst.connection_status, inst.last_error_category = "failing", exc.category[:40]
        result = {"ok": False, "category": exc.category, "detail": {}}
        m.INT_TESTS.inc(integration_type=inst.integration_type, result="failed")
    if inst.connection_status != before:
        from app.services.integrations import outbox
        outbox.publish(db, inst.workspace_id, "integration.health_changed", "integration", inst.id, f"integration.health_changed:{inst.id}:{inst.connection_status}:{now.isoformat()}")
    emit(db, workspace_id=inst.workspace_id, actor=actor, action="integration.tested", entity_type="integration", entity_id=inst.id,
         outcome="success" if result["ok"] else "failure", details={"category": result["category"]})
    return result


def set_enabled(db: Session, actor: str, inst: IntegrationInstallation, enabled: bool) -> IntegrationInstallation:
    inst.enabled, inst.updated_by = enabled, actor
    emit(db, workspace_id=inst.workspace_id, actor=actor, action="integration.enabled" if enabled else "integration.disabled", entity_type="integration", entity_id=inst.id)
    return inst


def rotate_credential(db: Session, settings: Settings, actor: str, inst: IntegrationInstallation, credential: str | None, ref: str | None) -> IntegrationInstallation:
    if not adapter_or_404(inst.integration_type).requires_credential:
        raise ValidationFailedError("This integration does not use a credential.")
    if not (credential or ref):
        raise ValidationFailedError("Provide a new credential or credential reference.")
    _store_credential(db, settings, inst, credential, ref, actor)
    inst.connection_status, inst.updated_by = "unknown", actor
    emit(db, workspace_id=inst.workspace_id, actor=actor, action="integration.credential_replaced", entity_type="integration", entity_id=inst.id)
    return inst


def rotate_webhook_secret(db: Session, settings: Settings, actor: str, inst: IntegrationInstallation) -> str:
    if not adapter_or_404(inst.integration_type).requires_webhook_secret:
        raise ValidationFailedError("This integration does not receive webhooks.")
    plain = _webhook_secret(db, settings, inst, actor)
    emit(db, workspace_id=inst.workspace_id, actor=actor, action="integration.webhook_secret_rotated", entity_type="integration", entity_id=inst.id)
    return plain


def revoke(db: Session, actor: str, inst: IntegrationInstallation) -> None:
    """Uninstall: disable, revoke credentials and stop accepting deliveries. Events, evidence and audit history are retained."""
    inst.enabled, inst.removed_at, inst.updated_by, inst.credential_ref, inst.connection_status = False, utcnow(), actor, None, "unknown"
    sec.revoke_owner(db, "installation", inst.id)
    sec.revoke_owner(db, "installation_webhook", inst.id)
    inst.webhook_secret_id = None
    emit(db, workspace_id=inst.workspace_id, actor=actor, action="integration.revoked", entity_type="integration", entity_id=inst.id)


def health_state(inst: IntegrationInstallation) -> str:
    """disabled is a deliberate state; failing means an enabled integration's last check failed."""
    if not inst.enabled:
        return "disabled"
    return {"connected": "healthy", "failing": "failing"}.get(inst.connection_status, "unknown")


def serialize(inst: IntegrationInstallation) -> dict[str, Any]:
    ad = get_adapter(inst.integration_type) if inst.integration_type else None
    return {"id": inst.id, "integration_type": inst.integration_type, "name": inst.name, "enabled": inst.enabled, "health": health_state(inst),
            "connection_status": inst.connection_status, "config": inst.config, "has_credential": bool(inst.credential_ref),
            "credential_kind": None if not inst.credential_ref else ("environment" if inst.credential_ref.startswith("env:") else "stored"),
            "requires_webhook_secret": bool(ad and ad.requires_webhook_secret), "has_webhook_secret": bool(inst.webhook_secret_id),
            "capabilities": list(ad.capabilities) if ad else [], "requested_permissions": inst.requested_permissions, "granted_permissions": inst.granted_permissions,
            "last_checked_at": inst.last_checked_at, "last_success_at": inst.last_success_at, "last_sync_at": inst.last_sync_at,
            "last_error_category": inst.last_error_category, "created_by": inst.created_by, "updated_by": inst.updated_by,
            "created_at": inst.created_at, "updated_at": inst.updated_at}


def list_installations(db: Session, workspace_id: str, page: int, size: int) -> tuple[list[IntegrationInstallation], int]:
    from sqlalchemy import func
    q = select(IntegrationInstallation).where(IntegrationInstallation.workspace_id == workspace_id, IntegrationInstallation.removed_at.is_(None))
    total = db.scalar(select(func.count()).select_from(q.subquery())) or 0
    rows = list(db.scalars(q.order_by(IntegrationInstallation.created_at, IntegrationInstallation.id).offset((page - 1) * size).limit(size)))
    return rows, total


def publish_status(db: Session, settings: Settings, actor: str, inst: IntegrationInstallation, *, external_repo: str, sha: str, state: str, description: str,
                   http: HttpClient | None = None) -> dict[str, Any]:
    """Explicit, authorized status publication to the provider. Only for installations that opted in (``publish_status``) and only for mapped repositories."""
    ad = adapter_or_404(inst.integration_type)
    if "publish_status" not in ad.capabilities:
        raise ValidationFailedError("This integration cannot publish statuses.")
    if not inst.enabled:
        raise ConflictError("The integration is disabled.")
    try:
        out = ad.publish_status(inst.config, _token(db, settings, inst), http or http_client(settings), external_repo=external_repo, sha=sha, state=state,
                                description=description, context="nova/verification")
    except Unsupported:
        raise ValidationFailedError("Status publishing is not enabled for this integration.") from None
    except AdapterError as exc:
        emit(db, workspace_id=inst.workspace_id, actor=actor, action="integration.status_publish_failed", entity_type="integration", entity_id=inst.id, outcome="failure", details={"category": exc.category})
        raise ConflictError("The provider request failed.", {"category": exc.category}) from None
    emit(db, workspace_id=inst.workspace_id, actor=actor, action="integration.status_published", entity_type="integration", entity_id=inst.id, details={"state": state, "sha": sha[:12]})
    return out
