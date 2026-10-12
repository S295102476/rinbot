[CmdletBinding()]
param()

Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'
if (Test-Path -LiteralPath (Join-Path $PSScriptRoot 'INCOMPLETE.txt')) { throw 'Package is incomplete.' }
$checksumPath = Join-Path $PSScriptRoot 'SHA256SUMS.csv'
$entries = @(Import-Csv -LiteralPath $checksumPath)
if (-not $entries.Count) { throw 'Checksum list is empty.' }
$prefix = [IO.Path]::GetFullPath($PSScriptRoot).TrimEnd('\') + '\'
$failed = @()
$index = 0
foreach ($entry in $entries) {
    $index++
    if ($index % 100 -eq 0) { Write-Progress -Activity 'Checking USB transfer' -Status "$index / $($entries.Count)" -PercentComplete (100 * $index / $entries.Count) }
    $path = [IO.Path]::GetFullPath((Join-Path $PSScriptRoot $entry.Path))
    if (-not $path.StartsWith($prefix, [StringComparison]::OrdinalIgnoreCase)) { throw 'Invalid path in checksum list.' }
    if (-not (Test-Path -LiteralPath $path -PathType Leaf)) { $failed += $entry.Path; continue }
    if ((Get-FileHash -LiteralPath $path -Algorithm SHA256).Hash -ne $entry.SHA256) { $failed += $entry.Path }
}
Write-Progress -Activity 'Checking USB transfer' -Completed
if ($failed.Count) {
    $failed | ForEach-Object { Write-Host "Missing or modified: $_" }
    throw "$($failed.Count) files did not pass verification. Files edited after packaging also appear here."
}
Write-Host "Verified $($entries.Count) files. USB transfer is complete."
