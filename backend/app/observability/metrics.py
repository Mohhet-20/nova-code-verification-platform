"""In-process metrics with bounded label cardinality, per-minute windows (for SLOs/alerts) and Prometheus text rendering.

Only real executed operations are recorded. State is in memory and is lost on restart; `STARTED_AT` is exposed so consumers can say how much history exists.
"""
from __future__ import annotations

import math
import threading
import time
from collections import defaultdict
from datetime import UTC, datetime

from app.observability.redact import clip

STARTED_AT = time.time()
MAX_SERIES = 300  # per metric; beyond this new label sets collapse into an "other" series
WINDOW_MINUTES = 24 * 60
LATENCY_BUCKETS = (0.005, 0.01, 0.025, 0.05, 0.1, 0.25, 0.5, 1.0, 2.5, 5.0, 10.0, 30.0, 120.0)
_LOCK = threading.RLock()


def _key(labels: dict[str, str]) -> tuple[tuple[str, str], ...]:
    return tuple(sorted((k, clip(str(v), 80)) for k, v in labels.items()))


class _Metric:
    kind = "untyped"

    def __init__(self, name: str, help_: str, label_names: tuple[str, ...] = ()) -> None:
        self.name, self.help, self.label_names = name, help_, label_names
        self.dropped = 0

    def _norm(self, labels: dict[str, str], series: dict) -> tuple[tuple[str, str], ...]:
        key = _key({k: labels.get(k, "") for k in self.label_names})
        if key not in series and len(series) >= MAX_SERIES:
            self.dropped += 1
            key = _key({k: "other" for k in self.label_names})
        return key


class Counter(_Metric):
    kind = "counter"

    def __init__(self, *a, **k) -> None:
        super().__init__(*a, **k)
        self.series: dict = defaultdict(float)

    def inc(self, value: float = 1.0, **labels: str) -> None:
        with _LOCK:
            self.series[self._norm(labels, self.series)] += value

    def value(self, **labels: str) -> float:
        with _LOCK:
            return self.series.get(_key(labels), 0.0)


class Gauge(_Metric):
    kind = "gauge"

    def __init__(self, *a, **k) -> None:
        super().__init__(*a, **k)
        self.series: dict = {}

    def set(self, value: float, **labels: str) -> None:
        with _LOCK:
            self.series[self._norm(labels, self.series)] = value


class Histogram(_Metric):
    kind = "histogram"

    def __init__(self, name: str, help_: str, label_names: tuple[str, ...] = (), buckets: tuple[float, ...] = LATENCY_BUCKETS) -> None:
        super().__init__(name, help_, label_names)
        self.buckets = buckets
        self.series: dict = {}

    def observe(self, value: float, **labels: str) -> None:
        with _LOCK:
            key = self._norm(labels, self.series)
            s = self.series.setdefault(key, {"counts": [0] * len(self.buckets), "sum": 0.0, "count": 0})
            for i, b in enumerate(self.buckets):
                if value <= b:
                    s["counts"][i] += 1
            s["sum"] += value
            s["count"] += 1


class Registry:
    def __init__(self) -> None:
        self.metrics: dict[str, _Metric] = {}

    def _get(self, cls, name: str, help_: str, labels: tuple[str, ...], **kw):
        with _LOCK:
            existing = self.metrics.get(name)
            if existing is not None:  # no duplicate registration: return the one instance
                if not isinstance(existing, cls):
                    raise ValueError(f"metric {name} already registered with another type")
                return existing
            m = cls(name, help_, labels, **kw)
            self.metrics[name] = m
            return m

    def counter(self, name, help_, labels=()) -> Counter:
        return self._get(Counter, name, help_, tuple(labels))

    def gauge(self, name, help_, labels=()) -> Gauge:
        return self._get(Gauge, name, help_, tuple(labels))

    def histogram(self, name, help_, labels=(), buckets=LATENCY_BUCKETS) -> Histogram:
        return self._get(Histogram, name, help_, tuple(labels), buckets=buckets)

    def render(self) -> str:
        out: list[str] = []
        with _LOCK:
            for m in sorted(self.metrics.values(), key=lambda x: x.name):
                out += [f"# HELP {m.name} {m.help}", f"# TYPE {m.name} {m.kind}"]
                for key, val in sorted(m.series.items()):
                    lab = ",".join(f'{k}="{_esc(v)}"' for k, v in key)
                    if isinstance(m, Histogram):
                        for b, c in zip(m.buckets, val["counts"], strict=True):
                            out.append(f'{m.name}_bucket{{{lab + "," if lab else ""}le="{b}"}} {c}')
                        out.append(f'{m.name}_bucket{{{lab + "," if lab else ""}le="+Inf"}} {val["count"]}')
                        out.append(f"{m.name}_sum{{{lab}}} {val['sum']:.6f}" if lab else f"{m.name}_sum {val['sum']:.6f}")
                        out.append(f"{m.name}_count{{{lab}}} {val['count']}" if lab else f"{m.name}_count {val['count']}")
                    else:
                        out.append(f"{m.name}{{{lab}}} {val}" if lab else f"{m.name} {val}")
        return "\n".join(out) + "\n"

    def reset(self) -> None:
        with _LOCK:
            for m in self.metrics.values():
                m.series.clear()
                m.dropped = 0


def _esc(v: str) -> str:
    return v.replace("\\", "\\\\").replace('"', '\\"').replace("\n", " ")


REGISTRY = Registry()
HTTP_REQUESTS = REGISTRY.counter("nova_http_requests_total", "HTTP requests by method, route template and status class", ("method", "route", "status_class"))
HTTP_LATENCY = REGISTRY.histogram("nova_http_request_duration_seconds", "HTTP request latency by route template", ("method", "route"))
HTTP_IN_FLIGHT = REGISTRY.gauge("nova_http_in_flight_requests", "Requests currently being processed")
JOBS = REGISTRY.counter("nova_jobs_total", "Background jobs by kind and outcome", ("kind", "outcome"))
JOB_DURATION = REGISTRY.histogram("nova_job_duration_seconds", "Background job duration by kind", ("kind",))
JOB_QUEUE_DELAY = REGISTRY.histogram("nova_job_queue_delay_seconds", "Delay between job creation and start (only where a creation time exists)", ("kind",))
JOB_RETRIES = REGISTRY.counter("nova_job_retries_total", "Recorded job retries", ("kind",))
WORKFLOW = REGISTRY.counter("nova_workflow_outcomes_total", "Workflow outcomes (indexing, static_analysis, tests, repair_verification, security_scan, cicd, readiness_gate)", ("workflow", "outcome"))
DEPENDENCY = REGISTRY.gauge("nova_dependency_up", "Last probed dependency state (1 up, 0 down)", ("dependency",))
UPTIME = REGISTRY.gauge("nova_process_uptime_seconds", "Process uptime")
CPU = REGISTRY.gauge("nova_process_cpu_seconds_total", "Process CPU time")
MEMORY = REGISTRY.gauge("nova_process_resident_memory_bytes", "Resident memory (when measurable on this platform)")
THREADS = REGISTRY.gauge("nova_process_threads", "Active threads")
ACTIVE_JOBS = REGISTRY.gauge("nova_jobs_in_flight", "Background jobs currently running", ("kind",))
GOV_POLICY_EVALS = REGISTRY.counter("nova_governance_policy_evaluations_total", "Governance policy evaluations by policy type and status", ("policy_type", "status"))
GOV_TRANSITIONS = REGISTRY.counter("nova_governance_transitions_total", "Governance workflow transitions/decisions by action and outcome", ("action", "outcome"))
GOV_QUEUE_DEPTH = REGISTRY.gauge("nova_governance_queue_open_items", "Open review-queue items (last dashboard/health read)")
GOV_PENDING = REGISTRY.gauge("nova_governance_workflows_in_review", "Workflow instances waiting for approval (last dashboard/health read)")
GOV_WORKFLOW_SECONDS = REGISTRY.histogram("nova_governance_workflow_duration_seconds", "Time from submission to a terminal workflow state", ("outcome",))
# Phase 16 integrations. Labels are bounded enums (integration type, result, category); never ids, URLs or user input.
INT_TESTS = REGISTRY.counter("nova_integration_connection_tests_total", "Integration connection tests", ("integration_type", "result"))
INT_INBOUND = REGISTRY.counter("nova_integration_inbound_total", "Inbound integration deliveries by outcome", ("integration_type", "outcome"))
WEBHOOK_ATTEMPTS = REGISTRY.counter("nova_webhook_delivery_attempts_total", "Outbound webhook delivery attempts", ("result",))
WEBHOOK_LATENCY = REGISTRY.histogram("nova_webhook_delivery_seconds", "Outbound webhook attempt duration", ("result",))
INT_JOBS = REGISTRY.counter("nova_integration_jobs_total", "Integration jobs finished by operation and result", ("operation", "result"))
INT_QUEUE_DEPTH = REGISTRY.gauge("nova_integration_queue_depth", "Pending outbound deliveries plus queued jobs (last worker/health read)", ())
INT_OLDEST_AGE = REGISTRY.gauge("nova_integration_oldest_due_age_seconds", "Age of the oldest due delivery or job (last worker/health read)", ())
INT_EXTENSION_RUNS = REGISTRY.counter("nova_extension_runs_total", "Built-in extension operations", ("extension", "result"))
TELEMETRY_DROPPED = REGISTRY.counter("nova_telemetry_dropped_total", "Telemetry dropped because a bounded buffer was full", ("signal",))
NOTIFICATIONS = REGISTRY.counter("nova_notification_deliveries_total", "Alert notification delivery attempts", ("channel", "outcome"))

# ---- per-minute windows (only for SLO / alert computations; bounded to WINDOW_MINUTES) ----
_WIN: dict[str, dict[int, dict[str, float]]] = defaultdict(dict)


def _minute(ts: float | None = None) -> int:
    return int((ts if ts is not None else time.time()) // 60)


def window_add(stream: str, field: str, value: float = 1.0, ts: float | None = None) -> None:
    with _LOCK:
        m = _minute(ts)
        buckets = _WIN[stream]
        buckets.setdefault(m, {})[field] = buckets.get(m, {}).get(field, 0.0) + value
        if len(buckets) > WINDOW_MINUTES + 5:
            for old in sorted(buckets)[: len(buckets) - WINDOW_MINUTES]:
                del buckets[old]


def window_sum(stream: str, minutes: int, now: float | None = None) -> dict[str, float]:
    with _LOCK:
        end = _minute(now)
        total: dict[str, float] = defaultdict(float)
        for m, fields in _WIN.get(stream, {}).items():
            if end - minutes < m <= end:
                for k, v in fields.items():
                    total[k] += v
        return dict(total)


def streams(prefix: str) -> list[str]:
    with _LOCK:
        return sorted(k for k in _WIN if k.startswith(prefix))


def reset_windows() -> None:
    with _LOCK:
        _WIN.clear()


def quantile(stream: str, minutes: int, q: float, now: float | None = None) -> float | None:
    """Approximate quantile from the latency bucket counts of a window (upper bucket bound; None when no samples)."""
    s = window_sum(stream, minutes, now)
    total = s.get("count", 0.0)
    if not total:
        return None
    target = q * total
    for b in LATENCY_BUCKETS:
        if s.get(f"le_{b}", 0.0) >= target:
            return b
    return math.inf


def record_latency(stream: str, seconds: float, ts: float | None = None) -> None:
    window_add(stream, "count", 1, ts)
    window_add(stream, "sum", seconds, ts)
    for b in LATENCY_BUCKETS:
        if seconds <= b:
            window_add(stream, f"le_{b}", 1, ts)


def count_within(stream: str, minutes: int, threshold: float, now: float | None = None) -> tuple[float, float] | None:
    """(requests within threshold, total) using the smallest bucket >= threshold. None when no sample exists."""
    s = window_sum(stream, minutes, now)
    total = s.get("count", 0.0)
    if not total:
        return None
    bucket = next((b for b in LATENCY_BUCKETS if b >= threshold), None)
    return (s.get(f"le_{bucket}", 0.0) if bucket is not None else total, total)


def update_resources() -> dict[str, float | None]:
    import threading as _t

    uptime = time.time() - STARTED_AT
    UPTIME.set(uptime)
    cpu = time.process_time()
    CPU.set(cpu)
    THREADS.set(_t.active_count())
    rss: float | None = None
    try:  # optional psutil, else Linux /proc, else resource (not on Windows); None when unmeasurable
        import psutil  # type: ignore[import-not-found]

        rss = float(psutil.Process().memory_info().rss)
    except Exception:  # noqa: BLE001, S110
        try:
            with open("/proc/self/statm") as fh:
                import os

                rss = float(int(fh.read().split()[1]) * os.sysconf("SC_PAGE_SIZE"))
        except Exception:  # noqa: BLE001, S110
            try:
                import resource

                rss = float(resource.getrusage(resource.RUSAGE_SELF).ru_maxrss) * 1024
            except Exception:  # noqa: BLE001, S110
                rss = None
    if rss is not None:
        MEMORY.set(rss)
    return {"uptime_seconds": uptime, "cpu_seconds": cpu, "memory_bytes": rss, "threads": float(_t.active_count())}


def started_at_iso() -> str:
    return datetime.fromtimestamp(STARTED_AT, UTC).isoformat()
