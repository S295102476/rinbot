[CmdletBinding()]
param(
    [string]$Destination,
    [string]$ProjectRoot,
    [string]$PublicProjectRoot
)

Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'

if (-not $ProjectRoot) { $ProjectRoot = Split-Path (Split-Path (Split-Path $PSScriptRoot -Parent) -Parent) -Parent }
if (-not $Destination) { $Destination = Join-Path (Split-Path $ProjectRoot -Parent) 'qqbot-migration' }

function Copy-Tree {
    param([string]$Source, [string]$Target, [string[]]$ExcludeDirectories = @(), [string[]]$ExcludeFiles = @())
    New-Item -ItemType Directory -Path $Target -Force | Out-Null
    $arguments = @($Source, $Target, '/E', '/COPY:DAT', '/DCOPY:DAT', '/XJ', '/MT:8', '/R:2', '/W:1', '/NFL', '/NDL', '/NJH', '/NJS', '/NP')
    if ($ExcludeDirectories.Count) { $arguments += '/XD'; $arguments += $ExcludeDirectories }
    if ($ExcludeFiles.Count) { $arguments += '/XF'; $arguments += $ExcludeFiles }
    & robocopy.exe @arguments | Out-Null
    if ($LASTEXITCODE -ge 8) { throw "Copy failed (robocopy $LASTEXITCODE): $Source" }
}

function Get-ToolSource {
    param([string]$Name)
    $command = Get-Command $Name -CommandType Application -ErrorAction Stop | Select-Object -First 1
    return $command.Source
}

function Write-Utf8 {
    param([string]$Path, [string]$Content)
    [System.IO.File]::WriteAllText($Path, $Content, (New-Object System.Text.UTF8Encoding $false))
}

function Get-DatabaseSnapshot {
    param([string]$Root)
    $snapshot = @{}
    Get-ChildItem -LiteralPath $Root -Recurse -Force -File | Where-Object {
        $_.Name -match '\.(db|sqlite|sqlite3)(-wal|-shm)?$' -and $_.FullName -notmatch '\\(\.venv|venv|node_modules)\\'
    } | ForEach-Object { $snapshot[$_.FullName] = (Get-FileHash -LiteralPath $_.FullName -Algorithm SHA256).Hash }
    return $snapshot
}

$project = (Resolve-Path -LiteralPath $ProjectRoot).ProviderPath.TrimEnd('\')
if ($PublicProjectRoot) {
    throw 'RinBot now uses one workspace. Run without -PublicProjectRoot; GitHub and SFTP use ProjectRoot.'
}
$coreRoot = Join-Path $project 'gsuid_core_app'
$corePresent = Test-Path -LiteralPath (Join-Path $coreRoot 'gsuid_core') -PathType Container
$destinationRoot = [System.IO.Path]::GetFullPath($Destination).TrimEnd('\')
if ($destinationRoot.Equals($project, [StringComparison]::OrdinalIgnoreCase) -or
    $destinationRoot.StartsWith($project + '\', [StringComparison]::OrdinalIgnoreCase)) {
    throw 'Destination must be outside the project, to prevent recursive copying.'
}
if (-not (Test-Path -LiteralPath (Join-Path $project 'qq-bot-py\bot.py'))) {
    throw 'ProjectRoot must contain qq-bot-py\bot.py.'
}
if (-not [Environment]::Is64BitOperatingSystem) { throw 'This package requires Windows x64.' }

# Active databases and Git writes must be stopped before making a snapshot.
$running = @(Get-CimInstance Win32_Process | Where-Object {
    $_.Name -match '^(python|pythonw|core)(\.exe)?$' -and
    ($_.Name -match '^core' -or ($_.CommandLine -and
        ($_.CommandLine.IndexOf($project, [StringComparison]::OrdinalIgnoreCase) -ge 0 -or $_.CommandLine -match 'bot\.py|gsuid_core')))
})
if ($running.Count) {
    throw ('Stop local project processes before packing. Process IDs: ' + (($running | ForEach-Object { $_.ProcessId }) -join ', '))
}

$codeCommand = Get-ToolSource 'code'
$codeRoot = Split-Path (Split-Path $codeCommand -Parent) -Parent
$gitCommand = Get-ToolSource 'git'
if (Test-Path -LiteralPath (Join-Path $project '.git\index.lock')) {
    throw 'The main Git repository is busy (index.lock exists). Finish its Git operation before packing.'
}
$gitTop = (& $gitCommand -C $project rev-parse --show-toplevel | Out-String).Trim()
if ($LASTEXITCODE -ne 0 -or -not $gitTop -or
    -not ([IO.Path]::GetFullPath($gitTop).TrimEnd('\').Equals($project, [StringComparison]::OrdinalIgnoreCase))) {
    throw 'ProjectRoot must be the root of the unified RinBot Git repository.'
}
$rootOrigin = (& $gitCommand -C $project remote get-url --push origin | Out-String).Trim()
if ($LASTEXITCODE -ne 0 -or $rootOrigin -notmatch '^(https://github\.com/[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+(?:\.git)?|git@github\.com:[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+(?:\.git)?)$') {
    throw 'The main workspace needs its credential-free GitHub origin. Remove the old private-snapshot push restriction before packing.'
}
$privateTracked = @(& $gitCommand -C $project ls-files -- '.vscode/sftp.json' 'qq-bot-py/.vscode/sftp.json' 'qq-bot-py/config.yaml' 'qq-bot-py/.env' 'gsuid_core_app')
if ($LASTEXITCODE -ne 0 -or $privateTracked.Count) {
    throw 'Private configuration or Core is tracked by the main repository. Review .gitignore and remove these files from the Git index before packing.'
}
$gitRoot = Split-Path (Split-Path $gitCommand -Parent) -Parent
$nodeCommand = Get-ToolSource 'node'
$nodeRoot = Split-Path $nodeCommand -Parent
foreach ($entry in @(
    @{ Path = (Join-Path $codeRoot 'Code.exe'); Name = 'VS Code' },
    @{ Path = (Join-Path $gitRoot 'cmd\git.exe'); Name = 'Git for Windows' },
    @{ Path = (Join-Path $nodeRoot 'npm.cmd'); Name = 'Node.js with npm' }
)) {
    if (-not (Test-Path -LiteralPath $entry.Path)) { throw "Unsupported installation layout: $($entry.Name)" }
}

$extensionSource = Join-Path $env:USERPROFILE '.vscode\extensions'
$userSource = Join-Path $env:APPDATA 'Code\User'
$extensionVersions = @(& $codeCommand --list-extensions --show-versions)
if ($LASTEXITCODE -ne 0) { throw 'Cannot list active VS Code extensions.' }
if (-not ($extensionVersions -match '^natizyskunk\.sftp@')) { throw 'The current SFTP extension is not installed.' }
$selectedExtensions = @()
$allExtensionDirectories = @(Get-ChildItem -LiteralPath $extensionSource -Directory)
foreach ($extension in $extensionVersions) {
    if ($extension -notmatch '^([^@]+)@(.+)$') { continue }
    $extensionId = $Matches[1]
    $extensionVersion = $Matches[2]
    $found = @($allExtensionDirectories | Where-Object {
        $packagePath = Join-Path $_.FullName 'package.json'
        if (-not (Test-Path -LiteralPath $packagePath)) { return $false }
        $package = Get-Content -LiteralPath $packagePath -Raw -Encoding UTF8 | ConvertFrom-Json
        return "$($package.publisher).$($package.name)" -eq $extensionId -and $package.version -eq $extensionVersion
    })
    if (-not $found.Count) { throw "Cannot locate active extension: $extension" }
    $selectedExtensions += $found[0]
}

$created = [DateTimeOffset]::UtcNow.ToOffset([TimeSpan]::FromHours(8))
$bundle = Join-Path $destinationRoot ('QQBot-' + $created.ToString('yyyyMMdd-HHmmss'))
if (Test-Path -LiteralPath $bundle) { throw "Package already exists: $bundle" }
New-Item -ItemType Directory -Path $bundle -Force | Out-Null
Write-Utf8 (Join-Path $bundle 'INCOMPLETE.txt') 'Packaging is unfinished. Do not use this directory yet.'
Write-Host "Package: $bundle"
Write-Host 'Do not edit project files or write to local databases until packaging finishes.'
$databaseSnapshot = Get-DatabaseSnapshot $project

Write-Host '[1/6] Copying the single workspace, Git history, private configuration and local data...'
$excludedDirectories = @('.venv', 'venv', 'node_modules', '__pycache__', '.pytest_cache', '.mypy_cache', '.ruff_cache', '.pdm-build', '.agents', 'task-exports') + @(
    (Join-Path $project 'logs'), (Join-Path $project 'qq-bot-py\logs'), (Join-Path $coreRoot 'logs')
)
$excludedFiles = @('*.pyc', '*.pyo', '*.log', 'Thumbs.db', '.DS_Store',
    (Join-Path $project '.git\index.lock'), (Join-Path $coreRoot '.git\index.lock'))
# Updates and task exports in the workspace root are transfer artifacts, not source.
$excludedFiles += @(Get-ChildItem -LiteralPath $project -File -Filter 'rinbot-*.zip*' | ForEach-Object { $_.FullName })
Copy-Tree $project (Join-Path $bundle 'workspace') $excludedDirectories $excludedFiles

Write-Host '[2/6] Copying editor, Git and Node.js...'
Copy-Tree $codeRoot (Join-Path $bundle 'apps\vscode') @('data') @('unins*', 'debug.log', '*.log')
Copy-Tree $gitRoot (Join-Path $bundle 'apps\git') @() @('unins*', '*.log')
Copy-Tree $nodeRoot (Join-Path $bundle 'apps\node') @() @('*.log')

Write-Host '[3/6] Copying active extensions and editor preferences...'
foreach ($extensionDirectory in $selectedExtensions) {
    Copy-Tree $extensionDirectory.FullName (Join-Path $bundle ('extensions\' + $extensionDirectory.Name))
}
$userTarget = Join-Path $bundle 'vscode-user\User'
New-Item -ItemType Directory -Path $userTarget -Force | Out-Null
foreach ($name in @('settings.json', 'keybindings.json')) {
    $sourceFile = Join-Path $userSource $name
    if (Test-Path -LiteralPath $sourceFile) { Copy-Item -LiteralPath $sourceFile -Destination (Join-Path $userTarget $name) }
}
$snippetSource = Join-Path $userSource 'snippets'
if (Test-Path -LiteralPath $snippetSource) { Copy-Tree $snippetSource (Join-Path $userTarget 'snippets') }

Write-Host '[4/6] Preparing launchers and environment inventory...'
foreach ($name in @('Open-QQBot.cmd', 'Restore-Dev.cmd', 'Restore-Dev.ps1', 'README.md', 'Verify-Package.ps1', 'Verify-Package.cmd')) {
    Copy-Item -LiteralPath (Join-Path $PSScriptRoot ('templates\' + $name)) -Destination (Join-Path $bundle $name)
}
$sourcePython = Join-Path $project 'qq-bot-py\.venv\Scripts\python.exe'
$pythonVersion = $null
if (Test-Path -LiteralPath $sourcePython) {
    $pythonVersion = (& $sourcePython --version 2>&1 | Out-String).Trim()
    if ($LASTEXITCODE -ne 0) { throw 'The source Python environment cannot run.' }
    $frozen = @(& $sourcePython -m pip freeze --exclude-editable)
    if ($LASTEXITCODE -ne 0) { throw 'Cannot capture Python dependency versions.' }
    # Machine-local wheel paths cannot be installed on another computer.
    $localRequirements = @($frozen | Where-Object { $_ -match '^\S+\s+@\s+file:' -or $_ -match '^[A-Za-z]:[\\/]' })
    if ($localRequirements.Count) {
        Write-Warning 'Local Python package paths were omitted from the lock file; review python-local-packages.txt.'
        Write-Utf8 (Join-Path $bundle 'python-local-packages.txt') ($localRequirements -join "`r`n")
    }
    $portableRequirements = @($frozen | Where-Object { $_ -notin $localRequirements })
    Write-Utf8 (Join-Path $bundle 'python-requirements.lock.txt') (($portableRequirements -join "`r`n") + "`r`n")
}
$gitUser = @{}
foreach ($field in @('name', 'email')) {
    $value = @(& $gitCommand -C $project config --get "user.$field")
    if ($LASTEXITCODE -eq 0 -and $value.Count) { $gitUser[$field] = $value[0] }
}
$rootCommit = (& $gitCommand -C $project rev-parse HEAD | Out-String).Trim()
if ($LASTEXITCODE -ne 0) { throw 'Cannot read project Git history.' }
$coreCommit = $null
$coreGitPresent = $corePresent -and (Test-Path -LiteralPath (Join-Path $coreRoot '.git'))
if ($coreGitPresent) {
    $coreCommit = (& $gitCommand -C $coreRoot rev-parse HEAD | Out-String).Trim()
    if ($LASTEXITCODE -ne 0) { throw 'Cannot read the private GSUID Core Git history.' }
}
# Author identity belongs to the copied repository; authentication stays on the PC.
$repositories = @('workspace')
if ($coreGitPresent) { $repositories += 'workspace\gsuid_core_app' }
foreach ($repository in $repositories) {
    foreach ($field in $gitUser.Keys) {
        & $gitCommand -C (Join-Path $bundle $repository) config --local "user.$field" $gitUser[$field]
        if ($LASTEXITCODE -ne 0) { throw "Cannot restore Git author identity: $repository" }
    }
}
# The main workspace keeps its real GitHub remote. Core is an optional private
# snapshot outside the main repository's tracked files, so disable only its push.
if ($coreGitPresent) {
    $copiedCore = Join-Path $bundle 'workspace\gsuid_core_app'
    $coreRemotes = @(& $gitCommand -C $copiedCore remote)
    if ($LASTEXITCODE -ne 0) { throw 'Cannot inspect the copied Core remotes.' }
    foreach ($remote in $coreRemotes) {
        & $gitCommand -C $copiedCore config --local "remote.$remote.pushurl" 'disabled://private-core-snapshot'
        if ($LASTEXITCODE -ne 0) { throw 'Cannot protect the private Core snapshot from an accidental push.' }
    }
}
$manifest = [ordered]@{
    formatVersion = 3
    createdAt = $created.ToString('o')
    platform = 'windows-x64'
    scope = 'Development workstation and SFTP connection to the existing server'
    containsSecrets = $true
    rootCommit = $rootCommit
    rootOrigin = $rootOrigin
    gsuidCoreCommit = $coreCommit
    workspace = 'workspace'
    workspaceMode = 'single-source'
    rootGitPushDisabled = $false
    corePresent = [bool]$corePresent
    privateCoreGitPushDisabled = [bool]$coreGitPresent
    vscodeVersion = @(& $codeCommand --version)
    gitVersion = (& $gitCommand --version | Out-String).Trim()
    nodeVersion = (& $nodeCommand --version | Out-String).Trim()
    pythonVersion = $pythonVersion
    extensions = $extensionVersions
    excludedDirectories = $excludedDirectories
    excludedFiles = $excludedFiles
    userDataCopied = @('settings.json', 'keybindings.json', 'snippets')
    requiresFreshLogin = 'Extensions that use account sign-in; GitHub remote authentication'
    sshPrivateKeysCopied = $false
    gitCredentialsCopied = $false
}
Write-Utf8 (Join-Path $bundle 'manifest.json') ($manifest | ConvertTo-Json -Depth 5)

Write-Host '[5/6] Checking copied configuration and relocated tools...'
$databaseAfter = Get-DatabaseSnapshot $project
if ($databaseSnapshot.Count -ne $databaseAfter.Count) { throw 'Local database files changed during packaging. Stop local services and create a new package.' }
foreach ($databasePath in $databaseSnapshot.Keys) {
    $relativePath = $databasePath.Substring($project.Length + 1)
    $databaseCopy = Join-Path (Join-Path $bundle 'workspace') $relativePath
    if (-not $databaseAfter.ContainsKey($databasePath) -or $databaseSnapshot[$databasePath] -ne $databaseAfter[$databasePath] -or
        -not (Test-Path -LiteralPath $databaseCopy) -or (Get-FileHash -LiteralPath $databaseCopy -Algorithm SHA256).Hash -ne $databaseSnapshot[$databasePath]) {
        throw "Local database changed during packaging: $relativePath. Stop local services and create a new package."
    }
}
foreach ($file in @('.vscode\sftp.json', 'qq-bot-py\.vscode\sftp.json', 'qq-bot-py\config.yaml', 'qq-bot-py\.env', 'gsuid_core_app\.env')) {
    $original = Join-Path $project $file
    $copied = Join-Path (Join-Path $bundle 'workspace') $file
    if (Test-Path -LiteralPath $original) {
        if (-not (Test-Path -LiteralPath $copied)) { throw "Private configuration missing: $file" }
        if ((Get-FileHash -LiteralPath $original).Hash -ne (Get-FileHash -LiteralPath $copied).Hash) {
            throw "Configuration changed while packing: $file"
        }
    }
}
& (Join-Path $bundle 'apps\git\cmd\git.exe') --version | Out-Null
if ($LASTEXITCODE -ne 0) { throw 'Copied Git cannot run.' }
& (Join-Path $bundle 'apps\node\node.exe') --version | Out-Null
if ($LASTEXITCODE -ne 0) { throw 'Copied Node.js cannot run.' }
$copiedCode = Join-Path $bundle 'apps\vscode\bin\code.cmd'
& $copiedCode --user-data-dir (Join-Path $bundle 'vscode-user') --extensions-dir (Join-Path $bundle 'extensions') --version | Out-Null
if ($LASTEXITCODE -ne 0) { throw 'Copied VS Code cannot run.' }
$copiedExtensions = @(& $copiedCode --user-data-dir (Join-Path $bundle 'vscode-user') --extensions-dir (Join-Path $bundle 'extensions') --list-extensions --show-versions)
if ($LASTEXITCODE -ne 0 -or (Compare-Object ($extensionVersions | Sort-Object) ($copiedExtensions | Sort-Object))) {
    throw 'Copied VS Code extension list does not match the source.'
}

Write-Host '[6/6] Calculating checksums for the USB transfer...'
$bundlePrefix = $bundle.TrimEnd('\') + '\'
$files = @(Get-ChildItem -LiteralPath $bundle -Recurse -Force -File | Where-Object {
    $_.Name -ne 'INCOMPLETE.txt' -and $_.Name -ne 'SHA256SUMS.csv'
})
$checksums = foreach ($file in $files) {
    [pscustomobject]@{ Path = $file.FullName.Substring($bundlePrefix.Length); SHA256 = (Get-FileHash -LiteralPath $file.FullName -Algorithm SHA256).Hash }
}
$checksums | Export-Csv -LiteralPath (Join-Path $bundle 'SHA256SUMS.csv') -NoTypeInformation -Encoding UTF8
Remove-Item -LiteralPath (Join-Path $bundle 'INCOMPLETE.txt')
$totalBytes = ($files | Measure-Object -Property Length -Sum).Sum
Write-Host ("Ready: {0:N2} GB, {1} files" -f ($totalBytes / 1GB), $files.Count)
Write-Host "Copy the entire folder to USB: $bundle"
Write-Host 'On the other PC, double-click Open-QQBot.cmd. This folder contains passwords and API keys.'
