# Corresponding source build

This source kit contains the first-party project files and the exact upstream source snapshots used by the readers. The scene snapshot is AnimeStudio `7cfb26b9c158f6150c887f9157de97a3fa7672e1`; the character snapshot is Variante AnimeStudio `5bb936ca1d3662fe9b956ec669ab7ebea08bfd07`; the Ruri snapshot is `ed62f85af4e5da83dabd514b29ee93ef7d34e86b`. Their file inventories, source licenses, and patch hashes are recorded in each `SOURCE-PROVENANCE.json` and in the release-level `SOURCE-MANIFEST.json`.

The Ooz native library was rebuilt successfully from the included pinned Oodle and SIMDe sources with the documented CMake helper. The character reader uses the portable JSON/model source path; it does not build FBX export, Autodesk SDK, or FMOD bindings. The source kit excludes extracted game source, game shader/resource payloads, and `CNKeys.json`. The existing default VFS key constant is an explicit approved inclusion exception for compatibility; the release-level source manifest records the exception without repeating its value. This build record is not a legal clearance or a guarantee about a particular release archive.

The precompiled user release does not need these build tools or source downloads. To rebuild, work from the extracted source-kit root with CPython 3.11–3.14, .NET SDK 9.0.308, and Visual Studio 2022 C++ build tools with CMake. Restore NuGet and npm packages from their checked-in lock files; `third_party/NuGet.Config` selects nuget.org without extra feeds. No Git checkout is needed because the pinned AnimeStudio and Ruri source is already included.

Build the scene helpers from the included scene source snapshot:

```powershell
$root = (Get-Location).Path
$work = Join-Path $root 'build\scene'
python src\endfield_dependency_bootstrap.py `
  --workspace (Join-Path $work 'workspace') `
  --log-dir (Join-Path $work 'logs') `
  --output-root (Join-Path $work 'artifacts') `
  --source-root (Join-Path $root 'third_party\sources\scene')
```

Build the character tools from the included character and Ruri snapshots:

```powershell
$root = (Get-Location).Path
$work = Join-Path $root 'build\character'
$env:TEMP = Join-Path $work 'temp'
$env:TMP = $env:TEMP
New-Item -ItemType Directory -Force -Path $env:TEMP | Out-Null
$dotnet = (Get-Command dotnet.exe -ErrorAction Stop).Source
python src\endfield_character_tools.py `
  --output-root (Join-Path $work 'artifacts') `
  --animestudio-root (Join-Path $root 'third_party\sources\character') `
  --ruri-root (Join-Path $root 'third_party\sources\ruri') `
  --nuget-packages (Join-Path $work 'nuget_packages') `
  --nuget-config (Join-Path $root 'third_party\NuGet.Config') `
  --dotnet $dotnet
```

`src/build_ooz_source.ps1` rebuilds the Ooz native library from the complete included Oodle and SIMDe source tree. The generated DLL belongs in the build workspace; it is not part of the source snapshot. Keep the source snapshot manifests and all notices under `third_party/` with the corresponding sources.
