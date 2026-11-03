"""Operational intelligence API: health, metrics, logs, traces, incidents, alerts, SLOs, diagnostics, notifications.

Authorization: every route needs the normal request context. Process-wide telemetry that can carry other tenants' identifiers
(metrics, logs, traces, retention) is restricted to the workspace owner. Incidents, alert rules, SLOs and deliveries are workspace-scoped.
"""
from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, Query
from fastapi.responses import PlainTextResponse
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.core.config import Settings, get_settings
from app.core.errors import ForbiddenError
from app.db.session import get_db
from app.models import OpsConfig, OpsDelivery, OpsEvaluation, OpsEvent, OpsIncident
from app.observability import alerts, diagnostics, notify, periodic, retention, slo, tracing
from app.observability import incidents as inc_svc
from app.observability import metrics as m
from app.observability.errors import OpsNotFound
from app.observability.health import dependency_health
from app.observability.redact import clean
from app.schemas.common import Page, make_page
from app.schemas.ops import DeploymentBody, IncidentActionBody, RuleUpdateBody, SloUpdateBody
from app.security.auth import RequestContext, get_context
from app.services import audit

router = APIRouter(prefix="/ops", tags=["operations"])
SIGNALS = [
    {"signal": "structured_logs", "status": "implemented", "exported": "stderr JSON; bounded in-memory ring for the investigation view", "limits": "ring lost on restart"},
    {"signal": "correlation_ids", "status": "implemented", "exported": "X-Request-ID response header, log fields", "limits": "incoming ids are validated and are never identity"},
    {"signal": "traces", "status": "implemented_local", "exported": "optional exporter callable (off by default)", "limits": "in-memory ring; sampling configurable; OTLP export not verified against a collector"},
    {"signal": "metrics", "status": "implemented", "exported": "Prometheus text at /api/v1/ops/metrics (authenticated)", "limits": "in-memory; reset on restart; bounded label cardinality"},
    {"signal": "health", "status": "implemented", "exported": "/health, /ready, /api/v1/ops/health", "limits": "dependency probes run on demand with a short cache"},
    {"signal": "alerts", "status": "implemented", "exported": "incidents + optional webhook/Slack", "limits": "evaluated on request; periodic only if enabled; email not implemented"},
    {"signal": "slo", "status": "implemented_local", "exported": "API", "limits": "computed from in-memory windows (max 24h); restart resets history"},
]


def _admin(ctx: RequestContext) -> None:
    if ctx.role != "owner":
        raise ForbiddenError("This operational view is restricted to the workspace owner.")


def _inc(i: OpsIncident) -> dict[str, Any]:
    return {c: (getattr(i, c).isoformat() if hasattr(getattr(i, c), "isoformat") else getattr(i, c)) for c in (
        "id", "fingerprint", "source", "rule_id", "rule_version", "severity", "component", "title", "summary", "status", "occurrences", "evidence", "correlation_id",
        "first_seen_at", "last_seen_at", "acknowledged_by", "acknowledged_at", "resolved_at", "resolution_note", "last_notified_at")}


def _ev(e: OpsEvent) -> dict[str, Any]:
    return {"id": e.id, "incident_id": e.incident_id, "event_type": e.event_type, "severity": e.severity, "component": e.component, "occurred_at": e.occurred_at.isoformat(),
            "detected_at": e.detected_at.isoformat(), "request_id": e.request_id, "trace_id": e.trace_id, "summary": e.summary, "evidence": e.evidence, "actor_user_id": e.actor_user_id, "note": e.note}


def _cfg(c: OpsConfig) -> dict[str, Any]:
    return {"key": c.key, "kind": c.kind, "version": c.version, "enabled": c.enabled, "definition": c.definition, "created_by": c.created_by, "created_at": c.created_at.isoformat()}


@router.get("/overview")
def overview(ctx: RequestContext = Depends(get_context), db: Session = Depends(get_db), settings: Settings = Depends(get_settings)) -> dict[str, Any]:
    res = m.update_resources()
    health = dependency_health(settings)
    http = m.window_sum("http", 60)
    jobs = m.window_sum("jobs", 60)
    slos = slo.evaluate_all(db, ctx.workspace.id)
    db.commit()
    wf = {st.split(":", 1)[1]: m.window_sum(st, 60) for st in m.streams("workflow:")}
    return {
        "health": {"status": health["status"], "critical_unavailable": health["critical_unavailable"], "degraded": health["degraded"]},
        "resources": res, "observed_since": m.started_at_iso(),
        "requests_last_hour": {"total": http.get("total", 0), "errors_5xx": http.get("error5xx", 0), "p95_seconds": m.quantile("http_latency", 60, 0.95)},
        "jobs_last_hour": {"total": jobs.get("total", 0), "failed": jobs.get("bad", 0), "in_flight": sum(m.ACTIVE_JOBS.series.values()) if m.ACTIVE_JOBS.series else 0},
        "workflows_last_hour": wf, "open_incidents": alerts.count_open(db, ctx.workspace.id),
        "slos": [{"slo_id": s["slo_id"], "status": s["status"], "sli_value": s["sli_value"], "target": s["target"]} for s in slos],
        "notifications": notify.status(settings), "periodic_evaluation": {"enabled": settings.ops_periodic_evaluation, **periodic.STATE},
        "telemetry_dropped": {"/".join(k_[0][1:2]) if k_ else "": v for k_, v in m.TELEMETRY_DROPPED.series.items()},
        "limitations": ["Metrics, spans and recent logs are in memory and reset on restart.", "No continuous monitoring unless periodic evaluation is enabled."],
    }


@router.get("/signals")
def signals(_: RequestContext = Depends(get_context)) -> dict[str, Any]:
    return {"signals": SIGNALS}


@router.get("/health")
def health_deps(force: bool = False, _: RequestContext = Depends(get_context), settings: Settings = Depends(get_settings)) -> dict[str, Any]:
    return dependency_health(settings, force=force)


@router.get("/metrics", response_class=PlainTextResponse)
def metrics(ctx: RequestContext = Depends(get_context), settings: Settings = Depends(get_settings)) -> PlainTextResponse:
    _admin(ctx)
    m.update_resources()
    return PlainTextResponse(m.REGISTRY.render(), media_type="text/plain; version=0.0.4")


@router.get("/logs")
def logs(level: str | None = Query(None, max_length=10), event: str | None = Query(None, max_length=60), request_id: str | None = Query(None, max_length=64),
         q: str | None = Query(None, max_length=100), limit: int = Query(100, ge=1, le=500), ctx: RequestContext = Depends(get_context)) -> dict[str, Any]:
    _admin(ctx)
    from app.core.logging import LOG_RING

    items = [r for r in reversed(list(LOG_RING)) if (not level or r.get("level") == level.upper()) and (not event or r.get("event") == event)
             and (not request_id or r.get("request_id") == request_id) and (not q or q.lower() in str(r.get("message", "")).lower())]
    return {"items": items[:limit], "buffered": len(LOG_RING), "capacity": LOG_RING.maxlen, "note": "Recent redacted records from a bounded in-memory ring; lost on restart. Stack traces are not served."}


@router.get("/traces")
def traces(trace_id: str | None = Query(None, max_length=32), request_id: str | None = Query(None, max_length=64), limit: int = Query(100, ge=1, le=500), ctx: RequestContext = Depends(get_context)) -> dict[str, Any]:
    _admin(ctx)
    return {"items": tracing.recent(limit, trace_id, request_id), "sample_ratio": tracing.CONFIG["sample_ratio"], "buffered": len(tracing.SPANS), "capacity": tracing.SPANS.maxlen,
            "exporter_enabled": tracing.CONFIG["exporter"] is not None}


@router.get("/incidents")
def list_incidents(status: str | None = Query(None, max_length=14), severity: str | None = Query(None, max_length=10), page: int = Query(1, ge=1), page_size: int = Query(25, ge=1, le=100),
                   ctx: RequestContext = Depends(get_context), db: Session = Depends(get_db)) -> Page[dict[str, Any]]:
    q = select(OpsIncident).where(OpsIncident.workspace_id == ctx.workspace.id)
    if status:
        q = q.where(OpsIncident.status == status)
    if severity:
        q = q.where(OpsIncident.severity == severity)
    total = db.scalar(select(func.count()).select_from(q.subquery())) or 0
    rows = db.scalars(q.order_by(OpsIncident.last_seen_at.desc()).offset((page - 1) * page_size).limit(page_size)).all()
    return make_page([_inc(r) for r in rows], total, page, page_size)


@router.get("/incidents/{incident_id}")
def get_incident(incident_id: str, ctx: RequestContext = Depends(get_context), db: Session = Depends(get_db)) -> dict[str, Any]:
    inc = inc_svc.get_incident(db, ctx, incident_id)
    events = db.scalars(select(OpsEvent).where(OpsEvent.incident_id == inc.id).order_by(OpsEvent.occurred_at)).all()
    deliveries = db.scalars(select(OpsDelivery).where(OpsDelivery.incident_id == inc.id).order_by(OpsDelivery.created_at)).all()
    return {**_inc(inc), "history": [_ev(e) for e in events], "deliveries": [_dl(d) for d in deliveries]}


@router.post("/incidents/{incident_id}/{action}")
def incident_action(incident_id: str, action: str, body: IncidentActionBody, ctx: RequestContext = Depends(get_context), db: Session = Depends(get_db)) -> dict[str, Any]:
    if action not in inc_svc.ACTION_STATUS:
        raise OpsNotFound("unknown_action", "Unknown incident action.", {"allowed": list(inc_svc.ACTION_STATUS)})
    return _inc(inc_svc.transition(db, ctx, incident_id, action, body.note))


@router.get("/events")
def list_events(event_type: str | None = Query(None, max_length=40), page: int = Query(1, ge=1), page_size: int = Query(25, ge=1, le=100),
                ctx: RequestContext = Depends(get_context), db: Session = Depends(get_db)) -> Page[dict[str, Any]]:
    q = select(OpsEvent).where(OpsEvent.workspace_id == ctx.workspace.id)
    if event_type:
        q = q.where(OpsEvent.event_type == event_type)
    total = db.scalar(select(func.count()).select_from(q.subquery())) or 0
    rows = db.scalars(q.order_by(OpsEvent.occurred_at.desc()).offset((page - 1) * page_size).limit(page_size)).all()
    return make_page([_ev(r) for r in rows], total, page, page_size)


@router.post("/events/deployment")
def record_deployment(body: DeploymentBody, ctx: RequestContext = Depends(get_context), db: Session = Depends(get_db)) -> dict[str, Any]:
    ctx.require_write()
    ev = inc_svc.record_event(db, ctx.workspace.id, "deployment_recorded", component="platform", summary=body.summary, evidence={"version": body.version, **(body.details or {})}, actor=ctx.user.id)
    db.flush()
    audit.record(db, workspace_id=ctx.workspace.id, actor_user_id=ctx.user.id, action="ops.deployment.record", entity_type="ops_event", entity_id=ev.id, details={"version": body.version})
    db.commit()
    return _ev(ev)


@router.get("/alerts/rules")
def list_rules(ctx: RequestContext = Depends(get_context), db: Session = Depends(get_db)) -> dict[str, Any]:
    alerts.ensure_defaults(db, ctx.workspace.id)
    db.commit()
    return {"items": [_cfg(c) for c in alerts.current_configs(db, ctx.workspace.id, "alert_rule").values()], "metrics": list(alerts.METRICS)}


@router.put("/alerts/rules/{key}")
def update_rule(key: str, body: RuleUpdateBody, ctx: RequestContext = Depends(get_context), db: Session = Depends(get_db)) -> dict[str, Any]:
    return _cfg(alerts.put_rule(db, ctx, key, body.model_dump(exclude={"enabled"}, exclude_none=True), body.enabled))


@router.get("/alerts/rules/{key}/versions")
def rule_versions(key: str, ctx: RequestContext = Depends(get_context), db: Session = Depends(get_db)) -> dict[str, Any]:
    rows = db.scalars(select(OpsConfig).where(OpsConfig.workspace_id == ctx.workspace.id, OpsConfig.kind == "alert_rule", OpsConfig.key == key).order_by(OpsConfig.version.desc())).all()
    if not rows:
        raise OpsNotFound("rule_not_found", "Unknown alert rule.")
    return {"items": [_cfg(c) for c in rows]}


@router.post("/alerts/evaluate")
def evaluate(ctx: RequestContext = Depends(get_context), db: Session = Depends(get_db), settings: Settings = Depends(get_settings)) -> dict[str, Any]:
    ctx.require_write()
    results = alerts.evaluate_all(db, ctx.workspace.id, settings, notify=lambda d, s, i: notify.dispatch(d, s, i), actor=ctx.user.id)
    return {"evaluations": results, "evaluated_at": results[0]["evaluated_at"] if results else None, "continuous_monitoring": settings.ops_periodic_evaluation}


@router.get("/alerts/evaluations")
def list_evaluations(rule_id: str | None = Query(None, max_length=60), page: int = Query(1, ge=1), page_size: int = Query(25, ge=1, le=100),
                     ctx: RequestContext = Depends(get_context), db: Session = Depends(get_db)) -> Page[dict[str, Any]]:
    q = select(OpsEvaluation).where(OpsEvaluation.workspace_id == ctx.workspace.id)
    if rule_id:
        q = q.where(OpsEvaluation.rule_id == rule_id)
    total = db.scalar(select(func.count()).select_from(q.subquery())) or 0
    rows = db.scalars(q.order_by(OpsEvaluation.created_at.desc()).offset((page - 1) * page_size).limit(page_size)).all()
    items = [{"id": r.id, "rule_id": r.rule_id, "rule_version": r.rule_version, "status": r.status, "observed": r.observed, "threshold": r.threshold, "detail": r.detail, "incident_id": r.incident_id, "created_at": r.created_at.isoformat()} for r in rows]
    return make_page(items, total, page, page_size)


@router.get("/slos")
def list_slos(ctx: RequestContext = Depends(get_context), db: Session = Depends(get_db)) -> dict[str, Any]:
    items = slo.evaluate_all(db, ctx.workspace.id)
    db.commit()
    return {"items": items}


@router.get("/slos/{key}/versions")
def slo_versions(key: str, ctx: RequestContext = Depends(get_context), db: Session = Depends(get_db)) -> dict[str, Any]:
    rows = slo.versions(db, ctx.workspace.id, key)
    if not rows:
        raise OpsNotFound("slo_not_found", "Unknown SLO.")
    return {"items": [_cfg(c) for c in rows]}


@router.put("/slos/{key}")
def update_slo(key: str, body: SloUpdateBody, ctx: RequestContext = Depends(get_context), db: Session = Depends(get_db)) -> dict[str, Any]:
    return _cfg(slo.put_slo(db, ctx, key, body.model_dump(exclude_none=True)))


@router.get("/diagnostics")
def get_diagnostics(window_minutes: int = Query(60, ge=5, le=1440), ctx: RequestContext = Depends(get_context), db: Session = Depends(get_db)) -> dict[str, Any]:
    return diagnostics.run(db, ctx.workspace.id, window=window_minutes)


@router.get("/notifications")
def notification_status(_: RequestContext = Depends(get_context), settings: Settings = Depends(get_settings)) -> dict[str, Any]:
    return notify.status(settings)


def _dl(d: OpsDelivery) -> dict[str, Any]:
    return {"id": d.id, "incident_id": d.incident_id, "channel": d.channel, "destination": d.destination, "idempotency_key": d.idempotency_key, "attempt": d.attempt,
            "status": d.status, "http_status": d.http_status, "error_class": d.error_class, "created_at": d.created_at.isoformat()}


@router.get("/deliveries")
def list_deliveries(page: int = Query(1, ge=1), page_size: int = Query(25, ge=1, le=100), ctx: RequestContext = Depends(get_context), db: Session = Depends(get_db)) -> Page[dict[str, Any]]:
    q = select(OpsDelivery).where(OpsDelivery.workspace_id == ctx.workspace.id)
    total = db.scalar(select(func.count()).select_from(q.subquery())) or 0
    rows = db.scalars(q.order_by(OpsDelivery.created_at.desc()).offset((page - 1) * page_size).limit(page_size)).all()
    return make_page([_dl(r) for r in rows], total, page, page_size)


@router.post("/retention/run")
def run_retention(ctx: RequestContext = Depends(get_context), db: Session = Depends(get_db), settings: Settings = Depends(get_settings)) -> dict[str, Any]:
    ctx.require_write()
    _admin(ctx)
    res = retention.run(db, ctx.workspace.id, settings.ops_retention_days)
    audit.record(db, workspace_id=ctx.workspace.id, actor_user_id=ctx.user.id, action="ops.retention.run", entity_type="workspace", entity_id=ctx.workspace.id, details=clean(res["deleted"]))
    db.commit()
    return res
