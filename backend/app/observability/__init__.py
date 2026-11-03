"""NOVA observability: structured logs, correlation, tracing, bounded metrics, job tracking, health, incidents, alerts, SLOs and diagnostics.

Everything runs in-process with no external dependency. Metrics, spans and recent logs live in bounded in-memory buffers and are lost on restart;
operational events, incidents, evaluations and delivery records are persisted. See docs/architecture/OBSERVABILITY.md.
"""
