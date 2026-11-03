# Final benchmark report (Phase 18)

**Environment:** Linux 6.8.0-138 x86_64, CPython 3.12.14, SQLite 3.53.1, 2 shared CPUs (noisy), assessed 2026-10-21. Synthetic generated fixtures only; no confidential content. These are **sandbox measurements, not production numbers**, and are not comparable to any other machine. No optimization was made in this phase, so there is no before/after comparison; correctness checks ran with every measurement.

## 1. Indexing, analysis, read APIs (`python -m app.perf --repeat N`, correctness-checked)

Small fixture: 36 files, 895 lines (n=5). Medium fixture: 314 files, 11,325 lines (n=3). p95/p99 are not reported (n < 20). Python peak memory for the medium index run: ≈ 5.2 MiB (tracemalloc, separate run).

| Operation | small median | medium median | queries (medium) |
|---|---|---|---|
| index_full | 54.9 ms | 403.3 ms | 60 |
| index_incremental_one_change | 51.1 ms | 319.8 ms | 67 |
| index_incremental_noop | 53.2 ms | 320.1 ms | 64 |
| api_files_list | 4.2 ms | 5.2 ms | 9 |
| api_symbols_search | 4.8 ms | 6.1 ms | 10 |
| api_summary | 3.9 ms | 4.1 ms | 9 |
| api_file_detail | 4.1 ms | 3.8 ms | 10 |
| api_dependents | 4.8 ms | 4.5 ms | 10 |
| api_cycles | 3.9 ms | 5.8 ms | 9 |
| api_impact | 4.7 ms | 5.8 ms | 12 |
| static_analysis_full | 83.8 ms | 825.1 ms | 834 |
| security_scan (deps+code+secrets) | 65.7 ms | 451.5 ms | 33 |

The committed `benchmarks/baseline.json` could not be compared (`missing_baseline`: 12/12), so **no regression check passed or failed**; I did not overwrite the committed baseline.

## 2. Larger indexing run (`scripts/measure_indexing.py --files 1500`, one run)

First index 1.54 s (18,000 symbols); unchanged re-index 1.64 s; one-file-change re-index 1.47 s; full re-index 2.03 s; freshness check 0.096 s; symbol search avg 29.6 ms / max 50.7 ms; dependency/impact queries 5–40 ms. **Incremental re-index is not faster than a first index** (known limit 1; open).

## 3. Governance, queue and webhooks (`tests/test_acceptance_bench.py`, 120 submitted workflow items, SQLite)

| Operation | n | median | max | errors |
|---|---|---|---|---|
| review queue page (50 of 120) | 15 | 14.7 ms | 15.6 ms | 0 |
| workflow instances list | 15 | 6.0 ms | 8.1 ms | 0 |
| governance overview | 15 | 8.0 ms | 9.3 ms | 0 |
| audit page (100) | 15 | 5.9 ms | 7.1 ms | 0 |
| policy evaluation | 15 | 11.5 ms | 14.0 ms | 0 |
| inbound webhook, signed, new delivery | 40 | 10.1 ms (p95 11.6) | 12.9 ms | 0 |
| inbound webhook, duplicate | 20 | 3.3 ms | 5.1 ms | 0 |
| inbound webhook, forged signature (401) | 20 | 3.0 ms | 4.7 ms | 0 |

Timings are in-process through the ASGI test client (no network or TLS); real HTTP adds overhead. Single-user, single-process: **no concurrent-load or throughput test was run**, and no database larger than a few thousand rows.

## 4. Backup/restore (synthetic, from Phase 17)
53 MB database + 2,000 files: backup 0.64 s, verify 0.08 s, restore with checks 0.72 s.

## Targets
No pre-existing latency targets were defined. A justified proposal, to be confirmed on target hardware: API reads p95 < 200 ms at ≥ 10k files; indexing ≥ 500 files/s for first index; restore of the production data set within the agreed RTO. None is claimed as met.
