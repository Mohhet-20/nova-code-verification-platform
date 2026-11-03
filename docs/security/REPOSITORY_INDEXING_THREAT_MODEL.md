# Repository Indexing Threat Model (Phase 02)

**Assumption:** the repository is untrusted data. NOVA runs locally under the user's account; the API is intended for
loopback use. This document covers the indexing and navigation surface only; see `docs/architecture/SECURITY_MODEL.md`
for the platform model.

| # | Threat | Mitigation | Test evidence |
|---|---|---|---|
| 1 | Repo code executes during indexing (setup.py, conftest, package scripts, import side effects) | Files are read as bytes and parsed by tree-sitter only; no `import`, `exec`, subprocess, package manager or build tool is ever invoked on repo content. `importlib` is used only to load the pinned tree-sitter grammar modules by hard-coded name, never anything derived from repository content. Git inspection uses fixed argument arrays with hardened config (Phase 01). | `test_repo_code_is_never_executed` (setup.py/conftest.py/x.py write a marker if run; marker must not exist) |
| 2 | Symlink / junction traversal reads files outside the repo | `lstat`-based walk; symlinks and Windows reparse points are skipped (`SYMLINK_REJECTED`); each directory's canonical path must stay inside the root; final file path re-validated with `safe_join` | `test_symlinks_are_not_followed`, `test_directory_escaping_root_is_skipped` |
| 3 | Credentials indexed or exposed | Credential-like names (`.env`, keys, `.pem`, `id_rsa`, tokens…) are skipped and counted without recording the name; hard-coded safety rules cannot be overridden by `.gitignore`; no string-literal values or file contents are stored | `test_secrets_and_junk_never_indexed`, `test_secret_like_paths_are_counted_but_never_named`, `test_no_source_or_secret_values_in_any_response`, `test_diagnostics_report_reasons_without_leaking_secret_names` |
| 4 | Resource exhaustion (huge repos, huge files, symbol floods, deep nesting) | Limits: files, depth, per-file bytes, total bytes, wall-clock budget, symbols per file, diagnostics; parser crashes and `RecursionError` are contained per file | `test_max_files_truncates`, `test_total_bytes_limit`, `test_time_budget_truncation`, `test_symbol_limit_is_enforced`, `test_deeply_nested_input_does_not_crash`, `test_extractor_crash_is_contained` |
| 5 | Malformed / adversarial source crashes the index | Parse errors yield `partial`/`failed` with sanitized diagnostics (line numbers only, never source text); other files continue | `test_syntax_error_is_partial_not_fabricated`, `test_syntax_error_file_makes_run_completed_with_warnings` |
| 6 | Corrupt or half-built index published | Generation staging; atomic publish; failure deletes only staged rows | `test_failed_run_keeps_previous_index` |
| 7 | Concurrent runs corrupt state | Partial unique index on running snapshots; 409 on conflict | `test_concurrent_index_request_conflicts` |
| 8 | Cross-repository leakage | Every query is scoped by workspace → repository → current generation; file/symbol lookups verify repository ownership | `test_repository_isolation` |
| 9 | Injection via search input (LIKE wildcards, huge input) | Parameterized queries; `%`, `_`, `\` escaped; length limits; page-size caps | `test_search_treats_wildcards_literally`, `test_invalid_input_is_rejected` |
| 10 | Unauthenticated access when a token is configured | Same bearer dependency as Phase 01 on all new routes | `test_intelligence_endpoints_require_token_when_configured` (covers `/capabilities` too) |
| 11 | Source sent to external services | No network calls in the indexing/navigation path; no AI provider is implemented | Code review plus a grep of `services/intelligence` and `indexer.py`: no HTTP client, subprocess, `exec`, `eval` or `os.system` |
| 12 | Log leakage | Logs carry error type and ids only; no file contents, no symbol text from repo | Phase 01 log tests; pipeline logs only `snapshot_id`, `error_type` |
| 13 | Migration destroys user data | Backfill before NOT NULL; FK enforcement handled in `env.py`; integrity check after | Manual migration verification recorded in the verification log (data-preservation check is a scripted manual run, **not** an automated test) |

## Residual risks and non-goals

* `/capabilities` reports parser versions and configured limits; it requires the same token as every other route.
* Parsers (tree-sitter C code) are a native attack surface for malformed input; mitigations are per-file containment
  and size limits, not sandboxing. A parser segfault would terminate the backend process.
* Time-of-check/time-of-use races (a file swapped for a symlink mid-scan) are narrowed by `lstat` + re-validation, not
  eliminated; this is a local single-user tool.
* A repository path that is a network share or has unusual filesystem semantics is not specially handled.
* Hard-link de-duplication relies on inode numbers; where the filesystem reports none, duplicates are indexed twice.
* Freshness shows relative paths of changed files (up to 20) to the authenticated caller.
* Windows behaviour (reparse points, path casing, long paths) is implemented defensively but **not verified** here.
