"""Explicitly build pinned character readers; normal application startup never fetches tools."""

from __future__ import annotations

import argparse
from dataclasses import dataclass
import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess
from typing import Mapping


ROOT = Path(__file__).resolve().parent.parent
LOCK = ROOT / "third_party" / "character-dotnet.lock.json"
TOOL_PROJECTS = {
    "static_reader": "CharacterStaticProbe",
    "shader_reader": "ShaderPropertyDefaultsProbe",
    "visibility_reader": "RendererVisibilityProbe",
    "animation_reader": "CharacterAnimationReader",
    "auxiliary_sampler": "AnimatorAuxSampler",
    "npc_assembly_probe": "NpcAssemblyProbe",
    "npc_shader_probe": "NpcShaderIdentityProbe",
    "npc_animation_reader": "NpcAnimationReader",
    "controller_audit": "ControllerAudit",
    "weather_reader": "GlobalWeatherProbe",
    "expression_reader": "ExpressionSourceProbe",
    "snow_channel_reader": "SnowChannelProbe",
    "json_reader": "JsonAssetReader",
}
DLL_TOOLS = frozenset(("animation_reader", "auxiliary_sampler", "npc_animation_reader", "expression_reader"))


class CharacterToolsError(RuntimeError):
    """A pinned source, build prerequisite, or installed artifact is invalid."""


@dataclass(frozen=True)
class CharacterToolBuild:
    output_root: Path
    animestudio_root: Path
    ruri_root: Path
    nuget_packages: Path
    nuget_config: Path
    dotnet: Path


def _sha(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _object(value: object, field: str) -> dict[str, object]:
    if not isinstance(value, dict) or not all(isinstance(key, str) for key in value):
        raise CharacterToolsError(f"CHARACTER_TOOLS_INVALID_OBJECT: {field}")
    return value


def _text(value: object, field: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise CharacterToolsError(f"CHARACTER_TOOLS_MISSING_TEXT: {field}")
    return value


def _relative(root: Path, value: object, field: str) -> Path:
    text = _text(value, field)
    path = (root / text).resolve()
    if Path(text).is_absolute() or not path.is_relative_to(root.resolve()):
        raise CharacterToolsError(f"CHARACTER_TOOLS_PATH_OUTSIDE_ROOT: {field}={text}")
    return path


def _lock() -> dict[str, object]:
    value = _object(json.loads(LOCK.read_text(encoding="utf-8")), "dependency lock")
    if value.get("format") != "EndfieldCharacterDotnetDependencies/1":
        raise CharacterToolsError("CHARACTER_TOOLS_UNSUPPORTED_LOCK_FORMAT")
    return value


def _verify_file(path: Path, expected: object, field: str) -> None:
    digest = _text(expected, field).lower()
    if len(digest) != 64 or any(char not in "0123456789abcdef" for char in digest):
        raise CharacterToolsError(f"CHARACTER_TOOLS_INVALID_HASH: {field}")
    if not path.is_file() or _sha(path) != digest:
        raise CharacterToolsError(f"CHARACTER_TOOLS_SOURCE_HASH_MISMATCH: {path}; install the pinned source and patch before building.")


def _snapshot_files(root: Path) -> dict[str, Path]:
    """Enumerate the complete supplied source inventory, excluding generated build directories."""
    generated = {".git", "bin", "obj", "bin-flat", "obj-flat", ".vs"}
    return {path.relative_to(root).as_posix(): path for path in root.rglob("*") if path.is_file()
            and path.name != "SOURCE-PROVENANCE.json"
            and not generated.intersection(path.relative_to(root).parts)}


def _verify_snapshot(root: Path, dependency: dict[str, object], name: str) -> None:
    """Verify a complete pinned source archive without requiring a Git executable."""
    manifest = _object(json.loads((root / "SOURCE-PROVENANCE.json").read_text(encoding="utf-8")), f"{name}.snapshot")
    if (manifest.get("format") != "EndfieldPinnedSourceSnapshot/1"
            or manifest.get("repository") != dependency.get("repository")
            or manifest.get("commit") != dependency.get("commit")):
        raise CharacterToolsError(f"CHARACTER_TOOLS_SOURCE_SNAPSHOT_IDENTITY_MISMATCH: {name}")
    _text(manifest.get("license"), f"{name}.snapshot.license")
    inventory = _object(manifest.get("files"), f"{name}.snapshot.files")
    actual = _snapshot_files(root)
    if not inventory or set(inventory) != set(actual):
        raise CharacterToolsError(f"CHARACTER_TOOLS_SOURCE_SNAPSHOT_INVENTORY_MISMATCH: {name}; "
                                 f"missing={sorted(set(inventory) - set(actual))}; extra={sorted(set(actual) - set(inventory))}")
    for path, digest in inventory.items():
        _verify_file(_relative(root, path, f"{name}.snapshot.path"), digest, f"{name}.snapshot.sha256")
    patches = manifest.get("appliedPatches")
    if not isinstance(patches, list):
        raise CharacterToolsError(f"CHARACTER_TOOLS_SOURCE_SNAPSHOT_PATCHES_MISSING: {name}")
    rows = [_object(value, f"{name}.snapshot.appliedPatches") for value in patches]
    for row in rows:
        _verify_file(_relative(ROOT, row.get("path"), f"{name}.snapshot.patch.path"), row.get("sha256"), f"{name}.snapshot.patch.sha256")
    if not any(row.get("path") == dependency.get("patch") and row.get("sha256") == dependency.get("patchSha256") for row in rows):
        raise CharacterToolsError(f"CHARACTER_TOOLS_SOURCE_SNAPSHOT_PATCH_MISMATCH: {name}")


def _verify_checkout(root: Path, dependency: dict[str, object], name: str) -> None:
    """Check Git provenance for a developer checkout; archive builds use the snapshot inventory."""
    result = subprocess.run(["git", "-C", str(root), "rev-parse", "HEAD"], capture_output=True, text=True, check=True)
    if result.stdout.strip() != dependency.get("commit"):
        raise CharacterToolsError(f"CHARACTER_TOOLS_SOURCE_COMMIT_MISMATCH: {name}")
    changed = subprocess.run(["git", "-C", str(root), "--no-pager", "diff", "HEAD", "--name-only"], capture_output=True, text=True, check=True)
    compiled = dependency.get("compiledFiles")
    if not isinstance(compiled, list):
        raise CharacterToolsError(f"CHARACTER_TOOLS_MISSING_COMPILED_FILES: {name}")
    allowed = {_text(_object(item, f"{name}.compiledFiles")["path"], f"{name}.path") for item in compiled}
    removed = dependency.get("removedPaths", [])
    if not isinstance(removed, list) or any(not isinstance(path, str) for path in removed):
        raise CharacterToolsError(f"CHARACTER_TOOLS_INVALID_REMOVED_SOURCE_PATHS: {name}")
    allowed.update(removed)
    if set(changed.stdout.splitlines()) - allowed:
        raise CharacterToolsError(f"CHARACTER_TOOLS_UNEXPECTED_SOURCE_CHANGES: {name}")
    untracked = subprocess.run(["git", "-C", str(root), "ls-files", "--others", "--exclude-standard"], capture_output=True, text=True, check=True)
    if any(Path(path).suffix in {".cs", ".csproj", ".props", ".targets"} for path in untracked.stdout.splitlines()):
        raise CharacterToolsError(f"CHARACTER_TOOLS_UNTRACKED_BUILD_SOURCE: {name}")


def verify_character_sources(animestudio_root: Path, ruri_root: Path) -> dict[str, object]:
    """Validate the exact public checkout and the explicitly adapted third-party files."""
    lock = _lock()
    for name, root in (("animeStudio", animestudio_root), ("ruri", ruri_root)):
        dependency = _object(lock.get(name), name)
        patch = _relative(ROOT, dependency.get("patch"), f"{name}.patch")
        _verify_file(patch, dependency.get("patchSha256"), f"{name}.patchSha256")
        entries = dependency.get("compiledFiles")
        if not isinstance(entries, list) or not entries:
            raise CharacterToolsError(f"CHARACTER_TOOLS_MISSING_COMPILED_FILES: {name}")
        for item in entries:
            entry = _object(item, f"{name}.compiledFiles")
            _verify_file(_relative(root, entry.get("path"), f"{name}.path"), entry.get("sha256"), f"{name}.sha256")
            if "override" in entry:
                _verify_file(_relative(ROOT, entry["override"], f"{name}.override"), entry["sha256"], f"{name}.override.sha256")
        removed = dependency.get("removedPaths", [])
        if not isinstance(removed, list):
            raise CharacterToolsError(f"CHARACTER_TOOLS_INVALID_REMOVED_SOURCE_PATHS: {name}")
        for path in removed:
            if _relative(root, path, f"{name}.removedPath").exists():
                raise CharacterToolsError(f"CHARACTER_TOOLS_FORBIDDEN_SOURCE_FILE_PRESENT: {name}/{path}")
        if (root / "SOURCE-PROVENANCE.json").is_file():
            _verify_snapshot(root, dependency, name)
        else:
            _verify_checkout(root, dependency, name)
    return lock


def character_tool_source_identity() -> str:
    """Identify repository build inputs without including installed binaries or game files."""
    files = {LOCK, ROOT / "global.json", Path(__file__).resolve(), ROOT / "src" / "setup_character_tools.py",
             ROOT / "src" / "release_source_overrides.py", ROOT / "src" / "build_ooz_source.ps1"}
    for directory in (ROOT / "dotnet" / "CharacterTools", ROOT / "third_party" / "character-nuget",
                      ROOT / "third_party" / "source-overrides", ROOT / "third_party" / "licenses"):
        files.update(path for path in directory.rglob("*") if path.is_file()
                     and not {"bin", "obj"}.intersection(path.relative_to(directory).parts)
                     and path.suffix in {".cs", ".csproj", ".props", ".targets", ".json", ".txt", ".md"})
    for name in ("animeStudio", "ruri"):
        entry = _object(_lock()[name], name)
        files.add(_relative(ROOT, entry["patch"], f"{name}.patch"))
    entries = [(path.relative_to(ROOT).as_posix(), _sha(path)) for path in sorted(files)]
    return hashlib.sha256(json.dumps(entries, separators=(",", ":")).encode()).hexdigest()


def _run(command: list[str], output: Path, environment: Mapping[str, str]) -> None:
    with output.open("wb") as handle:
        result = subprocess.run(command, cwd=ROOT, env=dict(environment), stdout=handle, stderr=subprocess.STDOUT, check=False)
    if result.returncode != 0:
        raise CharacterToolsError(f"CHARACTER_TOOLS_BUILD_FAILED: exit={result.returncode}; command={command}; log={output}")


def _native_cmake() -> Path:
    """Locate the CMake bundled with the required Visual Studio native build tools."""
    installer = Path(os.environ["ProgramFiles(x86)"]) / "Microsoft Visual Studio" / "Installer" / "vswhere.exe"
    if not installer.is_file():
        raise CharacterToolsError("CHARACTER_TOOLS_NATIVE_BUILD_TOOLS_MISSING: install Visual Studio 2022 C++ build tools with CMake.")
    result = subprocess.run([str(installer), "-latest", "-version", "[17.0,18.0)", "-products", "*",
                             "-requires", "Microsoft.VisualStudio.Component.VC.CMake.Project", "-find",
                             "Common7/IDE/CommonExtensions/Microsoft/CMake/CMake/bin/cmake.exe"],
                            capture_output=True, text=True, check=True)
    paths = result.stdout.strip().splitlines()
    if len(paths) != 1 or not Path(paths[0]).is_file():
        raise CharacterToolsError(f"CHARACTER_TOOLS_NATIVE_CMAKE_MISSING: Visual Studio 2022 CMake result={result.stdout!r}")
    return Path(paths[0])


def _verify_geometry_dependencies(directory: Path) -> None:
    """Reject proprietary SDK files or managed references in the concrete tool installation."""
    forbidden = ("fbxnative", "fbxwrapper", "fbxsdk", "libfbx")
    for path in directory.rglob("*"):
        if not path.is_file():
            continue
        if any(name in path.name.casefold() for name in forbidden):
            raise CharacterToolsError(f"CHARACTER_TOOLS_AUTODESK_FILE_PRESENT: {path}")
        if path.name.endswith(".deps.json"):
            dependencies = _object(json.loads(path.read_text(encoding="utf-8")), "tool dependency manifest")
            libraries = _object(dependencies.get("libraries"), "tool dependency libraries")
            matches = [name for name in libraries if any(value in name.casefold() for value in forbidden)]
            if matches:
                raise CharacterToolsError(f"CHARACTER_TOOLS_AUTODESK_REFERENCE_PRESENT: manifest={path}; libraries={matches}")


def build_character_tools(request: CharacterToolBuild) -> Path:
    """Build an installation from supplied pinned source roots and explicit NuGet configuration."""
    lock = verify_character_sources(request.animestudio_root, request.ruri_root)
    license_sources = ROOT / "third_party" / "licenses"
    if not license_sources.is_dir():
        raise CharacterToolsError("CHARACTER_TOOLS_LICENSE_MATERIALS_MISSING")
    source_identity = character_tool_source_identity()
    if not request.dotnet.is_file() or not request.nuget_config.is_file() or not request.nuget_packages.is_dir():
        raise CharacterToolsError("CHARACTER_TOOLS_PREREQUISITES_MISSING: dotnet, NuGet config and package directory must exist.")
    if request.output_root.exists():
        raise CharacterToolsError(f"CHARACTER_TOOLS_OUTPUT_EXISTS: choose a new installation directory: {request.output_root}")
    version = subprocess.run([str(request.dotnet), "--version"], cwd=ROOT, capture_output=True, text=True, check=True)
    if version.stdout.strip() != lock.get("sdk"):
        raise CharacterToolsError(f"CHARACTER_TOOLS_SDK_MISMATCH: required={lock.get('sdk')}; actual={version.stdout.strip()}")
    cmake = _native_cmake()
    for source in (ROOT / "third_party" / "character-nuget").rglob("packages.lock.json"):
        destination = request.animestudio_root / source.relative_to(ROOT / "third_party" / "character-nuget")
        if not any(destination.parent.glob("*.csproj")):
            continue
        if (request.animestudio_root / "SOURCE-PROVENANCE.json").is_file():
            _verify_file(destination, _sha(source), "animeStudio.nugetPackageLock")
        else:
            shutil.copyfile(source, destination)
    request.output_root.mkdir(parents=True)
    temporary = request.output_root / "temp"
    logs = request.output_root / "logs"
    temporary.mkdir()
    logs.mkdir()
    environment = {**os.environ, "TEMP": str(temporary), "TMP": str(temporary), "DOTNET_CLI_HOME": str(temporary),
                   "DOTNET_SKIP_FIRST_TIME_EXPERIENCE": "1", "DOTNET_CLI_TELEMETRY_OPTOUT": "1", "NUGET_PACKAGES": str(request.nuget_packages)}
    native_library = request.output_root / "native" / "AnimeStudio.Ooz.dll"
    _run(["powershell.exe", "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass", "-File",
          str(ROOT / "src" / "build_ooz_source.ps1"), "-SourceRoot", str(request.animestudio_root / "AnimeStudio.Oodle"),
          "-BuildRoot", str(request.output_root / "native-build"), "-OutputPath", str(native_library), "-CMakePath", str(cmake)],
         logs / "Ooz.source-build.log", environment)
    if not native_library.is_file():
        raise CharacterToolsError(f"CHARACTER_TOOLS_NATIVE_ARTIFACT_MISSING: {native_library}")
    properties = [f"-p:AnimeStudioRoot={request.animestudio_root}", f"-p:RuriRoot={request.ruri_root}",
                  f"-p:AnimeStudioOozPath={native_library}", "-p:UseArtifactsOutput=true",
                  f"-p:ArtifactsPath={request.output_root / 'build'}", "-p:RestorePackagesWithLockFile=true"]
    tools: dict[str, str] = {}
    projects = [(key, ROOT / "dotnet" / "CharacterTools" / name / f"{name}.csproj", name) for key, name in TOOL_PROJECTS.items()]
    for key, project, name in projects:
        _run([str(request.dotnet), "restore", str(project), "--locked-mode", "--configfile", str(request.nuget_config),
              "--packages", str(request.nuget_packages), *properties], logs / f"{name}.restore.log", environment)
        output = request.output_root / "tools" / name
        _run([str(request.dotnet), "build", str(project), "--no-restore", "-c", "Release", "-f", "net9.0-windows", "--output", str(output),
              *properties], logs / f"{name}.build.log", environment)
        artifact_name = "AnimeStudio.CLI" if key == "json_reader" else name
        artifact = output / f"{artifact_name}{'.dll' if key in DLL_TOOLS else '.exe'}"
        if not artifact.is_file():
            raise CharacterToolsError(f"CHARACTER_TOOLS_ARTIFACT_MISSING: {artifact}")
        tools[key] = artifact.relative_to(request.output_root).as_posix()
    sampler_reader = request.output_root / "tools" / "AnimatorAuxSampler" / "CharacterAnimationReader.dll"
    if not sampler_reader.is_file():
        raise CharacterToolsError("CHARACTER_TOOLS_AUXILIARY_READER_DEPENDENCY_MISSING")
    _verify_geometry_dependencies(request.output_root / "tools")
    licenses = request.output_root / "licenses"
    licenses.mkdir()
    shutil.copytree(license_sources, licenses, dirs_exist_ok=True)
    shutil.copyfile(request.ruri_root / "LICENSE", licenses / "Ruri.AGPL-3.0.txt")
    shutil.copyfile(request.animestudio_root / "LICENSE", licenses / "AnimeStudio.MIT.txt")
    artifacts = {key: {"bytes": (request.output_root / path).stat().st_size, "sha256": _sha(request.output_root / path)} for key, path in tools.items()}
    files = {path.relative_to(request.output_root).as_posix(): {"bytes": path.stat().st_size, "sha256": _sha(path)}
             for directory in (request.output_root / "tools", licenses) for path in sorted(directory.rglob("*")) if path.is_file()}
    if character_tool_source_identity() != source_identity:
        raise CharacterToolsError("CHARACTER_TOOLS_BUILD_INPUTS_CHANGED: repository source or license inputs changed during compilation.")
    receipt = {"format": "EndfieldCharacterTools/1", "status": "built", "source_identity": source_identity,
               "sdk": lock["sdk"], "target_framework": lock["targetFramework"], "tools": tools, "artifacts": artifacts,
               "files": files, "dependencies": {name: _object(lock[name], name) for name in ("animeStudio", "ruri")},
               "source_snapshots": {name: _sha(root / "SOURCE-PROVENANCE.json") for name, root in
                                    (("animeStudio", request.animestudio_root), ("ruri", request.ruri_root))
                                    if (root / "SOURCE-PROVENANCE.json").is_file()},
               "source_built_native": {"path": native_library.relative_to(request.output_root).as_posix(),
                                       "bytes": native_library.stat().st_size, "sha256": _sha(native_library),
                                       "build_log": "logs/Ooz.source-build.log"},
               "scope": "Explicitly built offline reader processes; no game assets, native decompiled source or live game state are distributed.",
               "native_runtime_boundary": lock["runtimeArtifacts"]}
    path = request.output_root / "character_tools.json"
    path.write_text(json.dumps(receipt, indent=2) + "\n", encoding="utf-8")
    return path


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    for name in ("output-root", "animestudio-root", "ruri-root", "nuget-packages", "nuget-config", "dotnet"):
        parser.add_argument(f"--{name}", type=Path, required=True)
    args = parser.parse_args()
    request = CharacterToolBuild(*(getattr(args, name).resolve() for name in
                                  ("output_root", "animestudio_root", "ruri_root", "nuget_packages", "nuget_config", "dotnet")))
    print(json.dumps({"status": "built", "receipt": str(build_character_tools(request))}))


if __name__ == "__main__":
    main()
