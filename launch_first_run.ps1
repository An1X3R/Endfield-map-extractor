param()

$root = Split-Path -Parent $MyInvocation.MyCommand.Path
$bootstrap = Join-Path $root 'bootstrap_environment.py'
$coordinator = Join-Path $root 'src\endfield_first_run.py'
$venvPython = Join-Path $root '.venv\Scripts\python.exe'

$hostPython = $null
$pythonVersionCheck = "import sys; raise SystemExit(0 if sys.implementation.name == 'cpython' and (3, 11) <= sys.version_info[:2] < (3, 15) else 1)"
$py = Get-Command py -ErrorAction SilentlyContinue
if ($py) {
    foreach ($pythonArgument in @('-3.14', '-3.13', '-3.12', '-3.11')) {
        & $py.Source $pythonArgument -c $pythonVersionCheck *> $null
        if ($LASTEXITCODE -eq 0) {
            $hostPython = @($py.Source, $pythonArgument)
            break
        }
    }
}
if (-not $hostPython) {
    $python = Get-Command python -ErrorAction SilentlyContinue
    if ($python) {
        & $python.Source -c $pythonVersionCheck *> $null
        if ($LASTEXITCODE -eq 0) {
            $hostPython = @($python.Source)
        }
    }
}
if (-not $hostPython) {
    throw 'No compatible CPython 3.11 through 3.14 interpreter was found. Install one and retry.'
}

if ($hostPython.Count -eq 2) {
    & $hostPython[0] $hostPython[1] $bootstrap --check
} else {
    & $hostPython[0] $bootstrap --check
}
if ($LASTEXITCODE -ne 0) {
    exit $LASTEXITCODE
}
if (-not (Test-Path -LiteralPath $venvPython -PathType Leaf)) {
    throw "The isolated Python environment was not created: $venvPython"
}

& $venvPython $coordinator @args
exit $LASTEXITCODE
