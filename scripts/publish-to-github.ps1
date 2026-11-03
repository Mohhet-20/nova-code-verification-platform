<#
.SYNOPSIS  Push this repository to GitHub for the first time.
.DESCRIPTION
  1. On github.com create an EMPTY repository (no README, license or .gitignore) named "nova".
  2. Run:  powershell -ExecutionPolicy Bypass -File scripts\publish-to-github.ps1 -Owner <your-github-username>
  The script fills your username into the README's CI badge, commits that, runs the secret scan, adds the "origin" remote and pushes "main".
  Git will ask you to sign in to GitHub in the browser the first time. Nothing is pushed if any check fails.
.EXAMPLE   powershell -ExecutionPolicy Bypass -File scripts\publish-to-github.ps1 -Owner octocat
.EXAMPLE   powershell -ExecutionPolicy Bypass -File scripts\publish-to-github.ps1 -Owner octocat -RemoteUrl git@github.com:octocat/nova.git
#>
param(
    [Parameter(Mandatory = $true)][ValidatePattern('^[A-Za-z0-9](?:[A-Za-z0-9-]{0,38})$')][string]$Owner,
    [string]$Repo = "nova",
    # Optional: e.g. git@github.com:<owner>/nova.git to push over SSH. Defaults to HTTPS.
    [string]$RemoteUrl = ""
)
# Exit codes are checked explicitly (Invoke-Checked); git writes normal progress to stderr, which must not count as a failure.
$ErrorActionPreference = "Continue"
$root = Split-Path -Parent $PSScriptRoot
Set-Location $root

function Invoke-Checked([string]$what, [scriptblock]$cmd) {
    & $cmd
    if ($LASTEXITCODE -ne 0) { throw "$what failed (exit code $LASTEXITCODE). Nothing was pushed." }
}

if ((git status --porcelain) -ne $null) { throw "The working tree has uncommitted changes. Commit or stash them first." }
if ((git remote) -contains "origin") { throw "A remote named 'origin' already exists: $(git remote get-url origin)" }

$py = if (Test-Path "backend\.venv\Scripts\python.exe") { "backend\.venv\Scripts\python.exe" } else { "python" }
Invoke-Checked "Secret scan" { & $py scripts\scan_secrets.py }

$readme = Get-Content README.md -Raw -Encoding UTF8
if ($readme.Contains("GITHUB_OWNER")) {
    $updated = $readme.Replace("GITHUB_OWNER/nova", "$Owner/$Repo")
    [System.IO.File]::WriteAllText((Join-Path $root "README.md"), $updated, (New-Object System.Text.UTF8Encoding $false))
    Invoke-Checked "git add" { git add README.md }
    Invoke-Checked "git commit" { git commit -m "README: point the CI badge at $Owner/$Repo" }
}

if (-not $RemoteUrl) { $RemoteUrl = "https://github.com/$Owner/$Repo.git" }
Invoke-Checked "git remote add" { git remote add origin $RemoteUrl }
Invoke-Checked "git push" { git push -u origin main }
Write-Host ""
Write-Host "Pushed. Next:" -ForegroundColor Green
Write-Host "  * Watch the first CI run: https://github.com/$Owner/$Repo/actions"
Write-Host "  * Repository settings: add a description and topics, enable private vulnerability reporting (Security tab),"
Write-Host "    and upload docs/images/intelligence.png as the social preview image."
