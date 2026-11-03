# Runbooks

Conventions: **[R]** read-only command, **[M]** mutating command. `nova` below means `cd backend && python -m app.release` on a host install, or `$COMPOSE exec api python -m app.release` in the container deployment (`COMPOSE` is defined in `DEPLOYMENT_GUIDE.md`; backups default to `/data/backups` there). Container logs: `$COMPOSE logs --since 30m api`. All commands work on Windows, macOS and Linux
(use the venv's Python). Nothing here deletes persistent data to clear an error; where data could be lost it is stated. Escalation = whoever owns the deployment, with the
output of `nova status`, `nova check-config` and the last 20 lines of `nova history` (none of which contain secrets).

---
## RB-01 Initial setup
**Symptoms:** new machine/host. **Steps:** [M] create venv, `pip install -r requirements.txt`; [M] copy `.env.example` to `backend/.env`; [R] `nova check-config`; [M] `nova migrate`; [R] `nova status` -> `at_head`; start API; [R] `nova smoke --url http://127.0.0.1:8000`. **Warning:** never reuse placeholder secrets. **Verify:** smoke exit code 0. **Escalate:** if `migrate` fails see RB-04.

## RB-02 Local development
[M] `scripts/dev-backend.ps1` and `scripts/dev-frontend.ps1` (or `uvicorn app.main:create_app --factory --reload` and `npm run dev`). Development needs no token or key; `check-config` shows warnings only. Reset a *dev* database only by choosing a new `NOVA_DATABASE_URL` path - never delete the old one without a backup. **Verify:** `scripts/check-all.*`.

## RB-03 Production deploy
**Pre:** [R] `nova check-config` (no errors), [R] `nova list-backups` shows a recent valid backup, off-host copy exists. **Do:** [M] `nova deploy --backup-dir <dir>` (preflight, verified pre-migration backup, locked migration); start the new API; [R] `nova smoke --url <base> --wait 60`; [R] `nova history`. **Success only when smoke exits 0.** **On failure:** ROLLBACK_AND_RECOVERY.md. **Warning:** the API is down during migration; no zero-downtime claim. **Escalate** if preflight reports `schema_unknown`.

## RB-04 Failed migration
**Symptoms:** `deploy`/`migrate` exits 1 with "Migration failed", API refuses to start ("schema is behind"). **Read-only diagnostics:** [R] `nova status`; [R] `nova integrity`; [R] `nova list-backups`; inspect the leftover `*.migrate.lock` (age). **Causes:** a migration error (message names the exception type only - read the server log), a locked database, a crashed runner holding the lock. **Recovery:** the database is never recreated. If the lock is older than 1 h and no runner is alive: [M] re-run with `--break-stale-lock`. If the migration partially applied and integrity fails: restore the `pre-migration` backup into a NEW directory (RB-08). **Data warning:** restoring loses writes since that backup. **Verify:** `nova status` = `at_head`, `nova integrity` ok. **Escalate** with the exception type and revision numbers.

## RB-05 Startup failure
**Symptoms:** process exits immediately; container restarts. **Diagnostics:** [R] read the first error line - `Invalid production configuration: [code] message` or `Database schema is behind` ; [R] `nova check-config`; [R] `nova status`. **Causes/fixes:** config rule violated (fix the named setting - values are never logged); schema behind (run `nova deploy`); unknown revision (a newer release touched this DB: deploy that release or restore a compatible backup - do not edit `alembic_version`). **Verify:** `/api/v1/ready` 200 then smoke.

## RB-06 Failed rollback
**Symptoms:** after redeploying the previous release the API errors or fails readiness. **Diagnostics:** [R] `nova rollback-plan <previous release-manifest.json>`; [R] `nova status`; [R] `nova integrity`. **Causes:** non-additive migration in between (`requires_data_decision`), config mismatch. **Recovery:** roll **forward** to the fixed release, or restore the pre-migration backup into a NEW directory and switch (RB-08). Do not downgrade the schema by hand and do not delete uploads. **Data warning:** restore drops post-migration writes. **Verify:** smoke.

## RB-07 Backup
[M] `nova backup --backup-dir <dir>`; [R] `nova verify <dir>/<name>`; [R] `nova list-backups`; copy the directory off-host; [M] `nova prune --keep N` (keeps the last valid copy). **Failure:** "database does not exist"/"integrity check" -> run `nova integrity`; a `.partial-*` directory is an interrupted run - safe to delete by a human after confirming a newer valid backup exists. **Warning:** the encryption key is not in the backup (RB-09). **Verify:** `verify` prints `valid: true`.

## RB-08 Restore (isolated)
**Pre:** [R] `nova verify <backup>`. **Do:** [M] `nova restore <backup> --target <NEW empty dir>`; read the report - `ok` must be true (integrity, FK check, referenced files present, no row shrink). Start a second API instance (or stop the first) pointed at `<target>/db.sqlite3` and `<target>/artifacts`, run [R] `nova smoke`. Only then switch production, keeping the old directories until validation completes. **Data warning:** anything written after the backup time is not in the restore; the previous directories are the only copy of it - keep them. **Never** restore over the live database (the tool refuses). **Escalate** if `ok` is false: the report lists which check failed.

## RB-09 Credential loss / rotation
**API token lost/leaked:** set a new `NOVA_API_TOKEN`, restart, update clients; review audit (RB-15). **Encryption key lost:** stored provider credentials and webhook secrets in the DB are unrecoverable by design. Recovery: restore the key from your secret store; otherwise [M] reinstall/re-enter each credential in the Integration Center (env-reference credentials are unaffected). **Planned key rotation:** new key as `NOVA_INTEGRATION_SECRET_KEY`, old into `..._KEYS_PREVIOUS`, restart, run re-encryption, verify credentials still test OK, remove the old key. **Warning:** never remove the old key before re-encryption completes. See PRODUCTION_CONFIGURATION.md.

## RB-10 Database contention / "database is locked"
(SQLite has no connection pool exhaustion; the equivalent is writer contention.) **Symptoms:** 500s/slow requests, "database is locked", job lease timeouts. **Diagnostics:** [R] `/api/v1/ops/diagnostics`, [R] API logs for `OperationalError`; check for a long backup/index job; confirm only **one** API replica and no other process holds the file. **Recovery:** stop extra writers; wait for long jobs; restart the API (graceful, 20 s). Do not delete `-journal`/`-wal` files by hand. **Escalate** if it persists: a single-node SQLite limit has been reached and a database migration project (not claimed here) is needed.

## RB-11 Worker failure
**Symptoms:** `/api/v1/integrations/health` degraded, jobs stuck `leased`. **Diagnostics:** [R] `GET /api/v1/integrations/jobs`, `/health`; logs `integration_worker_error`. **Causes:** worker disabled (`NOVA_INTEGRATION_WORKER_ENABLED=false`), exception loop, provider outage. **Recovery:** leases expire and work is re-queued automatically; restart the API to restart the loop; jobs are bounded with retry limits, so no manual purge is needed. Optional integrations never fail core readiness. **Verify:** queue depth falls; smoke passes.

## RB-12 Webhook backlog
**Diagnostics:** [R] `GET /api/v1/integrations/webhook-deliveries`, `/jobs`. **Causes:** receiver down, SSRF guard blocking a private destination (intended), rate limits. **Recovery:** fix the receiver; deliveries retry with backoff up to the retry limit then go to dead state where they can be replayed through the integrations API (there is no UI for this yet). Do not disable the SSRF guard in production. **Data warning:** none; delivery history is kept.

## RB-13 Provider outage
**Symptoms:** SCM/CI calls failing, integration health degraded. **Diagnostics:** [R] `/api/v1/integrations/health`; provider status page. **Behaviour:** calls time out and are retried within limits; core review/analysis features keep working; readiness stays green because integrations are optional. **Recovery:** wait; after the outage, jobs resume on their own; replay missed webhooks via the provider's redelivery.

## RB-14 Disk exhaustion
**Symptoms:** writes fail, readiness `artifact_dir_not_writable`, backups fail preflight (`disk_low`). **Diagnostics:** [R] disk usage of the data volume; [R] `nova list-backups` (size); largest artifact directories. **Recovery:** free space by moving **verified, off-host-copied** backups away (`nova prune --keep N` after copying); extend the volume. Do not delete the database, `-journal` files or artifacts that DB rows reference (`nova integrity` lists broken references). **Verify:** preflight ok, `nova integrity` ok.

## RB-15 Security incident
**Immediate:** rotate `NOVA_API_TOKEN` and, if exposed, the encryption key (RB-09); revoke provider credentials at the provider. **Diagnostics (read-only):** audit events (`/api/v1/governance/audit`), webhook deliveries, ops logs; look for unexpected principals, installations, or exports. **Preserve evidence:** [M] `nova backup --label incident` before changing data. **Never** delete audit history. **Recovery:** restore from a pre-incident backup into a new directory only if data integrity is in doubt (RB-08). **Escalate** per your security policy; report which secrets were exposed and when.

## RB-16 Disaster recovery and post-restore validation
**Scenario:** host/volume lost. **Needs:** latest off-host backup, the encryption key, the release matching (or newer than) the backup's `schema_revision`, `deployment/production.env`. **Steps:** [M] provision host; [R] `nova verify` the backup; [M] `nova restore` into a fresh data directory (migrates to head); start API on it; **post-restore validation:** [R] `nova integrity` (DB + FK + file references), [R] `nova status`, [R] `nova smoke`, spot-check: users/orgs and role assignments present, repositories linked to the right org, workflow/policy history and audit log present (row counts vs the manifest), integration installations decrypt (test connection), recent webhook deliveries consistent. **Record** the outcome and elapsed time (that is your measured RTO). **Warning:** RPO = age of the last backup. DR on separate hardware has not been exercised for this repository.

## RB-17 Users get HTTP 429 "Too many failed authentication attempts"
**Cause:** more than `NOVA_AUTH_FAILURE_LIMIT` invalid bearer tokens from one client address within `NOVA_AUTH_FAILURE_WINDOW_SECONDS` (default 20 in
300 s). Usually a client with an old token after a rotation; possibly guessing. **Diagnostics:** [R] API logs: `"status": 429` and 401 lines with
the same `route`; the `Retry-After` header says how long. **If every user is blocked at once:** the API is seeing the proxy's address instead of clients'
- check the trust chain in `ARCHITECTURE_AND_NETWORKING.md` ("Client addresses"). **Recovery:** fix the client's token and wait for the window, or
restart the API (state is in memory). Do not set `NOVA_AUTH_FAILURE_LIMIT=0` permanently. **Escalate** as RB-15 if the source is unknown.

## RB-18 Test runs end as `rejected` - "Running repository code is disabled on this server"
**Cause:** `NOVA_TEST_EXECUTION_ENABLED=false` (the recommended production default unless every registered repository is trusted). Nothing was run and
nothing is reported as passed. **Change it** only after reading `ARCHITECTURE_AND_NETWORKING.md` ("Security boundaries"): set it to `true` in
`production.env` and `$COMPOSE up -d`. Other refusals: "administrator/root privileges" (the API must not run as root) and "pytest is not available"
(the configured `NOVA_TEST_PYTHON` lacks pytest; the image ships it).

## RB-19 Code review says "not a Git working tree" or "Git is not available"
**Causes:** the registered path is not the repository root (NOVA does not search parent directories for a `.git` - by design since 0.19.0); the files
are not owned by uid 10001 inside the container, so git refuses them as "dubious ownership"; or `git` is missing (it is installed in the image).
**Fix:** register the repository root; `sudo chown -R 10001:10001 <NOVA_REPOS_HOST_DIR>/<repo>` on the host. **Verify:** [R]
`$COMPOSE exec api git -C /srv/repos/<repo> rev-parse --is-inside-work-tree` prints `true`.

## RB-20 "Propose a fix with Claude" fails
**Codes:** `ai_disabled` (409) - `NOVA_AI_PROVIDER` is not `anthropic`; `ai_unavailable` (503) - no or invalid `ANTHROPIC_API_KEY`, timeout
(`NOVA_AI_TIMEOUT_SECONDS`), network or provider error, or a truncated answer; `too_many_requests` (429) - provider rate limit, retry later;
`ai_declined` (422) - Claude's safety classifier declined (after the server-side fallback); `ai_edit_rejected` (422) - the proposal failed NOVA's
edit gate, the response lists each problem; `ai_no_fix` (409) - Claude found no safe change, its reason is shown; `ai_no_context` (409) - no
relevant file could be sent (refresh the evidence or add hint files). **Diagnostics:** [R] the audit event `repair.patch_created` records model,
request id and token usage for successful proposals; API logs carry the request id. **Never** relax the edit gate or approve a proposal without
reading the diff. Details: `docs/architecture/AI_ASSISTED_REPAIR.md`.
