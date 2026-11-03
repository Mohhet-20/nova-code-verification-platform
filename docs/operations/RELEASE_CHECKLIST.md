# Release checklist

Tick every box for each release. "Evidence" means the command output is saved with the release (CI run link or a text file). Commands are run from the
repository root unless stated; `COMPOSE` is defined in `DEPLOYMENT_GUIDE.md`.

## A. One-time items still open for 0.19.0 (blockers - see `DEPLOYMENT_BLOCKERS.md`)

- [ ] Put the project under version control (`git init`, first commit, push to the hosting platform). Releases need a source revision.
- [ ] Let `.github/workflows/ci.yml` run once on GitHub; fix anything it finds. The `containers` job is the first real `docker build` and compose boot.
- [ ] First deployment on a **staging** host following `DEPLOYMENT_GUIDE.md` end to end, including `--write` smoke and the restore drill.
- [ ] Owner sign-off on the accepted limitations listed in `FINAL_DEPLOYMENT_REPORT.md`.

## B. Before tagging a release

- [ ] `CHANGELOG.md` and the version (`backend/app/core/config.py` `APP_VERSION`, `backend/pyproject.toml`) updated together.
- [ ] CI green on the commit to be tagged: `backend`, `frontend`, `security`, `containers` (all four are required gates).
- [ ] New migrations: `python -m app.release classify` reviewed; any non-additive revision has a written rollback decision (restore vs fix forward).
- [ ] `python scripts/scan_secrets.py` -> 0 findings; `git diff` reviewed for secrets, generated clutter and unrelated changes.
- [ ] Dependency changes reviewed (no unreviewed major upgrades); `pip-audit` and `npm audit` clean or each finding dispositioned in `RELEASE.md`.
- [ ] Tag `vX.Y.Z`; `release.yml` attaches `release-manifest.json` and `sbom.cdx.json` (unsigned).

## C. Deploying the release

- [ ] Off-host copy of a fresh verified backup (`BACKUP_AND_RESTORE.md`).
- [ ] `NOVA_IMAGE_TAG` and `NOVA_SOURCE_REVISION` set to the release; previous tag and manifest kept for rollback.
- [ ] `$COMPOSE config -q`, `$COMPOSE build`, `$COMPOSE run --rm --no-deps migrate python -m app.release check-config` (no errors).
- [ ] `$COMPOSE up -d`; `migrate` exited 0; `api` and `web` healthy.
- [ ] `/api/v1/ready` is 200 with `migrations: ok`.
- [ ] `python3 scripts/deployment_smoke.py --url <base> --web-url <base>` exits 0 (add `--write` on staging).
- [ ] `$COMPOSE exec api python -m app.release history` shows the deployment.
- [ ] If anything fails: `ROLLBACK_AND_RECOVERY.md`.

## D. After deploying

- [ ] Log in through the UI with the token; open Repositories, one repository's Intelligence and Health pages.
- [ ] Backups scheduled and the first scheduled backup verified and copied off-host.
- [ ] External uptime check on `/api/v1/ready` (nothing in NOVA pages anyone).

## Updating pinned images

Base images are pinned as `name:tag@sha256:digest` in `backend/Dockerfile`, `frontend/Dockerfile` and `deployment/docker-compose.tls.yml`.
To update (monthly, or for a security advisory):

1. Pick the newest **patch** release of the same line (Python 3.12, Node 22, nginx 1.30 stable, Caddy 2.x). Do not change major/minor lines in a patch release.
2. Resolve the multi-arch digest: `docker buildx imagetools inspect python:3.12.X-slim-bookworm` (the `Digest:` line), or the `digest` field from
   `https://hub.docker.com/v2/repositories/library/python/tags/3.12.X-slim-bookworm`.
3. Replace both the tag and the digest; let CI (`containers` job) build and smoke-test it; deploy like any release.

GitHub Actions are pinned to commit SHAs the same way: resolve with `git ls-remote --tags https://github.com/actions/checkout 'refs/tags/v4.*'`, update the
SHA and the trailing tag comment. Dependabot or Renovate can automate both.
