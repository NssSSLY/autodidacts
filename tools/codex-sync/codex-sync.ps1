$ErrorActionPreference = 'Stop'
$utf8 = New-Object System.Text.UTF8Encoding($false)
[Console]::InputEncoding = $utf8
[Console]::OutputEncoding = $utf8
$OutputEncoding = $utf8
$env:PYTHONIOENCODING = 'utf-8'

$sourceRoot = Join-Path $PSScriptRoot 'src'
if (-not (Test-Path -LiteralPath (Join-Path $sourceRoot 'codex_sync\cli.py'))) {
    throw "Missing Python CLI package under $sourceRoot. Re-run the installer."
}
if (-not (Get-Command python -ErrorAction SilentlyContinue)) {
    throw 'Python 3.10 or newer is required for codex-sync V2.'
}

if ($env:PYTHONPATH) {
    $env:PYTHONPATH = "$sourceRoot;$env:PYTHONPATH"
} else {
    $env:PYTHONPATH = $sourceRoot
}
& python -m codex_sync @args
exit $LASTEXITCODE
