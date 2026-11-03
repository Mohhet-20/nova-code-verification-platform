# Verification log

Only results from commands that were actually executed are recorded. "Not run" means exactly that.

**Environment used for all runs:** Linux 6.8 (x86_64 VM, 2 CPUs), Python 3.12.14, Node via npm in a copy of `frontend/`
(`~/fe-build`, because `node_modules` is not installed on the Windows mount). Project folder mounted at `F:\last proj`.
Key versions: FastAPI 0.143.5, SQLAlchemy 2.1.5, Alembic 1.20.0, pytest 9.1.1, ruff 0.17.1, tree-sitter 0.26.0,
tree-sitter-python 0.25.0, tree-sitter-javascript 0.25.0, tree-sitter-typescript 0.23.2; frontend React 19.3.1, Vite 8.3.4, TypeScript 5.9.3, Vitest 5.0.5.

## Phase 01 (transcribed from the earlier session; not re-run in this exact form)

| Check | Result |
|---|---|
| Backend `ruff check .` and `pytest` from a fresh venv and fresh database | 120 passed, ruff clean |
| Frontend `tsc -b`, `eslint`, `vitest run`, `vite build` | all passed; 23 tests |
| Live contract run: real backend + UI (jsdom): register → index → task → plan | passed; 10 files indexed, credentials/binaries/dependency folders not stored |
| mypy | removed from the project: mypy 2.4.0 crashed with an internal error, so no static type checking of the backend exists |

## Phase 02

### Migration `0002` (manual script, not an automated test)
1. `alembic upgrade 0001`, inserted one workspace/repository/snapshot/`indexed_files` row with SQL, `alembic upgrade head`:
   the file row survived, `repository_id` was backfilled (`'r'`), `PRAGMA foreign_key_check` returned no violations.
2. `alembic downgrade 0001`: succeeded; the file row (`f`, snapshot `s`) and snapshot count were intact.
3. `alembic upgrade head` again, then `alembic check`: "No new upgrade operations detected."
4. Defect found and fixed during this check: `migrations/env.py` ran `PRAGMA` outside a committed transaction, so the version row was never saved and
   a second `upgrade` re-ran `0001`. Fixed with explicit commits. `test_models_match_migration` (automated) guards schema drift.

### Backend
| Command (in `backend/`) | Result |
|---|---|
| `ruff check .` | All checks passed |
| `python -m pytest` (existing venv) | 173 passed |
| Fresh venv: `uv venv --python 3.12`, `uv pip install -r requirements.txt -r requirements-dev.txt`, import of all four tree-sitter modules, `ruff check .`, `pytest` | imports ok; ruff clean; **173 passed** (75.8 s on the final run) |

Test distribution: Phase 01 files (adapted where skip codes/status changed) plus new `test_symbol_extraction.py` (14), `test_repository_intelligence.py` (17),
`test_api_intelligence.py` (14), `test_language_detection.py` (4), and 5 new discovery tests in `test_indexer.py`.
New tests cover: discovery reason codes, BOM/invalid encoding, Unicode/hidden names, bounded diagnostics, parsing (Python/TS/JSX/TSX), syntax errors,
empty files, duplicate names, unsupported and unavailable parsers, parser crash containment, deep nesting, symbol limit, import resolution kinds,
dependents/extends, idempotent re-runs, full vs incremental, change/add/remove reconciliation, stable symbol IDs, failed-run rollback, concurrent-run 409,
parser-unavailable recovery, time-budget truncation, truncated re-run (removed count unknown), cross-repository isolation, snapshot pruning,
no execution of repository code, search ranking/ambiguity/wildcards/invalid input, pagination/filters, diagnostics without secret names, freshness states,
empty repository, capabilities, auth on all new routes.

### Frontend (in a copy at `~/fe-build`)
| Command | Result |
|---|---|
| `npx tsc -b` | no errors |
| `npx eslint src` | no errors |
| `npx vitest run` | 31 passed, 1 skipped (the live test, which skips without env) |
| `npm run build` | built; JS 411.7 kB (124.0 kB gzip), CSS 24.6 kB |
| Live test: `uvicorn` on port 8123 with a throwaway DB and `scripts/make_sample_repo.py` output, then `NOVA_LIVE_API=… NOVA_LIVE_REPO_PATH=… npx vitest run src/test/live.test.tsx` | 1 passed (register → index → symbol search finds `src/auth/login.py` → analysis coverage shown → task → plan) |

Not run: any browser (Chrome/Edge/Firefox), Playwright (unusable in this environment), Windows, PostgreSQL, Python 3.11, `scripts/*.ps1`.

### Performance (`scripts/measure_indexing.py`, single run each, synthetic content)
Environment as above (2 CPUs, SQLite on a VM disk, timings include FastAPI TestClient overhead and background-task completion).

| Fixture | First index | Unchanged re-index | One file changed | Full re-index | Freshness | Symbol search (avg / max) |
|---|---|---|---|---|---|---|
| 60 files (720 symbols) | 0.46 s | 0.45 s | 0.39 s | 0.35 s | 0.17 s | 41 ms / 67 ms |
| 3,000 files (36,000 symbols, 3,000 imports, 1,000 relationships) | 7.5 s | 12.2 s | 12.1 s | 16.5 s | 0.46 s | 170 ms / 213 ms |

History: the first 3,000-file run took ~22 s because symbols were inserted as ORM objects; switching to core multi-row inserts and set-based
deletion of the old generation reduced it to 7.5 s. Incremental runs are slower than the first index for this fixture (see Known limit 1). No repeated
runs were made, so variance is unknown, and these numbers say nothing about other hardware or real-world repositories.
One earlier 3,000-file run failed with "database or disk is full" because the sandbox disk allowance was exhausted by old test artifacts; that was an environment
problem, resolved by cleaning temporary files, and not a product defect (the failure path correctly marked the run `failed`).

### Defects found and fixed during Phase 02 verification
* Migration runner never committed the Alembic version row (above).
* Core executemany with heterogeneous relationship rows silently wrote NULLs for missing keys → normalized key sets per chunk (caught by tests).
* `/capabilities` was initially unauthenticated → now requires the same token as all other routes (test tightened).
* Python `__all__` export record reported line 1 → now the real line.
* Local export lists were counted as unresolved imports → marked `unsupported` with a note.

## Phase 03

All commands run on the Linux VM described above (Python 3.12.14, 2 CPUs). Nothing was run on Windows, in a real browser, on PostgreSQL or on Python 3.11.

### Migration `0003` (manual script)
1. Built a database at head by indexing a 2-file repo through the API (2 relationships, 2 symbols, 1 `file_calls` row; edges `CALLS 1`, `IMPORTS_FILE 1`).
2. `alembic downgrade 0002`: succeeded; relationships (2) and symbols (2) intact, `relationships.line` removed, version `0002`.
3. `alembic upgrade head` again, then `alembic check`: "No new upgrade operations detected."; `PRAGMA foreign_key_check` returned nothing.
Caveat: downgrading discards `file_calls` and `relationships.line` data (re-indexing restores it). `test_models_match_migration` guards drift automatically.

### Backend
| Command (in `backend/`) | Result |
|---|---|
| `ruff check .` | All checks passed |
| `pytest` (full suite) | **204 passed** (173 before Phase 03 + 31 in `tests/test_graph_intelligence.py`; one Phase 02 assertion about `CALLS` being unsupported was updated) |
| `mypy app --cache-dir=<outside mount>` | 13 errors in 5 Phase 01 files; 0 in Phase 02/03 modules (not a project gate; see PROJECT_STATUS.md limit 14). Phase 03 introduced and then fixed several errors in its own modules during the run. |

`test_graph_intelligence.py` covers: BFS depth/cycle/determinism/node cap, Tarjan (cycles, self loops, 20,000-node chain without recursion), severity rules, call extraction and
dynamic imports, call limit, syntax-error regions, direct/transitive dependencies and dependents with chains, unresolved/dynamic imports listed but not counted, pagination and
invalid parameters (422/400/404), path-escape rejection (`../`, absolute, drive, NUL, backslash), file impact multi-level, symbol impact through calls and inheritance, symbol detail,
cycles with severity and no-cycle repositories, add/modify/delete/unchanged reindex and idempotence, failed-run reporting with the previous generation still serving and recovery,
rebuild re-parsing every file, index-status states, metrics values and definitions, empty and never-indexed repositories, auth on every new route (401 without token),
unknown-repository isolation (404), a 150-file chain with depth limits, and the traversal node cap flag.

### Frontend (in `~/fe-build`)
| Command | Result |
|---|---|
| `npx tsc -b` | no errors |
| `npx eslint src` | no errors (two `react-hooks/set-state-in-effect` errors found in the new page and fixed) |
| `npx vitest run` | 42 passed, 1 skipped (live test skips without env); 11 new tests in `intelligence.test.tsx` cover loading, error with retry, empty, success, symbol detail → impact hand-off, dependency exploration, rejected-path error, cycle warnings, cycles list, rebuild confirmation / no double submit, running state, failed-run message |
| `npm run build` | built; JS 434.8 kB (128.8 kB gzip), CSS 25.0 kB |
| Live test: `uvicorn` on port 8123, throwaway DB, `scripts/make_sample_repo.py` output, then `NOVA_LIVE_API=… NOVA_LIVE_REPO_PATH=… npx vitest run src/test/live.test.tsx` | 1 passed (previous flow plus: Code intelligence page → overview → symbol detail → dependency explorer → cycles → back) |

### Performance (`scripts/measure_indexing.py --files 3000`, single run, synthetic content)
First index 7.1 s; unchanged re-index 9.9 s; one file changed 10.4 s; full re-index 12.7 s; freshness 0.27 s; symbol search 83 ms avg / 130 ms max (36,000 symbols).
Phase 03 endpoints (single request each, no cache): metrics 173 ms, cycles 21 ms, dependencies depth 5 28 ms, dependents depth 5 8 ms, impact depth 10 15 ms, symbol detail 44 ms, symbol impact 11 ms.
The generated fixture has no call edges between functions and an acyclic, shallow import graph, so these numbers show the endpoint overhead, not worst-case traversals; the 150-file chain test is the only deep-graph measurement.
Single runs, so variance is unknown. Incremental runs remain slower than the first index (Phase 02 limit 1, unchanged).

### Defects found and fixed during Phase 03 verification
* `CAPABILITIES`/`/capabilities` still listed `CALLS` as unsupported (stale Phase 02 test caught it).
* Self-imports were silently dropped, so the documented "self-import = info" cycle could never occur; now stored as self-edges and excluded from traversals.
* `IMPORTS_FILE` edges had no line even though the specification requires file/line evidence; now recorded.
* Rebuild test initially asserted `files_unchanged == 0`; that field is content-based, so the test now counts parser invocations instead (proves every file is re-parsed).
* Package-style imports (`from big import m1`) also create an edge to the package `__init__`; the large-chain test was adjusted to assert the chain files rather than an exact count.

---

## Phase 04 — Static analysis, code-quality diagnostics and actionable findings (2026-10-18)

Environment: Linux VM, Python 3.12.14, 2 CPUs. Not verified on Windows, in a real browser, on PostgreSQL or Python 3.11. The project folder is not a git repository, so no `git diff` could be produced.

### Backend
| Check | Result |
|---|---|
| Baseline before changes: `pytest` | 204 passed |
| `ruff check .` | all checks passed |
| `pytest` (full) | 248 passed (204 existing + 44 in `tests/test_static_analysis.py`); the final re-run after all edits was run in two halves because the VM had slowed past the 3-minute tool limit: 75 passed (static analysis + graph) and 173 passed (the rest) |
| Migration cycle on a disposable SQLite DB: `upgrade head` → `check` → `downgrade 0003` → `upgrade head` → `downgrade base` → `upgrade head` → `check` | all steps succeeded; `alembic check` reports no new upgrade operations |

`test_static_analysis.py` covers: true and false positives for 11 rule pairs; catalog metadata completeness; thresholds via configuration (function length, complexity); the complexity definition; finding fields and redaction in findings, detail, summary, status, run history and captured logs; parse failure alongside healthy files; one rule raising while others still report (no message or path leaked); invalid and malformed configuration (422 with all problems), unknown top-level keys such as server limits; disabled rule, severity override, exclusions and generated files; language-specific rule config; rules/config endpoints; idempotent repeat runs; fingerprints stable under line shifts; resolved → reopened; not resolved on parse failure or rule failure; distinct `file_removed` / `rule_disabled` reasons; incremental skipping; suppression workflow including invalid transitions; empty repository; no-index conflict; filters, sorting, pagination, invalid parameters; relative paths and no stack traces; cross-repository 404s; 401 on every route when a token is configured; concurrent-run conflict; startup recovery of an interrupted run; file-count and file-size limits reported as warnings; a repository whose Python files would write a marker file when executed (marker never created); a run where nothing can be analysed ends `failed`.

### Real analysis on a synthetic repository (uvicorn, throwaway DB)
Repository with `API_KEY`/`DB_PASSWORD` literals, `eval`, bare `except`, `shell=True`, `tempfile.mktemp`, `pickle.loads`, a syntax-error file, a generated file and a config disabling `PY-MNT-006`:
10 findings in `svc.py`; run `completed_with_warnings` with 1 parsed-failed file, 1 excluded generated file and a disabled-rule warning. The secret values appeared **0 times** in the findings JSON, summary JSON, server log and the SQLite file.
Defect found by this verification and fixed: a literal with a known token shape was reported twice (by name and by shape); it is now reported once, by shape.

### Frontend (in `~/fe-build`)
| Command | Result |
|---|---|
| `npx tsc -b` | no errors (a type-name clash with an existing `AnalysisStatus` was found and fixed) |
| `npx eslint src` | no errors (a `set-state-in-effect` error was fixed by remounting the detail pane with a key) |
| `npx vitest run` | 50 passed, 1 skipped (live test skips without env); 8 new tests in `health.test.tsx`: summary and list from real responses, empty state, error with retry, warnings / rule errors / invalid config blocking the run button, duplicate-submit prevention, suppress with required reason and server-confirmed result, rejected update shows the server message, filters sent to the server |
| `npm run build` | built |
| Live test: `uvicorn` on port 8123, throwaway DB, sample repo, `NOVA_LIVE_API=… NOVA_LIVE_REPO_PATH=… npx vitest run src/test/live.test.tsx` | 1 passed (previous flow plus Code health: run analysis → overview → findings section → back) |

The duplicate-submit test initially failed (two POSTs); the page now holds a synchronous lock until the request settles.

### Performance (single runs, synthetic content, 1,500 small Python files)
Index 1,500 files, then full analysis 3.5 s (4,500 open findings); unchanged incremental analysis 0.17 s; `GET /findings?page_size=100&sort=path` 34 ms. Variance unknown.

### Defects found and fixed during Phase 04
* A run with one failing rule on its only file was marked `failed`; it is now `completed_with_warnings` when any rule produced a result.
* Duplicate credential findings (above).
* Missing-from-disk files were first classified as `file_excluded`; they are now `file_removed`.
* Unchanged-file handling could flip just-reopened findings to `existing` within the same run.

## Phase 05 — Test generation, coverage-gap analysis, safe execution (2026-10-19)

Environment: Linux, Python 3.12.14, pytest 9.1.1, coverage.py 7.16.2, non-root user.

| Check | Result |
|---|---|
| Baseline before changes | 248 backend / 50 frontend (+1 skipped) tests passed |
| `ruff check app tests` | All checks passed |
| `alembic upgrade head`, `alembic check` | "No new upgrade operations detected" (downgrade 0004 → upgrade verified when the migration was written) |
| `pytest tests/test_testing.py` | 35 passed |
| Regression, run in groups because the VM exceeds the tool time limit | 72 + 115 + 61 passed = 248 existing tests, all still passing (283 total with the 35 new) |
| Frontend `npx tsc -b`, `npx eslint src`, `npx vitest run`, `npm run build` | clean; 58 passed + 1 skipped (8 new in `src/test/testing.test.tsx`); build OK |
| Live API run on a synthetic repo (TestClient) | discover → plan → generate → execute before approval refused (409) → validate → approve → execute: passed 4/4, line 70% / branch 50% measured, existing-suite run measured and gap list showed measured missing lines |

`test_testing.py` covers: discovery incl. syntax-error file and import-based association; no repository code executed; static config read; plan fields and no invented outputs; generated content safe/non-trivial and staged outside the repo; idempotent generate; no auto-execution; target rejections; validator rejections (syntax, imports, eval, open, tamper); oversize/duplicate/changed-target/path checks; production files unmodified; approval required and post-approval tampering blocks execution; read-only role 403; export never overwrites user tests; pass, fail, timeout, bounded and redacted output; minimal environment; unusable interpreter reported as error; elevated privilege refused; interrupted-run recovery; no-data vs 0%; gaps static vs measured and not inferred from file names; cross-repo isolation; per-run artifacts; no absolute paths in any response; 401 on all routes with a token.

### Defects found and fixed during Phase 05
* The recorded command exposed an absolute staging path; now replaced with `<staged>/name`.
* A second run request returned 429 instead of the specific 409 "already in progress"; per-repository check now comes first.
* Gap analysis counted name-based test association as evidence; now only import-based association counts.

### Not verified
Windows (no resource limits there), a real browser, PostgreSQL, Python 3.11, and any sandbox/container isolation (none exists).

## Phase 06 — Bug localization, root-cause analysis, evidence-based debugging (2026-10-19)

Environment: Linux, Python 3.12.14, pytest 9.1.1, coverage.py 7.16.2, non-root user. Backend tests run from `backend/` with `$HOME/venv`; the VM is slow, so suites were run in groups under the tool time limit.

| Check | Result |
|---|---|
| Baseline before changes | 283 backend / 58 frontend (+1 skipped) tests passed |
| `ruff check app tests` | All checks passed |
| `alembic upgrade head` on a fresh SQLite DB, then `alembic check` | "No new upgrade operations detected"; downgrade 0005 → upgrade 0006 verified when the migration was written |
| `pytest tests/test_debugging.py` | 36 passed |
| Regression, in groups | 67 (api_*, auth, git) + 93 (graph, health/migrations, indexer, integration, language, paths) + 27 (planner, symbols) + 17 (repository_intelligence) + 44 (static_analysis, two halves by `-k`) + 35 (testing) = 283 existing tests, all passing (319 total) |
| Frontend `npx tsc -b`, `npx eslint src`, `npx vitest run`, `npm run build` | clean; 69 passed + 1 skipped (11 new in `src/test/debugging.test.tsx`); build OK |

`test_debugging.py` covers: real pytest output shapes; assertion and repository-code exceptions; failures with no repository frames; malformed traces; oversized reports (413); paths escaping the repository; credential redaction in API responses and logs; JUnit XML including collection errors and XML-entity rejection; fingerprint de-duplication, recurrence counts and timestamps, distinct failures, cross-repository isolation; single-candidate and multi-candidate ranking, documented weights and overrides, determinism and evidence de-duplication, insufficient evidence, static findings, missing dependency data, cyclic traversal and depth limits; investigations with supporting and contradicting evidence; verification preview without execution, validated targets, PERSISTS, NOT_REPRODUCED_IN_VERIFICATION_RUN, CHANGED_FAILURE and INCONCLUSIVE; authentication, viewer role and cross-repository access; pagination, filters and invalid inputs; absolute-path leakage.

### End-to-end run with a real failing test
`test_verification_persists_then_not_reproduced_after_fix` (command: `python -m pytest tests/test_debugging.py -k verification_persists`) builds a synthetic repository (`pkg/calc.py`, `tests/test_a.py`), runs its real suite through `POST /tests/execute` (a real pytest subprocess: 1 passed, 2 failed), ingests the run's failures (2 created), investigates the `ValueError`, verifies with approval (PERSISTS, occurrence count 2), edits the code, verifies again (`NOT_REPRODUCED_IN_VERIFICATION_RUN`, 1 passed) and checks the list order. A separate test edits the code to a wrong fix and observes `CHANGED_FAILURE`.

### Defects found and fixed during Phase 06
* Investigation and verification rows were audited before their ids existed (NOT NULL); now flushed first.
* Datetimes in the verification JSON response were not serializable; now encoded.
* Absolute-path regex produced `<repo><path>/x`; collection errors lost the exception type; coverage from the failure's own run was used as evidence (circular) and is now skipped.
* Recurrence responses returned naive timestamps while creation returned UTC; all timestamps are now UTC.
* A Python 3.11-incompatible f-string was replaced.

### Not verified
Windows, a real browser, PostgreSQL, Python 3.11, any sandbox, and the optional AI provider (none exists).


## Phase 07 — Intelligent code review, change-risk analysis and regression prevention (2026-10-19)

Environment: as above. Backend tests run from `backend/` with `$HOME/venv`, in groups under the tool time limit (`-o addopts=""` to print the summary line).

| Check | Result |
|---|---|
| Baseline before changes | 319 backend / 69 frontend (+1 skipped) tests passed |
| `ruff check app tests` | All checks passed |
| `alembic upgrade head` on a fresh SQLite DB, then `alembic check` | "No new upgrade operations detected"; downgrade 0006 → upgrade 0007 verified when the migration was written |
| `pytest tests/test_review.py` | 46 passed |
| Regression, in groups | 160 (api_*, auth, git, graph, health/migrations, indexer, integration, language, paths) + 115 (planner, symbols, repository_intelligence, testing, debugging) + 44 (static_analysis, two halves by `-k`: 11 + 33) + 46 (review) = 365 passed |
| Frontend `npx tsc -b`, `npx eslint src`, `npx vitest run`, `npm run build` | clean; 84 passed + 1 skipped (15 new in `src/test/review.test.tsx`); build OK |

`test_review.py` covers: added/modified/deleted/renamed files; staged vs unstaged vs working tree; two-revision comparison and `HEAD~1`; invalid and malicious revisions (nothing persisted, no command executed); non-Git repository (409); binary and symlink files; selected paths and path escapes; file-count, diff-size and file-size limits; preview without storage; mapping to a real indexed symbol; stale index (`fresh_parse`, warning); unparseable files; risk determinism, factor explanations, thresholds, unknown results, saturation; authorization change (informational only); large harmless change (not defective); depth-bounded impact, cycles, missing dependency data, deleted-but-imported module; correlation with stored findings, new vs preexisting, unrelated findings not attributed; de-duplication; per-file and per-review bounds; public-interface checks; secret redaction; test recommendations and testing-gap evidence rules; status tracking and stage list; partial analyzer failure; acquisition failure with a scrubbed message; finding lifecycle and rationale; pagination, filters and invalid inputs; repeated-review stability and inherited decisions; no AI use; clean-review wording; empty reviews; unchanged working tree and Git state; no shell interpretation; no absolute-path leakage; 401 without a token; viewer role; cross-repository isolation.

### End-to-end run on a synthetic Git repository
Command: a temporary test (`test_print_e2e`, `python -m pytest <file> -s -o addopts=""`) built a repository with a clean new file (`pkg/clean.py`), a changed function with dependents and a deliberate `eval` (`pkg/calc.py`), a changed test (`tests/test_calc.py`) and a deleted module (`pkg/legacy.py`, still imported), then ran a working-tree review.
`git diff HEAD --stat` reported 4 files, 13 insertions, 3 deletions; the review reported 4 files, +13 / −3, with `calc.py` +6/−2, `clean.py` +2/−0, `legacy.py` +0/−1, `test_calc.py` +5/−0. Changed symbols: `helper` (modified), `run_expr` (added), `add` (added), `test_helper` (added). Findings: `PY-SEC-001` (eval, `pkg/calc.py:10`, analyzer) and `REV-DEL-001` (deleted module still imported, heuristic); nothing for the clean file. Status `completed_with_warnings` because the index predated the edits (symbols were `fresh_parse`, flagged). Risk 11.4, low, with coverage/failure_history/test_results unavailable. Before and after the review, every file byte (including `.git`), `HEAD` and `git status --porcelain` were identical.
That scratch test is neutralized (an empty placeholder file remains because the sandbox cannot delete files); the same flow is covered permanently by `test_synthetic_end_to_end_review`.

### Defects found and fixed during Phase 07
* Blank lines between functions were counted as module-level changes; only code lines count now.
* Callers were duplicated across symbols in the impact output; now de-duplicated by symbol and path at minimum depth.
* A selected path outside the change fails the (background) review rather than returning 422 at creation; documented and tested as is.

### Not verified
Windows, a real browser, PostgreSQL, Python 3.11, a hosted pull-request service, and the optional AI provider (none exists).


## Phase 08 — Automated repair suggestions, patch generation and safe verification (2026-10-19)

Environment: as above (Linux, Python 3.12). Backend tests run from `backend/` with `$HOME/venv`, in groups under the tool time limit (`-o addopts=""` to print the summary line); each group was run in the foreground.

| Check | Result |
|---|---|
| Baseline before changes | 365 backend / 84 frontend (+1 skipped) tests passed |
| `ruff check app tests` | All checks passed |
| `alembic upgrade head` on a fresh SQLite DB, then `alembic check` | "No new upgrade operations detected"; downgrade 0008 → 0007 and re-upgrade verified when the migration was written |
| `pytest tests/test_repair.py` | 33 passed |
| Regression, in groups | 67 + 93 (= 160: api_*, auth, git, graph, health/migrations, indexer, integration, language, paths) + 44 + 71 (= 115: planner, symbols, repository_intelligence, testing, debugging) + 11 + 33 (= 44: static_analysis) + 46 (review) + 33 (repair) = **398 passed** |
| Frontend `npx tsc -b`, `npx eslint src`, `npx vitest run`, `npm run build` | clean; 94 passed + 1 skipped (10 new in `src/test/repair.test.tsx`); build OK (existing >500 kB chunk warning only) |

`test_repair.py` covers: the complete worked example; investigation creation, retrieval, filters, `%` escaping and pagination; source attribution (`observed` vs `stated_by_requester`); ranking of relevant over unrelated evidence; explicit insufficient evidence (and 409 when generating a patch from it); a finding-sourced investigation; patch-engine units (exact match, CRLF, create, overlap, binary); autofix units and declined cases; valid preview; garbage, binary, delete and rename rejection; path traversal, absolute and `.git` paths and disallowed suffixes; file and directory symlink escapes; file-count and size limits; stale source, hash tampering and unexpected files (and `extra_files`); validation leaving the tree and `.git` byte-identical; temp-directory cleanup after a failure inside validation; viewer 403 on every write; approval-required, confirmation-required, state-machine conflicts; exact content and file-mode preservation on apply; duplicate apply (409); four concurrent applies with exactly one winner; source modified after approval refused with the file untouched; reject reason rules and audit pagination without file content; rollback guidance; failed verification keeps the change and the evidence; pre-existing vs new failures against a baseline; timeout and runner failure never PASSED; bounded and redacted output; BLOCKED without discovery, unknown tests 422, patch-not-applied 409; cross-repository 404; 401 without a token; no absolute paths in responses; Git state untouched by evidence, proposal and patch creation.

### End-to-end run on a disposable repository
`test_worked_example_end_to_end` builds a throwaway repository under a pytest temp directory (never a real user project) and drives the HTTP API. Real outcomes: the test run reported 1 failed (`test_carts_are_independent`); the top hypothesis was `pkg/cart.py` / `pkg.cart.add_item` (score 6.75, high, labelled unconfirmed); the proposal was `ready` with rule `PY-COR-002`; the tree hash was identical after proposal, patch creation, validation, submission and approval; apply before approval returned 409; after apply `pkg/cart.py` matched the expected fixed text and the investigation was `applied` with no verification outcome; verification without `confirm` returned 422; with `confirm` the run passed (2 passed, 0 failed), `original_failure_resolved` was `pass`, the outcome `PASSED`, and the investigation and patch became `verified`; the audit actions appeared in the order created → evidence → proposal → patch created / validated / submitted / approved / applied → verification started / evaluated.

### Defects found and fixed during Phase 08
* `PatchDetail` / `VerificationDetail` schemas required fields the list endpoints did not provide; defaults added.
* Confirmation buttons in the UI showed "Working…" when merely disabled (reason too short); a separate `disabled` state was added.
* The output-bounding test originally checked the wrong response keys (`stdout`/`stderr` instead of `stdout_tail`/`stderr_tail`), so it passed vacuously; corrected to assert a non-empty, bounded value.

### Not verified
Windows, a real browser, PostgreSQL, Python 3.11, a sandbox for verification, and an AI provider (none exists). The race window between the final staleness check and the file replace is narrowed, not eliminated.


## Phase 09 — Intelligent refactoring (2026-10-19)

| Check | Result |
|---|---|
| Baseline before Phase 09 (Phase 08 final) | 398 backend, 94 frontend + 1 skipped |
| Migration `0009`: upgrade, `alembic check`, downgrade to 0008, upgrade head | clean |
| `tests/test_refactor.py` | 52 passed |
| Regression, in groups | 52 + 80 (repair, auth/errors, health/migrations, paths) + 46 (review) + 35 (testing) + 41 (debugging, git) + 75 (graph, static) + 72 (api_intelligence, repositories, tasks, indexer) + 49 (integration, language, planner, repository_intelligence, symbols) = **450 passed** |
| `ruff check app tests/test_refactor.py` | all checks passed |
| Frontend `tsc -b`, `eslint src`, `vitest run`, `npm run build` | clean; 110 passed + 1 skipped (16 new in `refactor.test.tsx`); build OK |

Worked example executed on a disposable Git repository (see `docs/architecture/REFACTORING.md`): rename `add_item → append_item` produced a 4-file diff, validation passed all 7 checks without touching the tree, apply left the changes uncommitted (` M` x4), the same-named method, the shadowing local and the string were untouched; a baseline requested after approval was refused (409).

Defects found and fixed: impact `direct_dependents` empty (changed dependents were seeds); renamed names counted as import-edge changes; JS rename blocked its own definition as shadowed; language-name case mismatch between index and engines; duplicate statement-window groups duplicating function groups; extract picked the wrong statement list and treated loop targets as parameters; a disk-full condition from pytest temp dirs caused spurious errors (cleaned and rerun).

Not verified: Windows, a real browser, PostgreSQL, Python 3.11, a sandbox, JS type-aware renames, formatter/linter/type-checker results. Three empty throwaway test files (`test_zz_tmp_ser.py`, `test_explore_tmp.py`, `test_zz_e2e_print.py`) could not be deleted in the connected folder and are comment-only.


## Phase 10 — Continuous verification (2026-10-19)

| Check | Result |
|---|---|
| Baseline before Phase 10 | 450 backend, 110 frontend + 1 skipped (Phase 09 final) |
| Migration `0010`: upgrade, `alembic check` (no new operations), downgrade to 0009, upgrade head | clean; tables `cv_policies`, `cv_baselines`, `cv_runs`, `cv_checks`, `cv_events` |
| `tests/test_verification.py` | 53 passed |
| Regression, in groups | 53 + 52 (refactor) + 80 (repair, auth/errors, health/migrations, paths) + 46 (review) + 40 (testing, git) + 36 (debugging) + 72 (api_intelligence, repositories, tasks, indexer) + 75 (graph, static) + 49 (integration, language, planner, repository_intelligence, symbols) = **503 passed** |
| `ruff check app tests` | all checks passed |
| Frontend `tsc -b`, `eslint src`, `vitest run`, `npm run build` | clean; 131 passed + 1 skipped (21 new in `verification.test.tsx`); build OK |

Demonstration executed on a disposable Git repository (see `docs/architecture/CONTINUOUS_VERIFICATION.md`): baseline 4/4 passed; a harmless change gave a focused PASS running 3 of 4 tests; a broken `clamp` gave FAIL with `test_clamp_low` classified *new* against the baseline; the fix gave PASS and the broken run became stale; an empty change set gave BLOCKED.

Defects found and fixed while verifying the Phase 10 backend: an empty change set reported a misleading "no test imports" detail (now "nothing to verify"); the overview gate counts included baseline runs (now verify runs only); the draft tests contained placeholder `or True` assertions, an invalid hand-written diff, a non-existent import and a wrong precedence expectation (BLOCKED outranks INCOMPLETE) — all corrected. A background-process attempt to run the regression was killed by the tool environment; the regression was rerun in foreground groups.

Not verified: Windows, a real browser, PostgreSQL, Python 3.11, a sandbox, parallel run contention beyond the tested duplicate-start guard, ecosystems other than Python/pytest. Cancellation does not kill an already-running pytest process.


## Phase 11 — CI/CD integration and release readiness (2026-10-19)

| Check | Result |
|---|---|
| Baseline before Phase 11 | 503 backend, 131 frontend + 1 skipped (Phase 10 final) |
| Migration `0011`: upgrade, `alembic check` (no new operations), downgrade to 0010, upgrade head | clean; seven `ci_*`/`change_requests` tables |
| `tests/test_cicd.py` | 52 passed |
| Regression, in parallel groups | 38 (api_intelligence, repositories) + 34 (api_tasks, auth, git, health) + 67 (integration, language, paths, planner, symbols) + 36 (debugging) + 31 (graph) + 17 (indexer) + 44 (static) + 17 (repo intelligence) + 35 (testing) + 33 (repair) + 46 (review) + 52 (refactor) + 53 (verification) + 52 (cicd) = 555 passed |
| `ruff check .` (backend, including migrations) | all checks passed |
| Frontend `tsc -b`, `eslint src`, `vitest run`, `npm run build` | clean; 143 passed + 1 skipped (12 new in `workflow.test.tsx`); build OK |
| `.github/workflows/ci.yml` | YAML parsed; every command executed locally (ruff, pytest, npm scripts, vitest junit output). NOT run on GitHub |

Demonstration executed on a disposable Git repository (see `docs/architecture/CICD.md`); provider interactions were mocked and are labelled so.

Defects found and fixed while testing: status-report attempt counter was `None` on first send (crash); `external_repo` accepted `../x`; Markdown reports let `[x](javascript:...)` link syntax through; webhook response lacked a uniform `outcome`; migrations 0005-0010 had unsorted imports that would fail the CI lint step.

Not verified: live GitHub, the Actions workflow on GitHub, Windows, a real browser, PostgreSQL, Python 3.11, other providers. Three empty throwaway test files remain (`rm` is not permitted in the connected folder).


## Phase 12 — Security engineering and supply-chain intelligence (2026-10-20)

| Check | Result |
|---|---|
| Baseline before Phase 12 | 555 backend, 143 frontend + 1 skipped (Phase 11 final) |
| Migration `0012`: upgrade, `alembic check` (no new operations) | clean; eleven `sec_*` tables; downgrade to 0011 and re-upgrade covered by `test_security_migration_upgrades_and_downgrades` |
| `tests/test_security.py` | 50 passed |
| Backend regression, sequential foreground groups | 94 + 98 + 86 + 88 + 35 + 98 + 106 = 605 passed (555 existing + 50 new) |
| `ruff check .` | all checks passed |
| Frontend `tsc -b`, `eslint src`, `vitest run`, `npm run build` | clean; 155 passed + 1 skipped (12 new in `security.test.tsx`); build OK |

Demonstration executed on a disposable Git repository with a synthetic advisory (see `docs/architecture/SECURITY.md`).

Defects found and fixed while testing: fixed secret findings never resolved (kind key mismatch); suppression counts were overwritten in the scan summary; a scan with only an empty workspace store reported "complete"; a policy requiring inventory/advisory data passed when the dependencies scan was not run; a duplicate-proposal conflict leaked an ORM object into the error payload.

Not verified: live OSV, Windows, a real browser, PostgreSQL, Python 3.11, JavaScript code rules, ecosystems other than PyPI/npm. Three empty throwaway test files remain (`rm` is not permitted in the connected folder).

## Phase 13 — Observability, reliability engineering and operational intelligence (2026-10-20)

| Check | Result |
|---|---|
| Baseline before Phase 13 | 605 backend, 155 frontend + 1 skipped (Phase 12 final) |
| Migration `0013`: upgrade, `alembic check` (no new operations) | clean; five `ops_*` tables |
| `tests/test_observability.py` | 70 passed |
| Backend regression, sequential foreground groups | 94 + 52 + 81 + 53 + 85 + 74 + 166 = 605 existing tests passed unchanged, plus 70 new = 675 |
| `ruff check .` | all checks passed |
| Frontend `tsc -b`, `eslint src`, `vitest run`, `npm run build` | clean; 164 passed + 1 skipped (9 new in `operations.test.tsx`); build OK |

Defects found and fixed while testing: audit entries for config/deployment writes were created before the row id existed (NOT NULL violation); HTTP metric labels initially lacked the router prefix (now the full route template, ids replaced by parameter names); the 500 response did not carry `X-Request-ID` because the context was reset before the outer handler ran (id now stored on the ASGI scope).

Not verified: a real OTLP collector (not implemented), real Slack or webhook receivers (injected transport only), email (not implemented), Windows, a real browser, PostgreSQL, multi-process deployments. Telemetry is in-memory by design. Empty throwaway test files remain (`rm` is not permitted in the connected folder): `tests/test_zz_tmp_ser.py`, `tests/test_explore_tmp.py`, `tests/test_zz_e2e_print.py`, `tests/test_ops_smoke_tmp.py`, `tests/test_zz_tmp_probe.py`.

## Phase 14 — Performance engineering, scalability and resource optimization (2026-10-20)

| Check | Result |
|---|---|
| Baseline before Phase 14 | 675 backend, 164 frontend + 1 skipped (Phase 13 final) |
| Migration `0014` (three FK indexes): upgrade, downgrade to 0013 and re-upgrade, `alembic check` | clean; `EXPLAIN QUERY PLAN` uses `ix_relationships_source_file_id` (tested) |
| `tests/test_performance.py` | 29 tests: 27 passed, 2 skipped (large fixtures are covered by the CLI) |
| Backend regression, sequential chunks | 704 collected = 675 existing (unchanged) + 29 new; all chunks passed, 2 skipped. One chunk showed 4 setup errors while the sandbox disk was 100 % full (`database or disk is full`); after freeing space the same 45 tests passed on re-run |
| `ruff check .` | all checks passed |
| Frontend `tsc -b`, `eslint .`, `vitest run` | clean; 169 passed + 1 skipped (5 new in `performance.test.tsx`; two existing loading-state assertions changed from `getByText` to `findByText` because routes now load lazily) |
| Entry bundle (`vite build`) | 669.94 kB → 253.15 kB (gzip 179.74 → 78.38 kB); same build tool before and after |
| Index, large fixture (1,719 files), repeat 3 | `index_incremental_noop` median 7,530 ms → 2,105 ms; `index_full` min 2,514 → 2,582 ms |
| Static analysis, medium fixture, interleaved A/B | median 975 ms → 813 ms |

Baselines recorded explicitly with `python -m app.perf --profile small|medium --repeat 5 --update-baseline` (`backend/benchmarks/baseline.json`, 24 entries). Exact commands are in `docs/architecture/PERFORMANCE.md`.

Defects found and fixed while testing: the compare mode initially exited 0 when no baseline existed (now exit 2, "not a pass"); generation switch cost was dominated by unindexed FK child columns; a plain before/after timing of the analyzer change was inconclusive, so an interleaved A/B was used.

Not verified: PostgreSQL, Windows, multi-process deployment, a real browser (load time), production-scale data, memory limits (no hard isolation exists), regressions smaller than 2×. Empty throwaway test files from earlier phases remain (`rm` is not permitted in the connected folder).

## Phase 15 — Enterprise governance, team collaboration and workflow orchestration (2026-10-21)

| Check | Result |
|---|---|
| Baseline before Phase 15 | 704 backend (2 skipped), 169 frontend + 1 skipped (Phase 14 final) |
| Migration `0015`: fresh upgrade to head, `alembic check`, downgrade to 0014 and re-upgrade | clean ("No new upgrade operations detected"); head `0015`; existing `memberships` rows survive upgrade (`status` defaults to `active`) and the downgrade round trip (tested in `test_migration_preserves_existing_data_and_roundtrips`); append-only triggers created / removed with the migration |
| `tests/test_governance.py` | 38 passed (authentication and tokens, member lifecycle and last-administrator rule, team CRUD / pagination / archive rules, membership and ownership rules, cross-tenant isolation, hidden team resources, policy validation / permissions / versioning, missing-evidence and unknown-type policies, historical evaluation versions, workflow happy path and timeline, self-approval and independence, reject / request-changes / rounds, abstain / cancel, input validation, duplicate decisions and DB constraint, optimistic locking, 4-thread concurrent approval, multi-stage quorum, definition snapshot, atomic policy-blocked approval, attestation cannot satisfy verification, prohibited transition, queue lifecycle / filters / pagination / deleted resources / enqueue, audit scoping and append-only triggers, deduplicated denials, overview / health, metric labels, bounded query count, removed users and archived teams) |
| Backend regression, sequential foreground groups | 742 collected = 704 existing (unchanged, all passed, 2 skipped) + 38 new. The governance file was re-run in full after the last edits; the other groups ran before the final change that added structured log lines to the governance services (which only governance code paths execute) |
| `ruff check .` | all checks passed |
| Frontend `tsc -b`, `eslint src --max-warnings 0`, `vitest run`, `vite build` | clean; 190 passed + 1 skipped (21 new in `governance.test.tsx`); build OK, entry bundle 254.80 kB (gzip 78.76 kB; 253.15 kB after Phase 14) |

Defects found and fixed while testing: workflow definitions accepted `required_policy_keys` that do not exist (now 422); review-queue `status` / `priority` filters were not validated (now 422); a queue claim by an ineligible user answered 422 instead of 403; approved workflows stayed open in the queue after the verdict (now closed on any verdict); default definition keys contained underscores that the definition API itself rejects (now hyphenated); `/governance/health` did not state that no scheduler exists (now `scheduler: not_implemented`); the first version of the metric-label test checked a path that does not exist and passed vacuously (now asserts the governance metrics exist and carry no ids or users); governance services had no structured logging (added). The suspected SAVEPOINT / `StaleDataError` risks did not materialise on SQLite: the atomic policy-block test and the locking and concurrency tests pass.

Status: **PARTIAL**. Acceptance criteria not met: UI screens for organization members, policy detail / create / edit / version history, workflow definitions and ownership assignment (the API for each exists and is tested; the UI only lists ownership read-only and toggles policies). Everything else listed in the specification was implemented and verified as stated above.

Not verified: PostgreSQL (the append-only triggers are SQLite-only; no equivalent exists), multi-process deployment (concurrency was tested with threads in one process), a real browser (UI tested in jsdom with mocked fetch) or assistive technology, SSO / OIDC (not implemented), notifications and any scheduler (not implemented), cryptographic audit integrity (not implemented; the file can be edited by anyone with write access), Windows, production-scale data, and the interaction of per-user tokens with the Settings page token field in a live browser. Empty throwaway test files from earlier phases remain, and `tests/test_zz_dump.py` (a helper created during this phase, now emptied) cannot be removed because `rm` is not permitted in the connected folder.

## Phase 16 — Enterprise integrations, API contracts and extension ecosystem (2026-10-21)

| Check | Result |
|---|---|
| Baseline before Phase 16 | 742 backend collected (740 passed + 2 skipped), 190 frontend + 1 skipped (Phase 15 final) |
| Migration `0016`: fresh upgrade to head, `alembic check`, downgrade to 0015 and re-upgrade | clean ("No new upgrade operations detected"); head `0016`; a workspace row created at 0015 survives upgrade and downgrade (`test_migration_0016_preserves_existing_data`) |
| `tests/test_integrations.py` | 48 passed: credentials (round trip, AAD binding, fail-closed without key, bad key, key rotation, `env:` prefix limit, secrets never in responses / DB / audit), SSRF (nine blocked URL forms, DNS answer with a private address, host allowlist, subscription URL validation), installation lifecycle and strict validation, authorization by role, tenant isolation, inbound happy path / rejections / duplicates / no-downgrade / secret rotation, local-test adapter, CI-evidence policy semantics, outbound signing and verification, retry / backoff / permanent failure / attempt bound, lease recovery and replay, issue search / link / create / idempotency / duplicate prevention, jobs (dedupe, retry, dead, crash recovery, stuck indicator), extension manifest rejection of module / entrypoint fields, status publishing opt-in, only the inbound route is reachable without a bearer token |
| `tests/test_api_contract.py` | 3 passed (baseline has no breaking change; detector flags each breaking kind and allows additive changes; no secret-like response property) |
| Backend regression, file by file in the foreground | 793 collected = 742 existing (all passed, 2 skipped) + 48 + 3 new; every file was run to completion in this session (the full-suite background run was killed when its shell call ended and was discarded). One `test_review.py` run showed 34 errors while the sandbox disk was 100% full (SQLAlchemy OperationalError); after freeing space the same file passed 46/46 |
| `ruff check .`; `alembic check`; `python -m app.contracts` | all checks passed; no new operations; contract OK |
| Frontend `tsc -b`, `eslint src`, `vitest run`, `vite build` | clean; 199 passed + 1 skipped (9 new in `integrations.test.tsx`); entry bundle 255.95 kB (254.80 kB after Phase 15) |

Defects found and fixed while testing: GitHub installations need the encryption key because the inbound signing secret is stored encrypted (the creation path now returns 503 `secrets_unavailable` instead of failing later); the content-type check ran before authentication (now after the signature); naive/aware datetime comparison in queue statistics (SQLite returns naive timestamps); a malformed regex in the Pydantic config models (look-around is unsupported there; replaced by a validator).

Status: **PARTIAL**. Not met: UI for issue search / link / create and for publishing a status (APIs exist and are tested), jobs panel, subscription removal button, accessibility audit; no automated retention purge; PR events are not converted into change requests; `nova_extension_runs_total` is defined but never incremented because no built-in extension runs code.

Not verified: any real GitHub or issue-tracker account, PostgreSQL, multi-process workers (the lease logic was tested with simulated crashes in one process), a real browser, production secret management (application-level encryption only), network-level egress controls.

## Phase 17 — release engineering, deployment automation, disaster recovery (2026-10-21)

| Check | Outcome |
|---|---|
| `tests/test_release_ops.py` | 34 passed: config contract, startup refusal, migration states/lock/stale-lock/failure/unknown revision, upgrade from the previous revision, backup/verify/tamper/interrupted/prune/restore/file references, rollback planner, manifest/SBOM, preflight, CLI + a real `uvicorn` process (refuses stale schema, deploy, backup, restore, readiness, authenticated smoke) |
| Backend regression (827 tests collected; run in 15-test chunks in the foreground) | 825 passed, 2 skipped, 0 failed |
| `ruff check .`; `alembic check`; `python -m app.contracts` | all checks passed; no new operations; contract OK |
| Frontend `tsc -b`, `eslint src`, `vitest run`, `vite build` | clean; 199 passed + 1 skipped; entry bundle 255.95 kB |
| `pip-audit -r requirements.txt --no-deps --disable-pip`; `npm audit --audit-level=high` | no known vulnerabilities; 0 vulnerabilities (advisory databases as of run time) |
| `python scripts/scan_secrets.py` | 0 findings (4 reviewed synthetic-fixture allowlist entries) |
| Synthetic restore timing (53 MB DB, 2,000 files, this sandbox) | backup 0.64 s, verify 0.08 s, restore 0.72 s; empty-database migration 4.8 s — not representative of production |

Not verified: Docker builds and compose (no Docker), the GitHub workflows (YAML parsed only), PostgreSQL, multi-process deployment, TLS, image scanning, real production deployment, DR drill on separate hardware, Windows execution of the CLI. Artifacts are unsigned. Status: **PARTIAL**.

## Phase 18 — final enterprise acceptance (2026-10-21)

Environment: Linux 6.8, Python 3.12.14, SQLite 3.53.1, Node 22.23.2, 2 CPUs; no Docker, PostgreSQL or real browser; not a git checkout (revision unavailable).

| Check | Outcome |
|---|---|
| Backend regression (842 tests, 15–25-test chunks, foreground) | 840 passed, 2 skipped, 0 failed |
| New: `test_acceptance_sweeps` (7), `test_acceptance_e2e` (2), `test_acceptance_resilience` (4), `test_acceptance_bench` (2) | all passed |
| `ruff check .`; `alembic check`; `python -m app.contracts`; `scripts/scan_secrets.py` | clean; no new operations; contract OK; 0 findings |
| `pip-audit` 2.10.1 (`--no-deps`); `npm audit --audit-level=high` | no known vulnerabilities; 0 vulnerabilities |
| Frontend `tsc -b`, `eslint src`, `vitest run`, `vite build` | clean; 199 passed + 1 skipped; entry bundle 255.95 kB |
| Benchmarks (`app.perf` small/medium, `measure_indexing --files 1500`, governance/webhook benchmark test) | recorded in `docs/performance/FINAL_BENCHMARK_REPORT.md`; committed baseline not comparable (`missing_baseline`) so no regression verdict |

Defects found and fixed: F-001 (no request-size limit; reproduced with a 64 MB body), F-002 (storage failure reported as 500), F-006 (empty throwaway tests, slow fixture). Open Medium findings: F-003, F-005, F-007, F-008, F-009, F-011, F-012 (see `docs/acceptance/DEFECT_REGISTER.md`).

Not verified: container builds, compose, GitHub workflow execution, PostgreSQL, multi-process operation, TLS, real providers, real browser / accessibility, Windows, a real DR drill, signed or revision-traceable artifacts. **Decision: NOT RELEASE READY.**


## Phase 19 — production deployment readiness and release engineering (2026-10-22)

Environment: Windows 11 (`F:\last proj`; Python 3.11.9, Node 24.18.0, npm 11.16.0) and WSL2 Ubuntu (Linux 6.6.87, Python 3.12.3, SQLite 3.45.1, 22 CPUs);
Chrome 155 / Edge 155; nginx 1.30.6 (official Windows build, for config verification); **no container engine**; not a git checkout.

| Check | Outcome |
|---|---|
| Baseline, Linux, pristine copy (before any change) | 840 passed, 2 skipped — matches Phase 18 |
| Baseline, Windows (first ever) | 63 failed, 777 passed, 2 skipped — causes: a Git repository above the temp directory (product defect: git escaped the registered root), CRLF fixtures vs the LF-only patch engine, two POSIX-only checks |
| Baseline, frontend clean install | `npm run build` failed (`@types/node` missing from package.json); vitest 3 timing failures |
| Final backend, Linux (Python 3.12.3) | **854 passed, 2 skipped**; files changed afterwards re-run: 185 passed, 65 passed |
| Final backend, Windows (Python 3.11.9) | **852 passed, 4 skipped** (2 slow benchmarks, 1 POSIX file-name test, 1 Linux-only /proc test); files changed afterwards re-run: 48 passed + 1 skipped, 46 passed |
| Frontend `tsc -b`, `eslint src`, `vitest run`, `npm run build` | clean; 200 passed + 1 skipped; entry 255.96 kB (78.97 kB gzip) |
| `ruff check .`, `pip check`, `alembic check`, `python -m app.contracts`, `scripts/scan_secrets.py` | clean; no new operations; contract OK; 0 findings |
| `pip-audit` 2.10.1 (pins `--no-deps`, and `--path` over a requirements.txt-only venv); `npm audit --audit-level=high` | no known vulnerabilities; 0 vulnerabilities |
| Image-equivalent production rehearsal (read-only code tree, runtime-only venv, production env, image CMD) | old compose flow reproduced the migrate crash; fixed flow: deploy -> 0016, ready 3.4–7.2 s, smoke 32/32 direct and 39/39 through nginx, restore drill ok, graceful stop 0.26 s |
| Real browser (`frontend/e2e/browser_smoke.mjs` through nginx) | 15/15 Chrome 155, 15/15 Edge 155 |
| Secrets vs repository test code | before: child read `NOVA_API_TOKEN` from `/proc/<ppid>/environ` (LEAKED); after: PermissionError; hostile test in a real NOVA test run passed; negative control fails as expected |
| Performance sanity (278-file / 53,888-line repository) | index 3.25 s, analysis 9.71 s, RSS 192–276 MB; c1 p50 0.7–15 ms; c8 `/metrics` 1.95 s p50 (CPU-bound, documented); `/ready` 32.9 -> 1.4 ms after caching the head revision |

Not verified: Docker image builds, compose start, CI workflow execution, TLS overlay, image scanning, cloud/staging deployment, DR on separate hardware, real
integration providers. Decision: NOT DEPLOYMENT READY (container gate blocked) — `docs/operations/FINAL_DEPLOYMENT_REPORT.md`.

## Phase 19 — portfolio pass: AI-assisted repair, Git, concurrency fix (2026-10-22)

| Check | Outcome |
|---|---|
| Backend, Linux (Python 3.12.3), 871 tests | **869 passed, 2 skipped**, 0 failed |
| Backend, Windows (Python 3.11.9) | **867 passed, 4 skipped**, 0 failed (skips: 2 slow benchmarks, 1 POSIX file-name test, 1 Linux-only /proc test) |
| Frontend `tsc -b`, `eslint src`, `vitest run`, `npm run build` | clean; **203 passed, 1 skipped**; build OK |
| `tests/test_ai_repair.py` (fake Anthropic client, no network) | 13 passed: propose -> validate -> submit -> approve -> apply; secret not sent; unrelated files not sent; 6 gate rejections store nothing; no-fix / refusal / truncation / auth / rate-limit / connection errors; request shape; prod warnings; migration 0017 guard |
| Migration `0017` | fresh DB to head; `alembic check` clean; table rebuild diff shows only the CHECK change; downgrade refused while AI rows exist; lossless down/up cycle otherwise |
| `python -m app.contracts`; `scripts/scan_secrets.py`; `pip check` | contract OK (summary identical); 0 findings; consistent (anthropic 1.12.1) |
| Concurrency fix | edge query c8 21.9 -> 1,701 ops/s (identical results); `/metrics` c8 9.8 -> 74 req/s, p50 893 -> 95 ms; equivalence test added |
| Windows-only failure in the previous run (`test_time_budget_truncation`) | cause: `time.monotonic()` resolution 15.6 ms on Windows; fixed with `perf_counter`; passes 3/3 |
| Real-app screenshots (`docs/images/`) | production build behind nginx 1.30.6, production-mode API, Chrome 155 |

Not verified: a real Claude API call (no credentials), container builds, CI execution (not pushed).
