"""Prepare source-bound Fur Curves profiles from one normal character export."""
from __future__ import annotations

import argparse
import hashlib
import json
import math
from pathlib import Path
import subprocess
from typing import cast


SETTINGS_FORMAT = 'EndfieldFurCurvesPreparationSettings/1'
PREPARATION_FORMAT = 'EndfieldFurCurvesPreparation/1'
PROFILE_FORMAT = 'EndfieldFurCurvesBuildProfile/1'
CHANNEL_REQUEST_FORMAT = 'EndfieldCharacterSnowChannelRequest/1'
SIDECAR_FORMAT = 'EndfieldCharacterSnowProjection/1'
EXPORT_MAP_FORMAT = 'EndfieldCharacterRendererExportMap/1'
SHELL_TOLERANCE = 1e-6


def object_value(value: object, label: str) -> dict[str, object]:
    if not isinstance(value, dict) or any(not isinstance(key, str) for key in value):
        raise TypeError(f'Expected JSON object: {label}')
    return cast(dict[str, object], value)


def array_value(value: object, label: str) -> list[object]:
    if not isinstance(value, list):
        raise TypeError(f'Expected JSON array: {label}')
    return value


def text_value(value: object, label: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise TypeError(f'Expected nonempty text: {label}')
    return value.strip()


def integer_value(value: object, label: str, minimum: int) -> int:
    if type(value) is not int or value < minimum:
        raise TypeError(f'Expected integer >= {minimum}: {label}')
    return value


def number_value(value: object, label: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(float(value)):
        raise TypeError(f'Expected finite number: {label}')
    return float(value)


def read_object(path: Path, label: str) -> dict[str, object]:
    try:
        value = json.loads(path.read_text(encoding='utf-8-sig'))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as error:
        raise ValueError(f'Cannot read {label}: path={path}; error={error}') from error
    return object_value(value, label)


def file_hash(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open('rb') as stream:
        while block := stream.read(1024 * 1024):
            digest.update(block)
    return digest.hexdigest()


def identity(value: object, label: str) -> tuple[str, str]:
    row = object_value(value, label)
    source = text_value(row.get('source'), label + '.source')
    raw_path_id = row.get('path_id')
    if isinstance(raw_path_id, bool) or not isinstance(raw_path_id, (int, str)):
        raise TypeError(f'Expected integer PathID: {label}.path_id')
    try:
        path_id = str(int(raw_path_id))
    except ValueError as error:
        raise ValueError(f'PathID must be a decimal integer: {label}.path_id={raw_path_id!r}') from error
    return source, path_id


def identity_json(value: tuple[str, str]) -> dict[str, str]:
    return {'source': value[0], 'path_id': value[1]}


def resolved_pointer(value: object, label: str) -> dict[str, object] | None:
    row = object_value(value, label)
    resolved = row.get('resolved')
    if resolved is None:
        return None
    return object_value(resolved, label + '.resolved')


def settings_paths(settings_path: Path, settings: dict[str, object]) -> tuple[Path, Path, str, str]:
    tools = object_value(settings.get('tools'), 'settings.tools')
    probe_row = object_value(tools.get('snow_channel_probe'), 'settings.tools.snow_channel_probe')
    helper_row = object_value(tools.get('curve_helpers'), 'settings.tools.curve_helpers')

    def verified(row: dict[str, object], label: str) -> tuple[Path, str]:
        value = Path(text_value(row.get('path'), label + '.path'))
        path = (settings_path.parent / value).resolve(strict=True)
        expected = text_value(row.get('sha256'), label + '.sha256').casefold()
        if len(expected) != 64 or any(character not in '0123456789abcdef' for character in expected):
            raise ValueError(f'Expected SHA-256 hex digest: {label}.sha256')
        actual = file_hash(path)
        if actual != expected:
            raise ValueError(f'Configured tool hash changed: label={label}; path={path}; expected={expected}; actual={actual}')
        return path, actual

    probe, probe_hash = verified(probe_row, 'settings.tools.snow_channel_probe')
    helpers, helpers_hash = verified(helper_row, 'settings.tools.curve_helpers')
    return probe, helpers, probe_hash, helpers_hash


def read_variant(settings: dict[str, object]) -> dict[str, object]:
    row = object_value(settings.get('source_vertex_variant'), 'settings.source_vertex_variant')
    shader_name = text_value(row.get('shader_name'), 'source_vertex_variant.shader_name')
    if shader_name != 'HGRP/CharacterNPR':
        raise ValueError(f'Fur Curves source shader is unsupported: {shader_name}')
    return {
        'shader_name': shader_name,
        'sub_shader_index': integer_value(row.get('sub_shader_index'), 'sub_shader_index', 0),
        'pass_index': integer_value(row.get('pass_index'), 'pass_index', 0),
        'vertex_sub_program_index': integer_value(row.get('vertex_sub_program_index'), 'vertex_sub_program_index', 0),
        'vertex_program_blob_index': integer_value(row.get('vertex_program_blob_index'), 'vertex_program_blob_index', 0),
    }


def read_artist_options(settings: dict[str, object]) -> dict[str, object]:
    row = object_value(settings.get('artist_options'), 'settings.artist_options')
    up_raw = array_value(row.get('source_world_up'), 'artist_options.source_world_up')
    if len(up_raw) != 3:
        raise ValueError('artist_options.source_world_up must contain three components')
    up = tuple(number_value(value, f'artist_options.source_world_up[{index}]') for index, value in enumerate(up_raw))
    if abs(math.sqrt(sum(value * value for value in up)) - 1.0) > 1e-6:
        raise ValueError('artist_options.source_world_up must be normalized')
    radius = number_value(row.get('radius_meters'), 'artist_options.radius_meters')
    stride = integer_value(row.get('root_stride'), 'artist_options.root_stride', 1)
    multiplier = number_value(row.get('artist_length_multiplier'), 'artist_options.artist_length_multiplier')
    root_slot = integer_value(row.get('root_material_slot'), 'artist_options.root_material_slot', 0)
    if radius <= 0 or multiplier <= 0:
        raise ValueError('Artist Fur radius and length multiplier must be positive')
    return {'source_world_up': list(up), 'radius_meters': radius, 'root_stride': stride,
            'artist_length_multiplier': multiplier, 'root_material_slot': root_slot}


def shader_name_table(document: dict[str, object]) -> dict[tuple[str, str], str]:
    rows = array_value(document.get('shaders'), 'shader identities.shaders')
    result: dict[tuple[str, str], str] = {}
    for position, raw in enumerate(rows):
        row = object_value(raw, f'shader identities.shaders[{position}]')
        key = identity(row.get('shader'), f'shaders[{position}].shader')
        name = text_value(row.get('parsed_form_name') or row.get('top_level_name'), f'shaders[{position}].name')
        if key in result and result[key] != name:
            raise ValueError(f'Conflicting names for shader identity: {key}')
        result[key] = name
    return result


def material_keyword_set(row: dict[str, object], label: str) -> frozenset[str]:
    if row.get('available') is not True:
        raise ValueError(f'Source material TypeTree is unavailable: {label}')
    data = object_value(row.get('data'), label + '.data')
    keywords = data.get('m_ValidKeywords')
    if keywords is None:
        keywords = data.get('m_ShaderKeywords')
    if isinstance(keywords, str):
        return frozenset(part for part in keywords.split() if part)
    values = array_value(keywords, label + '.keywords')
    return frozenset(text_value(value, label + '.keyword') for value in values)


def source_material_requirements(row: dict[str, object], label: str) -> tuple[int, dict[str, float]]:
    data = object_value(row.get('data'), label + '.data')
    saved = object_value(data.get('m_SavedProperties'), label + '.m_SavedProperties')
    textures = array_value(saved.get('m_TexEnvs'), label + '.m_TexEnvs')
    texture_rows: dict[str, dict[str, object]] = {}
    for position, raw in enumerate(textures):
        item = object_value(raw, f'{label}.m_TexEnvs[{position}]')
        key = text_value(item.get('Key'), f'{label}.texture.Key')
        texture_rows[key] = object_value(item.get('Value'), f'{label}.{key}.Value')

    uv_indices: list[int] = []
    for slot in ('_BaseMap', '_FurDirMap'):
        if slot not in texture_rows:
            raise ValueError(f'Fur source material has no required texture slot: {label}/{slot}')
        value = texture_rows[slot]
        texture = object_value(value.get('m_Texture'), f'{label}.{slot}.m_Texture')
        if isinstance(texture.get('m_PathID'), bool) or not isinstance(texture.get('m_PathID'), int) or texture['m_PathID'] == 0:
            raise ValueError(f'Fur source texture is unresolved: {label}/{slot}')
        uv_indices.append(integer_value(value.get('m_UVSetIndex'), f'{label}/{slot}.m_UVSetIndex', 0))
    if uv_indices[0] != uv_indices[1] or uv_indices[0] > 7:
        raise ValueError(f'Fur and Base texture UV sets differ or exceed exported UV0-UV7: {label}; uv_sets={uv_indices}')

    floats = array_value(saved.get('m_Floats'), label + '.m_Floats')
    float_values: dict[str, float] = {}
    for position, raw in enumerate(floats):
        item = object_value(raw, f'{label}.m_Floats[{position}]')
        key = text_value(item.get('Key'), f'{label}.m_Floats.Key')
        value = item.get('Value')
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            continue
        float_values[key] = number_value(value, f'{label}.{key}')
    required_floats = ('_FurLengthIntensity', '_FurGravityStrength', '_DisableRainEffectOnMaterial')
    if any(key not in float_values for key in required_floats):
        raise ValueError(f'Fur source material lacks source scalar values: {label}')
    if (float_values['_FurLengthIntensity'] <= 0
            or not 0 <= float_values['_FurGravityStrength'] <= 1
            or not 0 <= float_values['_DisableRainEffectOnMaterial'] <= 1):
        raise ValueError(f'Fur source material has unsupported scalar values: {label}')
    return uv_indices[0], {key: float_values[key] for key in required_floats}


def visibility_table(path: Path, actual_root: tuple[str, str]) -> tuple[dict[tuple[str, str], dict[str, object]], dict[str, object] | None]:
    if not path.is_file():
        return {}, None
    document = read_object(path, 'renderer visibility')
    if document.get('format') != 'EndfieldCharacterRendererVisibility/1':
        raise ValueError(f'Unsupported renderer visibility report: {path}')
    if identity(document.get('actual_root'), 'renderer visibility.actual_root') != actual_root:
        raise ValueError(f'Renderer visibility belongs to another selected root: {path}')
    table: dict[tuple[str, str], dict[str, object]] = {}
    for position, raw in enumerate(array_value(document.get('renderers'), 'renderer visibility.renderers')):
        row = object_value(raw, f'renderer visibility.renderers[{position}]')
        key = identity(row.get('renderer'), f'renderer visibility.renderers[{position}].renderer')
        table[key] = row
    return table, {'path': str(path), 'sha256': file_hash(path)}


def export_name_table(source_dir: Path) -> dict[tuple[str, str], frozenset[str]]:
    path = source_dir / 'renderer_export_map.json'
    document = read_object(path, 'renderer export map')
    if document.get('format') != EXPORT_MAP_FORMAT:
        raise ValueError(f'Unsupported renderer export map: {path}')
    result: dict[tuple[str, str], set[str]] = {}
    for position, raw in enumerate(array_value(document.get('renderers'), 'renderer export map.renderers')):
        row = object_value(raw, f'renderer export map.renderers[{position}]')
        mesh = identity(row.get('mesh_identity'), f'renderer export map.renderers[{position}].mesh_identity')
        name = text_value(row.get('export_name'), f'renderer export map.renderers[{position}].export_name')
        result.setdefault(mesh, set()).add(name)
    return {key: frozenset(names) for key, names in result.items()}


def required_channel(sidecar: dict[str, object], sidecar_path: Path, mesh_id: tuple[str, str], semantic: str) -> bool:
    matches = tuple(object_value(raw, 'sidecar mesh') for raw in array_value(sidecar.get('meshes'), 'sidecar.meshes')
                    if identity(object_value(raw, 'sidecar mesh').get('identity'), 'sidecar mesh.identity') == mesh_id)
    if len(matches) != 1:
        raise ValueError(f'Channel sidecar must contain one selected mesh: mesh={mesh_id}; matches={len(matches)}')
    mesh = matches[0]
    data_rows = tuple(object_value(raw, f'{semantic} record') for raw in array_value(mesh.get('data'), 'sidecar mesh.data')
                      if object_value(raw, f'{semantic} record').get('semantic') == semantic)
    if len(data_rows) != 1:
        return False
    record = data_rows[0]
    count = integer_value(mesh.get('vertex_count'), 'sidecar mesh.vertex_count', 1)
    if integer_value(record.get('components_per_vertex'), f'{semantic}.components_per_vertex', 1) != 2:
        return False
    expected_bytes = count * 2 * 4
    if integer_value(record.get('bytes'), f'{semantic}.bytes', 0) != expected_bytes:
        return False
    relative = Path(text_value(record.get('file'), f'{semantic}.file'))
    channel = (sidecar_path.parent / relative).resolve(strict=True)
    if channel.parent != sidecar_path.parent.resolve(strict=True):
        raise ValueError(f'Raw mesh channel escaped the sidecar directory: {channel}')
    payload = channel.read_bytes()
    expected_hash = text_value(record.get('sha256'), f'{semantic}.sha256').casefold()
    if len(payload) != expected_bytes or hashlib.sha256(payload).hexdigest() != expected_hash:
        raise ValueError(f'Raw mesh channel size or hash differs: {channel}')
    return True


def visibility_summary(row: dict[str, object] | None) -> dict[str, object]:
    if row is None:
        return {'status': 'not_available; candidate retained for Blender visibility filtering'}
    enabled = row.get('enabled')
    active = row.get('active_self')
    shadows_only = row.get('is_shadows_only')
    if type(enabled) is not bool or type(active) is not bool or type(shadows_only) is not bool:
        raise ValueError('Renderer visibility facts require enabled, active_self and is_shadows_only booleans')
    if not enabled:
        state = 'renderer_disabled'
    elif not active:
        state = 'inactive_game_object'
    elif shadows_only:
        state = 'shadows_only'
    else:
        state = 'source_enabled_and_not_shadows_only'
    return {'status': state, 'enabled': enabled, 'active_self': active, 'is_shadows_only': shadows_only,
            'path': row.get('path')}


def prepare(source_dir: Path, input_path: Path, shader_identity_path: Path, settings_path: Path,
            output: Path) -> dict[str, object]:
    settings = read_object(settings_path, 'preparation settings')
    if settings.get('format') != SETTINGS_FORMAT:
        raise ValueError(f'Unsupported Curves preparation settings: {settings_path}')
    probe, helpers, probe_hash, helper_hash = settings_paths(settings_path, settings)
    preparation_helper = Path(__file__).resolve(strict=True)
    preparation_helper_hash = file_hash(preparation_helper)
    variant = read_variant(settings)
    artist = read_artist_options(settings)
    if output.exists():
        raise FileExistsError(f'Curves preparation output must be new: {output}')
    source_dir = source_dir.resolve(strict=True)
    input_path = input_path.resolve(strict=True)
    shader_identity_path = shader_identity_path.resolve(strict=True)
    source_audit_path = source_dir / 'source_audit.json'
    material_sources_path = source_dir / 'material_sources.json'
    renderer_map_path = source_dir / 'renderer_export_map.json'
    for path in (source_audit_path, material_sources_path, renderer_map_path):
        if not path.is_file():
            raise FileNotFoundError(f'Normal character export artifact is missing: {path}')
    output.mkdir(parents=True)
    requests_dir = output / 'requests'
    sidecars_dir = output / 'sidecars'
    profiles_dir = output / 'profiles'
    for directory in (requests_dir, sidecars_dir, profiles_dir):
        directory.mkdir()

    input_doc = read_object(input_path, 'character input manifest')
    if input_doc.get('format') != 'EndfieldSingleCharacterInput/1':
        raise ValueError(f'Unsupported character input manifest: {input_path}')
    input_root = object_value(input_doc.get('root'), 'input.root')
    audit = read_object(source_audit_path, 'source audit')
    if audit.get('format') != 'EndfieldCharacterSourceAudit/1':
        raise ValueError(f'Unsupported source audit: {source_audit_path}')
    requested_root = object_value(audit.get('requested_root'), 'source audit.requested_root')
    if (text_value(input_root.get('source'), 'input.root.source').casefold()
            != text_value(requested_root.get('source'), 'source audit.requested_root.source').casefold()
            or text_value(input_root.get('resource_path'), 'input.root.resource_path').replace('\\', '/').casefold()
            != text_value(requested_root.get('resource_path'), 'source audit.requested_root.resource_path').replace('\\', '/').casefold()):
        raise ValueError('Character input manifest and source audit select different roots')
    audit_hash = file_hash(source_audit_path)
    shader_doc = read_object(shader_identity_path, 'shader identities')
    if (text_value(shader_doc.get('source_audit_sha256'), 'shader_identity.source_audit_sha256').casefold()
            != audit_hash):
        raise ValueError('Shader identities were generated from a different source audit')
    shader_names = shader_name_table(shader_doc)
    material_sources_doc = array_value(json.loads(material_sources_path.read_text(encoding='utf-8-sig')),
                                       'material sources')
    material_sources: dict[tuple[str, str], dict[str, object]] = {}
    for position, raw in enumerate(material_sources_doc):
        row = object_value(raw, f'material sources[{position}]')
        material_sources[identity(row.get('object'), f'material sources[{position}].object')] = row
    fur_materials: dict[tuple[str, str], dict[str, object]] = {}
    for position, raw in enumerate(array_value(audit.get('materials'), 'source audit.materials')):
        row = object_value(raw, f'source audit.materials[{position}]')
        material_id = identity(row.get('object'), f'source audit.materials[{position}].object')
        shader_ref = resolved_pointer(row.get('shader'), f'source audit.materials[{position}].shader')
        if shader_ref is None:
            continue
        shader_id = identity(shader_ref, f'source audit.materials[{position}].shader.resolved')
        shader_name = shader_names.get(shader_id)
        if shader_name != variant['shader_name']:
            continue
        source_row = material_sources.get(material_id)
        if source_row is None:
            raise ValueError(f'Fur-capable HGRP material has no TypeTree source row: {material_id}')
        keywords = material_keyword_set(source_row, f'material {material_id}')
        if '_CHARACTER_FUR' not in keywords:
            continue
        fur_materials[material_id] = {
            'identity': material_id, 'shader_identity': shader_id,
            'material_name': object_value(source_row.get('object'), 'material source object').get('name'),
            'keywords': sorted(keywords), 'source_row': source_row,
        }

    visibility_path = source_dir.parent / 'renderer_visibility.json'
    actual_root = identity(audit.get('actual_root'), 'source audit.actual_root')
    visibility, visibility_record = visibility_table(visibility_path, actual_root)
    export_names = export_name_table(source_dir)
    candidates_by_mesh: dict[tuple[str, str], dict[str, object]] = {}
    for position, raw in enumerate(array_value(audit.get('renderers'), 'source audit.renderers')):
        renderer = object_value(raw, f'source audit.renderers[{position}]')
        renderer_id = identity(renderer.get('object'), f'source audit.renderers[{position}].object')
        mesh_id = identity(renderer.get('mesh'), f'source audit.renderers[{position}].mesh')
        matched_materials: list[dict[str, object]] = []
        for slot, raw_pointer in enumerate(array_value(renderer.get('materials'), f'renderer {renderer_id}.materials')):
            resolved = resolved_pointer(raw_pointer, f'renderer {renderer_id}.materials[{slot}]')
            if resolved is None:
                continue
            material_id = identity(resolved, f'renderer {renderer_id}.materials[{slot}].resolved')
            material = fur_materials.get(material_id)
            if material is not None:
                matched_materials.append({**material, 'slot_index': slot})
        if not matched_materials:
            continue
        group_ids = {cast(tuple[str, str], row['identity']) for row in matched_materials}
        if len(group_ids) != 1:
            raise ValueError(f'One source Fur Mesh has multiple Fur material groups: renderer={renderer_id}; materials={sorted(group_ids)}')
        if mesh_id not in export_names:
            raise ValueError(f'Fur Mesh is absent from renderer_export_map: renderer={renderer_id}; mesh={mesh_id}')
        mesh_export_names = export_names[mesh_id]
        if len(mesh_export_names) != 1:
            raise ValueError(f'Fur Mesh has multiple imported names in renderer_export_map: renderer={renderer_id}; mesh={mesh_id}; names={sorted(mesh_export_names)}')
        root_material_slot = cast(int, artist['root_material_slot'])
        material_count = len(array_value(renderer.get('materials'), f'renderer {renderer_id}.materials'))
        if root_material_slot >= material_count:
            raise ValueError(f'Configured root material slot is outside selected Fur renderer: renderer={renderer_id}; slot={root_material_slot}; material_count={material_count}')
        material = matched_materials[0]
        source_row = cast(dict[str, object], material['source_row'])
        uv_index, source_floats = source_material_requirements(source_row, f'Fur material {material["identity"]}')
        shader_id = cast(tuple[str, str], material['shader_identity'])
        candidate = candidates_by_mesh.get(mesh_id)
        row = {
            'source_renderer_identity': identity_json(renderer_id),
            'source_group_identity': identity_json(cast(tuple[str, str], material['identity'])),
            'source_group_name': material['material_name'],
            'source_shader_identity': identity_json(shader_id),
            'source_shader_name': variant['shader_name'],
            'keywords': material['keywords'], 'root_uv_layer': f'UV{uv_index}',
            'source_values': source_floats,
            'visibility': visibility_summary(visibility.get(renderer_id)),
        }
        if candidate is None:
            candidates_by_mesh[mesh_id] = {
                'source_mesh_identity': identity_json(mesh_id), 'source_mesh_name': object_value(
                    object_value(renderer.get('mesh'), 'renderer.mesh'), 'renderer.mesh').get('name'),
                'export_name': next(iter(mesh_export_names)), 'shader_identity': identity_json(shader_id),
                'group_identity': identity_json(cast(tuple[str, str], material['identity'])),
                'group_name': material['material_name'], 'keywords': material['keywords'],
                'root_uv_layer': f'UV{uv_index}', 'source_values': source_floats,
                'renderer_rows': [row], 'renderer_identity': renderer_id,
            }
        else:
            if (candidate['group_identity'] != identity_json(cast(tuple[str, str], material['identity']))
                    or candidate['shader_identity'] != identity_json(shader_id)):
                raise ValueError(f'Shared source Mesh resolves to conflicting Fur materials or shaders: mesh={mesh_id}')
            cast(list[dict[str, object]], candidate['renderer_rows']).append(row)

    if not candidates_by_mesh:
        receipt = {
            'format': PREPARATION_FORMAT, 'status': 'no_matching_fur',
            'source_dir': str(source_dir), 'source_audit_sha256': audit_hash,
            'inputs': {'input_manifest': {'path': str(input_path), 'sha256': file_hash(input_path)},
                       'material_sources': {'path': str(material_sources_path), 'sha256': file_hash(material_sources_path)},
                       'shader_identities': {'path': str(shader_identity_path), 'sha256': file_hash(shader_identity_path)},
                       'settings': {'path': str(settings_path.resolve(strict=True)), 'sha256': file_hash(settings_path)},
                       'preparation_helper': {'path': str(preparation_helper), 'sha256': preparation_helper_hash}},
            'candidate_count': 0, 'profiles': [], 'candidates': [], 'probe_runs': [],
            'message': 'No selected renderer material has HGRP/CharacterNPR with _CHARACTER_FUR.'}
        (output / 'preparation.json').write_text(json.dumps(receipt, indent=2), encoding='utf-8')
        return receipt

    groups: dict[tuple[str, str], list[dict[str, object]]] = {}
    for candidate in candidates_by_mesh.values():
        shader_id = identity(candidate['shader_identity'], 'candidate.shader_identity')
        groups.setdefault(shader_id, []).append(candidate)
    probe_runs: list[dict[str, object]] = []
    profiles: list[str] = []
    candidate_records: list[dict[str, object]] = []
    for shader_id, candidates in sorted(groups.items()):
        candidates.sort(key=lambda item: identity(item['source_mesh_identity'], 'candidate.source_mesh_identity'))
        token = hashlib.sha256(json.dumps({'shader': shader_id,
            'meshes': [item['source_mesh_identity'] for item in candidates]}, sort_keys=True).encode()).hexdigest()[:16]
        request_path = requests_dir / f'{token}.json'
        sidecar_output = sidecars_dir / token
        request_meshes = []
        for candidate in candidates:
            renderer_id = cast(tuple[str, str], candidate['renderer_identity'])
            request_meshes.append({'renderer': identity_json(renderer_id),
                                   'mesh': candidate['source_mesh_identity']})
        request = {
            'format': CHANNEL_REQUEST_FORMAT, 'source_audit_sha256': audit_hash,
            'shader': identity_json(shader_id), 'sub_shader_index': variant['sub_shader_index'],
            'pass_index': variant['pass_index'],
            'vertex_sub_program_index': variant['vertex_sub_program_index'],
            'vertex_program_blob_index': variant['vertex_program_blob_index'],
            'meshes': request_meshes,
        }
        request_path.write_text(json.dumps(request, indent=2), encoding='utf-8')
        command = [str(probe), '--input', str(input_path), '--source-audit', str(source_audit_path),
                   '--request', str(request_path), '--output', str(sidecar_output)]
        if probe.suffix.casefold() == '.dll':
            tools = object_value(settings.get('tools'), 'settings.tools')
            dotnet = (settings_path.parent / text_value(tools.get('dotnet'), 'settings.tools.dotnet')).resolve(strict=True)
            command.insert(0, str(dotnet))
        result = subprocess.run(command, cwd=probe.parent, capture_output=True, text=True, check=False)
        if result.returncode != 0:
            raise RuntimeError(f'Snow channel probe failed: exit={result.returncode}; command={command}; stdout={result.stdout}; stderr={result.stderr}')
        sidecar_path = sidecar_output / 'snow_projection_sidecar.json'
        sidecar = read_object(sidecar_path, 'fresh Snow channel sidecar')
        if sidecar.get('format') != SIDECAR_FORMAT:
            raise ValueError(f'Unsupported Snow channel sidecar: {sidecar_path}')
        if (text_value(sidecar.get('source_audit_sha256'), 'sidecar.source_audit_sha256').casefold() != audit_hash
                or text_value(sidecar.get('input_manifest_sha256'), 'sidecar.input_manifest_sha256').casefold() != file_hash(input_path)
                or text_value(sidecar.get('request_sha256'), 'sidecar.request_sha256').casefold() != file_hash(request_path)):
            raise ValueError(f'Fresh sidecar provenance does not match its exact inputs: {sidecar_path}')
        binding = object_value(sidecar.get('shader_binding'), 'sidecar.shader_binding')
        actual_blob = binding.get('actual_blob_at_expected_index')
        if actual_blob is not None and actual_blob != variant['vertex_program_blob_index']:
            raise ValueError(f'Snow reader found a different vertex blob at the configured index: expected={variant["vertex_program_blob_index"]}; actual={actual_blob}')
        probe_run = {
            'request': str(request_path), 'request_sha256': file_hash(request_path),
            'sidecar': str(sidecar_path), 'sidecar_sha256': file_hash(sidecar_path),
            'shader_binding_status': binding.get('binding_status'),
            'expected_vertex_sub_program_index': variant['vertex_sub_program_index'],
            'expected_vertex_program_blob_index': variant['vertex_program_blob_index'],
        }
        probe_runs.append(probe_run)
        for candidate in candidates:
            mesh_id = identity(candidate['source_mesh_identity'], 'candidate.source_mesh_identity')
            has_uv0 = required_channel(sidecar, sidecar_path, mesh_id, 'TEXCOORD0')
            has_uv1 = required_channel(sidecar, sidecar_path, mesh_id, 'TEXCOORD1')
            candidate_receipt = {
                'source_mesh_identity': candidate['source_mesh_identity'],
                'source_mesh_name': candidate['source_mesh_name'], 'export_name': candidate['export_name'],
                'source_group_identity': candidate['group_identity'], 'source_group_name': candidate['group_name'],
                'shader_identity': candidate['shader_identity'], 'shader_name': variant['shader_name'],
                'keywords': candidate['keywords'], 'source_material_values': candidate['source_values'],
                'root_uv_layer': candidate['root_uv_layer'],
                'raw_texcoord0_available': has_uv0, 'raw_texcoord1_available': has_uv1,
                'renderer_facts': candidate['renderer_rows'], 'sidecar': str(sidecar_path),
                'sidecar_sha256': file_hash(sidecar_path), 'shader_binding_status': binding.get('binding_status'),
            }
            if not has_uv0 or not has_uv1:
                candidate_receipt['status'] = 'skipped_missing_required_raw_uv_channel'
                candidate_records.append(candidate_receipt)
                continue
            profile_path = profiles_dir / f'{hashlib.sha256((mesh_id[0] + ":" + mesh_id[1]).encode()).hexdigest()[:16]}.json'
            profile = {
                'format': PROFILE_FORMAT,
                'source_mesh_identity': candidate['source_mesh_identity'],
                'source_group_identity': candidate['group_identity'],
                'raw_channel_sidecar': str(sidecar_path),
                'raw_channel_sidecar_sha256': file_hash(sidecar_path),
                'curve_helpers': str(helpers), 'curve_helpers_sha256': helper_hash,
                'root_uv_layer': candidate['root_uv_layer'],
                'source_world_up': artist['source_world_up'],
                'radius_meters': artist['radius_meters'],
                'root_material_slot': artist['root_material_slot'],
                'root_stride': artist['root_stride'],
                'artist_length_multiplier': artist['artist_length_multiplier'],
                'shell_tolerance': SHELL_TOLERANCE,
            }
            profile_path.write_text(json.dumps(profile, indent=2), encoding='utf-8')
            profiles.append(str(profile_path.resolve()))
            candidate_receipt['status'] = 'prepared'
            candidate_receipt['profile'] = str(profile_path.resolve())
            candidate_receipt['profile_sha256'] = file_hash(profile_path)
            candidate_records.append(candidate_receipt)

    receipt = {
        'format': PREPARATION_FORMAT, 'status': 'prepared',
        'source_dir': str(source_dir),
        'inputs': {
            'input_manifest': {'path': str(input_path), 'sha256': file_hash(input_path)},
            'source_audit': {'path': str(source_audit_path), 'sha256': audit_hash},
            'material_sources': {'path': str(material_sources_path), 'sha256': file_hash(material_sources_path)},
            'renderer_export_map': {'path': str(renderer_map_path), 'sha256': file_hash(renderer_map_path)},
            'shader_identities': {'path': str(shader_identity_path), 'sha256': file_hash(shader_identity_path)},
            'renderer_visibility': visibility_record,
            'settings': {'path': str(settings_path.resolve(strict=True)), 'sha256': file_hash(settings_path)},
            'preparation_helper': {'path': str(preparation_helper), 'sha256': preparation_helper_hash},
            'snow_channel_probe': {'path': str(probe), 'sha256': probe_hash},
            'curve_helpers': {'path': str(helpers), 'sha256': helper_hash},
        },
        'source_vertex_variant': variant,
        'artist_options': artist,
        'candidate_count': len(candidates_by_mesh), 'profiles': profiles,
        'candidates': candidate_records, 'probe_runs': probe_runs,
        'visibility_policy': 'Candidates are retained for the Blender builder to filter against the imported object visibility; available source visibility facts are attached as evidence.',
        'shader_binding_limit': 'If the reader reports that serialized vertex subprogram records are unavailable, the requested index/blob is not treated as verified variant admission.',
    }
    (output / 'preparation.json').write_text(json.dumps(receipt, indent=2), encoding='utf-8')
    return receipt


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--settings', required=True, type=Path)
    parser.add_argument('--source-dir', required=True, type=Path)
    parser.add_argument('--input', required=True, type=Path)
    parser.add_argument('--shader-identities', required=True, type=Path)
    parser.add_argument('--output', required=True, type=Path)
    args = parser.parse_args()
    receipt = prepare(args.source_dir, args.input, args.shader_identities, args.settings, args.output)
    print(json.dumps({'format': receipt['format'], 'status': receipt['status'],
                      'candidate_count': receipt['candidate_count'], 'profile_count': len(receipt['profiles']),
                      'preparation': str((args.output.resolve() / 'preparation.json'))}, separators=(',', ':')))


if __name__ == '__main__':
    main()
