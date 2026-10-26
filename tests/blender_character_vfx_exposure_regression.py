"""Verify family-specific exposure adaptation with actual retained source packets."""
from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
import sys

import bpy

sys.dont_write_bytecode = True
sys.path[:0] = [str(Path(__file__).resolve().parents[1] / 'src'),
                str(Path(__file__).resolve().parents[1] / 'src/character_runtime')]
from blender_scene_effect_materials import make_mesh_effect_material
from blender_scene_materials import layout_shader_node_tree
from character_mesh_effect import material_group, mesh_color_source
from character_npr_vfx import CHARACTER_VFX_SHADER, character_vfx_preview_record
from endfield_scene_material_contract import load_catalog, validate_material
from rebuild_character_materials import read_packet


def _image_cache() -> dict[tuple[tuple[str, int], str, str], bpy.types.Image]:
    result: dict[tuple[tuple[str, int], str, str], bpy.types.Image] = {}
    trees = (*bpy.data.node_groups, *(material.node_tree for material in bpy.data.materials if material.use_nodes))
    for tree in trees:
        for node in tree.nodes:
            if node.type != 'TEX_IMAGE' or node.image is None or 'source_identity' not in node:
                continue
            source, path_id = json.loads(node['source_identity'])
            identity = (str(source).casefold(), int(path_id))
            image = node.image
            result[(identity, image.colorspace_settings.name, image.alpha_mode)] = image
    return result


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--payload', type=Path, required=True)
    parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args(sys.argv[sys.argv.index('--') + 1:])
    blend = args.payload / 'build/character.blend'
    digest = hashlib.sha256(blend.read_bytes()).hexdigest()
    prepared = args.payload / 'prepared'
    packet = read_packet(prepared / 'static_probe', prepared / 'shader_identity/shader_identity.json',
                         args.payload / 'build/selection_report.json', prepared / 'renderer_visibility.json')
    bpy.ops.wm.open_mainfile(filepath=str(blend), use_scripts=False, load_ui=False)
    candidates = tuple(source for source in packet.materials if source.shader == CHARACTER_VFX_SHADER
                       and source.floats['_UseFresnel'] == 1. and source.floats['_ExpIntensity'] > 0.)
    if not candidates:
        raise AssertionError('Actual packet has no CharacterNPR_VFX Fresnel source with nonzero exposure intensity')
    source = max(candidates, key=lambda value: value.floats['_ExpIntensity'])
    obj = next(obj for obj in bpy.context.scene.objects if obj.type == 'MESH' and not obj.hide_render
               and any(slot.material is not None and list(source.identity) in json.loads(
                   slot.material.get('source_material_stack', '[]')) for slot in obj.material_slots))
    source_group, = (tree for tree in bpy.data.node_groups if tree.get('source_shader') == CHARACTER_VFX_SHADER
                     and tree.get('source_identity') and tuple(json.loads(tree['source_identity'])) == source.identity)
    record = validate_material(json.loads(source_group['endfield_fx_source_material_json']))
    before = json.dumps(record, sort_keys=True)
    preview = character_vfx_preview_record(record)
    if (preview['Floats']['_ExpIntensity'] != 0. or record['Floats']['_ExpIntensity'] <= 0.
            or json.dumps(record, sort_keys=True) != before
            or preview['Floats']['_TintColorIntensity'] != record['Floats']['_TintColorIntensity']
            or preview['Floats']['_FresnelPower'] != record['Floats']['_FresnelPower']
            or preview['Colors']['_FresnelColor'] != record['Colors']['_FresnelColor']):
        raise AssertionError('Character family adaptation changed source parameters or retained the absent gain')
    group = material_group(source, packet, _image_cache(), mesh_color_source(obj), layout_shader_node_tree)
    exposure = group.nodes['Exposure Intensity']
    if exposure.inputs[3].default_value != 0.:
        raise AssertionError('Actual CharacterNPR_VFX factory still amplifies excess source RGB')
    normal = group.nodes['Fresnel Source Normal']
    if normal.inputs[3].is_linked or normal.inputs[3].default_value != 1.:
        raise AssertionError('Character VFX applies a second backface flip to Blender shading normals')
    contract = json.loads(group['source_character_vfx_exposure'])
    if (contract['source_intensity'] != record['Floats']['_ExpIntensity']
            or contract['preview_intensity'] != 0. or contract['current_compiled_variant_verified'] is not False
            or json.loads(group['source_floats'])['_ExpIntensity'] != record['Floats']['_ExpIntensity']):
        raise AssertionError('Saved family contract lost original exposure values or overstated native verification')
    native_sources, native_textures = load_catalog((args.payload / 'potential/effects/scene_manifest.json',))
    native = next(value for value in native_sources.values() if value['Shader']['Name'] == 'HGRP/Effect/VFXBaseV2'
                  and value['Floats']['_ExpIntensity'] > 0.)
    native_before = json.dumps(native, sort_keys=True)
    native_material = make_mesh_effect_material(native, native_textures, 1., 4)
    if (native_material.node_tree.nodes['Exposure Intensity'].inputs[3].default_value != native['Floats']['_ExpIntensity']
            or json.dumps(native, sort_keys=True) != native_before):
        raise AssertionError('Native VFXBaseV2 lost its own source threshold/excess gain')
    native_name, group_name = native_material.name, group.name
    group.use_fake_user = True
    native_material.use_fake_user = True
    args.output.mkdir(parents=True, exist_ok=False)
    target = args.output / 'family_exposure.blend'
    bpy.context.preferences.filepaths.save_version = 0
    bpy.ops.wm.save_as_mainfile(filepath=str(target), check_existing=False)
    bpy.ops.wm.open_mainfile(filepath=str(target), use_scripts=False, load_ui=False)
    if (bpy.data.node_groups[group_name].nodes['Exposure Intensity'].inputs[3].default_value != 0.
            or bpy.data.materials[native_name].node_tree.nodes['Exposure Intensity'].inputs[3].default_value != native['Floats']['_ExpIntensity']):
        raise AssertionError('Family-specific exposure graph did not survive save/reopen')
    saved_normal = bpy.data.node_groups[group_name].nodes['Fresnel Source Normal']
    if saved_normal.inputs[3].is_linked or saved_normal.inputs[3].default_value != 1.:
        raise AssertionError('Saved/reopened Character VFX normal facing was applied twice')
    if hashlib.sha256(blend.read_bytes()).hexdigest() != digest:
        raise AssertionError('CPU regression modified the input delivery')
    result = {'status': 'passed', 'rendered': False, 'character_source': source.identity,
        'source_intensity': record['Floats']['_ExpIntensity'], 'preview_intensity': 0.,
        'native_vfx_source': [native['Source'], native['PathId']], 'native_vfx_intensity_preserved': native['Floats']['_ExpIntensity'],
        'source_unchanged': True, 'save_reopen': True, 'contract': contract, 'blend': str(target)}
    (args.output / 'receipt.json').write_text(json.dumps(result, indent=2), encoding='utf-8')
    print('CHARACTER_VFX_EXPOSURE_REGRESSION_PASSED', flush=True)


if __name__ == '__main__':
    main()
