"""Configure already bundled readers locally; never fetch source or invoke a compiler."""
from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys

from endfield_character_runtime import configure_character_runtime, read_character_tools
from endfield_character_tools import CharacterToolsError, _object, _relative
from endfield_tool_bundle import read_tool_bundle


def _configured_path(path: Path, parent: Path) -> str:
    if path.resolve().drive.casefold() != parent.resolve().drive.casefold():
        return str(path.resolve())
    return os.path.relpath(path.resolve(), parent.resolve()).replace("\\", "/")


def _rebind_runtime(destination: Path, receipt: Path, scanner: Path, scene_probe: Path,
                    dotnet: Path, python: Path) -> None:
    """Refresh verified tool paths while preserving source selections and artist settings."""
    document = _object(json.loads(destination.read_text(encoding="utf-8")), "runtime")
    if document.get("format") != "EndfieldCharacterRuntime/2":
        raise CharacterToolsError(f"Bundled tools require a Runtime/2 configuration: {destination}")
    parent = destination.parent
    exports = _object(document.get("export_defaults"), "export_defaults")
    authoring = _object(document.get("authoring_tools"), "authoring_tools")
    npc_animation = _object(document.get("npc_animation_tools"), "npc_animation_tools")
    settings_path = _relative(parent, exports.get("fur_curves_settings"), "fur_curves_settings")
    settings = _object(json.loads(settings_path.read_text(encoding="utf-8")), "fur settings")
    settings_tools = _object(settings.get("tools"), "fur tools")
    installed = read_character_tools(receipt)
    paths = {name: _configured_path(path, parent) for name, path in installed.items()}
    npc_assembly = _object(document.get("npc_assembly_tools"), "npc_assembly_tools")
    with installed["snow_channel_reader"].open("rb") as stream:
        snow_hash = hashlib.file_digest(stream, "sha256").hexdigest()
    updated = {**document,
               "tools_receipt": _configured_path(receipt, parent), "scanner": _configured_path(scanner, parent),
               "weather_reader": paths["weather_reader"],
               "export_defaults": {**exports, "python": _configured_path(python, parent),
                                   **{key: paths[key] for key in ("static_reader", "shader_reader", "visibility_reader")},
                                   "potential_json_reader": paths["json_reader"],
                                   "potential_scene_probe": _configured_path(scene_probe, parent)},
               "authoring_tools": {**authoring, "dotnet": _configured_path(dotnet, parent),
                                   **{key: paths[key] for key in ("animation_reader", "auxiliary_sampler", "controller_audit", "expression_reader")},
                                   "controller_json_reader": paths["json_reader"]},
               "npc_assembly_tools": {**npc_assembly, "json_reader": paths["json_reader"],
                                      "assembly_probe": paths["npc_assembly_probe"], "shader_probe": paths["npc_shader_probe"]},
               "npc_animation_tools": {**npc_animation, "dotnet": _configured_path(dotnet, parent), "reader": paths["npc_animation_reader"]}}
    updated_settings = {**settings, "tools": {**settings_tools, "dotnet": _configured_path(dotnet, parent),
                        "snow_channel_probe": {"path": paths["snow_channel_reader"], "sha256": snow_hash}}}
    for path, value in ((settings_path, updated_settings), (destination, updated)):
        temporary = path.with_suffix(path.suffix + ".setup-tmp")
        with temporary.open("x", encoding="utf-8") as stream:
            stream.write(json.dumps(value, indent=2) + "\n")
        os.replace(temporary, path)


def install_bundled_tools(bundle_root: Path, dotnet: Path, python: Path) -> Path:
    """Verify the installed binary closure and bind it to this machine's interpreters."""
    tools = read_tool_bundle(bundle_root)
    if not python.is_file():
        raise FileNotFoundError(f"Python interpreter is missing: {python}")
    runtimes = subprocess.run([str(dotnet), "--list-runtimes"], check=True, capture_output=True, text=True)
    if not any(line.startswith("Microsoft.NETCore.App 9.") for line in runtimes.stdout.splitlines()):
        raise RuntimeError(f".NET 9 runtime is required by the bundled readers: {dotnet}; found: {runtimes.stdout.strip()}")
    destination = tools.character_receipt.parent / "runtime.json"
    if destination.exists():
        from endfield_character_service import CharacterService
        from endfield_scene_helpers import require_prepared_scene_helpers
        helpers = require_prepared_scene_helpers(destination, tools.scene)
        _rebind_runtime(destination, tools.character_receipt, helpers.scanner, helpers.probe, dotnet, python)
        CharacterService(destination)._runtime_defaults({}, None)
        return destination
    return configure_character_runtime(tools.character_receipt, dotnet, python, tools.scene, destination)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--bundle-root", type=Path, required=True)
    parser.add_argument("--dotnet", type=Path, required=True)
    args = parser.parse_args()
    path = install_bundled_tools(args.bundle_root.resolve(), args.dotnet.resolve(), Path(sys.executable).resolve())
    print(json.dumps({"status": "configured", "runtime": str(path), "source_downloaded": False, "tools_compiled": False}))


if __name__ == "__main__":
    main()
