"""Export one explicitly selected character through source readers and one Blender build."""
from __future__ import annotations

import argparse
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
import hashlib
import json
import math
import os
from pathlib import Path
import subprocess
import sys
import time
from typing import Literal

from character_geometry_source import geometry_source
from character_material_options import (
    CharacterMaterialOptions, parse_character_material_options,
)


@dataclass(frozen=True)
class ExportRequest:
    repository_src: Path
    prepare_script: Path
    python: Path
    blender: Path
    static_reader: Path
    shader_reader: Path
    visibility_reader: Path
    catalog: Path
    game_root: Path
    database: Path
    cab_inventory: Path
    weather_manifest: Path
    output: Path
    cache_dir: Path | None
    variant: Literal['characters', 'npc']
    character_id: str
    template_id: str
    part_name: str
    resource_path: str
    provider: str
    unity_version: str
    head_bone: str
    face_right: tuple[float, ...]
    face_up: tuple[float, ...]
    face_forward: tuple[float, ...]
    hair_x: tuple[float, ...]
    hair_y: tuple[float, ...]
    hair_z: tuple[float, ...]
    authoring: Path | None
    fur_curves_settings: Path | None
    material_options: CharacterMaterialOptions | None


@dataclass(frozen=True)
class StageTiming:
    stage: str
    seconds: float
    cumulative_seconds: float
    exit_code: int
    disposition: str
    log: str
    command: tuple[str, ...]


def require_object(value: object, context: str) -> dict[str, object]:
    if not isinstance(value, dict) or any(not isinstance(key, str) for key in value):
        raise TypeError(f'Expected a JSON object: {context}')
    return value


def require_text(data: dict[str, object], key: str) -> str:
    value = data[key]
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f'Expected a nonempty string: {key}')
    return value


def require_vector(data: dict[str, object], key: str) -> tuple[float, ...]:
    value = data[key]
    if not isinstance(value, list) or len(value) != 3:
        raise ValueError(f'Expected three components: {key}')
    if any(isinstance(item, bool) or not isinstance(item, (int, float)) for item in value):
        raise TypeError(f'Expected numeric vector components: {key}')
    result = tuple(float(item) for item in value)
    if not all(math.isfinite(item) for item in result):
        raise ValueError(f'Nonfinite vector components: {key}')
    return result


def read_request(path: Path) -> ExportRequest:
    raw = require_object(json.loads(path.read_text(encoding='utf-8-sig')), 'request')
    if raw['format'] != 'EndfieldCharacterExportRequest/2':
        raise ValueError('Unsupported character export request format')

    def source(key: str) -> Path:
        value = Path(require_text(raw, key))
        return (path.parent / value).resolve(strict=True)

    cache_value = raw.get('cache_dir')
    if cache_value is not None and (not isinstance(cache_value, str) or not cache_value.strip()):
        raise ValueError('Expected cache_dir to be a nonempty path when provided')
    cache_dir = (path.parent / cache_value).resolve() if isinstance(cache_value, str) else None
    variant_value = raw.get('variant', 'characters')
    if not isinstance(variant_value, str) or variant_value not in ('characters', 'npc'):
        raise ValueError('Expected variant to be characters or npc')

    request = ExportRequest(
        source('repository_src'), source('prepare_script'), source('python'), source('blender'),
        source('static_reader'), source('shader_reader'), source('visibility_reader'), source('catalog'),
        source('game_root'), source('database'), source('cab_inventory'), source('weather_manifest'),
        (path.parent / require_text(raw, 'output')).resolve(), cache_dir, variant_value,
        *(require_text(raw, key) for key in ('character_id', 'template_id', 'part_name', 'resource_path', 'provider', 'unity_version', 'head_bone')),
        *(require_vector(raw, key) for key in ('face_right', 'face_up', 'face_forward', 'hair_x', 'hair_y', 'hair_z')),
        source('authoring') if raw.get('authoring') is not None else None,
        source('fur_curves_settings') if raw.get('fur_curves_settings') is not None else None,
        parse_character_material_options(raw['material_options']) if 'material_options' in raw else None,
    )
    if request.output.exists():
        raise FileExistsError(request.output)
    if request.output.is_relative_to(request.game_root):
        raise ValueError('Export output must remain outside the read-only game directory')
    if request.authoring is not None:
        authoring = require_object(json.loads(request.authoring.read_text(encoding='utf-8-sig')), 'authoring request')
        if authoring['character_id'] != request.character_id:
            raise ValueError('Authoring request belongs to a different selected character')
    if request.fur_curves_settings is not None and 'fur_curves_settings_sha256' in raw:
        expected = require_text(raw, 'fur_curves_settings_sha256')
        if hashlib.sha256(request.fur_curves_settings.read_bytes()).hexdigest() != expected:
            raise ValueError('Fur Curves settings changed after the character request was queued')
    return request


def require_source_rebuild(sample: Path) -> None:
    """Permit only the documented FBX extra-pass gap, with complete source artifacts."""
    receipt = require_object(json.loads((sample / 'static_probe/receipt.json').read_text(encoding='utf-8')), 'static receipt')
    sources = require_object(json.loads((sample / 'static_probe/source_audit.json').read_text(encoding='utf-8')), 'source audit')
    textures = json.loads((sample / 'static_probe/source_texture_artifacts.json').read_text(encoding='utf-8'))
    if receipt['status'] != 'incomplete_material_coverage' or not isinstance(textures, list):
        raise RuntimeError('Static reader did not produce the documented source-rebuild handoff')
    materials = sources['materials']
    renderers = sources['renderers']
    if not isinstance(materials, list) or not isinstance(renderers, list):
        raise TypeError('Static source materials and renderers must be arrays')
    if len(materials) != receipt['source_materials'] or len(textures) != receipt['source_textures']:
        raise RuntimeError('Static reader failed to retain all source materials or textures')
    if any(require_object(row, 'renderer')['positive_unresolved_bone_influences'] != 0 for row in renderers):
        raise RuntimeError('Static source contains unresolved weighted bones')
    geometry_source(sample / 'static_probe')


def run_stage(name: str, command: tuple[str, ...], logs: Path, environment: dict[str, str], started: float) -> StageTiming:
    log = logs / (name + '.log')
    tick = time.perf_counter()
    with log.open('wb') as stream:
        result = subprocess.run(command, env=environment, stdout=stream, stderr=subprocess.STDOUT, check=False)
    return StageTiming(name, time.perf_counter() - tick, time.perf_counter() - started,
                       result.returncode, 'passed' if result.returncode == 0 else 'failed', str(log), command)


def write_material_options(request: ExportRequest) -> Path | None:
    """Write canonical settings for Blender only when the caller supplied them."""
    if request.material_options is None:
        return None
    path = request.output / 'material_options.json'
    path.write_text(request.material_options.canonical_json() + '\n', encoding='utf-8')
    return path


def build_arguments(request: ExportRequest, sample: Path, target: Path,
                    material_options_path: Path | None) -> tuple[str, ...]:
    """Build the legacy Blender arguments plus an explicit optional settings file."""
    if (request.material_options is None) != (material_options_path is None):
        raise ValueError('Material settings and Blender settings path must be provided together')
    material_profile_root = sample / 'material_profile'
    arguments = ['--fbx', str(geometry_source(sample / 'static_probe')), '--source-dir', str(sample / 'static_probe'),
        '--shader-identities', str(sample / 'shader_identity/shader_identity.json'), '--renderer-visibility', str(sample / 'renderer_visibility.json'),
        '--profile', str(material_profile_root / 'material_profile.json'), '--repository-src', str(request.repository_src),
        '--alpha-audit', str(material_profile_root / 'shadow_alpha_audit.json'),
        '--output-dir', str(target), '--head-bone', request.head_bone]
    for key in ('face_right', 'face_up', 'face_forward', 'hair_x', 'hair_y', 'hair_z'):
        arguments.extend(('--' + key.replace('_', '-'), *(str(value) for value in getattr(request, key))))
    if request.authoring is not None:
        arguments.extend(('--authoring-request', str(request.authoring)))
    if request.fur_curves_settings is not None:
        arguments.extend(('--fur-curves-preparation', str(sample / 'fur_curves/preparation.json')))
    if material_options_path is not None:
        arguments.extend(('--material-options', str(material_options_path)))
    return tuple(arguments)


def main(request_path: Path) -> None:
    request = read_request(request_path)
    request.output.mkdir(parents=True)
    logs = request.output / 'logs'
    logs.mkdir()
    temporary = request.output / 'tmp'
    temporary.mkdir()
    environment = {**os.environ, 'TEMP': str(temporary), 'TMP': str(temporary), 'PYTHONDONTWRITEBYTECODE': '1'}
    sample = request.output / 'prepared'
    target = request.output / 'build'
    module = Path(__file__).resolve().parent
    started_utc = datetime.now(timezone.utc).isoformat()
    started = time.perf_counter()
    stages: list[StageTiming] = []

    def execute(name: str, command: tuple[str, ...]) -> None:
        timing = run_stage(name, command, logs, environment, started)
        stages.append(timing)
        timing_path = request.output / 'stage_timings.json'
        timing_path.write_text(json.dumps([asdict(row) for row in stages], indent=2), encoding='utf-8')
        if timing.exit_code != 0:
            if name != 'source_export' or timing.exit_code != 1 or not (sample / 'static_probe/receipt.json').is_file():
                raise RuntimeError(f'Character export failed: stage={name}, exit={timing.exit_code}, log={timing.log}')
            require_source_rebuild(sample)
            timing = StageTiming(timing.stage, timing.seconds, timing.cumulative_seconds, timing.exit_code,
                                 'source_material_rebuild_required', timing.log, timing.command)
        stages[-1] = timing
        timing_path.write_text(json.dumps([asdict(row) for row in stages], indent=2), encoding='utf-8')
        print(json.dumps({'stage': name, 'seconds': timing.seconds, 'disposition': timing.disposition}), flush=True)

    cache = None
    cache_identity = None
    restored = None
    job_sample = sample
    if request.cache_dir is not None:
        sys.path.insert(0, str(request.repository_src))
        from endfield_character_cache import CharacterDataCache
        from endfield_character_prepared_cache import prepared_identity, restore_prepared, new_prepared_root, publish_prepared
        blocked = (request.game_root, request.database.parent, request.cab_inventory.parent)
        if any(request.cache_dir.is_relative_to(path) for path in blocked):
            raise ValueError('Prepared character cache must remain outside game and read-only indexes')
        cache = CharacterDataCache(request.cache_dir)
        catalog = require_object(json.loads(request.catalog.read_text(encoding='utf-8-sig')), 'source catalog')
        provider_source = require_object(catalog['provider_index_source'], 'provider source')
        dependencies = {Path(require_text(provider_source, 'path')), request.prepare_script, request.python, request.weather_manifest, Path(__file__).resolve()}
        dependencies.update(request.repository_src.glob('endfield_character*.py'))
        dependencies.update((request.repository_src / 'extractor_core').glob('*.py'))
        dependencies.update(module / name for name in ('character_material_profile.py', 'character_material_profile_template.json', 'prepare_character_fur_curves.py', 'character_fur_texture_contract.py'))
        for tool in (request.static_reader, request.shader_reader, request.visibility_reader):
            dependencies.update(path for path in tool.parent.iterdir() if path.is_file())
        weather = require_object(json.loads(request.weather_manifest.read_text(encoding='utf-8-sig')), 'weather manifest')
        texture_rows = weather['textures']
        if not isinstance(texture_rows, list):
            raise TypeError('Weather cache inputs require a texture array')
        for raw_texture in texture_rows:
            texture = require_object(raw_texture, 'weather texture')
            dependencies.add((request.weather_manifest.parent / require_text(texture, 'artifact')).resolve(strict=True))
        if request.fur_curves_settings is not None:
            dependencies.add(request.fur_curves_settings)
            settings = require_object(json.loads(request.fur_curves_settings.read_text(encoding='utf-8-sig')), 'Fur settings')
            fur_tools = require_object(settings['tools'], 'Fur settings tools')
            for name in ('snow_channel_probe', 'curve_helpers'):
                tool_row = require_object(fur_tools[name], 'Fur tool ' + name)
                tool = (request.fur_curves_settings.parent / require_text(tool_row, 'path')).resolve(strict=True)
                dependencies.add(tool)
                if tool.suffix.casefold() in ('.dll', '.exe'):
                    dependencies.update(path for path in tool.parent.iterdir() if path.is_file())
            dependencies.add((request.fur_curves_settings.parent / require_text(fur_tools, 'dotnet')).resolve(strict=True))
        def cache_cancel() -> None:
            return None
        cache_tick = time.perf_counter()
        cache_options = (request.character_id, request.template_id, request.part_name, request.resource_path,
                         request.variant, request.unity_version)
        cache_identity = prepared_identity(cache, request.game_root, request.database, request.cab_inventory,
            request.provider, tuple(sorted(dependencies)), cache_options, cache_cancel)
        restored = restore_prepared(cache, cache_identity, job_sample, cache_cancel)
        cache_log = logs / 'prepared_cache.log'
        cache_log.write_text(json.dumps({'key': cache_identity.key, 'hit': restored is not None,
            'shared_root': str(restored) if restored is not None else None}), encoding='utf-8')
        stages.append(StageTiming('prepared_cache', time.perf_counter() - cache_tick, time.perf_counter() - started,
            0, 'verified_cache_hit' if restored is not None else 'cache_miss', str(cache_log), ()))
        if restored is None:
            sample = new_prepared_root(cache)
        else:
            print(json.dumps({'stage': 'prepared_cache', 'seconds': stages[-1].seconds, 'disposition': 'verified_cache_hit'}), flush=True)
    prepare_command = (str(request.python), '-B', str(request.prepare_script), '--repository-src', str(request.repository_src),
        '--catalog', str(request.catalog), '--game-root', str(request.game_root), '--database', str(request.database),
        '--cab-inventory', str(request.cab_inventory), '--output', str(sample), '--character-id', request.character_id,
        '--template-id', request.template_id, '--part-name', request.part_name, '--resource-path', request.resource_path,
        '--provider', request.provider, '--unity-version', request.unity_version, '--variant', request.variant)
    if request.cache_dir is not None:
        prepare_command += ('--cache-dir', str(request.cache_dir))
    if restored is None:
        execute('prepare_bundles', prepare_command)
        execute('source_export', (str(request.static_reader), '--input', str(sample / 'input.json'), '--output', str(sample / 'static_probe')))
        execute('shader_identity', (str(request.shader_reader), '--input', str(sample / 'input.json'), '--source-audit', str(sample / 'static_probe/source_audit.json'), '--output', str(sample / 'shader_identity')))
        material_profile_root = sample / 'material_profile'
        execute('material_profile', (
            str(request.python), '-B', str(module / 'character_material_profile.py'),
            '--shader-identities', str(sample / 'shader_identity/shader_identity.json'),
            '--weather-manifest', str(request.weather_manifest), '--output-dir', str(material_profile_root),
            '--template', str(module / 'character_material_profile_template.json')))
        execute('renderer_visibility', (str(request.visibility_reader), '--input', str(sample / 'input.json'), '--output', str(sample / 'renderer_visibility.json')))
        if request.fur_curves_settings is not None:
            execute('fur_curves_source_channels', (
                str(request.python), '-B', str(module / 'prepare_character_fur_curves.py'),
                '--settings', str(request.fur_curves_settings), '--source-dir', str(sample / 'static_probe'),
                '--input', str(sample / 'input.json'), '--shader-identities', str(sample / 'shader_identity/shader_identity.json'),
                '--output', str(sample / 'fur_curves')))
        if cache is not None and cache_identity is not None:
            publish_tick = time.perf_counter()
            publish_prepared(cache, cache_identity, sample, job_sample, cache_cancel)
            publish_log = logs / 'prepared_cache_publish.log'
            publish_log.write_text(json.dumps({'key': cache_identity.key, 'shared_root': str(sample)}), encoding='utf-8')
            stages.append(StageTiming('prepared_cache_publish', time.perf_counter() - publish_tick, time.perf_counter() - started,
                0, 'published_and_copied', str(publish_log), ()))
        sample = job_sample
    print(json.dumps({'stage': 'prepared_ready', 'seconds': time.perf_counter() - started, 'disposition': 'private_prepared_copy_complete'}), flush=True)
    material_options_path = write_material_options(request)
    build_args = build_arguments(request, sample, target, material_options_path)
    common = (str(request.blender), '--background', '--factory-startup', '--disable-autoexec', '--python-exit-code', '1', '--python')
    execute('blender_build', common + (str(module / 'build_character_blend.py'), '--', *build_args))
    built_seconds = time.perf_counter() - started
    execute('final_reopen', common + (str(module / 'audit_built_character.py'), '--', '--build-receipt', str(target / 'build_receipt.json'),
        '--repository-src', str(request.repository_src), '--output', str(target / 'reopen_audit.json')))
    completed_seconds = time.perf_counter() - started
    audited = require_object(json.loads((target / 'reopen_audit.json').read_text(encoding='utf-8')), 'reopen audit')
    if audited['status'] != 'passed':
        raise AssertionError('Final reopened character verification did not pass')
    blend = target / 'character.blend'
    digest = hashlib.sha256(blend.read_bytes()).hexdigest()
    report = {'format': 'EndfieldCharacterExport/1', 'status': 'exported_reopened', 'started_utc': started_utc,
        'character_id': request.character_id, 'config': str(request_path), 'config_sha256': hashlib.sha256(request_path.read_bytes()).hexdigest(),
        'blend': str(blend), 'blend_sha256': digest, 'blend_bytes': blend.stat().st_size,
        'blend_available_seconds': built_seconds, 'completed_seconds': completed_seconds, 'stages': [asdict(row) for row in stages],
        'cache_scope': 'Verified immutable prepared character dependencies; private job copies; fresh Blender build and saved-file reopen; OS file cache not flushed.',
        'prepared_cache_hit': restored is not None,
        'validation_scope': 'One final scripts-disabled reopen; regression renders are separate from normal export.'}
    if request.material_options is not None:
        report['material_options'] = request.material_options.as_json()
        report['material_options_sha256'] = hashlib.sha256(request.material_options.canonical_json().encode('utf-8')).hexdigest()
        if cache_identity is not None:
            report['prepared_cache_key'] = cache_identity.key
    (request.output / 'export_receipt.json').write_text(json.dumps(report, indent=2), encoding='utf-8')
    print(json.dumps({'status': report['status'], 'blend': str(blend), 'completed_seconds': completed_seconds}), flush=True)


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--request', required=True, type=Path)
    main(parser.parse_args().request.resolve(strict=True))
