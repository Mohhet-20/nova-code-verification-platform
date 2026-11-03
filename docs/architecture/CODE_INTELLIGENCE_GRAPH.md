# Code Intelligence: Call Graph, Dependency Analysis and Impact Assessment (Phase 03)

Phase 03 adds call-site extraction, a persisted dependency graph, graph algorithms (traversal, cycles, depth), change-impact
analysis, an index-status/rebuild workflow and a metrics endpoint on top of the Phase 02 index.
Read `REPOSITORY_INTELLIGENCE.md` and `INDEXING_DATA_MODEL.md` first; this document only covers what Phase 03 adds or changes.

**Everything here is static analysis of source text.** Nothing is executed or imported. Results are estimates; the API and UI say so.

## 1. Parser and indexing architecture

```
discover -> detect language -> parse (tree-sitter) -> extract {symbols, imports, calls, dynamic imports}
         -> persist into a new generation -> resolve imports (global) -> resolve calls (global) -> derive relationships -> publish
```

* **Parser abstraction.** `parser_registry.py` holds per-language capabilities and grammar loading. `register_extractor()` binds a
  language to an extractor function with the signature `extract(data: bytes, rel_path: str, parser, max_symbols: int = 5000) -> ParseResult`.
  `ParseResult(status, parser, symbols, imports, diagnostics, calls)` now carries `calls` as well. Extractors never raise; failures become an explicit
  per-file analysis status (`parsed`, `partial`, `failed`, `unavailable`, `unsupported`).
* **Python uses tree-sitter, not `ast`.** The specification suggests Python's `ast`; I kept tree-sitter so Python and TypeScript share one error-tolerant
  extractor shape and a file with a syntax error still yields its valid regions (`ast` rejects the whole file). Trade-off: no
  `ast`-level semantics (for example decorators are not interpreted). This is a deliberate deviation.
* **Call extraction** (`extractors/calls.py`). One iterative traversal per file (no recursion) that tracks the enclosing function, method,
  constructor or class and records each call as `(caller_key, callee_text, line)`. The callee text is kept only when it is a plain dotted name
  (`helper`, `self.go`, `mod.fn`); any other expression (`a[0]()`, `f()()`, `(x or y)()`) is recorded with no callee and later marked `unsupported`.
  Limits: 5,000 calls per file (a diagnostic note says the rest were not recorded); syntax-error regions are skipped.
* **Dynamic imports** are recorded as `file_imports` rows with `kind = "dynamic"` and resolution status `unsupported`:
  JavaScript `import(...)` and `require(<non-literal>)`; Python `__import__`, `importlib.import_module`, `import_module`.
  They never become edges.
* **Supported languages:** Python, JavaScript, JSX, TypeScript, TSX (structural). Everything else is metadata only.
* **Analysis version.** `ANALYSIS_VERSION` is now 2 (call extraction added). Unchanged files are reused only when their stored analysis
  version equals the current one, so the first index after upgrading re-parses everything automatically.

## 2. Data model additions (migration `0003`)

| Table / column | Purpose |
|---|---|
| `file_calls` | Observed call sites: `snapshot_id`, `file_id`, `caller_stable_id` (NULL = module level), `callee`, `line`, `resolution_status`, `resolution_note`, `candidate_count`. Index on `(snapshot_id, resolution_status)`. |
| `relationships.line` | Source line of the edge (import statement or call site). Nullable. |
| Indexes on `relationships` | `(snapshot_id, target_symbol_stable_id)`, `(snapshot_id, source_symbol_stable_id)`, `(snapshot_id, type)` for reverse traversal and metrics. |

Relationship types (stored vs. the vocabulary in the prompt):

| Specification term | Stored as | Notes |
|---|---|---|
| IMPORTS | `IMPORTS_FILE` (file to file), `DEPENDS_ON_PACKAGE` (external) | observed |
| DEFINES | not stored | derived: `symbols.parent_stable_id` and `symbols.file_id` |
| CALLS | `CALLS` (symbol or module to symbol) | **inferred** from names; `line` = call site |
| INHERITS | `EXTENDS_SYMBOL` | inferred; same-file or directly imported bases |
| REFERENCES | not provided | not genuinely extractable without type/scope resolution; reported as unsupported |

Every edge carries source, target, type, status (`resolved`, `ambiguous`, `external`, `unresolved`, `unsupported`), evidence (`observed` / `inferred`),
file and line, and belongs to a snapshot (generation). Unresolved and unsupported imports and calls stay in `file_imports` / `file_calls` with a note; they are
never edges. Edge confidence is expressed by `status` plus `evidence`; there is no numeric score because none would be defensible.

## 3. Resolution rules

**Imports** are resolved globally on each run (Phase 02 rules: relative levels, packages, `index` files, extension probing, declared dependencies).

**Calls** (`call_resolver.py`) use only names, same-file scope, direct import bindings and simple inheritance:

1. Bare name `f()`: a function or class defined in the same file (top level or in an enclosing scope), else an imported name
   (`from m import f`, `import { f } from "./m"`), else `unresolved`.
2. `self.x()`, `cls.x()`, `this.x()`: a method of the enclosing class, following same-file or directly-imported base classes up to depth 5.
3. `alias.name()`: `name` in the module bound to `alias`. `Class.method()`: for same-file or imported classes.
4. A name bound to an external package is `external` (not an edge). More than two segments (`a.b.c()`) is `unsupported`. Unknown receiver is `unresolved`.
5. Several candidates (up to 5) are `ambiguous`: an edge to each candidate with status `ambiguous`; nothing is guessed.

Not modelled: types, dynamic dispatch, monkey-patching, decorators that rewrap callables, higher-order calls (`map(f, xs)` records the call to `map`, not to `f`),
re-export chains, and names introduced by `exec`/`eval`/`getattr`.

## 4. Algorithms (`intelligence/graph.py`, pure; database access is in `graph_queries.py`)

| Operation | Method | Complexity | Bounds |
|---|---|---|---|
| Dependencies / dependents / impact | frontier BFS, one SQL `IN` query per level | O(visited + edges examined) | `depth` 1 to 10; hard node cap 5,000 (`truncated = true` when hit); `more_beyond_depth` when deeper nodes exist |
| Cycle detection | iterative Tarjan SCC over resolved `IMPORTS_FILE` edges | O(V + E) | edges loaded capped at 500,000 (`note` says so if hit) |
| Max dependency depth | longest path (in edges) on the SCC condensation DAG | O(V + E) | none beyond the edge cap |

* **Determinism:** frontiers and edges are sorted; a node reached by several paths is reported once at its shortest depth via its lexicographically smallest
  discovering edge. Same index in, same output out.
* **Cycles cannot cause infinite traversal:** each node is visited once. Impact results in a cycle also return `cycles` warnings.
* **Cycle severity** (documented rule, structural not behavioural): self-import `info`; 2 to 3 files `medium`; 4 or more files `high`. A "structural dependency
  cycle" means the files can reach each other through resolved imports. Many ecosystems tolerate import cycles; this is a coupling indicator, not proof of failure.
* **Impact:** for a file, reverse `IMPORTS_FILE` traversal. For a symbol, reverse `CALLS` and `EXTENDS_SYMBOL` traversal (module-level callers appear as leaf `module` items).
  Each item has depth, relationship, status, evidence, line, the chain of names from the changed target to the item, and an explanation sentence. Responses always include
  the static-analysis disclaimer.
* **No N+1:** a traversal is O(depth) queries plus one batched lookup for paths/names.

## 5. Incremental behaviour, consistency and recovery

* Unchanged files (same SHA-256, language and `analysis_version`, previous status not `failed` / `unavailable`) have symbols, imports and raw call sites copied
  into the new generation with resolution reset; resolution (imports, calls, relationships) is recomputed globally each run, so an added, modified or deleted file is
  reflected in every edge that touches it, including edges between files that did not change.
* The new generation is staged under a `running` snapshot and published atomically. **A failed run deletes its staged rows and leaves the previous generation serving
  queries** (`GET /index/status` then reports `state = "failed"`, `serving` = the old generation, and the failure message). One running index per repository (409 otherwise).
  A process killed mid-run leaves a `running` snapshot that startup recovery marks `failed`.
* **Re-running without changes is idempotent:** counts, edges and cycles are identical; only the generation number increases (tested).
* `POST /index/rebuild` is the recovery operation: it forces `mode = full`, re-parsing every file and ignoring reuse. Use it after a parser or analysis change, or if you suspect a bad index.
  The old generation keeps answering until the rebuild publishes.

### How to rebuild after a parser change
1. Change the extractor and bump `ANALYSIS_VERSION` in `intelligence/types.py` (this alone makes the next incremental run re-parse every file).
2. For an unversioned fix or a grammar upgrade, call `POST /api/v1/repositories/{id}/index/rebuild` (or use "Rebuild index" in the UI).
3. Confirm `GET /index/status` shows `completed` and the new `generation`.

### How to add a language adapter
1. Add the grammar package to `requirements.txt` (pinned) and a `LanguageCapability` entry plus a `_GRAMMARS` entry in `parser_registry.py` (declare honestly: `semantic=False`).
2. Write `extractors/<lang>_extractor.py` returning `ParseResult(status, parser_name, symbols, imports, diagnostics, calls)`; reuse `FileContext` and `collect_calls` where the grammar's
   node names allow, and call `register_extractor(("<Language>",), extract)` from `extractors/__init__.py`.
3. Add import resolution in `import_analyzer.py` if the language's module system differs, and a binding rule in `pipeline._bind` for call resolution (otherwise calls stay `unresolved`).
4. Add fixtures and tests to `tests/test_symbol_extraction.py` and `tests/test_graph_intelligence.py`; bump `ANALYSIS_VERSION`.

## 6. API (all under `/api/v1/repositories/{repository_id}`; all require the API token when one is configured)

| Method and path | Purpose |
|---|---|
| `GET /symbols?q=&kind=&language=&file_id=&exported=&page=&page_size=` | paginated symbol list (`q` is a literal case-insensitive substring) |
| `GET /symbols/{symbol_id}` | detail by stable id: children, callers, callees, bases, subclasses, unresolved call count |
| `GET /dependencies?path=\|file_id=&direction=out\|in&depth=1..10&page=&page_size=` | direct and transitive dependencies or dependents with chains; unresolved and dynamic imports listed separately |
| `GET /dependencies/impact?path=\|file_id=\|symbol_id=&max_depth=1..10&page=&page_size=` | change impact with explanations, cycle warnings and disclaimer |
| `GET /dependencies/cycles?severity=&min_size=&page=&page_size=` | structural cycles with severity rules |
| `GET /index/status?include_pending=` | `never_indexed`, `running`, `completed`, `completed_with_warnings`, `failed`; the generation being served; files awaiting indexing |
| `POST /index` (Phase 02) / `POST /index/rebuild` | incremental / full indexing (202; 409 if one is running) |
| `GET /metrics?include_pending=` | counts, edge/import/call breakdowns, cycles, max depth, parse failures, and a `definitions` map explaining each metric |

Errors use the standard envelope. Invalid paging or enum values give 422; a path containing `..`, an absolute path, a drive prefix or NUL gives 400 `unsafe_path`
(the path is only ever compared against indexed paths, never opened); unknown ids give 404; no `path`/`file_id` (or both) gives 400.

Example (PowerShell):

```powershell
$base = "http://127.0.0.1:8000/api/v1/repositories/<repo-id>"
Invoke-RestMethod "$base/dependencies/impact?path=src/auth/login.py&max_depth=3" | ConvertTo-Json -Depth 6
Invoke-RestMethod "$base/dependencies/cycles?severity=high"
Invoke-RestMethod "$base/index/status?include_pending=true"
Invoke-RestMethod -Method Post "$base/index/rebuild"
# with a token:  -Headers @{ Authorization = "Bearer $env:NOVA_API_TOKEN" }
```

## 7. Running and testing

Windows PowerShell (from `F:\last proj`):

```powershell
powershell -ExecutionPolicy Bypass -File scripts\setup.ps1        # venv, deps, migrations (includes 0003), npm ci
cd backend; .\.venv\Scripts\python.exe -m alembic upgrade head
.\.venv\Scripts\python.exe -m uvicorn app.main:app --host 127.0.0.1 --port 8000
# second terminal
cd frontend; npm run dev                                           # UI: /repositories/<id>/intelligence
```

Checks: `cd backend; .\.venv\Scripts\python.exe -m ruff check .; .\.venv\Scripts\python.exe -m pytest` and `cd frontend; npm run typecheck; npm run lint; npm test; npm run build`
(or `scripts\check-all.ps1`). Phase 03 tests: `tests/test_graph_intelligence.py` (29) and `src/test/intelligence.test.tsx` (11). The PowerShell commands were not executed on
Windows (see `PROJECT_STATUS.md`).

## 8. Limits (honest list)

* Name-based call resolution: recall is limited and unresolved calls are common for method calls on non-`self` receivers (the type of `obj` is unknown).
* No `REFERENCES`, `IMPLEMENTS`, type-aware or cross-package call resolution; no tsconfig path aliases; no dynamic imports as edges.
* File-level graph (dependencies, dependents, cycles, depth) uses `IMPORTS_FILE` only; a call without an import (for example via a global) has no edge.
* `CALLS` edges are evidence of a *probable* call by name, not of execution.
* Impact traversal stops at 5,000 nodes and depth 10; cycle analysis reads at most 500,000 edges.
* Graph endpoints recompute per request (no cache). Measured at 3,000 files in the performance table in `VERIFICATION_LOG.md`.
