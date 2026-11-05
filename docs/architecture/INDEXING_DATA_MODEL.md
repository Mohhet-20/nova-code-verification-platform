# Indexing Data Model (migration `0002`)

All tables use string UUID keys. Every child row carries its generation (`snapshot_id`) and, where useful for
isolation, `repository_id`. Foreign keys cascade on delete. SQLite batch mode is used for portability.

| Table | Purpose | Key columns / constraints |
|---|---|---|
| `index_snapshots` | One indexing run == one generation | `status` (running/completed/completed_with_warnings/failed), `generation`, `mode`, change counts (`files_new/changed/unchanged/removed(nullable)`), `files_failed`, `files_skipped`, `symbols_count`, `imports_total`, `imports_unresolved`, `relationships_count`, `analysis_counts` JSON, `skipped_counts` JSON, `warnings` JSON, `duration_ms`. **Partial unique index** `(repository_id) WHERE status='running'`. |
| `indexed_files` | Files of a generation | unique `(snapshot_id, rel_path)`; `repository_id` (NOT NULL, FK), `sha256`, `mtime_ns`, `change_status`, `language`, `language_confidence`, `analysis_status`, `parser`, `analysis_version`, `symbol_count`, `import_count`, `parse_diagnostics` JSON |
| `symbols` | Extracted definitions | unique `(snapshot_id, stable_id)`; indexes on `(snapshot_id, name_lower)`, `(snapshot_id, qualified_lower)`, `(snapshot_id, kind)`; 1-based line/column range, `parent_stable_id`, `exported`, `signature`, `doc_summary`, `bases` JSON, `parser`, `extraction_method`, `confidence` |
| `file_imports` | Raw import/export records + resolution | `kind` (import/export/reexport/require), `specifier`, `names` JSON, `level`, `line`, `type_only`, `resolution_status` (resolved/external/unresolved/unsupported), `resolution_note`, `package`, `resolved_file_id` (SET NULL) |
| `relationships` | Derived edges | `type` (IMPORTS_FILE/DEPENDS_ON_PACKAGE/EXTENDS_SYMBOL), source/target file ids, source/target symbol stable ids, `target_text`, `status`, `evidence` (observed/inferred); indexes on `(snapshot_id, target_file_id)` and `(snapshot_id, source_file_id)` |
| `index_diagnostics` | Bounded per-run report | `rel_path` (NULL for credential-like paths), `code`, `severity`, `detail` (short, never source text) |

Not stored: file contents, comments beyond a bounded doc-summary sentence, string-literal values, environment values,
or the names of credential-like files.

## Migration behaviour

* `0002` adds columns with server defaults, creates the four new tables, and **backfills** `indexed_files.repository_id`
  from the owning snapshot before making it NOT NULL, so data survives.
* `migrations/env.py` disables SQLite foreign-key enforcement during the migration (batch table rebuilds would
  otherwise cascade-delete child rows), commits explicitly, then runs `PRAGMA foreign_key_check` and aborts if it
  finds violations.
* Verified: upgrade `0001 → head` with seeded data (row preserved, `repository_id` backfilled), downgrade to `0001`,
  upgrade again, and `alembic check` reports no drift. Downgrade drops tables and columns by design.
* PostgreSQL: the schema avoids SQLite-only features (the partial index has a PostgreSQL `WHERE`), but it has **not**
  been run against PostgreSQL.

## Generation lifecycle

1. `begin_indexing` inserts a `running` snapshot (409 on a race).
2. The pipeline writes `indexed_files`, `symbols`, `file_imports` in batches (core multi-row inserts), commits
   progress periodically; none of this is visible to queries.
3. Resolution fills `file_imports.resolution_*` and inserts `relationships`.
4. Publish transaction: set final status and counts, delete all other generations' child rows then file rows for
   the repository, prune snapshot rows beyond the newest 10, write the audit event, commit.
5. On error: roll back, delete this snapshot's staged rows, mark it `failed` with a bounded message.
