# CI/CD integration, pull-request intelligence and release readiness

NOVA connects the verification engine to change requests (pull requests or local branches), binds every result to an exact commit, and turns the evidence into a conservative release-readiness decision. It reuses `app/services/verification/` (change detection, `engine.run_job`, `gate`, policy versions, staleness); it adds no second runner or gate engine.

## Architecture

| Piece | Location |
|---|---|
| Provider adapters (local Git, GitHub), transport, webhook signature check, error classification | `app/services/cicd/providers.py` |
| Connections, change requests, workflow runs, status reporting | `app/services/cicd/service.py` |
| Webhook receiver (HMAC, dedupe, identity check) | `app/services/cicd/webhook.py` |
| Readiness policy + evaluator | `app/services/cicd/readiness.py` |
| Reports (JSON + Markdown) | `app/services/cicd/reports.py` |
| API | `app/api/cicd.py` (`/repositories/{id}/ci/...`, `/webhooks/github/{connection_id}`) |
| UI | `/repositories/:id/workflow`, `/repositories/:id/workflow/change-requests/:crId` |
| Tables (migration `0011`) | `ci_connections`, `ci_readiness_policies`, `change_requests`, `ci_workflow_runs`, `ci_status_reports`, `ci_webhook_deliveries`, `ci_reports` |
| GitHub Actions | `.github/workflows/ci.yml` |

## Providers

* **Local Git** (no network, no credentials): scans branches against the configured default branch, using the merge-base for the diff. It only reads Git (`rev-parse`, `diff`, `branch`) and never checks out, fetches or changes anything. A deleted branch closes its change request.
* **GitHub** (optional): receives `pull_request` webhooks, lists changed files through the REST API (paginated, bounded) and posts commit statuses. The API host is fixed by configuration (`NOVA_CI_GITHUB_API_BASE`); the transport refuses any other host and any non-HTTPS URL, so URLs inside payloads are never fetched.

### Credentials
NOVA stores only the **names** of environment variables (`^[A-Z][A-Z0-9_]{2,63}$`), e.g. `NOVA_GITHUB_TOKEN` and `NOVA_GITHUB_WEBHOOK_SECRET`. Values are read from the process environment at the moment of use, are never persisted, never returned by the API (only `token_configured`/`webhook_secret_configured` booleans) and are scrubbed from error text. A token-sized value typed where a name belongs is rejected.

A connection starts `unconfigured`. It becomes `validated` only after a successful provider call (`POST .../connections/{id}/validate`), and changing the credential variable names resets it. The local provider is validated when the repository is a Git repository.

Suggested token scope for status reporting: a fine-grained token limited to this repository with *Commit statuses: read & write* and *Pull requests: read*.

### Webhook configuration (GitHub)
Payload URL `https://<your-nova-host>/api/v1/webhooks/github/<connection_id>`, content type `application/json`, the secret in the environment variable named by the connection, event **Pull requests**. NOVA is local-first; exposing it requires a reverse proxy or tunnel that you control.

## Webhook security
Order of checks: connection lookup (unknown or local → 404; disabled → 403) → size cap while streaming (413 `payload_too_large`) → `X-Hub-Signature-256` over the **raw bytes** with `hmac.compare_digest` (401, before any parsing) → header validation → JSON parse → normalisation (strict SHA/ref/name patterns) → repository identity must equal the configured repository (403) → delivery-id dedupe (`UNIQUE(connection_id, delivery_id)`; a replay changes nothing) → event handling.

Nothing in a payload is executed, fetched, or trusted as a path or URL. A webhook **never starts test execution**: pull-request code is untrusted. It upserts the change request, invalidates earlier verification when the head moved, and records an idempotent *awaiting* workflow run. An authorised user starts verification explicitly (`confirm=true`). Events older than the recorded state (`updated_at`) are ignored, so out-of-order delivery cannot move the head backwards.

## Revision-bound verification
A workflow run stores the exact `head_sha`/`base_sha` it verified and the run id. The revision binding still applies: the head must equal the checked-out `HEAD` with a clean tree, otherwise the tests check is **BLOCKED** (NOVA never checks out another revision). When the change request's head moves, every earlier workflow run is *stale*: its readiness is reported as BLOCKED (`readiness_effective`), and no external status for the old SHA can be `success` for the new head.

Idempotency: the start key hashes (change request, head, base, mode, verification policy version, readiness policy version). An identical start returns the existing run (`reused: true`); `force` creates a new attempt; a different start while a run is active returns 409 `verification_in_progress`. Nothing is deleted, merged, pushed or checked out.

## Release readiness
Outcomes: `READY`, `NOT_READY`, `BLOCKED`, `INCOMPLETE`; precedence `NOT_READY > BLOCKED > INCOMPLETE > READY`. Missing or unverifiable evidence can never be READY.

| Rule | Pass when |
|---|---|
| `revision_current` | the evidence is for the change request's current head |
| `revision_verified` `quality_gate` | a completed run exists and its gate is PASS |
| `check:<id>` | each required check (default `syntax`, `static_analysis`, `tests`) passed; optional checks must pass unless the policy allows unresolved ones |
| `no_disallowed_findings` | no new static findings in the blocked severities |
| `evidence_recent` | the run finished within `max_evidence_age_hours` (default 168) |
| `policy_compatible` | the verification policy snapshot of the run equals the current one |
| `source_unchanged_since` | the working tree still matches what was verified |
| `migration_check`, `deployment_checks` | BLOCKED when the policy requires them: NOVA has no evidence source for either, and unit tests do not prove a deployment |

The policy is versioned and owner-editable (`PUT /readiness-policy`, audited). The decision is decision support, not a guarantee of correctness, and the output says so.

## External status reporting
Context `nova/<name>` (anything else is rejected so unrelated checks are never overwritten). Mapping: **success** only for READY + gate PASS on the exact current head; NOT_READY or gate FAIL → `failure`; INCOMPLETE → `pending`; BLOCKED or unevaluated → `error`; awaiting/queued/running → `pending`. A report for a head that is no longer current is `skipped_stale`.

Missing credentials persist the intent as `unconfigured` and send nothing. Errors: 401, 404, 422 and other 4xx are permanent; 403 with rate-limit headers, 429, 5xx and network/timeout errors are retryable with exponential backoff `min(3600, base·2^(n−1))` seconds up to `NOVA_CI_STATUS_MAX_ATTEMPTS`; early retries are refused (`retry_too_soon`); a `sent` report is never sent twice. A reporting failure never changes the verification outcome.

## Reports
`GET .../change-requests/{id}/review-report` builds the unified report from persisted rows only; `POST .../reports` stores a `review` or `release` report with a sha256 over the canonical JSON (excluding generation time, so the same inputs give the same hash). Reports are sanitised (secret patterns redacted, repository root replaced by `<repo>`, Markdown/HTML metacharacters and link syntax escaped) and size-fitted (`NOVA_CI_REPORT_MAX_BYTES`, with an explicit `truncated` flag). They state that **no security analysis was performed** — absence of findings is not a security conclusion.

## GitHub Actions
`.github/workflows/ci.yml` runs on `pull_request` (never `pull_request_target`) and pushes to `main` with read-only permissions, no secrets, job timeouts and lockfile installs. Backend: `ruff check .`, `pytest` with JUnit XML. Frontend: `npm ci`, `tsc -b`, `eslint src`, `vitest run` (JUnit XML), `npm run build`. Test results are uploaded as artifacts. The commands were each executed locally; the workflow itself has **not** run on GitHub.

## Executed local demonstration
Run on a disposable Git repository. Lines marked **[MOCKED]** use an in-process fake of the GitHub HTTP boundary; nothing contacted GitHub.

```
1. Disposable repository indexed; baseline recorded on main.
2. Feature branch 'feature' committed: 2a11bf43 (introduces a regression)
3. Local provider scan: {'created': 1, 'updated': 0, 'head_changed': 0, 'closed': 0, 'branches': 1}
4. Change request: feature -> main 2a11bf43 verified? False
5. Verification: gate FAIL | readiness NOT_READY | {'quality_gate': 'fail', 'check:tests': 'fail'}
6. Review report: regressions {'new': 1, ...} | security analysis performed: False
7. New commit 3072376a pushed -> {'head_changed': 1, ...}
8. Old result now stale: True | effective readiness BLOCKED | revision_current rule: blocked
9. Re-verified new head: gate PASS | readiness READY
10. [MOCKED GitHub] webhook for the same PR head -> accepted, outcome created
    forged signature -> 401 signature_invalid
    [MOCKED] provider 503 -> status retry_scheduled, state success, attempts 1, retryable True
    verification unaffected: gate PASS | readiness READY
    token leaked in any API output? False
```

## Limitations and residual risks
* Live GitHub behaviour is **unverified**: only the mocked transport was exercised. GitHub Enterprise Server, GitLab, Bitbucket and Azure DevOps are not implemented (the adapter interface is provider-neutral).
* Webhook delivery needs a reachable URL; there is no queue — retries of status reports are user/API-triggered, not a background scheduler.
* Verification is Python/pytest only and bound to the checked-out revision: for a GitHub pull request the head must be checked out locally before NOVA can verify it.
* Readiness and gate results are decision support. Security analysis, dependency/supply-chain checks, deployment and migration evidence are not performed (and later).
* Workflow actions are referenced by major version tag; pin to commit SHAs in a security-sensitive repository (SHAs could not be verified from the development environment).
* Webhook replay protection is per delivery id; GitHub does not sign a timestamp, so a captured, signed payload with a fresh delivery id would be accepted (its effect is limited to upserting a change request to a state at least as new as the recorded one).
* Not verified on Windows, in a real browser, on PostgreSQL or Python 3.11.

## Troubleshooting
* *Connection stays `unconfigured`*: the token variable is empty in NOVA's process environment, or validate was not run.
* *401 `signature_invalid`*: the secret variable differs from the one configured in GitHub, or a proxy altered the body.
* *Verification BLOCKED, tests*: the change request's head is not the checked-out commit, or the tree is dirty.
* *Status `retry_scheduled`*: wait for `next_retry_at`, then `POST /status-reports/{id}/retry`.
