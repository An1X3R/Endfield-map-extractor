param(
    [switch]$Check,
    [switch]$NoInstall,
    [Parameter(ValueFromRemainingArguments = $true)]
    [string[]]$ForwardArgs
)

$root = Split-Path -Parent $MyInvocation.MyCommand.Path
$python = Join-Path $root '.venv\Scripts\python.exe'
$bootstrap = Join-Path $root 'bootstrap_environment.py'
$webui = Join-Path $root 'launch_webui.py'
$pythonVersionCheck = "import sys; raise SystemExit(0 if sys.implementation.name == 'cpython' and (3, 11) <= sys.version_info[:2] < (3, 15) else 1)"

if ($Check) {
    if (-not (Test-Path -LiteralPath $python -PathType Leaf)) {
        $pythonCommand = Get-Command py -ErrorAction SilentlyContinue
        if ($pythonCommand) {
            foreach ($pythonArgument in @('-3.14', '-3.13', '-3.12', '-3.11')) {
                & $pythonCommand.Source $pythonArgument -c $pythonVersionCheck *> $null
                if ($LASTEXITCODE -eq 0) {
                    & $pythonCommand.Source $pythonArgument -B $bootstrap --check
                    exit $LASTEXITCODE
                }
            }
        }
        $pythonCommand = Get-Command python -ErrorAction SilentlyContinue
        if ($pythonCommand) {
            & $pythonCommand.Source -c $pythonVersionCheck *> $null
            if ($LASTEXITCODE -eq 0) {
                & $pythonCommand.Source -B $bootstrap --check
                exit $LASTEXITCODE
            }
        }
        if ($env:ENDFIELD_EXTRACTOR_PYTHON) {
            if (Test-Path -LiteralPath $env:ENDFIELD_EXTRACTOR_PYTHON -PathType Leaf) {
                & $env:ENDFIELD_EXTRACTOR_PYTHON -B $bootstrap --check
                exit $LASTEXITCODE
            }
        }
        throw 'No CPython 3.11 through 3.14 interpreter was found. Install one or set ENDFIELD_EXTRACTOR_PYTHON to a supported local interpreter.'
    }
    & $python -B $bootstrap --check
    exit $LASTEXITCODE
}

if (-not (Test-Path -LiteralPath $python -PathType Leaf)) {
    throw "Prepared Python environment is missing: $python. Run the explicit environment setup first."
}

# NoInstall remains accepted for compatibility; launch_webui.py never installs dependencies.
& $python -B $webui @ForwardArgs
exit $LASTEXITCODE
