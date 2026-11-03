# Changelog

Format: [Keep a Changelog](https://keepachangelog.com/). Versions follow the phase roadmap, not semver releases.

## [Unreleased] - 2026-11-03 - publication pass

### Fixed
- Timelines (run events, audit trails, review-queue and workflow history) could list same-tick rows out of order: every such query orders by
  `(created_at, id)` and `id` was a random uuid4. `new_id()` is now time-ordered (nanosecond prefix, strictly increasing per process, plus
  64 random bits; still 32 hex characters, no migration). Surfaced as an intermittent failure of
  `test_cancel_requested_while_running_keeps_partial_results` under load; `tests/test_ids.py` pins the property.

### Changed
- Repository layout for publication: the phase specifications and master roadmap are kept outside the repository;
  `PROJECT_STATUS.md` moved to `docs/implementation/`.

## [0.19.0] - 2026-10-22 - Phase 19: production deployment readiness and release engineering (decision: NOT DEPLOYMENT READY - container build unverified)

### Added (portfolio pass)
- **AI-assisted repair proposals** (`NOVA_AI_PROVIDER=anthropic`, off by default): Claude (`claude-opus-5-5`, structured outputs, server-side refusal fallback)
  proposes search/replace edits; NOVA's edit gate (allowed files only, exactly-one-match anchors, no redacted text, edit limit) turns them into an ordinary
  draft patch that still needs validation, approval and test verification. `source: ai_assisted`, provenance in the audit trail, UI action with disclosure.
  Settings `NOVA_AI_MODEL`, `NOVA_AI_EFFORT`, `NOVA_AI_TIMEOUT_SECONDS`, `NOVA_AI_MAX_CONTEXT_BYTES`, `NOVA_AI_MAX_EDITS`; production warning `ai_egress`.
- Migration `0017`: widens `ck_repair_patches_source`; the downgrade refuses to run while AI-assisted rows exist.
- Git repository (initial commit `7fa5a8a`); release manifests now record the real revision. Real screenshots and a rewritten README.
### Fixed (portfolio pass)
- Whole-graph loads (metrics, cycles) collapsed under concurrency because Python's `sqlite3` releases the GIL per row: graph loaded as one SQLite row
  (`/metrics` at 8 concurrent clients: 9.8 -> 74 req/s, p50 893 -> 95 ms).
- Indexing time budget used `time.monotonic()`, which ticks every ~15.6 ms on Windows (a short walk could read 0 s elapsed); now `perf_counter`.
- Task planning raised an error for any provider other than `none`; it stays deterministic with `anthropic`.
- Stale "local-first, never uses AI" statements in the README and architecture documents.

### Fixed
- Compose `migrate` crashed after migrating (deployment history written into the read-only code tree), so `api` could never start: the image sets `NOVA_RELEASE_STATE_DIR` / `NOVA_BACKUP_DIR` on `/data`; history-write failures are warnings.
- nginx proxied to a non-existent `backend` host; now `api:8000`, resolved per request.
- Repository test code could read the API's secrets from `/proc/<pid>/environ`: the API process is made non-dumpable at startup (Linux).
- Git searched above the registered root (an un-versioned directory inside another repository was reviewed as the outer repository): `GIT_CEILING_DIRECTORIES` on every git call.
- The production template's own placeholder token passed the weak-token check.
- Clean `npm ci` + build failed (`@types/node` missing from `package.json` but present in the lockfile).
- Runtime image lacked `git` and `pytest` (code review / test execution unusable in containers).
- Backups dropped empty artifact directories, so restores after a refused test run reported `ok: false`.
- `app.release integrity` crashed on an unmigrated database.
- Phone-width layout overflow; UI copy that claimed nothing is ever executed or modified.
- Flaky frontend tests (async timeout); Windows test portability (CRLF fixtures, POSIX-only checks).
### Added
- Per-client throttling of invalid bearer tokens (`NOVA_AUTH_FAILURE_LIMIT`, `NOVA_AUTH_FAILURE_WINDOW_SECONDS`; 429 + `Retry-After`).
- `NOVA_TEST_EXECUTION_ENABLED` kill switch for every feature that runs repository code; production contract warnings `test_execution_unsandboxed`, `auth_throttle_disabled`.
- Security headers on every `/api` response; `PATCH` allowed by CORS; `NOVA_BACKUP_DIR` for the release CLI.
- `scripts/deployment_smoke.py` (end-to-end post-deploy checks incl. write workflow, review, test execution, web proxy), `frontend/e2e/browser_smoke.mjs` (real Chrome/Edge).
- `deployment/docker-compose.tls.yml` + `Caddyfile` (optional automatic HTTPS); repository bind mount; log rotation; digest-pinned base images; SHA-pinned GitHub Actions; CI job that boots the compose stack and runs the smoke test and a restore.
- Operations docs: DEPLOYMENT_AUDIT, DEPLOYMENT_BLOCKERS, ARCHITECTURE_AND_NETWORKING, INCIDENT_RESPONSE, RELEASE_CHECKLIST, FINAL_DEPLOYMENT_REPORT.
### Changed
- Renamed for clarity: CONFIGURATION -> PRODUCTION_CONFIGURATION, BACKUP_RESTORE -> BACKUP_AND_RESTORE, ROLLBACK -> ROLLBACK_AND_RECOVERY, RUNBOOKS -> RUNBOOK, DEPLOYMENT -> DEPLOYMENT_GUIDE (references in older entries and documents were updated to the new names).
- `pytest` moved from `requirements-dev.txt` to `requirements.txt` (runtime dependency of test execution).
- Base images: Python 3.12.15-slim-bookworm, Node 22.23.4-alpine3.24, nginx-unprivileged 1.30.6-alpine (1.27 is unmaintained). API container memory limit 1 GiB -> 2 GiB.
- Version 0.19.0. One migration (`0017`, head).

## [0.18.0] - 2026-10-21 - Phase 18: final acceptance, security audit, benchmarks and release certification (decision: NOT RELEASE READY)

### Added
- `tests/test_acceptance_sweeps.py` (route-wide authentication, role, cross-tenant, credential-leak, git option-injection, path-traversal and request-size sweeps), `test_acceptance_e2e.py`, `test_acceptance_resilience.py`, `test_acceptance_bench.py`.
- `docs/acceptance/*`, `docs/security/FINAL_SECURITY_AUDIT.md`, `docs/performance/FINAL_BENCHMARK_REPORT.md`, `docs/operations/RELEASE_READINESS_REPORT.md`.
### Fixed
- F-001: global request-body size limit (`NOVA_MAX_REQUEST_BODY_BYTES`, default 8 MiB, 413).
- F-002: missing / unmigrated / corrupt database now answers 503 `database_unavailable` instead of a generic 500.
### Changed
- Version 0.18.0.

## [0.17.0] - 2026-10-21 - Phase 17: release engineering, deployment, disaster recovery and operational readiness (PARTIAL)

### Added
- `app/release/` and `python -m app.release` (check-config, status, classify, migrate, backup, verify, list-backups, restore, prune, integrity, manifest, smoke, deploy, history, rollback-plan).
- Production configuration contract with startup refusal (weak or placeholder token, wildcard CORS, relaxed SSRF guard, public metrics, bad encryption key, schema not at head); API docs disabled in production; graceful shutdown awaits background loops.
- Verified backups (SQLite online backup, SHA-256 manifest, atomic rename), isolated restore with integrity / foreign-key / file-reference checks, safe retention; locked migration with pre-migration backup; additive-migration classifier; rollback planner that never downgrades.
- Release manifest and CycloneDX SBOM (unsigned), secret scanner, Dockerfiles, nginx config, compose file (unbuilt), CI security/container/migration gates, tag-only release workflow, `docs/operations/` (deployment, configuration, backup/restore, rollback, release, 16 runbooks).
### Changed
- Test fixture migrates the template database once per session and copies it per test (suite is much faster; migrations are still tested directly).
- Removed six empty throwaway test files.
### Not verified
- Docker builds, GitHub workflow execution, PostgreSQL, multi-process deployment, TLS, image scanning, production deployment, DR drill on separate hardware.

## [0.16.0] - 2026-10-21 - Phase 16: enterprise integrations, API contracts and extension ecosystem (PARTIAL)

### Added
- Migration `0016` (12 tables): integration installations, encrypted credential envelopes, inbound events, CI evidence runs, webhook subscriptions, outbox events, deliveries and attempts, integration jobs, idempotency records, external links, extension activations.
- `app/services/integrations/`: typed adapter registry (GitHub, GitHub Issues, local test), AES-256-GCM credential store (fail-closed, key outside the DB, rotation via `reencrypt_all`), SSRF-guarded HTTP client, signature-verified deduplicated inbound webhooks, outbox + signed/retried/replayable outbound webhooks, durable jobs with leases, built-in-only extension registry with strict manifests, idempotency keys, issue links.
- `external_ci_required` governance policy type (fresh success required; missing, pending, cancelled, unknown or stale evidence is never a pass).
- `/api/v1/integrations/*` API, API contract governance (`app/contracts.py`, `contracts/openapi.baseline.json`, `tests/test_api_contract.py`), integration metrics, optional in-process worker (`NOVA_INTEGRATION_WORKER_ENABLED`).
- Integration Center UI under `/integrations` and `docs/architecture/INTEGRATIONS.md`.
- Dependency: `cryptography==50.0.2`. New settings: `NOVA_INTEGRATION_SECRET_KEY`, `NOVA_INTEGRATION_SECRET_KEYS_PREVIOUS`, `NOVA_INTEGRATION_*`, `NOVA_WEBHOOK_*`.

### Known gaps
- UI for issue search/link/create, status publishing and the jobs panel; accessibility audit; retention purge; PR-event to change-request mapping; all behaviour against real providers is unverified (mocked HTTP only).

## [0.15.0] - 2026-10-21 - Phase 15: enterprise governance, team collaboration and workflow orchestration

### Added
- Migration `0015`: teams, team memberships, repository ownership, per-user API tokens, versioned governance policies, policy evaluations, versioned workflow definitions, workflow instances / decisions / events, review queue items and history; `memberships.status` / `removed_at`. SQLite triggers make audit, workflow-event, decision, evaluation, policy-version and queue-history rows append-only.
- Central deny-by-default authorization (`app/security/authz.py`), per-user tokens (`nova_u_…`), organization roles (owner, maintainer, engineer, reviewer, auditor, viewer), team roles, separation of duties, last-administrator protection.
- Deterministic policy engine (nine policy types; unavailable evidence is never a pass), guarded workflow engine (versioned definitions, snapshots, stages, quorum, optimistic locking), persistent review queue, read-only governance audit API, overview and health endpoints.
- (Partial UI) UI under `/governance` (overview, my approvals, review queue, teams, workflow detail with timeline and decision controls, policies and evaluation history, audit) with confirmation dialogs and loading / empty / error / permission-denied states.
- `docs/architecture/GOVERNANCE.md`.

### Changed
- Repositories owned by a team are hidden (404) from non-members and from roles without organization-wide visibility; unowned repositories stay workspace-visible.
- Unknown, revoked or removed user tokens return 401.

## [0.14.0] - 2026-10-20 - Phase 14: performance engineering, scalability and resource optimization

### Added
- Migration `0014`: single-column indexes on `file_imports.resolved_file_id`, `relationships.source_file_id`, `relationships.target_file_id`.
- `app/perf/` benchmark framework and CLI (`python -m app.perf`), deterministic fixtures, baselines/budgets (`backend/benchmarks/baseline.json`; a missing baseline is never a pass).
- `NOVA_INDEX_MAX_CONCURRENT_RUNS` (default 4): indexing admission limit (HTTP 429). Frontend: lazily loaded routes, cancellable list requests. `docs/architecture/PERFORMANCE.md`.

### Changed
- Static security analysis shares one per-file call/taint computation across rules (findings unchanged).
- Generation publish is much faster at scale (see PERFORMANCE.md for measured numbers and limits).

## [0.13.0] - 2026-10-20 - Phase 13: observability, reliability engineering and operational intelligence

### Added
- Migration `0013`: `ops_configs`, `ops_incidents`, `ops_events`, `ops_evaluations`, `ops_deliveries`.
- `app/observability/` (context, redact, metrics, tracing, middleware, jobs, health, incidents, alerts, notify, slo, diagnostics, retention, periodic) and `app/api/ops.py` (`/api/v1/ops/...`).
- Structured redacted JSON logs with correlation fields; `X-Request-ID`; route-template HTTP metrics; Prometheus text endpoint; job/workflow metrics for all background jobs.
- Versioned alert rules, deduped incident lifecycle, SSRF-guarded signed-webhook / Slack-compatible notifier, SLOs with error budgets, deterministic diagnostics, retention.
- Opt-in readiness rule `require_operational_health`. UI: `/operations`. `docs/architecture/OBSERVABILITY.md`; `NOVA_OPS_*` settings.

### Changed
- The access-log middleware is replaced by `ObservabilityMiddleware`; `app/core/logging.py` gained structured fields and redaction; the unhandled-error response now carries `X-Request-ID`; CORS exposes it.

## [0.12.0] - 2026-10-20 - Phase 12: enterprise security engineering and supply-chain intelligence

### Added
- Migration `0012`: `sec_scans`, `sec_components`, `sec_advisories`, `sec_findings`, `sec_events`, `sec_suppressions`, `sec_exceptions`, `sec_policies`, `sec_evaluations`, `sec_remediations`, `sec_reports`.
- `app/services/security/` (inventory, versions, advisories, codescan, secrets_scan, priority, policy, scan, service, remediation, reports), analyzer `py-security-flow` (PY-SEC-006..014), `app/api/security.py` (`/repositories/{id}/security...`).
- Opt-in readiness rule `require_security_scan`; CI reports state whether a scan covered the exact revision.
- UI: `/repositories/:id/security` (Security Intelligence). `docs/architecture/SECURITY.md`; `NOVA_SECURITY_*` settings.

## [0.11.0] - 2026-10-19 - Phase 11: CI/CD integration, pull-request intelligence and release readiness

### Added
- Migration `0011`: `ci_connections`, `ci_readiness_policies`, `change_requests`, `ci_workflow_runs`, `ci_status_reports`, `ci_webhook_deliveries`, `ci_reports`.
- `app/services/cicd/` (providers, service, webhook, readiness, reports) and `app/api/cicd.py` (`/repositories/{id}/ci...`, `/webhooks/github/{connection_id}`).
- UI: `/repositories/:id/workflow` and `/workflow/change-requests/:crId` (Engineering Workflow Center).
- `.github/workflows/ci.yml`; `docs/architecture/CICD.md`; `NOVA_CI_*` settings.

### Changed
- App version constant is `0.11.0`. Import ordering in migrations 0005-0010 normalised so `ruff check .` passes in CI.

## [0.10.0] - 2026-10-19 - Phase 10: Continuous verification, regression intelligence and quality gates

### Added
- Migration `0010`: `cv_policies`, `cv_baselines`, `cv_runs`, `cv_checks`, `cv_events`.
- `app/services/verification/` (changes, planner, engine, regression, gate, policy, service) and `app/api/verification.py` (`/repositories/{id}/verification...`).
- UI: `/repositories/:id/verification` and `/repositories/:id/verification/:runId`.
- `docs/architecture/CONTINUOUS_VERIFICATION.md`; `NOVA_CV_*` settings.

### Changed
- App version constant is `0.10.0`. Roadmap Phase 10 now describes this work.

## [0.9.0] - 2026-10-19 - Phase 09: Intelligent refactoring and multi-file transformation

### Added
- Migration `0009`: `refactor_transformations`, `refactor_verifications`.
- `app/services/refactor/` (registry, Python and JS/TS rename, duplicates, extract-function, simplify, cycles, impact, lifecycle, verification) and `app/api/refactor.py` (`/repositories/{id}/refactors...`).
- UI: `/repositories/:id/refactors` and `/refactors/:refactorId`.
- `docs/architecture/REFACTORING.md`; `NOVA_REFACTOR_*` settings.

### Changed
- App version constants are `0.9.0`. Roadmap Phase 09 is now refactoring; the earlier "Git integration" scope is unscheduled.

## [0.8.0] - 2026-10-19 - Phase 08: Automated repair suggestions, patch generation and safe verification

### Added
- Migration `0008`: `repair_investigations`, `repair_patches`, `repair_verifications`.
- `app/services/repair/` (patch engine, evidence and ranking, deterministic autofix, proposal, test selection, lifecycle service, verification) and `app/api/repair.py` (`/repositories/{id}/repairs...`).
- UI: `/repositories/:id/repairs` and `/repositories/:id/repairs/:repairId`.
- `docs/architecture/REPAIR.md`; `NOVA_REPAIR_*` settings.

### Changed
- App version constants are `0.8.0`; roadmap Phase 08 now describes this work.

## [0.7.0] - 2026-10-19 - Phase 07: Intelligent code review, change-risk analysis and regression prevention

### Added
- Migration `0007`: `code_reviews`, `review_changes`, `review_changed_symbols`, `review_findings`.
- `app/services/review/` (gitops, symbols, risk, findings, impact, recommend, pipeline) and `app/api/review.py` (`/repositories/{id}/reviews...`).
- UI: `/repositories/:id/reviews` and `/repositories/:id/reviews/:reviewId`.
- `docs/architecture/CODE_REVIEW.md`; `NOVA_REVIEW_*` settings.

### Changed
- App version constants are `0.7.0`; roadmap Phase 07 now describes this work.

## [0.6.0] - 2026-10-19 - Phase 06: Bug localization, root-cause analysis and evidence-based debugging

### Added
- Migration `0006`: `failure_reports`, `failure_frames`, `failure_occurrences`, `localization_candidates`, `investigations`, `verification_runs`.
- `app/services/debugging/` (parsing, ingest/fingerprint, localization, investigations and impact, verification) and `app/api/debugging.py`.
- UI: `/repositories/:id/failures` and `/repositories/:id/failures/:failureId`.
- `docs/architecture/DEBUGGING.md`; `NOVA_DEBUG_*` settings; HTTP 413 `payload_too_large`.

### Changed
- App version constants are `0.6.0`; `begin_existing` accepts validated test targets; roadmap Phase 06 now describes this work.

## [0.5.0] - 2026-10-19 - Phase 05: Test generation, coverage-gap analysis, safe execution and verification workflows

### Added
- Migration `0005`: `test_discoveries`, `test_files`, `test_generation_runs`, `test_candidates`, `test_execution_runs`, `coverage_files`.
- `app/services/testing/`: static pytest discovery, facts/planner/generator, validator, restricted-subprocess runner with coverage.py (branch), execution orchestration, gap analysis.
- API: `tests/discover|plan|generate|execute`, `tests`, `tests/files`, `tests/generations[/{id}[/validate|approve|execute]]`, candidate export, `tests/runs[/{id}]`, `coverage`, `coverage/files`, `coverage/gaps`.
- UI: `/repositories/:id/testing` (overview, discovery, coverage, generation/review/approval, runs).
- `docs/architecture/TEST_ENGINEERING.md`; `coverage` dependency; `NOVA_ARTIFACT_DIR`, `NOVA_TEST_*` settings.

### Changed
- App version constants are `0.5.0`; roadmap Phase 05 now describes this work.

## [0.4.0] - 2026-10-18 - Phase 04: Static analysis, code-quality diagnostics and actionable findings

### Added
- Migration `0004`: `analysis_runs`, `analysis_file_states`, `findings`.
- Analyzer framework and 19 Python rules (`PY-COR-001..003`, `PY-REL-001..003`, `PY-MNT-001..007`, `PY-CPX-001`, `PY-SEC-001..005`) on the standard-library `ast`; cyclomatic complexity with a documented definition.
- Findings with line-independent fingerprints, lifecycle (new / existing / resolved / reopened / suppressed), resolution reasons, secret redaction.
- Strict repository configuration `.nova/analysis.json` (rules, severity, thresholds, exclusions, generated files, language-specific rules).
- Incremental runs keyed by content hash and configuration; run diagnostics for parse failures, rule errors and limits; startup recovery of interrupted runs.
- API: `POST /analysis`, `GET /analysis/status|runs|runs/{id}|summary|rules|config`, `GET /findings`, `GET|PATCH /findings/{id}`.
- UI: `/repositories/:id/health` (analysis controls, overview, findings explorer, details, suppress / reinstate).
- `docs/architecture/STATIC_ANALYSIS.md`; `TooManyRequestsError` (429); `NOVA_ANALYSIS_*` settings.

### Changed
- App version constants are `0.4.0`.
- Roadmap Phase 04 now describes this work; "Requirements decomposition and task planning" is unscheduled.

## [0.3.0] - 2026-10-18 - Phase 03: Semantic code intelligence, dependency analysis and impact assessment

### Added
- Migration `0003`: `file_calls` table; `relationships.line`; indexes for reverse traversal and metrics.
- Call-site extraction (Python, JavaScript, JSX, TypeScript, TSX) and dynamic-import detection (recorded as `unsupported`, never as edges).
- Conservative name-based call resolution (`resolved` / `ambiguous` / `unresolved` / `unsupported` / `external`) and `CALLS` edges (inferred).
- Graph algorithms: bounded deterministic BFS, iterative Tarjan SCC, longest-path depth on the condensation; cycle severity rules.
- API: `GET /symbols/{symbol_id}`, `GET /dependencies`, `GET /dependencies/impact`, `GET /dependencies/cycles`, `GET /index/status`, `POST /index/rebuild`, `GET /metrics`; `q` filter on `GET /symbols`.
- UI: `/repositories/:id/intelligence` (overview cards, symbol explorer and detail, dependency explorer, impact panel, cycles, index controls with rebuild confirmation).
- `docs/architecture/CODE_INTELLIGENCE_GRAPH.md`; Phase 03 benchmarks in `scripts/measure_indexing.py`.

### Changed
- `ANALYSIS_VERSION` is 2: the first index after upgrading re-parses every file.
- Self-imports now produce an `IMPORTS_FILE` self-edge (reported as `info` cycles) instead of being dropped; `IMPORTS_FILE` and `DEPENDS_ON_PACKAGE` edges now record the import line.
- `/capabilities` lists `CALLS` as supported (name-based) and no longer lists it as unsupported.
- App version constants (`APP_VERSION`, `pyproject.toml`) were still `0.1.0`; they are now `0.3.0`.

## [0.2.0] - 2026-10-18 - Phase 02: Repository intelligence

### Added
- Migration `0002`: generation metadata on snapshots, per-file analysis fields, `symbols`, `file_imports`, `relationships`,
  `index_diagnostics`; data-preserving backfill; SQLite-safe migration runner.
- Reason-coded discovery (`IGNORED_PATTERN`, `BINARY_FILE`, `FILE_TOO_LARGE`, `UNSUPPORTED_ENCODING`, `OUTSIDE_REPOSITORY`,
  `SYMLINK_REJECTED`, `SECRET_LIKE_PATH`, `UNSUPPORTED_FILE_TYPE`, …), time budget, bounded diagnostics.
- Tree-sitter symbol and import extraction for Python, JavaScript, JSX, TypeScript, TSX; honest `parsed/partial/failed/unavailable/unsupported` status.
- Import resolution, `IMPORTS_FILE` / `DEPENDS_ON_PACKAGE` / `EXTENDS_SYMBOL` relationships; incremental, atomic generation publish; `completed_with_warnings`.
- API: snapshots, diagnostics, freshness, file detail/imports/dependencies/dependents, symbols, symbol search, capabilities.
- UI: symbol search, file detail page, freshness notice, analysis coverage, diagnostics, full re-index.
- `scripts/measure_indexing.py`; architecture, data-model and threat-model documents.

### Changed
- Skip reasons are now stable upper-case codes (previously lower-case keys); `TSX` and `JSX` are reported as their own languages.
- Index status may be `completed_with_warnings`; clients must treat it as a published index.
- New runtime dependencies: `tree-sitter`, `tree-sitter-python`, `tree-sitter-javascript`, `tree-sitter-typescript` (pinned).

## [0.1.0] - 2026-10-18 - Phase 01: Foundation and repository intelligence

### Added
- FastAPI backend with SQLAlchemy 2, Alembic migration `0001`, structured logging and a consistent error envelope.
- Safe repository registration (canonical paths, optional registration allow-list) and bounded indexing
  (never follows symlinks, honours `.gitignore`, skips credentials/binaries/dependency folders, enforces file-count,
  depth, per-file and total-size limits).
- Controlled git inspection (argument arrays, no shell, hardened config, timeout, output cap).
- Engineering tasks with an editable deterministic baseline draft plan (no AI, no execution).
- Audit events, optional bearer-token protection, role check on write endpoints.
- React + TypeScript (strict) + Vite + Tailwind frontend with 10 connected screens, typed API client.
- Backend (pytest) and frontend (vitest) automated tests; project documentation.

### Known gaps
See `docs/implementation/PROJECT_STATUS.md`.
