# Final deployment report - NOVA 0.19.0 (Phase 19, 2026-10-22)

**Decision: NOT DEPLOYMENT READY** - one critical verification could not be performed: the Docker images and compose stack (the intended production
method) have never been built or started, because no container engine was available and installing one needs an administrator. Everything else that
can be verified without a container engine was run and passes. The path to a positive decision is short and listed in section E.

## Addendum - portfolio pass (later on 2026-10-22)

Work added after the first version of this report; the release decision is unchanged (the container gate is still blocked).

| Change | Evidence |
|---|---|
| AI-assisted repair proposals with Claude (opt-in), migration `0017` | 13 backend tests (fake client) + 3 frontend tests; migration rebuild diffed (only the CHECK constraint changed; FKs and indexes kept); guarded downgrade tested; `alembic check` clean; contract unchanged. **No real Claude call** (no credentials) - L-11 |
| Git repository, initial commit `7fa5a8a` | manifest now reports `source: git` and the revision |
| Graph loads no longer collapse under concurrency | `/metrics` at c8: 9.8 -> 74 req/s, p50 893 -> 95 ms; equivalence test; investigation in `docs/architecture/PERFORMANCE.md` |
| Indexing time budget on Windows used a 15.6 ms clock | found by one Windows-only test failure under load; `perf_counter`; failing test passes 3/3 |
| README rewritten with real screenshots; stale "never uses AI / local-first" statements corrected | `README.md`, `docs/architecture/OVERVIEW.md`, `REPAIR.md` |
| Final suites | Linux / Python 3.12: **869 passed, 2 skipped** (871); Windows / Python 3.11: **867 passed, 4 skipped**, 0 failed (skips: 2 slow benchmarks, 1 POSIX file-name test, 1 Linux-only /proc test); frontend: **203 passed, 1 skipped**, build OK |

Environment: Windows 11 (`F:\last proj`, Python 3.11.9, Node 24.18.0, npm 11.16.0) and WSL2 Ubuntu (Linux 6.6.87, Python 3.12.3, SQLite 3.45.1,
22 CPUs, 15 GiB RAM). Browsers: Chrome 155.0.8059.85, Edge 155.0.4283.62. Not a Git checkout (no revision). No Docker / Podman.

## A. Implementation status

**Found:** FastAPI modular monolith (307 API operations), SQLite with 16 Alembic migrations, in-process bounded background jobs, React/Vite SPA, a
release CLI (`python -m app.release`), Dockerfiles, compose file, CI workflows and 8 operations documents - with the container/CI parts never executed.
Full inventory: `DEPLOYMENT_AUDIT.md`.

**Changed (all in this release):**

| Area | Change |
|---|---|
| Container deployment | Fixed two stack-stopping defects (migrate crash on read-only code tree; nginx upstream `backend` vs service `api`). Image: `git` added, data-volume paths for backups/history, explicit single worker, proxy headers. Compose: repository bind mount, proxy-header trust, log rotation, merge-key hardening, both API services buildable. Optional Caddy TLS overlay. All base images pinned by tag **and digest**; nginx moved off the unmaintained 1.27 line. |
| Security | API process made non-dumpable (test code can no longer read API secrets from `/proc`); `NOVA_TEST_EXECUTION_ENABLED` kill switch; per-client invalid-token throttling; security headers on all API responses; git confined to the registered root; template placeholders rejected as tokens; CORS `PATCH`. |
| Data safety | Backups keep empty artifact directories (restores no longer fail after a refused test run); `integrity` reports instead of crashing on an unmigrated database; deployment-history write failures no longer fail a completed migration. |
| Frontend | Clean-install build fixed (`@types/node`); phone-width overflow fixed; 401 guidance with a link to Settings; inaccurate "nothing is executed or modified" copy corrected; flaky tests fixed. |
| Tests | 14 new backend tests (throttle, headers, CORS, kill switch, `/proc` hardening, git ceiling, placeholder token, empty-dir backup, unmigrated integrity, CLI defaults); 1 new frontend test; Windows portability of the suite. |
| Operability | `scripts/deployment_smoke.py` (39 checks incl. write workflow, git review, test execution, web proxy); `frontend/e2e/browser_smoke.mjs` (real Chrome/Edge); readiness probe 33 ms -> 1.4 ms; quieter readiness logs. |
| CI | Actions pinned to SHAs; the always-passing guard check replaced by a real one; new `containers` job boots the compose stack, runs the smoke test through the web proxy, and does backup + verify + isolated restore inside the container. |
| Docs | 11 operations documents (5 renamed and extended, 6 new); every one of the 125 settings documented, generated from the code. |

**Known limitations / unfinished:** `DEPLOYMENT_BLOCKERS.md` (B-01..B-03 open; L-01..L-10 accepted limitations needing owner sign-off). Phase 15/16 UI gaps
remain (API-only functions). Heavy analytical endpoints are CPU-bound in one process (section B, performance).

## B. Verification evidence

| # | Check | Command (working directory) | Status | Result |
|---|---|---|---|---|
| 1 | Backend deps + lint | `pip check`, `ruff check .` (`backend/`) | **pass** | no broken requirements; all checks passed (Windows and Linux) |
| 2 | Backend tests, Linux | `pytest -o addopts= -p no:cacheprovider -q` (`backend/`, Python 3.12.3) | **pass** | **854 passed, 2 skipped**, 0 failed (17 min); later-changed files re-run: 185 passed, then 65 passed |
| 3 | Backend tests, Windows | same (`backend/`, Python 3.11.9) | **pass** | **852 passed, 4 skipped**, 0 failed (24 min) - first passing Windows run (baseline: 63 failed); later-changed files re-run: 48 passed + 1 skipped, 46 passed |
| 4 | Frontend install | `npm ci` (`frontend/`) | **pass** | lockfile consistent (`npm ls --all` ok) |
| 5 | Frontend static checks | `npx tsc -b`, `npx eslint src` | **pass** | clean |
| 6 | Frontend tests | `npx vitest run` | **pass** | **200 passed, 1 skipped** |
| 7 | Production build | `npm run build` | **pass** | 45 assets; JS 760 KB raw / 239 KB gzip; entry chunk 255.96 KB / 78.97 KB gzip |
| 8 | Migrations | fresh DB: `python -m app.release migrate --no-backup`; `alembic check` | **pass** | head `0016`; "No new upgrade operations detected" |
| 9 | Upgrade of existing data | copy of the user's `backend/data/nova.db` -> `migrate --backup-dir ...` | **pass** | the existing file is an empty, never-migrated DB; copy upgraded to `0016`; original unchanged |
| 10 | API contract | `python -m app.contracts` | **pass** | contract OK |
| 11 | Production config guard | `check-config`; `test_release_ops` | **pass** | template placeholder now rejected; 37 release-ops tests pass |
| 12 | **Container image builds** | `docker compose ... build` | **BLOCKED** | no container engine (B-01) |
| 13 | **Container start + health** | `docker compose ... up -d` | **BLOCKED** | same; replaced by the rehearsal below (not equivalent to a real container) |
| 14 | Image-equivalent rehearsal | WSL: code tree copied like the Dockerfile and made **read-only**, separate `/data`-like dir, runtime-only venv from `requirements.txt`, production env, exact image `CMD` | **pass** | original compose flow reproduced the D-01 crash (exit 1); fixed flow: check-config 0 errors, deploy -> `0016`, integrity ok, idempotent re-deploy, ready in 3.4-7.2 s, `process_hardening: nondumpable`, `/proc/<pid>/environ` root-owned |
| 15 | Deployment smoke, direct | `python scripts/deployment_smoke.py --url http://127.0.0.1:18000 --write --repo-path <sample>` | **pass** | **32/32** (auth negative cases, 404/422/413, no docs, index -> symbols -> analysis -> findings, git review, test execution refused explicitly, task write/read) |
| 16 | Deployment smoke via nginx | same with `--url/--web-url http://127.0.0.1:18080` through official nginx 1.30.6 serving `frontend/dist` with `frontend/nginx.conf` (only root/upstream/resolver/port substituted) | **pass** | **39/39** incl. CSP, SPA fallback, missing asset 404, 1-year asset caching, `/api` proxy |
| 17 | nginx config syntax | `nginx -t` on the **unmodified** `frontend/nginx.conf` (nginx 1.30.6) | **pass** | syntax ok, test successful |
| 18 | Test execution enabled | rehearsal with `NOVA_TEST_EXECUTION_ENABLED=true` + hostile test reading ancestors' `/proc/*/environ` | **pass** | 2 collected, 2 passed (probe found no readable secret); negative control without hardening: probe finds the token and fails |
| 19 | Auth throttling | 21 invalid tokens from one forwarded address, direct and through nginx | **pass** | 20x401 then 429 `Retry-After: 300`; valid token from same address 429; other addresses 200 |
| 20 | Backup / restore drill | `backup` (API running) -> `verify` -> tampered copy -> `restore --target <new>` -> second API on restored data | **pass** | backup 0.49 s, verify ok, tamper rejected, restore 0.46 s `ok: true` (0 missing refs), refuses existing target, restored API 18/18 smoke, identical counts |
| 21 | Graceful shutdown | SIGTERM to the API | **pass** | stopped in 0.25-0.26 s, "Application shutdown complete" |
| 22 | Real browser | `node frontend/e2e/browser_smoke.mjs http://127.0.0.1:18080 chrome|msedge` | **pass** | **15/15 in Chrome 155 and Edge 155** (401 guidance, token entry, real data pages, reload, backend-down error, 390 px no overflow, a11y basics, no unexpected console errors). Earlier run found the 259 px overflow (fixed) |
| 23 | Dependency scan, Python | `pip-audit -r backend/requirements.txt --no-deps --disable-pip` and `pip-audit --path <runtime site-packages>` (pip-audit 2.10.1) | **pass** | no known vulnerabilities (pins and all resolved transitive packages) |
| 24 | Dependency scan, npm | `npm audit --audit-level=high` (npm 11.16.0) | **pass** | 0 vulnerabilities |
| 25 | Secret scan | `python scripts/scan_secrets.py` | **pass** | 0 findings (one false positive in a new file was restructured, not allow-listed) |
| 26 | SBOM / manifest | `python -m app.release manifest --out ... --sbom ...` | **pass** | CycloneDX 1.5, 311 components; 15/16 migrations additive (`0002` needs review); unsigned |
| 27 | CI workflows | YAML parsed (PyYAML) | **partial** | all parse; **never executed** (B-02) |
| 28 | Image / container scanning, TLS overlay, cloud deploy | - | **not run** | no engine / no scanner / no authorization |
| 29 | Diff review | `diff -rq` against the pristine copy taken at start + line-ending scan | **pass** | only intended files changed; CRLF churn introduced by my own edits was found and reverted; no secrets, no `production.env`, no data files |

**Performance sanity (rehearsal API, WSL2, 22 CPUs, single API process; dataset = NOVA's own backend: 278 files, 53,888 lines):** full index 3.25 s,
unchanged incremental re-index 2.65 s (known limit F-003: not much faster), full static analysis 9.71 s (277 files, 655 findings); API RSS 192 MB idle,
240 MB after jobs, 276 MB after the load test. Latency p50/p95 at concurrency 1: health 0.7/1.2 ms, overview 4.3/6.4 ms, repositories 4.1/6.4 ms,
symbols 13.5/17.3 ms, search 14.5/18.9 ms, findings 6.9/9.6 ms, metrics 15.4/19.7 ms, ready 1.4/2.2 ms (after the fix; 32.9/39.5 ms before).
At concurrency 8: light endpoints 80-110 ms p50, symbols 304 ms, findings 580 ms, **metrics 1.95 s** (throughput ~4 req/s vs ~65 req/s serial: the
cycle analysis is recomputed in Python per request and CPU-bound requests contend in one process). This is a laptop measurement, not a capacity claim;
it indicates the deployment suits a small team, and that `/metrics` is the first endpoint to cache if usage grows.

## C. Deployment instructions

The complete, ordered procedure is `DEPLOYMENT_GUIDE.md`; the essential commands (repository root on a Linux host with Docker Engine + Compose v2.20+):

```bash
# configure
cp deployment/production.env.example deployment/production.env && chmod 600 deployment/production.env
python3 -c "import secrets; print(secrets.token_urlsafe(48))"                              # NOVA_API_TOKEN
python3 -c "import base64, os; print(base64.urlsafe_b64encode(os.urandom(32)).decode())"   # NOVA_INTEGRATION_SECRET_KEY
sudo mkdir -p /srv/nova/repos && sudo chown -R 10001:10001 /srv/nova/repos                 # NOVA_REPOS_HOST_DIR
COMPOSE="docker compose -f deployment/docker-compose.yml --env-file deployment/production.env"
# build and validate
$COMPOSE config -q && $COMPOSE build && $COMPOSE run --rm --no-deps migrate python -m app.release check-config
# migrate + start (migrate is a one-shot release step that runs first)
$COMPOSE up -d && $COMPOSE ps -a && $COMPOSE logs migrate
# health and readiness
curl -fsS http://127.0.0.1:8080/api/v1/health && curl -fsS http://127.0.0.1:8080/api/v1/ready
# smoke tests
export NOVA_API_TOKEN="$(grep '^NOVA_API_TOKEN=' deployment/production.env | cut -d= -f2-)"
python3 scripts/deployment_smoke.py --url http://127.0.0.1:8080 --web-url http://127.0.0.1:8080
# logs and troubleshooting
$COMPOSE logs -f api ; $COMPOSE exec api python -m app.release status ; $COMPOSE exec api python -m app.release integrity
# stop safely (data kept); never use `down -v` on a real deployment
$COMPOSE stop -t 30
```

## D. Live deployment status

| Stage | Status |
|---|---|
| Local application verified | **Yes** - production-mode API in an image-equivalent layout (WSL2 Linux), real nginx 1.30.6 in front, real Chrome/Edge, full smoke and restore drill |
| Production container configuration verified | **No** - Dockerfiles and compose were corrected and statically checked (YAML, `nginx -t`, digests resolved) but never built or run |
| Deployment platform configuration prepared | **Yes** - single Linux VM with Docker Compose (+ optional Caddy TLS overlay); documented, untested |
| Cloud deployment completed and externally verified | **No** - not authorized, no host provided |

## E. Release decision: **NOT DEPLOYMENT READY**

| Blocking issue | Severity | Evidence | Exact remediation |
|---|---|---|---|
| B-01 Container images/compose never built or started | Critical | `docker: command not found` (Windows, WSL); rootless impossible (`newuidmap` missing) | In WSL: `sudo apt-get install -y docker.io docker-compose-v2 && sudo usermod -aG docker $USER`, new shell, then `DEPLOYMENT_GUIDE.md` sections 3-7 with `--write` and the container restore commands in `BACKUP_AND_RESTORE.md`; or push to GitHub and let the `containers` job run |
| B-02 No version control; CI never executed | High | `fatal: not a git repository` | `git init`, commit, push to a private repository; review the first CI run (all four jobs are required gates) |
| B-03 No staging/production deployment | High | - | Staging VM per `DEPLOYMENT_GUIDE.md`, then `RELEASE_CHECKLIST.md` |

When B-01 and B-02 pass, the evidence supports **DEPLOYMENT READY WITH LIMITATIONS** for a single-host deployment, subject to owner sign-off on
L-01..L-10 in `DEPLOYMENT_BLOCKERS.md`.

**What is needed from you:** (1) an administrator password to install Docker in WSL (or Docker Desktop), *or* authorization to create a Git repository
and push to GitHub so CI can build the images; (2) a staging host when you want the first real deployment.
