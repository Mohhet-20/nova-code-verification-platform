# Repository Intelligence

How NOVA turns a registered directory into a queryable, honest model of its code. This builds on the
inventory; see `INDEXING_DATA_MODEL.md` for tables and `docs/security/REPOSITORY_INDEXING_THREAT_MODEL.md` for the
safety analysis.

## Pipeline

```
register -> POST /index -> [ discover -> detect language -> parse -> persist -> resolve -> publish ]
```

1. **Discovery** (`services/indexer.py`). Walks the tree without following symlinks or reparse points, honours
   nested `.gitignore` files, built-in exclusions and `NOVA_INDEX_EXTRA_EXCLUDES`, and stops at configured limits
   (files, depth, per-file bytes, total bytes, time budget). Every skip has a stable reason code:
   `IGNORED_PATTERN`, `BINARY_FILE`, `FILE_TOO_LARGE`, `UNSUPPORTED_ENCODING`, `OUTSIDE_REPOSITORY`,
   `SYMLINK_REJECTED`, `SECRET_LIKE_PATH`, `UNSUPPORTED_FILE_TYPE` (plus `UNREADABLE`, `MAX_DEPTH_EXCEEDED`,
   `DUPLICATE_FILE`). Credential-like files are counted but their names are never recorded.
2. **Language detection** (`intelligence/language_detection.py`). From file name, extension and (for extensionless
   scripts) a shebang. Ambiguous extensions (`.h`, `.m`, `.pl`, `.v`) are reported with `low` confidence and
   alternatives. Detection says what a file *is*; it does not imply NOVA can parse it.
3. **Parsing** (`intelligence/symbol_extractor.py`, `extractors/`). Tree-sitter grammars for Python, JavaScript, JSX,
   TypeScript and TSX. Parsing is syntactic: source is read as bytes, parsed as data, never executed or imported.
4. **Persist** into a *new generation* (staged under a snapshot whose status is still `running`).
5. **Resolve** imports across the whole new file set and derive relationships.
6. **Publish** atomically: status flips and the previous generation is deleted in one commit.

## Capability levels (what NOVA claims)

| Language | Level | Notes |
|---|---|---|
| Python | structural (tree-sitter) | classes, functions, methods, constructors, fields, constants, variables, nested defs, `__all__`, docstring summaries, imports with relative levels |
| JavaScript / JSX | structural | functions, arrow-function consts, classes (+`extends`), methods, fields, import/export/re-export/`require("literal")`, doc comments |
| TypeScript / TSX | structural | the above plus interfaces, type aliases, enums, namespaces, type-only imports |
| everything else | metadata only | counted and listed, no symbols, no import analysis |

`GET /api/v1/capabilities` reports the same table live, including whether each parser could actually be loaded.
**Nothing here is compiler-accurate.** There is no type resolution. Code intelligence adds a name-based call graph and dynamic-import detection (see `CODE_INTELLIGENCE_GRAPH.md`).

## Per-file analysis status

`parsed` (clean parse) · `partial` (syntax errors; only valid regions extracted) · `failed` (parser crashed or
exceeded bounds) · `unavailable` (a parser exists but cannot be loaded here) · `unsupported` (no parser; metadata only).
Files that are `failed` or `unavailable` are re-analyzed on the next run even if their content is unchanged.

## Symbols

Normalized record: name, qualified name, kind, language, start/end line and column (1-based; columns are characters,
not bytes), parent, visibility, exported flag, signature (first line, bounded), doc summary (first sentence, bounded),
base names, parser, confidence. **Stable IDs** are `sha256(repository_id, rel_path, local_key, kind)[:32]` where
`local_key` is `<nesting path>#<ordinal among same-named siblings>`: they survive edits elsewhere in the file and in
other files, but change if the symbol is renamed, moved, or reordered among same-named siblings.

## Imports and relationships

Raw import records are stored as observed. Resolution is conventional, not compiler-accurate:

* Python: relative levels; roots tried are the top package root, repo root, `src`, `lib`; `from pkg import submodule`
  also links the submodule; standard library (via `sys.stdlib_module_names`) and packages declared in
  `pyproject.toml` / `requirements*.txt` are **external**; anything else is **unresolved**.
* JS/TS: relative specifiers with extension/index probing (including `.js` → `.ts`); `node:` and Node built-ins and
  packages declared in the nearest `package.json` are external; `@/…`, `~/…`, `#…` aliases and absolute paths are
  **unsupported** (path-mapping is not interpreted); undeclared bare specifiers are unresolved.

Relationship types: `IMPORTS_FILE` (observed), `DEPENDS_ON_PACKAGE` (observed, declared/external packages only),
`EXTENDS_SYMBOL` (**inferred**: base name bound in the same file or through a directly resolved import).
`CALLS`, `REFERENCES`, `IMPLEMENTS` are explicitly **not supported** and are never fabricated.
Resolution runs globally on every run, so adding or removing a file correctly changes other files' edges.

## Incremental indexing

Each run builds a complete new generation. A file whose sha256 and `analysis_version` match the previous generation
(and whose language and status allow reuse) has its symbols and raw imports copied instead of re-parsed. Statuses:
`new`, `changed`, `unchanged`; removed files are counted (`files_removed`, `null` when the scan was truncated, because
removal cannot be established). A rename is a delete plus an add. `mode: "full"` re-parses everything. Content is read
and hashed on every run, so incremental mode saves parsing, not I/O. See `docs/performance/BENCHMARKS.md` for
measured costs.

## Run outcomes

`completed` · `completed_with_warnings` (truncated scan, partial/failed/unavailable files, diagnostics truncated) ·
`failed` (previous index untouched). Only `completed*` generations are queryable.

## Failure and concurrency

* One `running` snapshot per repository, enforced by a partial unique index; a racing request gets `409`.
* A failed or crashed run deletes only its own staged rows; the last good generation keeps serving. Startup recovers
  snapshots left `running` by a crash.
* The newest 10 snapshot rows per repository are kept for history; file/symbol rows exist only for the published one.

## API (all under `/api/v1`, bearer token if configured)

`POST /repositories/{id}/index` (`{mode}`), `GET …/summary`, `…/snapshots`, `…/snapshots/{sid}`,
`…/snapshots/{sid}/diagnostics`, `…/freshness`, `…/files` (+`q`, `language`, `analysis_status`, `change_status`),
`…/files/{fid}`, `…/files/{fid}/imports|dependencies|dependents`, `…/symbols`, `…/symbols/search`,
`GET /capabilities`. Search results say why they matched (`matched_on`), include the index generation, and report
ambiguity rather than guessing. File and symbol IDs belong to a generation; after a re-index, old file IDs return 404.

**Freshness** compares size and modification time with the index (no hashing, no reading). It is an estimate: touched
but identical files count as changed, same-size edits within the same mtime tick are missed, and "new" may include
files the indexer would skip.
