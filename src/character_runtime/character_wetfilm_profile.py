"""Attach source Standard wet-film equations with explicit editable preview state."""
from __future__ import annotations

import hashlib
import json
import math
from pathlib import Path
import struct
from typing import Callable

import bpy

from attach_character_weather_surface import WeatherProjection, bind_standard_wet_surface
from character_weather_nodes import WeatherOverride, WeatherUniforms, make_native_standard_wet_film, make_weather_controls
from character_weather_per_draw import decode_weather_per_draw_bits
from character_material_profile import profile_shader_identity
from patch_character_hgrp import source_keywords
from patch_native_character_weather import rest_attributes
from review_character_hgrp import visible_source_group_names
from character_windows_paths import io_path


DRY_WEATHER_SOURCE = 'explicit_dry_artist_values'
NATIVE_WEATHER_SOURCE = 'explicit_native_per_draw_word'
DRY_WETFILM_STATUS = 'standard_wetfilm_connected_dry_default'
NATIVE_WETFILM_STATUS = 'standard_wetfilm_connected_native_per_draw'


def file_digest(path: Path) -> str:
    with io_path(path).open('rb') as stream:
        return hashlib.file_digest(stream, 'sha256').hexdigest()


def verified_file(parent: Path, value: object, digest: object) -> Path:
    if not isinstance(value, str) or not value or not isinstance(digest, str):
        raise ValueError('Wet-film source requires a configured path and SHA-256')
    path = io_path(parent / value).resolve(strict=True)
    if file_digest(path) != digest.casefold():
        raise ValueError(f'Wet-film source SHA-256 mismatch: {path}')
    return path


def weather_control_source_record(controls: bpy.types.ShaderNodeTree) -> dict[str, object]:
    """Read persistent source identity from the shared editable weather controls."""
    source_kind = controls.get('source_input_kind', DRY_WEATHER_SOURCE)
    if source_kind == DRY_WEATHER_SOURCE:
        return {'kind': DRY_WEATHER_SOURCE, 'runtime_capture_verified': False}
    if source_kind != NATIVE_WEATHER_SOURCE:
        raise ValueError(f'Unsupported Standard weather-control source kind: {controls.name}/{source_kind!r}')
    input_hash = controls.get('source_per_draw_input_sha256')
    word_bits_hex = controls.get('source_per_draw_packed_word_bits_hex')
    height_bits_hex = controls.get('source_per_draw_height_float32_bits_hex')
    provenance = controls.get('source_provenance')
    if not isinstance(input_hash, str) or len(input_hash) != 64 or any(character not in '0123456789abcdef' for character in input_hash):
        raise ValueError(f'Native weather controls lack the validated input SHA-256: {controls.name}')
    if not isinstance(word_bits_hex, str) or len(word_bits_hex) != 10 or not word_bits_hex.startswith('0x'):
        raise ValueError(f'Native weather controls lack raw uint32 metadata: {controls.name}')
    if not isinstance(height_bits_hex, str) or len(height_bits_hex) != 10 or not height_bits_hex.startswith('0x'):
        raise ValueError(f'Native weather controls lack float32 height metadata: {controls.name}')
    if any(character not in '0123456789abcdef' for character in word_bits_hex[2:].casefold()):
        raise ValueError(f'Native weather uint32 metadata is malformed: {controls.name}')
    if any(character not in '0123456789abcdef' for character in height_bits_hex[2:].casefold()):
        raise ValueError(f'Native weather float32 metadata is malformed: {controls.name}')
    if not isinstance(provenance, str) or not provenance.strip():
        raise ValueError(f'Native weather controls lack explicit provenance: {controls.name}')
    return {'kind': NATIVE_WEATHER_SOURCE, 'input_sha256': input_hash,
            'packed_word_bits_hex': word_bits_hex, 'height_float32_bits_hex': height_bits_hex,
            'provenance': provenance, 'runtime_capture_verified': False}


def verify_weather_controls(
    controls: bpy.types.ShaderNodeTree,
    expected_record: dict[str, object],
) -> None:
    """Check persisted node values against the receipt's dry or raw per-draw source."""
    actual_record = weather_control_source_record(controls)
    if actual_record != expected_record:
        raise AssertionError(f'Saved Standard weather source identity differs: {controls.name}')
    source_kind = actual_record['kind']
    if source_kind == DRY_WEATHER_SOURCE:
        expected = {'User Override Weight': 1.0, 'Source Water Height': 0.0,
                    'Source Preview Enable': 0.0, 'Source Preview Water Height': 0.0,
                    **{'Source Effect ' + axis: 0.0 for axis in 'XYZW'},
                    **{'User Effect ' + axis: 0.0 for axis in 'XYZW'}}
    else:
        word = int(str(actual_record['packed_word_bits_hex']), 16)
        channels = decode_weather_per_draw_bits(word).normalized()
        height_bits = int(str(actual_record['height_float32_bits_hex']), 16)
        height = struct.unpack('<f', struct.pack('<I', height_bits))[0]
        expected = {'User Override Weight': 0.0, 'Source Water Height': height,
                    'Source Preview Enable': 0.0, 'Source Preview Water Height': 0.0,
                    **{'Source Effect ' + axis: value for axis, value in zip('XYZW', channels)},
                    **{'User Effect ' + axis: 0.0 for axis in 'XYZW'}}
    for label, value in expected.items():
        controls_found = tuple(node for node in controls.nodes if node.type == 'VALUE' and node.label == label)
        if len(controls_found) != 1 or not math.isclose(
            float(controls_found[0].outputs[0].default_value), value, rel_tol=0.0, abs_tol=1e-7,
        ):
            raise AssertionError(f'Saved Standard weather control differs: {controls.name}/{label}; expected={value}')


def attach_profile_wetfilm(
    profile: Path,
    layout: Callable[[bpy.types.NodeTree], None],
    weather_controls: bpy.types.ShaderNodeTree | None,
) -> dict[str, object]:
    """Keep zero weather neutral; source runtime uniforms remain explicit artist inputs."""
    document = json.loads(io_path(profile).read_text(encoding='utf-8'))
    if 'standard_wet_film' not in document:
        return {'status': 'not_configured', 'groups': []}
    config = document['standard_wet_film']
    if not isinstance(config, dict) or config.get('format') != 'EndfieldStandardWetFilmProfile/1':
        raise ValueError('Unsupported Standard wet-film profile')
    shader_identity = profile_shader_identity(profile, 'HGRP/CharacterNPR')
    shader_sha256 = shader_identity['object_sha256']
    manifest = verified_file(profile.parent, config['texture_manifest'], config['texture_manifest_sha256'])
    orientation, tiling = config['orientation'], config['tiling']
    if type(orientation) not in (float, int) or orientation not in (0, 1):
        raise ValueError('Wet-film projection orientation must be explicitly 0 or 1')
    if type(tiling) not in (float, int) or not math.isfinite(tiling) or not 0 < tiling < 1000:
        raise ValueError('Wet-film projection tiling must be finite and positive')
    active = visible_source_group_names()
    groups = tuple(group for group in bpy.data.node_groups if group.name in active
                   and group.get('source_shader') == 'HGRP/CharacterNPR'
                   and '_CHARACTER_FUR' not in source_keywords(group))
    if not groups:
        return {'status': 'no_matching_standard_materials', 'groups': []}
    if any(group.get('wet_surface_binding') for group in groups):
        raise ValueError('Standard wet-film profile is already attached')
    names = frozenset(group.name for group in groups)
    meshes: dict[int, bpy.types.Object] = {}
    for obj in bpy.context.scene.objects:
        if obj.type != 'MESH' or obj.hide_render:
            continue
        used = any(slot.material is not None and slot.material.use_nodes
                   and any(node.type == 'GROUP' and node.node_tree is not None and node.node_tree.name in names
                           for node in slot.material.node_tree.nodes) for slot in obj.material_slots)
        if not used:
            continue
        key = obj.data.as_pointer()
        if key in meshes and meshes[key].matrix_world != obj.matrix_world:
            raise ValueError(f'Shared weather mesh has incompatible transforms: {obj.name}')
        meshes[key] = obj
    if not meshes:
        raise ValueError('Standard wet-film groups have no visible source mesh')
    attributes = rest_attributes(tuple(meshes.values()))
    records = json.loads(io_path(manifest).read_text(encoding='utf-8'))
    if not isinstance(records, list):
        raise ValueError('Weather texture manifest must be an array')
    rain = tuple(row for row in records if isinstance(row, dict) and row.get('slot') == 'characterRainDropTexture')
    if len(rain) != 1 or rain[0].get('color_space') != 0:
        raise ValueError('Weather texture manifest requires one linear source RainDrop texture')
    texture = verified_file(manifest.parent, rain[0]['artifact'], rain[0]['sha256'])
    image = bpy.data.images.load(str(texture), check_existing=False)
    image.colorspace_settings.name = 'Non-Color'
    image.alpha_mode = 'CHANNEL_PACKED'
    image.pack()
    image['endfield_weather_source_sha256'] = file_digest(texture)
    projection_provenance = 'Rest-space projection approximates unverified native varying bindings'
    if weather_controls is None:
        provenance = 'Explicit artist weather state; ' + projection_provenance
        controls = make_weather_controls(WeatherUniforms((0., 0., 0., 0.), 0., 0., 0., provenance),
                                         WeatherOverride(1., (0., 0., 0., 0.), 0.), layout)
    else:
        controls = weather_controls
    source_record = weather_control_source_record(controls)
    native_weather = source_record['kind'] == NATIVE_WEATHER_SOURCE
    controls['endfield_standard_wetfilm_controls'] = True
    kernel = make_native_standard_wet_film(image, shader_sha256, layout)
    projection = WeatherProjection('EF_EXPERIMENTAL_REST_UNITY_POSITION',
        'EF_EXPERIMENTAL_REST_UNITY_NORMAL', float(orientation), float(tiling), projection_provenance)
    bindings = tuple(bind_standard_wet_surface(group, kernel, controls, projection, layout) for group in groups)
    notes = bpy.data.texts.new('Standard wet film controls')
    notes.write(('Standard cloth wet film uses the caller-supplied decoded per-draw source on the editable Source controls; runtime capture remains unverified. Set User Override Weight to 1 to use User Effect X/Z/Y/W.\n'
                 if native_weather else
                 'Standard cloth wet film: enter the shared weather group and adjust User Effect X (rain) or Z (wetness), 0 to 1.\n')
                + 'All user effects default to zero. User Effect Y enables the adjustable water-height mask.\n'
                'Compiled Standard surface equations and original packed rain texture are retained.\n'
                'Optional Standard snow uses User Effect W when configured in the same profile.\n'
                'Rest-space projection and live weather state remain unverified; Skin/Hair/Fur snow and dynamic drip normals are not reconstructed here.\n')
    return {'status': NATIVE_WETFILM_STATUS if native_weather else DRY_WETFILM_STATUS,
            'weather_source': source_record, 'groups': [group.name for group in groups],
            'controls': controls.name, 'bindings': bindings, 'projection_attributes': attributes,
            'packed_texture': image.name, 'texture_sha256': file_digest(texture),
            'source_shader_identity': shader_identity,
            'source_files': [{'path': str(path), 'sha256': file_digest(path)} for path in (manifest, texture)],
            'native_runtime_state_verified': False, 'native_projection_verified': False}


def verify_profile_wetfilm(receipt: dict[str, object]) -> dict[str, object]:
    """Verify the saved weather controls, consumer groups and packed source texture."""
    if receipt['status'] in ('not_configured', 'no_matching_standard_materials'):
        return {'status': receipt['status']}
    status = receipt['status']
    if status not in (DRY_WETFILM_STATUS, NATIVE_WETFILM_STATUS):
        raise ValueError('Unrecognized wet-film receipt status')
    controls = bpy.data.node_groups[str(receipt['controls'])]
    source_record = receipt.get('weather_source')
    if not isinstance(source_record, dict):
        raise TypeError('Saved Standard weather source record is malformed')
    expected_kind = NATIVE_WEATHER_SOURCE if status == NATIVE_WETFILM_STATUS else DRY_WEATHER_SOURCE
    if source_record.get('kind') != expected_kind:
        raise AssertionError('Saved Standard wet-film status and source kind disagree')
    verify_weather_controls(controls, source_record)
    for name in receipt['groups']:
        group = bpy.data.node_groups[name]
        if group.get('source_shader') != 'HGRP/CharacterNPR' or '_CHARACTER_FUR' in source_keywords(group):
            raise AssertionError(f'Saved Standard weather group is outside the exact non-Fur family: {name}')
        if group.get('wet_surface_binding') != 'HGRPStandardWetSurface/1':
            raise AssertionError(f'Saved Standard weather consumer is absent: {name}')
    image = bpy.data.images[str(receipt['packed_texture'])]
    if image.packed_file is None or hashlib.sha256(image.packed_file.data).hexdigest() != receipt['texture_sha256']:
        raise AssertionError('Saved Standard rain texture bytes differ')
    if image.colorspace_settings.name != 'Non-Color' or image.alpha_mode != 'CHANNEL_PACKED':
        raise AssertionError('Saved Standard rain texture interpretation differs')
    return {'status': 'passed', 'standard_groups': len(receipt['groups']),
            'weather_source_kind': expected_kind, 'runtime_capture_verified': False,
            'dry_default': expected_kind == DRY_WEATHER_SOURCE,
            'rain_texture_packed': True}
