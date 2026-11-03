"""Shared helpers: governance audit events, bounded safe details, pagination."""
from __future__ import annotations

import logging
import re
from typing import Any

from sqlalchemy.orm import Session

from app.models import AuditEvent

logger = logging.getLogger("nova.governance")

_SECRET_KEY = re.compile(r"token|secret|password|authorization|credential|api[_-]?key", re.I)


def clean(value: Any, depth: int = 0) -> Any:
    """Bounded, secret-free copy of audit details: scalars, short lists/dicts, no values under secret-looking keys."""
    if depth > 3:
        return "…"
    if isinstance(value, str):
        return value[:300]
    if isinstance(value, (int, float, bool)) or value is None:
        return value
    if isinstance(value, (list, tuple)):
        return [clean(v, depth + 1) for v in list(value)[:25]]
    if isinstance(value, dict):
        return {str(k)[:40]: ("[redacted]" if _SECRET_KEY.search(str(k)) else clean(v, depth + 1)) for k, v in list(value.items())[:30]}
    return str(value)[:100]


def emit(db: Session, *, workspace_id: str, actor: str | None, action: str, entity_type: str, entity_id: str,
         outcome: str = "success", team_id: str | None = None, details: dict[str, Any] | None = None) -> None:
    """Append a governance audit event (never contains secrets, tokens or source contents)."""
    body: dict[str, Any] = {"outcome": outcome}
    if team_id:
        body["team_id"] = team_id
    if details:
        body.update(clean(details))
    db.add(AuditEvent(workspace_id=workspace_id, actor_user_id=actor, action=action, entity_type=entity_type, entity_id=entity_id[:32], details=body))
    if outcome == "success" and action.startswith("workflow."):
        from app.services.integrations import (
            outbox,  # lazy: integrations depend on governance, not the reverse at import time
        )

        outbox.publish_from_audit(db, workspace_id, action, entity_type, entity_id, outcome)
    # Structured log line: identifiers and outcome only (no justification text, names, tokens or source contents).
    logger.info("governance_event", extra={"event": "governance_event", "action": action, "entity_type": entity_type, "entity_id": entity_id[:32], "outcome": outcome, "team_id": team_id, "actor_id": actor})


def paginate(page: int, page_size: int) -> tuple[int, int]:
    page_size = max(1, min(page_size, 100))
    return (max(1, page) - 1) * page_size, page_size
