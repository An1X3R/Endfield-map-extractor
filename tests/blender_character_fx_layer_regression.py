"""Verify transparent extra-pass composition using an actual exported character."""
from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys

import bpy

sys.dont_write_bytecode = True
sys.path[:0] = [str(Path(__file__).resolve().parents[1] / 'src'),
                str(Path(__file__).resolve().parents[1] / 'src/character_runtime')]
from blender_scene_materials import layout_shader_node_tree
from character_material_authoring import package_material_authoring, shader_dependencies
from character_mesh_effect import _extra_pass_layer
from character_weather_panel import clothing_effect_controls
from rebuild_character_materials import read_packet, surface_stack


def _signature(material: bpy.types.Material) -> frozenset[tuple[str, str, str]]:
    live = shader_dependencies(material)
    trees = (material.node_tree, *bpy.data.node_groups)
    return frozenset((node.name, node.bl_idname, node.label) for tree in trees for node in tree.nodes
                     if (tree.as_pointer(), node.as_pointer()) in live.nodes)


def _check_kernel(kernel: bpy.types.ShaderNodeTree) -> None:
    add = kernel.nodes['Add Shader']
    if len(add.inputs[0].links) != 1 or len(add.inputs[1].links) != 1:
        raise AssertionError('Layer source/destination branches are disconnected')
    attenuation = add.inputs[0].links[0].from_node
    emission = add.inputs[1].links[0].from_node
    if (attenuation.type != 'MIX_SHADER' or attenuation.inputs[1].is_linked
            or len(attenuation.inputs[0].links) != 1
            or attenuation.inputs[0].links[0].from_node != kernel.nodes['Destination Factor']
            or len(attenuation.inputs[2].links) != 1
            or attenuation.inputs[2].links[0].from_socket.name != 'Previous Shader'
            or emission.type != 'EMISSION' or len(emission.inputs['Color'].links) != 1
            or emission.inputs['Color'].links[0].from_node != kernel.nodes['Premultiply Source Once']):
        raise AssertionError('Layer composition changed the source premultiply or destination factor')


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--payload', type=Path, required=True)
    parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args(sys.argv[sys.argv.index('--') + 1:])
    prepared = args.payload / 'prepared'
    packet = read_packet(prepared / 'static_probe', prepared / 'shader_identity/shader_identity.json',
                         args.payload / 'build/selection_report.json', prepared / 'renderer_visibility.json')
    bpy.ops.wm.open_mainfile(filepath=str(args.payload / 'build/character.blend'), use_scripts=False, load_ui=False)
    sources = {source.identity: source for source in packet.materials}
    candidates = tuple(material for material in bpy.data.materials
                       if len(json.loads(material.get('source_material_stack', '[]'))) > 1
                       and clothing_effect_controls(material))
    original, = candidates
    identities = tuple(tuple(value) for value in json.loads(original['source_material_stack']))
    body, effect = (sources[key] for key in identities)
    if effect.floats['_SurfaceType'] != 1. or effect.floats['_SrcBlend'] != 1. or effect.floats['_DstBlend'] != 10.:
        raise AssertionError('Actual extra-pass packet does not exercise transparent premultiplied source-over')
    groups = {}
    for key in identities:
        matches = tuple(tree for tree in bpy.data.node_groups if tree.get('source_identity')
                        and json.loads(tree['source_identity']) == list(key)
                        and tree.get('source_shader') == sources[key].shader)
        source, = matches
        groups[key] = source.copy()
    standalone = surface_stack((effect,), groups, layout_shader_node_tree)
    opaque = surface_stack((body,), groups, layout_shader_node_tree)
    standalone_before, opaque_before = _signature(standalone), _signature(opaque)
    effect_tree = groups[effect.identity]
    _extra_pass_layer(effect_tree, effect)
    if _signature(standalone) != standalone_before or _signature(opaque) != opaque_before:
        raise AssertionError('Layer support changed the existing standalone FX or opaque body shader')
    layered = surface_stack((body, effect), groups, layout_shader_node_tree)
    instance, = (node for node in layered.node_tree.nodes if node.type == 'GROUP' and node.node_tree == effect_tree)
    if not instance.inputs['Previous Shader'].is_linked or not instance.outputs['Layer Shader'].is_linked:
        raise AssertionError('Transparent extra pass does not consume the previous body surface')
    previous = instance.inputs['Previous Shader'].links[0].from_socket
    mix, = (node for node in layered.node_tree.nodes if node.label == 'Optional transparent FX over retained source surface')
    if mix.inputs[1].links[0].from_socket != previous or mix.inputs[2].links[0].from_socket != instance.outputs['Layer Shader']:
        raise AssertionError('Transparent extra-pass enable control lost its exact previous surface route')
    kernel, = (node.node_tree for node in effect_tree.nodes if node.type == 'GROUP'
               and node.label == 'Transparent source over previous surface / host approximation')
    _check_kernel(kernel)
    kernel_name = kernel.name
    package_material_authoring((layered,), layout_shader_node_tree)
    live = shader_dependencies(layered)
    controls = clothing_effect_controls(layered)
    if not controls or any((socket.id_data.as_pointer(), socket.node.as_pointer()) not in live.nodes for socket in controls):
        raise AssertionError('Final authoring packaging discarded the live body special-effect control')
    for obj in bpy.context.scene.objects:
        for slot in obj.material_slots:
            if slot.material == original:
                slot.material = layered
    saved_name = layered.name
    args.output.mkdir(parents=True, exist_ok=False)
    target = args.output / 'layer_regression.blend'
    bpy.context.preferences.filepaths.save_version = 0
    bpy.ops.wm.save_as_mainfile(filepath=str(target), check_existing=False)
    bpy.ops.wm.open_mainfile(filepath=str(target), use_scripts=False, load_ui=False)
    material = bpy.data.materials[saved_name]
    if not clothing_effect_controls(material):
        raise AssertionError('Saved/reopened layered material lost its body special-effect control')
    kernel = bpy.data.node_groups[kernel_name]
    _check_kernel(kernel)
    attenuation = kernel.nodes['Add Shader'].inputs[0].links[0].from_node
    destination = attenuation.inputs[0].links[0].from_socket
    kernel.links.remove(attenuation.inputs[0].links[0])
    try:
        _check_kernel(kernel)
    except AssertionError:
        pass
    else:
        raise AssertionError('Saved layered kernel accepted a disconnected destination factor')
    finally:
        kernel.links.new(destination, attenuation.inputs[0])
    _check_kernel(kernel)

    result = {'status': 'passed', 'rendered': False, 'actual_source_stack': identities,
              'opaque_route_unchanged': True, 'standalone_fx_route_unchanged': True,
              'source_premultiplied_once': True, 'destination_factor_preserved': True,
              'packaged_save_reopen': True, 'destination_factor_disconnection_rejected': True, 'blend': str(target),
              'scope': 'Blender surface-over approximation; native framebuffer equivalence is unverified.'}
    (args.output / 'receipt.json').write_text(json.dumps(result, indent=2), encoding='utf-8')
    print('CHARACTER_FX_LAYER_REGRESSION_PASSED', flush=True)


if __name__ == '__main__':
    main()
