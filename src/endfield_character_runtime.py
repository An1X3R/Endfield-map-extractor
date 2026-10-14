"""Configure the integrated character chain from an explicitly installed reader pack."""
from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import sys

from endfield_character_tools import character_tool_source_identity, CharacterToolsError


def _object(value: object, label: str) -> dict[str, object]:
    if not isinstance(value, dict) or any(not isinstance(key, str) for key in value):
        raise CharacterToolsError(f"Expected an object: {label}")
    return value


def _text(value: object, label: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise CharacterToolsError(f"Expected nonempty text: {label}")
    return value


def _hash(path: Path) -> str:
    with path.open('rb') as stream:
        return hashlib.file_digest(stream, 'sha256').hexdigest()


def read_character_tools(receipt: Path) -> dict[str, Path]:
    """Verify the entire installed closure, including native and managed dependencies."""
    root = receipt.resolve().parent
    raw = _object(json.loads(receipt.read_text(encoding='utf-8')), 'reader receipt')
    if raw.get('format') != 'EndfieldCharacterTools/1' or raw.get('status') != 'built':
        raise CharacterToolsError('Character reader installation is incomplete; rerun explicit tool setup.')
    if raw.get('source_identity') != character_tool_source_identity():
        raise CharacterToolsError('Character reader sources changed; rebuild readers with setup_character_tools.py.')
    files = _object(raw.get('files'), 'files')
    if not files:
        raise CharacterToolsError('Character reader receipt has no runtime dependency inventory.')
    for relative, value in files.items():
        path = (root / relative).resolve()
        if Path(relative).is_absolute() or not path.is_relative_to(root):
            raise CharacterToolsError(f'Reader artifact escapes installation: {relative}')
        expected = _object(value, relative)
        if not path.is_file() or path.stat().st_size != expected.get('bytes') or _hash(path) != expected.get('sha256'):
            raise CharacterToolsError(f'Reader runtime artifact changed or missing: {relative}; rerun explicit tool setup.')
    result: dict[str, Path] = {}
    for name, value in _object(raw.get('tools'), 'tools').items():
        relative = _text(value, name)
        if relative not in files:
            raise CharacterToolsError(f'Reader entry is absent from dependency inventory: {name}')
        result[name] = (root / relative).resolve()
    return result


def configure_character_runtime(receipt: Path, dotnet: Path, python: Path, helper_cache: Path, destination: Path) -> Path:
    """Write relocatable project paths; game/cache/output selections remain user-owned."""
    tools = read_character_tools(receipt)
    root = Path(__file__).resolve().parent.parent
    source = root / 'src'
    modules = source / 'character_runtime'
    from endfield_scene_helpers import require_prepared_scene_helpers
    scanner = require_prepared_scene_helpers(destination, helper_cache).scanner
    if not dotnet.is_file() or not python.is_file():
        raise CharacterToolsError('Select existing dotnet and Python executables before configuring character tools.')
    if destination.exists():
        raise FileExistsError(f'Runtime configuration already exists: {destination}')
    destination.parent.mkdir(parents=True, exist_ok=True)

    def relative(path: Path) -> str:
        if path.resolve().drive.casefold() != destination.resolve().drive.casefold():
            return str(path.resolve())
        return os.path.relpath(path.resolve(), destination.parent.resolve()).replace('\\', '/')

    exports: dict[str, object] = {name: relative(tools[name]) for name in ('static_reader', 'shader_reader', 'visibility_reader')}
    exports.update({'repository_src': relative(source), 'prepare_script': relative(modules / 'prepare_sample.py'),
                    'python': relative(python), 'unity_version': '2021.3.34f5', 'head_bone': 'Bip001_Head',
                    'face_right': [1, 0, 0], 'face_up': [0, 0, 1], 'face_forward': [0, -1, 0],
                    'hair_x': [-1, 0, 0], 'hair_y': [0, 0, 1], 'hair_z': [0, -1, 0]})
    authoring = {name: relative(tools[name]) for name in ('animation_reader', 'auxiliary_sampler', 'controller_audit', 'expression_reader')}
    authoring.update({name: relative(modules / filename) for name, filename in {
        'expression_exporter': 'prepare_expression_source.py', 'animation_exporter': 'export_animation.py',
        'authoring_builder': 'build_character_authoring.py', 'controller_resolver': 'resolve_character_controller.py'}.items()})
    authoring.update({'controller_json_reader': relative(tools['json_reader']), 'dotnet': relative(dotnet)})
    settings_path = destination.with_name('fur_curves_settings.json')
    if settings_path.exists():
        raise FileExistsError(settings_path)
    helper = modules / 'mesh_to_native_curves.py'
    settings = {'format': 'EndfieldFurCurvesPreparationSettings/1', 'tools': {
        'snow_channel_probe': {'path': relative(tools['snow_channel_reader']), 'sha256': _hash(tools['snow_channel_reader'])},
        'curve_helpers': {'path': relative(helper), 'sha256': _hash(helper)}, 'dotnet': relative(dotnet)},
        'source_vertex_variant': {'shader_name': 'HGRP/CharacterNPR', 'sub_shader_index': 0, 'pass_index': 0,
                                  'vertex_sub_program_index': 0, 'vertex_program_blob_index': 0},
        'artist_options': {'source_world_up': [0.0, 0.0, 1.0], 'radius_meters': 0.00008, 'root_stride': 1,
                           'artist_length_multiplier': 1.0, 'root_material_slot': 0}}
    settings_path.write_text(json.dumps(settings, indent=2) + '\n', encoding='utf-8')
    exports['fur_curves_settings'] = relative(settings_path)
    npc = {'exporter': relative(source / 'export_configured_npc.py'), 'json_reader': relative(tools['json_reader']),
           'assembly_probe': relative(tools['npc_assembly_probe']), 'shader_probe': relative(tools['npc_shader_probe']),
           'fbx_auditor': relative(modules / 'audit_character_fbx.py'),
           'material_builder': relative(modules / 'build_npc_material_sample.py'),
           'material_module_dir': relative(modules), 'repository_src': relative(source)}
    animation = {'reader': relative(tools['npc_animation_reader']), 'dotnet': relative(dotnet),
                 **{key: relative(modules / filename) for key, filename in {
                     'catalog_inspector': 'inspect_npc_animation.py', 'exporter': 'export_npc_animation.py',
                     'action_builder': 'build_configured_npc_action.py',
                     'prefab_action_builder': 'build_prefab_npc_action.py'}.items()}}
    document = {'format': 'EndfieldCharacterRuntime/2', 'state_root': 'state', 'tools_receipt': relative(receipt),
                'exporter': relative(modules / 'export_character.py'), 'export_defaults': exports, 'scanner': relative(scanner),
                'weather_reader': relative(tools['weather_reader']),
                'npc_prepare_script': relative(source / 'prepare_independent_npc.py'),
                'authoring_tools': authoring, 'npc_assembly_tools': npc, 'npc_animation_tools': animation}
    destination.write_text(json.dumps(document, indent=2) + '\n', encoding='utf-8')
    return destination


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--tools-receipt', type=Path, required=True)
    parser.add_argument('--dotnet', type=Path, required=True)
    parser.add_argument('--output', type=Path, required=True)
    parser.add_argument('--scene-helper-cache', type=Path, required=True)
    args = parser.parse_args()
    path = configure_character_runtime(args.tools_receipt.resolve(), args.dotnet.resolve(), Path(sys.executable), args.scene_helper_cache.resolve(), args.output.resolve())
    print(json.dumps({'status': 'configured', 'runtime': str(path)}))


if __name__ == '__main__':
    main()
