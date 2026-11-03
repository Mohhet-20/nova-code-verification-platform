# Release readiness report (Phase 18)

> **Historical (Phase 18, 2026-10-21).** Superseded by [FINAL_DEPLOYMENT_REPORT.md](FINAL_DEPLOYMENT_REPORT.md) (Phase 19, 2026-10-22), which re-verified and fixed several items listed here.

Assessment date 2026-10-21. Environment: Linux 6.8, Python 3.12.14, SQLite 3.53.1, Node 22.23.2, 2 CPUs; **no Docker, no PostgreSQL, no real browser, not a git checkout**.
Legend: **Verified** executed evidence · **Failed** · **Blocked** (environment) · **Not implemented** · **N/A**.

| # | Gate | Outcome | Evidence / reason |
|---|---|---|---|
| 1 | Application builds | **Verified** for backend (deps install, imports, runs) and frontend (`tsc`, `vite build`); **container images Blocked** | no Docker |
| 2 | Critical unit and integration tests pass | **Verified** | 840 passed, 2 skipped, 0 failed; frontend 199 passed, 1 skipped |
| 3 | Migrations succeed in isolation | **Verified** (SQLite) | fresh, upgrade from 0015, failed-migration and lock tests; `alembic check` |
| 4 | Authentication and authorization tests pass | **Verified** | per-feature tests + route-wide sweeps over 307 operations |
| 5 | Tenant isolation | **Verified** | `cross_tenant_isolation`; sweep over every repository-scoped operation |
| 6 | Core repository-analysis workflows | **Verified** | workflows 1–2 |
| 7 | Test execution / verification evidence trustworthy | **Verified** (restricted subprocess) | `test_testing`, `test_verification` |
| 8 | Repair operations bounded and safe | **Verified** | `test_repair` |
| 9 | Governance invariants | **Verified** | `test_governance`, E2E |
| 10 | Integration/webhook controls | **Verified with mocked providers** | no live provider |
| 11 | No unresolved Critical/High findings | **Verified within reviewed scope** | none found; Medium findings remain open (see register) |
| 12 | Deployment configuration validated | **Verified** | production contract, startup refusal, `check-config` |
| 13 | Backup and isolated restore | **Verified locally** (synthetic and governed data sets); **off-host / separate-hardware DR drill not performed** | `test_release_ops`, E2E |
| 14 | Health checks and smoke tests | **Verified** against a real local server process; container healthchecks Blocked | `smoke` |
| 15 | Release artifacts traceable to a source revision | **Failed / Blocked** | tree is not a git checkout; manifests record revision `unknown`; artifacts unsigned |
| 16 | Documentation and runbooks | **Verified (exist)**; drills not performed | `docs/operations/*` |

Additional unverified areas: PostgreSQL (not claimed), multi-process/replica operation, TLS, real GitHub/issue-tracker accounts, a real browser and accessibility, Windows, a real production deployment, GitHub workflow execution.

## Decision

**NOT RELEASE READY** (for production use). Reasons: gate 15 fails (no source revision, unsigned artifacts), gate 1 container build is blocked, the deployment path (containers, compose, workflows) has never run, DR was not demonstrated on separate hardware, the UI has not been exercised in a real browser and has known screen gaps (Phases 15–16 are PARTIAL), and integrations have only been verified against mocks.
What *is* supported by evidence: single-user or small-team local use on SQLite with the documented limits, protected by a strong `NOVA_API_TOKEN`.

## What must change before reassessment
1. Put the code under version control (or supply a verified revision) and publish a manifest from CI so artifacts trace to a commit.
2. Build both images, run the compose stack, run `smoke` against it and execute the CI workflows once on a real runner; record results.
3. Run a restore drill on separate hardware from an off-host backup.
4. Real-browser end-to-end and accessibility pass; finish the Phase 15/16 UI screens.
5. Exercise one real provider (GitHub) in a staging account; decide on PostgreSQL scope (support it and test it, or state SQLite-only).
6. Close or formally accept F-003, F-005, F-007, F-011, F-012 with a named owner.
