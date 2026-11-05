# Security policy

NOVA analyses and can modify source code, so security reports are taken seriously.

## Reporting a vulnerability

Please **do not open a public issue**. Use GitHub's private vulnerability reporting (the repository's **Security** tab ->
**Report a vulnerability**), and include steps to reproduce and the affected version or commit.

## Scope and design notes

* The security model, threat model and known limitations are documented in `docs/architecture/SECURITY_MODEL.md`,
  `docs/security/` and `docs/operations/KNOWN_LIMITATIONS.md`.
* Approved repository test runs execute repository code in a restricted subprocess, **not a sandbox**. Register only trusted repositories when
  `NOVA_TEST_EXECUTION_ENABLED=true` (the production template sets it to `false`).
* AI-assisted repair (`NOVA_AI_PROVIDER=anthropic`, off by default) sends an investigation's relevant files, after secret scrubbing, to Anthropic
  when a user requests a proposal. See `docs/architecture/AI_ASSISTED_REPAIR.md`.
