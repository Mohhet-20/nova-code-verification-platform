# Intelligent code review, change-risk analysis and regression prevention

NOVA reviews **local Git changes**. It reads the diff and file contents from the repository on disk, maps the change onto the code index, runs the existing
static analyzers on the changed code, applies a small set of documented change checks, estimates change risk with a transparent formula, bounds the impact,
and recommends tests. It never executes tests or repository code, never modifies the working tree or Git history, and never talks to a hosted pull-request
service (GitHub, GitLab, ...). No AI provider exists, so no output is AI-assisted; every finding records `source` (`analyzer` or `heuristic`) and `ai_assisted: false`.

## Review modes
| Mode | Compares | Notes |
|---|---|---|
| `working_tree` | working tree against `HEAD` | staged and unstaged changes together |
| `staged` | index against `HEAD` | new content is read from the Git blob |
| `unstaged` | working tree against the index | |
| `revisions` | `base` against `head` (default `HEAD`) | both resolved to commit ids; uncommitted edits are ignored |

Untracked files are not part of any Git diff. They are reported in the review warnings, not analysed. A repository that is not a Git work tree returns **409**.

## Revision and diff handling
* Revisions must match a conservative pattern and are rejected when they contain `..`, `@{`, whitespace, shell metacharacters, a leading `-`, or end in `/` or `.lock`.
  They are resolved with `git rev-parse --verify --quiet --end-of-options <ref>^{commit}`; only the resulting 40-character id is used afterwards.
* Only these Git operations run: `rev-parse`, `diff` (`--raw -z`, `--numstat -z`, per-file `-U0` patch with rename detection), `cat-file blob`, `ls-files --others`.
  All use argument arrays (never a shell), `--literal-pathspecs`, a sanitized environment, `GIT_OPTIONAL_LOCKS=0` (no index refresh), timeouts and bounded output.
* User-selected paths (`paths`, at most 50) are validated as repository-relative and are **only used to filter the list Git reports**; they are never passed to Git.
  A path that is not part of the change fails the review with a clear message.
* Limits (`NOVA_REVIEW_*`): `MAX_FILES` 200, `MAX_DIFF_BYTES` 2,000,000, `MAX_FILE_BYTES` 500,000, `TIME_BUDGET_SECONDS` 90. Files beyond a limit are listed with
  `extraction_status` `not_read` or `too_large` and a warning; they are not silently dropped.
* Binary files, symlinks, submodules and type changes are reported as `binary`/`unsupported` and receive no symbols.

## Change-to-symbol mapping
Python files are parsed with `ast` on the exact new content (and the old blob, for signature, removal and error-handling comparisons). Hunk headers give the changed
line ranges; a symbol is *modified* when a changed line falls inside it (blank and comment-only gaps at module level do not count), *added* or *removed* when it
exists on one side only. Stable ids and qualified names from the index are attached **only** when the indexed file's SHA-256 equals the reviewed content
(`mapping: index`). Otherwise the symbol is parsed from the diff (`mapping: fresh_parse`, no stable id) and the review is flagged `index_stale` with a warning.
Files that fail to parse produce no symbols and a warning; symbols are never invented. Signatures contain names and shape only (defaults are shown as `=…`).

## Pipeline and status
Stages: `acquire_changes`, `normalize_changes`, `map_symbols`, `static_analysis`, `correlate_findings`, `impact_analysis`, `change_checks`, `risk_analysis`,
`test_recommendations`, `persist_findings`. Each records `ok`/`warning`/`failed`, detail and duration. Review status: `pending` → `running` → `completed`,
`completed_with_warnings` (any warning, stale index, omitted file or failed stage) or `failed` (changes could not be acquired; the stored message is scrubbed of paths).
A failed analyzer never looks like success: the stage is `failed`, the dependent risk factor is `unavailable`, and the review is `completed_with_warnings`.
Reviews run in a background task; at most 3 may be active per instance (429 beyond that).

## Findings
Each finding has a stable id and fingerprint, rule, location in the new revision, severity, category (correctness, security, reliability, maintainability,
performance, testing gap, informational), explanation, evidence, remediation, limitations, confidence, an `actionable` flag, lifecycle status and related ids.
* **Analyzer findings** reuse the analyzers on changed files and keep only findings on changed lines. They use the exact fingerprint, so a stored
  finding with the same fingerprint is linked (`related.preexisting`) instead of being counted as new. Unchanged lines and unchanged files are never attributed to the change.
* **Change checks (heuristic):** `REV-API-001` public function removed or signature broken (new required parameter, removed or reordered parameter; added defaults are not flagged),
  `REV-DEL-001` deleted/renamed module that is still imported (needs an index built before the deletion), `REV-TEST-001` changed code with no associated test
  (only when test discovery has run and no test changed in the review), `REV-INFO-001` / `REV-INFO-002` informational notes for auth-related and data-model areas.
  Touching sensitive code is **informational**, never a defect.
* **Noise control:** duplicates by fingerprint are merged (strongest wins); findings below `NOVA_REVIEW_MIN_CONFIDENCE` (default `medium`) are counted, not shown, unless
  severity is critical or high; at most 10 per file and 100 per review (counts of omitted findings are in the summary). Serious findings are never suppressed by thresholds.
* **Workflow:** `open`, `accepted`, `dismissed` (rationale ≥ 5 characters required), `follow_up`. A decision on the same fingerprint in an earlier review of the same repository is
  inherited (with its rationale and source review) but remains visible. Secrets in rationales are redacted.
* A review with no findings states that this **does not show the change is free of defects**.

## Change-risk formula (version 1.0)
`score = 100 * sum(weight_i * normalized_i) / sum(weight_i)` over **measured** factors only. Each factor is normalized by a saturating function `x / (x + k)`, so size can
never push a factor above 1 and large repositories do not automatically score high. Findings are counted once, in exactly one of two factors (already-stored vs new).
| Factor | Weight | Evidence |
|---|---|---|
| change_size | 10 | lines changed |
| files_changed | 5 | analysable code files |
| symbols_changed | 10 | changed symbols |
| public_interface | 12 | public symbol additions/removals/signature changes |
| auth_sensitive | 10 | changes in auth-related names/paths |
| data_model | 8 | changes in data-model-related names/paths |
| error_handling | 6 | changed handlers/raises |
| complexity | 9 | cyclomatic complexity of changed functions |
| existing_findings | 10 | severity-weighted stored findings on changed lines |
| new_findings | 12 | severity-weighted new findings |
| dependents | 8 | indexed dependents |
| coverage | 10 | measured uncovered share of changed lines (usually unavailable) |
| failure_history | 6 | recorded failures in changed files |
| test_results | 5 | **always unavailable**: reviews never run tests |

Categories: `critical` ≥ 65, `high` ≥ 40, `moderate` ≥ 20, otherwise `low`. The result is **unknown** (no number) when measured evidence is under 50% of the total weight or no
analysable code changed. The score is a ranking aid for regression exposure. It is **not a probability of defects**, and a high score does not mean the change is wrong.
The API returns every factor with its status (`measured`/`unavailable`), raw value, weight, contribution and evidence, plus the formula and thresholds.

## Impact
Reuses the impact analysis over the graph: direct and transitive dependents to `NOVA_REVIEW_IMPACT_DEPTH` (default 2, max 4; cycles handled, duplicates
removed by minimum depth), symbol-level callers when the changed symbol has an index id, recorded failures, and limitations (graph missing, stale, depth-limited).
**Potentially affected tests** are tests that import a changed file (`imports_changed_file`); the API states that this does not show they exercise the change.

## Test recommendations
Each item has target, behavior, reason, evidence and its source (`signature_change`, `changed_symbol`, `finding`, `removed_symbol`, ...), priority, confidence,
existing-test status and `status: recommended_only`. Nothing is generated into the repository and nothing is executed. Bounded by `NOVA_REVIEW_MAX_RECOMMENDATIONS` (30).

## Security boundaries
Server-side repository ownership and role checks on every endpoint (viewer: read only); no endpoint accepts a command or arbitrary Git arguments; repository content is
returned only as short escaped excerpts in text fields (the UI renders text, never HTML); absolute paths and secrets are scrubbed from responses, logs and stored errors;
the review is a read-only operation (verified: file bytes, `.git/index`, HEAD, refs, reflog and object counts are identical before and after all modes).

## API (all under `/api/v1/repositories/{repository_id}`)
| Method and path | Purpose |
|---|---|
| `POST /reviews/preview` | list files that would be analysed; stores and executes nothing |
| `POST /reviews` | start a review (202, status `pending`) |
| `GET /reviews` | paginated list (`status`, `mode` filters) |
| `GET /reviews/{id}` | review with stages and summary |
| `GET /reviews/{id}/summary` | headline, counts, noise control, checks run, AI disclosure |
| `GET /reviews/{id}/changes` | changed files with symbols (`change_type`, `q` filters) |
| `GET /reviews/{id}/findings` | paginated findings (`severity`, `category`, `confidence`, `status`, `path`, `actionable`, `sort`) |
| `PATCH /reviews/{id}/findings/{finding_id}` | accept / dismiss / follow up / reopen |
| `GET /reviews/{id}/risk`, `/impact`, `/test-recommendations` | risk factors, impact, recommendations |

```bash
curl -X POST localhost:8000/api/v1/repositories/$REPO/reviews -H 'content-type: application/json' -d '{"mode":"revisions","base":"main"}'
curl localhost:8000/api/v1/repositories/$REPO/reviews/$ID/findings?severity=high
curl -X PATCH localhost:8000/api/v1/repositories/$REPO/reviews/$ID/findings/$FID -H 'content-type: application/json' \
     -d '{"status":"dismissed","rationale":"Input comes from trusted config."}'
```
Add `-H "Authorization: Bearer $NOVA_API_TOKEN"` when a token is configured.

## Windows (PowerShell)
Git for Windows must be on `PATH` (`git --version`).
```powershell
cd backend
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
.\.venv\Scripts\python.exe -m alembic upgrade head
.\.venv\Scripts\python.exe -m pytest tests\test_review.py -q
```
Windows is **not verified**. Case-insensitive paths and CRLF line endings are untested; the tests create real repositories with the `git` on `PATH`.

## Troubleshooting
* *409 "not a Git working tree"*: register the repository root, or run `git init`.
* *422 on revisions*: use a branch, tag or commit that exists locally; `a..b` ranges are not accepted (use `base` and `head`).
* *Index stale / symbols without ids*: re-index (`POST /repositories/{id}/index`) before reviewing to get stable ids, dependents and callers.
* *No `REV-TEST-001` findings*: run test discovery (`POST /tests/discover`) first; without it NOVA does not claim a testing gap.
* *Files not analysed*: raise `NOVA_REVIEW_MAX_FILES`, `NOVA_REVIEW_MAX_DIFF_BYTES` or `NOVA_REVIEW_MAX_FILE_BYTES`.

## Known limits
Local Git only; Python gets full symbol/signature analysis, other indexed languages only index-based symbols when the index is current; no untracked-file review; no hosted PRs;
heuristics are narrow and a clean review is not proof of correctness; risk weights are documented judgement, not calibrated; rename detection is Git's; no AI provider;
background task is in-process (lost on restart); not verified on Windows, PostgreSQL, Python 3.11 or in a real browser.
