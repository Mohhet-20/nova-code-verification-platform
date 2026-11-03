# NOVA operations

Start with **[FINAL_DEPLOYMENT_REPORT.md](FINAL_DEPLOYMENT_REPORT.md)** (what was verified, release decision), then **[DEPLOYMENT_GUIDE.md](DEPLOYMENT_GUIDE.md)**.

| Document | Purpose |
|---|---|
| [FINAL_DEPLOYMENT_REPORT.md](FINAL_DEPLOYMENT_REPORT.md) | Phase 19 result: changes, exact verification evidence, live-deployment status, release decision |
| [DEPLOYMENT_AUDIT.md](DEPLOYMENT_AUDIT.md) | Baseline inventory derived from the repository, baseline test results, defects found and fixed |
| [DEPLOYMENT_BLOCKERS.md](DEPLOYMENT_BLOCKERS.md) | Open blockers with exact remediation, accepted limitations |
| [DEPLOYMENT_GUIDE.md](DEPLOYMENT_GUIDE.md) | Host choice, configuration, build, migrate, start, health, smoke tests, logs, stop, upgrades |
| [ARCHITECTURE_AND_NETWORKING.md](ARCHITECTURE_AND_NETWORKING.md) | Services, ports, storage, request path and limits, client addresses, egress, security boundaries |
| [PRODUCTION_CONFIGURATION.md](PRODUCTION_CONFIGURATION.md) | Production contract, every environment variable, secrets provisioning / rotation / revocation |
| [BACKUP_AND_RESTORE.md](BACKUP_AND_RESTORE.md) | Data classes, backup format, isolated restore, container commands, measurements |
| [ROLLBACK_AND_RECOVERY.md](ROLLBACK_AND_RECOVERY.md) | Application, schema, data, config and credential rollback; container procedure |
| [RUNBOOK.md](RUNBOOK.md) | 20 symptom-based runbooks (read-only vs mutating commands marked) |
| [INCIDENT_RESPONSE.md](INCIDENT_RESPONSE.md) | Triage, credential-exposure procedure, recovery, close-out |
| [RELEASE.md](RELEASE.md) | CI gates, release manifest and SBOM, supply-chain findings |
| [RELEASE_CHECKLIST.md](RELEASE_CHECKLIST.md) | Per-release checklist, updating pinned images and actions |
| [RELEASE_READINESS_REPORT.md](RELEASE_READINESS_REPORT.md) | Phase 18 assessment (historical; superseded by the final deployment report) |

Everything operational is driven by one cross-platform CLI: `cd backend && python -m app.release <command>` (or `$COMPOSE exec api python -m app.release
<command>` in the container deployment). Read-only: `check-config status classify verify list-backups integrity manifest smoke history rollback-plan`.
Mutating: `migrate backup restore(new dir only) prune deploy`. Post-deploy end-to-end checks: `scripts/deployment_smoke.py`.
