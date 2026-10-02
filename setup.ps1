# One-time local setup for Windows.
# Run with: powershell -ExecutionPolicy Bypass -File setup.ps1
# Safe to re-run: it never overwrites an existing .env.
$ErrorActionPreference = 'Stop'
Set-Location -Path $PSScriptRoot

# Use the first interpreter that actually runs and is new enough. A "python" on
# PATH can be a Microsoft Store shortcut rather than Python.
$python = $null
foreach ($candidate in @('py', 'python')) {
    if (-not (Get-Command $candidate -ErrorAction SilentlyContinue)) { continue }
    try { & $candidate -c 'import sys; sys.exit(sys.version_info < (3, 11))' 2>$null } catch { continue }
    if ($LASTEXITCODE -eq 0) { $python = $candidate; break }
}
if (-not $python) {
    Write-Error 'Python 3.11 or newer is required but was not found on PATH.'
    exit 1
}

if (-not (Test-Path '.venv')) {
    Write-Host 'Creating virtual environment in .venv'
    & $python -m venv .venv
    if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }
}
$venvPython = Join-Path $PSScriptRoot '.venv\Scripts\python.exe'

Write-Host 'Installing dependencies'
& $venvPython -m pip install --quiet --upgrade pip
if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }
& $venvPython -m pip install --quiet -r requirements-dev.txt
if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }

if (Test-Path '.env') {
    Write-Host '.env already exists; left unchanged'
} else {
    Copy-Item '.env.example' '.env'
    Write-Host 'Created .env from .env.example'
}

function Get-EnvValue([string]$name) {
    $line = Get-Content '.env' | Where-Object { $_ -match "^$name=" } | Select-Object -Last 1
    if (-not $line) { return '' }
    return ($line -replace "^$name=", '').Trim().Trim('"', "'")
}
$binary = Get-EnvValue 'VORA_BROWSER_BINARY'
if (-not $binary) {
    Write-Warning 'VORA_BROWSER_BINARY is empty in .env. Set it to a Chrome or Chromium executable (any works).'
} elseif (-not (Test-Path -LiteralPath $binary -PathType Leaf)) {
    Write-Warning "VORA_BROWSER_BINARY does not point to a file: $binary"
}
if (-not (Get-EnvValue 'NVIDIA_API_KEY') -and -not (Get-EnvValue 'GEMINI_API_KEY')) {
    Write-Host 'NOTE: no LLM API key set; the deterministic planner will be used.'
}
if (-not (Get-EnvValue 'VORA_SEARCH_API_KEY')) {
    Write-Host 'NOTE: no search API key set; searches run in the browser, which search engines often challenge.'
    Write-Host '      For dependable results set VORA_SEARCH_API=brave (or google) and VORA_SEARCH_API_KEY in .env.'
}

Write-Host @'

Setup complete. Next steps:
  1. Edit .env and set VORA_BROWSER_BINARY (and optionally an LLM API key)
  2. .\.venv\Scripts\Activate.ps1
  3. python app.py            the API is at http://127.0.0.1:8000 (docs at /docs)
     The web app is a separate project (VORA/Frontend); see README, "Web app".
Tests:
  python -m unittest discover -s tests
'@
