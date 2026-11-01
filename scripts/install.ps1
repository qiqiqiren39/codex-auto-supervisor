param(
    [string]$BinDir
)

$ErrorActionPreference = 'Stop'
$repo = Split-Path -Parent $PSScriptRoot
$entry = Join-Path $repo 'scripts\threadwatch.py'
if (-not (Test-Path -LiteralPath $entry)) {
    throw "Missing entry point: $entry"
}
if ([string]::IsNullOrWhiteSpace($BinDir)) {
    if (-not [string]::IsNullOrWhiteSpace($env:THREADWATCH_BIN)) {
        $BinDir = $env:THREADWATCH_BIN
    } else {
        $BinDir = Join-Path $HOME '.local\bin'
    }
}
New-Item -ItemType Directory -Path $BinDir -Force | Out-Null
$launcher = Join-Path $BinDir 'threadwatch.cmd'
$lines = @(
    '@echo off',
    'setlocal',
    ('py -3 "{0}" %*' -f $entry),
    'exit /b %errorlevel%'
)
[IO.File]::WriteAllLines($launcher, $lines, [Text.Encoding]::ASCII)
Write-Output "Installed launcher: $launcher"
Write-Output 'No Codex configuration or login startup entry was modified.'
