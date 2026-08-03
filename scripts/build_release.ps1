<#
.SYNOPSIS
Builds and assembles the UpgradePackage release with editable external resources.

.DESCRIPTION
Runs PyInstaller as `pyinstaller -F main.py` and stages the release at
`dist/UpgradePackage` by default. The staged layout contains `main.exe` and the
repository resources tree, including `resources/version.yml`.

.EXAMPLE
powershell -NoProfile -File scripts/build_release.ps1
#>
param(
    [string]$PyInstallerExecutable = "pyinstaller",
    [string]$PythonExecutable = "python",
    [string]$ExecutableSource,
    [string]$ResourcesSource,
    [string]$StageDirectory,
    [switch]$SkipBuild
)

$ErrorActionPreference = "Stop"
Set-StrictMode -Version Latest

function Get-AbsolutePath {
    param([Parameter(Mandatory = $true)][string]$Path)

    return [System.IO.Path]::GetFullPath(
        $ExecutionContext.SessionState.Path.GetUnresolvedProviderPathFromPSPath($Path)
    )
}

$repositoryRoot = (Resolve-Path -LiteralPath (Join-Path $PSScriptRoot "..")).Path
$distRoot = Get-AbsolutePath (Join-Path $repositoryRoot "dist")
if (Test-Path -LiteralPath $distRoot) {
    $distRootItem = Get-Item -LiteralPath $distRoot -Force
    if (($distRootItem.Attributes -band [System.IO.FileAttributes]::ReparsePoint) -ne 0) {
        throw "Repository dist directory must not be a symbolic link or junction: $distRoot"
    }
}

if ([string]::IsNullOrWhiteSpace($StageDirectory)) {
    $StageDirectory = Join-Path $distRoot "UpgradePackage"
}
$stagePath = Get-AbsolutePath $StageDirectory

$distPrefix = $distRoot.TrimEnd(
    [System.IO.Path]::DirectorySeparatorChar,
    [System.IO.Path]::AltDirectorySeparatorChar
) + [System.IO.Path]::DirectorySeparatorChar
if (-not $stagePath.StartsWith($distPrefix, [System.StringComparison]::OrdinalIgnoreCase)) {
    throw "Stage directory must be a strict descendant of repository dist: $distRoot"
}

# A junction or symbolic link between dist and the stage could redirect a recursive
# clean outside the validated tree. Refuse such paths before removing anything.
$relativeStagePath = $stagePath.Substring($distPrefix.Length)
$currentPath = $distRoot
foreach ($segment in $relativeStagePath.Split(
    [System.IO.Path]::DirectorySeparatorChar,
    [System.StringSplitOptions]::RemoveEmptyEntries
)) {
    $currentPath = Join-Path $currentPath $segment
    if (Test-Path -LiteralPath $currentPath) {
        $item = Get-Item -LiteralPath $currentPath -Force
        if (($item.Attributes -band [System.IO.FileAttributes]::ReparsePoint) -ne 0) {
            throw "Stage directory must not traverse a symbolic link or junction: $currentPath"
        }
    }
}

if ([string]::IsNullOrWhiteSpace($ExecutableSource)) {
    $ExecutableSource = Join-Path $distRoot "main.exe"
}
if ([string]::IsNullOrWhiteSpace($ResourcesSource)) {
    $ResourcesSource = Join-Path $repositoryRoot "resources"
}

$executablePath = Get-AbsolutePath $ExecutableSource
$resourcesPath = Get-AbsolutePath $ResourcesSource
$sourceVersionPath = Join-Path $resourcesPath "version.yml"

if (-not (Test-Path -LiteralPath $sourceVersionPath -PathType Leaf)) {
    throw "Required external version configuration is missing: $sourceVersionPath"
}
if ($SkipBuild -and -not (Test-Path -LiteralPath $executablePath -PathType Leaf)) {
    throw "Executable source is missing: $executablePath"
}

if (-not $SkipBuild) {
    Push-Location $repositoryRoot
    try {
        & $PyInstallerExecutable -F main.py
        $buildSucceeded = $?
        $buildExitCodeVariable = Get-Variable -Name LASTEXITCODE -ErrorAction SilentlyContinue
        $buildExitCode = if ($null -eq $buildExitCodeVariable) { 0 } else { $buildExitCodeVariable.Value }
    }
    finally {
        Pop-Location
    }

    if (-not $buildSucceeded -or ($null -ne $buildExitCode -and $buildExitCode -ne 0)) {
        throw "PyInstaller failed with exit code $buildExitCode"
    }
    if (-not (Test-Path -LiteralPath $executablePath -PathType Leaf)) {
        throw "PyInstaller completed but executable source is missing: $executablePath"
    }
}

if (Test-Path -LiteralPath $stagePath) {
    Remove-Item -LiteralPath $stagePath -Recurse -Force
}
New-Item -ItemType Directory -Path $stagePath | Out-Null

try {
    $stagedExecutable = Join-Path $stagePath "main.exe"
    $stagedResources = Join-Path $stagePath "resources"
    Copy-Item -LiteralPath $executablePath -Destination $stagedExecutable -Force
    Copy-Item -LiteralPath $resourcesPath -Destination $stagedResources -Recurse -Force

    $stagedVersionPath = Join-Path $stagedResources "version.yml"
    if (-not (Test-Path -LiteralPath $stagedExecutable -PathType Leaf)) {
        throw "Staged executable verification failed: $stagedExecutable"
    }
    if (-not (Test-Path -LiteralPath $stagedVersionPath -PathType Leaf)) {
        throw "Staged version configuration verification failed: $stagedVersionPath"
    }

    $validationCode = "import sys; from consts.version_config import load_version_config; load_version_config(sys.argv[1])"
    Push-Location $repositoryRoot
    try {
        & $PythonExecutable -c $validationCode $stagedVersionPath
        $validationSucceeded = $?
        $validationExitCodeVariable = Get-Variable -Name LASTEXITCODE -ErrorAction SilentlyContinue
        $validationExitCode = if ($null -eq $validationExitCodeVariable) { 0 } else { $validationExitCodeVariable.Value }
    }
    finally {
        Pop-Location
    }
    if (-not $validationSucceeded -or ($null -ne $validationExitCode -and $validationExitCode -ne 0)) {
        throw "Staged version configuration validation failed with exit code $validationExitCode"
    }
}
catch {
    $originalFailure = $_
    try {
        if (Test-Path -LiteralPath $stagePath) {
            Remove-Item -LiteralPath $stagePath -Recurse -Force
        }
    }
    catch {
        Write-Warning "Failed to clean incomplete stage directory: $stagePath"
    }
    throw $originalFailure
}

Write-Output "Staged release: $stagePath"
