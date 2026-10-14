param()

$ErrorActionPreference = 'Stop'
$root = Split-Path -Parent $MyInvocation.MyCommand.Path
$bundleRoot = Join-Path $root '.tools'
$receipt = Join-Path $bundleRoot 'tool_bundle.json'
if (-not (Test-Path -LiteralPath $receipt -PathType Leaf)) {
    throw "Precompiled tool bundle is missing: $receipt. Use the release package containing .tools, or prepare a developer build explicitly."
}
$dotnet = Get-Command dotnet.exe -ErrorAction SilentlyContinue
if (-not $dotnet) {
    throw 'Install the .NET 9 x64 runtime before configuring the precompiled readers. No SDK or Git is required.'
}

# Reuse Python and WebUI preparation without requesting source checkout or compilation.
& powershell -ExecutionPolicy Bypass -File (Join-Path $root 'setup_development_environment.ps1')
if ($LASTEXITCODE -ne 0) {
    throw "Python/WebUI preparation failed with exit code $LASTEXITCODE."
}
$python = Join-Path $root '.venv\Scripts\python.exe'
& $python -B (Join-Path $root 'src\install_bundled_tools.py') --bundle-root $bundleRoot --dotnet $dotnet.Source
if ($LASTEXITCODE -ne 0) {
    throw "Precompiled tool configuration failed with exit code $LASTEXITCODE."
}
Write-Output "Release environment is ready. Start with: $root\launch_extractor.ps1"
