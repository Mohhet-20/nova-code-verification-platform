## What and why

<!-- One or two sentences: the problem and the change. -->

## How it was verified

- [ ] Backend: `ruff check .` and `pytest` (from `backend/`)
- [ ] Frontend: `npm run typecheck`, `npm run lint`, `npm test`, `npm run build` (from `frontend/`)
- [ ] New migration? `python -m app.release classify` reviewed; downgrade never destroys data
- [ ] User-visible behaviour checked in the running app, or explain why not

## Risk

<!-- What could break, and how it would be noticed. Note any security-relevant change. -->
