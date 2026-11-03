# Continuous verification, regression intelligence and quality gates (Phase 10)

Answers one question per change: *what evidence exists, for exactly this source state, that the change did not break the repository?*
Everything is deterministic and computed from recorded runs. **A `PASS` is evidence under a policy, not proof of correctness.**

## Architecture
`app/services/verification/`: `changes` (change detection and fingerprints), `planner` (impact-aware check plan), `engine` (execution),
`regression` (baselines, classification, coverage), `gate` (quality gate), `policy` (versioned policy), `service` (start, cancel, link,
overview, staleness), `errors`. API: `app/api/verification.py` under `/api/v1/repositories/{id}/verification`. Persistence (migration
`0010`): `cv_policies`, `cv_baselines`, `cv_runs`, `cv_checks`, `cv_events`. Planned checks and their results are `cv_checks` rows; the
regression findings, coverage summary, static result, gate decision and policy snapshot are stored on the run as JSON so a decision is
reproducible from the record. Reused, not duplicated: the Phase 05 restricted runner (`execution.begin_existing` / `run_execution`) and test
discovery, Phase 04 static analysis, the Phase 03 index for dependents, the Phase 08/09 link types, `audit`, `RequestContext` roles.

## Change detection (read-only)
Sources: `working_tree` (tracked changes vs HEAD plus untracked files), `staged`, `revisions` (two commits; `HEAD`-style names are resolved
to SHAs), `patch` (a unified diff checked against files on disk, **never applied**) and `manifest` (explicit paths). Git runs only as fixed
argument arrays and never writes; tests assert HEAD, the index, stashes and the working tree are unchanged. Paths are repository-relative,
allow-listed, symlinks and traversal are refused, file counts and sizes are capped (`NOVA_CV_*`). Each change set has a fingerprint over
paths and content hashes. A run whose source is a revision pair or staged tree is **bound** to it: if the working tree differs from the
staged/revision content, the test check is `blocked` ("differs from the working tree"), because tests would not describe that content.

## Planner
Tier 0 syntax, tier 1 static analysis (new vs previous content of each changed file), tier 2 tests, tier 3 coverage (only when requested).
Modes only widen the test scope: `focused` (tests importing the changed modules plus changed tests), `standard` (also tests of direct
dependents, needs a published index), `full` (every discovered test). Each check has a reason, required/optional, confidence and
prerequisites. No endpoint accepts a command: only these checks can run. Selection is static import association, not proof a test exercises
the change; the plan says so. An empty change set plans a blocked test check ("nothing to verify").

## Execution
The test check creates a Phase 05 execution run (allow-listed pytest invocation, timeouts, bounded and redacted output). Running needs
`confirm: true` and a non-viewer role. Runs execute as a background task; the UI polls. `cancel` takes effect between checks and rejects a
pending execution; a pytest process already running is bounded by the runner timeout, not killed. Partial results are kept. Runs left
`queued/running` by a restart are marked `error` with gate `BLOCKED` at startup. A working tree that changes while verifying yields
`INCOMPLETE` (`source_unchanged`).

## Baselines and regression intelligence
A baseline is a full test run recorded for a state, with scope, totals, failing tests, HEAD, tree fingerprint, a hash of the test
configuration and an **integrity hash** over its content. Comparison requires compatibility: same configuration hash, scope covering the
test, and an untampered record (a modified baseline is reported and not used). Classification:
* **new** — a compatible baseline covered the test's file and it did not fail there;
* **pre-existing** — the baseline failed it too;
* **resolved** — failed in the baseline, in scope now, passing now;
* **flaky** — only with repeated evidence: failed here and passed in another completed run on a byte-identical source state (look-back `cv_flaky_lookback`);
* **unclassified** — no compatible baseline: never called new, and handled conservatively by the gate.
Every item carries an explanation and evidence (baseline id, flaky runs). `GET …/runs/{id}/compare?baseline_id=` recomputes against a chosen
baseline without changing the recorded decision.

## Coverage
Only from the coverage tool's real output (`measured`); otherwise `unavailable`/`not_requested` with the tool's error. Coverage rules in the
policy with no measurement make the gate `BLOCKED`, never pass. A drop limit with no comparable baseline coverage is `BLOCKED`.

## Quality gate
Outcomes `PASS`, `FAIL`, `BLOCKED`, `INCOMPLETE`. **Precedence: FAIL > BLOCKED > INCOMPLETE > PASS.**
* FAIL — a required criterion definitively failed (failing test, new static findings over the limit, syntax error, coverage below minimum, pre-existing failures when not allowed).
* BLOCKED — required evidence could not be produced (nothing to run, no tests selected, tool unavailable, baseline required but missing, unbound revision, runner error, interrupted run).
* INCOMPLETE — evidence only partly produced (cancelled, timed out, source changed while running, changed files with no verifier, or nothing applicable).
* PASS — every required criterion has verified evidence and satisfies the policy and at least one required criterion actually ran.
Criteria recorded per run: `check:*`, `tests_executed`, `max_skipped`, `no_new_failures`, `static_new_findings`, `coverage*`, `baseline`,
`changed_files_verified`, `source_unchanged`, `not_cancelled`. Documentation-only changes have no evidence and are not `PASS`.

## Policy
`PolicyRules`: `default_mode`, `static.{enforce,max_new_findings}`, `tests.{min_tests_executed,allow_preexisting_failures,max_skipped}`,
`coverage.{min_line_percent,max_drop_percent}`, `baseline.required`, `require_unchanged_tree`. Strict schema (unknown keys rejected). Each
change is a new immutable **version**; only a workspace `owner` may change it (audited); every run stores the exact policy snapshot it
used, so later edits never rewrite past decisions.

## Staleness
A completed run is `stale` when HEAD or the working-tree fingerprint no longer matches what it verified. The API (`freshness`) and UI mark it
stale; the recorded outcome is kept and never presented as describing the current state.

## API (all under `/api/v1/repositories/{id}/verification`)
`GET /overview` · `GET|PUT /policy`, `GET /policy/history` · `POST /changes/preview` (plans, runs nothing) · `POST /runs` (202, `confirm`),
`GET /runs?status&outcome&kind&page`, `GET /runs/{id}`, `/checks`, `/regressions`, `/coverage`, `/gate`, `/events`, `POST /runs/{id}/cancel`,
`GET /runs/{id}/compare?baseline_id=`, `PUT /runs/{id}/link` (repair/refactor/review/debugging) · `POST /baselines` (202, `confirm`),
`GET /baselines[/{id}]`.

## Frontend
`/repositories/:id/verification` (overview, change source and plan preview, run, baselines, policy, history with outcome filter) and
`/verification/:runId` (live status by polling, checks, regressions, quality-gate criteria, baseline comparison, change set, events). The UI
renders the backend outcome verbatim (no decision shows "No decision (status)"), marks stale results, and never shows PASS otherwise.

## Security
Fixed argument arrays only; no user string reaches a shell; patch/manifest paths validated; cross-repository ids return 404; viewers get
403 on writes; tampered baselines rejected; output bounded and secrets redacted; no absolute paths in responses. **Not a sandbox**: tests run
as the NOVA user through the Phase 05 restricted subprocess, so only verify repositories you trust.

## Reproducible demonstration (executed on a disposable Git repository)
Repo: `app_pkg/util.py` (`clamp`), `app_pkg/pricing.py`, `tests/test_util.py` (3 tests), `tests/test_pricing.py` (1), all committed.
1. **Baseline** `POST /baselines {label:"clean", confirm:true}` → valid, 4 collected / 4 passed, integrity ok.
2. **Change** add `noop()` to `util.py` → `POST /changes/preview` lists `app_pkg/util.py (modified)`; checks syntax (tier 0), static analysis (tier 1), tests (tier 2: `tests/test_util.py`, "imports changed module app_pkg/util.py", confidence medium); nothing executed.
3. **Focused verify** → gate **PASS**; syntax/static/tests passed; 3 tests executed (not the pricing test); baseline selected "latest valid baseline (implicit)".
4. **Break** `clamp` (returns `high` for low values) → gate **FAIL**; `tests.test_util::test_clamp_low` is **new** ("The baseline covered this test and it did not fail there.", message `assert 10 == 0`); `no_new_failures` = fail.
5. **Compare** `GET /runs/{id}/compare?baseline_id=…` → same single new failure; `persisted:false`, note that the recorded decision is unchanged.
6. **Fix** (restore, plus an unrelated addition so there is a change) → gate **PASS**, 0 new failures; the earlier FAIL run now reports `freshness.stale = true`.
Also verified: reverting to the committed state gives an empty change set → **BLOCKED** ("nothing to verify"), never PASS.

## Limitations
Python/pytest only (the Phase 05 runner); static analysis only for Python files; other file types are "no verifier" → `INCOMPLETE`.
Focused selection is import association. Cancellation does not kill a running pytest. A fingerprint cannot detect changes made between
the check and the read of the same file. Runs use an in-process background task. No CI integration yet (Phase 11). Not verified on
Windows, PostgreSQL or in a real browser.
