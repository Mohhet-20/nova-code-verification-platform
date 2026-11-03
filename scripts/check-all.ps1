# Runs every automated check (secret scan, API contract, backend lint + tests, frontend typecheck + lint + tests + build).
# Stops at the first failing step and exits non-zero (Windows PowerShell does not stop on a failing native command by itself).
$ErrorActionPreference = "Stop"
$root = Split-Path -Parent $PSScriptRoot

function Invoke-Checked([string]$what, [scriptblock]$cmd) {
    Write-Host "== $what"
    & $cmd
    if ($LASTEXITCODE -ne 0) { Write-Host "FAILED: $what (exit code $LASTEXITCODE)" -ForegroundColor Red; exit $LASTEXITCODE }
}

Set-Location "$root\backend"
Invoke-Checked "Secret scan" { & ".\.venv\Scripts\python.exe" "$root\scripts\scan_secrets.py" }
Invoke-Checked "Backend lint" { & ".\.venv\Scripts\python.exe" -m ruff check . }
Invoke-Checked "API contract" { & ".\.venv\Scripts\python.exe" -m app.contracts }
Invoke-Checked "Backend tests" { & ".\.venv\Scripts\python.exe" -m pytest }
Set-Location "$root\frontend"
Invoke-Checked "Frontend type check" { npm run typecheck }
Invoke-Checked "Frontend lint" { npm run lint }
Invoke-Checked "Frontend tests" { npm test }
Invoke-Checked "Frontend build" { npm run build }
Write-Host "All checks passed." -ForegroundColor Green
