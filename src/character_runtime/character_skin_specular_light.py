"""Build the compiled Skin SpecularOnly response using the verified punctual kernel.

Source mode 2 excludes diffuse, wrap and color normalization. Its three mode
parameters gate perceptual roughness, narrow the GGX alpha, and select metallic
surfaces. The input alpha blend is restricted to the verified interpolation
domain by the caller. Native packing, cookies and shadow producers are separate.
"""
from __future__ import annotations

import bpy

from character_node_graph import math_node, node
from character_weather_nodes import scalar_mix, smoothstep


def build_skin_specular_light(
    name: str, punctual: bpy.types.ShaderNodeTree,
) -> bpy.types.ShaderNodeTree:
    """Reuse the plain kernel with constant-unit wrap and zero diffuse color."""
    tree = bpy.data.node_groups.new(name, 'ShaderNodeTree')
    section = 'Source SpecularOnly Light'
    overridden = {'NPR Color', 'Wrap', 'Color Normalize', 'Specular Weight', 'Perceptual Roughness'}
    for item in punctual.interface.items_tree:
        if item.item_type == 'SOCKET' and item.in_out == 'INPUT' and item.name not in overridden:
            tree.interface.new_socket(name=item.name, in_out='INPUT', socket_type=item.socket_type)
    for label in ('Specular Weight', 'Perceptual Roughness', 'Metallic', 'Roughness Threshold', 'Alpha Narrowing', 'Metallic Gate'):
        tree.interface.new_socket(name=label, in_out='INPUT', socket_type='NodeSocketFloat')
    for label, kind in (('Contribution', 'NodeSocketColor'), ('Material Gate', 'NodeSocketFloat'), ('Alpha', 'NodeSocketFloat')):
        tree.interface.new_socket(name=label, in_out='OUTPUT', socket_type=kind)
    entry = node(tree, 'NodeGroupInput', 'Source material and light inputs', section)
    output = node(tree, 'NodeGroupOutput', 'Source SpecularOnly outputs', section)
    instance = node(tree, 'ShaderNodeGroup', 'Verified punctual GGX and attenuation', section)
    instance.node_tree = punctual
    for socket in entry.outputs:
        if socket.name in instance.inputs and socket.name not in overridden:
            tree.links.new(socket, instance.inputs[socket.name])
    v = entry.outputs
    lower = math_node(tree, 'ADD', v['Roughness Threshold'], .05, 'Roughness gate upper edge', section)
    upper = math_node(tree, 'SUBTRACT', v['Roughness Threshold'], .05, 'Roughness gate lower edge', section)
    rough_gate = smoothstep(tree, lower, upper, v['Perceptual Roughness'], 'Source reversed roughness gate')
    below = math_node(tree, 'LESS_THAN', v['Metallic'], .5, 'Source metallic below half', section)
    metal_step = math_node(tree, 'SUBTRACT', 1., below, 'Source metallic step including equality', section)
    metal_gate = scalar_mix(tree, v['Metallic Gate'], 1., metal_step, 'Source metallic gate interpolation')
    gate = math_node(tree, 'MULTIPLY', rough_gate, metal_gate, 'Source SpecularOnly material gate', section)
    weight = math_node(tree, 'MULTIPLY', v['Specular Weight'], gate, 'Source gated specular weight', section)
    alpha = math_node(tree, 'MAXIMUM', .0078125, math_node(tree, 'MULTIPLY', v['Perceptual Roughness'], v['Perceptual Roughness'], 'Source perceptual roughness squared', section), 'Source alpha floor', section)
    narrowed = scalar_mix(tree, v['Alpha Narrowing'], alpha, .01, 'Source SpecularOnly narrowed alpha')
    perceptual = math_node(tree, 'SQRT', narrowed, 0., 'Kernel perceptual roughness from source alpha', section)
    tree.links.new(weight, instance.inputs['Specular Weight'])
    tree.links.new(perceptual, instance.inputs['Perceptual Roughness'])
    instance.inputs['NPR Color'].default_value = (0., 0., 0., 1.)
    instance.inputs['Wrap'].default_value = 2.
    instance.inputs['Color Normalize'].default_value = 0.
    tree.links.new(instance.outputs['Contribution'], output.inputs['Contribution'])
    tree.links.new(gate, output.inputs['Material Gate'])
    tree.links.new(narrowed, output.inputs['Alpha'])
    for item in tree.nodes:
        item['endfield_layout_section'] = section
    tree['source_mode'] = 2
    tree['scope'] = 'Compiled Skin SpecularOnly response; explicit light inputs; not a native GPU record or shadow producer.'
    tree['alpha_narrowing_domain'] = 'Validated interpolation domain [0,1]; out-of-domain source records need explicit review.'
    return tree
