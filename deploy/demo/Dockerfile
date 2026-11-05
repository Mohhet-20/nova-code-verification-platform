# syntax=docker/dockerfile:1
# Hosted public demo of NOVA (render.yaml). Build context: the repository root.
# The showcase (NOVA's own backend, indexed, analysed, scanned and reviewed) is produced HERE, at build time, by
# scripts/demo_build.py; scripts/demo_start.py copies it fresh on every start and runs the API in production mode
# with guest access on and repository test execution off. For a real deployment use backend/Dockerfile instead.
ARG PYTHON_IMAGE=python:3.12.15-slim-bookworm@sha256:6a0e06f5ba0b373d051aabd3fffb9b873f27f9a2a3a14bcbe37dee6f0e3bbc70

FROM ${PYTHON_IMAGE} AS builder
ENV PIP_NO_CACHE_DIR=1 PIP_DISABLE_PIP_VERSION_CHECK=1
WORKDIR /build
COPY backend/requirements.txt .
# httpx: the build drives the API through FastAPI's TestClient to produce the showcase.
RUN python -m venv /opt/venv && /opt/venv/bin/pip install -r requirements.txt httpx==0.28.1

FROM ${PYTHON_IMAGE} AS runtime
RUN apt-get update \
    && apt-get install -y --no-install-recommends git \
    && rm -rf /var/lib/apt/lists/* \
    && groupadd --system --gid 10001 nova \
    && useradd --system --uid 10001 --gid nova --home-dir /tmp/home --no-create-home --shell /usr/sbin/nologin nova \
    && mkdir -p /app/showcase && chown nova:nova /app/showcase
ENV PATH=/opt/venv/bin:$PATH PYTHONUNBUFFERED=1 PYTHONDONTWRITEBYTECODE=1 HOME=/tmp/home NOVA_DEMO_ROOT=/tmp/nova
COPY --from=builder /opt/venv /opt/venv
WORKDIR /app
COPY backend/alembic.ini backend/requirements.txt backend/pyproject.toml ./backend/
COPY backend/app ./backend/app
COPY backend/tests ./backend/tests
COPY backend/migrations ./backend/migrations
COPY scripts/demo_build.py scripts/demo_start.py ./scripts/
USER 10001:10001
RUN python scripts/demo_build.py && cp -a /tmp/nova/. /app/showcase/ && rm -rf /tmp/nova
EXPOSE 8000
CMD ["python", "scripts/demo_start.py"]
