param(
    [Parameter(Mandatory = $true)]
    [ValidateNotNullOrEmpty()]
    [string]$SourceRoot,

    [Parameter(Mandatory = $true)]
    [ValidateNotNullOrEmpty()]
    [string]$BuildRoot,

    [Parameter(Mandatory = $true)]
    [ValidateNotNullOrEmpty()]
    [string]$OutputPath,

    [Parameter(Mandatory = $false)]
    [AllowNull()]
    [string]$CMakePath = $null
)

$ErrorActionPreference = 'Stop'

function Resolve-CMakePath {
    param([string]$RequestedPath)

    if (-not [string]::IsNullOrWhiteSpace($RequestedPath)) {
        $requested = [System.IO.Path]::GetFullPath($RequestedPath)
        if (-not (Test-Path -LiteralPath $requested -PathType Leaf)) {
            throw "CMAKE_EXECUTABLE_MISSING: CMake was not found at $requested."
        }
        return $requested
    }

    $pathCommand = Get-Command 'cmake.exe' -CommandType Application -ErrorAction SilentlyContinue
    if ($null -ne $pathCommand) {
        return [System.IO.Path]::GetFullPath($pathCommand.Source)
    }

    $programFilesX86 = [Environment]::GetFolderPath('ProgramFilesX86')
    $vswhere = Join-Path $programFilesX86 'Microsoft Visual Studio/Installer/vswhere.exe'
    if (Test-Path -LiteralPath $vswhere -PathType Leaf) {
        $vsCmake = & $vswhere -latest -products '*' -find 'Common7/IDE/CommonExtensions/Microsoft/CMake/CMake/bin/cmake.exe'
        if ($LASTEXITCODE -eq 0) {
            $candidate = @($vsCmake | Where-Object { -not [string]::IsNullOrWhiteSpace($_) } | Select-Object -First 1)
            if ($candidate.Count -eq 1 -and (Test-Path -LiteralPath $candidate[0] -PathType Leaf)) {
                return [System.IO.Path]::GetFullPath($candidate[0])
            }
        }
    }

    throw 'CMAKE_EXECUTABLE_MISSING: CMake was not found on PATH or in a Visual Studio installation discovered by vswhere.'
}

function Get-Sha256Hex {
    param([string]$Path)

    $algorithm = [System.Security.Cryptography.SHA256]::Create()
    $stream = [System.IO.File]::OpenRead($Path)
    try {
        return [BitConverter]::ToString($algorithm.ComputeHash($stream)).Replace('-', '').ToLowerInvariant()
    }
    finally {
        $stream.Dispose()
        $algorithm.Dispose()
    }
}

$source = [System.IO.Path]::GetFullPath($SourceRoot)
$build = [System.IO.Path]::GetFullPath($BuildRoot)
$output = [System.IO.Path]::GetFullPath($OutputPath)
$cmake = Resolve-CMakePath $CMakePath

if (-not (Test-Path -LiteralPath (Join-Path $source 'CMakeLists.txt') -PathType Leaf)) {
    throw "OOZ_SOURCE_INVALID: CMakeLists.txt is missing under $source."
}
if (-not (Test-Path -LiteralPath (Join-Path $source 'simde') -PathType Container)) {
    throw "OOZ_SOURCE_INVALID: the complete simde source directory is missing under $source."
}
$sourcePrefix = $source.TrimEnd([System.IO.Path]::DirectorySeparatorChar, [System.IO.Path]::AltDirectorySeparatorChar) + [System.IO.Path]::DirectorySeparatorChar
if ($build.Equals($source, [System.StringComparison]::OrdinalIgnoreCase) -or
    $build.StartsWith($sourcePrefix, [System.StringComparison]::OrdinalIgnoreCase)) {
    throw 'OOZ_BUILD_PATH_INVALID: keep generated build files outside the source tree.'
}
if ($output.Equals($source, [System.StringComparison]::OrdinalIgnoreCase) -or
    $output.StartsWith($sourcePrefix, [System.StringComparison]::OrdinalIgnoreCase)) {
    throw 'OOZ_OUTPUT_PATH_INVALID: keep the generated DLL outside the source tree.'
}

if (-not (Test-Path -LiteralPath $build -PathType Container)) {
    New-Item -ItemType Directory -Path $build | Out-Null
}
$outputDirectory = Split-Path -Parent $output
if (-not (Test-Path -LiteralPath $outputDirectory -PathType Container)) {
    New-Item -ItemType Directory -Path $outputDirectory | Out-Null
}

& $cmake -S $source -B $build -G 'Visual Studio 17 2022' -A x64 `
    -D OOZ_BUILD_BUN=OFF -D OOZ_BUILD_EXE=OFF -D OOZ_BUILD_VALIDATE=OFF
if ($LASTEXITCODE -ne 0) {
    throw "OOZ_CMAKE_CONFIGURE_FAILED: CMake exited with code $LASTEXITCODE."
}

& $cmake --build $build --target libooz --config Release
if ($LASTEXITCODE -ne 0) {
    throw "OOZ_CMAKE_BUILD_FAILED: CMake exited with code $LASTEXITCODE."
}

$builtLibrary = Join-Path $build 'Release/libooz.dll'
if (-not (Test-Path -LiteralPath $builtLibrary -PathType Leaf)) {
    throw "OOZ_BUILD_OUTPUT_MISSING: expected $builtLibrary after a successful build."
}

Copy-Item -LiteralPath $builtLibrary -Destination $output -Force
$result = [pscustomobject]@{
    algorithm = 'SHA256'
    sha256 = Get-Sha256Hex $output
    path = $output
}
$result | ConvertTo-Json -Compress
