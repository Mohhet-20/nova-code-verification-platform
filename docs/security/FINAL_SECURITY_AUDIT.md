# Final security audit (Phase 18)

Assessed 2026-10-21 against the repository as it stands (not a git checkout; no revision id). Method: manual review of critical paths, targeted regression tests, route-wide sweeps over the live OpenAPI document (307 operations), dependency and secret scans. **This is not a penetration test and not a complete audit**; nothing was run against external targets.

## Controls verified by executed tests

| Class | Evidence | Result |
|---|---|---|
| Broken authentication | `test_acceptance_sweeps::every_operation_rejects_missing_or_wrong_token`: every non-public operation answers 401 for no token, a wrong token and a fake `nova_u_` token; inbound webhook endpoints reject unsigned requests | pass |
| Function-level authorization / privilege escalation | sweep: viewer and auditor tokens get no 2xx from any mutating operation; `test_governance` role, ownership and last-admin tests; request bodies cannot claim roles | pass |
| Object-level authorization, cross-tenant leakage, IDOR | sweep: another organization's owner token against every repository-scoped operation (real repository id) → only 401/403/404/405/422; `test_governance::cross_tenant_isolation` | pass |
| Mass assignment | `test_governance::requests_cannot_claim_roles_or_scope`; request schemas are defined per endpoint | pass |
| SQL injection | no string-built SQL in `app/` outside the release CLI (table names from `sqlite_master`/constants); ORM/bound parameters elsewhere | pass (review) |
| Command injection / unsafe subprocess | no `shell=True`, `os.system`, `eval`, unsafe loaders in `app/` runtime code; all `subprocess` calls use argument lists | pass (review) |
| Git argument/option injection | sweep over every operation accepting `base`/`head`: `--output=<file>`, `-p`, `--upload-pack=…`, `a..b`, `x@{1}` rejected, victim file never created; webhook SHAs must be 40 hex; diffs use `--no-ext-diff --no-textconv` | pass |
| Path traversal / arbitrary file write | sweep over every path-like query/body parameter with traversal, absolute, Windows-style, encoded and NUL payloads: no outside content, no 5xx; `test_paths_security` | pass |
| SSRF | `test_integrations` (private, loopback, link-local/metadata, DNS rebinding with allow-list) and notifier tests | pass (mocked resolver) |
| Webhook verification and replay | HMAC verification, uniform rejection, delivery-id dedupe, no status downgrade, secret rotation (`test_integrations`) | pass |
| Credential leakage | sweep: no GET body contains any issued token; secrets never in responses/DB/audit (`test_integrations`); health/config errors name settings, never values (`test_release_ops`) | pass |
| Weak production secrets | production contract rejects missing/short/placeholder tokens, wildcard CORS, relaxed SSRF guard, public metrics, invalid encryption key; startup refuses | pass |
| Resource exhaustion | **F-001 fixed** (global body limit, 413); malformed/deeply nested JSON fuzz on all mutating operations: no 5xx; indexing/job limits from earlier phases | pass |
| Information disclosure on failure | **F-002 fixed** (storage failures → 503 `database_unavailable`, no paths/SQL in body); corruption mid-flight test | pass |
| Audit coverage / integrity | denials and workflow actions audited; append-only enforced by SQLite triggers (`test_governance`) | pass, see residual |
| Untrusted generated patches / test execution | patches validated in isolation, base-hash checked; tests run in a restricted subprocess | pass with residual |

## Findings

| ID | Severity | Status |
|---|---|---|
| F-001 No global request-body size limit (64 MB body fully parsed) | Medium | **Fixed** + regression test |
| F-002 Storage failures reported as generic 500 | Low | **Fixed** + regression test |
| F-005 Audit immutability depends on SQLite triggers removable with file write access | Medium (accepted, documented since Phase 15) | Open |
| F-007 Inbound rate limiting is per process; no retention purge job | Medium | Open |
| F-011 Test execution is a restricted subprocess, not a sandbox; a native tree-sitter crash would end the process | Medium | Open (documented threat model) |
| F-012 Container/image scanning, DAST, signed releases not performed | Medium | Open (tooling unavailable) |

No Critical or High finding was identified. **That statement is limited to what was reviewed and tested above.**

## Supply chain

`pip-audit` 2.10.1 (`--no-deps`, advisory database queried at run time): no known vulnerabilities in the 13 pinned backend requirements. `npm audit` (npm 10.9.8): 0 vulnerabilities across 297 lockfile packages. Limits: pins are not hash-locked; transitive Python packages were audited only if pinned; base images are tag-pinned, not digest-pinned; GitHub Actions are tag-pinned (SHAs unverifiable here); no image scan was possible; an empty scanner result is not proof of safety.

## Residual risk summary

Development mode with no `NOVA_API_TOKEN` trusts every caller (intended for single-user local use; production refuses to start). Anyone with file-system access to the database can alter audit rows. Single-process rate limiting. Real providers, TLS and a hostile network were not exercised.
