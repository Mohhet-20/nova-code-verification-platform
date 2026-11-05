"""Observability, reliability and operational intelligence. Deterministic: no network, no real credentials, no sleeping."""
from __future__ import annotations

import hashlib
import hmac
import json
import logging
import time
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from typing import Any

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import func, select

from app.core.config import get_settings
from app.core.logging import LOG_RING, build_payload
from app.db.base import utcnow
from app.db.session import get_session_factory
from app.models import (
    AuditEvent,
    OpsConfig,
    OpsDelivery,
    OpsEvaluation,
    OpsEvent,
    OpsIncident,
    User,
    Workspace,
)
from app.observability import alerts, diagnostics, notify, retention, slo, tracing
from app.observability import context as ctxmod
from app.observability import incidents as inc_svc
from app.observability import metrics as m
from app.observability.errors import OpsConflict, OpsInvalid
from app.observability.health import dependency_health, reset_cache
from app.observability.jobs import track, tracked
from app.observability.redact import clean, clean_str
from app.security.auth import RequestContext, ensure_local_identity

SECRET = "ghp_" + "a1B2c3D4e5F6g7H8i9J0k1L2m3N4o5P6q7R8"


@pytest.fixture(autouse=True)
def clean_telemetry() -> Iterator[None]:
    def reset() -> None:
        m.REGISTRY.reset()
        m.reset_windows()
        tracing.SPANS.clear()
        tracing.configure(1.0, 2000, None)
        LOG_RING.clear()
        reset_cache()

    reset()
    yield
    reset()


@pytest.fixture
def db(migrated_url: str) -> Iterator[Any]:
    with get_session_factory()() as session:
        yield session


@pytest.fixture
def owner(db: Any) -> RequestContext:
    user, ws, mem = ensure_local_identity(db)
    return RequestContext(user=user, workspace=ws, role=mem.role)


def _record(msg: str = "hello", **extra: Any) -> logging.LogRecord:
    r = logging.LogRecord("nova.test", logging.INFO, __file__, 1, msg, (), None)
    r.__dict__.update(extra)
    return r


# ---------------------------------------------------------------- logging
def test_log_payload_has_structured_fields_and_correlation() -> None:
    t = ctxmod.REQUEST_ID.set("req_abcdef123456")
    try:
        p = build_payload(_record("request done", event="http_request", ms=12.5))
    finally:
        ctxmod.REQUEST_ID.reset(t)
    for key in ("ts", "level", "logger", "event", "message", "service", "environment", "request_id", "duration_ms"):
        assert key in p
    assert p["request_id"] == "req_abcdef123456" and p["ts"].endswith("+00:00")


def test_log_redacts_credentials_and_sensitive_keys_and_bounds_fields() -> None:
    p = build_payload(_record(f"token {SECRET} leaked", authorization=f"Bearer {SECRET}", cookie="sid=1", detail="x" * 5000, note=f"password=hunter2hunter2 {SECRET}"))
    blob = json.dumps(p)
    assert SECRET not in blob and "hunter2hunter2" not in blob
    assert p["authorization"] == "[REDACTED]" and p["cookie"] == "[REDACTED]"
    assert len(p["detail"]) <= 1001


def test_log_injection_is_neutralised() -> None:
    out = clean_str("line1\nFAKE {\"level\":\"INFO\"}\r\x1b[31m red")
    assert "\n" not in out and "\r" not in out and "\x1b" not in out


def test_stack_traces_stay_out_of_the_ring_but_error_class_is_kept() -> None:
    try:
        raise ValueError("boom " + SECRET)
    except ValueError:
        import sys

        rec = logging.LogRecord("nova.test", logging.ERROR, __file__, 1, "failed", (), sys.exc_info())
    assert "stack" in build_payload(rec) and SECRET not in build_payload(rec)["stack"]
    ring = build_payload(rec, stack=False)
    assert "stack" not in ring and ring["error_class"] == "ValueError"


def test_ring_is_bounded_and_logging_never_raises(client: TestClient) -> None:
    log = logging.getLogger("nova.ringtest")
    log.setLevel(logging.INFO)
    for i in range(LOG_RING.maxlen + 50):
        log.info("n%s", i)
    assert len(LOG_RING) <= LOG_RING.maxlen


# ---------------------------------------------------------------- correlation
def test_request_id_generated_echoed_and_malformed_discarded(client: TestClient) -> None:
    r = client.get("/api/v1/health")
    assert r.headers["x-request-id"].startswith("req_")
    assert client.get("/api/v1/health", headers={"X-Request-ID": "client-supplied-123"}).headers["x-request-id"] == "client-supplied-123"
    for bad in ("short", "has space and !!", "x" * 200, "id\twith-tab-12345"):
        got = client.get("/api/v1/health", headers={"X-Request-ID": bad}).headers["x-request-id"]
        assert got != bad and got.startswith("req_")


def test_traceparent_links_trace_but_is_not_identity(client: TestClient) -> None:
    tid = "4bf92f3577b34da6a3ce929d0e0e4736"
    r = client.get("/api/v1/health", headers={"traceparent": f"00-{tid}-00f067aa0ba902b7-01"})
    rid = r.headers["x-request-id"]
    span = tracing.recent(request_id=rid)[0]
    assert span["trace_id"] == tid and span["parent_id"] == "00f067aa0ba902b7"
    assert rid != tid
    assert ctxmod.parse_traceparent("00-" + "0" * 32 + "-00f067aa0ba902b7-01") is None
    assert ctxmod.parse_traceparent("garbage") is None


def test_http_metrics_use_route_templates_not_raw_ids(client: TestClient) -> None:
    client.get("/api/v1/repositories/does-not-exist-123456")
    client.get("/api/v1/repositories/another-missing-654321")
    text = m.REGISTRY.render()
    assert 'route="/api/v1/repositories/{repository_id}"' in text
    assert "does-not-exist" not in text and "another-missing" not in text
    client.get("/totally/unknown/path")
    assert 'route="unmatched"' in m.REGISTRY.render()


def test_unhandled_error_returns_request_id_and_no_internals(migrated_url: str) -> None:
    from app.main import create_app

    app = create_app()

    @app.get("/boom")
    def boom() -> None:
        raise RuntimeError("secret detail " + SECRET)

    with TestClient(app, raise_server_exceptions=False) as c:
        r = c.get("/boom")
    assert r.status_code == 500 and SECRET not in r.text and r.headers.get("x-request-id")
    assert m.HTTP_REQUESTS.value(method="GET", route="/boom", status_class="5xx") == 1
    assert all(SECRET not in json.dumps(rec) for rec in LOG_RING)


# ---------------------------------------------------------------- tracing
def test_spans_nest_and_record_errors_and_allowlist_attributes() -> None:
    with pytest.raises(KeyError):
        with tracing.span("outer", http_method="GET") as o:
            o.set("source_code", "def secret(): ...")  # not allow-listed
            o.set("error.class", f"x {SECRET}")
            with tracing.span("inner") as i:
                assert i.parent_id == o.span_id and i.trace_id == o.trace_id
            raise KeyError("x")
    spans = {s["name"]: s for s in tracing.SPANS}
    assert spans["outer"]["status"] == "error" and spans["inner"]["status"] == "ok"
    assert "source_code" not in spans["outer"]["attributes"]
    assert SECRET not in json.dumps(spans["outer"])


def test_sampling_zero_records_nothing_and_exporter_failure_is_safe() -> None:
    tracing.configure(0.0, 100, None)
    with tracing.span("quiet"):
        pass
    assert len(tracing.SPANS) == 0

    def bad_exporter(batch: list[dict[str, Any]]) -> None:
        raise ConnectionError("collector down")

    tracing.configure(1.0, 100, bad_exporter)
    with tracing.span("loud"):
        pass
    res = tracing.flush_exporter()
    assert res == {"enabled": True, "exported": 0, "failed": 1}
    assert m.TELEMETRY_DROPPED.value(signal="export") == 1
    assert tracing.flush_exporter()["failed"] == 0


def test_span_ring_is_bounded_and_counts_drops() -> None:
    tracing.configure(1.0, 100, None)
    for _ in range(120):
        with tracing.span("s"):
            pass
    assert len(tracing.SPANS) == 100 and m.TELEMETRY_DROPPED.value(signal="spans") >= 20


# ---------------------------------------------------------------- metrics
def test_label_cardinality_is_bounded_and_registration_is_idempotent() -> None:
    c = m.REGISTRY.counter("nova_test_total", "t", ("who",))
    assert m.REGISTRY.counter("nova_test_total", "t", ("who",)) is c
    with pytest.raises(ValueError):
        m.REGISTRY.gauge("nova_test_total", "t")
    for i in range(m.MAX_SERIES + 40):
        c.inc(who=f"u{i}")
    assert len(c.series) <= m.MAX_SERIES + 1 and c.dropped >= 39
    assert c.value(who="other") >= 40


def test_prometheus_render_and_no_series_without_real_operations() -> None:
    assert "nova_jobs_total{" not in m.REGISTRY.render()
    m.HTTP_LATENCY.observe(0.03, method="GET", route="/x")
    text = m.REGISTRY.render()
    assert "# TYPE nova_http_request_duration_seconds histogram" in text and 'le="+Inf"} 1' in text


def test_metrics_endpoint_is_authenticated_and_owner_only(client: TestClient, monkeypatch: pytest.MonkeyPatch) -> None:
    assert client.get("/api/v1/ops/metrics").status_code == 200
    monkeypatch.setenv("NOVA_API_TOKEN", "unit-test-token")
    from app.core.config import reset_settings_cache

    reset_settings_cache()
    assert client.get("/api/v1/ops/metrics").status_code == 401
    ok = client.get("/api/v1/ops/metrics", headers={"Authorization": "Bearer unit-test-token"})
    assert ok.status_code == 200 and "unit-test-token" not in ok.text and "nova_http_requests_total" in ok.text
    reset_settings_cache()


def test_non_owner_cannot_read_process_wide_telemetry(client: TestClient, db: Any) -> None:
    from app.main import create_app  # noqa: F401
    from app.security.auth import get_context

    user, ws, _ = ensure_local_identity(db)
    client.app.dependency_overrides[get_context] = lambda: RequestContext(user=user, workspace=ws, role="viewer")
    try:
        for p in ("/metrics", "/logs", "/traces"):
            assert client.get("/api/v1/ops" + p).status_code == 403
        assert client.get("/api/v1/ops/overview").status_code == 200
        assert client.post("/api/v1/ops/alerts/evaluate").status_code == 403
    finally:
        client.app.dependency_overrides.clear()


# ---------------------------------------------------------------- jobs
def test_job_tracking_records_outcomes_parent_and_queue_delay() -> None:
    t = ctxmod.REQUEST_ID.set("req_parent000001")
    try:
        with track("demo", "job-1", created_at=utcnow() - timedelta(seconds=5), workflow="indexing"):
            assert ctxmod.JOB_ID.get() == "job-1"
        with pytest.raises(RuntimeError):
            with track("demo", "job-2", workflow="indexing"):
                raise RuntimeError("bad")
    finally:
        ctxmod.REQUEST_ID.reset(t)
    assert m.JOBS.value(kind="demo", outcome="succeeded") == 1 and m.JOBS.value(kind="demo", outcome="failed") == 1
    assert m.WORKFLOW.value(workflow="indexing", outcome="failed") == 1
    assert m.window_sum("queue_all", 5)["count"] == 1  # only the job with a creation time
    spans = {s["attributes"]["job.id"]: s for s in tracing.SPANS if s["name"] == "job.demo"}
    assert spans["job-1"]["attributes"]["parent.request_id"] == "req_parent000001"
    assert spans["job-2"]["status"] == "error" and ctxmod.JOB_ID.get() is None
    assert m.window_sum("jobfail:demo", 5) == {"RuntimeError": 1.0}


def test_no_parent_is_invented_outside_a_request() -> None:
    with track("orphan", "j"):
        pass
    span = next(s for s in tracing.SPANS if s["name"] == "job.orphan")
    assert "parent.request_id" not in span["attributes"]


def test_observability_failure_does_not_mask_job_exception(monkeypatch: pytest.MonkeyPatch) -> None:
    def broken(*a: Any, **k: Any) -> None:
        raise OSError("metrics store down")

    monkeypatch.setattr(m.JOBS, "inc", broken)
    with pytest.raises(ValueError, match="original"):
        with track("masktest", "j"):
            raise ValueError("original")


def test_tracked_decorator_uses_recorded_status(migrated_url: str, db: Any) -> None:
    from app.models import SecScan  # any model with a status column

    calls: list[str] = []

    @tracked("sec_demo", "security_scan", model=SecScan, failed=("failed",))
    def job(scan_id: str) -> str:
        calls.append(scan_id)
        return "done"

    assert job("missing-id") == "done" and calls == ["missing-id"]
    assert m.JOBS.value(kind="sec_demo", outcome="succeeded") == 1


# ---------------------------------------------------------------- health
def test_liveness_needs_no_dependencies_and_readiness_shape_unchanged(client: TestClient) -> None:
    assert client.get("/api/v1/health").json()["status"] == "ok"
    body = client.get("/api/v1/ready").json()
    assert body["status"] == "ready" and set(body["checks"]) >= {"database", "migrations"}


def test_dependency_health_states_and_optional_is_degraded_not_unavailable(migrated_url: str) -> None:
    h = dependency_health(get_settings(), force=True)
    assert h["status"] == "ok"
    names = {d["name"]: d for d in h["dependencies"]}
    assert names["database"]["critical"] and names["notifications"]["state"] == "disabled" and names["notifications"]["probed"] is False
    assert all("latency_ms" in names[n] for n in ("database", "migrations", "storage", "workers"))


def test_critical_failure_is_unavailable_with_safe_error_class(migrated_url: str, monkeypatch: pytest.MonkeyPatch) -> None:
    from app.observability import health

    def down() -> dict[str, Any]:
        raise ConnectionError("password=hunter2hunter2 host=db.internal")

    monkeypatch.setattr(health, "_db", down)
    h = dependency_health(get_settings(), force=True)
    d = next(x for x in h["dependencies"] if x["name"] == "database")
    assert h["status"] == "unavailable" and d["state"] == "unavailable" and d["error_class"] == "ConnectionError"
    assert "hunter2" not in json.dumps(h) and "db.internal" not in json.dumps(h)
    assert m.window_sum("dep:database", 5)["bad"] == 1


def test_probe_timeout_and_cache(migrated_url: str, monkeypatch: pytest.MonkeyPatch) -> None:
    from app.observability import health

    monkeypatch.setenv("NOVA_OPS_PROBE_TIMEOUT_SECONDS", "0.05")
    from app.core.config import reset_settings_cache

    reset_settings_cache()
    monkeypatch.setattr(health, "_workers", lambda: (time.sleep(0.4), {"detail": "x"})[1])
    h = dependency_health(get_settings(), force=True)
    w = next(x for x in h["dependencies"] if x["name"] == "workers")
    assert w["state"] == "unavailable" and w["error_class"] == "timeout"
    monkeypatch.undo()
    reset_settings_cache()
    first = dependency_health(get_settings(), force=True)
    assert dependency_health(get_settings())["cached"] is True and first["cached"] is False


# ---------------------------------------------------------------- incidents
def _raise(db: Any, ws: str, fp: str = "fp1", **kw: Any) -> tuple[OpsIncident, str]:
    return inc_svc.raise_incident(db, ws, fp=fp, source="alert", severity=kw.get("severity", "warning"), component="api", title="T", summary="S", evidence=kw.get("evidence", {}), rule_id="r")


def test_incident_dedupe_lifecycle_and_history(db: Any, owner: RequestContext) -> None:
    ws = owner.workspace.id
    a, t1 = _raise(db, ws)
    b, t2 = _raise(db, ws)
    assert a.id == b.id and (t1, t2) == ("opened", "recurred") and b.occurrences == 2
    db.commit()
    with pytest.raises(OpsConflict) as e:
        inc_svc.transition(db, owner, a.id, "reopen")
    assert e.value.code == "invalid_transition"
    inc_svc.transition(db, owner, a.id, "acknowledge")
    inc_svc.transition(db, owner, a.id, "investigate")
    with pytest.raises(OpsConflict) as e2:
        inc_svc.transition(db, owner, a.id, "resolve", " ")
    assert e2.value.code == "note_required"
    inc_svc.transition(db, owner, a.id, "resolve", "fixed the pool")
    c, t3 = _raise(db, ws)
    db.commit()
    assert t3 == "reopened" and c.status == "reopened" and c.resolved_at is None
    types = [e.event_type for e in db.scalars(select(OpsEvent).where(OpsEvent.incident_id == a.id).order_by(OpsEvent.occurred_at, OpsEvent.id))]
    assert {"incident_opened", "incident_acknowledged", "incident_investigating", "incident_resolved", "incident_reopened"} <= set(types)
    assert db.scalar(select(func.count()).select_from(AuditEvent).where(AuditEvent.action.like("ops.incident.%"))) == 3


def test_incident_tenant_isolation_and_viewer_cannot_write(db: Any, owner: RequestContext) -> None:
    inc, _ = _raise(db, owner.workspace.id)
    db.commit()
    other_ws = Workspace(name="Other", slug="other")
    other_user = User(handle="other-user", display_name="Other")
    db.add_all([other_ws, other_user])
    db.flush()
    other = RequestContext(user=other_user, workspace=other_ws, role="owner")
    from app.core.errors import NotFoundError

    with pytest.raises(NotFoundError):
        inc_svc.get_incident(db, other, inc.id)
    viewer = RequestContext(user=owner.user, workspace=owner.workspace, role="viewer")
    from app.core.errors import ForbiddenError

    with pytest.raises(ForbiddenError):
        inc_svc.transition(db, viewer, inc.id, "acknowledge")


def test_incident_evidence_is_scrubbed(db: Any, owner: RequestContext) -> None:
    inc, _ = _raise(db, owner.workspace.id, evidence={"token": SECRET, "note": f"x {SECRET}", "nested": {"api_key": "abc"}})
    db.commit()
    assert SECRET not in json.dumps(inc.evidence) and inc.evidence["token"] == "[REDACTED]"


# ---------------------------------------------------------------- alerts
def _feed_http(total: int, errors: int, ts: float | None = None) -> None:
    m.window_add("http", "total", total, ts)
    m.window_add("http", "error5xx", errors, ts)
    m.window_add("http", "ok", total - errors, ts)


def test_rules_without_data_report_no_data_not_ok(db: Any, owner: RequestContext) -> None:
    res = {r["rule_id"]: r for r in alerts.evaluate_all(db, owner.workspace.id, get_settings())}
    assert res["http_error_rate"]["status"] == "no_data" and res["http_error_rate"]["missing_data"] is True
    assert "min_samples" in res["http_error_rate"] and "process start" in res["http_error_rate"]["explanation"]
    assert res["db_connectivity"]["status"] == "ok"  # measured on a fresh probe
    assert db.scalar(select(func.count()).select_from(OpsIncident)) == 0


def test_error_rate_fires_explains_and_dedupes(db: Any, owner: RequestContext) -> None:
    _feed_http(100, 30)
    sent: list[str] = []
    res = {r["rule_id"]: r for r in alerts.evaluate_all(db, owner.workspace.id, get_settings(), notify=lambda d, s, i: sent.append(i.id))}["http_error_rate"]
    assert res["status"] == "firing" and res["observed"] == pytest.approx(0.3) and res["threshold"] == 0.05
    assert res["rule_version"] == 1 and res["window_minutes"] == 15 and res["samples"] == 100
    assert res["incident"]["transition"] == "opened" and len(sent) == 1
    res2 = {r["rule_id"]: r for r in alerts.evaluate_all(db, owner.workspace.id, get_settings(), notify=lambda d, s, i: sent.append(i.id))}["http_error_rate"]
    assert res2["incident"]["transition"] == "recurred" and res2["incident"]["notification_suppressed_by_cooldown"] is True
    assert len(sent) == 1  # cooldown
    assert db.scalar(select(func.count()).select_from(OpsIncident)) == 1
    assert db.scalar(select(func.count()).select_from(OpsEvaluation).where(OpsEvaluation.rule_id == "http_error_rate")) == 2


def test_min_samples_prevents_false_alarm_and_clearing_auto_resolves(db: Any, owner: RequestContext) -> None:
    _feed_http(5, 5)
    r = next(x for x in alerts.evaluate_all(db, owner.workspace.id, get_settings()) if x["rule_id"] == "http_error_rate")
    assert r["status"] == "no_data" and r["missing_data"]
    _feed_http(100, 40)
    alerts.evaluate_all(db, owner.workspace.id, get_settings())
    m.reset_windows()
    _feed_http(500, 0)
    r = next(x for x in alerts.evaluate_all(db, owner.workspace.id, get_settings()) if x["rule_id"] == "http_error_rate")
    assert r["status"] == "ok" and r["incident"]["transition"] == "auto_resolved"
    inc = db.scalar(select(OpsIncident))
    assert inc.status == "resolved" and "cleared" in inc.resolution_note


def test_acknowledged_incident_is_not_auto_resolved(db: Any, owner: RequestContext) -> None:
    _feed_http(100, 50)
    alerts.evaluate_all(db, owner.workspace.id, get_settings())
    inc = db.scalar(select(OpsIncident))
    inc_svc.transition(db, owner, inc.id, "acknowledge")
    m.reset_windows()
    _feed_http(500, 0)
    alerts.evaluate_all(db, owner.workspace.id, get_settings())
    db.refresh(inc)
    assert inc.status == "acknowledged"


def test_job_failure_and_latency_and_workflow_rules(db: Any, owner: RequestContext) -> None:
    m.window_add("jobs", "total", 10)
    m.window_add("jobs", "bad", 6)
    for _ in range(30):
        m.record_latency("http_latency", 4.0)
    m.window_add("workflow:cicd", "failed", 3)
    m.window_add("workflow:security_scan", "failed", 3)
    for _ in range(4):
        m.record_latency("queue_all", 120.0)
    res = {r["rule_id"]: r["status"] for r in alerts.evaluate_all(db, owner.workspace.id, get_settings())}
    assert res["job_failure_rate"] == res["http_latency_p95"] == res["ci_failures"] == res["security_scan_failures"] == res["queue_delay_p95"] == "firing"


def test_db_connectivity_rule_fires_when_database_is_down(db: Any, owner: RequestContext, monkeypatch: pytest.MonkeyPatch) -> None:
    from app.observability import health

    monkeypatch.setattr(health, "_db", lambda: (_ for _ in ()).throw(ConnectionError("down")))
    res = {r["rule_id"]: r for r in alerts.evaluate_all(db, owner.workspace.id, get_settings())}
    assert res["db_connectivity"]["status"] == "firing" and res["db_connectivity"]["incident"]["transition"] == "opened"
    assert res["db_connectivity"]["measurements"]["error_class"] == "ConnectionError"


def test_persistent_readiness_needs_consecutive_evaluations(db: Any, owner: RequestContext, monkeypatch: pytest.MonkeyPatch) -> None:
    from app.observability import health

    monkeypatch.setattr(health, "_storage", lambda s: (_ for _ in ()).throw(PermissionError("ro")))
    states = []
    for _ in range(3):
        states.append(next(r for r in alerts.evaluate_all(db, owner.workspace.id, get_settings()) if r["rule_id"] == "persistent_readiness_degradation")["status"])
    assert states == ["ok", "ok", "firing"]


def test_rule_versioning_validation_and_disable(db: Any, owner: RequestContext) -> None:
    v2 = alerts.put_rule(db, owner, "http_error_rate", {"threshold": 0.5}, None)
    assert v2.version == 2 and v2.definition["threshold"] == 0.5
    versions = db.scalars(select(OpsConfig).where(OpsConfig.key == "http_error_rate").order_by(OpsConfig.version)).all()
    assert [v.version for v in versions] == [1, 2] and versions[0].definition["threshold"] == 0.05  # history preserved
    _feed_http(100, 30)
    assert next(r for r in alerts.evaluate_all(db, owner.workspace.id, get_settings()) if r["rule_id"] == "http_error_rate")["status"] == "ok"
    alerts.put_rule(db, owner, "http_error_rate", {}, False)
    assert next(r for r in alerts.evaluate_all(db, owner.workspace.id, get_settings()) if r["rule_id"] == "http_error_rate")["status"] == "disabled"
    with pytest.raises(OpsInvalid) as e:
        alerts.put_rule(db, owner, "http_error_rate", {"metric": "made_up"}, None)
    assert e.value.code == "unknown_metric"
    with pytest.raises(OpsInvalid):
        alerts.put_rule(db, owner, "http_error_rate", {"comparator": "~"}, None)
    assert db.scalar(select(func.count()).select_from(OpsEvent).where(OpsEvent.event_type == "config_changed")) == 2


def test_notification_failure_does_not_break_evaluation(db: Any, owner: RequestContext) -> None:
    _feed_http(100, 50)

    def explode(*a: Any, **k: Any) -> None:
        raise OSError("smtp down")

    res = alerts.evaluate_all(db, owner.workspace.id, get_settings(), notify=explode)
    assert next(r for r in res if r["rule_id"] == "http_error_rate")["status"] == "firing"


# ---------------------------------------------------------------- notifier
class FakeTransport:
    def __init__(self, codes: list[int] | None = None, exc: Exception | None = None) -> None:
        self.codes, self.exc, self.calls = codes or [200], exc, []

    def post(self, url, host, port, ip, path, body, headers, timeout):  # type: ignore[no-untyped-def]
        self.calls.append({"url": url, "ip": ip, "headers": headers, "body": body, "timeout": timeout})
        if self.exc:
            raise self.exc
        return self.codes[min(len(self.calls) - 1, len(self.codes) - 1)]


def _settings(monkeypatch: pytest.MonkeyPatch, **env: str) -> Any:
    for k, v in env.items():
        monkeypatch.setenv(k, v)
    from app.core.config import reset_settings_cache

    reset_settings_cache()
    return get_settings()


def _incident(db: Any, owner: RequestContext) -> OpsIncident:
    inc, _ = _raise(db, owner.workspace.id)
    db.commit()
    return inc


PUBLIC = lambda host: ["93.184.216.34"]  # noqa: E731


@pytest.mark.parametrize("url,why", [
    ("https://127.0.0.1/hook", "blocked_address"), ("https://10.1.2.3/hook", "blocked_address"), ("https://192.168.0.5/x", "blocked_address"),
    ("https://169.254.169.254/latest/meta-data", "blocked_address"), ("https://[::1]/x", "blocked_address"), ("https://[::ffff:127.0.0.1]/x", "blocked_address"),
    ("https://metadata.google.internal/x", "metadata_host"), ("http://example.com/x", "https_required"), ("https://user:pw@example.com/x", "credentials_in_url"),
    ("ftp://example.com/x", "invalid_url"),
])
def test_ssrf_guard_blocks_dangerous_destinations(url: str, why: str) -> None:
    with pytest.raises(notify.Blocked) as e:
        notify.validate_destination(url, False, PUBLIC)
    assert str(e.value) == why


def test_ssrf_guard_blocks_dns_to_private_and_allows_when_configured() -> None:
    with pytest.raises(notify.Blocked):
        notify.validate_destination("https://innocent.example.com/x", False, lambda h: ["10.0.0.7"])
    with pytest.raises(notify.Blocked):
        notify.validate_destination("https://rebind.example.com/x", False, lambda h: ["93.184.216.34", "127.0.0.1"])
    assert notify.validate_destination("https://127.0.0.1:8443/x", True)[2] == "127.0.0.1"
    host, port, ip, path = notify.validate_destination("https://hooks.example.com/a/b?x=1", False, PUBLIC)
    assert (host, port, ip, path) == ("hooks.example.com", 443, "93.184.216.34", "/a/b?x=1")


def test_unconfigured_notifications_are_reported_disabled(db: Any, owner: RequestContext, migrated_url: str) -> None:
    st = notify.status(get_settings())
    assert st["enabled"] is False and next(c for c in st["channels"] if c["channel"] == "email")["implemented"] is False
    assert notify.dispatch(db, get_settings(), _incident(db, owner)) == []


def test_webhook_is_signed_minimal_and_recorded_without_secrets(db: Any, owner: RequestContext, monkeypatch: pytest.MonkeyPatch) -> None:
    s = _settings(monkeypatch, NOVA_OPS_WEBHOOK_URL="https://hooks.example.com/nova/TOKEN123", NOVA_OPS_WEBHOOK_SECRET_ENV="UNIT_HOOK_SECRET", UNIT_HOOK_SECRET="whsec_unit_only")
    tp = FakeTransport([200])
    inc = _incident(db, owner)
    out = notify.dispatch(db, s, inc, transport=tp, resolver=PUBLIC)
    assert out[0]["status"] == "delivered" and len(tp.calls) == 1
    call = tp.calls[0]
    assert call["ip"] == "93.184.216.34" and call["timeout"] == s.ops_notify_timeout_seconds
    ts = call["headers"]["X-Nova-Timestamp"]
    expect = "sha256=" + hmac.new(b"whsec_unit_only", ts.encode() + b"." + call["body"], hashlib.sha256).hexdigest()
    assert call["headers"]["X-Nova-Signature"] == expect
    body = json.loads(call["body"])
    assert set(body) == {"incident_id", "rule_id", "title", "severity", "component", "status", "summary", "occurrences", "first_seen_at", "last_seen_at"}
    rec = db.scalars(select(OpsDelivery)).one()
    assert rec.destination == "hooks.example.com" and "TOKEN123" not in json.dumps([rec.destination, rec.idempotency_key])
    assert "whsec_unit_only" not in call["body"].decode()
    # idempotent: the same notification is not delivered twice
    again = notify.dispatch(db, s, inc, transport=tp, resolver=PUBLIC)
    assert again[0]["status"] == "skipped" and len(tp.calls) == 1


def test_retries_with_backoff_then_gives_up_and_never_raises(db: Any, owner: RequestContext, monkeypatch: pytest.MonkeyPatch) -> None:
    s = _settings(monkeypatch, NOVA_OPS_WEBHOOK_URL="https://hooks.example.com/x", NOVA_OPS_NOTIFY_MAX_ATTEMPTS="3")
    sleeps: list[float] = []
    tp = FakeTransport([500, 503, 200])
    out = notify.dispatch(db, s, _incident(db, owner), transport=tp, resolver=PUBLIC, sleep=sleeps.append)
    assert out[0]["status"] == "delivered" and out[0]["attempts"] == 3 and sleeps == [0.25, 0.5]
    assert [d.status for d in db.scalars(select(OpsDelivery).order_by(OpsDelivery.attempt))] == ["failed", "failed", "delivered"]
    db.query(OpsDelivery).delete()
    tp2 = FakeTransport(exc=TimeoutError("slow"))
    inc2, _ = _raise(db, owner.workspace.id, fp="other")
    out2 = notify.dispatch(db, s, inc2, transport=tp2, resolver=PUBLIC, sleep=lambda x: None)
    assert out2[0]["status"] == "failed" and out2[0]["attempts"] == 3 and out2[0]["error_class"] == "TimeoutError"


def test_client_errors_are_not_retried_and_blocked_is_recorded(db: Any, owner: RequestContext, monkeypatch: pytest.MonkeyPatch) -> None:
    s = _settings(monkeypatch, NOVA_OPS_WEBHOOK_URL="https://hooks.example.com/x")
    tp = FakeTransport([400])
    out = notify.dispatch(db, s, _incident(db, owner), transport=tp, resolver=PUBLIC, sleep=lambda x: None)
    assert out[0]["status"] == "failed" and len(tp.calls) == 1
    s2 = _settings(monkeypatch, NOVA_OPS_WEBHOOK_URL="https://169.254.169.254/x")
    inc2, _ = _raise(db, owner.workspace.id, fp="b")
    tp2 = FakeTransport()
    out2 = notify.dispatch(db, s2, inc2, transport=tp2, sleep=lambda x: None)
    assert out2[0]["status"] == "blocked" and tp2.calls == [] and out2[0]["attempts"] == 1


def test_slack_payload_and_secret_from_env_only(db: Any, owner: RequestContext, monkeypatch: pytest.MonkeyPatch) -> None:
    s = _settings(monkeypatch, NOVA_OPS_SLACK_WEBHOOK_ENV="UNIT_SLACK", UNIT_SLACK="https://hooks.slack.example.com/services/T/B/X")
    tp = FakeTransport()
    out = notify.dispatch(db, s, _incident(db, owner), transport=tp, resolver=PUBLIC)
    assert out[0]["channel"] == "slack" and set(json.loads(tp.calls[0]["body"])) == {"text"}
    assert "services/T/B/X" not in json.dumps(db.scalars(select(OpsDelivery.destination)).all())


# ---------------------------------------------------------------- SLOs
def _slo(db: Any, ws: str, key: str) -> dict[str, Any]:
    return next(s for s in slo.evaluate_all(db, ws) if s["slo_id"] == key)


def test_slo_no_data_is_not_zero_failures(db: Any, owner: RequestContext) -> None:
    s = _slo(db, owner.workspace.id, "api_availability")
    assert s["status"] == "no_data" and s["sli_value"] is None and "missing data" in s["note"]
    assert s["limitations"] and s["observed_since"] and s["config_version"] == 1 and s["exclusions"] == ["health_checks"]


def test_slo_calculation_budget_and_statuses(db: Any, owner: RequestContext) -> None:
    ws = owner.workspace.id
    _feed_http(10, 0)
    assert _slo(db, ws, "api_availability")["status"] == "insufficient_data"
    m.reset_windows()
    _feed_http(1000, 2)
    s = _slo(db, ws, "api_availability")
    assert s["status"] == "ok" and s["good"] == 998 and s["total"] == 1000 and s["sli_value"] == pytest.approx(0.998)
    assert s["error_budget"]["allowed_bad_events"] == pytest.approx(5.0) and s["error_budget"]["consumed_fraction"] == pytest.approx(0.4)
    assert s["burn_rate"] == pytest.approx(0.4) and "good =" in s["calculation"]
    m.reset_windows()
    _feed_http(1000, 4)
    assert _slo(db, ws, "api_availability")["status"] == "at_risk"
    m.reset_windows()
    _feed_http(1000, 20)
    s = _slo(db, ws, "api_availability")
    assert s["status"] == "exhausted" and s["error_budget"]["remaining_fraction"] == 0.0


def test_slo_latency_and_job_and_workflow_and_dependency(db: Any, owner: RequestContext) -> None:
    ws = owner.workspace.id
    for _ in range(95):
        m.record_latency("http_latency", 0.05)
    for _ in range(5):
        m.record_latency("http_latency", 3.0)
    lat = _slo(db, ws, "api_latency")
    assert lat["total"] == 100 and lat["good"] == 95 and lat["source"]["effective_bucket_seconds"] == 1.0
    m.window_add("jobs", "total", 20)
    m.window_add("jobs", "good", 20)
    assert _slo(db, ws, "job_completion")["status"] == "ok"
    m.window_add("workflow:cicd", "succeeded", 9)
    m.window_add("workflow:cicd", "failed", 1)
    wf = _slo(db, ws, "workflow_success")
    assert wf["total"] == 10 and wf["good"] == 9
    assert _slo(db, ws, "dependency_availability")["status"] == "no_data"


def test_slo_config_versions_are_preserved_and_validated(db: Any, owner: RequestContext) -> None:
    slo.ensure_defaults(db, owner.workspace.id)
    v2 = slo.put_slo(db, owner, "api_availability", {"target": 0.999})
    assert v2.version == 2
    hist = slo.versions(db, owner.workspace.id, "api_availability")
    assert [h.version for h in hist] == [2, 1] and hist[1].definition["target"] == 0.995
    assert _slo(db, owner.workspace.id, "api_availability")["config_version"] == 2
    for bad in ({"target": 1.5}, {"exclusions": ["everything"]}, {"window_minutes": 99999}):
        with pytest.raises(OpsInvalid):
            slo.put_slo(db, owner, "api_availability", bad)
    with pytest.raises(OpsInvalid):
        slo.validate_slo({"id": "x", "sli": "latency", "target": 0.9, "window_minutes": 60})


def test_health_checks_are_excluded_only_by_explicit_rule(client: TestClient) -> None:
    for _ in range(3):
        client.get("/api/v1/health")
        client.get("/api/v1/ready")
    assert m.window_sum("http", 5).get("total", 0) == 0  # excluded paths
    client.get("/api/v1/workspace")
    assert m.window_sum("http", 5).get("total", 0) >= 1
    assert m.HTTP_REQUESTS.value(method="GET", route="/api/v1/health", status_class="2xx") == 3  # still visible in raw metrics


# ---------------------------------------------------------------- diagnostics
def test_diagnostics_empty_without_signals(db: Any, owner: RequestContext) -> None:
    out = diagnostics.run(db, owner.workspace.id)
    assert out["findings"] == [] and "not that the system is healthy" in out["note"]


def test_diagnostics_repeated_failures_with_evidence_and_no_causal_claim(db: Any, owner: RequestContext) -> None:
    m.window_add("workflow:cicd", "failed", 4)
    m.window_add("workflow:cicd", "succeeded", 1)
    m.window_add("jobfail:cicd", "gate_error", 3)
    m.window_add("jobfail:cicd", "TimeoutError", 2)
    out = {f["rule"]: f for f in diagnostics.run(db, owner.workspace.id)["findings"]}
    assert {"repeated_workflow_failures", "repeated_failure_reason", "job_timeouts"} <= set(out)
    for f in out.values():
        assert f["signals"] and f["evidence_refs"] and f["alternatives"] and f["investigation_steps"] and f["confidence"] in ("low", "medium", "high")
        assert f["causal_claim"] is False and f["window_minutes"] == 60


def test_diagnostics_db_failures_correlate_with_api_errors_as_correlation(db: Any, owner: RequestContext) -> None:
    _feed_http(50, 5)
    assert diagnostics.run(db, owner.workspace.id)["findings"] == []  # errors alone: no dependency evidence, nothing invented
    m.window_add("dep:database", "bad", 2)
    f = next(x for x in diagnostics.run(db, owner.workspace.id)["findings"] if x["rule"] == "db_failures_with_api_errors")
    assert f["confidence"] == "low" and "not causation" in f["relationship"]


def test_diagnostics_latency_after_recorded_change_is_temporal_only(db: Any, owner: RequestContext) -> None:
    now = (int(time.time() // 60)) * 60 + 30.0  # pinned mid-minute so minute buckets never straddle the change
    change = now - 20 * 60
    ev = inc_svc.record_event(db, owner.workspace.id, "deployment_recorded", summary="deploy v2", occurred_at=datetime.fromtimestamp(change, UTC))
    db.commit()
    for i in range(30):
        m.record_latency("http_latency", 0.05, change - 60 * (1 + i % 14))
        m.record_latency("http_latency", 3.0, change + 60 * (1 + i % 14))
    f = next(x for x in diagnostics.run(db, owner.workspace.id, now=now)["findings"] if x["rule"] == "latency_after_change")
    assert {"type": "ops_event", "ref": ev.id, "summary": "deploy v2"} in f["evidence_refs"]
    assert "does not show" in f["relationship"] and f["confidence"] == "low"


def test_diagnostics_latency_rule_needs_enough_samples(db: Any, owner: RequestContext) -> None:
    inc_svc.record_event(db, owner.workspace.id, "config_changed", summary="x", occurred_at=utcnow() - timedelta(minutes=10))
    db.commit()
    m.record_latency("http_latency", 3.0)
    assert diagnostics.run(db, owner.workspace.id)["findings"] == []


# ---------------------------------------------------------------- retention & periodic
def test_retention_deletes_old_ops_records_never_audit_or_open_incident_history(db: Any, owner: RequestContext) -> None:
    ws = owner.workspace.id
    old = utcnow() - timedelta(days=400)
    open_inc, _ = _raise(db, ws)
    db.flush()
    db.add_all([OpsEvent(workspace_id=ws, event_type="old", occurred_at=old), OpsEvent(workspace_id=ws, incident_id=open_inc.id, event_type="old_open", occurred_at=old),
                OpsEvaluation(workspace_id=ws, rule_id="r", rule_version=1, status="ok", created_at=old), OpsDelivery(workspace_id=ws, channel="webhook", destination="h", idempotency_key="k", status="failed", created_at=old),
                AuditEvent(workspace_id=ws, action="x", entity_type="t", entity_id="e", created_at=old)])
    db.commit()
    res = retention.run(db, ws, 90)
    db.commit()
    assert res["deleted"] == {"events": 1, "evaluations": 1, "deliveries": 1}
    assert db.scalar(select(func.count()).select_from(AuditEvent).where(AuditEvent.action == "x")) == 1
    assert db.scalar(select(func.count()).select_from(OpsEvent).where(OpsEvent.event_type == "old_open")) == 1


def test_periodic_evaluator_is_off_by_default_and_runs_once_on_demand(migrated_url: str, db: Any) -> None:
    from app.observability import periodic

    assert get_settings().ops_periodic_evaluation is False
    ensure_local_identity(db)
    assert periodic.evaluate_once(get_settings()) >= 1


# ---------------------------------------------------------------- API
def test_ops_api_endpoints_and_validation(client: TestClient) -> None:
    ov = client.get("/api/v1/ops/overview").json()
    assert ov["health"]["status"] == "ok" and ov["periodic_evaluation"]["enabled"] is False and ov["limitations"]
    assert {s["signal"] for s in client.get("/api/v1/ops/signals").json()["signals"]} >= {"metrics", "traces", "alerts"}
    ev = client.post("/api/v1/ops/alerts/evaluate").json()
    assert ev["continuous_monitoring"] is False and len(ev["evaluations"]) == 8
    rules = client.get("/api/v1/ops/alerts/rules").json()["items"]
    assert len(rules) == 8 and all(r["version"] >= 1 for r in rules)
    r = client.put("/api/v1/ops/alerts/rules/http_error_rate", json={"threshold": 0.2})
    assert r.status_code == 200 and r.json()["version"] == 2
    assert client.put("/api/v1/ops/alerts/rules/http_error_rate", json={"metric": "x"}).status_code == 422
    assert client.put("/api/v1/ops/alerts/rules/nope", json={"threshold": 1}).status_code == 404
    assert len(client.get("/api/v1/ops/alerts/rules/http_error_rate/versions").json()["items"]) == 2
    assert client.get("/api/v1/ops/slos").json()["items"][0]["limitations"]
    assert client.put("/api/v1/ops/slos/api_availability", json={"target": 0.99}).json()["version"] == 2
    assert client.get("/api/v1/ops/diagnostics?window_minutes=2").status_code == 422
    assert client.get("/api/v1/ops/notifications").json()["enabled"] is False
    d = client.post("/api/v1/ops/events/deployment", json={"summary": "deploy 1", "version": "1.0"})
    assert d.status_code == 200 and d.json()["event_type"] == "deployment_recorded"
    assert client.get("/api/v1/ops/events?event_type=deployment_recorded").json()["total"] == 1


def test_incident_api_flow_and_pagination(client: TestClient, db: Any, owner: RequestContext) -> None:
    for i in range(3):
        _raise(db, owner.workspace.id, fp=f"fp{i}")
    db.commit()
    page = client.get("/api/v1/ops/incidents?page_size=2").json()
    assert page["total"] == 3 and len(page["items"]) == 2 and page["pages"] == 2
    iid = page["items"][0]["id"]
    assert client.post(f"/api/v1/ops/incidents/{iid}/acknowledge", json={}).json()["status"] == "acknowledged"
    assert client.post(f"/api/v1/ops/incidents/{iid}/resolve", json={}).status_code == 409
    assert client.post(f"/api/v1/ops/incidents/{iid}/explode", json={}).status_code == 404
    done = client.post(f"/api/v1/ops/incidents/{iid}/resolve", json={"note": "handled"}).json()
    assert done["status"] == "resolved"
    detail = client.get(f"/api/v1/ops/incidents/{iid}").json()
    assert [h["event_type"] for h in detail["history"]][-2:] == ["incident_acknowledged", "incident_resolved"]
    assert client.get("/api/v1/ops/incidents/missing").status_code == 404
    assert client.get("/api/v1/ops/incidents?status=resolved").json()["total"] == 1


def test_logs_and_traces_endpoints_serve_redacted_data(client: TestClient) -> None:
    logging.getLogger("nova.test").warning("probe %s", f"password=hunter2hunter2 {SECRET}")
    client.get("/api/v1/health", headers={"X-Request-ID": "trace-me-0001"})
    logs = client.get("/api/v1/ops/logs?limit=500").json()
    assert SECRET not in json.dumps(logs) and "hunter2" not in json.dumps(logs)
    assert all("stack" not in r for r in logs["items"])
    by_req = client.get("/api/v1/ops/logs?request_id=trace-me-0001").json()["items"]
    assert by_req and all(r["request_id"] == "trace-me-0001" for r in by_req)
    spans = client.get("/api/v1/ops/traces?request_id=trace-me-0001").json()["items"]
    assert spans and spans[0]["attributes"]["http.route"] == "/api/v1/health"
    assert client.get("/api/v1/ops/logs?limit=9999").status_code == 422


def test_retention_endpoint_requires_owner_and_audits(client: TestClient, db: Any) -> None:
    r = client.post("/api/v1/ops/retention/run")
    assert r.status_code == 200 and "audit_events" in r.json()["untouched"]
    assert db.scalar(select(func.count()).select_from(AuditEvent).where(AuditEvent.action == "ops.retention.run")) == 1


# ---------------------------------------------------------------- readiness integration
def test_operational_health_readiness_rule(db: Any, owner: RequestContext) -> None:
    from app.services.cicd.readiness import _ops_rule

    repo = SimpleNamespace(workspace_id=owner.workspace.id)
    out: list[dict[str, Any]] = []
    _ops_rule(db, repo, {"require_operational_health": False}, out)
    assert out == []
    _ops_rule(db, repo, {"require_operational_health": True}, out)
    assert out[0]["status"] == "pass" and "not proof" in out[0]["detail"]
    inc, _ = _raise(db, owner.workspace.id, fp="c", severity="critical")
    db.commit()
    out2: list[dict[str, Any]] = []
    _ops_rule(db, repo, {"require_operational_health": True}, out2)
    assert out2[0]["status"] == "fail" and out2[0]["evidence"]["incident_ids"] == [inc.id]
    inc_svc.transition(db, owner, inc.id, "resolve", "done")
    out3: list[dict[str, Any]] = []
    _ops_rule(db, repo, {"require_operational_health": True}, out3)
    assert out3[0]["status"] == "pass"


def test_clean_helper_bounds_depth_and_redacts_keys() -> None:
    deep: dict[str, Any] = {"a": {"b": {"c": {"d": {"e": "x" * 10}}}}, "password": "p", "list": list(range(100))}
    out = clean(deep)
    assert out["password"] == "[REDACTED]" and len(out["list"]) == 50
