"""Insert compiled Fur wet channels before the existing source snow surface."""
from __future__ import annotations

from collections.abc import Callable
import json
import math

import bpy

from attach_character_weather_surface import linked_source
from character_node_graph import node


def attach_fur_wet_before_snow(tree: bpy.types.ShaderNodeTree,
                               snow: bpy.types.Node, state: bpy.types.Node,
                               shader_sha256: str,
                               layout: Callable[[bpy.types.NodeTree], None]) -> dict[str, object]:
    """Reuse the verified family selection and shared explicit weather state."""
    if snow.node_tree.get('snow_family') != 'Fur' or snow.node_tree.get('source_shader_sha256') != shader_sha256:
        raise ValueError('Fur wet attachment requires the matching compiled Fur snow variant')
    if tree.get('fur_wet_binding'):
        raise ValueError('Fur wet surface is already attached: ' + tree.name)
    floats = json.loads(tree['source_floats'])
    values = {name: float(floats[key]) for name, key in
              (('Disable Rain', '_DisableRainEffectOnMaterial'), ('Fur TT', '_FurTTIntensity'))}
    if not all(math.isfinite(value) for value in values.values()):
        raise ValueError('Fur wet source material parameters must be finite: ' + tree.name)
    sources = {name: linked_source(snow.inputs[name])
               for name in ('Albedo', 'Shadow Color', 'Roughness', 'Metallic')}
    from character_fur_wet import make_native_fur_wet
    wet = node(tree, 'ShaderNodeGroup', 'Compiled Fur wet surface before snow', 'Fur Wet Surface')
    wet.node_tree = make_native_fur_wet(shader_sha256, layout)
    for name, source in sources.items():
        tree.links.new(source, wet.inputs[name])
    controls = {'Rain Intensity': 'Native Rain Intensity',
                'Water Submersion': 'Native Water Submersion',
                'Global Wetness': 'Native Global Wetness'}
    for name, output in controls.items():
        tree.links.new(state.outputs[output], wet.inputs[name])
    for name, value in values.items():
        wet.inputs[name].default_value = value
    for name in ('Albedo', 'Shadow Color', 'Roughness'):
        tree.links.new(wet.outputs[name], snow.inputs[name])
    tree['fur_wet_binding'] = 'HGRPFurWetBeforeSnow/1'
    return {'format': 'HGRPFurWetBeforeSnow/1', 'instance': wet.name,
            'kernel': wet.node_tree.name, 'snow': snow.name, 'state': state.name,
            'shader_sha256': shader_sha256, 'material_values': values,
            'controls': controls,
            'sources': {name: {'node': socket.node.name,
                'output_index': list(socket.node.outputs).index(socket)} for name, socket in sources.items()},
            'live_outputs': ['Albedo', 'Shadow Color', 'Roughness'],
            'fur_tt_consumer': False,
            'height_scope': 'Shared explicit host world height and weather controls; no live Fur draw capture.',
            'lighting_scope': 'Retained host Fur PBR/artist diffuse; native FurTT lighting remains unconnected.'}


def verify_fur_wet_before_snow(tree: bpy.types.ShaderNodeTree,
                               receipt: dict[str, object]) -> dict[str, object]:
    """Verify actual dry inputs, wet-to-snow outputs and shared control links."""
    if receipt['format'] != 'HGRPFurWetBeforeSnow/1' or tree.get('fur_wet_binding') != receipt['format']:
        raise AssertionError('Saved Fur wet binding identity differs')
    wet, snow, state = (tree.nodes[receipt[name]] for name in ('instance', 'snow', 'state'))
    if wet.node_tree.name != receipt['kernel'] or wet.node_tree.get('source_shader_sha256') != receipt['shader_sha256']:
        raise AssertionError('Saved Fur wet kernel differs from its verified source')
    for name, row in receipt['sources'].items():
        source = tree.nodes[row['node']].outputs[row['output_index']]
        if linked_source(wet.inputs[name]) != source:
            raise AssertionError('Saved Fur wet dry source disconnected: ' + name)
        if name == 'Metallic' and linked_source(snow.inputs[name]) != source:
            raise AssertionError('Fur wet must preserve the original metallic input')
    for name, output in receipt['controls'].items():
        if linked_source(wet.inputs[name]) != state.outputs[output]:
            raise AssertionError('Saved Fur wet shared control disconnected: ' + name)
    for name, value in receipt['material_values'].items():
        if wet.inputs[name].is_linked or not math.isclose(wet.inputs[name].default_value, value, abs_tol=1e-7):
            raise AssertionError('Saved Fur wet source material parameter differs: ' + name)
    for name in receipt['live_outputs']:
        if linked_source(snow.inputs[name]) != wet.outputs[name]:
            raise AssertionError('Saved Fur wet output no longer feeds snow: ' + name)
    if wet.outputs['Fur TT'].is_linked:
        raise AssertionError('FurTT has no verified host lighting consumer')
    return {'status': 'passed', 'live_wet_to_snow_channels': len(receipt['live_outputs']),
            'fur_tt_consumer': False}
