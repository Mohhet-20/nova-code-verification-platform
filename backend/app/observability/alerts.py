"""Alert rules backed by real measurements, versioned configuration, explainable evaluation, dedupe through incidents.

Evaluation runs when requested (POST /ops/alerts/evaluate) or, only if `ops_periodic_evaluation` is enabled, on a background interval.
Without that setting NOVA makes no claim of continuous monitoring.
"""
from __future__ import annotations

import logging
import operator
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.core.config import Settings
from app.db.base import utcnow
from app.models import OpsConfig, OpsEvaluation, OpsIncident
from app.observability import incidents as inc_svc
from app.observability import metrics as m
from app.observability.errors import OpsInvalid, OpsNotFound
from app.observability.redact import clean_str
from app.security.auth import RequestContext
from app.services import audit

log = logging.getLogger("nova.alerts")
COMPARATORS: dict[str, Callable[[float, float], bool]] = {">": operator.gt, ">=": operator.ge, "<": operator.lt, "<=": operator.le}
METRICS = ("http_error_rate", "http_latency_p95", "job_failure_rate", "queue_delay_p95", "db_connectivity", "security_scan_failures", "ci_failures", "persistent_readiness_degradation")
COMPONENTS = {"http_error_rate": "api", "http_latency_p95": "api", "job_failure_rate": "jobs", "queue_delay_p95": "jobs", "db_connectivity": "database",
              "security_scan_failures": "security", "ci_failures": "cicd", "persistent_readiness_degradation": "platform"}

DEFAULT_RULES: list[dict[str, Any]] = [
    {"id": "http_error_rate", "description": "Share of API requests answered with 5xx (health checks excluded).", "metric": "http_error_rate", "window_minutes": 15, "comparator": ">", "threshold": 0.05, "min_samples": 20, "severity": "critical", "cooldown_minutes": 30},
    {"id": "http_latency_p95", "description": "p95 API latency (upper histogram bucket bound, seconds).", "metric": "http_latency_p95", "window_minutes": 15, "comparator": ">", "threshold": 2.0, "min_samples": 20, "severity": "warning", "cooldown_minutes": 30},
    {"id": "job_failure_rate", "description": "Share of background jobs that failed.", "metric": "job_failure_rate", "window_minutes": 60, "comparator": ">", "threshold": 0.25, "min_samples": 4, "severity": "warning", "cooldown_minutes": 30},
    {"id": "queue_delay_p95", "description": "p95 delay between job creation and start (only jobs with a creation time).", "metric": "queue_delay_p95", "window_minutes": 30, "comparator": ">", "threshold": 30.0, "min_samples": 3, "severity": "warning", "cooldown_minutes": 30},
    {"id": "db_connectivity", "description": "Database unreachable (1 = unavailable) on a fresh probe.", "metric": "db_connectivity", "window_minutes": 1, "comparator": ">=", "threshold": 1.0, "min_samples": 1, "severity": "critical", "cooldown_minutes": 15},
    {"id": "security_scan_failures", "description": "Security scans that failed in the window.", "metric": "security_scan_failures", "window_minutes": 60, "comparator": ">=", "threshold": 3.0, "min_samples": 1, "severity": "warning", "cooldown_minutes": 60},
    {"id": "ci_failures", "description": "CI/CD pipeline runs that failed in the window.", "metric": "ci_failures", "window_minutes": 60, "comparator": ">=", "threshold": 3.0, "min_samples": 1, "severity": "warning", "cooldown_minutes": 60},
    {"id": "persistent_readiness_degradation", "description": "Consecutive evaluations in which a critical dependency was unavailable.", "metric": "persistent_readiness_degradation", "window_minutes": 60, "comparator": ">=", "threshold": 3.0, "min_samples": 1, "severity": "warning", "cooldown_minutes": 60},
]


def _aware(dt: datetime | None) -> datetime | None:
    return dt if dt is None or dt.tzinfo else dt.replace(tzinfo=UTC)


def validate_rule(d: dict[str, Any]) -> dict[str, Any]:
    try:
        rule = {
            "id": str(d["id"]), "description": clean_str(str(d.get("description", "")), 300), "metric": str(d["metric"]), "window_minutes": int(d["window_minutes"]),
            "comparator": str(d.get("comparator", ">")), "threshold": float(d["threshold"]), "min_samples": int(d.get("min_samples", 1)),
            "severity": str(d.get("severity", "warning")), "cooldown_minutes": int(d.get("cooldown_minutes", 30)), "scope": "workspace",
        }
    except (KeyError, TypeError, ValueError) as exc:
        raise OpsInvalid("invalid_rule", f"Invalid alert rule: {type(exc).__name__}") from exc
    if rule["metric"] not in METRICS:
        raise OpsInvalid("unknown_metric", "Rules can only use measured metrics.", {"allowed": list(METRICS)})
    if rule["comparator"] not in COMPARATORS:
        raise OpsInvalid("invalid_comparator", "Unsupported comparator.", {"allowed": list(COMPARATORS)})
    if rule["severity"] not in inc_svc.SEVERITIES:
        raise OpsInvalid("invalid_severity", "Unsupported severity.")
    if not 1 <= rule["window_minutes"] <= m.WINDOW_MINUTES or rule["cooldown_minutes"] < 0 or rule["min_samples"] < 1:
        raise OpsInvalid("invalid_window", "Window must be 1..1440 minutes; cooldown >= 0; min_samples >= 1.")
    return rule


def current_configs(db: Session, workspace_id: str, kind: str) -> dict[str, OpsConfig]:
    rows = db.scalars(select(OpsConfig).where(OpsConfig.workspace_id == workspace_id, OpsConfig.kind == kind).order_by(OpsConfig.key, OpsConfig.version)).all()
    out: dict[str, OpsConfig] = {}
    for r in rows:
        out[r.key] = r  # ascending version: the last wins
    return out


def ensure_defaults(db: Session, workspace_id: str) -> None:
    have = current_configs(db, workspace_id, "alert_rule")
    for d in DEFAULT_RULES:
        if d["id"] not in have:
            db.add(OpsConfig(workspace_id=workspace_id, kind="alert_rule", key=d["id"], version=1, definition=validate_rule(d), enabled=True))
    db.flush()


def put_rule(db: Session, ctx: RequestContext, key: str, changes: dict[str, Any], enabled: bool | None) -> OpsConfig:
    ctx.require_write()
    ensure_defaults(db, ctx.workspace.id)
    cur = current_configs(db, ctx.workspace.id, "alert_rule").get(key)
    if cur is None:
        raise OpsNotFound("rule_not_found", "Unknown alert rule.")
    new_def = validate_rule({**cur.definition, **{k: v for k, v in changes.items() if k != "id" and v is not None}, "id": key})
    row = OpsConfig(workspace_id=ctx.workspace.id, kind="alert_rule", key=key, version=cur.version + 1, definition=new_def,
                    enabled=cur.enabled if enabled is None else enabled, created_by=ctx.user.id)
    db.add(row)
    db.flush()
    inc_svc.record_event(db, ctx.workspace.id, "config_changed", component="alerts", summary=f"alert rule {key} v{row.version}", actor=ctx.user.id, evidence={"rule": key, "version": row.version})
    audit.record(db, workspace_id=ctx.workspace.id, actor_user_id=ctx.user.id, action="ops.alert_rule.update", entity_type="ops_config", entity_id=row.id, details={"rule": key, "version": row.version})
    db.commit()
    return row


# ---- measurements: each returns (observed | None, samples, detail) ----
def _measure(metric: str, rule: dict[str, Any], db: Session, workspace_id: str, settings: Settings, now: float | None) -> tuple[float | None, float, dict[str, Any]]:
    w = rule["window_minutes"]
    if metric == "http_error_rate":
        s = m.window_sum("http", w, now)
        total = s.get("total", 0.0)
        return ((s.get("error5xx", 0.0) / total) if total else None), total, {"requests": total, "errors_5xx": s.get("error5xx", 0.0), "stream": "http"}
    if metric == "http_latency_p95":
        q = m.quantile("http_latency", w, 0.95, now)
        n = m.window_sum("http_latency", w, now).get("count", 0.0)
        return q, n, {"samples": n, "quantile": 0.95, "stream": "http_latency"}
    if metric == "job_failure_rate":
        s = m.window_sum("jobs", w, now)
        total = s.get("total", 0.0)
        return ((s.get("bad", 0.0) / total) if total else None), total, {"jobs": total, "failed": s.get("bad", 0.0), "stream": "jobs"}
    if metric == "queue_delay_p95":
        q = m.quantile("queue_all", w, 0.95, now)
        n = m.window_sum("queue_all", w, now).get("count", 0.0)
        return q, n, {"samples": n, "quantile": 0.95, "stream": "queue_all"}
    if metric in ("security_scan_failures", "ci_failures"):
        wf = "security_scan" if metric == "security_scan_failures" else "cicd"
        s = m.window_sum(f"workflow:{wf}", w, now)
        seen = sum(s.values())
        return (s.get("failed", 0.0) if seen else None), seen, {"workflow": wf, "outcomes": s}
    if metric == "db_connectivity":
        from app.observability.health import dependency_health

        h = dependency_health(settings, force=True)
        db_state = next(d for d in h["dependencies"] if d["name"] == "database")
        return (0.0 if db_state["state"] == "ok" else 1.0), 1.0, {"state": db_state["state"], "error_class": db_state.get("error_class")}
    if metric == "persistent_readiness_degradation":
        from app.observability.health import dependency_health

        h = dependency_health(settings, force=True)
        bad_now = 1 if h["critical_unavailable"] else 0
        need = int(rule["threshold"])
        prior = db.scalars(select(OpsEvaluation).where(OpsEvaluation.workspace_id == workspace_id, OpsEvaluation.rule_id == rule["id"]).order_by(OpsEvaluation.created_at.desc()).limit(max(0, need - 1))).all()
        streak = bad_now
        for p in prior:
            if bad_now and p.detail.get("bad_now"):
                streak += 1
            else:
                break
        return float(streak), 1.0, {"bad_now": bad_now, "critical_unavailable": h["critical_unavailable"], "consecutive": streak}
    return None, 0.0, {}


def evaluate_rule(db: Session, workspace_id: str, cfg: OpsConfig, settings: Settings, now: float | None = None) -> dict[str, Any]:
    rule = cfg.definition
    base = {"rule_id": cfg.key, "rule_version": cfg.version, "description": rule["description"], "metric": rule["metric"], "window_minutes": rule["window_minutes"],
            "comparator": rule["comparator"], "threshold": rule["threshold"], "min_samples": rule["min_samples"], "severity": rule["severity"], "scope": "workspace",
            "evaluated_at": datetime.now(UTC).isoformat()}
    if not cfg.enabled:
        return {**base, "status": "disabled", "fired": False, "observed": None, "samples": 0, "missing_data": False, "explanation": "Rule is disabled.", "measurements": {}}
    observed, samples, detail = _measure(rule["metric"], rule, db, workspace_id, settings, now)
    if observed is None or samples < rule["min_samples"]:
        why = "no measurements in the window" if observed is None else f"{int(samples)} sample(s) < min_samples {rule['min_samples']}"
        return {**base, "status": "no_data", "fired": False, "observed": observed, "samples": samples, "missing_data": True, "measurements": detail,
                "explanation": f"Not evaluated: {why}. In-memory telemetry starts at process start ({m.started_at_iso()})."}
    fired = COMPARATORS[rule["comparator"]](observed, rule["threshold"])
    return {**base, "status": "firing" if fired else "ok", "fired": fired, "observed": observed, "samples": samples, "missing_data": False, "measurements": detail,
            "explanation": f"{rule['metric']} observed {observed:.4g} {'meets' if fired else 'does not meet'} the condition {rule['comparator']} {rule['threshold']} over {rule['window_minutes']} min ({int(samples)} samples)."}


def evaluate_all(db: Session, workspace_id: str, settings: Settings, *, notify: Callable[..., Any] | None = None, now: float | None = None, actor: str | None = None) -> list[dict[str, Any]]:
    ensure_defaults(db, workspace_id)
    results: list[dict[str, Any]] = []
    for cfg in current_configs(db, workspace_id, "alert_rule").values():
        res = evaluate_rule(db, workspace_id, cfg, settings, now)
        fp = inc_svc.fingerprint(cfg.key, "workspace")
        incident_id = None
        res["incident"] = None
        if res["status"] == "firing":
            inc, tr = inc_svc.raise_incident(db, workspace_id, fp=fp, source="alert", severity=cfg.definition["severity"], component=m_component(cfg.definition["metric"]),
                                             title=f"Alert: {cfg.key}", summary=res["explanation"], evidence={"rule": cfg.key, "version": cfg.version, "observed": res["observed"], "threshold": cfg.definition["threshold"], "window_minutes": cfg.definition["window_minutes"], "measurements": res["measurements"]},
                                             rule_id=cfg.key, rule_version=cfg.version)
            incident_id = inc.id
            last = _aware(inc.last_notified_at)
            cooled = last is None or utcnow() - last >= timedelta(minutes=cfg.definition["cooldown_minutes"])
            res["incident"] = {"id": inc.id, "transition": tr, "status": inc.status, "notification_suppressed_by_cooldown": tr == "recurred" and not cooled}
            if notify is not None and inc.status != "acknowledged" and (tr in ("opened", "reopened") or cooled):
                try:
                    notify(db, settings, inc)
                    inc.last_notified_at = utcnow()
                except Exception as exc:  # noqa: BLE001 - a notification failure must never break evaluation
                    log.warning("notification_failed", extra={"event": "notification_failed", "error_class": type(exc).__name__, "rule_id": cfg.key})
        elif res["status"] == "ok":
            closed = inc_svc.auto_resolve(db, workspace_id, fp, "Condition cleared on evaluation.")
            if closed is not None:
                incident_id = closed.id
                res["incident"] = {"id": closed.id, "transition": "auto_resolved", "status": "resolved"}
        db.add(OpsEvaluation(workspace_id=workspace_id, rule_id=cfg.key, rule_version=cfg.version, status=res["status"], observed=res["observed"], threshold=cfg.definition["threshold"],
                             detail={**res["measurements"], "explanation": res["explanation"], "samples": res["samples"]}, incident_id=incident_id))
        results.append(res)
    db.commit()
    return results


def m_component(metric: str) -> str:
    return COMPONENTS.get(metric, "platform")


def count_open(db: Session, workspace_id: str) -> int:
    return db.scalar(select(func.count()).select_from(OpsIncident).where(OpsIncident.workspace_id == workspace_id, OpsIncident.status != "resolved")) or 0
