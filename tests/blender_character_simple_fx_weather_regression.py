"""Exercise compact body weather beneath actual retained transparent FX layers."""
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
from character_material_authoring import shader_dependencies
from character_material_options import parse_character_material_options
from character_material_variant import apply_material_variant, source_materials, verify_material_variant
from character_simple_materials import _source_passes, simple_material_surfaces
from character_weather_panel import clothing_effect_controls


def _verify_layers() -> int:
    count = 0
    for material in source_materials(bpy.context.scene):
        if not material.get('endfield_simple_material'):
            continue
        live = shader_dependencies(material)
        for effect in material.node_tree.nodes:
            if effect.type != 'GROUP' or effect.node_tree is None or 'source_fx_layer' not in effect.node_tree:
                continue
            previous, = effect.inputs['Previous Shader'].links
            layer, = effect.outputs['Layer Shader'].links
            mix = layer.to_node
            if (mix.type != 'MIX_SHADER' or layer.to_socket != mix.inputs[2]
                    or mix.inputs[1].links[0].from_socket != previous.from_socket
                    or not mix.inputs[0].is_linked
                    or (material.node_tree.as_pointer(), mix.as_pointer()) not in live.nodes):
                raise AssertionError('Retained extra layer lost its live previous-surface or enable route')
            count += 1
    if not count:
        raise AssertionError('The actual fixture did not exercise a compact body with retained transparent FX')
    controls = tuple(socket for material in source_materials(bpy.context.scene)
                     for socket in clothing_effect_controls(material))
    if not controls:
        raise AssertionError('Compact body lost the actual source clothing special-effect strength control')
    return count


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--blend', type=Path, required=True)
    parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args(sys.argv[sys.argv.index('--') + 1:])
    source_hash = hashlib.sha256(args.blend.read_bytes()).hexdigest()
    bpy.ops.wm.open_mainfile(filepath=str(args.blend), use_scripts=False, load_ui=False)
    scene = bpy.context.scene
    original = source_materials(scene)
    standalone = tuple(material for material in original
                       if all(item.family == 'retained_vfx' for item in _source_passes(material)))
    standalone_names = tuple(material.name for material in standalone)
    standalone_graphs = {material.name: tuple((node.name, node.bl_idname) for node in material.node_tree.nodes)
                         for material in standalone}
    geometry = {obj.name: (obj.data.name, len(obj.data.vertices), len(obj.data.loops),
                           tuple((modifier.name, modifier.type) for modifier in obj.modifiers))
                for obj in scene.objects if obj.type == 'MESH'}
    options = parse_character_material_options({'mode': 'simplified', 'lighting': 'simple',
        'weather': {'quality': 'simple', 'effects': ['wet', 'snow', 'rain']}})
    report = apply_material_variant(scene, options, [])
    validation = verify_material_variant(report)
    layers = _verify_layers()
    if not simple_material_surfaces(scene) or not validation['weather_groups']:
        raise AssertionError('Requested surface weather has no compact body consumers')
    for name in standalone_names:
        material = bpy.data.materials[name]
        if material.get('endfield_simple_material') or tuple((node.name, node.bl_idname)
                for node in material.node_tree.nodes) != standalone_graphs[name]:
            raise AssertionError('Standalone retained FX changed during compact body construction')
    after = {obj.name: (obj.data.name, len(obj.data.vertices), len(obj.data.loops),
                        tuple((modifier.name, modifier.type) for modifier in obj.modifiers))
             for obj in scene.objects if obj.type == 'MESH'}
    if after != geometry:
        raise AssertionError('Material/weather construction changed actual mesh or modifier bindings')
    args.output.mkdir(parents=True, exist_ok=False)
    target = args.output / 'simple_fx_weather.blend'
    bpy.context.preferences.filepaths.save_version = 0
    bpy.ops.wm.save_as_mainfile(filepath=str(target), check_existing=False)
    bpy.ops.wm.open_mainfile(filepath=str(target), use_scripts=False, load_ui=False)
    reopened = verify_material_variant(json.loads(bpy.context.scene['endfield_material_variant']))
    if _verify_layers() != layers:
        raise AssertionError('Saved/reopened compact material lost a retained transparent layer')
    material, weather = next((material, node) for material in source_materials(bpy.context.scene)
        for node in material.node_tree.nodes if node.type == 'GROUP' and node.node_tree is not None
        and node.node_tree.get('endfield_simple_weather'))
    link, = weather.outputs['表面粗糙度输出'].links
    destination = link.to_socket
    material.node_tree.links.remove(link)
    try:
        verify_material_variant(json.loads(bpy.context.scene['endfield_material_variant']))
    except AssertionError:
        pass
    else:
        raise AssertionError('Variant audit accepted disconnected requested weather')
    finally:
        material.node_tree.links.new(weather.outputs['表面粗糙度输出'], destination)
    if hashlib.sha256(args.blend.read_bytes()).hexdigest() != source_hash:
        raise AssertionError('CPU regression modified the input delivery')
    result = {'status': 'passed', 'rendered': False, 'source': str(args.blend),
        'source_sha256': source_hash, 'blend': str(target), 'retained_transparent_layers': layers,
        'standalone_fx_unchanged': list(standalone_names), 'mesh_bindings_unchanged': True,
        'clothing_strength_retained': True, 'disconnected_weather_rejected': True, 'saved_reopen': reopened}
    (args.output / 'receipt.json').write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding='utf-8')
    print('CHARACTER_SIMPLE_FX_WEATHER_REGRESSION_PASSED', flush=True)


if __name__ == '__main__':
    main()
