param(
    [switch]$PrepareSceneHelpers,
    [string]$HelperCacheRoot
)

$ErrorActionPreference = 'Stop'

function Test-SupportedPythonRuntime {
    param(
        [string]$Executable,
        [string[]]$RuntimeArguments,
        [string]$VersionCheck
    )

    $previousPreference = $ErrorActionPreference
    try {
        # A missing launcher version is an expected probe result in Windows PowerShell.
        $ErrorActionPreference = 'Continue'
        & $Executable @RuntimeArguments -c $VersionCheck *> $null
        return $LASTEXITCODE -eq 0
    } finally {
        $ErrorActionPreference = $previousPreference
    }
}

$root = Split-Path -Parent $MyInvocation.MyCommand.Path
$bootstrap = Join-Path $root 'bootstrap_environment.py'
$venvPython = Join-Path $root '.venv\Scripts\python.exe'
$webui = Join-Path $root 'webui'
$helpers = Join-Path $root 'src\endfield_scene_helpers.py'
$localCache = Join-Path $root '.tools\setup-cache'
$localTemp = Join-Path $localCache 'tmp'
New-Item -ItemType Directory -Path $localTemp -Force | Out-Null
$env:TEMP = $localTemp
$env:TMP = $localTemp
$env:PIP_CACHE_DIR = Join-Path $localCache 'pip'
$env:npm_config_cache = Join-Path $localCache 'npm'

$hostPython = $null
$py = Get-Command py -ErrorAction SilentlyContinue
$pythonVersionCheck = "import sys; raise SystemExit(0 if sys.implementation.name == 'cpython' and (3, 11) <= sys.version_info[:2] < (3, 15) else 1)"
if ($py) {
    foreach ($pythonArgument in @('-3.14', '-3.13', '-3.12', '-3.11')) {
        if (Test-SupportedPythonRuntime -Executable $py.Source -RuntimeArguments @($pythonArgument) -VersionCheck $pythonVersionCheck) {
            $hostPython = @($py.Source, $pythonArgument)
            break
        }
    }
}
if (-not $hostPython) {
    $python = Get-Command python -ErrorAction SilentlyContinue
    if ($python) {
        if (Test-SupportedPythonRuntime -Executable $python.Source -RuntimeArguments @() -VersionCheck $pythonVersionCheck) {
            $hostPython = @($python.Source)
        }
    }
}
if (-not $hostPython) {
    throw 'CPython 3.11 through 3.14 is required. Install a supported version, then rerun setup_development_environment.ps1.'
}

if ($hostPython.Count -eq 2) {
    & $hostPython[0] $hostPython[1] $bootstrap --prepare
} else {
    & $hostPython[0] $bootstrap --prepare
}
if ($LASTEXITCODE -ne 0) {
    throw "Python environment setup failed with exit code $LASTEXITCODE."
}
if (-not (Test-Path -LiteralPath $venvPython -PathType Leaf)) {
    throw "Python setup completed without the expected interpreter: $venvPython"
}

$npmCommand = Get-Command npm.cmd -ErrorAction SilentlyContinue
if (-not $npmCommand) {
    $npmCommand = Get-Command npm -ErrorAction SilentlyContinue
}
if (-not $npmCommand) {
    throw 'Node.js/npm is required to build the local WebUI. Install Node.js, then rerun this setup script.'
}
$nodeCommand = Get-Command node.exe -ErrorAction Stop

Push-Location $webui
$previousPath = $env:Path
try {
    # npm extends PATH for child shells; long host paths exceed cmd.exe limits.
    $env:Path = @(
        (Split-Path -Parent $nodeCommand.Source),
        (Split-Path -Parent $npmCommand.Source),
        [Environment]::SystemDirectory,
        [Environment]::GetFolderPath('Windows')
    ) -join ';'
    & $npmCommand.Source ci --no-audit --no-fund
    if ($LASTEXITCODE -ne 0) {
        throw "WebUI dependency setup failed with exit code $LASTEXITCODE."
    }
    $viteVariables = @(
        'VITE_ENDFIELD_GAME_ROOT',
        'VITE_ENDFIELD_WEBUI_OUTPUT_ROOT',
        'VITE_ENDFIELD_CACHE_ROOT',
        'VITE_ENDFIELD_BLENDER'
    )
    $previousViteValues = @{}
    foreach ($name in $viteVariables) {
        $previousViteValues[$name] = [Environment]::GetEnvironmentVariable($name, 'Process')
        Remove-Item "Env:$name" -ErrorAction SilentlyContinue
    }
    try {
        & $npmCommand.Source run build
    } finally {
        foreach ($name in $viteVariables) {
            $value = $previousViteValues[$name]
            if ($null -eq $value) {
                Remove-Item "Env:$name" -ErrorAction SilentlyContinue
            } else {
                Set-Item "Env:$name" $value
            }
        }
    }
    if ($LASTEXITCODE -ne 0) {
        throw "WebUI production build failed with exit code $LASTEXITCODE."
    }
} finally {
    $env:Path = $previousPath
    Pop-Location
}

if ($PrepareSceneHelpers) {
    if (-not $HelperCacheRoot) {
        throw 'Pass -HelperCacheRoot with the configured runtime cache directory when using -PrepareSceneHelpers.'
    }
    & $venvPython $helpers --bootstrap-cache-root $HelperCacheRoot
    if ($LASTEXITCODE -ne 0) {
        throw "Pinned SceneProbe/BundleScanner setup failed with exit code $LASTEXITCODE."
    }
}

Write-Output "Development environment is ready. Start with: $root\launch_extractor.ps1"
