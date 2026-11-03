<#
.SYNOPSIS  One-time setup for NOVA on Windows: backend venv + migrations + frontend packages.
.EXAMPLE   powershell -ExecutionPolicy Bypass -File scripts\setup.ps1
#>
$ErrorActionPreference = "Stop"
$root = Split-Path -Parent $PSScriptRoot

function Invoke-Checked([string]$what, [scriptblock]$cmd) {
    & $cmd
    if ($LASTEXITCODE -ne 0) { throw "$what failed (exit code $LASTEXITCODE)." }
}

# Pick a supported interpreter: 3.12 (what CI uses), then 3.13, then 3.11.
function Find-Python {
    if (Get-Command py -ErrorAction SilentlyContinue) {
        foreach ($v in "3.12", "3.13", "3.11") {
            # Windows PowerShell 5.1 turns a native command's stderr into an exception under "Stop"; a missing version must not abort setup.
            try { & py "-$v" -c "import sys" 2>$null } catch { $global:LASTEXITCODE = 1 }
            if ($LASTEXITCODE -eq 0) { return ,@("py", "-$v") }  # leading comma: return the array itself, not its unrolled items
        }
    }
    if (Get-Command python -ErrorAction SilentlyContinue) {
        & python -c "import sys; sys.exit(0 if (3, 11) <= sys.version_info[:2] <= (3, 13) else 1)"
        if ($LASTEXITCODE -eq 0) { return ,@("python") }
    }
    throw "Python 3.11, 3.12 or 3.13 is required (https://www.python.org/downloads/)."
}

Write-Host "== Backend =="
Set-Location "$root\backend"
if (-not (Test-Path ".venv")) {
    $py = Find-Python
    Write-Host "Creating .venv with: $($py -join ' ')"
    # Keep the arguments an ARRAY: splatting a plain string passes it character by character ("- 3 . 1 1"), which starts a REPL.
    $exe = $py[0]
    $pyArgs = @($py | Select-Object -Skip 1)
    Invoke-Checked "Creating the virtual environment" { & $exe @pyArgs -m venv .venv }
}
Invoke-Checked "Upgrading pip" { & ".\.venv\Scripts\python.exe" -m pip install --upgrade pip }
Invoke-Checked "Installing backend dependencies" { & ".\.venv\Scripts\python.exe" -m pip install -r requirements-dev.txt }
Invoke-Checked "Applying database migrations" { & ".\.venv\Scripts\python.exe" -m alembic upgrade head }

Write-Host "== Frontend =="
Set-Location "$root\frontend"
Invoke-Checked "Installing frontend dependencies" { npm ci }

Write-Host "Done. Start the app with scripts\dev-backend.ps1 and scripts\dev-frontend.ps1 (two terminals)."
