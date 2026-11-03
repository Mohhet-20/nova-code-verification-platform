# NOVA Security Model (Phase 01)

## Trust boundaries and honest limits
- **Single local user.** There is one local identity (`local-dev`, role `owner`). Roles exist in the data model and write endpoints
  enforce them, but **multi-user isolation is not implemented** and must not be assumed. Real authentication is Phase 14.
- **Optional bearer token** (`NOVA_API_TOKEN`) protects every `/api/v1` route except `/health` and `/ready`; compared in constant time. Without it
  the API trusts any client that can reach it. The server binds to `127.0.0.1` in the provided scripts; do not expose it without a token, and
  even with a token this version has no TLS, rate limiting or user accounts.
- **Registration is a powerful operation:** anyone who can call the API can index any readable directory on the server machine
  (metadata only, never contents through the API). Use `NOVA_REGISTRATION_BASE_DIRS` to restrict where repositories may live.
- **Browser filesystem access:** browsers cannot safely give a web page arbitrary local paths, so repositories are registered **on the
  backend** by typed absolute path; the browser never reads local files.

## Threats and mitigations
| Threat | Mitigation | Tested by |
|---|---|---|
| Path traversal / absolute-path injection | `resolve_repository_root` + `safe_join` reject `..`, absolute paths, NUL bytes, escapes; clients never supply file paths after registration (only relative paths produced by the indexer are stored) | `test_paths_security.py`, `test_client_supplied_paths_cannot_read_files` |
| Symlink / junction escape | Symlinks and Windows reparse points are **never followed**; each directory's canonical path is re-checked against the root; registration resolves symlinked roots before the allow-list check | `test_symlinks_are_not_followed`, `test_directory_escaping_root_is_skipped`, `test_symlinked_root_resolves…` |
| Secret indexing / leakage | Credential file names (`.env*`, keys, tfstate, …) are skipped and **not even recorded by name**, only counted; only `.env.example`-style templates are indexed; file contents are never stored, returned or logged | `test_secrets_and_junk_never_indexed`, `test_no_secret_content_leaks_into_results`, `test_files_endpoint_exposes_metadata_only` |
| Resource exhaustion | Limits on files, depth, per-file bytes, total bytes; oversized `.gitignore` ignored; bounded git output; page-size caps; input length caps | `test_indexer.py`, validation tests |
| Unsafe subprocess use | Only `git`, via argument arrays, `shell=False`, timeout, output cap, scrubbed environment, `core.fsmonitor` disabled, no pager/prompts | `test_git.py` |
| Malicious repository content / prompt injection | Repository content is treated as data. The baseline planner never reads contents and never executes anything; instructions inside task text or files cannot cause actions | `test_untrusted_instructions_in_task_text_are_inert` |
| Information leakage in errors/logs | Uniform error envelope; unhandled errors return no internals; validation errors don't echo submitted values; logs carry method/path/status only | `test_unhandled_error_hides_internals`, `test_validation_errors_do_not_echo_input`, `test_logs_do_not_contain_request_bodies` |
| Unintended external transmission | No outbound network code exists in the application; AI providers are unimplemented and rejected by configuration | code review; `test_unknown_ai_provider_is_rejected` |
| Unsafe patches / command execution | **Not implemented** in this phase: NOVA never writes to repositories or executes commands. Suggested commands are text only (`executed` is always `false`) | `test_planning_never_modifies_repository` |

## Known residual risks (Phase 01)
- Git is a program that reads repository config. Known command-running options (`core.fsmonitor`) are disabled and system config is ignored,
  but git on a hostile repository is not formally sandboxed. Treat unknown repositories with care.
- TOCTOU: a directory can change between the scan's checks and reads; the scan re-validates each file's resolved location, which narrows but
  does not eliminate this on a hostile local filesystem.
- `.gitignore` handling approximates git (per-directory specs, no cross-file negation, no global/`info/exclude` rules).
- SQLite has no row-level security; workspace scoping is enforced in application queries only.
- Secret-file detection is name-based; a credential stored in an ordinary-named file *is* indexed as metadata (hash, size) but its contents are never stored.
- The token is stored in the browser's sessionStorage when entered in Settings.
- Windows behaviour (junctions, long paths, path casing) has code paths but was **not exercised on Windows** in this phase.

## Requirements for future phases
Command execution (Phase 06) and patch application (Phase 05) require: explicit per-action human approval, an allow-list of commands, no shell,
timeouts and output caps, secret redaction of captured output, and audit events.
