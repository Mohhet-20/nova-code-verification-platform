# Observability, Reliability Engineering and Operational Intelligence

Everything runs in-process with **no mandatory external vendor and no new dependency**. This document is explicit about what is implemented, what is exported, and what is only local or experimental.

## Signals: implemented vs exported

| Signal | Implemented | Exported | Limits |
|---|---|---|---|
| Structured logs | JSON to stderr (`NOVA_LOG_JSON`), bounded in-memory ring (`NOVA_OPS_LOG_RING_SIZE`) for the UI | stderr only | ring lost on restart; stack traces stay in the stderr stream and are never served by the API |
| Correlation IDs | per request, `X-Request-ID` response header, log/span fields | header | incoming IDs are validated (`^[A-Za-z0-9][A-Za-z0-9._-]{7,63}$`); anything else (spaces, control chars, long values) is discarded and a new ID is generated |
| Traces | W3C-compatible trace/span IDs, parent links, sampling (`NOVA_OPS_TRACE_SAMPLE_RATIO`), bounded ring | optional exporter callable (off by default) | an incoming `traceparent` only links the trace id for correlation, it is never identity; **OTLP export is not implemented or verified against a collector** |
| Metrics | counters/gauges/histograms, per-minute windows | Prometheus text at `GET /api/v1/ops/metrics` (authenticated, owner only) | in memory, reset on restart; ≤300 series per metric, overflow collapses into an `other` series; labels are route *templates*, never ids, repo names or secrets |
| Health | liveness, readiness, dependency health | `/api/v1/health`, `/api/v1/ready`, `/api/v1/ops/health` | probes have timeouts and a short TTL cache (`NOVA_OPS_HEALTH_CACHE_SECONDS`); optional integrations are reported configured/disabled and **not contacted** |
| Alerts / incidents | versioned rules, deduped incidents, lifecycle | incidents (DB); optional signed webhook / Slack-compatible webhook | evaluated on request; periodic only when `NOVA_OPS_PERIODIC_EVALUATION=true`; **email is not implemented** |
| SLOs | five SLIs, error budgets, burn rate | API/UI | computed from in-memory windows (max 24 h); a restart resets history |

### Architecture (separation of concerns)
`app/observability/`: `context.py` (propagation), `redact.py` + `app/core/logging.py` (instrumentation of logs), `middleware.py` / `jobs.py` / `tracing.py` (instrumentation + context), `metrics.py` (collection + aggregation + Prometheus rendering), `health.py`, `alerts.py` (rule evaluation), `incidents.py` (events + lifecycle), `notify.py` (adapters), `slo.py`, `diagnostics.py`, `retention.py`, `periodic.py`. API: `app/api/ops.py`. Telemetry code never calls the services it observes, so there is no circular telemetry; the middleware, span and job wrappers swallow their own errors and always re-raise the original exception.

## Logging
Fields: UTC `ts`, `level`, `logger`, `event`, `message`, `service`, `environment`, `request_id`, `trace_id`, `span_id`, `job_id`, `job_type`, `error_class`, `duration_ms` (+ `stack` in the stderr stream only). Credentials (GitHub/AWS/Slack/Stripe/Google/JWT/private-key shapes and `password=`/`token=` assignments) are redacted by `app.services.analysis.redaction.scrub`; any extra whose **key** looks sensitive (authorization, cookie, password, secret, token, api key, signature, session…) is replaced with `[REDACTED]`. Strings are length-bounded and control characters / newlines are neutralised (log injection).

## HTTP and job context
Per request: method, **full route template** (`/api/v1/repositories/{repository_id}`; unmatched → `unmatched`), status class, latency histogram. Health/readiness/metrics requests are recorded in raw metrics but **excluded from the SLO/alert request stream by an explicit rule** (`health_checks` exclusion shown on each SLO).
Jobs (`@tracked` / `track()`): kind, id, outcome, duration, queue delay (only where the record has a creation time), retries, failure class, and `parent.request_id` only when a request context is active when the job runs (no invented parent links). Outcome comes from the job's recorded status where one exists (execution failure, not the verdict on the code under test; a test run whose tests fail is a *succeeded* job). Instrumented: indexing, static analysis, test execution, verification, security scans, CI/CD, review, repair/refactor/debug verification.

## Health
- **Liveness** `GET /api/v1/health` — no dependencies. **Readiness** `GET /api/v1/ready` — shape unchanged (DB + migrations).
- **Dependency health** `GET /api/v1/ops/health[?force=true]`: database, migrations, storage (critical), workers (critical; jobs run in-process via BackgroundTasks, no external worker exists), notifications and OSV (optional; configured/disabled only). States: `ok`, `degraded` (optional failed), `unavailable` (critical failed). Only the exception *class* is reported.

## Incidents and the alert engine
Event fields: id, type, severity, component, occurred/detected timestamps, request/trace refs, summary, evidence, actor, note (table `ops_events`, append-only). Incident lifecycle `open → acknowledged → investigating → resolved → reopened` (`ops_incidents`, one row per deterministic fingerprint `sha256(rule|scope)`; recurrence of a resolved incident reopens it and appends history; resolution requires a note; every transition is audited). Auto-resolve only closes incidents still `open`/`reopened` when the rule clears; acknowledged/investigating ones wait for a human.

Rules (versioned in `ops_configs`; changing one writes a new version and a `config_changed` event; old versions are kept): `http_error_rate`, `http_latency_p95`, `job_failure_rate`, `queue_delay_p95`, `db_connectivity`, `security_scan_failures`, `ci_failures`, `persistent_readiness_degradation`. Each has window, comparator, threshold, `min_samples`, severity, cooldown, enabled, scope. Every evaluation returns the rule/version, window, measurements, observed vs threshold, samples, fired/not, and `missing_data` (status `no_data` — **never reported as OK**). Evaluations are stored (`ops_evaluations`).
`POST /api/v1/ops/alerts/evaluate` evaluates on request. There is **no continuous monitoring** unless `NOVA_OPS_PERIODIC_EVALUATION=true` (asyncio loop every `NOVA_OPS_EVALUATION_INTERVAL_SECONDS`).

## Notifications
Opt-in. Webhook: `NOVA_OPS_WEBHOOK_URL`, signing secret read from the env var *named* by `NOVA_OPS_WEBHOOK_SECRET_ENV` (header `X-Nova-Signature: sha256=HMAC(secret, timestamp + "." + body)`, `X-Nova-Timestamp`, `X-Nova-Idempotency-Key`). Slack-compatible: the URL is read from the env var named by `NOVA_OPS_SLACK_WEBHOOK_ENV`. Payload is minimal (incident id, rule, title, severity, component, status, ≤300-char summary, counts, timestamps). Timeout `NOVA_OPS_NOTIFY_TIMEOUT_SECONDS`, up to `NOVA_OPS_NOTIFY_MAX_ATTEMPTS` with exponential backoff (5xx/429/network retried; other 4xx not), attempts recorded in `ops_deliveries` (host only, never URL/secret), idempotency key per channel+incident+status+occurrence. **SSRF guard**: https only, no credentials in URLs, every resolved address checked and blocked if loopback/private/link-local/multicast/reserved/unspecified/cloud-metadata (IPv4-mapped IPv6 unwrapped); the connection goes to the validated IP (no second DNS lookup) with SNI/Host set to the hostname; `NOVA_OPS_NOTIFY_ALLOW_PRIVATE=true` is the explicit override. Failures never propagate. **Not verified against real Slack or webhook receivers.**

## SLOs
SLIs: availability, latency (within `threshold_seconds`, using the smallest histogram bucket ≥ threshold), job completion, workflow success, dependency availability. Defaults seeded as version 1; edits create versions (`GET /ops/slos/{key}/versions`). Result: good, total, SLI, target, allowed bad events, consumed/remaining budget, burn rate, status `ok | at_risk (≥75% consumed) | exhausted (SLI < target) | insufficient_data (< min_samples) | no_data`, plus `calculation`, `exclusions`, `effective_window_minutes`, `observed_since` and limitations. **No data ≠ zero failures.**

## Diagnostics
Deterministic rules over in-memory telemetry + recorded events: repeated workflow failures, repeated failure reason (security/CI), job timeouts, dependency-correlated failures (database specialised), latency after a recorded deployment/config change. Each finding carries signals, window, evidence refs, confidence (always `low`/`medium`), alternative explanations, investigation steps, and `causal_claim: false`. Latency-after-change is a temporal association only. An empty result means no rule matched, not health. Deployments are recorded explicitly (`POST /ops/events/deployment`); config changes are recorded automatically.

## Security, privacy, retention
All routes require the request context. Process-wide telemetry that could expose other tenants' identifiers (metrics, logs, traces, retention) is **owner-only**; incidents, rules, SLOs, deliveries are workspace-scoped (404 across workspaces). Writes need a write role and are audited. `ops_retention_days` (default 90) drives `POST /ops/retention/run`, which deletes old events (not those of unresolved incidents), evaluations and delivery records. **Audit events, configs and incidents are never deleted.**

## Performance and resilience
Bounded: log ring, span ring, export queue (1000), series per metric (300), per-minute windows (24 h), page sizes (≤100), probe pool (4 threads). Drops are counted (`nova_telemetry_dropped_total`). On a full ring the oldest record is overwritten. All telemetry is lost on restart (documented in the UI). Process CPU uses `time.process_time()`; memory uses optional `psutil`, then `/proc`, then `resource`, otherwise reported as "not measurable" (e.g. Windows without psutil).

## Readiness integration
Opt-in readiness rule `require_operational_health`: a change request fails readiness while a **critical** operational incident is unresolved. A pass means "none is recorded" because alerts are evaluated on request.

## Run locally
```
cd backend && . venv/bin/activate        # or your venv
alembic upgrade head                     # creates ops_* tables (0013)
uvicorn app.main:app --reload
curl -H "Authorization: Bearer $NOVA_API_TOKEN" http://127.0.0.1:8000/api/v1/ops/overview
curl -X POST http://127.0.0.1:8000/api/v1/ops/alerts/evaluate
curl http://127.0.0.1:8000/api/v1/ops/metrics            # Prometheus text (owner)
```
UI: `/operations`. Optional settings (placeholders only): `NOVA_OPS_WEBHOOK_URL=https://hooks.example.invalid/nova`, `NOVA_OPS_WEBHOOK_SECRET_ENV=MY_HOOK_SECRET`, `NOVA_OPS_SLACK_WEBHOOK_ENV=MY_SLACK_URL`, `NOVA_OPS_PERIODIC_EVALUATION=true`.

## Known limitations
In-memory telemetry (restart resets metrics/spans/logs and SLO history); no OTLP exporter, no email, no live Slack/webhook verification; evaluation is on request unless enabled; single-process only (multiple workers each have their own telemetry); job queue delay only where a creation time exists; repair/refactor/review/debug jobs record failures by exception only (no status read); memory not measurable on some platforms; no browser or Windows verification.
