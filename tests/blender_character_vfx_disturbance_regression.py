"""Check CharacterNPR disturbance formulas on real Blender graphs and payloads."""
from __future__ import annotations

import argparse
from array import array
from dataclasses import replace
import hashlib
import json
import math
from pathlib import Path
import sys

import bpy

sys.dont_write_bytecode = True
root = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(root / 'src'), str(root / 'src/character_runtime')]

from blender_scene_effect_materials import make_mesh_effect_material  # noqa: E402
from blender_scene_materials import layout_shader_node_tree  # noqa: E402
from character_mesh_effect import MeshColorSource, VFX_SHADER, material_group, mesh_color_source  # noqa: E402
from character_npr_vfx import CHARACTER_VFX_SHADER  # noqa: E402
from endfield_scene_material_contract import load_catalog  # noqa: E402
from rebuild_character_materials import MaterialSource, SourcePacket, read_packet  # noqa: E402
Texel = tuple[float, float, float, float]
TexelSource = Texel | dict[str, Texel]
Vector = tuple[float, ...]

def _image_cache() -> dict[tuple[tuple[str, int], str, str], bpy.types.Image]:
    trees = (*bpy.data.node_groups, *(mat.node_tree for mat in bpy.data.materials if mat.use_nodes))
    return {((str(cab).casefold(), int(path_id)), node.image.colorspace_settings.name, node.image.alpha_mode): node.image
            for tree in trees for node in tree.nodes
            if node.type == 'TEX_IMAGE' and node.image and 'source_identity' in node
            for cab, path_id in (json.loads(node['source_identity']),)}

def _value(socket: bpy.types.NodeSocket, texel: TexelSource, seconds: float) -> float | Vector:
    if not socket.is_output:
        links = tuple(socket.links)
        if len(links) > 1:
            raise AssertionError(f'Multiple links at {socket.node.name}/{socket.name}')
        value = _value(links[0].from_socket, texel, seconds) if links else socket.default_value
        return float(value) if isinstance(value, (int, float)) else tuple(float(v) for v in value)
    node = socket.node
    if node.type == 'TEX_IMAGE':
        if isinstance(texel, dict):
            if node.name not in texel:
                raise AssertionError(f'Missing texel for disturbance texture: {node.name}')
            sample = texel[node.name]
        else:
            if node.name != 'Source _SampleTex0':
                raise AssertionError(f'Unexpected disturbance texture: {node.name}')
            sample = texel
        return sample[:3] if socket.name == 'Color' else sample[3]
    if node.type == 'VALUE':
        return seconds if node.name == 'EF FX Source Time Seconds' else float(socket.default_value)
    if node.type == 'SEPXYZ':
        vector = _value(node.inputs[0], texel, seconds)
        if not isinstance(vector, tuple):
            raise AssertionError(f'Expected vector at {node.name}')
        return vector[('X', 'Y', 'Z').index(socket.name)]
    if node.type == 'COMBXYZ':
        return tuple(float(_value(node.inputs[i], texel, seconds)) for i in range(3))
    if node.type == 'MATH':
        left, right = (float(_value(node.inputs[i], texel, seconds)) for i in range(2))
        return {'MULTIPLY': left * right, 'SUBTRACT': left - right, 'ADD': left + right}[node.operation]
    if node.type == 'VECT_MATH' and node.operation == 'SCALE':
        vector = _value(node.inputs[0], texel, seconds)
        factor = float(_value(node.inputs[3], texel, seconds))
        if not isinstance(vector, tuple):
            raise AssertionError(f'Expected vector at {node.name}')
        return tuple(v * factor for v in vector)
    if node.type == 'VECT_MATH' and node.operation == 'ADD':
        left, right = (_value(node.inputs[i], texel, seconds) for i in range(2))
        if isinstance(left, tuple) and isinstance(right, tuple):
            return tuple(a + b for a, b in zip(left, right, strict=True))
    raise AssertionError(f'Unsupported disturbance node: {node.name}/{node.type}/{node.operation}')

def _close(actual: float | tuple[float, ...], expected: float | tuple[float, ...], label: str) -> None:
    left = actual if isinstance(actual, tuple) else (actual,)
    right = expected if isinstance(expected, tuple) else (expected,)
    if len(left) != len(right) or not all(math.isclose(a, b, rel_tol=1e-6, abs_tol=1e-6) for a, b in zip(left, right, strict=True)):
        raise AssertionError(f'Formula mismatch at {label}: {actual} != {expected}')

def _build(payload: Path, name: str) -> tuple[MaterialSource, SourcePacket, bpy.types.NodeTree, MeshColorSource, str]:
    blend = payload / 'build/character.blend'
    digest = hashlib.sha256(blend.read_bytes()).hexdigest()
    prepared = payload / 'prepared'
    packet = read_packet(prepared / 'static_probe', prepared / 'shader_identity/shader_identity.json',
                         payload / 'build/selection_report.json', prepared / 'renderer_visibility.json')
    bpy.ops.wm.open_mainfile(filepath=str(blend), use_scripts=False, load_ui=False)
    source, = (row for row in packet.materials if row.name == name and row.shader == CHARACTER_VFX_SHADER)
    obj = next(obj for obj in bpy.context.scene.objects if obj.type == 'MESH' and not obj.hide_render
               and any(slot.material is not None and list(source.identity) in json.loads(
                   slot.material.get('source_material_stack', '[]')) for slot in obj.material_slots))
    color_source = mesh_color_source(obj)
    before = json.dumps({'Floats': dict(source.floats), 'Colors': dict(source.colors)}, sort_keys=True)
    group = material_group(source, packet, _image_cache(), color_source, layout_shader_node_tree)
    archived = json.loads(group['endfield_fx_source_material_json'])
    if (before != json.dumps({'Floats': dict(source.floats), 'Colors': dict(source.colors)}, sort_keys=True)
            or json.loads(group['source_floats']) != dict(source.floats)
            or json.dumps(archived['Floats'], sort_keys=True) != json.dumps(dict(source.floats), sort_keys=True)
            or json.dumps(archived['Colors'], sort_keys=True) != json.dumps(dict(source.colors), sort_keys=True)):
        raise AssertionError(f'Adapter changed source values: {name}')
    return source, packet, group, color_source, digest

def _texel(group: bpy.types.NodeTree, source: MaterialSource, preview: dict[str, object]) -> Texel:
    binding, = (row for row in source.textures if row.name == '_DisturbTex1')
    samples = tuple(node for node in group.nodes if node.type == 'TEX_IMAGE'
                    and tuple(json.loads(node.get('source_identity', '[]'))) == binding.identity)
    node = group.nodes.get('Source _SampleTex0')
    if (preview['Floats']['_UseDisturb2'] != 0.0 or any(n.name.startswith('Disturb 2') for n in group.nodes)
            or len(samples) != 1 or node != samples[0] or node.image is None or node.image.colorspace_settings.name not in ('Non-Color', 'sRGB')):
        raise AssertionError(f'Expected one linear disturbance sample: {group.name}; use2={preview["Floats"]["_UseDisturb2"]}; aliases={[n.name for n in group.nodes if n.name.startswith("Disturb 2")]}; source_samples={[n.name for n in samples]}; node={node}; colorspace={node.image.colorspace_settings.name if node and node.image else None}')
    pixels = array('f', [0.0]) * (node.image.size[0] * node.image.size[1] * 4)
    node.image.pixels.foreach_get(pixels)
    index = max(range(node.image.size[0] * node.image.size[1]),
                key=lambda i: abs(pixels[4 * i] - 0.5) + abs(pixels[4 * i + 1] - 0.5))
    rgba = tuple(float(pixels[4 * index + c]) for c in range(4))
    if node.image.colorspace_settings.name == 'sRGB':
        rgba = tuple((v / 12.92 if v <= 0.04045 else ((v + 0.055) / 1.055) ** 2.4)
                     if index < 3 else v for index, v in enumerate(rgba))
    return rgba

def _check_remap(group: bpy.types.NodeTree, source: MaterialSource, red: str, decoder: str, socket_index: int) -> None:
    scale, shift, red_node = (group.nodes[name] for name in ('Character Disturb Red Scale', 'Character Disturb Signed Red', red))
    bias = source.floats['_Bi_Disturb']
    if (scale.operation != 'MULTIPLY' or shift.operation != 'SUBTRACT'
            or scale.inputs[1].default_value != 1.0 + bias or shift.inputs[1].default_value != bias
            or len(scale.inputs[0].links) != 1 or scale.inputs[0].links[0].from_node != red_node
            or len(shift.inputs[0].links) != 1 or shift.inputs[0].links[0].from_node != scale
            or group.nodes[decoder].inputs[socket_index].links[0].from_node != shift):
        raise AssertionError(f'R * (1 + Bi) - Bi graph differs: {group.name}')

def _speed(group: bpy.types.NodeTree, source: MaterialSource, preview: dict[str, object], texel: Texel) -> dict[str, float]:
    speed = source.colors['_DisturbUVSpeed1']
    floats, colors = preview['Floats'], preview['Colors']
    if (floats['_UseDisturb2'] != 0.0 or colors['_SampleTex0UVSpeed'] != list(speed)
            or floats['_DisturbUIntensity1'] != source.floats['_DisturbUIntensity1']
            or floats['_DisturbVIntensity1'] != source.floats['_DisturbVIntensity1']
            or not any(abs(v) > 1e-8 for v in speed[:2])):
        raise AssertionError(f'Second alias, speed, or intensity changed: {source.name}')
    for axis, component in (('X', 0), ('Y', 1)):
        node = group.nodes['_SampleTex0 Speed ' + axis]
        if node.operation != 'MULTIPLY':
            raise AssertionError(f'Source UV speed operation changed: {node.name}')
        _close(node.inputs[1].default_value, speed[component], node.name)
        _close(_value(node.outputs[0], texel, 2.0), 2.0 * speed[component], node.name)
    return {'seconds': 2.0, 'x': 2.0 * speed[0], 'y': 2.0 * speed[1]}

def _normal(group: bpy.types.NodeTree, source: MaterialSource, texel: Texel) -> Vector:
    _check_remap(group, source, 'Disturb 1 Normal Red', 'Disturb 1 Normal Alpha Red', 1)
    strength = source.floats['_DisturbUIntensity1']
    _close(group.nodes['Disturb 1 Normal Intensity Weight'].inputs[1].default_value, strength, group.name)
    red = texel[0] * (1 + source.floats['_Bi_Disturb']) - source.floats['_Bi_Disturb']
    expected = ((2 * texel[3] * red - 1) * strength, (2 * texel[1] - 1) * strength, 0.0)
    actual = _value(group.nodes['Disturb 1 Normal UV Offset'].outputs['Vector'], texel, 2.0)
    _close(actual, expected, group.name)
    return tuple(actual)

def _scalar(group: bpy.types.NodeTree, source: MaterialSource, texel: Texel) -> Vector:
    _check_remap(group, source, 'Disturb 1 Red', 'Disturb 1 Particle Weight', 0)
    weights = (source.floats['_DisturbUIntensity1'], source.floats['_DisturbVIntensity1'], 0.0)
    _close(tuple(group.nodes['Disturb 1 Intensities'].inputs[0].default_value), weights, group.name)
    red = texel[0] * (1 + source.floats['_Bi_Disturb']) - source.floats['_Bi_Disturb']
    expected = (red * weights[0], red * weights[1], 0.0)
    actual = _value(group.nodes['Disturb 1 UV Offset'].outputs['Vector'], texel, 2.0)
    _close(actual, expected, group.name)
    return tuple(actual)

def _shared(payload: Path) -> dict[str, object]:
    sources, textures = load_catalog((payload / 'potential/effects/scene_manifest.json',))
    record, = (row for row in sources.values() if row['Shader']['Name'] == VFX_SHADER and row['Floats']['_ExpIntensity'] > 0.0)
    before = json.dumps(record, sort_keys=True)
    material = make_mesh_effect_material(record, textures, 1.0, 4)
    tree = material.node_tree
    if (json.dumps(record, sort_keys=True) != before
            or tree.nodes.get('Character Disturb Red Scale') is not None
            or tree.nodes.get('Character Disturb Signed Red') is not None
            or tree.nodes['Exposure Intensity'].inputs[3].default_value != record['Floats']['_ExpIntensity']):
        raise AssertionError('Character remap changed shared VFXBaseV2 behavior')
    return {'source': [record['Source'], record['PathId']], 'exposure': record['Floats']['_ExpIntensity'],
            'remap_absent': True, 'generic_exposure_preserved': True}

def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--payload', type=Path, required=True)
    parser.add_argument('--scalar-payload', type=Path, required=True)
    parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args(sys.argv[sys.argv.index('--') + 1:])
    normal_payload, scalar_payload = args.payload, args.scalar_payload
    normal_source, _, normal_group, _, normal_blend_hash = _build(
        normal_payload, 'M_fx_jsspsi_toppotential_01_4')
    normal_preview = json.loads(normal_group['source_vfx_preview_record'])
    normal_texel = _texel(normal_group, normal_source, normal_preview)
    normal_speed_eval = _speed(normal_group, normal_source, normal_preview, normal_texel)
    normal_offset = _normal(normal_group, normal_source, normal_texel)
    normal_texture_colorspace = normal_group.nodes['Source _SampleTex0'].image.colorspace_settings.name
    shared = _shared(normal_payload)

    scalar_source, packet, scalar_group, color_source, scalar_blend_hash = _build(
        scalar_payload, 'M_fx_typhoea_toppotential_01_2')
    scalar_preview = json.loads(scalar_group['source_vfx_preview_record'])
    scalar_texel = _texel(scalar_group, scalar_source, scalar_preview)
    scalar_speed_eval = _speed(scalar_group, scalar_source, scalar_preview, scalar_texel)
    scalar_offset = _scalar(scalar_group, scalar_source, scalar_texel)
    scalar_texture_colorspace = scalar_group.nodes['Source _SampleTex0'].image.colorspace_settings.name
    control_source = replace(scalar_source, floats={**scalar_source.floats, '_Bi_Disturb': 1.0,
                                                   '_DisturbVIntensity1': 0.25})
    control_group = material_group(control_source, packet, _image_cache(), color_source, layout_shader_node_tree)
    control_texel = _texel(control_group, control_source, json.loads(control_group['source_vfx_preview_record']))
    control_offset = _scalar(control_group, control_source, control_texel)
    if abs(control_offset[1]) < 1e-6 or scalar_source.floats['_Bi_Disturb'] != 0.0:
        raise AssertionError('Synthetic scalar control was not nontrivial or altered source')
    for payload, digest in ((normal_payload, normal_blend_hash), (scalar_payload, scalar_blend_hash)):
        if hashlib.sha256((payload / 'build/character.blend').read_bytes()).hexdigest() != digest:
            raise AssertionError(f'Input blend changed: {payload}')

    args.output.mkdir(parents=True, exist_ok=True)
    receipt = {'status': 'passed', 'rendered': False,
        'normal_source': [normal_source.name, *normal_source.identity],
        'normal': {'bi': normal_source.floats['_Bi_Disturb'], 'strength': normal_source.floats['_DisturbUIntensity1'],
                   'speed': list(normal_source.colors['_DisturbUVSpeed1']), 'texel': list(normal_texel),
                   'uv_offset': list(normal_offset), 'speed_eval': normal_speed_eval},
        'scalar_source': [scalar_source.name, *scalar_source.identity],
        'scalar': {'bi': scalar_source.floats['_Bi_Disturb'], 'u': scalar_source.floats['_DisturbUIntensity1'],
        'v': scalar_source.floats['_DisturbVIntensity1'], 'speed': list(scalar_source.colors['_DisturbUVSpeed1']),
        'texel': list(scalar_texel), 'uv_offset': list(scalar_offset), 'speed_eval': scalar_speed_eval,
        'game_source_evidence': True},
        'scalar_test_control': {'bi': 1.0, 'v': 0.25, 'texel': list(control_texel),
                                'uv_offset': list(control_offset), 'game_source_evidence': False},
        'texture_colorspaces': [normal_texture_colorspace, scalar_texture_colorspace],
        'shared_vfxbasev2': shared, 'input_blends_unchanged': True,
        'scope': 'Real Jsspsi normal and Typhoea scalar values/textures. CPU evaluation follows Blender disturbance nodes; sRGB texels are linearized. Nonzero scalar Bi/V is a test-only control; no full-scene render.'}
    (args.output / 'verification.json').write_text(json.dumps(receipt, indent=2), encoding='utf-8')
    print('CHARACTER_VFX_DISTURBANCE_REGRESSION_PASSED', flush=True)

if __name__ == '__main__':
    main()
