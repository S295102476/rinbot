[CmdletBinding()]
param(
    [string]$Python,
    [switch]$Console
)

Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'

function Invoke-Checked {
    param(
        [Parameter(Mandatory = $true)][string]$Executable,
        [string[]]$Arguments = @()
    )
    & $Executable @Arguments
    if ($LASTEXITCODE -ne 0) {
        throw ('Command failed with exit code {0}: {1}' -f $LASTEXITCODE, $Executable)
    }
}

function Test-PythonVersion {
    param([string]$Executable, [string[]]$Arguments = @())
    try {
        $probe = @($Arguments) + @('-c', 'import sys; print(sys.version.split()[0]); sys.exit(0 if sys.version_info >= (3, 11) else 1)')
        $version = & $Executable @probe 2>$null
        if ($LASTEXITCODE -ne 0) { return $false }
        Write-Host ('Python {0}: {1}' -f (($version | Select-Object -Last 1).ToString().Trim()), $Executable)
        return $true
    }
    catch { return $false }
}

try {
    $bundleRoot = [IO.Path]::GetFullPath($PSScriptRoot)
    if (Test-Path -LiteralPath (Join-Path $bundleRoot 'INCOMPLETE.txt')) {
        throw 'This migration package is incomplete. Create it again on the source computer before restoring dependencies.'
    }
    $projectRoot = Join-Path $bundleRoot 'workspace\qq-bot-py'
    $lockRequirements = Join-Path $bundleRoot 'python-requirements.lock.txt'
    $projectRequirements = Join-Path $projectRoot 'requirements.txt'
    if (-not (Test-Path -LiteralPath $projectRoot -PathType Container)) {
        throw 'The workspace\qq-bot-py folder is missing. Run this script from an extracted migration package.'
    }
    if (-not (Test-Path -LiteralPath $projectRequirements -PathType Leaf)) {
        throw 'workspace\qq-bot-py\requirements.txt is missing from the migration package.'
    }

    $pythonExecutable = $null
    $pythonArguments = @()
    if ($Python) {
        if (Test-Path -LiteralPath $Python -PathType Leaf) {
            $pythonExecutable = (Resolve-Path -LiteralPath $Python).Path
        }
        else {
            $pythonCommand = Get-Command $Python -CommandType Application -ErrorAction SilentlyContinue | Select-Object -First 1
            if ($pythonCommand) { $pythonExecutable = $pythonCommand.Source }
        }
        if (-not $pythonExecutable -or -not (Test-PythonVersion $pythonExecutable)) {
            throw 'The -Python executable must be an installed Python 3.11 or newer. Pass the path to python.exe.'
        }
    }
    else {
        $launcher = Get-Command py -CommandType Application -ErrorAction SilentlyContinue | Select-Object -First 1
        if ($launcher -and (Test-PythonVersion $launcher.Source @('-3.11'))) {
            $pythonExecutable = $launcher.Source
            $pythonArguments = @('-3.11')
        }
        else {
            $systemPython = Get-Command python -CommandType Application -ErrorAction SilentlyContinue | Select-Object -First 1
            if ($systemPython -and (Test-PythonVersion $systemPython.Source)) {
                $pythonExecutable = $systemPython.Source
            }
        }
        if (-not $pythonExecutable) {
            throw 'Python 3.11 or newer was not found. Install Python 3.11 separately, then run this script again, or pass -Python C:\path\to\python.exe.'
        }
    }

    if ($Console) {
        $consoleRoot = Join-Path $projectRoot 'console'
        $nodeRoot = Join-Path $bundleRoot 'apps\node'
        $npmExecutable = Join-Path $nodeRoot 'npm.cmd'
        $nodeExecutable = Join-Path $nodeRoot 'node.exe'
        if (-not (Test-Path -LiteralPath $npmExecutable -PathType Leaf) -or
            -not (Test-Path -LiteralPath $nodeExecutable -PathType Leaf)) {
            throw 'Bundled apps\node\npm.cmd or node.exe is missing. Create the migration package again on the source computer.'
        }
        if (-not (Test-Path -LiteralPath (Join-Path $consoleRoot 'package-lock.json') -PathType Leaf)) {
            throw 'The console package-lock.json is missing. npm ci requires the lock file.'
        }
    }

    $venvRoot = Join-Path $projectRoot '.venv'
    $venvPython = Join-Path $venvRoot 'Scripts\python.exe'
    if (Test-Path -LiteralPath $venvRoot) {
        if (-not (Test-Path -LiteralPath $venvPython -PathType Leaf) -or
            -not (Test-PythonVersion $venvPython)) {
            throw 'The existing workspace\qq-bot-py\.venv cannot run. Rename it before restoring the environment; this script does not delete it.'
        }
        Write-Host 'Reusing the existing project virtual environment.'
    }
    else {
        Write-Host 'Creating workspace\qq-bot-py\.venv ...'
        Invoke-Checked $pythonExecutable (@($pythonArguments) + @('-m', 'venv', $venvRoot))
    }
    if (-not (Test-Path -LiteralPath $venvPython -PathType Leaf)) {
        throw 'Virtual environment creation completed without Scripts\python.exe.'
    }

    $pipArguments = @('-m', 'pip', 'install', '-r', $projectRequirements)
    if (Test-Path -LiteralPath $lockRequirements -PathType Leaf) {
        $pipArguments += @('-r', $lockRequirements)
        Write-Host 'Installing project requirements together with python-requirements.lock.txt ...'
    }
    else {
        Write-Host 'Installing project requirements.txt ...'
    }
    Push-Location -LiteralPath $projectRoot
    try {
        Invoke-Checked $venvPython $pipArguments
    }
    finally { Pop-Location }

    if ($Console) {
        Write-Host 'Installing console dependencies with the bundled Node.js ...'
        $originalPath = $env:PATH
        try {
            $env:PATH = $nodeRoot + [IO.Path]::PathSeparator + $env:PATH
            Push-Location -LiteralPath $consoleRoot
            try { Invoke-Checked $npmExecutable @('ci') }
            finally { Pop-Location }
        }
        finally { $env:PATH = $originalPath }
    }

    Write-Host 'Development dependencies are ready. No bot or server has been started.'
    Write-Host 'Use workspace\qq-bot-py\.venv\Scripts\python.exe as the project Python interpreter.'
    exit 0
}
catch {
    Write-Host ('ERROR: {0}' -f $_.Exception.Message) -ForegroundColor Red
    exit 1
}
