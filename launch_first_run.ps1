param()

$root = Split-Path -Parent $MyInvocation.MyCommand.Path
$bootstrap = Join-Path $root 'bootstrap_environment.py'
$coordinator = Join-Path $root 'endfield_first_run.py'
$venvPython = Join-Path $root '.venv\Scripts\python.exe'

$hostPython = $null
$py = Get-Command py -ErrorAction SilentlyContinue
if ($py) {
    & $py.Source -3 -c "import sys; print(sys.executable)" *> $null
    if ($LASTEXITCODE -eq 0) {
        $hostPython = @($py.Source, '-3')
    }
}
if (-not $hostPython) {
    $python = Get-Command python -ErrorAction SilentlyContinue
    if ($python) {
        & $python.Source -c "import sys; print(sys.executable)" *> $null
        if ($LASTEXITCODE -eq 0) {
            $hostPython = @($python.Source)
        }
    }
}
if (-not $hostPython) {
    throw 'No usable Python 3 interpreter was found. Install Python 3.10+ and retry.'
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
