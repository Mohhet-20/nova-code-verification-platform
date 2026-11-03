# Deployment audit (Phase 19, 2026-10-22)

Derived from the repository as found on 2026-10-22 and from what was executed in this session; status documents were not taken on trust.
Environment: Windows 11 (the target machine, `F:\last proj`) with Python 3.11.9 and Node 24.18.0, plus WSL2 Ubuntu (Linux 6.6, Python 3.12.3, 22 CPUs,
15 GiB RAM). **No container engine** on either (Docker absent; installing one needs an administrator password). The folder is **not a Git repository**.

## Baseline before any change

| Check | Command | Result |
|---|---|---|
| Backend tests, Linux, pristine copy | `pytest` (Python 3.12.3) | **840 passed, 2 skipped** (26 min) - matches the Phase 18 claim |
| Backend tests, Windows (never run before) | `pytest` (Python 3.11.9) | **63 failed**, 777 passed, 2 skipped - causes below |
| Backend lint / deps | `ruff check .`, `pip check` | clean |
| Frontend clean install | `npm ci` then `npm run build` | **build FAILED**: `TS2688 Cannot find type definition file for 'node'` |
| Frontend tests | `vitest run` | **3 failed** (timing), 196 passed, 1 skipped |
| Existing `backend/data/nova.db` | read-only inspection of a copy | empty, never-migrated database (no tables) with a hot journal; no user data. Original untouched (SHA-256 `ddacef2b...ec10` before and after) |

## Application inventory

| Area | Found |
|---|---|
| Frontend | React 19.3, Vite 8.3, React Router 7.18 (42 lazily loaded routes), TanStack Query 5, Tailwind 4, TypeScript 5.9. Build: `npm run build` (`tsc -b && vite build`) -> `frontend/dist`. API base: `VITE_API_BASE_URL` (empty = same origin `/api/v1`). Auth: bearer token typed in Settings, stored in `sessionStorage`. 17 Vitest files (jsdom). |
| Backend | FastAPI 0.143 / uvicorn 0.54 / SQLAlchemy 2.1 / Alembic 1.20 / Pydantic 2.14. 18 router modules, **307 API operations on 270 paths** (174 GET, 114 POST, 14 PUT, 5 PATCH). Middleware: request-size limit, observability (request id, trace, structured log, metrics), CORS. Entry point `app.main:create_app` (factory). |
| Background work | In-process FastAPI background tasks with admission limits (indexing 4, analysis 2, test runs 1, reviews 3 per repository) and time budgets; interrupted runs are marked failed at startup. Optional in-process loops: integration worker, periodic alert evaluation (both off by default). No queue broker. |
| Database | SQLite via SQLAlchemy; 16 Alembic migrations (head `0016`); `foreign_keys=ON`, `busy_timeout=5000`; append-only audit tables enforced by triggers. Release CLI `python -m app.release` (check-config, deploy, migrate, backup, verify, restore, prune, integrity, smoke, manifest, rollback-plan). |
| "AI/ML" components | No model files, no GPU, no inference service. Code intelligence is deterministic: tree-sitter grammars (native wheels for Python/JS/TS) and stdlib `ast`. `NOVA_AI_PROVIDER` accepts only `none`. |
| File storage | `NOVA_ARTIFACT_DIR` (generated tests, run outputs, staged candidates). Registered repositories are read in place from local disk; approved repairs/refactorings write to them. |
| Auth / authz | Global bearer token (required in production) + hashed per-user tokens; roles, teams, organizations; deny-by-default; 404 for out-of-scope objects; route-wide auth and isolation sweeps in the test suite. |
| External integrations | GitHub (status/checks, webhooks), GitHub Issues, live OSV (opt-in), outbound signed webhooks, Slack-compatible notifications. All behind an SSRF guard; none verified against a real provider (mocked transports only). |
| Tests | Backend 34 test modules (842 tests); frontend 200 tests; release-ops tests start a real uvicorn process. |
| Deployment config | `backend/Dockerfile`, `frontend/Dockerfile` + `nginx.conf`, `deployment/docker-compose.yml`, `.github/workflows/{ci,release}.yml`, 8 operations documents. All container/CI files were marked "never executed". |

## Defects found (all fixed in this release unless stated)

| # | Severity | Defect | Evidence | Fix |
|---|---|---|---|---|
| D-01 | **Critical (deploy)** | Compose `migrate` exits 1 after migrating: the release CLI wrote its history into the read-only code tree (`/app/data/release-state`), so `api` (which waits for `migrate` to succeed) could never start | reproduced in the read-only rehearsal layout: `PermissionError: '/app/data'`, exit 1 | image sets `NOVA_RELEASE_STATE_DIR`/`NOVA_BACKUP_DIR` on `/data`; history-write failures are a warning, not a failed migration |
| D-02 | **Critical (deploy)** | nginx proxied to `http://backend:8000` but the compose service is `api`: nginx would fail to start ("host not found") | file inspection | upstream `api:8000`, resolved per request via Docker DNS |
| D-03 | **High (security)** | Repository test code (run under the API's own uid) could read `NOVA_API_TOKEN` and `NOVA_INTEGRATION_SECRET_KEY` from `/proc/<api-pid>/environ` despite the stripped child environment | probe printed `LEAKED`; negative control under an unhardened parent leaks, NOVA-hardened parent blocks | API process made non-dumpable at startup (`/proc/<pid>/environ` becomes root-owned); hostile test in the rehearsal confirms; `NOVA_TEST_EXECUTION_ENABLED` kill switch added |
| D-04 | **High (correctness/scope)** | Git discovery searched above the registered root: a non-repository directory inside another repository made reviews/CI read the **outer** repository | 4 Windows test failures (the user's home directory is a Git repository) | `GIT_CEILING_DIRECTORIES` on every git call + regression test |
| D-05 | **High (deploy)** | Production template placeholder `<generate: 48+ random characters>` passed the weak-token check, so a forgotten value would start in production | `weak_secret()` returned `None` | placeholder markers rejected + test |
| D-06 | High (deploy) | Frontend production build fails on a clean `npm ci` (`@types/node` in the lockfile root but missing from `package.json`) | `TS2688` | `@types/node@22.20.5` added to `devDependencies` (matches the lockfile) |
| D-07 | High (feature) | Runtime image has no `git` (code review / CI change detection return 409) and no `pytest` (every test-execution feature fails) | Dockerfile + `requirements.txt` inspection; runtime-only venv rehearsal | `git` installed in the image; `pytest` moved to runtime requirements |
| D-08 | Medium (data) | Backups dropped empty artifact directories; after any refused/empty test run every restore reported `ok: false` | restore drill: `missing_count: 1` | `artifact_dirs` recorded in the manifest, recreated and verified + test |
| D-09 | Medium (security) | No throttling of invalid bearer tokens | code inspection | per-client throttle (20 failures / 300 s -> 429 + `Retry-After`), forwarded-address aware |
| D-10 | Medium (UX/security) | Phone-width layout overflowed by 259 px on every page; UI text claimed "Nothing is modified or executed by this version" and listed implemented features as "planned" | real-browser run (Chrome 155) | nav wraps; copy states what can run or change and when |
| D-11 | Medium | API responses had no security headers; CORS omitted `PATCH` (cross-origin finding suppression would fail) | inspection | `nosniff`, `DENY`, `no-referrer`, `CSP default-src 'none'`, `no-store` on `/api`; `PATCH` allowed |
| D-12 | Medium | Compose did not mount any repository directory, so nothing could be registered in the container | inspection | `NOVA_REPOS_HOST_DIR` -> `/srv/repos` (required variable) |
| D-13 | Low | `app.release integrity` crashed with a traceback on an unmigrated database | traceback on the copy of the existing empty database | reports `tables_missing`, exit 1 + test |
| D-14 | Low (test) | 3 frontend tests flaky under load (1 s async timeout) | failed in full suite, passed alone | 5 s `asyncUtilTimeout` |
| D-15 | Low (test) | Windows: fixtures written with CRLF are refused by the LF-only patch engine (documented limitation); two tests are POSIX-only | Windows run | fixtures forced to LF on Windows for test modules only; two POSIX-only checks skipped on Windows |
| D-16 | Low (CI) | The CI "production guard" check could never fail (`! cmd` does not trip `set -e`, and `create_app()` does not run the guard) | inspection | real guard invocation with explicit failure |
| D-17 | Low (ops) | Every readiness probe logged two alembic INFO lines | rehearsal log | alembic migration logger at WARNING in the API process |

## Not changed (deliberately)

* SQLite stays the database: single-host deployment does not need PostgreSQL, and the migrations/audit triggers/backup tooling are SQLite-specific.
* No queue broker, no monitoring stack: in-process jobs with admission limits are adequate for one host; observability is documented as pull-based.
* The LF-only patch engine was not extended to CRLF (risky; refusal is explicit and documented).
* Release artifacts remain unsigned.
