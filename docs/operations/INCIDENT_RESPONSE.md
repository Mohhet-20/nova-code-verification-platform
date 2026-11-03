# Incident response (minimal)

For the person on call for a single-host NOVA deployment. `COMPOSE` is defined in `DEPLOYMENT_GUIDE.md`; detailed procedures are in `RUNBOOK.md`.
NOVA has **no external alerting connected**: nothing pages you. Monitoring that exists is pull-based (health/readiness endpoints, `/api/v1/ops/*`,
container health status, logs). Connect an external uptime check to `https://<domain>/api/v1/ready` if you need alerts.

## 1. Triage (5 minutes, read-only)

```bash
$COMPOSE ps -a                                        # which container is unhealthy / restarting / exited
curl -s -o /dev/null -w '%{http_code}\n' http://127.0.0.1:8080/api/v1/health   # 200 = API process alive
curl -s http://127.0.0.1:8080/api/v1/ready            # 503 + checks = database or migration problem
$COMPOSE logs --since 30m api | grep -E '"level": "(ERROR|WARNING)"' | tail -50
df -h /var/lib/docker                                 # disk (RB-14)
$COMPOSE exec api python -m app.release status        # schema state
```

| Observation | Likely cause | Go to |
|---|---|---|
| `api` restarting, log says `Invalid production configuration` / `Database schema is` | config or missing migration | RB-05 |
| `migrate` exited non-zero, `api` never started | migration failure (database untouched, backup named in the error) | RB-04 |
| readiness 503 `database: error:...` | disk full, permissions on the volume, corrupted file | RB-14, RB-10, RB-08 |
| HTTP 503 `database_unavailable` on requests | locked/missing database | RB-10 |
| all users get 401 | token rotated or wrong; users must re-enter it in Settings | PRODUCTION_CONFIGURATION.md "Secrets" |
| users get 429 | auth throttle | RB-17 |
| `web` healthy but pages show "Cannot reach the NOVA API" | `api` down or unhealthy | step 1 again for `api` |
| integration jobs/webhooks stuck | provider or receiver down | RB-11, RB-12, RB-13 |

Every error response carries an `X-Request-ID`; search the logs for it: `$COMPOSE logs api | grep <request id>`.

## 2. Security incident: suspected credential exposure

Do these in order. Nothing here deletes audit history.

1. **Preserve evidence:** `$COMPOSE exec api python -m app.release backup --label incident-$(date +%Y%m%d%H%M)` and copy it off-host.
   Save logs: `$COMPOSE logs --no-color api > incident-api.log`.
2. **Rotate the global API token:** generate a new one, put it in `production.env`, `$COMPOSE up -d`. The old token stops working immediately.
   Revoke exposed per-user tokens (governance API / UI) and have users rotate theirs.
3. **If the integration key was exposed** (`NOVA_INTEGRATION_SECRET_KEY`): rotate it (`PRODUCTION_CONFIGURATION.md`), and **revoke every stored provider
   credential at the provider** (GitHub tokens, webhook secrets) - assume ciphertext in any copied database is readable.
4. **If the host or `/data` was accessed:** treat the database as disclosed (it contains repository-derived content, audit history and hashed tokens);
   rotate everything above; rebuild the host from a clean image and restore from a pre-incident backup into a new directory (RB-08, RB-16).
5. **Review** for the exposure window: audit events (`/api/v1/governance/audit`), integration installations and webhook subscriptions created,
   repository registrations, approved repairs/refactorings applied to working trees, and test runs (repository code execution).
6. **Untrusted repository code** may have run if `NOVA_TEST_EXECUTION_ENABLED=true`: it ran as uid 10001 with access to `/data` and the network but
   **not** to the API's environment variables. Set it to `false` until the review is complete.

## 3. Restore service

Prefer the least destructive fix: restart (`$COMPOSE restart api`), roll back the application (`ROLLBACK_AND_RECOVERY.md`), and only then restore data
from a backup into a new directory. Never delete `nova.db`, journal files or artifacts to make an error go away.

## 4. Close out

Record: timeline, impact (which users, which data), commands run, backup names used, measured time to recover (your real RTO), and follow-ups.
Run `python3 scripts/deployment_smoke.py` before declaring recovery.
