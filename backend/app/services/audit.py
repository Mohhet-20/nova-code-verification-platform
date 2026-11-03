from __future__ import annotations

from typing import Any

from sqlalchemy.orm import Session

from app.models import AuditEvent


def record(db: Session, *, workspace_id: str, actor_user_id: str | None, action: str,
           entity_type: str, entity_id: str, details: dict[str, Any] | None = None) -> None:
    """Append an audit event. ``details`` must never contain file contents or secrets."""
    db.add(AuditEvent(workspace_id=workspace_id, actor_user_id=actor_user_id, action=action,
                      entity_type=entity_type, entity_id=entity_id, details=details or {}))
