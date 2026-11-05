# Path to RELEASE READY

Start from `docs/acceptance/RELEASE_CERTIFICATION.md` (decision: NOT RELEASE READY, 2026-10-21). Work in this order; each item names the evidence that closes it. Update `DEFECT_REGISTER.md`, `RELEASE_READINESS_REPORT.md`, `VERIFICATION_LOG.md` and `PROJECT_STATUS.md` only with results you actually ran.

1. **Version control (gate 15).** `git init`, commit, push. Make CI export `NOVA_SOURCE_REVISION=$GITHUB_SHA`. Evidence: `python -m app.release manifest` shows a real revision.
2. **Run the unrun deployment path (gates 1, 14).** On a machine with Docker: `docker build backend`, `docker build frontend`, `docker compose -f deployment/docker-compose.yml config`, then `up`, then `python -m app.release smoke --url http://127.0.0.1:8080 --wait 60`. Fix whatever breaks (the files were never built). Evidence: command output.
3. **Execute the GitHub workflows once** (`ci.yml`, tag-triggered `release.yml`); pin actions to verified commit SHAs and base images to digests. Add an image scan (e.g. Trivy) to CI.
4. **Restore drill on separate hardware (gate 13).** Backup off-host, `restore` into a fresh host, run `integrity` and `smoke`; record real RTO/RPO.
5. **Real-browser pass.** Playwright (or similar) over the critical journeys: console errors, failed requests, keyboard access, responsive layout. Finish the Phase 15/16 UI gaps listed in `PROJECT_STATUS.md`.
6. **One real provider** (GitHub staging account) for inbound webhooks, status publishing and CI evidence.
7. **Decide database scope.** Either state "SQLite only" in all docs, or add and run PostgreSQL integration tests.
8. **Open findings** in `DEFECT_REGISTER.md`: F-003 incremental re-index speed, F-005 audit immutability (hash chain), F-007 rate limiting / retention purge / PR events, F-011 test sandboxing, F-012 hash-locked requirements and signed artifacts. Fix or formally accept each with an owner.
9. **Re-run everything**: backend suite (chunk it; the sandbox is slow), `ruff`, `alembic check`, `python -m app.contracts`, `scripts/scan_secrets.py`, `pip-audit`, `npm audit`, frontend `tsc`/`eslint`/`vitest`/`build`, then re-evaluate the 16 gates.
