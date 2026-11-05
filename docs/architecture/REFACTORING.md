# Refactoring

AI-assisted-style refactoring that is **deterministic**: every transformation is computed from the index and the source, planned
as a multi-file diff, validated in an isolated copy, approved by a human, applied atomically and verified
runner. NOVA never executes proposed code, never commits or pushes, and never falls back to textual search-and-replace.

## Architecture
`app/services/refactor/`: `registry` (15 catalog entries; 8 usable, 7 reported `unsupported`), `pyscope` + `rename_py` (Python scope
analysis), `rename_js` (conservative tree-sitter JS/TS rename), `duplicates`, `extract` (Python extract-function), `simplify`
(proposal-only), `cycles` (import-cycle suggestions), `impact`, `service` (lifecycle), `verify`. Reused: the repair patch engine
(`parse`, `build`, `validate_isolated`, `write_results`, `restore`), runner, static analysis, graph,
Test-state helper. Persistence: `refactor_transformations`, `refactor_verifications` (migration `0009`).

Support levels: `applicable` (a diff NOVA will apply after approval), `proposal_only` (suggestions, never applied automatically),
`unsupported` (reported honestly, no plan).

## Symbol resolution
Python: a scope tree resolves the definition, imports (`import a.b`, `from a import b as c`, re-exports in `__init__`), module
attribute access, method definitions and local variables. Names that merely coincide (a local `add_item = 3`, a string, a comment)
are not touched. Unresolvable attribute calls (`obj.add_item()` where the type is unknown) are listed as
`unresolved_attribute_references` and must be **acknowledged** at approval. `dynamic_reference` (strings/`getattr`) and `public_api`
(`__all__`, re-export) are also acknowledgement codes. JS/TS: ES imports, namespace imports and re-exports are followed; it blocks on
shadowing, shorthand properties, CommonJS dependence, name conflicts, invalid names and unparsed dependents. No type-aware JS
resolution exists.

## Duplicates
Token shingles (4-grams) with a Jaccard score through an inverted index (very common shingles capped). Groups are `exact`,
`renamed` or `structural`, with similarity, excerpts, shared and differing elements, strategies, and `auto_merge: false`. Generated
code, tests, vendored directories and configured exclusions are filtered. A merge is never applied automatically.

## Extract function (Python only)
Selected statements become a new function; parameters, return values and dead assignments come from data-flow analysis. Blocked
for return/yield/break/continue/await inside the region, `global`/`nonlocal`, `del`, dynamic scope access (`locals()`, `eval`),
`super()`, conditional assignment, closure capture of a rebound variable, module-level regions, nested/static contexts, name
conflicts and generated code that does not parse.

## Lifecycle and validation
`generated → validated → awaiting_approval → approved → applied` (also `blocked`, `rejected`, `closed`, `failed`). Validation builds
the patch in a temporary copy: source unchanged, paths inside the repository, patch applies, syntax, no new undefined names,
import-edge set, static-analysis diff. Formatter/linter/type-checker results are reported `unavailable` (not run). Approval
requires acknowledging every warning code in `acknowledgements_required`. Apply requires `confirm`, holds a per-repository lock,
atomically claims `approved → applying`, re-checks hashes and writes atomically; originals are retained and rollback guidance
(reverse diff, hashes) is returned. A stale index with added/removed files blocks; content-only staleness warns.
CRLF files and files without a final newline are blocked. The index is refreshed after apply.

## Impact and verification
Impact lists direct and transitive dependents, import cycles (with proof), public entry points and affected tests. Verification
runs the selected tests runner, compared with an explicit baseline (taken before approval/apply via
`POST …/baseline`) or the latest finished run before apply, labelled implicit. Criteria: run completed, tests executed, no failures,
no skips, no new failures, applied state intact, no new static findings; `behavior_equivalence` is always `unavailable`.
Outcomes PASSED / FAILED / INCOMPLETE / BLOCKED. **PASSED means the selected tests passed — not that behavior is proven preserved.**

## API
Base `/api/v1/repositories/{id}/refactors`: `GET /types`; `POST /rename`, `/extract-function`, `/duplicates`, `/simplify`,
`/cycle-suggestions`; `GET ""`, `/{rid}`, `/{rid}/plan`, `/{rid}/diff`, `/{rid}/duplicates`, `/{rid}/rollback`, `/{rid}/audit`;
`POST /{rid}/validate|submit|approve|reject|close|apply|baseline|verify/preview|verifications`; `GET /{rid}/verifications[/{vid}]`.
Writes need a non-viewer role.

## Frontend
`/repositories/:id/refactors` (catalog, stale-index notice, request tabs, symbol search, history filters) and
`/refactors/:refactorId` (plan, findings, impact, multi-file diff, duplicates, validation checks, decisions with confirmation and
acknowledgements, recovery, verification with polling, audit).

## Security
Repository-relative allow-listed paths, no symlinks, `.git` excluded, size/file-count limits, sha256 integrity on stored patches,
roles enforced, no absolute paths in responses, output scrubbed, proposed code is parsed but never executed.

## Worked example (executed on a disposable repository in a pytest temp dir; Git repo with one commit)
Files: `pkg/__init__.py`, `pkg/cart.py` (defines `add_item`, class `Cart` with method `add_item`, a function with a local named
`add_item`, string `"add_item"`), `pkg/use.py`, `tests/test_cart.py`.

1. `POST /refactors/rename {type_id: python.rename_symbol, rel_path: pkg/cart.py, name: add_item, new_name: append_item}` → `201`,
   status `generated`, support `applicable`, files `pkg/__init__.py`, `pkg/cart.py`, `pkg/use.py`, `tests/test_cart.py`.
   `acknowledgements_required`: `dynamic_reference`, `public_api`, `unresolved_attribute_references`.
2. `validate` → `validated`; checks `source_unchanged`, `paths_inside_repository`, `patch_applies`, `syntax`,
   `no_new_undefined_names`, `dependency_edges`, `static_analysis` all `pass`; working tree untouched.
3. `submit` → `awaiting_approval`; `approve` with the three acknowledgements → `approved`.
4. `baseline` after approval → **409** (a baseline must be taken before approval); apply then uses the labelled implicit baseline.
5. `apply {confirm:true}` → `200 applied`. `git status` shows ` M` for the four files (not staged, not committed).
6. Result: `def append_item`, `Cart.add_item` method **kept** (a different symbol), `cart.add_item` call in `Cart` now calls
   `append_item`; the local `add_item = 3`, the comment and the `"add_item"` string are **unchanged**; in `use.py`
   `from pkg import append_item as put`, `pkg.cart.append_item(3)`, `cart.append_item(4)`, `append_item(5)`, while `c.add_item(1)`
   (method on an object of unknown type) stays and is the reason the unresolved-reference acknowledgement was required.
7. `GET …/rollback` → originals retained plus per-file before/after hashes and the reverse diff.

## Limitations
No JS scope resolution beyond imports; no type inference for method renames; extract-function is Python-only; CRLF and
no-final-newline files unsupported; formatter/lint/type-check not run; verification is restricted-subprocess, not a
sandbox; behavior equivalence is not proven; not tested on Windows or in a real browser.
