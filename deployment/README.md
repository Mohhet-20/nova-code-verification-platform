# Deployment

Single-host container deployment. Procedure: [`../docs/operations/DEPLOYMENT_GUIDE.md`](../docs/operations/DEPLOYMENT_GUIDE.md).

* `docker-compose.yml` - `migrate` (one-shot release step) -> `api` -> `web`. Read-only root filesystems, no capabilities, resource limits, log rotation,
  persistent volume `nova-data`, repository bind mount `NOVA_REPOS_HOST_DIR` -> `/srv/repos`.
* `docker-compose.tls.yml` + `Caddyfile` - optional overlay: Caddy with automatic HTTPS for `NOVA_DOMAIN`.
* `production.env.example` - placeholders only. Copy to `production.env` (git-ignored, `chmod 600`) and fill in real values.
* The CI `containers` job builds and boots this stack.
* Nothing in this repository deploys, publishes or pushes anything automatically.
