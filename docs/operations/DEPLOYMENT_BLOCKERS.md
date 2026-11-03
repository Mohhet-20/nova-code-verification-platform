# Deployment blockers and accepted limitations (2026-10-22)

Severity: **Critical** = deployment safety cannot be established until resolved; **High** = must be resolved before production use of the affected
feature; **Medium/Low** = documented, accepted for a first single-host deployment if the owner signs off.

## Open blockers

| ID | Severity | Blocker | Evidence | Exact remediation | Needs |
|---|---|---|---|---|---|
| B-01 | **Critical** | The Docker images and the compose stack - the intended production method - have **never been built or started**. Two defects that would have stopped the stack (D-01, D-02 in `DEPLOYMENT_AUDIT.md`) were found by rehearsal and inspection; others that only a real build reveals (e.g. a package or path inside the images) cannot be ruled out. | `docker`: command not found on Windows and in WSL; rootless Docker impossible (`newuidmap` missing); installing needs an administrator password | Either (a) in WSL: `sudo apt-get install -y docker.io docker-compose-v2 && sudo usermod -aG docker $USER`, open a new shell, then run the steps of the CI `containers` job by hand (`DEPLOYMENT_GUIDE.md` sections 3-7 with `--write`), or (b) resolve B-02 and let the `containers` CI job run on GitHub. | **You**: an administrator password for (a), or a GitHub repository for (b) |
| B-02 | High | **Partly resolved:** the project is now a local Git repository (initial commit `7fa5a8a`; releases record the revision), but it has not been pushed, so the CI workflows (including the container gate) have never run. | `git log`; no remote configured | Create a private repository on GitHub, `git remote add origin <url> && git push -u origin main`, review the first CI run | **You**: the GitHub repository and authorization to push |
| B-03 | High | No staging or production host has been deployed or externally verified. | - | Follow `DEPLOYMENT_GUIDE.md` on a staging VM, including the restore drill in `BACKUP_AND_RESTORE.md` | **You**: a host and authorization (may incur cost) |

## Accepted limitations (need owner sign-off, not blocking a single-host deployment)

| ID | Severity | Limitation | Mitigation in place |
|---|---|---|---|
| L-01 | Medium | Repository test execution is a restricted subprocess, not a sandbox: it runs as the API user with access to `/data` and the network. | API secrets are no longer readable (non-dumpable process, verified with a hostile test); `NOVA_TEST_EXECUTION_ENABLED=false` in the production template; `check-config` warns when it is on. |
| L-02 | Medium | Integrations (GitHub, issue trackers, OSV, webhooks) were verified with mocked transports only. | Off unless configured; SSRF guard enforced in production. Validate each integration in staging before relying on it. |
| L-03 | Medium | SQLite single node: one API process, no replication, RPO = backup interval. | Online verified backups, isolated restore, documented off-host copy and schedule. |
| L-04 | Medium | Audit tables are append-only through SQLite triggers, not cryptographically immutable (F-005). | Host and volume access control; backups preserve audit history. |
| L-05 | Medium | Phase 15/16 UI gaps (member directory, policy editor, issue linking UI, jobs panel); those functions are API-only. | APIs exist and are tested. |
| L-06 | Low | No external monitoring/alerting connected; telemetry is in memory. | Health/readiness endpoints, container health checks, structured logs; `INCIDENT_RESPONSE.md` explains how to attach an uptime check. |
| L-07 | Low | Release artifacts and images are unsigned; no image vulnerability scan (no scanner available). | Digest-pinned bases, SHA-pinned actions, `pip-audit`/`npm audit` clean, SBOM generated. Add Trivy/Grype to CI when available. |
| L-08 | Low | Repair/refactor patch engine supports LF files only; CRLF files (common in Windows checkouts) are refused with an explicit blocker. | Explicit `unsupported_line_endings` finding; nothing is changed silently. |
| L-09 | Low | Auth throttling and job admission are per process and in memory (reset on restart). | Only one process is supported anyway. |
| L-10 | Low | The TLS overlay (Caddy) and the container restore commands are documented but unexecuted (same cause as B-01). | Caddy config is minimal; any TLS proxy in front of `web:8080` works. |
| L-11 | Medium | AI-assisted repair proposals were verified with a fake Anthropic client only; no real Claude call was made (no credentials available), and proposal quality has not been evaluated. | Off by default; every proposal passes NOVA's edit gate, validation, human approval and test verification. Run one proposal on staging with `ANTHROPIC_API_KEY` before enabling it for users. |
| L-12 | Low | One API process: list endpoints that return many wide rows (e.g. findings pages) lose ~5x throughput at 8 concurrent clients (Python `sqlite3` releases the GIL per row). The worst case, whole-graph loads for metrics/cycles, was fixed (9.8 -> 74 req/s). | Adequate for a small team; documented in `FINAL_DEPLOYMENT_REPORT.md`. |

## What would change the decision

B-01 resolved (images build, the stack starts, `scripts/deployment_smoke.py --write` and the in-container backup/restore pass) plus B-02 (a revision to
release from) would support **DEPLOYMENT READY WITH LIMITATIONS** for a single-host deployment, with L-01 to L-10 signed off by the owner.
