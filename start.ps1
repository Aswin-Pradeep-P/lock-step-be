# Quick start: containers, deps, migrations, demo data, API. Ctrl+C stops it.
# Run tests separately with `uv run pytest`. Pass -Reset to rebuild the demo data.
param([switch]$Reset)
$ErrorActionPreference = "Stop"
Set-Location $PSScriptRoot

$uv = (Get-Command uv -ErrorAction SilentlyContinue).Source
if (-not $uv) {
    # ponytail: uv installed via pip lands in a Scripts dir that isn't on PATH
    $uv = (Get-ChildItem "$env:LOCALAPPDATA\Python\*\Scripts\uv.exe" -ErrorAction SilentlyContinue |
           Select-Object -First 1).FullName
}
if (-not $uv) { throw "uv not found. Install it: pip install uv" }

docker compose up -d
if (-not (Test-Path .env)) { Copy-Item .env.example .env }
& $uv sync --group dev
& $uv run alembic upgrade head

# Seed only when the database is empty, unless -Reset was passed.
$env:PYTHONIOENCODING = "utf-8"
if ($Reset) { & $uv run python -m lockstep.seed --reset }
else { & $uv run python -m lockstep.seed }

& $uv run uvicorn lockstep.main:app --reload --port 8010
