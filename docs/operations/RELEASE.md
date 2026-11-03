# Release engineering

## CI gates (`.github/workflows/ci.yml`; triggers: `pull_request`, push to `main`; read-only token, no secrets, no `pull_request_target`)

| Gate | Job |
|---|---|
| Lockfile install (`pip install -r` pinned, `npm ci`), `pip check` | backend, frontend |
| Lint + bandit rules (`ruff`, `eslint`), type check (`tsc`) | backend, frontend |
| Migration chain vs models (`alembic check`), fresh install to head + integrity, API contract check | backend |
| Backend unit + integration tests (junit), production-config contract tests | backend |
| Frontend tests (junit) and production build | frontend |
| Secret scan (`scripts/scan_secrets.py` + reviewed allowlist), `pip-audit`, `npm audit --audit-level=high` | security |
| Container builds, non-root check, production guard rejects an unconfigured start, `git` present; **full compose boot** (migrate -> api -> web) with a generated env, `scripts/deployment_smoke.py --write` through the web proxy, backup + verify + isolated restore inside the API container, graceful stop (no push) | containers |

Release publication is separate (`release.yml`): only a `v*` tag triggers it; the verify job has a read-only token; only the `publish` job has `contents: write`, and it only attaches the manifest and SBOM to the GitHub release. It deploys nothing, pushes no image, and has no secrets.

**Action pinning:** every third-party action is pinned to a full commit SHA, resolved on 2026-10-22 with `git ls-remote` (tag in a trailing comment). Base images are pinned by tag and digest (resolved from Docker Hub the same day). `pip-audit` is pinned (2.10.1). **None of these workflows has been executed** (the folder is not yet a Git repository); the first run is an item in `RELEASE_CHECKLIST.md`.

## Release manifest (`python -m app.release manifest --out release-manifest.json --sbom sbom.cdx.json [--junit file]`)

Contains version, source revision (from `NOVA_SOURCE_REVISION`/`GITHUB_SHA`/git; otherwise `unknown`), build time, runtime, migration head plus an additive/needs-review classification per revision, the pinned backend dependency inventory (pin vs installed), frontend dependency count, a test summary parsed from a junit file (otherwise `available: false`), image metadata (`built: false` unless supplied), and `signature: null`, `attestation: null`. The **CycloneDX 1.5 SBOM** lists backend pins and the frontend lockfile packages as purls. **Nothing is signed or attested.**

Real output on 2026-10-22, now from the Git checkout: version `0.19.0`, source revision `7fa5a8a` (`source: git`, `dirty` until the follow-up commit), migration head `0017` (15 of 17 revisions classified additive; `0002` and `0017` need review - `0017` only widens a CHECK constraint and its downgrade refuses to run while AI-assisted rows exist), CycloneDX 1.5 SBOM with 312 components (including `anthropic`).

## Supply-chain / security findings

| Check | Result | Disposition |
|---|---|---|
| `pip-audit -r requirements.txt --no-deps` (2.10.1, 2026-10-22) | no known vulnerabilities | clean (advisory database at run time) |
| `pip-audit --path <runtime venv site-packages>` (all resolved transitive packages of a `requirements.txt`-only install) | no known vulnerabilities | clean |
| `npm audit --audit-level=high` (npm 11.16.0, 2026-10-22) | 0 vulnerabilities (all severities) | clean |
| `scripts/scan_secrets.py` (2026-10-22) | 0 findings; 4 reviewed allowlist entries, all synthetic test fixtures | accepted (listed with reasons in `scripts/secret_scan_allowlist.txt`) |
| `ruff --select S` (bandit rules) | clean | - |
| Image scan (e.g. Trivy), DAST, SAST beyond bandit rules | **not run** (tools unavailable) | unresolved - add to CI when available |
| Dependencies are pinned but not hash-locked | `pip install --require-hashes` not used | accepted risk, documented |
| Unsigned artifacts, no provenance attestation | by design not claimed | unresolved |
