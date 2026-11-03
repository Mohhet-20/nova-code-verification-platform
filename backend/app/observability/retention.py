"""Retention for operational telemetry records. Audit events are never touched."""
from __future__ import annotations

from datetime import timedelta
from typing import Any

from sqlalchemy import delete, or_, select
from sqlalchemy.orm import Session

from app.db.base import utcnow
from app.models import OpsDelivery, OpsEvaluation, OpsEvent, OpsIncident


def run(db: Session, workspace_id: str, days: int) -> dict[str, Any]:
    cutoff = utcnow() - timedelta(days=days)
    resolved = select(OpsIncident.id).where(OpsIncident.workspace_id == workspace_id, OpsIncident.status == "resolved")
    ev = db.execute(delete(OpsEvent).where(OpsEvent.workspace_id == workspace_id, OpsEvent.occurred_at < cutoff, or_(OpsEvent.incident_id.is_(None), OpsEvent.incident_id.in_(resolved))))
    evals = db.execute(delete(OpsEvaluation).where(OpsEvaluation.workspace_id == workspace_id, OpsEvaluation.created_at < cutoff))
    deliveries = db.execute(delete(OpsDelivery).where(OpsDelivery.workspace_id == workspace_id, OpsDelivery.created_at < cutoff))
    return {"retention_days": days, "cutoff": cutoff.isoformat(), "deleted": {"events": ev.rowcount or 0, "evaluations": evals.rowcount or 0, "deliveries": deliveries.rowcount or 0},
            "untouched": ["audit_events", "ops_configs", "ops_incidents", "events of unresolved incidents"]}
