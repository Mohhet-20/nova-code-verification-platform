# Deployment guide

Target: **one Linux host running Docker Engine with the Compose plugin**, persistent local disk, TLS in front. This is the supported production path.
The release CLI, the production configuration, the API process layout, nginx config and the smoke tests are exercised by CI, and the
API image is built and run by the hosted demo. Do the first deployment on a staging host and run every verification step below.

## 1. Choose the host

**Recommendation: a single Linux VM** (for example Ubuntu 24.04 LTS on any cloud or on-premises hypervisor) with a persistent disk.

| Requirement (from the code) | Consequence |
|---|---|
| SQLite, one writer, one API process, in-process background jobs | one long-running container; no autoscaling, no scale-to-zero |
| Repositories must be on a local filesystem the API can read and (for approved repairs) write | bind-mounted host directory; serverless/PaaS without volumes does not fit |
| `git` CLI and repository test execution inside the API container | a full container runtime, not a functions platform |
| Durable `/data` volume for database, artifacts and backups | persistent block storage, plus an off-host copy of backups |

Sizing starting point (not a measured capacity claim): 2 vCPU, 4 GiB RAM, 20 GiB disk plus the size of the repositories and ~3x the database for
backups. The API container is limited to 2 CPU / 2 GiB; raise `mem_limit` if large repositories are indexed. PaaS options that offer a persistent volume and a
single long-running Docker container can run the same images, but keep the "one process, one volume" rule; nothing in this repository is platform-specific
except `deployment/docker-compose.tls.yml` and `deployment/Caddyfile`.

## 2. Prepare the host (run as an administrator on the host)

```bash
# Docker Engine + Compose plugin: follow https://docs.docker.com/engine/install/ for your distribution, then:
docker version && docker compose version          # Compose v2.20+ (needs `service_completed_successfully`)

# Repositories NOVA may analyse. The API runs as uid 10001; files must be owned by it or git reports "dubious ownership".
sudo mkdir -p /srv/nova/repos
sudo git clone https://example.invalid/your/repo.git /srv/nova/repos/your-repo   # or copy an existing checkout
sudo chown -R 10001:10001 /srv/nova/repos
```

## 3. Get the release and configure it (working directory: the repository root)

```bash
cd /opt/nova                                           # wherever you placed this repository / release archive
cp deployment/production.env.example deployment/production.env
chmod 600 deployment/production.env

python3 -c "import secrets; print(secrets.token_urlsafe(48))"                              # -> NOVA_API_TOKEN
python3 -c "import base64, os; print(base64.urlsafe_b64encode(os.urandom(32)).decode())"   # -> NOVA_INTEGRATION_SECRET_KEY
```

Edit `deployment/production.env` and set at least: `NOVA_API_TOKEN`, `NOVA_CORS_ORIGINS` (the exact `https://` origin users type), `NOVA_REPOS_HOST_DIR=/srv/nova/repos`,
`NOVA_REGISTRATION_BASE_DIRS=/srv/repos`, `NOVA_SOURCE_REVISION` (commit or tag), `NOVA_TEST_EXECUTION_ENABLED` (keep `false` unless every repository is trusted),
and `NOVA_DOMAIN` if you use the TLS overlay. Store the token and the encryption key in your password manager / secret store as well: the key is **not**
included in backups. Every variable: `PRODUCTION_CONFIGURATION.md`.

Define a shell shortcut for the rest of this guide:

```bash
COMPOSE="docker compose -f deployment/docker-compose.yml --env-file deployment/production.env"
# with automatic HTTPS (Caddy, needs DNS for NOVA_DOMAIN -> this host and ports 80/443 open):
# COMPOSE="docker compose -f deployment/docker-compose.yml -f deployment/docker-compose.tls.yml --env-file deployment/production.env"
```

## 4. Build and validate

```bash
$COMPOSE config -q                                     # file and interpolation check (fails if a required variable is missing)
$COMPOSE build                                         # builds nova-api:local and nova-web:local from digest-pinned base images
docker run --rm --entrypoint id nova-api:local -u     # must print 10001
$COMPOSE run --rm --no-deps migrate python -m app.release check-config
```

`check-config` exits 1 and names the setting (never its value) if the production contract is violated (e.g. `api_token_weak`, `cors_wildcard`). Warnings
such as `sqlite_in_production` and `test_execution_unsandboxed` are expected and explained in `PRODUCTION_CONFIGURATION.md`.

## 5. Initialize or migrate the database, then start

```bash
$COMPOSE up -d                 # migrate (preflight -> pre-migration backup if data exists -> locked migration) -> api -> web [-> proxy]
$COMPOSE ps -a                 # migrate: "Exited (0)"; api, web: "healthy"
$COMPOSE logs migrate          # JSON report: {"migration": {"changed": ..., "after": "0017", "backup": ...}}
```

Migration is an explicit release step: it runs in the one-shot `migrate` container before the API starts, takes a verified backup first when the database
already holds data, never drops or recreates the database, and the API **refuses to start** in production if the schema is not at the head revision.
To run the release step on its own (for example before switching traffic): `$COMPOSE run --rm migrate`.

## 6. Verify health and readiness

```bash
curl -fsS http://127.0.0.1:8080/api/v1/health      # liveness: {"status":"ok","version":"0.19.0"}
curl -fsS http://127.0.0.1:8080/api/v1/ready       # readiness: {"status":"ready","checks":{"database":"ok","migrations":"ok","revision":"0017"}}
curl -fsS http://127.0.0.1:8080/healthz            # web container
# with the TLS overlay: curl -fsS https://$NOVA_DOMAIN/api/v1/ready
```

`/api/v1/health` does not touch the database (container health check). `/api/v1/ready` returns 503 until the database is reachable and migrated.

## 7. Run the deployment smoke tests

From the repository root on the host (Python 3.11+ standard library only; the token is read from the environment and never printed):

```bash
export NOVA_API_TOKEN="$(grep '^NOVA_API_TOKEN=' deployment/production.env | cut -d= -f2-)"
python3 scripts/deployment_smoke.py --url http://127.0.0.1:8080 --web-url http://127.0.0.1:8080
# Full workflow on a NEW deployment or staging only (creates a repository record, index, analysis, review, test run and a task):
python3 scripts/deployment_smoke.py --url http://127.0.0.1:8080 --web-url http://127.0.0.1:8080 --write --repo-path /srv/repos/your-repo
$COMPOSE exec api python -m app.release smoke --url http://127.0.0.1:8000     # the release CLI's own post-deploy checks, from inside
```

Exit code 0 only if every check passed; each check prints one JSON line. A deployment is successful only after the smoke test passes.
Optional real-browser check (Chrome or Edge installed on the machine you run it from): `frontend/e2e/browser_smoke.mjs` (instructions in the file header).

## 8. Use it

Open `https://<your domain>/` (or `http://127.0.0.1:8080/` through an SSH tunnel), go to **Settings & help**, enter the API token (kept in the tab's
session storage only), then **Repositories -> Register** with a path under `/srv/repos/`.

## 9. Logs, troubleshooting, stop

```bash
$COMPOSE logs -f api                      # structured JSON, one line per request with request_id, route, status, duration
$COMPOSE logs --since 30m web
$COMPOSE exec api python -m app.release status       # schema state
$COMPOSE exec api python -m app.release integrity    # database + file references
$COMPOSE exec api python -m app.release history      # deployment history
$COMPOSE stop -t 30                        # graceful stop (SIGTERM; the API drains for up to 20 s); data is kept
$COMPOSE start                             # start again
$COMPOSE down                              # remove containers and network; volumes are KEPT
```

**Never run `$COMPOSE down -v` on a real deployment**: `-v` deletes the `nova-data` volume (database, artifacts and the backups stored on it).
Symptom-based procedures: `RUNBOOK.md`. Incidents: `INCIDENT_RESPONSE.md`.

## 10. Upgrades

```bash
$COMPOSE exec api python -m app.release backup --label pre-upgrade && $COMPOSE cp api:/data/backups ./offhost-backups   # see BACKUP_AND_RESTORE.md
# fetch the new release, update NOVA_SOURCE_REVISION, then:
$COMPOSE build && $COMPOSE up -d          # migrate runs again; api restarts only after it succeeds
python3 scripts/deployment_smoke.py --url http://127.0.0.1:8080 --web-url http://127.0.0.1:8080
```

There is a short outage while `migrate` runs and the API restarts; no zero-downtime claim is made. If the smoke test fails: `ROLLBACK_AND_RECOVERY.md`.

## Local (non-container) deployment

The same release steps work without Docker (this is what was rehearsed): from `backend/` with a virtual environment built from `requirements.txt`,
export the production variables plus `NOVA_DATABASE_URL`, `NOVA_ARTIFACT_DIR`, `NOVA_BACKUP_DIR`, `NOVA_RELEASE_STATE_DIR`, then
`python -m app.release deploy` and
`uvicorn app.main:create_app --factory --host 127.0.0.1 --port 8000 --workers 1 --proxy-headers --timeout-graceful-shutdown 20 --no-server-header --no-access-log`,
and serve `frontend/dist` (from `npm ci && npm run build`) with `frontend/nginx.conf` adjusted for `root`, the upstream address and the resolver.
