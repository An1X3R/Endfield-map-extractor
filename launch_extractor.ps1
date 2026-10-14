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

if ($Check) {
    if (-not (Test-Path -LiteralPath $python -PathType Leaf)) {
        $pythonCommand = Get-Command py -ErrorAction SilentlyContinue
        if ($pythonCommand) {
            & $pythonCommand.Source -3 -B $bootstrap --check
            exit $LASTEXITCODE
        }
        $pythonCommand = Get-Command python -ErrorAction SilentlyContinue
        if ($pythonCommand) {
            & $pythonCommand.Source -B $bootstrap --check
            exit $LASTEXITCODE
        }
        if ($env:ENDFIELD_EXTRACTOR_PYTHON) {
            if (Test-Path -LiteralPath $env:ENDFIELD_EXTRACTOR_PYTHON -PathType Leaf) {
                & $env:ENDFIELD_EXTRACTOR_PYTHON -B $bootstrap --check
                exit $LASTEXITCODE
            }
        }
        throw 'No usable Python interpreter was found. Install Python 3.11+ or set ENDFIELD_EXTRACTOR_PYTHON to a local interpreter.'
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
