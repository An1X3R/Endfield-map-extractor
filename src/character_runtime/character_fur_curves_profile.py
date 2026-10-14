"""Build an optional source-derived Fur Curves snow layer inside the normal character build."""
from __future__ import annotations

from dataclasses import asdict, dataclass
import hashlib
import importlib.util
import json
import math
from pathlib import Path
from types import ModuleType
import sys
from typing import Callable

import bpy
import numpy as np

from audit_character_materials import source_geometry_digest
from character_fur_curve_weather import CurveTextureBinding, attach_fur_curve_snow, texture_binding
from character_fur_geometry import (
    FurGeometryParameters, attach_source_fur_skin_deformation, build_source_fur_curves,
    detect_shell_series, interpolate_source_fur_guides, vertex_uvs,
)
from character_hair_fur_snow_profile import HairFurSnowProfile, read_hair_fur_snow_profile
from character_wetfilm_profile import file_digest, verified_file
from character_windows_paths import io_path
from patch_character_hgrp import source_keywords


PROFILE_FORMAT = 'EndfieldFurCurvesBuildProfile/1'
SIDECAR_FORMAT = 'EndfieldCharacterSnowProjection/1'
EXPORT_MAP_FORMAT = 'EndfieldCharacterRendererExportMap/1'


@dataclass(frozen=True, slots=True)
class FurCurvesBuildProfile:
    path: Path
    sha256: str
    mesh_identity: tuple[str, str]
    group_identity: tuple[str, str]
    sidecar: Path
    sidecar_sha256: str
    helpers: Path
    helpers_sha256: str
    uv_layer: str
    source_world_up: tuple[float, float, float]
    radius_meters: float
    root_material_slot: int
    root_stride: int
    artist_length_multiplier: float
    shell_tolerance: float


@dataclass(frozen=True, slots=True)
class FurCurvesPreparation:
    path: Path
    sha256: str
    profiles: tuple[FurCurvesBuildProfile, ...]


def read_fur_curves_preparation(path: Path | None) -> FurCurvesPreparation | None:
    """Read source-derived candidates; final source visibility is applied in Blender."""
    if path is None:
        return None
    source = path.resolve(strict=True)
    raw = _object(json.loads(source.read_text(encoding='utf-8-sig')), 'Fur Curves preparation')
    paths = raw.get('profiles')
    if raw.get('format') != 'EndfieldFurCurvesPreparation/1' or not isinstance(paths, list):
        raise ValueError(f'Unsupported Fur Curves preparation: {source}')
    if raw.get('status') not in ('prepared', 'no_matching_fur'):
        raise ValueError(f'Incomplete Fur Curves preparation: {source}')
    if (raw['status'] == 'prepared') != bool(paths):
        raise ValueError(f'Fur Curves preparation status/profile count differs: {source}')
    profiles = tuple(read_fur_curves_build_profile(source.parent / _string(item, 'profile path')) for item in paths)
    identities = tuple(item.mesh_identity for item in profiles)
    if len(set(identities)) != len(identities):
        raise ValueError(f'Fur Curves preparation repeats a mesh identity: {source}')
    return FurCurvesPreparation(source, file_digest(source), profiles)


def _object(value: object, label: str) -> dict[str, object]:
    if not isinstance(value, dict) or any(not isinstance(key, str) for key in value):
        raise TypeError(f'Expected an object: {label}')
    return value


def _string(value: object, label: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise TypeError(f'Expected a nonempty string: {label}')
    return value


def _number(value: object, label: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(float(value)):
        raise TypeError(f'Expected a finite number: {label}')
    return float(value)


def _path_id(value: object, label: str) -> str:
    if isinstance(value, bool) or not isinstance(value, (str, int)):
        raise TypeError(f'Expected a string or integer PathID: {label}')
    text = str(value)
    try:
        return str(int(text, 10))
    except ValueError as error:
        raise ValueError(f'PathID must be a decimal integer: {label}={text!r}') from error


def _identity(value: object, label: str) -> tuple[str, str]:
    if isinstance(value, dict):
        source_value, path_value = value.get('source'), value.get('path_id')
    elif isinstance(value, list) and len(value) == 2:
        source_value, path_value = value
    else:
        raise TypeError(f'Expected a source/PathID identity: {label}')
    source = _string(source_value, label + '.source')
    path_id = _path_id(path_value, label + '.path_id')
    return source.casefold(), path_id


def read_fur_curves_build_profile(path: Path | None) -> FurCurvesBuildProfile | None:
    """Parse an explicit caller-selected source mesh, shell channel and Curves profile."""
    if path is None:
        return None
    source = path.resolve(strict=True)
    if not source.is_file():
        raise ValueError(f'Fur Curves build profile is not a file: {source}')
    raw = _object(json.loads(source.read_text(encoding='utf-8-sig')), 'Fur Curves build profile')
    if raw.get('format') != PROFILE_FORMAT:
        raise ValueError(f'Unsupported Fur Curves build profile format: {source}')
    mesh_identity = _identity(raw.get('source_mesh_identity'), 'source_mesh_identity')
    group_identity = _identity(raw.get('source_group_identity'), 'source_group_identity')
    sidecar = verified_file(source.parent, raw.get('raw_channel_sidecar'), raw.get('raw_channel_sidecar_sha256'))
    helpers = verified_file(source.parent, raw.get('curve_helpers'), raw.get('curve_helpers_sha256'))
    uv_layer = _string(raw.get('root_uv_layer'), 'root_uv_layer')
    up_raw = raw.get('source_world_up')
    if not isinstance(up_raw, list) or len(up_raw) != 3:
        raise TypeError('source_world_up must be an explicit three-number array')
    up = tuple(_number(value, f'source_world_up[{index}]') for index, value in enumerate(up_raw))
    if abs(math.sqrt(sum(value * value for value in up)) - 1.0) > 1e-6:
        raise ValueError('source_world_up must be normalized')
    radius = _number(raw.get('radius_meters'), 'radius_meters')
    root_slot = raw.get('root_material_slot')
    stride = raw.get('root_stride')
    if isinstance(root_slot, bool) or not isinstance(root_slot, int) or root_slot < 0:
        raise TypeError('root_material_slot must be a nonnegative integer')
    if isinstance(stride, bool) or not isinstance(stride, int) or stride < 1:
        raise TypeError('root_stride must be a positive integer')
    multiplier = _number(raw.get('artist_length_multiplier'), 'artist_length_multiplier')
    tolerance = _number(raw.get('shell_tolerance'), 'shell_tolerance')
    if radius <= 0 or multiplier <= 0 or tolerance <= 0:
        raise ValueError('Fur radius, artist multiplier and shell tolerance must be positive')
    return FurCurvesBuildProfile(
        source, file_digest(source), mesh_identity, group_identity, sidecar, file_digest(sidecar),
        helpers, file_digest(helpers), uv_layer, up, radius, root_slot, stride, multiplier, tolerance,
    )


def _read_sidecar_channel(mesh_row: dict[str, object], semantic: str, sidecar: Path) -> tuple[np.ndarray, dict[str, object]]:
    raw_data = mesh_row.get('data')
    if not isinstance(raw_data, list):
        raise TypeError('Snow projection mesh data must be an array')
    entries = tuple(_object(row, f'{semantic} channel') for row in raw_data
                    if isinstance(row, dict) and row.get('semantic') == semantic)
    if len(entries) != 1:
        raise ValueError(f'Expected exactly one raw shell channel: {semantic}')
    entry = entries[0]
    vertex_count = mesh_row.get('vertex_count')
    components = entry.get('components_per_vertex')
    byte_count = entry.get('bytes')
    if isinstance(vertex_count, bool) or not isinstance(vertex_count, int) or vertex_count <= 0:
        raise TypeError('Snow projection vertex_count must be a positive integer')
    if components != 2 or isinstance(components, bool):
        raise ValueError(f'{semantic} must contain exactly two float32 components per vertex')
    expected_bytes = vertex_count * 2 * 4
    if isinstance(byte_count, bool) or not isinstance(byte_count, int) or byte_count != expected_bytes:
        raise ValueError(f'{semantic} byte count does not match its declared vertex count')
    relative = Path(_string(entry.get('file'), semantic + '.file'))
    channel_path = io_path(sidecar.parent / relative).resolve(strict=True)
    if channel_path.parent != io_path(sidecar.parent).resolve(strict=True):
        raise ValueError(f'Raw {semantic} channel must be beside its sidecar: {channel_path}')
    raw = channel_path.read_bytes()
    digest = hashlib.sha256(raw).hexdigest()
    expected_digest = _string(entry.get('sha256'), semantic + '.sha256').casefold()
    if len(raw) != expected_bytes or digest != expected_digest:
        raise ValueError(f'Raw {semantic} channel size or SHA-256 differs: {channel_path}')
    values = np.frombuffer(raw, dtype='<f4').reshape(vertex_count, 2).copy()
    if not np.isfinite(values).all():
        raise ValueError(f'Raw {semantic} channel contains non-finite values: {channel_path}')
    return values, {'semantic': semantic, 'path': str(channel_path), 'bytes': len(raw), 'sha256': digest}


def _load_helpers(profile: FurCurvesBuildProfile) -> ModuleType:
    name = 'endfield_source_fur_helpers_' + profile.helpers_sha256[:16]
    spec = importlib.util.spec_from_file_location(name, profile.helpers)
    if spec is None or spec.loader is None:
        raise RuntimeError(f'Cannot load configured Curves helper module: {profile.helpers}')
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    for attribute in ('read_mesh_components', 'image_pixel_array', 'sample_image_pixels', 'make_sampled_curve_material'):
        if not callable(getattr(module, attribute, None)):
            raise ValueError(f'Configured Curves helper lacks required function {attribute}: {profile.helpers}')
    return module


def _mapped_fur_mesh(source_dir: Path, identity: tuple[str, str]) -> tuple[bpy.types.Object, dict[str, object]]:
    export_map_path = source_dir / 'renderer_export_map.json'
    document = _object(json.loads(export_map_path.read_text(encoding='utf-8-sig')), 'renderer export map')
    if document.get('format') != EXPORT_MAP_FORMAT or not isinstance(document.get('renderers'), list):
        raise ValueError(f'Unsupported renderer export map: {export_map_path}')
    rows = tuple(_object(row, 'renderer export row') for row in document['renderers']
                 if isinstance(row, dict) and _identity(row.get('mesh_identity'), 'renderer mesh identity') == identity)
    export_names = tuple(sorted({_string(row.get('export_name'), 'renderer export_name') for row in rows}))
    if not rows or len(export_names) != 1:
        raise ValueError(f'Source mesh identity does not map to one imported FBX object: {identity}')
    mesh = bpy.data.objects.get(export_names[0])
    if mesh is None or mesh.type != 'MESH':
        raise ValueError(f'Mapped source mesh is absent from the imported scene: {export_names[0]}')
    return mesh, {'path': str(export_map_path.resolve(strict=True)), 'sha256': file_digest(export_map_path),
                  'export_name': export_names[0], 'source_path': rows[0].get('source_path')}


def _group_tree(material: bpy.types.Material) -> tuple[bpy.types.ShaderNodeTree, ...]:
    if not material.use_nodes or material.node_tree is None:
        return ()
    pending = [material.node_tree]
    seen: set[int] = set()
    result: list[bpy.types.ShaderNodeTree] = []
    while pending:
        tree = pending.pop()
        pointer = tree.as_pointer()
        if pointer in seen:
            continue
        seen.add(pointer)
        if tree.get('source_shader') == 'HGRP/CharacterNPR':
            if '_CHARACTER_FUR' in source_keywords(tree):
                result.append(tree)
        pending.extend(item.node_tree for item in tree.nodes if item.type == 'GROUP' and item.node_tree is not None)
    return tuple(result)


def _source_fur_material(source_mesh: bpy.types.Object, profile: FurCurvesBuildProfile,
                         weather_profile: HairFurSnowProfile) -> tuple[bpy.types.Material, bpy.types.ShaderNodeTree, tuple[str, ...]]:
    pairs: list[tuple[bpy.types.Material, bpy.types.ShaderNodeTree]] = []
    seen: set[tuple[int, int]] = set()
    for slot in source_mesh.material_slots:
        material = slot.material
        if material is None:
            continue
        for group in _group_tree(material):
            key = material.as_pointer(), group.as_pointer()
            if key not in seen:
                seen.add(key)
                pairs.append((material, group))
    groups = {group.as_pointer(): group for _, group in pairs}
    if len(groups) != 1:
        raise ValueError(f'Configured source Mesh must have exactly one Fur shader group; found {len(groups)}: {source_mesh.name}')
    matches = tuple((material, group) for material, group in pairs
                    if _identity(json.loads(group['source_identity']), 'source Fur group identity') == profile.group_identity)
    if len(matches) != 1:
        raise ValueError(f'Configured Fur group identity is absent or ambiguous on source Mesh: {profile.group_identity}')
    material, group = matches[0]
    keywords = tuple(sorted(source_keywords(group)))
    families = tuple(item for item in weather_profile.families if item.family == 'HGRP/CharacterNPR')
    if len(families) != 1 or keywords not in families[0].keyword_sets:
        raise ValueError('Configured source Fur keyword variant is outside the selected Hair/Fur snow profile')
    return material, group, keywords


def _source_scalars(group: bpy.types.ShaderNodeTree) -> tuple[float, float, float, tuple[float, float, float, float]]:
    raw_floats = _object(json.loads(group.get('source_floats', '{}')), 'source Fur floats')
    values = tuple(_number(raw_floats.get(key), key) for key in
                   ('_FurLengthIntensity', '_FurGravityStrength', '_DisableRainEffectOnMaterial'))
    raw_colors = _object(json.loads(group.get('source_colors', '{}')), 'source Fur colors')
    tint_raw = raw_colors.get('_BaseColor')
    if not isinstance(tint_raw, list) or len(tint_raw) != 4:
        raise TypeError('Source Fur _BaseColor must be an explicit four-number array')
    tint = tuple(_number(value, f'_BaseColor[{index}]') for index, value in enumerate(tint_raw))
    return values[0], values[1], values[2], tint


def _static_mapping(binding: CurveTextureBinding, slot: str, uv_layer: str, identity: bool) -> tuple[tuple[float, float], tuple[float, float]]:
    uv_name = binding.uv_layer
    mapping = binding.mapping
    if uv_name != uv_layer:
        raise ValueError(f'Source {slot} uses {uv_name}, expected configured UV {uv_layer}')
    if mapping.vector_type != 'POINT' or any(mapping.inputs[name].is_linked for name in ('Location', 'Rotation', 'Scale')):
        raise ValueError(f'Source {slot} requires a static POINT mapping')
    location = tuple(_number(float(value), f'{slot}.Location') for value in mapping.inputs['Location'].default_value)
    rotation = tuple(_number(float(value), f'{slot}.Rotation') for value in mapping.inputs['Rotation'].default_value)
    scale = tuple(_number(float(value), f'{slot}.Scale') for value in mapping.inputs['Scale'].default_value)
    if identity and (location != (0.0, 0.0, 0.0) or rotation != (0.0, 0.0, 0.0) or scale != (1.0, 1.0, 1.0)):
        raise ValueError(f'Source {slot} mapping is not the identity transform required by the Curves generator')
    if rotation != (0.0, 0.0, 0.0):
        raise ValueError(f'Source {slot} rotation cannot be represented by the Curves generator')
    return (scale[0], scale[1]), (location[0], location[1])


def attach_profile_fur_curves(
    profile: FurCurvesBuildProfile | None,
    source_dir: Path,
    weather_profile_path: Path,
    controls: bpy.types.ShaderNodeTree,
    layout: Callable[[bpy.types.NodeTree], None],
) -> dict[str, object]:
    """Create one opt-in Curves layer from normal-build FBX and source-profile data."""
    if profile is None:
        return {'status': 'not_requested', 'source_meshes_added': 0, 'mesh_fur_retained': True}
    if not source_dir.is_dir():
        raise FileNotFoundError(f'Configured Curves source directory is absent: {source_dir}')
    source_audit_path = source_dir / 'source_audit.json'
    export_map_path = source_dir / 'renderer_export_map.json'
    for required in (source_audit_path, export_map_path):
        if not required.is_file():
            raise FileNotFoundError(f'Configured Curves source map is absent: {required}')
    if file_digest(profile.path) != profile.sha256 or file_digest(profile.sidecar) != profile.sidecar_sha256 or file_digest(profile.helpers) != profile.helpers_sha256:
        raise ValueError('Configured Fur Curves profile, sidecar or helper changed after preflight')
    weather_profile = read_hair_fur_snow_profile(weather_profile_path)
    if weather_profile is None:
        raise ValueError('Curves snow requires an explicit Hair/Fur source snow profile')
    source_audit_sha256 = file_digest(source_audit_path)
    sidecar_document = _object(json.loads(profile.sidecar.read_text(encoding='utf-8-sig')), 'snow projection sidecar')
    if sidecar_document.get('format') != SIDECAR_FORMAT:
        raise ValueError(f'Unsupported snow projection sidecar: {profile.sidecar}')
    if str(sidecar_document.get('source_audit_sha256', '')).casefold() != source_audit_sha256:
        raise ValueError('Snow shell sidecar was produced from a different source audit')
    mesh_rows_raw = sidecar_document.get('meshes')
    if not isinstance(mesh_rows_raw, list):
        raise TypeError('Snow projection meshes must be an array')
    mesh_rows = tuple(_object(row, 'snow projection mesh')
                      for row in mesh_rows_raw
                      if isinstance(row, dict) and _identity(row.get('identity'), 'snow mesh identity') == profile.mesh_identity)
    if len(mesh_rows) != 1:
        raise ValueError(f'Snow sidecar must contain exactly one configured source Mesh: {profile.mesh_identity}')
    mesh_row = mesh_rows[0]
    source_mesh, export_map = _mapped_fur_mesh(source_dir, profile.mesh_identity)
    vertex_count = mesh_row.get('vertex_count')
    if isinstance(vertex_count, bool) or not isinstance(vertex_count, int) or vertex_count != len(source_mesh.data.vertices):
        raise ValueError('Snow sidecar and normal-build FBX Mesh vertex counts differ')
    if source_mesh.hide_render or source_mesh.hide_get():
        raise ValueError(f'Configured source Fur Mesh is not visible for rendering: {source_mesh.name}')
    raw_uv, uv_record = _read_sidecar_channel(mesh_row, 'TEXCOORD0', profile.sidecar)
    raw_layers, layer_record = _read_sidecar_channel(mesh_row, 'TEXCOORD1', profile.sidecar)
    actual_uv = vertex_uvs(source_mesh.data, profile.uv_layer, profile.shell_tolerance)
    if actual_uv.shape != raw_uv.shape or float(np.max(np.abs(actual_uv - raw_uv))) > profile.shell_tolerance:
        raise ValueError('Raw TEXCOORD0 does not match normal-build FBX vertex order and UV layer')
    material, group, keywords = _source_fur_material(source_mesh, profile, weather_profile)
    length, gravity, disable_rain, tint = _source_scalars(group)
    direction = texture_binding(group, '_FurDirMap')
    base = texture_binding(group, '_BaseMap')
    base_scale, base_offset = _static_mapping(base, '_BaseMap', profile.uv_layer, False)
    _static_mapping(direction, '_FurDirMap', profile.uv_layer, True)
    if direction.texture.image is None or direction.texture.image.colorspace_settings.name != 'Non-Color':
        raise ValueError('Source FurDirMap must be the packed linear RGBA geometry texture')
    if base.texture.image is None or base.texture.image.packed_file is None:
        raise ValueError('Source BaseMap must be the packed source appearance texture')
    if not base.texture.image.has_data:
        base.texture.image.pixels[0]
    helpers = _load_helpers(profile)
    series = detect_shell_series(source_mesh.data, raw_layers, profile.uv_layer, profile.shell_tolerance, helpers)
    geometry = source_geometry_digest((source_mesh.name,))
    collection_name = 'EF Optional Source Fur Curves Weather ' + source_mesh.name
    if bpy.data.collections.get(collection_name) is not None:
        raise ValueError(f'Optional Curves collection already exists: {collection_name}')
    collection = bpy.data.collections.new(collection_name)
    bpy.context.scene.collection.children.link(collection)
    parameters = FurGeometryParameters(length, gravity, disable_rain, profile.source_world_up,
                                       profile.radius_meters, profile.root_stride, profile.artist_length_multiplier)
    built = build_source_fur_curves(source_mesh, series, direction.texture.image, base.texture.image,
                                    profile.uv_layer, base_scale, base_offset, tint, parameters, collection, helpers)
    bind_guides = bpy.data.objects[built.object_name]
    interpolation = interpolate_source_fur_guides(source_mesh, bind_guides, profile.root_material_slot, collection)
    curves = bpy.data.objects[interpolation.object_name]
    skin = attach_source_fur_skin_deformation(source_mesh, bind_guides, curves, collection)
    bind_guides.hide_render = True
    bind_guides.hide_set(True)
    bind_guides['fur_curves_role'] = 'Hidden source-derived bind guides for the optional driven Curves layer'
    weather = attach_fur_curve_snow(source_mesh, material, group, bind_guides, curves, controls, weather_profile, layout)
    after = source_geometry_digest((source_mesh.name,))
    if geometry != after:
        raise AssertionError(f'Optional Fur Curves generation changed original Mesh geometry or skin: {source_mesh.name}')
    return {
        'format': 'EndfieldOptionalFurCurvesSnow/1',
        'status': 'connected',
        'source_mesh': source_mesh.name,
        'source_mesh_identity': {'source': profile.mesh_identity[0], 'path_id': profile.mesh_identity[1]},
        'source_group': group.name,
        'source_group_identity': {'source': profile.group_identity[0], 'path_id': profile.group_identity[1]},
        'source_material': material.name,
        'source_keywords': list(keywords),
        'source_vertex_count': vertex_count,
        'root_uv_layer': profile.uv_layer,
        'raw_channels': [uv_record, layer_record],
        'source_audit_sha256': source_audit_sha256,
        'renderer_export_map': export_map,
        'sidecar': {'path': str(profile.sidecar), 'sha256': profile.sidecar_sha256},
        'profile': {'path': str(profile.path), 'sha256': profile.sha256},
        'curve_helpers': {'path': str(profile.helpers), 'sha256': profile.helpers_sha256},
        'source_fur_values': {'length_intensity': length, 'gravity_strength': gravity,
                              'disable_rain_effect_on_material': disable_rain},
        'source_geometry_preserved': True,
        'collection': collection.name,
        'bind_guides': asdict(built),
        'interpolation': asdict(interpolation),
        'skin_binding': asdict(skin),
        'curve_weather': weather,
        'source_curve_approximation': 'Source Fur shell layers are converted to visible editable Blender Curves; original Mesh shell remains visible. The source host expansion, sampling and snow kernel remain explicit approximations, not captured runtime Curves.',
        'selection_mode': 'verified_source_profile',
    }


def verify_profile_fur_curves(receipt: dict[str, object]) -> dict[str, object]:
    """Check saved Curves and their exact weather material after scripts-disabled reopen."""
    if receipt.get('format') == 'EndfieldPreparedFurCurvesSnow/1':
        entries = receipt.get('entries')
        skipped = receipt.get('skipped_hidden_sources')
        if not isinstance(entries, list) or not isinstance(skipped, list):
            raise ValueError('Prepared Curves receipt requires entries and hidden sources')
        if receipt.get('status') != 'completed' or receipt.get('source_mesh_candidates') != len(entries) + len(skipped):
            raise AssertionError('Prepared Curves candidate count or completion status differs')
        identities = tuple(_identity(_object(row, 'Curves candidate').get('source_mesh_identity'), 'candidate identity')
                           for row in entries + skipped)
        if len(set(identities)) != len(identities):
            raise AssertionError('Prepared Curves receipt repeats a source mesh')
        preparation_row = _object(receipt.get('preparation'), 'preparation reference')
        preparation = read_fur_curves_preparation(Path(_string(preparation_row.get('path'), 'preparation.path')))
        if (preparation.sha256 != preparation_row.get('sha256')
                or set(identities) != {profile.mesh_identity for profile in preparation.profiles}):
            raise AssertionError('Prepared Curves receipt differs from its source candidate set')
        checks = [verify_profile_fur_curves(_object(entry, 'Curves entry')) for entry in entries]
        for value in skipped:
            row = _object(value, 'hidden Curves source')
            obj = bpy.data.objects.get(_string(row.get('source_mesh'), 'source_mesh'))
            if obj is None or not (obj.hide_render or obj.hide_get()):
                raise AssertionError('Skipped source Curves candidate is missing or unexpectedly visible')
        return {'status': 'passed', 'entries': checks, 'skipped_hidden_source_count': len(skipped)}
    status = receipt.get('status')
    if status == 'not_requested':
        if any(obj.type == 'CURVES' and obj.get('fur_curve_weather') for obj in bpy.data.objects):
            raise AssertionError('Unrequested Fur Curves snow objects appeared in the saved build')
        return {'status': 'not_requested', 'mesh_fur_retained': True}
    if status != 'connected' or receipt.get('format') != 'EndfieldOptionalFurCurvesSnow/1':
        raise ValueError('Unknown saved optional Fur Curves status')
    curves_record = _object(receipt.get('curve_weather'), 'Curves weather attachment')
    from character_fur_curve_weather import verify_fur_curve_snow
    weather_audit = verify_fur_curve_snow(curves_record)
    source_mesh_name = _string(receipt.get('source_mesh'), 'source_mesh')
    source_mesh = bpy.data.objects.get(source_mesh_name)
    curves_name = _string(curves_record.get('curves'), 'driven Curves')
    guides_name = _string(curves_record.get('bind_guides'), 'bind guides')
    if source_mesh is None or source_mesh.type != 'MESH' or source_mesh.hide_render:
        raise AssertionError('Saved Curves source Mesh is missing or hidden')
    curves = bpy.data.objects.get(curves_name)
    guides = bpy.data.objects.get(guides_name)
    if curves is None or curves.type != 'CURVES' or curves.hide_render or curves.parent != source_mesh:
        raise AssertionError('Saved source-derived Curves are missing or disconnected from their Mesh')
    if guides is None or guides.type != 'CURVES' or not guides.hide_render or guides.parent != source_mesh:
        raise AssertionError('Saved hidden source-derived bind guides are missing or visible')
    collection_name = _string(receipt.get('collection'), 'Curves collection')
    collection = bpy.data.collections.get(collection_name)
    if collection is None or curves.name not in collection.objects or guides.name not in collection.objects:
        raise AssertionError('Saved optional Fur Curves collection membership differs')
    return {'status': 'passed', 'source_mesh': source_mesh.name, 'driven_curves': curves.name,
            'bind_guides_hidden': True, 'source_mesh_retained_visible': True, 'weather': weather_audit}


def attach_prepared_fur_curves(
    preparation: FurCurvesPreparation,
    source_dir: Path,
    weather_profile_path: Path,
    controls: bpy.types.ShaderNodeTree,
    layout: Callable[[bpy.types.NodeTree], None],
) -> dict[str, object]:
    """Build only visible source candidates, retaining explicit hidden-LOD records."""
    if file_digest(preparation.path) != preparation.sha256:
        raise ValueError('Fur Curves preparation changed after preflight')
    entries: list[dict[str, object]] = []
    skipped: list[dict[str, object]] = []
    for profile in preparation.profiles:
        mesh, mapping = _mapped_fur_mesh(source_dir, profile.mesh_identity)
        if mesh.hide_render or mesh.hide_get():
            skipped.append({'source_mesh': mesh.name, 'source_mesh_identity': list(profile.mesh_identity),
                            'reason': 'source_visibility_or_lod_hidden', 'renderer_export_map': mapping})
            continue
        entries.append(attach_profile_fur_curves(profile, source_dir, weather_profile_path, controls, layout))
    return {'format': 'EndfieldPreparedFurCurvesSnow/1', 'status': 'completed',
            'preparation': {'path': str(preparation.path), 'sha256': preparation.sha256},
            'entries': entries, 'skipped_hidden_sources': skipped,
            'source_mesh_candidates': len(preparation.profiles), 'mesh_fur_retained': True}

