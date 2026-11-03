"""Deterministic reliability diagnostics. Every finding lists signals, window, evidence references, confidence, alternatives and investigation
steps. Findings describe correlation or temporal association; none claims a cause. Nothing is produced without recorded data."""
from __future__ import annotations

import time
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import OpsEvent
from app.observability import metrics as m

CHANGE_EVENTS = ("config_changed", "deployment_recorded")


def _aware_ts(dt: datetime) -> float:
    return (dt if dt.tzinfo else dt.replace(tzinfo=UTC)).timestamp()


def _finding(rule: str, title: str, severity: str, confidence: str, window: int, signals: list[dict[str, Any]], evidence: list[dict[str, Any]], note: str, alternatives: list[str], steps: list[str]) -> dict[str, Any]:
    return {"rule": rule, "title": title, "severity": severity, "confidence": confidence, "window_minutes": window, "signals": signals, "evidence_refs": evidence,
            "relationship": note, "causal_claim": False, "alternatives": alternatives, "investigation_steps": steps, "generated_at": datetime.now(UTC).isoformat()}


def run(db: Session, workspace_id: str, *, window: int = 60, now: float | None = None) -> dict[str, Any]:
    t = now if now is not None else time.time()
    out: list[dict[str, Any]] = []
    out += _repeated_workflow_failures(window, t)
    out += _repeated_failure_reasons(window, t)
    out += _job_timeouts(window, t)
    out += _dependency_correlation(window, t)
    out += _latency_after_change(db, workspace_id, window, t)
    return {"findings": out, "window_minutes": window, "evaluated_at": datetime.now(UTC).isoformat(), "observed_since": m.started_at_iso(),
            "note": "Rules are deterministic and use only in-memory telemetry since process start plus recorded operational events. An empty list means no rule matched, not that the system is healthy."}


def _repeated_workflow_failures(window: int, t: float) -> list[dict[str, Any]]:
    res = []
    for st in m.streams("workflow:"):
        s = m.window_sum(st, window, t)
        failed, total = s.get("failed", 0.0), sum(s.values())
        if failed >= 3 and failed / total >= 0.5:
            wf = st.split(":", 1)[1]
            res.append(_finding("repeated_workflow_failures", f"Repeated failures in workflow '{wf}'", "warning", "medium", window,
                                [{"name": "failed", "value": failed}, {"name": "executions", "value": total}],
                                [{"type": "metric_stream", "ref": st}, {"type": "metric", "ref": "nova_workflow_outcomes_total", "labels": {"workflow": wf}}],
                                "Failures are repeated within the window; this finding does not identify why.",
                                ["Failures share a single bad input (repository or configuration).", "A transient dependency problem.", "Genuine defects the workflow is correctly reporting."],
                                ["Open the workflow's recent runs and compare failure details.", "Search /ops/logs for event 'job_finished' with outcome 'failed'.", "Check whether failures cluster on one repository."]))
    return res


def _repeated_failure_reasons(window: int, t: float) -> list[dict[str, Any]]:
    res = []
    for st in m.streams("jobfail:"):
        kind = st.split(":", 1)[1]
        if kind not in ("security_scan", "cicd", "ci_finalize", "verification"):
            continue
        for reason, n in m.window_sum(st, window, t).items():
            if n >= 3:
                res.append(_finding("repeated_failure_reason", f"'{kind}' failed {int(n)} times with the same reason '{reason}'", "warning", "medium", window,
                                    [{"name": "occurrences", "value": n}, {"name": "reason", "value": reason}], [{"type": "metric_stream", "ref": st}],
                                    "The same recorded failure class repeats; the class is a label, not a root cause.",
                                    ["A single misconfiguration being retried.", "An upstream policy or gate that is correctly failing."],
                                    ["Inspect the latest failing run's details.", f"Search /ops/logs for job_kind={kind}."]))
    return res


def _job_timeouts(window: int, t: float) -> list[dict[str, Any]]:
    res = []
    for st in m.streams("jobfail:"):
        s = m.window_sum(st, window, t)
        n = sum(v for k, v in s.items() if "timeout" in k.lower() or "timedout" in k.lower())
        if n >= 2:
            res.append(_finding("job_timeouts", f"Job timeouts in '{st.split(':', 1)[1]}'", "warning", "medium", window, [{"name": "timeouts", "value": n}], [{"type": "metric_stream", "ref": st}],
                                "Timeouts are counted from recorded failure classes.", ["Inputs larger than the configured time limit.", "Resource contention on the host."],
                                ["Compare job duration to the configured time limit.", "Check process CPU/memory on the overview."]))
    return res


def _dependency_correlation(window: int, t: float) -> list[dict[str, Any]]:
    res = []
    http = m.window_sum("http", window, t)
    errors = http.get("error5xx", 0.0)
    if not errors:
        return res
    for st in m.streams("dep:"):
        dep = st.split(":", 1)[1]
        bad = m.window_sum(st, window, t).get("bad", 0.0)
        if bad:
            rule = "db_failures_with_api_errors" if dep == "database" else "dependency_correlated_failures"
            res.append(_finding(rule, f"API 5xx responses coincide with failed '{dep}' probes", "critical" if dep == "database" else "warning", "low", window,
                                [{"name": "api_5xx", "value": errors}, {"name": "failed_probes", "value": bad}, {"name": "dependency", "value": dep}],
                                [{"type": "metric_stream", "ref": "http"}, {"type": "metric_stream", "ref": st}],
                                "Co-occurrence in the same window only (probes run on demand, so timing is coarse). This is correlation, not causation.",
                                ["The 5xx errors have an unrelated application cause.", "The dependency failure was a result, not a cause, of load.", "Probe failures were transient."],
                                [f"Check /ops/health for '{dep}' state and error class.", "Search /ops/logs for event 'unhandled_error' in the window.", "Compare request ids of failing requests against probe times."]))
    return res


def _latency_after_change(db: Session, workspace_id: str, window: int, t: float) -> list[dict[str, Any]]:
    ev = db.scalars(select(OpsEvent).where(OpsEvent.workspace_id == workspace_id, OpsEvent.event_type.in_(CHANGE_EVENTS)).order_by(OpsEvent.occurred_at.desc()).limit(1)).first()
    if ev is None:
        return []
    ts = _aware_ts(ev.occurred_at)
    minutes = int((t - ts) // 60)
    if not 1 <= minutes <= window:
        return []
    after = m.quantile("http_latency", minutes, 0.95, t)
    before = m.quantile("http_latency", minutes, 0.95, ts)
    n_after = m.window_sum("http_latency", minutes, t).get("count", 0.0)
    n_before = m.window_sum("http_latency", minutes, ts).get("count", 0.0)
    if after is None or before is None or n_after < 10 or n_before < 10 or not after > before * 2:
        return []
    return [_finding("latency_after_change", f"p95 latency rose after recorded {ev.event_type.replace('_', ' ')}", "warning", "low", window,
                     [{"name": "p95_after_s", "value": after}, {"name": "p95_before_s", "value": before}, {"name": "samples_after", "value": n_after}, {"name": "samples_before", "value": n_before}],
                     [{"type": "ops_event", "ref": ev.id, "summary": ev.summary}, {"type": "metric_stream", "ref": "http_latency"}],
                     "Temporal association with a recorded change; it does not show the change caused the increase.",
                     ["Traffic mix or volume changed at the same time.", "A different workload (e.g. indexing) overlapped.", "Normal variance with small samples."],
                     ["Review the change event details.", "Compare slow routes in /ops/traces before and after the change.", "Repeat after more traffic to confirm persistence."])]
