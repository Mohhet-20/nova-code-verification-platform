# Defect register (Phase 18, assessed 2026-10-21)

Severity is not adjusted to improve the outcome. "Regression test" names the test that fails without the fix or that guards the behaviour.

| ID | Subsystem | Sev | Finding / evidence | Root cause | Remediation | Regression test | Status / residual |
|---|---|---|---|---|---|---|---|
| F-001 | Platform / API | Medium | A 64 MB JSON body POSTed to `/api/v1/tasks` was fully read and parsed (answered 422). Reproduced by direct call. | No request-size limit outside the two webhook endpoints | `app/core/limits.py` ASGI middleware, `NOVA_MAX_REQUEST_BODY_BYTES` (default 8 MiB), 413 `payload_too_large` for declared and streamed bodies | `test_acceptance_sweeps::test_oversized_request_bodies_are_rejected_before_parsing` | **Fixed**. Residual: one request can still occupy up to the limit per connection; no per-client rate limiting. |
| F-002 | Platform / ops | Low | Missing, unmigrated or corrupt database made every route return a generic 500 `internal_error`. Reproduced on an empty database. | Only a catch-all handler existed | `OperationalError`/`DatabaseError` handler → 503 `database_unavailable` + `Retry-After`, no internal detail; integrity errors keep the old path | `test_acceptance_resilience::...unmigrated_or_missing...`, `...corrupted_database_mid_flight...` | **Fixed** |
| F-006 | Test suite | Low | Six empty throwaway test files from earlier phases; per-test 5 s migration made the suite very slow | housekeeping | files removed; template-database fixture (migrated once per session, copied per test) | whole suite | **Fixed**. Migrations remain tested directly. |
| F-003 | Repository intelligence | Medium | Unchanged re-index costs ≈ a first index (1.64 s vs 1.54 s at 1,500 files; 320 ms vs 403 ms medium) | rows are copied into a new generation for atomic publish (limit 1/15) | none (deferred, risky near final acceptance) | `app.perf` workloads | **Open**; correctness fine, speed claim not made |
| F-004 | Platform | Informational | With no `NOVA_API_TOKEN` and no user tokens the API treats every caller as a local owner | single-user local mode by design | production contract refuses to start without a strong token | `test_release_ops` | Accepted limitation |
| F-005 | Governance / audit | Medium | Audit and decision tables are append-only through SQLite triggers; anyone with file-write access can drop the triggers | SQLite has no privilege model; no hash chain | none | `test_governance::audit_and_decision_rows_are_append_only_in_the_database` | **Open** (documented since Phase 15) |
| F-007 | Integrations | Medium | Inbound rate limiting is per process; no retention purge; GitHub PR events not turned into change requests | scope limits | none | `test_integrations` | **Open** |
| F-008 | Frontend | Medium | Phase 15/16 UI gaps (member directory, policy create/edit, issue link/create UI, jobs panel, subscription removal); no accessibility audit; no real-browser test | scope | none | vitest (jsdom) | **Open** |
| F-009 | Deployment | Medium | Dockerfiles, compose and GitHub workflows have never been executed | no Docker in the environment | none possible here | — | **Open (blocked)** |
| F-010 | Release engineering | Low | Migration classifier is a heuristic; no source revision (not a git checkout); releases unsigned | tooling limits | documented; manifest records `unknown` revision | `test_release_ops::manifest_and_sbom_are_honest` | **Open** |
| F-011 | Test execution | Medium | Repository test code runs in a restricted subprocess, not a sandbox | threat-model decision | none | `test_testing` | **Open** (documented) |
| F-012 | Supply chain | Medium | No image scan, no hash-locked requirements, tag-pinned actions/base images, unsigned artifacts | tooling / verification limits | documented | — | **Open** |

No Critical or High defects were identified in the scope reviewed. Total fixed in this phase: 3 (F-001, F-002, F-006).
