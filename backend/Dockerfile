# syntax=docker/dockerfile:1
# NOVA API image. Status: the build has NOT been executed in the authoring environment (no Docker engine available); the equivalent runtime layout
# (Python 3.12, non-root user, read-only code tree, /data volume, same environment and command) was exercised in WSL - see
# docs/operations/FINAL_DEPLOYMENT_REPORT.md. Base images are pinned by tag AND digest (resolved from Docker Hub on 2026-10-22).
# Update procedure: docs/operations/RELEASE_CHECKLIST.md "Updating pinned images".
ARG PYTHON_IMAGE=python:3.12.15-slim-bookworm@sha256:6a0e06f5ba0b373d051aabd3fffb9b873f27f9a2a3a14bcbe37dee6f0e3bbc70

FROM ${PYTHON_IMAGE} AS builder
ENV PIP_NO_CACHE_DIR=1 PIP_DISABLE_PIP_VERSION_CHECK=1
WORKDIR /build
COPY requirements.txt .
RUN python -m venv /opt/venv && /opt/venv/bin/pip install -r requirements.txt

FROM ${PYTHON_IMAGE} AS runtime
ARG NOVA_SOURCE_REVISION=unknown
# git: code review, CI change detection and repository metadata call the git CLI (argument arrays only; see app/services/git.py).
RUN apt-get update \
    && apt-get install -y --no-install-recommends git \
    && rm -rf /var/lib/apt/lists/* \
    && groupadd --system --gid 10001 nova \
    && useradd --system --uid 10001 --gid nova --home-dir /data/home --no-create-home --shell /usr/sbin/nologin nova \
    && mkdir -p /data /app /srv/repos && chown nova:nova /data
ENV PATH=/opt/venv/bin:$PATH PYTHONUNBUFFERED=1 PYTHONDONTWRITEBYTECODE=1 HOME=/data/home \
    NOVA_ENVIRONMENT=production NOVA_SOURCE_REVISION=${NOVA_SOURCE_REVISION} \
    NOVA_DATABASE_URL=sqlite:////data/nova.db NOVA_ARTIFACT_DIR=/data/artifacts NOVA_LOG_JSON=true \
    NOVA_BACKUP_DIR=/data/backups NOVA_RELEASE_STATE_DIR=/data/release-state
LABEL org.opencontainers.image.title="nova-api" org.opencontainers.image.revision="${NOVA_SOURCE_REVISION}"
COPY --from=builder /opt/venv /opt/venv
WORKDIR /app
COPY --chown=root:root alembic.ini ./
COPY --chown=root:root app ./app
COPY --chown=root:root migrations ./migrations
USER 10001:10001
VOLUME ["/data"]
EXPOSE 8000
STOPSIGNAL SIGTERM
# Liveness only (cheap, no dependencies). Readiness is /api/v1/ready and is what deploy automation waits for.
HEALTHCHECK --interval=15s --timeout=3s --start-period=20s --retries=3 CMD ["python", "-c", "import urllib.request,sys; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8000/api/v1/health', timeout=2).status == 200 else 1)"]
# Migrations are a separate release step (`python -m app.release deploy`); the API refuses to start on a stale schema in production.
# One worker process: SQLite allows one writer and the job/throttle state is per process (docs/operations/ARCHITECTURE_AND_NETWORKING.md).
# Behind the bundled web proxy set FORWARDED_ALLOW_IPS so client addresses (used by auth throttling and logs) come from X-Forwarded-For.
CMD ["uvicorn", "app.main:create_app", "--factory", "--host", "0.0.0.0", "--port", "8000", "--workers", "1", "--proxy-headers", "--timeout-graceful-shutdown", "20", "--timeout-keep-alive", "5", "--no-server-header", "--no-access-log"]
