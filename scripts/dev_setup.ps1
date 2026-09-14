$ErrorActionPreference = "Stop"
if (-not (Test-Path .env)) { Copy-Item .env.example .env }
docker compose up -d
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -U pip
.\.venv\Scripts\pip.exe install -e ".[dev]"
.\.venv\Scripts\playwright.exe install chromium
.\.venv\Scripts\autodidact.exe init-db
.\.venv\Scripts\autodidact.exe bootstrap
Write-Host "Ready. Edit .env, then run: .\\.venv\\Scripts\\autodidact.exe run-once"
