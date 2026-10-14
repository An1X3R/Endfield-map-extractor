"""Current compiled Skin mode-1 Ramped light, with distinct body and SDF ramps.

Lit Albedo and Shadow Albedo are the unscaled post-weather colors, before
the source .96*(1-metallic) factor. Neither input is accumulated NPR Color.
Normal is the body surface normal or the face SDF-blended surface normal;
Specular Normal remains the source GGX normal. SDF Lightmap Alpha is the sampled
SDFLightmap.a (_1530), not SDFMask.a. Source Shadow Visibility is the supplied
per-light _2584/_3141; atlas sampling and GPU descriptor packing are external.
The existing punctual kernel supplies host attenuation and the verified GGX.
"""
from __future__ import annotations

import bpy

from character_node_graph import color_mix, math_node, node
from character_weather_nodes import scalar_mix, smoothstep, vector_math


SECTION = 'Skin Ramped Light'
EXCLUDED = frozenset(('NPR Color', 'Wrap', 'Wrap Normal', 'Color Normalize', 'Global Normalize Scale', 'Visibility'))


def _base(name: str, punctual: bpy.types.ShaderNodeTree, extra: tuple[str, ...]) -> tuple[bpy.types.ShaderNodeTree, bpy.types.Node, bpy.types.Node, bpy.types.Node]:
    if not isinstance(name, str) or not name.strip():
        raise ValueError('Skin Ramped group name must be nonempty')
    required = {'Attenuation', 'GGX'}
    actual = {item.name for item in punctual.interface.items_tree if item.item_type == 'SOCKET' and item.in_out == 'OUTPUT'}
    if not required.issubset(actual):
        raise ValueError(f'Punctual kernel is missing required outputs: {sorted(required - actual)}')
    tree = bpy.data.node_groups.new(name, 'ShaderNodeTree')
    for item in punctual.interface.items_tree:
        if item.item_type == 'SOCKET' and item.in_out == 'INPUT' and item.name not in EXCLUDED:
            tree.interface.new_socket(name=item.name, in_out='INPUT', socket_type=item.socket_type)
    for label in ('Lit Albedo', 'Shadow Albedo'):
        socket = tree.interface.new_socket(name=label, in_out='INPUT', socket_type='NodeSocketColor')
        socket.description = 'Unscaled source post-weather color before .96*(1-metallic); never accumulated NPR Color.'
    for label in ('Metallic', 'Ramp Offset', 'Base Response Scale', 'Source Shadow Visibility', *extra):
        socket = tree.interface.new_socket(name=label, in_out='INPUT', socket_type='NodeSocketFloat')
        if label == 'SDF Lightmap Alpha':
            socket.description = 'Actual mirrored SDFLightmap.a (_1530); not SDFMask.a.'
    for label, kind in (('Contribution', 'NodeSocketColor'), ('Diffuse Response', 'NodeSocketColor'), ('Specular Response', 'NodeSocketColor'), ('Ramp', 'NodeSocketFloat'), ('Attenuation', 'NodeSocketFloat'), ('GGX', 'NodeSocketFloat')):
        tree.interface.new_socket(name=label, in_out='OUTPUT', socket_type=kind)
    entry = node(tree, 'NodeGroupInput', 'Explicit source Ramped inputs', SECTION)
    output = node(tree, 'NodeGroupOutput', 'Compiled Ramped contribution', SECTION)
    kernel = node(tree, 'ShaderNodeGroup', 'Verified punctual attenuation and GGX', SECTION)
    kernel.node_tree = punctual
    for value in entry.outputs:
        if value.name in kernel.inputs:
            tree.links.new(value, kernel.inputs[value.name])
    for label, value in (('Visibility', 1.), ('Wrap', 2.), ('Color Normalize', 0.), ('Global Normalize Scale', 1.)):
        kernel.inputs[label].default_value = value
    kernel.inputs['NPR Color'].default_value = (0., 0., 0., 1.)
    tree['source_mode'] = 1
    tree['scope'] = 'Compiled Skin Ramped arithmetic; host punctual attenuation; supplied per-light shadow visibility; no native atlas or light-record capture.'
    return tree, entry, output, kernel


def _normal_dot(tree: bpy.types.ShaderNodeTree, entry: bpy.types.Node) -> bpy.types.NodeSocket:
    v = entry.outputs
    delta = vector_math(tree, 'SUBTRACT', v['Light Position'], v['Position'], 'Ramped light delta').outputs[0]
    point = vector_math(tree, 'NORMALIZE', delta, (0., 0., 0.), 'Ramped point direction').outputs[0]
    axis = vector_math(tree, 'NORMALIZE', v['Light Axis'], (0., 0., 0.), 'Ramped host light axis').outputs[0]
    selected = color_mix(tree, 'MIX', v['Is Directional'], point, axis, 'Ramped selected direction', SECTION)
    direction = vector_math(tree, 'NORMALIZE', selected, (0., 0., 0.), 'Ramped normalized direction').outputs[0]
    normal = vector_math(tree, 'NORMALIZE', v['Normal'], (0., 0., 0.), 'Ramped source normal').outputs[0]
    ndl = vector_math(tree, 'DOT_PRODUCT', normal, direction, 'Ramped source NdotL').outputs['Value']
    shifted = math_node(tree, 'ADD', ndl, v['Ramp Offset'], 'Source Ramped NdotL offset', SECTION)
    return math_node(tree, 'MINIMUM', 1., math_node(tree, 'MAXIMUM', -1., shifted, 'Source Ramped lower clamp', SECTION), 'Source Ramped upper clamp', SECTION)


def _finish(tree: bpy.types.ShaderNodeTree, entry: bpy.types.Node, output: bpy.types.Node, kernel: bpy.types.Node, ramp: bpy.types.NodeSocket) -> None:
    v = entry.outputs
    k = math_node(tree, 'MULTIPLY', .96, math_node(tree, 'SUBTRACT', 1., v['Metallic'], 'Source nonmetal fraction', SECTION), 'Source .96 nonmetal diffuse factor', SECTION)
    dark = color_mix(tree, 'MULTIPLY', 1., v['Shadow Albedo'], math_node(tree, 'MULTIPLY', k, v['Base Response Scale'], 'Source Ramped base scale', SECTION), 'Source scaled base response', SECTION)
    lit = color_mix(tree, 'MULTIPLY', 1., v['Lit Albedo'], k, 'Source Ramped lit-albedo endpoint', SECTION)
    diffuse = color_mix(tree, 'MIX', ramp, dark, lit, 'Source Ramped diffuse interpolation', SECTION)
    spec_scale = math_node(tree, 'MULTIPLY', kernel.outputs['GGX'], math_node(tree, 'MULTIPLY', v['Specular Weight'], ramp, 'Source Ramped specular ramp weight', SECTION), 'Source Ramped weighted GGX', SECTION)
    specular = color_mix(tree, 'MULTIPLY', 1., v['Specular Color'], spec_scale, 'Source Ramped specular response', SECTION)
    energy = math_node(tree, 'MULTIPLY', kernel.outputs['Attenuation'], math_node(tree, 'MULTIPLY', v['Energy'], v['Energy Scale'], 'Host explicit energy conversion', SECTION), 'Ramped attenuated energy', SECTION)
    radiance = color_mix(tree, 'MULTIPLY', 1., v['Light Color'], energy, 'Source Ramped radiance', SECTION)
    total = color_mix(tree, 'MULTIPLY', 1., radiance, color_mix(tree, 'ADD', 1., diffuse, specular, 'Source Ramped diffuse plus specular', SECTION), 'Source Ramped light contribution', SECTION)
    for label, value in (('Contribution', total), ('Diffuse Response', diffuse), ('Specular Response', specular), ('Ramp', ramp), ('Attenuation', kernel.outputs['Attenuation']), ('GGX', kernel.outputs['GGX'])):
        tree.links.new(value, output.inputs[label])
    for item in tree.nodes:
        item['endfield_layout_section'] = SECTION


def build_body_skin_ramped_light(name: str, punctual: bpy.types.ShaderNodeTree) -> bpy.types.ShaderNodeTree:
    """Build the non-SDF body ramp, current fragment lines 40702-40756."""
    tree, entry, output, kernel = _base(name, punctual, ())
    ndl = _normal_dot(tree, entry)
    ramp = math_node(tree, 'MULTIPLY', math_node(tree, 'MAXIMUM', 0., ndl, 'Source body ramp saturation', SECTION), entry.outputs['Source Shadow Visibility'], 'Source body shadowed ramp', SECTION)
    _finish(tree, entry, output, kernel, ramp)
    tree['source_branch'] = 'Skin no-SDF body; current 0125 fragment 40336-40340, 40702-40756'
    return tree


def build_face_skin_ramped_light(name: str, punctual: bpy.types.ShaderNodeTree) -> bpy.types.ShaderNodeTree:
    """Build the SDF face max ramp, current fragment lines 85480-85534."""
    tree, entry, output, kernel = _base(name, punctual, ('SDF Mask G', 'SDF Lightmap Alpha', 'Face Ramp Bias', 'Face Alpha Threshold'))
    v = entry.outputs
    ndl = _normal_dot(tree, entry)
    alpha_ramp = smoothstep(tree, 0., -.2, math_node(tree, 'SUBTRACT', v['SDF Lightmap Alpha'], v['Face Alpha Threshold'], 'Source SDF alpha threshold difference', SECTION), 'Source reversed SDF alpha ramp')
    width = scalar_mix(tree, v['SDF Mask G'], .1, 1., 'Source SDF Mask G ramp width')
    bias = math_node(tree, 'MULTIPLY', v['Face Ramp Bias'], math_node(tree, 'SUBTRACT', 1., v['SDF Mask G'], 'Source SDF complement', SECTION), 'Source SDF ramp bias', SECTION)
    normal_ramp = smoothstep(tree, 0., width, math_node(tree, 'ADD', ndl, bias, 'Source face normal plus SDF bias', SECTION), 'Source face normal ramp')
    shadowed = math_node(tree, 'MULTIPLY', normal_ramp, v['Source Shadow Visibility'], 'Source shadowed face normal ramp', SECTION)
    ramp = math_node(tree, 'MAXIMUM', alpha_ramp, shadowed, 'Source maximum SDF and normal ramps', SECTION)
    _finish(tree, entry, output, kernel, ramp)
    tree['source_branch'] = 'Skin SDF face; current 0269 fragment 85086-85090, 85114-85119, 85480-85534'
    return tree
