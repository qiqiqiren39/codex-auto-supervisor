param(
    [Parameter(Mandatory = $true)]
    [string]$Version,
    [string]$OutputDir = 'dist'
)

$ErrorActionPreference = 'Stop'
$repo = [IO.Path]::GetFullPath((Split-Path -Parent $PSScriptRoot))
if ($Version -notmatch '^v[0-9]+\.[0-9]+\.[0-9]+$') {
    throw "Version must look like v1.0.0: $Version"
}
$output = [IO.Path]::GetFullPath((Join-Path $repo $OutputDir))
$sep = [IO.Path]::DirectorySeparatorChar
if (-not $output.StartsWith($repo + $sep, [StringComparison]::OrdinalIgnoreCase)) {
    throw "Output directory must stay inside the repository: $output"
}
$stageRoot = Join-Path $output 'stage'
$bundleRoot = Join-Path $stageRoot 'codex-auto-supervisor'
$relativeFiles = @(
    'SKILL.md',
    'README.md',
    'README.zh-CN.md',
    'scripts\threadwatch.py',
    'scripts\install.ps1',
    'scripts\uninstall.ps1',
    'threadwatch.cmd'
)

if (Test-Path -LiteralPath $output) {
    Remove-Item -LiteralPath $output -Recurse -Force
}
New-Item -ItemType Directory -Path $bundleRoot -Force | Out-Null
foreach ($relative in $relativeFiles) {
    $source = Join-Path $repo $relative
    if (-not (Test-Path -LiteralPath $source)) {
        throw "Missing release input: $source"
    }
    $destination = Join-Path $bundleRoot $relative
    $parent = Split-Path -Parent $destination
    New-Item -ItemType Directory -Path $parent -Force | Out-Null
    Copy-Item -LiteralPath $source -Destination $destination -Force
}

$actualFiles = Get-ChildItem -LiteralPath $bundleRoot -Recurse -File |
    ForEach-Object { $_.FullName.Substring($bundleRoot.Length).TrimStart('\', '/').Replace('/', '\') } |
    Sort-Object
$expectedFiles = $relativeFiles | ForEach-Object { $_.Replace('/', '\') } | Sort-Object
$unexpected = Compare-Object -ReferenceObject $expectedFiles -DifferenceObject $actualFiles
if ($unexpected) {
    throw "Release bundle file set does not match the allowlist: $($unexpected | Out-String)"
}

$archiveName = "codex-auto-supervisor-$Version.zip"
$archivePath = Join-Path $output $archiveName
Compress-Archive -LiteralPath $bundleRoot -DestinationPath $archivePath -CompressionLevel Optimal

Add-Type -AssemblyName System.IO.Compression.FileSystem
$archive = [IO.Compression.ZipFile]::OpenRead($archivePath)
try {
    $zipFiles = $archive.Entries |
        Where-Object { -not [string]::IsNullOrEmpty($_.Name) } |
        ForEach-Object { $_.FullName.Replace('/', '\') } |
        Sort-Object
} finally {
    $archive.Dispose()
}
$expectedZipFiles = $relativeFiles | ForEach-Object { 'codex-auto-supervisor\' + $_.Replace('/', '\') } | Sort-Object
$zipUnexpected = Compare-Object -ReferenceObject $expectedZipFiles -DifferenceObject $zipFiles
if ($zipUnexpected) {
    throw "ZIP file set does not match the allowlist: $($zipUnexpected | Out-String)"
}

$hash = (Get-FileHash -Algorithm SHA256 -LiteralPath $archivePath).Hash.ToLowerInvariant()
$sumPath = Join-Path $output 'SHA256SUMS.txt'
[IO.File]::WriteAllText($sumPath, "$hash  $archiveName" + [Environment]::NewLine, (New-Object Text.UTF8Encoding($false)))
Write-Output "Release archive: $archivePath"
Write-Output "Checksums: $sumPath"
Write-Output "SHA256: $hash"
