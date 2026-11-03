# Static analysis and code-quality diagnostics (Phase 04 / Phase 04)

Status: implemented and verified on Linux / Python 3.12. Not verified on Windows, in a real browser or on PostgreSQL.

## What it is — and is not

NOVA parses Python files with the standard-library `ast` module and runs a catalog of **pattern rules** over them. Each rule
reports *where* it matched, *why that pattern is risky*, *evidence*, a *next step* and *what the analyzer cannot determine*.
A finding is a pattern, not a confirmed defect. Nothing from a repository is imported, compiled, executed or passed to a shell.
JavaScript/TypeScript and other languages are indexed (Phases 02–03) but have **no analysis rules**; the API says so.

## Architecture

```
POST /analysis ──► begin_run (validate config, require published index, concurrency checks) ──► AnalysisRun(status=running)
                                   │ FastAPI background task (in-process, no queue, no cancellation)
                                   ▼
orchestrator.run_analysis ─► for each eligible Python file of the published index generation
     read bytes (size cap, symlink-safe) ─► sha256 ─► unchanged since last good run? skip
     ast.parse ─► collect_facts (one walk) ─► Analyzer.run (each rule isolated in try/except)
     ─► normalise ─► fingerprint ─► upsert Finding rows ─► AnalysisFileState
     ─► reconcile (resolve vanished findings only where the evidence supports it)
```

Execution is **synchronous work inside a background task of the API process**. There is no durable queue, no cancellation and
no resume. A run left `running` by a killed process is marked `failed` at the next startup. States used:
`running`, `completed`, `completed_with_warnings`, `failed`. (`pending`/`cancelled` are not implemented and never reported.)

Code layout (`backend/app/services/analysis/`): `model.py` (RuleSpec, RawFinding, Analyzer), `catalog.py` (registered analyzers),
`analyzers/` (`py_facts`, `python_correctness`, `python_maintainability`, `python_security`), `complexity.py`, `config.py`,
`fingerprint.py`, `redaction.py`, `orchestrator.py`. API: `app/api/analysis.py`, `app/schemas/analysis.py`.
Tables (migration `0004`): `analysis_runs`, `analysis_file_states`, `findings`.

## Analyzer plugin interface

An analyzer is a named, versioned group of rules: `Analyzer(id, name, version, languages, rules)`. A rule is a generator
`fn(RuleContext) -> Iterator[RawFinding]` decorated with `@rule(RuleSpec(...))`. `RuleSpec` carries id, title, description,
category, default severity, languages, version, remediation, limitations, default confidence, `OptionSpec` list (name, default,
min, max), `support` (`implemented` | `partial`) and references. `RuleContext` exposes the parsed tree, shared `facts`
(functions, nodes with scope) and the rule's validated options. `RawFinding` carries line/column, scope, a **discriminator**
(what was found, never the line number), explanation, evidence and optional confidence/severity/metric.

A rule that raises is recorded under `analyzer_errors` (rule id + exception type only; no message, source or path) and the file
is **not** treated as fully analysed for that rule.

## Rules

| Rule | Title | Category | Default severity | Default confidence | Support | Options (defaults) |
|---|---|---|---|---|---|---|
| PY-COR-001 | Comparison to None with == or != | correctness | low | high | implemented | |
| PY-COR-002 | Mutable default argument | correctness | medium | high | implemented | |
| PY-COR-003 | Unreachable code after return, raise, break or continue | correctness | low | high | implemented | |
| PY-REL-001 | Empty exception handler | reliability | medium | high | implemented | |
| PY-REL-002 | Broad exception handler | reliability | low (bare/BaseException: medium) | medium | implemented | |
| PY-REL-003 | Exception handler discards the error silently | reliability | low | medium | implemented | |
| PY-MNT-001 | Function is too long | maintainability | low | high | implemented | max_lines=80 |
| PY-MNT-002 | File is too long | maintainability | low | high | implemented | max_lines=1000 |
| PY-MNT-003 | Excessive nesting depth | maintainability | low | high | implemented | max_depth=4 |
| PY-MNT-004 | Too many parameters | maintainability | low | high | implemented | max_params=6 |
| PY-CPX-001 | High cyclomatic complexity | complexity | medium | high | implemented | max_complexity=10 |
| PY-MNT-005 | Unused import | maintainability | low | medium | partial | |
| PY-MNT-006 | Unused local variable | maintainability | info | medium | partial | |
| PY-MNT-007 | Duplicate function bodies in one file | maintainability | low | medium | partial | min_statements=4 |
| PY-SEC-001 | Dynamic code execution (eval/exec) | security | medium | medium | implemented | |
| PY-SEC-002 | Shell command execution | security | medium (runtime-built command: high) | medium | implemented | |
| PY-SEC-003 | Hardcoded credential-like value | security | medium (known token shape: high) | medium | partial | |
| PY-SEC-004 | Insecure temporary file pattern | security | medium | high | implemented | |
| PY-SEC-005 | Potentially unsafe deserialization | security | medium | medium | implemented | |

`GET /analysis/rules` returns the live catalog with the effective (configured) values. "Partial" means the rule is known to
miss cases or to be heuristic; its `limitations` text says how. Cross-file duplicate detection and security data-flow
(taint) analysis are **not implemented**.

### Supported languages

| Language | Analysis |
|---|---|
| Python | implemented (19 rules above; standard-library AST) |
| JavaScript / TypeScript | unsupported — indexed only |
| Everything else | unsupported — metadata only |

## Confidence semantics

* **high** — the pattern is syntactic and unambiguous in the AST (for example `x == None`). False positives come from intent, not detection.
* **medium** — the match is structural but depends on context the analyzer cannot see (for example whether the input of `eval` is trusted, whether a name is used via `globals()`).
* **low** — heuristic naming/shape match with notable false-positive risk (for example a credential-like name with a short literal).

Confidence is about *detection*, not about severity. A rule can emit lower confidence than its default for a particular match.
Severity precedence: repository configuration override > severity set by the match > rule default.

## Cyclomatic complexity

`complexity = 1 + decision points`. Each of these adds 1: `if`/`elif`, conditional expression, `for`, `async for`, `while`,
each `except` handler, each non-wildcard `match` case, each extra operand of `and`/`or`, and each `for` clause and `if` filter in
a comprehension. `assert`, `with`, `try`/`finally` and loop `else` add nothing. Nested functions and classes are separate units.
Lambdas count towards the enclosing function. It is the common extended-McCabe variant, not a formally verified metric.

## Configuration: `.nova/analysis.json`

Optional, read from the repository (never executed). Size ≤ 64 KiB, UTF-8 JSON object, regular file (symlinks are not followed).
Unknown keys, unknown rules, unknown options, wrong types and out-of-range values are rejected — **the run is refused with
HTTP 422 and every problem listed**, instead of silently using defaults.

```json
{
  "version": 1,
  "exclude": ["migrations/**", "vendor/**"],
  "exclude_generated": true,
  "rules": {
    "PY-MNT-005": {"enabled": false},
    "PY-COR-001": {"severity": "high"},
    "PY-CPX-001": {"options": {"max_complexity": 15}}
  },
  "languages": {"python": {"rules": {"PY-MNT-001": {"options": {"max_lines": 120}}}}}
}
```

* `exclude` uses gitignore syntax; patterns must be relative and contain no `..`.
* `exclude_generated` (default true) skips `*_pb2.py` and files whose first lines say "generated" / "DO NOT EDIT".
* Language-specific rules are applied after the global `rules`.
* **Trust boundary:** the file belongs to the repository being analysed, so it is untrusted. It can tune or disable *rules* and
  exclude *files*; it cannot change server limits, authorisation, redaction, path validation or the "never execute" guarantee.
  Exclusions and disabled rules are never presented as success: they are counted in the run (`files_excluded`, `rules_disabled`),
  appear in warnings and in the summary notes, and findings affected are resolved with an explicit reason (see below), not "fixed".
* The API exposes the effective configuration read-only (`GET /analysis/config`).

Server limits (environment, not repository-controllable): `NOVA_ANALYSIS_MAX_FILES` (5000), `NOVA_ANALYSIS_MAX_FILE_BYTES`
(500000), `NOVA_ANALYSIS_TIME_BUDGET_SECONDS` (120), `NOVA_ANALYSIS_MAX_FINDINGS_PER_FILE` (200),
`NOVA_ANALYSIS_MAX_CONCURRENT_RUNS` (2). Exceeding a limit yields `completed_with_warnings`, never silent success.

## Finding model

Persisted per finding: id, repository, rule id and version, analyzer id and version, repo-relative path, language, line/column
range, enclosing scope and (when the index knows it) stable symbol id, severity (`critical|high|medium|low|info`), category
(`correctness|maintainability|reliability|security|complexity`), title, explanation, evidence, remediation, limitations,
confidence, optional metric value, fingerprint, status, resolution/suppression reason, reopen count, first/last seen, first/last
run id. **Source excerpts are not stored.** Free text passes through `redaction.scrub` (known token shapes and
`password=…`-style assignments are replaced by `[REDACTED]`), and credential rules build their evidence without the value in
the first place.

## Lifecycle

| Status | Meaning |
|---|---|
| new | first seen in the latest run |
| existing | seen in more than one run |
| reopened | was resolved, reported again (`reopened_count` increments) |
| suppressed | a user suppressed it with a reason; analysis keeps tracking it but does not change that status |
| resolved | no longer reported, with `resolution_reason` |

Resolution reasons: `fixed` (the rule ran on the file in this run and no longer matches), `file_removed`, `rule_disabled`,
`file_excluded`. The summary reports them separately so disabling a rule is never counted as fixing code.

**A finding is not resolved when** the file failed to parse, was skipped (size/limit/time budget), could not be read, the rule
raised an error for that file, or too many findings were produced in the file (truncated). Those findings keep their previous
status and the run reports why.

User transitions (`PATCH /findings/{id}`): open → `suppressed` (reason of 5–300 characters required) and `suppressed` → `existing`.
A `resolved` finding cannot be suppressed (409). Every other status is owned by analysis (422 for invalid values).

## Fingerprinting

`fingerprint = sha256(rule id, repo-relative path, enclosing scope, rule discriminator, ordinal)[:40]`. The discriminator
describes what was found (parameter name, operator, called function …), **never the line number**; the ordinal separates
identical constructs in one scope. Inserting or deleting unrelated lines keeps identity. Renaming the file or the enclosing
function, or changing the construct, creates a new finding and resolves the old one. Adding an identical construct *before* an
existing one in the same scope shifts later ordinals (documented limitation). Repeated runs on unchanged input create no duplicates
(unique `(repository_id, fingerprint)`).

## Incremental analysis

Each file's last result is stored in `analysis_file_states` (content sha-256, hash of the effective configuration plus analyzer
versions, outcome). In `incremental` mode a file is skipped when its content, configuration and analyzer versions are unchanged
and it was analysed successfully; `full` mode re-analyses everything. Limits: files come from the **published index generation**,
so edits are seen only after re-indexing (`index_changed_since_run` flags this); the file is still read and hashed on every run
(cost is proportional to repository size, not to the number of changed files); cross-file rules do not exist, so per-file caching
is sound today and must be revisited if one is added.

## API (prefix `/api/v1/repositories/{repository_id}`)

| Method and path | Purpose |
|---|---|
| `POST /analysis` `{"mode":"incremental"\|"full"}` | start a run → 202 with the run. 409 no index / already running, 422 invalid config, 429 too many runs |
| `GET /analysis/status` | latest run, state, config validity, whether the index changed since |
| `GET /analysis/runs`, `GET /analysis/runs/{run_id}` | history (paginated) |
| `GET /findings` | filters `severity`, `category`, `status`, `rule_id`, `language`, `path` (substring), `q`, `min_confidence`; `sort` ∈ severity,last_seen,first_seen,path,rule,line; `order`; `page`, `page_size ≤ 100` |
| `GET /findings/{id}`, `PATCH /findings/{id}` | detail; suppress / reinstate |
| `GET /analysis/summary` | counts by severity, category, status, resolution reason; top rules; run diagnostics; notes. **No composite score** |
| `GET /analysis/rules`, `GET /analysis/config` | catalog with effective settings; read-only effective configuration |

Every route resolves the repository through the caller's workspace (another workspace's repository or a finding id from a
different repository returns 404), requires the API token when one is configured, requires write access for `POST`/`PATCH`,
and returns repo-relative paths only. Errors use the standard envelope without stack traces or paths.

```powershell
$h = @{ Authorization = "Bearer $env:NOVA_API_TOKEN" }   # omit when no token is configured
$b = "http://127.0.0.1:8000/api/v1/repositories/<id>"
Invoke-RestMethod -Method Post  -Uri "$b/analysis" -Headers $h -ContentType 'application/json' -Body '{"mode":"incremental"}'
Invoke-RestMethod -Uri "$b/analysis/status" -Headers $h
Invoke-RestMethod -Uri "$b/findings?severity=high&category=security&sort=severity&page_size=25" -Headers $h
Invoke-RestMethod -Method Patch -Uri "$b/findings/<finding-id>" -Headers $h -ContentType 'application/json' `
  -Body '{"status":"suppressed","reason":"Reviewed: input is a constant in this module"}'
```

## Security controls

* No import, `compile`, `exec`, `eval`, subprocess or shell on repository content; only `ast.parse` on bytes read with a size cap.
* Paths: repository root resolved against the registration allow-list; files are opened through `safe_join` with symlink checks; findings store relative paths only.
* Secrets: values are never put into findings; text is scrubbed; logs record rule ids, counts and exception *types* only. Tests assert the secret is absent from API output, run diagnostics and captured logs; the live check also grepped the server log and the SQLite file.
* Limits on files, bytes, findings per file, run time and concurrent runs; a partial run says so.
* Untrusted configuration is strictly validated and cannot reach server limits or security controls.

## False-positive and false-negative risks

* PY-MNT-005 (unused import) can flag imports used only for side effects, re-exports in `__init__.py`, or names referenced via `globals()`/`__all__` tricks; string annotations are considered, dynamic access is not.
* PY-MNT-006 (unused local) misses uses through `locals()`/`eval` and flags intentionally ignored values.
* PY-SEC-001/002/005 flag the call, not the data flow: a constant argument or a trusted input is still reported (reported at severity low where the argument is a literal) and an unsafe call hidden behind an alias or wrapper is missed.
* PY-SEC-003 is heuristic: credential-like names with real-looking literals are reported, so test fixtures and sample values appear; secrets in unusual shapes or built at runtime are missed. Known token shapes are reported with high confidence.
* PY-REL-002/003 cannot judge whether a broad handler at a process boundary is appropriate.
* PY-MNT-007 compares function bodies inside one file only.
* Files that cannot be parsed (including Python 2 syntax) are reported as warnings and produce no findings.

## Adding and testing an analyzer or rule

1. Add a function in a module under `analyzers/` (or a new module) decorated with `@rule(RuleSpec(...))`; choose a stable id
   (`<LANG>-<AREA>-<NNN>`), write `remediation` and honest `limitations`, declare `OptionSpec`s with bounds, and give each
   `RawFinding` a discriminator that excludes line numbers.
2. For a new analyzer, construct `Analyzer(id, name, version, languages, rules)` and register it in `catalog.ANALYZERS`;
   bump the analyzer or rule version when behaviour changes (this invalidates per-file caches).
3. Add a `(rule_id, bad, good)` row to `CASES` in `backend/tests/test_static_analysis.py` (true and false positive), and tests for any option.
4. Update the rules table here. `test_every_catalog_rule_has_metadata` enforces complete metadata.

```powershell
cd backend
.\.venv\Scripts\python -m pytest tests/test_static_analysis.py -q
.\.venv\Scripts\python -m ruff check .
```
