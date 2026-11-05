# NOVA Architecture Overview

NOVA is a self-hosted developer platform: a FastAPI backend with a SQLite database, and a React single-page app. By default nothing
runs outside the host and no source code is transmitted anywhere; opt-in integrations and AI-assisted repair proposals (`NOVA_AI_PROVIDER=anthropic`,
see `AI_ASSISTED_REPAIR.md`) send data only when an administrator enables them and a user requests the action.

```
Browser (React/TS)  --/api/v1-->  FastAPI  -->  services  -->  SQLAlchemy -> SQLite
   Vite dev proxy                 api/         indexer          (Alembic migrations)
                                  security/    planner
                                              git (subprocess wrapper)
                                              local filesystem (read-only, bounded)
```

## Real directory structure
```
backend/
  app/api/        HTTP routes (health, workspace/overview, repositories, tasks)
  app/core/       settings (NOVA_* env), logging, error envelope
  app/db/         declarative base, engine/session
  app/models/     ORM entities (metadata only; no file contents)
  app/schemas/    Pydantic request/response models
  app/security/   path boundary, secret-file rules, auth/roles
  app/services/   indexer, git wrapper, planner, plan-provider interface, repository & task services, audit
  migrations/     Alembic environment + 0001_initial_schema
  tests/          pytest suite
frontend/src/
  app/            App shell: router, layout, error boundary
  components/     reusable UI primitives
  pages/          the 10 screens (feature code is small enough to live with its page)
  services/       typed API client, formatting helpers
  types/          API contract types (mirror backend schemas)
  styles/         Tailwind entry
  test/           vitest suites (+ optional live-backend contract test)
docs/ scripts/ deployment/ (deployment/ is reserved and currently only holds a README)
```

## Data model
`Workspace` 1–n `Membership` n–1 `User` · `Workspace` 1–n `Repository` 1–n `IndexSnapshot` 1–n `IndexedFile` ·
`Repository` 1–n `Task` 1–1 `TaskPlan` · `AuditEvent` per workspace.

- Identifiers are 32-char hex UUIDs; timestamps are timezone-aware UTC.
- `IndexedFile` stores relative POSIX path, extension, language, size, line count and SHA-256 — **never contents**.
- Only the newest completed snapshot keeps its file rows; older snapshots keep their summary rows.
- `TaskPlan.content` is a validated JSON document (`PlanContent`), versioned by `revision`.
- A verification-run table is intentionally **not** created yet (no implemented use yet).

## Key flows
**Register** → `resolve_repository_root` (absolute, exists, directory, canonical, not a filesystem/home root, optional allow-list) → git
metadata via the controlled wrapper → row + audit event.
**Index** → `POST /repositories/{id}/index` creates a `running` snapshot and returns `202`; a background task scans and writes
file rows in batches, then marks the snapshot `completed` or `failed`. The UI polls the snapshot. One run per repository at a time (`409`).
**Task + plan** → task text is validated; the planner ranks indexed file *paths* by keyword overlap, detects manifests, and emits a
`PlanContent`. Plans are editable (`PUT`) and regenerable (`POST …/regenerate`, which discards edits and is confirmed in the UI).

## Planner (deterministic baseline)
Tokenizes task text (camelCase/snake_case aware, stop-words removed), scores file paths (file-name hit ×3, directory hit ×1, explicit
path mention +20), picks candidate source and test files, infers suggested checks from root manifests, and sets a confidence level.
It reads no file contents, runs nothing, and is fully deterministic. A `PlanProvider` interface exists so can add other
providers; only `none` is accepted by configuration today.

## API conventions
`/api/v1/...`; errors use `{"error": {"code", "message", "details?"}}`; lists use `{items,total,page,page_size,pages}`; OpenAPI docs at
`/docs`. `GET /health` is liveness; `GET /ready` checks DB connectivity **and** that migrations are at head.

## Database path
SQLite by default (`backend/data/nova.db`). To use PostgreSQL later: install a driver (e.g. `psycopg`), set `NOVA_DATABASE_URL`,
run `alembic upgrade head`. The models use portable types (JSON, String, BigInteger). **Not verified** — only SQLite was tested.

## Technology choices (verified versions are pinned in the requirement/lock files)
Python 3.12 (tested; 3.11+ supported by code but 3.11 was not run), FastAPI, Pydantic 2, SQLAlchemy 2, Alembic, pathspec; React 19,
TypeScript 5.9 (strict), Vite 8, Tailwind 4, TanStack Query, React Router 7, Vitest, ESLint.
