# Starts the NOVA UI on http://localhost:5173 (proxies /api to the backend on port 8000).
$ErrorActionPreference = "Stop"
Set-Location "$(Split-Path -Parent $PSScriptRoot)\frontend"
npm run dev
