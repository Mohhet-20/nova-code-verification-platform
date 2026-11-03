"""SLIs, SLOs and error budgets computed from real recorded operations (in-memory windows).

Limits stated in every result: telemetry is in memory and starts at process start; a restart resets it; windows longer than the observed
history are reported with the effective (observed) minutes and flagged. No data is never reported as zero failures.
"""
from __future__ import annotations

import math
import time
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import OpsConfig
from app.observability import incidents as inc_svc
from app.observability import metrics as m
from app.observability.alerts import current_configs
from app.observability.errors import OpsInvalid, OpsNotFound
from app.observability.redact import clean_str
from app.security.auth import RequestContext
from app.services import audit

SLI_KINDS = ("availability", "latency", "job_completion", "workflow_success", "dependency_availability")
LIMITATIONS = ["Telemetry is held in memory and starts at process start; a restart resets history.", "Windows are capped at 24 hours."]
DEFAULT_SLOS: list[dict[str, Any]] = [
    {"id": "api_availability", "description": "Share of API requests that did not return 5xx. Health/readiness/metrics requests are excluded by rule.", "sli": "availability", "target": 0.995, "window_minutes": 1440, "min_samples": 30, "exclusions": ["health_checks"]},
    {"id": "api_latency", "description": "Share of API requests completed within the latency threshold.", "sli": "latency", "target": 0.95, "window_minutes": 1440, "min_samples": 30, "threshold_seconds": 1.0, "exclusions": ["health_checks"]},
    {"id": "job_completion", "description": "Share of background jobs that did not fail.", "sli": "job_completion", "target": 0.95, "window_minutes": 1440, "min_samples": 10, "exclusions": []},
    {"id": "workflow_success", "description": "Share of workflow executions (all workflows) that succeeded.", "sli": "workflow_success", "target": 0.9, "window_minutes": 1440, "min_samples": 10, "workflow": "*", "exclusions": []},
    {"id": "dependency_availability", "description": "Share of dependency probes that were healthy. Probes run only when health is requested.", "sli": "dependency_availability", "target": 0.99, "window_minutes": 1440, "min_samples": 10, "exclusions": []},
]


def validate_slo(d: dict[str, Any]) -> dict[str, Any]:
    try:
        slo = {"id": str(d["id"]), "description": clean_str(str(d.get("description", "")), 300), "sli": str(d["sli"]), "target": float(d["target"]),
               "window_minutes": int(d["window_minutes"]), "min_samples": int(d.get("min_samples", 30)), "exclusions": [str(x) for x in d.get("exclusions", [])][:5]}
        if "threshold_seconds" in d:
            slo["threshold_seconds"] = float(d["threshold_seconds"])
        if "workflow" in d:
            slo["workflow"] = str(d["workflow"])[:40]
    except (KeyError, TypeError, ValueError) as exc:
        raise OpsInvalid("invalid_slo", f"Invalid SLO: {type(exc).__name__}") from exc
    if slo["sli"] not in SLI_KINDS:
        raise OpsInvalid("unknown_sli", "Unsupported SLI.", {"allowed": list(SLI_KINDS)})
    if not 0.0 < slo["target"] < 1.0 or not 1 <= slo["window_minutes"] <= m.WINDOW_MINUTES or slo["min_samples"] < 1:
        raise OpsInvalid("invalid_slo", "Target must be in (0,1), window 1..1440 minutes, min_samples >= 1.")
    if slo["sli"] == "latency" and slo.get("threshold_seconds", 0) <= 0:
        raise OpsInvalid("invalid_slo", "Latency SLOs need threshold_seconds > 0.")
    if any(x not in ("health_checks",) for x in slo["exclusions"]):
        raise OpsInvalid("invalid_exclusion", "Only the 'health_checks' exclusion is supported.")
    return slo


def ensure_defaults(db: Session, workspace_id: str) -> None:
    have = current_configs(db, workspace_id, "slo")
    for d in DEFAULT_SLOS:
        if d["id"] not in have:
            db.add(OpsConfig(workspace_id=workspace_id, kind="slo", key=d["id"], version=1, definition=validate_slo(d), enabled=True))
    db.flush()


def put_slo(db: Session, ctx: RequestContext, key: str, changes: dict[str, Any]) -> OpsConfig:
    ctx.require_write()
    ensure_defaults(db, ctx.workspace.id)
    cur = current_configs(db, ctx.workspace.id, "slo").get(key)
    if cur is None:
        raise OpsNotFound("slo_not_found", "Unknown SLO.")
    new_def = validate_slo({**cur.definition, **{k: v for k, v in changes.items() if k != "id" and v is not None}, "id": key})
    row = OpsConfig(workspace_id=ctx.workspace.id, kind="slo", key=key, version=cur.version + 1, definition=new_def, enabled=True, created_by=ctx.user.id)
    db.add(row)
    db.flush()
    inc_svc.record_event(db, ctx.workspace.id, "config_changed", component="slo", summary=f"SLO {key} v{row.version}", actor=ctx.user.id, evidence={"slo": key, "version": row.version})
    audit.record(db, workspace_id=ctx.workspace.id, actor_user_id=ctx.user.id, action="ops.slo.update", entity_type="ops_config", entity_id=row.id, details={"slo": key, "version": row.version})
    db.commit()
    return row


def versions(db: Session, workspace_id: str, key: str) -> list[OpsConfig]:
    return list(db.scalars(select(OpsConfig).where(OpsConfig.workspace_id == workspace_id, OpsConfig.kind == "slo", OpsConfig.key == key).order_by(OpsConfig.version.desc())).all())


def _good_total(slo: dict[str, Any], now: float | None) -> tuple[float, float, str, dict[str, Any]]:
    w = slo["window_minutes"]
    k = slo["sli"]
    if k == "availability":
        s = m.window_sum("http", w, now)
        return s.get("ok", 0.0), s.get("total", 0.0), "good = requests not answered with 5xx; total = requests (health checks excluded)", {"source": "http request stream"}
    if k == "latency":
        r = m.count_within("http_latency", w, slo["threshold_seconds"], now)
        good, total = r if r else (0.0, 0.0)
        eff = next((b for b in m.LATENCY_BUCKETS if b >= slo["threshold_seconds"]), None)
        return good, total, "good = requests at or below the smallest histogram bucket >= threshold; total = requests", {"source": "http latency histogram", "effective_bucket_seconds": eff}
    if k == "job_completion":
        s = m.window_sum("jobs", w, now)
        return s.get("good", 0.0), s.get("total", 0.0), "good = jobs that did not fail; total = jobs finished", {"source": "job tracker"}
    if k == "workflow_success":
        wf = slo.get("workflow", "*")
        streams = m.streams("workflow:") if wf == "*" else [f"workflow:{wf}"]
        good = total = 0.0
        for st in streams:
            s = m.window_sum(st, w, now)
            good += s.get("succeeded", 0.0)
            total += sum(v for kk, v in s.items() if kk in ("succeeded", "failed", "cancelled", "timed_out"))
        return good, total, "good = workflow executions that succeeded; total = succeeded + failed + cancelled + timed_out", {"source": "workflow outcomes", "workflow": wf}
    s = m.window_sum("dependency", w, now)
    return s.get("good", 0.0), s.get("total", 0.0), "good = healthy dependency probes; total = probes executed", {"source": "dependency probes (run on demand, not continuously)"}


def compute(cfg: OpsConfig, now: float | None = None) -> dict[str, Any]:
    slo = cfg.definition
    good, total, formula, src = _good_total(slo, now)
    observed_minutes = max(0.0, ((now if now is not None else time.time()) - m.STARTED_AT) / 60)
    eff_window = min(slo["window_minutes"], observed_minutes)
    base = {"slo_id": cfg.key, "config_version": cfg.version, "description": slo["description"], "sli": slo["sli"], "target": slo["target"], "window_minutes": slo["window_minutes"],
            "effective_window_minutes": round(eff_window, 1), "window_fully_observed": observed_minutes >= slo["window_minutes"], "exclusions": slo["exclusions"],
            "min_samples": slo["min_samples"], "calculation": formula, "source": src, "limitations": LIMITATIONS, "observed_since": m.started_at_iso()}
    if total <= 0:
        return {**base, "status": "no_data", "good": 0, "total": 0, "sli_value": None, "error_budget": None, "burn_rate": None, "note": "No qualifying events were recorded. This is missing data, not zero failures."}
    bad = total - good
    allowed = total * (1 - slo["target"])
    consumed = (bad / allowed) if allowed > 0 else (math.inf if bad else 0.0)
    sli_value = good / total
    if total < slo["min_samples"]:
        status = "insufficient_data"
    elif sli_value < slo["target"]:
        status = "exhausted"
    elif consumed >= 0.75:
        status = "at_risk"
    else:
        status = "ok"
    burn = (bad / total) / (1 - slo["target"])
    return {**base, "status": status, "good": good, "total": total, "bad": bad, "sli_value": sli_value, "error_budget": {"fraction": 1 - slo["target"], "allowed_bad_events": allowed, "consumed_fraction": consumed if math.isfinite(consumed) else None, "remaining_fraction": max(0.0, 1 - consumed) if math.isfinite(consumed) else 0.0},
            "burn_rate": burn, "note": "Fewer samples than min_samples; treat as indicative only." if status == "insufficient_data" else None}


def evaluate_all(db: Session, workspace_id: str, now: float | None = None) -> list[dict[str, Any]]:
    ensure_defaults(db, workspace_id)
    return [compute(c, now) for c in current_configs(db, workspace_id, "slo").values()]
