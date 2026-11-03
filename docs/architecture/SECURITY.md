# Security Intelligence and Supply-Chain Analysis (Phase 12)

NOVA scans a registered repository for vulnerable dependencies, insecure code patterns and possible secrets, evaluates the results against a versioned policy, and proposes reviewable fixes. **Scans only read files.** Nothing from the scanned repository is executed, imported or installed, no lifecycle script runs, and no network request is made unless an advisory source was explicitly enabled.

## Architecture

`app/services/security/`: `inventory` (manifests), `versions` (PEP 440 / semver), `advisories` (providers, matching), `codescan` + `analysis/analyzers/python_security*.py` (rules), `secrets_scan`, `priority`, `policy`, `scan` (orchestrator, finding lifecycle), `service` (triage, suppressions, exceptions), `remediation`, `reports`. API: `app/api/security.py` under `/api/v1/repositories/{id}/security`. UI: `/repositories/:id/security`. Tables: migration `0012` (`sec_*`).

A scan runs as a background job (one active scan per repository, interrupted scans are marked failed at startup), records the git `HEAD` and whether tracked files were modified (`dirty`), and stores a summary and a coverage report.

## Dependency inventory

Python: `requirements*.txt/.in` (with `-r` includes, VCS/URL lines, markers), `pyproject.toml` (PEP 621, optional dependencies, dependency groups, Poetry), `poetry.lock`, `uv.lock`, `Pipfile.lock`. JavaScript: `package.json`, `package-lock.json` / `npm-shrinkwrap.json` (v1-v3). Direct vs transitive relationships and dependency paths come from lockfiles. A version is **never invented**: `flask>=2.0` has version `unknown` (state `range`). Unsupported manifests (`yarn.lock`, `pnpm-lock.yaml`, `Pipfile`, `setup.py`, `go.mod`, ...) are listed as coverage gaps; symlinks and paths outside the repository are skipped. The inventory digest is reproducible.

## Advisories

Providers (all attributed in every finding): `workspace-store` (cached/imported advisories, workspace-scoped), `fixtures` (a directory of OSV JSON, `NOVA_SECURITY_ADVISORY_DIR`, for offline use and tests), `osv` (off unless `NOVA_SECURITY_OSV_ENABLED=true`; sends package names and versions only; **live behaviour is unverified**, only a mocked transport was tested). Advisories are deduplicated by id/alias without losing their sources. Matching uses ecosystem version semantics. Outcomes: `confirmed` (installed version in an affected range), `possible` (version unknown or range not evaluable), `unassessed` (unpinned, unsupported ecosystem, no source), `no_known_advisory` (explicitly *not* a safety claim). An unspecified severity stays unspecified (stored as medium with `severity_source: unspecified`; the policy decides how to treat it, default high).

## Rules and secrets

Python rules PY-SEC-001..014 cover dynamic code, shell execution, deserialization, SQL injection, path traversal, SSRF, weak hashes/randomness, sensitive logging, TLS verification, insecure configuration and tainted command execution; taint tracking is intraprocedural only. Findings state evidence quality (`dataflow`, `syntactic`, `heuristic`) and never claim exploitability. Secrets (SEC-SECRET-001/002): known token formats and generic assignments with entropy and placeholder filtering. The matched value is **never stored or returned**: findings carry a redacted preview and a salted hash for identity across line moves. Exclusions are recorded in coverage and the policy treats excluded files as unknown. NOVA never tests, revokes or rotates credentials. **Detection is best-effort and not exhaustive.**

## Lifecycle, suppression, exceptions

Statuses: `open`, `resolved` (only when a later scan of that type completed fully), `false_positive`, `accepted_risk`, `suppressed`. Every change is an append-only event. Suppressions require a reason, are scoped to one finding or one rule within a path glob (no `*`/`**`), may expire, and are audited; suppressed findings stay visible. Policy exceptions require an owner, a reason and an expiry (max 365 days), and every evaluation lists the exceptions it applied or found expired.

## Policy and prioritisation

`SecurityRules` (versioned, owner-edited): thresholds for code/secret/advisory findings, handling of possible matches and unspecified severities, required scan types, inventory/advisory-data requirements, prohibited packages and licences, maximum scan age and `indeterminate_effect` (`block` | `incomplete` | `allow`). An evaluation returns `pass` | `fail` | `indeterminate` with policy id and version, checks, causing findings, missing data, skipped checks, exceptions applied, timestamp, revision and explanation. **Missing or partial evidence is `indeterminate`, never `pass`.** Priority model `nova.priority/1` combines severity, confidence, evidence quality, observed reachability (imported package) and fix availability with configurable weights; unknown reachability never lowers a score, and critical/high severities have a bucket floor. No exploitability or business impact is inferred.

## CI/CD readiness

Release readiness (`readiness-policy`) gains an opt-in rule `require_security_scan` (default off, existing behaviour unchanged). When on, a completed scan of a clean tree at the change request's exact head must exist; the security policy result maps to pass / fail / blocked / incomplete (or an explicit "allow" for indeterminate). CI reports state whether a scan covered the revision.

## Remediation

Proposals are generated only where supported: a dependency upgrade (lowest fixed version above the installed one that no known advisory still lists as affected, direct dependencies in `requirements*.txt`/`package.json` with simple specifiers; major bumps flagged; lockfiles are **not** modified) or a deterministic rewrite (`yaml.load` to `yaml.safe_load`, `debug=True` to `False`, re-checked with the rules). Possible matches, transitive dependencies and secrets get `cannot_fix` with the reason. Submitting a proposal creates a repair investigation and patch and validates it without touching files; approval and apply stay in the repair workflow (explicit confirmation, originals preserved, rollback guidance). A failed validation leaves every file untouched. NOVA never claims a change preserves behaviour.

## Reports

JSON (`nova.security.report/1`), Markdown, HTML (escaped, restrictive CSP) and CSV (formula-injection guarded). Redacted, hashed, size-capped. A report states whether the scan was complete: zero findings from a complete scan reads "not a guarantee of security"; from a partial scan, "INCOMPLETE; absence of findings is not a result".

## API (`/repositories/{id}/security`)

`overview`; `scans` (POST 202, list, `/{id}`, `/{id}/components`); `findings` (filters kind/severity/status/rule/path/q/bucket, sort, pagination) and `/{id}`, `/{id}/triage`, `/{id}/remediations`; `advisory-matches`; `suppressions`, `exceptions` (+revoke); `policy` (GET/PUT, `/history`), `evaluate`, `evaluations`; `reports` (+download); `remediations`; `providers`; `advisories/import` (owner). Writes need a write role; policy, exceptions and import are owner-only. Every id is repository-scoped; other tenants' ids return 404.

## Run locally

```
export NOVA_SECURITY_ADVISORY_DIR=/path/to/osv-json   # optional offline advisories
# export NOVA_SECURITY_OSV_ENABLED=true               # optional, network, unverified
python -m pytest tests/test_security.py
```

## Executed demonstration

A disposable git repository (`pyyaml==5.3`, `requests>=2.0`, an unsafe `yaml.load`, a `shell=True` call fed by request data, a synthetic AWS-style key) and one synthetic advisory file:

```
SCAN completed partial | 5 open finding(s). The scan is PARTIAL.
GAPS ['advisory matching: 1 component(s) could not be assessed']      # requests>=2.0 has no pinned version
  P1 high DEP-ADVISORY requirements.txt        P2 high PY-SEC-012 app.py:6
  P3 high PY-SEC-002 app.py:6                  P3 high SEC-SECRET-001 settings.py:1  KEY = 'AKIA************'
  P4 medium PY-SEC-005 app.py:7
POLICY fail - FAIL: 1 advisory match(es) at or above high. 2 open code finding(s) ...
FIX proposed pyyaml==5.3 -> pyyaml==5.4   (diff to requirements.txt only; nothing applied)
SECRET LEAK: False   # the key never appears in any API response
```

## Limits and gaps

Python-only code rules (JavaScript/TypeScript code is not analysed); intraprocedural taint; npm/PyPI only; no lockfile editing; licences come from lockfiles when present; live OSV and any provider other than fixtures are unverified; not verified on Windows, PostgreSQL or Python 3.11; no SBOM export, container or IaC scanning; the policy gate is decision support, not a guarantee.
