# Starts the NOVA API on http://127.0.0.1:8000 (docs at /docs). Binds to localhost only.
$ErrorActionPreference = "Stop"
Set-Location "$(Split-Path -Parent $PSScriptRoot)\backend"
& ".\.venv\Scripts\python.exe" -m alembic upgrade head
& ".\.venv\Scripts\python.exe" -m uvicorn app.main:app --host 127.0.0.1 --port 8000 --reload
