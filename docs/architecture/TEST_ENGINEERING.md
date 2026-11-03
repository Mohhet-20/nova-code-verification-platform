# Test engineering (Phase 05)

Discovery, gap analysis, planning, deterministic test generation, validation, **approved** execution and measured coverage for Python/pytest repositories. Code: `backend/app/services/testing/`, `backend/app/api/testing.py`, UI at `/repositories/:id/testing`.

## Principles
* Repository code is never imported or executed during discovery, planning, generation or validation (standard-library `ast` only).
* Generated tests are **staged outside the repository** and never run automatically. Running needs: validated → approved (pinned to the SHA-256 of the reviewed bytes) → an explicit execute request.
* Nothing is estimated. Coverage appears only when coverage.py measured it; otherwise the API says `never_measured` / `not_requested` / `tool_unavailable` / `failed`. "No data" is never shown as 0%.
* Static recommendations (gaps) are labelled as such and kept apart from measurements.

## Discovery
`POST /tests/discover` reads pytest configuration statically (`pytest.ini`, `pyproject.toml`, `tox.ini`, `setup.cfg`: `python_files`, `python_classes`, `python_functions`, `testpaths`), then parses each matching file. It records test ids (functions, `Test*` classes without `__init__`, `unittest.TestCase`), parametrization, parse errors and exclusions. Source association is **by import** (high confidence) or by name (low); gap analysis uses imports only. Discovered does not mean executed or passing. Only the five latest discoveries are kept.

## Frameworks and adapters
Only pytest on Python is supported. No claim is made for other languages. To add an adapter: implement discovery returning the `DiscoveredFile` shape, a planner/generator that renders files with the `NOVA-GENERATED` header, a runner producing `RunResult` (JUnit parsing in `runner.parse_junit` is reusable), register it where `service.run_discovery` and `execution._run` choose the framework, and add tests mirroring `tests/test_testing.py`.

## Planning and templates
`plan_function` produces cases with target, purpose, preconditions, input strategy, expected outcome, category, rationale, confidence and assumptions. A case is **runnable** only when code or docs determine the outcome: docstring `>>>` examples, validation-preamble guards (`if cond: raise Exc`) and boundary values next to such guards. Return-type contract checks are low confidence. Everything else is a template: `@pytest.mark.skip` plus `NotImplementedError`, marked review-required. No expected values are invented. Async functions, generators, methods and decorated functions get templates only.

## Validation (defence in depth, not a proof of safety)
Checks: output path inside the run's staging dir, integrity (hash equals generated), size, target unchanged since planning, duplicate content, syntax, structure (at least one test function, no duplicate test names), import allow-list (the target module plus a small standard-library set), prohibited patterns (banned calls such as `eval`, `exec`, `open`; names such as `os`, `sys`, `subprocess`, `socket`; file-system, process and network attribute calls; dunder escapes; shell-like strings). A rejected candidate cannot be approved.

## Approval workflow
generated → validated | rejected → approved → executed. Approval records user, time and the hash. Execution re-hashes the staged file; a change returns 409 and nothing runs. Read-only roles get 403 on every mutating route. Export (`.../candidates/{id}/export`) copies an approved file into the repository; it never overwrites a file unless that file carries the NOVA marker and `overwrite=true`.

## Execution and isolation limits
Run states: pending, running, passed, failed, timed_out, rejected, error. One active run per repository. The runner is a **restricted subprocess, not a sandbox**: network and file system are NOT isolated and the code runs as the NOVA user. Applied controls: refusal to run as root/administrator, minimal environment (no inherited secrets), HOME/TMP under the run directory, wall-clock timeout with process-group kill, bounded captured output, and on POSIX CPU, address-space, file-size, open-file and core-dump limits (not available on Windows). Command, environment key names, exit code, tool versions and source hashes are stored. Output is scrubbed for secrets and absolute paths. There is no API to run an arbitrary command.
Generated runs use the run directory as cwd/rootdir with a generated `pytest.ini`; the repository's `conftest.py` is not loaded. Running the repository's own suite (`POST /tests/execute`) needs `confirm: true` and a prior discovery, and does load the repository's own configuration, so it executes repository code.

## Coverage
coverage.py with branch tracking via a NOVA-written rcfile. Generated runs measure exactly the target files (`include`); the suite run measures the repository (`source`) minus virtualenv/test directories. Results are per-run (`coverage_files`), with missing lines as ranges. Scope: coverage of generated tests is **not** the suite's coverage; the API reports the basis. Unmeasured targets are listed.

## Artifact retention
`NOVA_ARTIFACT_DIR` (default `backend/data/artifacts`), laid out `<repo_id>/generated/<run_id>/` and `<repo_id>/executions/<run_id>/`. Nothing is deleted automatically; remove directories manually when no longer needed.

## API (under `/api/v1/repositories/{id}`)
`POST tests/discover`, `GET tests`, `GET tests/files`, `POST tests/plan`, `POST tests/generate` (idempotent), `GET tests/generations`, `GET|POST tests/generations/{run}[/validate|/approve|/execute]`, `POST .../candidates/{c}/export`, `POST tests/execute`, `GET tests/runs[/{run}]`, `GET coverage`, `GET coverage/files`, `GET coverage/gaps`. Responses contain repository-relative paths only.

## Settings
`NOVA_ARTIFACT_DIR`, `NOVA_TEST_PYTHON` (interpreter with pytest and coverage; default NOVA's own), `NOVA_TEST_TIMEOUT_SECONDS` (120), `NOVA_TEST_MAX_OUTPUT_BYTES` (100000), `NOVA_TEST_MEMORY_LIMIT_MB` (1024), `NOVA_TEST_MAX_CANDIDATE_BYTES`, `NOVA_TEST_MAX_FILES_DISCOVERED`, `NOVA_TEST_MAX_FILE_BYTES`, `NOVA_TEST_MAX_CONCURRENT_RUNS` (1).

## Windows (PowerShell)
```powershell
cd backend
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
$env:NOVA_ARTIFACT_DIR = "$PWD\data\artifacts"
.\.venv\Scripts\python.exe -m pytest tests\test_testing.py -q
```
Resource limits are not enforced on Windows; the timeout and output cap are. Windows is not verified in this phase.

## Troubleshooting
* *Coverage `tool_unavailable`/`failed`*: the interpreter in `NOVA_TEST_PYTHON` lacks `coverage` or pytest; install both there.
* *Run `error` with "Interrupted"*: the backend restarted mid-run; start it again.
* *409 "approved version"*: the staged file changed after approval; validate and approve again.
* *429*: another run is active; wait for it.
* *Generated tests import errors*: the module could not be resolved from package structure; fix packaging or edit and review manually.

## Known limits
Python/pytest only; static planning cannot know intended behaviour so most functions yield review templates; no mutation testing; no cancellation; no durable queue; not verified on Windows, PostgreSQL or Python 3.11.
