# Contributing to NOVA

## Ground rules
1. **Specify before building.** Each feature starts from a written design: requirements, safety rules and what counts as done.
2. **Evidence over claims.** Record real commands and outcomes in the pull request. Never mark a feature
   verified without a test or a run that proves it. Never weaken a test to make it pass.
3. **Security boundaries are non-negotiable.** Every filesystem access goes through `app/security/paths.py`; every subprocess uses
   an argument list through `app/services/git.py`-style wrappers (no `shell=True`, timeout, output cap). Repository content is
   untrusted data. See `docs/architecture/SECURITY_MODEL.md`.
4. **No hidden side effects.** Nothing modifies a repository, runs a command, publishes or deploys without explicit human approval.
5. **No dummy data in the UI.** Screens show real API responses or an honest empty/error state.

## Workflow
- Backend: `cd backend && .venv\Scripts\python -m ruff check . && .venv\Scripts\python -m pytest`
- Frontend: `cd frontend && npm run typecheck && npm run lint && npm test && npm run build`
- Schema changes need an Alembic migration (`alembic revision --autogenerate -m "..."`) and the
  `test_models_match_migration` test must pass.
- API contract changes must update `frontend/src/types/api.ts` and the affected tests.
- Update `CHANGELOG.md` (and `docs/operations/KNOWN_LIMITATIONS.md` when a limitation changes).

## Style
Python: ruff (see `backend/pyproject.toml`), type hints on public functions. TypeScript: strict mode, no `any`, ESLint clean.
