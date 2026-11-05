# Performance Engineering, Scalability and Resource Optimization

Scope is deliberately evidence-led: a benchmark framework, profiles of real workloads, and **only the optimizations that profiling justified**. Numbers below are from this development sandbox (Linux, CPython 3.12.14, SQLite 3.53, **2 CPUs, shared and noisy**). They are *synthetic* benchmark results on generated fixtures, not production measurements, and are not portable to other hardware.

## Benchmark framework (`backend/app/perf/`)
| Module | Role |
|---|---|
| `schema.py` | Result schema `nova.benchmark/1` (`kind: synthetic_benchmark`), environment capture, `Stats` |
| `runner.py` | `run_benchmark` (monotonic clock, setup excluded from timing, warm-up, correctness `check`), `QueryCounter` (SQL count/time via SQLAlchemy events; statements normalised and bounded, **parameters never stored**), opt-in `tracemalloc` peak (separate run) |
| `fixtures.py` | Deterministic repository generator (seeded; manifest records *actual* file/byte/line counts, languages, a content digest) |
| `workloads.py` | Throw-away NOVA instance (own SQLite DB, real migrations, real API); each workload validates its own output |
| `budgets.py` | Baseline store and regression policy |
| `__main__.py` | CLI |

Statistics: min, median, mean, max, stdev, MAD; p95 only with n ≥ 20 and p99 only with n ≥ 100 (never reported from too few samples). Benchmarks are a **separate mode** (CLI), not part of the request path; the framework adds no telemetry system — runtime job/HTTP timing continues to use the registry.

### Fixture profiles (counts are measured, labels are not)
`python -m app.perf --list-fixtures` prints them. Examples: small 36 files / 20,434 B; medium 314 / 282,810 B; large 1,719 / 1,856,460 B; many_files_little_code 1,533 files; large_source_files 19 files / 3,002,545 B; deep_nesting 307; import_heavy 274; malformed 114 (some syntactically broken); many_dependencies 47 (+ manifests). Generation is deterministic (same seed ⇒ same digest; tested).

### Commands
```
cd backend
PYTHONPATH=. python -m app.perf --list-fixtures
PYTHONPATH=. python -m app.perf --profile medium --repeat 5                       # run all workloads
PYTHONPATH=. python -m app.perf --profile large --workloads index_full,index_noop --repeat 3 --out result.json
PYTHONPATH=. python -m app.perf --profile medium --memory --workloads index_full  # tracemalloc peak (Python allocations only)
PYTHONPATH=. python -m app.perf --profile medium --profile-cpu static_analysis_full   # cProfile top-25
PYTHONPATH=. python -m app.perf --profile medium --compare                         # compare with benchmarks/baseline.json
PYTHONPATH=. python -m app.perf --profile medium --repeat 5 --update-baseline     # explicit baseline update
```
Workloads: `index_full`, `index_incremental` (one changed file), `index_noop`, `api_reads` (files list, symbol search, summary, file detail, dependents, cycles, impact), `static_analysis`, `security_scan`.

### Budgets and regression policy
Baseline key = operation + workload + fixture version/digest + benchmark version, stored in `backend/benchmarks/baseline.json` (small and medium profiles recorded with `--repeat 5`, 2-CPU sandbox). Regression = median more than **2.0×** the baseline *and* more than **25 ms** slower (generous on purpose: sandbox timing is noisy). Statuses: `ok`, `regression`, `improved`, `missing_baseline`, `correctness_failed`, `error`, `environment_mismatch`. **A missing baseline, an environment mismatch, a failed check or an error is never a pass**: `--compare` exits 1 on error/regression, 2 when anything is unverified. Baselines change only through `--update-baseline`, and only successful, correctness-checked results are recorded. Baselines recorded on different hardware (Python version or CPU count) are reported as `environment_mismatch`.

## Optimizations (each with evidence)
### 1. Repeated taint analysis in the security analyzer
- **Bottleneck:** `cProfile` of `static_analysis_full` (medium: 314 files) showed per-rule re-walking of every function and rebuilding the `Taint` state for every security rule that iterated calls.
- **Change:** one per-file, memoised list of `(call, taint, qualname, lineno, resolved)` shared by all rules (`FileFacts.memo`, `python_security_flow._calls_of`). Findings are unchanged (test: cached == recomputed == repeated).
- **Evidence:** interleaved A/B (to cancel sandbox noise), median **975 ms → 813 ms (≈ −17 %)** on medium static analysis. A plain sequential before/after was inconclusive because of noise — the interleaved design is why the number is trusted at all. Remaining uncertainty: single machine, n small.
- **Trade-off:** extra memory per analysed file for the call list, released with the file's facts.

### 2. Unindexed foreign-key child columns (migration `0014`)
- **Bottleneck:** at 1,719 files a no-op incremental index took 7.5 s, of which the publish step's `DELETE FROM indexed_files …` (generation switch) took ≈ 5.3 s: `file_imports.resolved_file_id`, `relationships.source_file_id` and `relationships.target_file_id` had no standalone index, so every deleted parent row scanned the child tables (SQLite enforces FK actions this way).
- **Change:** three single-column indexes (`ix_file_imports_resolved_file_id`, `ix_relationships_source_file_id`, `ix_relationships_target_file_id`), added by migration `0014`. `EXPLAIN QUERY PLAN` confirms the index is used (tested). Composite indexes leading with `snapshot_id` cannot serve these lookups.
- **Evidence (large, repeat 3, same machine):**

| Operation | Before | After |
|---|---|---|
| `index_incremental_noop` median | 7,530 ms (SQL ≈ 5,868 ms) | **2,105 ms** (SQL ≈ 913 ms) |
| `index_full` min / median | 2,514 / 7,952 ms | 2,582 / 2,944 ms |

  `index_full` *minimum* is unchanged within noise (+2.7 %), so the write overhead of three extra indexes is small; its median before was inflated by the same delete cost on repeats. Previously, incremental indexing at this scale was *slower* than a full re-index; now it is faster.
- **Trade-offs:** three more indexes to maintain on write; slightly larger database. Rollback: `alembic downgrade 0013`. SQLite-specific: PostgreSQL does not auto-index FK child columns either, so the same indexes help there, but this was not measured.

### Investigated and deliberately *not* changed
- Per-file N+1 queries in analysis persistence: ≈ 30 ms of a multi-second run — not worth the risk.
- `safe_join` resolving the root per call: ≈ 8 % of no-op scan time; left as is (security-sensitive path code).
- **No new application cache was added.** Profiling found no read path that justified one (API reads are 3–7 ms on medium, 1–2 SQL-bound queries each). The existing generation/sha256-based file reuse is the incremental cache, and it is tenant-scoped through the repository→workspace relationship. A cache would add invalidation risk without a measured benefit.

## Indexing and incremental correctness
Unchanged design, now regression-tested: a run builds a new snapshot generation and publishes it in one transaction, so a partial index is never treated as complete; unchanged files are reused only when sha256, `ANALYSIS_VERSION` and language all match; deletes/renames/additions are reconciled (test: incremental after edit+delete+rename+add equals a fresh full index, file-for-file); on a truncated scan removals are not reported; a per-repository running-snapshot unique index prevents concurrent indexing of the same repository.

## Scheduling and admission
- Existing: `analysis_max_concurrent_runs`, `test_max_concurrent_runs`, per-run time budgets, per-repository exclusivity, subprocess output caps.
- **New:** `NOVA_INDEX_MAX_CONCURRENT_RUNS` (default 4): indexing admission across all repositories; excess requests get HTTP 429 *before* any work or snapshot is created. The per-repository conflict (409) takes precedence. Job states are the accurate persisted states (`running`/`completed`/`completed_with_warnings`/`failed`).
- **Not implemented:** a priority queue, memory-aware admission, cancellation of running jobs, transient-only retry policy. Jobs run in-process via `BackgroundTasks`; there is **no hard memory or CPU isolation** and none is claimed. Time budgets are cooperative.

## Graph and impact analysis
BFS uses a visited set (cycle-safe), `max_depth` and `max_nodes`, and reports `truncated` and `more_beyond_depth` explicitly (existing, covered by `test_graph_intelligence.py`). Complexity is O(V+E) in the visited region; cycle detection is Tarjan, O(V+E), iterative (deep chains tested).

## Database notes
SQLite only is verified. Indexes were added only for the measured DELETE path. Listing endpoints are paginated (page-size caps enforced; pagination completeness/disjointness tested). SQLite serialises writers; SQLite's `EXPLAIN QUERY PLAN` was used, PostgreSQL plans were not examined.

## Frontend
- Every page route is lazily loaded (`react-router` `lazy`), with an accessible `role="status"` fallback. **Entry bundle: 669.94 kB (gzip 179.74 kB) → 253.15 kB (gzip 78.38 kB)**; pages are 0.25–28.5 kB chunks plus a shared 121.6 kB chunk. These are build sizes, not runtime load-time measurements, and were not tested in a browser.
- File and symbol lists pass the `AbortSignal` from react-query to `fetch`, so a superseded search request is cancelled; an aborted request is never shown as a "cannot reach the API" error. Symbol search was already debounced (300 ms).
- No virtualization or heavy UI library: lists are server-paginated (25–50 rows).

## Security
Benchmarks run on generated fixtures in a throw-away database; results contain counts, timings and bounded normalised SQL text only — no file contents, parameters, tokens or paths of real repositories (tested). `cProfile` output goes to the console only. The shared analysis memo lives on a per-file facts object and cannot cross tenants. Authorization is untouched.

## Known limits
Single-machine, 2-CPU, noisy timings; no production data; no multi-process, PostgreSQL, Windows or real-browser measurements; the 2× budget cannot detect small regressions; large-profile numbers are from 3 repeats.

## Concurrency finding: per-row GIL hand-off in `sqlite3` (0.19.0)

Measured on the production-mode rehearsal (WSL2, one API process, NOVA's own backend as the dataset: 283 files, 1,460 import edges).

| Step | Result |
|---|---|
| Symptom | `GET /repositories/{id}/metrics` p50 11 ms alone, **893 ms** at 8 concurrent clients; throughput 86 -> **9.8 req/s** |
| Profile (cProfile, 10 calls) | ~15 ms per request, no hotspot: cycle analysis ~2 ms, the rest small SQL and Python |
| Hypothesis 1: GIL switch interval | `sys.setswitchinterval(0.0005)` vs default: no change (8.5 vs 9.8 req/s) - rejected |
| Isolation | Same work in 8 threads without HTTP: CPU part 653 -> 583 ops/s (fine); SQL part 304 -> **16** ops/s |
| Raw SQLite | aggregate queries scale (2,124 -> 1,922 ops/s); the 1,460-row edge query falls **1,475 -> 14.6** ops/s |
| Cause | CPython's `sqlite3` releases and re-acquires the GIL around every `sqlite3_step`, i.e. per result row; with 8 threads each hand-off queues behind the others |
| Fix | `graph_queries.file_edges` loads nodes and edges as **one row** (`group_concat`) on SQLite; identical results (equivalence test), cap preserved |
| Result | edge query 21.9 -> 1,701 ops/s at c8; `/metrics` c8 p50 **95 ms**, **74 req/s**; `/dependencies/cycles` c8 102 req/s |
| Not fixed | paginated lists with wide rows (findings: 66 KB per page) still lose ~5x throughput at c8; a thread-limiter A/B (40/4/2 threads) did not help, so nothing was added |

Rule of thumb for new code: on SQLite, avoid returning thousands of rows per request on hot paths; aggregate in SQL or return one packed row.
