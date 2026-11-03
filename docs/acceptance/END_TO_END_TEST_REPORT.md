# End-to-end test report (Phase 18)

Environment: Linux 6.8, Python 3.12.14, SQLite 3.53.1, FastAPI 0.143.5, SQLAlchemy 2.1.5, 2 CPUs. Source revision: **unavailable** (not a git checkout). Assessed 2026-10-21. No live provider, container runtime or real browser was used.

| Workflow | How it was exercised | Outcome |
|---|---|---|
| 1 Repository onboarding (register → index → symbols → modify → reindex → stale removal; org boundary; audit) | existing API-level tests: `test_integration_flow`, `test_api_repositories`, `test_api_intelligence` (old file ids 404 after reindex, stale detection), `test_indexer`; tenant boundary: `test_acceptance_sweeps` | **Verified** |
| 2 Change analysis and review (static analysis, risk, impact, report, valid locations, traceability) | `test_acceptance_e2e::test_change_review_to_independent_approval_with_audit_lineage` (real git working-tree change → real review → changes/findings/risk; every reported path is a real in-repo relative file; working tree never modified) plus `test_review` | **Verified** |
| 3 Test generation and verification (syntax validation, restricted execution, outcomes tied to revision, stale evidence, failed ≠ pass) | `test_testing`, `test_verification` | **Verified** (restricted subprocess, Python/pytest) |
| 4 Repair and regression prevention (disposable workspace, diff boundary, validation, rejection, original unchanged) | `test_repair` (worked example end-to-end, tree-state comparison, path-escape and stale-base refusals), `test_refactor` | **Verified** |
| 5 Governance and approvals (roles, policy, unauthorized transition, self-approval, duplicate decisions, audit) | `test_governance` (38 scenarios incl. concurrency) and the E2E above (author and requester refused, outsider 404, independent approval, repeated decision 409, one decision event, denial and workflow audit actions present) | **Verified** |
| 6 Integrations and webhooks (valid, forged, malformed, duplicate, outbound signing, retry bounds) | `test_integrations` (mocked HTTP; signature, dedupe, uniform rejection, outbox retries and terminal failure, replay); benchmark test confirms signed/duplicate/forged behaviour | **Verified with mocked providers** |
| 7 Release and disaster recovery | build: backend deps + `vite build` ✔; fresh DB + migrate ✔; real uvicorn process + smoke ✔ (`test_release_ops`); backup → isolated restore → row counts, FK and referential checks, artifact files, head schema ✔ (`test_acceptance_e2e::test_release_and_disaster_recovery_chain...`); rollback **planning** ✔ (no downgrade ever executed) | **Verified for the CLI path; container build, compose, and real-host DR NOT executed (blocked: no Docker)** |

## Commands and results

| Command | Result |
|---|---|
| backend regression, run in 15–25-test chunks in the foreground (`pytest <node ids> -p no:cacheprovider -W ignore`) | **840 passed, 2 skipped, 0 failed** (842 tests) |
| `ruff check .` (backend, includes bandit rules) | all checks passed |
| `alembic check` on a fresh migrated database | no new upgrade operations |
| `python -m app.contracts` | contract OK |
| `python scripts/scan_secrets.py` | 0 findings (4 reviewed synthetic-fixture allowlist entries) |
| frontend: `tsc -b`, `eslint src`, `vitest run`, `vite build` (in a copy) | clean; 199 passed + 1 skipped; entry bundle 255.95 kB |
| `pip-audit -r requirements.txt --no-deps --disable-pip` (2.10.1) | no known vulnerabilities |
| `npm audit --audit-level=high` | 0 vulnerabilities |

Skips: 2 pre-existing skipped backend tests and 1 skipped frontend test (not re-evaluated here). **Frontend/browser:** only jsdom with mocked fetch; no real browser, console-error check, keyboard/accessibility audit or responsive review was performed. Part of the requested UI scope (Phase 15/16 screens) does not exist.
