# Production configuration

All application settings are environment variables with the `NOVA_` prefix, read by `backend/app/core/config.py` (pydantic-settings; a `backend/.env`
file is also read in local development). Unset = the default below. In containers they come from `deployment/production.env` (git-ignored,
`chmod 600`) or your platform's secret manager; nothing secret is baked into images, compose files or the frontend bundle.

`python -m app.release check-config` prints findings as codes and messages - never values - and exits 1 on production errors. In production the API
also runs these checks at startup and **refuses to start** on any error.

## Environments

| `NOVA_ENVIRONMENT` | Behaviour |
|---|---|
| `development` (default) | No token required (local trust), Swagger at `/docs`, findings are warnings only. Local single-user use. |
| `test` | Same as development; used by the test suite. |
| `production` | Contract below enforced at startup; `/docs` and `/openapi.json` disabled; schema must be at the migration head. |

## Production contract

| Rule | Finding code | Level |
|---|---|---|
| `NOVA_API_TOKEN` set, >= 32 characters, enough variety, no placeholder text (`replace`, `changeme`, `example`, `password`, `token`, `generate`, `random`, `<`, `>`, ...) | `auth_disabled`, `api_token_weak` | **error - API will not start** |
| `NOVA_CORS_ORIGINS` contains no `*` | `cors_wildcard` | error |
| `NOVA_INTEGRATION_ALLOW_PRIVATE_DESTINATIONS=false` (SSRF guard on) | `ssrf_guard_relaxed` | error |
| `NOVA_OPS_METRICS_PUBLIC=false` | `metrics_public` | error |
| `NOVA_INTEGRATION_SECRET_KEY`, if set, is urlsafe-base64 of exactly 32 bytes | `integration_key_invalid` | error |
| `NOVA_ENVIRONMENT` is one of the three values | `environment_unknown` | error |
| Database schema at the migration head | - | startup refusal (run the `migrate` release step) |
| Localhost CORS origin; JSON logs off; artifact dir unset; repository registration unrestricted; integration key missing | `cors_local_origin`, `log_not_json`, `artifact_dir_default`, `registration_unrestricted`, `integration_key_missing` | warning |
| SQLite in production (single node, back it up) | `sqlite_in_production` | warning (expected) |
| Non-SQLite database URL | `database_backend_unverified` | **error** in production |
| Repository test execution enabled (not a sandbox) | `test_execution_unsandboxed` | warning (0.19.0) |
| Invalid-token throttling disabled | `auth_throttle_disabled` | warning (0.19.0) |
| `NOVA_AI_PROVIDER` is `none` or `anthropic` | `ai_provider_unknown` | error |
| AI-assisted proposals send repository excerpts to Anthropic | `ai_egress` | warning (when `anthropic`) |

## Required and recommended variables

| Variable | Required | Purpose | How to generate / choose |
|---|---|---|---|
| `NOVA_ENVIRONMENT` | yes | selects the contract | `production` |
| `NOVA_API_TOKEN` | yes | bearer token for every API call except health/readiness; users enter it in Settings | `python3 -c "import secrets; print(secrets.token_urlsafe(48))"` |
| `NOVA_CORS_ORIGINS` | yes | browser origins allowed to call the API cross-origin (comma separated). Same-origin use through `web` does not need CORS, but set your real origin | `https://nova.example.org` |
| `NOVA_REGISTRATION_BASE_DIRS` | yes (warning if empty) | only directories under these may be registered | `/srv/repos` in the container |
| `NOVA_INTEGRATION_SECRET_KEY` | recommended | AES-256-GCM key for stored integration credentials and webhook secrets; without it they cannot be stored (env-reference credentials still work) | `python3 -c "import base64, os; print(base64.urlsafe_b64encode(os.urandom(32)).decode())"`; keep a copy outside the backups |
| `NOVA_SOURCE_REVISION` | recommended | commit/tag recorded in backups, deployment history, release manifest and image label | `git rev-parse HEAD` |
| `NOVA_TEST_EXECUTION_ENABLED` | recommended `false` unless repositories are trusted | kill switch for every feature that runs repository code | `true` / `false` |
| `NOVA_AI_PROVIDER` + `ANTHROPIC_API_KEY` | optional | `anthropic` enables AI-assisted repair proposals (Claude); the key is read by the Anthropic SDK, never by NOVA settings. See `docs/architecture/AI_ASSISTED_REPAIR.md` | key from the Anthropic Console |
| `NOVA_DATABASE_URL`, `NOVA_ARTIFACT_DIR` | set by the image | `/data/nova.db`, `/data/artifacts` | keep the image defaults |

### Variables read outside the settings model

| Variable | Read by | Purpose |
|---|---|---|
| `NOVA_BACKUP_DIR` | `python -m app.release` | default backup directory (image: `/data/backups`; local: `backend/data/backups`) |
| `NOVA_RELEASE_STATE_DIR` | `python -m app.release` | where `deployments.jsonl` is written (image: `/data/release-state`) |
| `NOVA_SOURCE_REVISION` | backups, manifest, deployment history | see above |
| `NOVA_CRED_*` | integrations | credential references of the form `env:NOVA_CRED_<NAME>` (no encryption key needed) |
| `FORWARDED_ALLOW_IPS` | uvicorn | peers whose `X-Forwarded-For` is trusted; compose sets `*` because the API port is internal (`ARCHITECTURE_AND_NETWORKING.md`) |
| `NOVA_REPOS_HOST_DIR`, `NOVA_WEB_BIND`, `NOVA_WEB_PORT`, `NOVA_IMAGE_TAG`, `NOVA_DOMAIN` | compose interpolation only | host repository directory, web port binding, image tag, TLS domain |
| `VITE_API_BASE_URL` | frontend build argument | compiled into public JavaScript; empty = same origin (recommended). Never put a secret in any `VITE_*` value |
| `NOVA_BACKEND_URL` | Vite dev/preview server only | where the dev proxy sends `/api` |

## Security-relevant behaviour

* **Sessions/tokens:** there are no cookies or server sessions. The browser keeps the token in `sessionStorage` for one tab and sends it as
  `Authorization: Bearer`. The global token has no expiry; rotate it (below). Per-user tokens (`nova_u_...`) are stored hashed and can be revoked or
  rotated through the governance API. Because no cookie is used, CSRF does not apply to the API.
* **Throttling:** after `NOVA_AUTH_FAILURE_LIMIT` (20) invalid tokens from one client within `NOVA_AUTH_FAILURE_WINDOW_SECONDS` (300) that client gets 429
  with `Retry-After`, even with a valid token, until the window clears. Missing tokens are not counted. Per process, in memory.
* **Trusted hosts:** the API never builds URLs from the `Host` header; restrict host names at the TLS proxy (Caddy serves only `NOVA_DOMAIN`).
* **Debug:** there is no debug mode; production disables `/docs` and `/openapi.json`, and error responses never include stack traces (they carry a
  request id instead).
* **Logs:** JSON with `request_id`/`trace_id`; keys that look sensitive are replaced with `[REDACTED]`, values are pattern-redacted and length-bounded;
  request bodies, tokens and file contents are never logged; configuration findings name settings, never values.

## Secrets: provision, rotate, revoke

* **Provision:** generate on the host (commands above); put them in `deployment/production.env` (`chmod 600`) or the platform secret store. Never in git, images or `VITE_*`.
* **Rotate the API token:** set a new `NOVA_API_TOKEN`, `$COMPOSE up -d` (recreates `api`), update users/clients. The old token stops working immediately.
* **Rotate the encryption key:** new key in `NOVA_INTEGRATION_SECRET_KEY`, old key in `NOVA_INTEGRATION_SECRET_KEYS_PREVIOUS`, restart, run re-encryption
  (Integration Center / `reencrypt_all`), verify credentials, then remove the previous key. Never remove the old key before re-encryption completes.
* **Revoke after exposure:** see `INCIDENT_RESPONSE.md` (rotate first, then review audit events and webhook deliveries for the exposure window).

## Database

SQLite is the only verified database. `check-config` treats any other URL as an **error** in production: the migrations use SQLite batch operations, the
append-only audit tables rely on SQLite triggers, and backup/restore is SQLite-specific. Moving to PostgreSQL is not required for a single-host
deployment and would be a separate, tested project. SQLite settings applied on every connection: `foreign_keys=ON`, `busy_timeout=5000`.

## Complete reference (all 130 settings in `Settings`, generated from the code on 2026-10-22)

Booleans accept `true`/`false`; list values are comma separated. Repository configuration can never raise these limits.

#### Core

| Variable | Default | Constraint |
|---|---|---|
| `NOVA_ENVIRONMENT` | `development` | - |
| `NOVA_DATABASE_URL` | `sqlite:///<backend>/data/nova.db` | - |
| `NOVA_LOG_LEVEL` | `INFO` | - |
| `NOVA_LOG_JSON` | `True` | - |
| `NOVA_API_TOKEN` | (empty) | - |
| `NOVA_CORS_ORIGINS` | `http://localhost:5173,http://127.0.0.1:5173` | - |
| `NOVA_REGISTRATION_BASE_DIRS` | (empty) | - |
| `NOVA_REQUIRE_CURRENT_SCHEMA` | `False` | - |
| `NOVA_MAX_REQUEST_BODY_BYTES` | `8388608` | >= 65536 |
| `NOVA_DOCS_ENABLED` | `True` | - |
| `NOVA_AUTH_FAILURE_LIMIT` | `20` | >= 0 |
| `NOVA_AUTH_FAILURE_WINDOW_SECONDS` | `300` | >= 10 |
| `NOVA_GIT_TIMEOUT_SECONDS` | `10.0` | > 0 |
| `NOVA_GIT_MAX_OUTPUT_BYTES` | `1000000` | >= 1024 |

#### AI-assisted repair (opt-in)

| Variable | Default | Constraint |
|---|---|---|
| `NOVA_AI_PROVIDER` | `none` | - |
| `NOVA_AI_MODEL` | `claude-opus-5-5` | - |
| `NOVA_AI_EFFORT` | `high` | - |
| `NOVA_AI_TIMEOUT_SECONDS` | `180.0` | > 0, <= 900 |
| `NOVA_AI_MAX_CONTEXT_BYTES` | `120000` | >= 4000, <= 1000000 |
| `NOVA_AI_MAX_EDITS` | `20` | >= 1, <= 100 |

#### Indexing (Phase 02-03)

| Variable | Default | Constraint |
|---|---|---|
| `NOVA_INDEX_MAX_CONCURRENT_RUNS` | `4` | >= 1 |
| `NOVA_INDEX_MAX_FILES` | `20000` | >= 1 |
| `NOVA_INDEX_MAX_DEPTH` | `12` | >= 1 |
| `NOVA_INDEX_MAX_FILE_BYTES` | `1000000` | >= 1 |
| `NOVA_INDEX_MAX_TOTAL_BYTES` | `200000000` | >= 1 |
| `NOVA_INDEX_TIME_BUDGET_SECONDS` | `300.0` | > 0 |
| `NOVA_INDEX_MAX_SYMBOLS_PER_FILE` | `5000` | >= 1 |
| `NOVA_INDEX_MAX_DIAGNOSTICS` | `500` | >= 0 |
| `NOVA_INDEX_EXTRA_EXCLUDES` | (empty) | - |

#### Static analysis (Phase 04)

| Variable | Default | Constraint |
|---|---|---|
| `NOVA_ANALYSIS_MAX_FILES` | `5000` | >= 1 |
| `NOVA_ANALYSIS_MAX_FILE_BYTES` | `500000` | >= 1024 |
| `NOVA_ANALYSIS_TIME_BUDGET_SECONDS` | `120.0` | > 0 |
| `NOVA_ANALYSIS_MAX_FINDINGS_PER_FILE` | `200` | >= 1 |
| `NOVA_ANALYSIS_MAX_CONCURRENT_RUNS` | `2` | >= 1 |

#### Test engineering (Phase 05)

| Variable | Default | Constraint |
|---|---|---|
| `NOVA_ARTIFACT_DIR` | (empty) | - |
| `NOVA_TEST_PYTHON` | (empty) | - |
| `NOVA_TEST_MAX_CANDIDATE_BYTES` | `60000` | >= 1000 |
| `NOVA_TEST_MAX_CANDIDATES_PER_RUN` | `40` | >= 1 |
| `NOVA_TEST_MAX_FILES_DISCOVERED` | `5000` | >= 1 |
| `NOVA_TEST_MAX_FILE_BYTES` | `500000` | >= 1024 |
| `NOVA_TEST_TIMEOUT_SECONDS` | `120.0` | > 0 |
| `NOVA_TEST_MAX_OUTPUT_BYTES` | `100000` | >= 1024 |
| `NOVA_TEST_MEMORY_LIMIT_MB` | `1024` | >= 64 |
| `NOVA_TEST_MAX_CONCURRENT_RUNS` | `1` | >= 1 |
| `NOVA_TEST_EXECUTION_ENABLED` | `True` | - |

#### Debugging (Phase 06)

| Variable | Default | Constraint |
|---|---|---|
| `NOVA_DEBUG_MAX_REPORT_BYTES` | `65536` | - |
| `NOVA_DEBUG_MAX_FRAMES` | `60` | - |
| `NOVA_DEBUG_MAX_CANDIDATES` | `10` | - |
| `NOVA_DEBUG_IMPACT_DEPTH` | `2` | - |
| `NOVA_DEBUG_WEIGHTS_JSON` | (empty) | - |

#### Code review (Phase 07)

| Variable | Default | Constraint |
|---|---|---|
| `NOVA_REVIEW_MAX_FILES` | `200` | >= 1 |
| `NOVA_REVIEW_MAX_DIFF_BYTES` | `2000000` | >= 1024 |
| `NOVA_REVIEW_MAX_FILE_BYTES` | `500000` | >= 1024 |
| `NOVA_REVIEW_TIME_BUDGET_SECONDS` | `90.0` | > 0 |
| `NOVA_REVIEW_IMPACT_DEPTH` | `2` | >= 1, <= 4 |
| `NOVA_REVIEW_MAX_FINDINGS` | `100` | >= 1 |
| `NOVA_REVIEW_MAX_FINDINGS_PER_FILE` | `10` | >= 1 |
| `NOVA_REVIEW_MIN_CONFIDENCE` | `medium` | - |
| `NOVA_REVIEW_MAX_RECOMMENDATIONS` | `30` | >= 1 |

#### Repair (Phase 08)

| Variable | Default | Constraint |
|---|---|---|
| `NOVA_REPAIR_MAX_FILES` | `10` | >= 1, <= 50 |
| `NOVA_REPAIR_MAX_PATCH_BYTES` | `200000` | >= 1024 |
| `NOVA_REPAIR_MAX_FILE_BYTES` | `500000` | >= 1024 |
| `NOVA_REPAIR_OUTPUT_CHARS` | `4000` | >= 200 |
| `NOVA_REPAIR_MIN_ROOT_CAUSE_SCORE` | `2.0` | > 0 |
| `NOVA_REPAIR_ALLOWED_SUFFIXES` | `.py,.md,.txt,.toml,.cfg,.ini,.json,.yaml,.yml,.js,.jsx,.ts,.tsx,.html,.css,.rst` | - |

#### Refactoring (Phase 09)

| Variable | Default | Constraint |
|---|---|---|
| `NOVA_REFACTOR_MAX_FILES` | `60` | >= 1, <= 500 |
| `NOVA_REFACTOR_MAX_EDITS` | `3000` | >= 1 |
| `NOVA_REFACTOR_MAX_SCAN_FILES` | `5000` | >= 1 |
| `NOVA_REFACTOR_DUP_MIN_LINES` | `6` | >= 2 |
| `NOVA_REFACTOR_DUP_MIN_TOKENS` | `40` | >= 5 |
| `NOVA_REFACTOR_DUP_THRESHOLD` | `0.85` | > 0, <= 1 |
| `NOVA_REFACTOR_DUP_MAX_UNITS` | `4000` | >= 10 |
| `NOVA_REFACTOR_DUP_EXCLUDE` | `node_modules,vendor,dist,build,.venv,venv,site-packages,__pycache__,.tox,third_party,.git` | - |
| `NOVA_REFACTOR_OUTPUT_CHARS` | `4000` | >= 200 |

#### Continuous verification (Phase 10)

| Variable | Default | Constraint |
|---|---|---|
| `NOVA_CV_MAX_CHANGED_FILES` | `2000` | >= 1 |
| `NOVA_CV_MAX_FILE_BYTES` | `1000000` | >= 1000 |
| `NOVA_CV_HISTORY_LIMIT` | `200` | >= 1 |
| `NOVA_CV_FLAKY_LOOKBACK` | `10` | >= 2, <= 50 |
| `NOVA_CV_OUTPUT_CHARS` | `4000` | >= 200 |

#### CI/CD (Phase 11)

| Variable | Default | Constraint |
|---|---|---|
| `NOVA_CI_WEBHOOK_MAX_BYTES` | `1000000` | >= 1024 |
| `NOVA_CI_HTTP_TIMEOUT_SECONDS` | `10.0` | > 0 |
| `NOVA_CI_HTTP_MAX_RESPONSE_BYTES` | `1000000` | >= 1024 |
| `NOVA_CI_GITHUB_API_BASE` | `https://api.github.com` | - |
| `NOVA_CI_STATUS_MAX_ATTEMPTS` | `5` | >= 1, <= 20 |
| `NOVA_CI_RETRY_BASE_SECONDS` | `30` | >= 1 |
| `NOVA_CI_MAX_CHANGED_FILES` | `500` | >= 1 |
| `NOVA_CI_REPORT_MAX_BYTES` | `400000` | >= 10000 |

#### Security scanning (Phase 12)

| Variable | Default | Constraint |
|---|---|---|
| `NOVA_SECURITY_MAX_FILES` | `5000` | >= 1 |
| `NOVA_SECURITY_MAX_FILE_BYTES` | `1000000` | >= 1000 |
| `NOVA_SECURITY_MAX_MANIFEST_BYTES` | `5000000` | >= 1000 |
| `NOVA_SECURITY_MAX_COMPONENTS` | `20000` | >= 1 |
| `NOVA_SECURITY_MAX_FINDINGS` | `2000` | >= 1 |
| `NOVA_SECURITY_TIME_LIMIT_SECONDS` | `120.0` | > 0 |
| `NOVA_SECURITY_ADVISORY_DIR` | (empty) | - |
| `NOVA_SECURITY_OSV_ENABLED` | `False` | - |
| `NOVA_SECURITY_OSV_BASE` | `https://api.osv.dev` | - |
| `NOVA_SECURITY_HTTP_TIMEOUT_SECONDS` | `10.0` | > 0 |
| `NOVA_SECURITY_HTTP_MAX_RESPONSE_BYTES` | `2000000` | >= 1000 |
| `NOVA_SECURITY_REPORT_MAX_BYTES` | `2000000` | >= 10000 |

#### Observability (Phase 13)

| Variable | Default | Constraint |
|---|---|---|
| `NOVA_OPS_LOG_RING_SIZE` | `2000` | >= 100, <= 50000 |
| `NOVA_OPS_LOG_FIELD_MAX` | `1000` | >= 100, <= 10000 |
| `NOVA_OPS_TRACE_SAMPLE_RATIO` | `1.0` | >= 0.0, <= 1.0 |
| `NOVA_OPS_TRACE_RING_SIZE` | `2000` | >= 100, <= 50000 |
| `NOVA_OPS_METRICS_PUBLIC` | `False` | - |
| `NOVA_OPS_RETENTION_DAYS` | `90` | >= 1 |
| `NOVA_OPS_HEALTH_CACHE_SECONDS` | `10.0` | >= 0 |
| `NOVA_OPS_PROBE_TIMEOUT_SECONDS` | `2.0` | > 0 |
| `NOVA_OPS_PERIODIC_EVALUATION` | `False` | - |
| `NOVA_OPS_EVALUATION_INTERVAL_SECONDS` | `60.0` | >= 5 |
| `NOVA_OPS_NOTIFY_TIMEOUT_SECONDS` | `5.0` | > 0 |
| `NOVA_OPS_NOTIFY_MAX_ATTEMPTS` | `3` | >= 1, <= 6 |
| `NOVA_OPS_NOTIFY_ALLOW_PRIVATE` | `False` | - |
| `NOVA_OPS_WEBHOOK_URL` | (empty) | - |
| `NOVA_OPS_WEBHOOK_SECRET_ENV` | (empty) | - |
| `NOVA_OPS_SLACK_WEBHOOK_ENV` | (empty) | - |

#### Integrations and webhooks (Phase 16)

| Variable | Default | Constraint |
|---|---|---|
| `NOVA_INTEGRATION_SECRET_KEY` | (empty) | - |
| `NOVA_INTEGRATION_SECRET_KEYS_PREVIOUS` | (empty) | - |
| `NOVA_INTEGRATION_ALLOW_PRIVATE_DESTINATIONS` | `False` | - |
| `NOVA_INTEGRATION_INBOUND_MAX_BYTES` | `262144` | >= 1024 |
| `NOVA_INTEGRATION_REPLAY_WINDOW_SECONDS` | `300` | >= 30 |
| `NOVA_INTEGRATION_HTTP_TIMEOUT_SECONDS` | `8.0` | > 0, <= 30 |
| `NOVA_INTEGRATION_HTTP_MAX_RESPONSE_BYTES` | `262144` | >= 1024 |
| `NOVA_WEBHOOK_MAX_ATTEMPTS` | `6` | >= 1, <= 12 |
| `NOVA_WEBHOOK_BACKOFF_BASE_SECONDS` | `30.0` | >= 0 |
| `NOVA_WEBHOOK_BACKOFF_MAX_SECONDS` | `3600.0` | >= 0 |
| `NOVA_WEBHOOK_DISABLE_AFTER_FAILURES` | `20` | >= 1 |
| `NOVA_WEBHOOK_LEASE_SECONDS` | `120` | >= 10 |
| `NOVA_INTEGRATION_WORKER_ENABLED` | `False` | - |
| `NOVA_INTEGRATION_WORKER_INTERVAL_SECONDS` | `5.0` | >= 1 |
| `NOVA_INTEGRATION_EVENT_RETENTION_DAYS` | `30` | >= 1 |
