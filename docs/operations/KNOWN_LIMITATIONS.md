# Known limitations

What NOVA does not do yet, or does with a stated caveat, and what mitigates each. Severity: **Medium/Low** = documented and acceptable for a single-host deployment.

| ID | Severity | Limitation | Mitigation in place |
|---|---|---|---|
| L-01 | Medium | Repository test execution is a restricted subprocess, not a sandbox: it runs as the API user with access to `/data` and the network. | API secrets are no longer readable (non-dumpable process, verified with a hostile test); `NOVA_TEST_EXECUTION_ENABLED=false` in the production template; `check-config` warns when it is on. |
| L-02 | Medium | Integrations (GitHub, issue trackers, OSV, webhooks) were verified with mocked transports only. | Off unless configured; SSRF guard enforced in production. Validate each integration in staging before relying on it. |
| L-03 | Medium | SQLite single node: one API process, no replication, RPO = backup interval. | Online verified backups, isolated restore, documented off-host copy and schedule. |
| L-04 | Medium | Audit tables are append-only through SQLite triggers, not cryptographically immutable (F-005). | Host and volume access control; backups preserve audit history. |
| L-05 | Medium | UI gaps (member directory, policy editor, issue linking UI, jobs panel); those functions are API-only. | APIs exist and are tested. |
| L-06 | Low | No external monitoring/alerting connected; telemetry is in memory. | Health/readiness endpoints, container health checks, structured logs; `INCIDENT_RESPONSE.md` explains how to attach an uptime check. |
| L-07 | Low | Release artifacts and images are unsigned; no image vulnerability scan (no scanner available). | Digest-pinned bases, SHA-pinned actions, `pip-audit`/`npm audit` clean, SBOM generated. Add Trivy/Grype to CI when available. |
| L-08 | Low | Repair/refactor patch engine supports LF files only; CRLF files (common in Windows checkouts) are refused with an explicit blocker. | Explicit `unsupported_line_endings` finding; nothing is changed silently. |
| L-09 | Low | Auth throttling and job admission are per process and in memory (reset on restart). | Only one process is supported anyway. |
| L-10 | Low | The TLS overlay (Caddy) and the container restore commands are documented but unexecuted. | Caddy config is minimal; any TLS proxy in front of `web:8080` works. |
| L-11 | Medium | AI-assisted repair proposals were verified with a fake Anthropic client only; no real Claude call was made (no credentials available), and proposal quality has not been evaluated. | Off by default; every proposal passes NOVA's edit gate, validation, human approval and test verification. Run one proposal on staging with `ANTHROPIC_API_KEY` before enabling it for users. |
| L-12 | Low | One API process: list endpoints that return many wide rows (e.g. findings pages) lose ~5x throughput at 8 concurrent clients (Python `sqlite3` releases the GIL per row). The worst case, whole-graph loads for metrics/cycles, was fixed (9.8 -> 74 req/s). | Adequate for a small team. |
