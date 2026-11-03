# Bug localization, root-cause analysis and evidence-based debugging (Phase 06)

NOVA turns a test failure into a stored, de-duplicated **failure report**, ranks the repository locations most worth reading first (a **heuristic**, never a probability), builds an **investigation** that keeps observed facts apart from unverified hypotheses, and checks a user-approved rerun against the original failure. No part of this phase edits repository files, guesses a revision, or claims a bug is fixed.

## Data model (migration `0006`)
`failure_reports` (unique `repository_id + fingerprint`), `failure_frames` (unique `failure_id + idx`), `failure_occurrences` (unique `failure_id + execution_run_id`), `localization_candidates` (unique `failure_id + rank`), `investigations` (at most one non-closed per failure, enforced by a partial unique index), `verification_runs`. All are scoped to a repository and workspace; every endpoint resolves the repository through the caller's workspace first, so another repository's ids return 404.

## Ingestion
`POST /repositories/{id}/failures/ingest` with `source`:

| source | input | notes |
|---|---|---|
| `pytest_text` | pasted pytest failure text | frames `path:line: in func`, `path:line: ExcType`, `E   Type: msg` lines |
| `python_traceback` | a pasted Python traceback | `File "...", line N, in f` frames |
| `junit_xml` | JUnit XML | failures and errors; collection errors (`classname=""`) become `collection_error`; DOCTYPE/ENTITY is rejected |
| `execution_run` | `execution_run_id` of a Phase 05 run | uses the failures the run already stored; 409 if the run is unfinished or has none |

Only text sent in the request (or stored by an earlier approved run) is parsed. NOVA never reads log files or any supplied path, never executes anything during ingest, and never invents a commit hash (`revision` is null unless known). Text is limited by `NOVA_DEBUG_MAX_REPORT_BYTES` (default 65 536, HTTP 413) and frames by `NOVA_DEBUG_MAX_FRAMES` (60). Control characters and ANSI codes are stripped, the repository root becomes `<repo>`, remaining absolute paths become `<path>/name`, and credential-like values are replaced with `[REDACTED]`. A path that resolves outside the repository (`..`, other roots) is never mapped to a file; it is shown as `<outside-repository>/name` with a warning. Site-packages and the standard library are labelled `dependency` and `runtime` scope.

## Fingerprints and de-duplication
`sha1` over: fingerprint version, category, exception type (last dotted segment), the message **structure** (hex → `H`, quoted strings → `S`, numbers → `N`, first line, 200 chars), the test id without `[params]`, and the last 6 repository frames as `(path, function)`. Timestamps, random ids, absolute paths and line numbers are excluded. A matching fingerprint in the same repository records a recurrence: `occurrence_count` increases, `last_seen_at` updates, `first_seen_at` is kept, and a resolved or ignored failure is reopened. Failures are never merged across repositories.

Limitations: edits that only move code do not split a failure; two failures raised on different lines of the same function with the same message and test are one failure; renaming a file or function creates a new one; a frame whose line falls outside every indexed symbol loses its function name.

## Localization
Evidence is gathered per candidate (file/symbol), keyed by `sha1(type|ref)` so one observation counts once, summed, and floored at 0. Output is the top `NOVA_DEBUG_MAX_CANDIDATES` (default 10), sorted by score, role rank, path, line, stable id, so identical stored evidence gives identical output.

| evidence type | default weight | meaning |
|---|---|---|
| `exception_origin` | 5.0 | the exception surfaced on this repository line (final frame) |
| `immediate_failure` | 3.5 | deepest repository frame; the exception came from outside the repository |
| `caller_context` | 2.0, −0.25 per hop, floor 0.75 | repository frame above the origin |
| `called_on_failing_line` | 3.0 | no repository frames: a resolved call on the failing line of the test |
| `called_by_failing_test` | 1.0 | resolved call elsewhere in the failing test (×0.5 if the resolution was ambiguous) |
| `test_imports_file` | 0.75 | the failing test's file imports this file |
| `finding_on_frame_line` / `finding_in_symbol` / `finding_in_file` | 2.0 / 1.0 / 0.4 (total capped at 3.0) | open static-analysis findings |
| `executed_in_measured_run` / `not_executed_in_measured_run` | +0.4 / −0.5 | measured coverage from a different run; the failure's own run is skipped as circular |
| `changed_since_failure` | see `DEFAULT_WEIGHTS` | file content hash differs from the indexed snapshot at ingestion |
| related failures sharing files | see `DEFAULT_WEIGHTS` | other failures with frames in the same file |

Override individual weights with `NOVA_DEBUG_WEIGHTS_JSON` (a JSON object of known keys); an invalid object is ignored and reported in `notes`. The response always includes the weights used.

**Roles** distinguish the exception origin, the immediate failure, caller context, and code called by the test. **Confidence**: `high` = strong role (origin, immediate failure, or call on the failing line) with ≥2 evidence types, rank 1, and a margin ≥ 1.5 over rank 2 (or the only candidate); `medium` = strong role or score ≥ 3; otherwise `low`. Confidence describes evidence strength, not the chance of a defect. If there are no candidates or the best score is below 1.5 the state is `insufficient_evidence` and the `missing_evidence` list says what is absent (no repository frames, no dependency data, no measured coverage, a stale index, ...).

## Investigations
`POST failures/{id}/investigations` builds deterministic content from templates and stored evidence: **observations** (facts), **hypotheses** (`kind: hypothesis`, `status: unverified`, with supporting and contradicting evidence and verification steps), missing information, next tests, related findings and failures, dependency impact, and limitations. No AI provider is used, so nothing is invented; any future provider output must be labelled and may not add evidence. Closing requires the user's own `resolution` (`fixed_by_user`, `not_a_bug`, `cannot_reproduce`, `wont_fix`, `duplicate`) and notes are scrubbed of credentials.

**Impact** (`impact`) walks resolved file dependents breadth-first to `NOVA_DEBUG_IMPACT_DEPTH` (default 2, max 4, at most 200 nodes). Cycles are handled. Relationships are `confirmed_import` (resolved, observed) or `likely_association`; unresolved imports, undiscovered tests, an empty graph and truncation are listed as limitations. Missing data means *unknown*, not *no impact*.

## Verification
`POST investigations/{id}/verification`: without `confirm` it **only previews** the request (`executes: false`, the exact test ids, and a statement that execution is a restricted subprocess, not a sandbox). With `confirm: true` it starts a run through the Phase 05 workflow: targets must be discovered tests (invalid ids are 422), one active run per repository, `viewer` roles are refused (403). If the test infrastructure is unavailable the outcome is `INCONCLUSIVE`.

| outcome | meaning |
|---|---|
| `PENDING` | the run has not finished |
| `PERSISTS` | the same fingerprint occurred again (the occurrence is linked and counted) |
| `NOT_REPRODUCED_IN_VERIFICATION_RUN` | the run completed, collected and passed ≥1 test with 0 skipped and 0 errors, and no matching failure occurred; this is **not proof of a fix** |
| `CHANGED_FAILURE` | the same test failed with a different fingerprint |
| `INCONCLUSIVE` | the run was rejected, errored, timed out, or its result cannot be compared |

NOVA never reports "fixed" or "permanently fixed".

## API (under `/api/v1/repositories/{id}`)
`POST failures/ingest`, `GET failures` (`page`, `page_size`≤100, `status`, `category`, `q`, `path`, `sort`=`last_seen|first_seen|occurrences|category`, `order`), `GET|PATCH failures/{fid}`, `GET failures/{fid}/localization`, `GET failures/{fid}/related`, `POST failures/{fid}/investigations`, `GET investigations`, `GET|PATCH investigations/{iid}`, `POST investigations/{iid}/verification`, `GET investigations/{iid}/verifications`. Invalid filter, sort or enum values are 422. Responses contain repository-relative paths only.

```bash
curl -X POST $API/repositories/$R/failures/ingest -H 'content-type: application/json' \
  -d '{"source":"pytest_text","text":"pkg/calc.py:3: ValueError\nE   ValueError: bad value 0\n","test_id":"tests/test_a.py::test_exc"}'
curl $API/repositories/$R/failures/$F/localization
curl -X POST $API/repositories/$R/investigations/$I/verification -H 'content-type: application/json' -d '{}'                 # preview
curl -X POST $API/repositories/$R/investigations/$I/verification -H 'content-type: application/json' -d '{"confirm":true}'   # approve + run
```

## UI
`/repositories/:id/failures` (inbox with paste-in form, filters, search, pagination, loading/empty/error states) and `/repositories/:id/failures/:failureId` (facts, frames, localization labelled heuristic with missing evidence, investigation workspace with facts and hypotheses in separate regions, verification preview → approval → side-by-side result). No success is shown until the backend reports an outcome.

## Security boundaries
Parsing is stdlib-only and never executes input. Reports are size-limited, sanitized before storage, and responses and logs carry no absolute paths or credentials (tested). Authorization and workspace/repository isolation are server-side. Verification runs repository test code in a restricted subprocess that is **not a sandbox**.

## Settings
`NOVA_DEBUG_MAX_REPORT_BYTES`, `NOVA_DEBUG_MAX_FRAMES`, `NOVA_DEBUG_MAX_CANDIDATES`, `NOVA_DEBUG_IMPACT_DEPTH`, `NOVA_DEBUG_WEIGHTS_JSON`, plus the Phase 05 `NOVA_ARTIFACT_DIR` and `NOVA_TEST_*`.

## Windows (PowerShell)
```powershell
cd backend
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
$env:NOVA_ARTIFACT_DIR = "$PWD\data\artifacts"
.\.venv\Scripts\python.exe -m alembic upgrade head
.\.venv\Scripts\python.exe -m pytest tests\test_debugging.py -q
```
Windows is **not verified** in this phase; path handling for `C:\...` frames is unit-tested on Linux only, and resource limits for reruns are not enforced on Windows.

## Troubleshooting
* *409 "not indexed"*: index the repository first; frames cannot be mapped without an index.
* *Verification 409 / 422*: run test discovery, then use discovered test ids.
* *INCONCLUSIVE*: check `NOVA_TEST_PYTHON` has pytest installed; see the run's `reason`.
* *`insufficient_evidence`*: the report has no repository frames and the failing test calls nothing resolvable; supply the full traceback or index more code.
* *413*: raise `NOVA_DEBUG_MAX_REPORT_BYTES` or trim the pasted text.

## Known limits
Python/pytest only; ranking is a transparent heuristic from static evidence; arguments and runtime values are not in a traceback so root causes remain hypotheses; no git history or blame; no AI provider; no cancellation or durable queue; not verified on Windows, PostgreSQL, Python 3.11 or in a real browser.
