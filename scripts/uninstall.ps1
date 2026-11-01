param(
    [string]$BinDir,
    [switch]$PurgeState
)

$ErrorActionPreference = 'Stop'
$repo = Split-Path -Parent $PSScriptRoot
$entry = Join-Path $repo 'scripts\threadwatch.py'
if ([string]::IsNullOrWhiteSpace($BinDir)) {
    if (-not [string]::IsNullOrWhiteSpace($env:THREADWATCH_BIN)) {
        $BinDir = $env:THREADWATCH_BIN
    } else {
        $BinDir = Join-Path $HOME '.local\bin'
    }
}
$launcher = Join-Path $BinDir 'threadwatch.cmd'
if (Test-Path -LiteralPath $entry) {
    & py -3 $entry stop | Out-Null
}
if (Test-Path -LiteralPath $launcher) {
    Remove-Item -LiteralPath $launcher -Force
    Write-Output "Removed launcher: $launcher"
}
if ($PurgeState) {
    $data = Join-Path $env:LOCALAPPDATA 'CodexAutoSupervisor'
    if (Test-Path -LiteralPath $data) {
        Remove-Item -LiteralPath $data -Recurse -Force
        Write-Output "Removed state: $data"
    }
}
Write-Output 'No Codex configuration was changed.'
