"""Distinct compiled Hair/Fur snow kernels sharing only established frame arithmetic."""
from __future__ import annotations

from collections.abc import Callable

import bpy

from character_node_graph import node, math_node, color_mix
from character_weather_nodes import (
    make_native_skin_snow, equation_node, smoothstep, scalar_mix, combine_vector,
)


SECTION = 'Hair and Fur Snow Equations'


def _coverage(tree: bpy.types.ShaderNodeTree, nonlinear: bpy.types.NodeSocket,
              normal_scale: float, normal_bias: float) -> bpy.types.NodeSocket:
    source = equation_node(tree, 'Explicit source varyings and state').outputs
    ny = equation_node(tree, 'Native orientation normal Y').outputs[0]
    sample = equation_node(tree, 'Native snow XY normal and Z coverage').outputs['Blue']
    response = math_node(tree, 'ADD', math_node(tree, 'ADD',
        math_node(tree, 'MULTIPLY', ny, normal_scale, 'Family normal coverage scale', SECTION),
        normal_bias, 'Family normal coverage bias', SECTION), sample, 'Family snow response', SECTION)
    coverage = smoothstep(tree,
        math_node(tree, 'SUBTRACT', 2.0, nonlinear, 'Family coverage low', SECTION),
        math_node(tree, 'SUBTRACT', 2.3499999046325684, nonlinear, 'Family coverage high', SECTION),
        response, 'Family snow coverage')
    enabled = equation_node(tree, 'Native snow branch enabled').outputs[0]
    return math_node(tree, 'MULTIPLY', coverage,
        math_node(tree, 'MULTIPLY', source['Front Facing'], enabled, 'Family front and gate', SECTION),
        'Family gated coverage', SECTION)


def _replace_coverage(tree: bpy.types.ShaderNodeTree, coverage: bpy.types.NodeSocket) -> None:
    original = equation_node(tree, 'Native branch-gated coverage').outputs[0]
    for target in tuple(link.to_socket for link in original.links):
        tree.links.new(coverage, target)


def _finish(tree: bpy.types.ShaderNodeTree, layout: Callable[[bpy.types.NodeTree], None]) -> None:
    """Preserve the exact incoming surface when the native snow branch is inactive."""
    entry = equation_node(tree, 'Explicit source varyings and state')
    output = equation_node(tree, 'Native snow material channels')
    enabled = equation_node(tree, 'Native snow branch enabled').outputs[0]
    for label in ('Albedo', 'Shadow Color'):
        current = output.inputs[label].links[0].from_socket
        selected = color_mix(tree, 'MIX', enabled, entry.outputs[label], current,
                             'Inactive family branch ' + label, SECTION)
        tree.links.new(selected, output.inputs[label])
    for label in ('Roughness', 'Metallic'):
        current = output.inputs[label].links[0].from_socket
        tree.links.new(scalar_mix(tree, enabled, entry.outputs[label], current,
                                 'Inactive family branch ' + label), output.inputs[label])
    current = node(tree, 'ShaderNodeSeparateXYZ', 'Computed family snow normal', SECTION)
    original = node(tree, 'ShaderNodeSeparateXYZ', 'Original family surface normal', SECTION)
    tree.links.new(output.inputs['Normal'].links[0].from_socket, current.inputs[0])
    tree.links.new(entry.outputs['Geometric Normal'], original.inputs[0])
    selected = combine_vector(tree, tuple(scalar_mix(tree, enabled, original.outputs[axis],
        current.outputs[axis], 'Inactive family normal ' + axis) for axis in 'XYZ'), 'Family branch normal')
    tree.links.new(selected, output.inputs['Normal'])
    # Remove unused Skin-specific math after replacing the family response.
    retained = {entry, output}
    pending = [output]
    while pending:
        item = pending.pop()
        for socket in item.inputs:
            for link in socket.links:
                if link.from_node not in retained:
                    retained.add(link.from_node)
                    pending.append(link.from_node)
    for item in tuple(tree.nodes):
        if item.type != 'FRAME' and item not in retained:
            tree.nodes.remove(item)
    tree['endfield_family_snow_revision'] = 1
    tree['binding_scope'] = ('Compiled local surface equations; explicit host weather and projection inputs. '
                             'No native runtime weather writer, caustic or live per-draw state claim.')
    for label in ('Base Texture Alpha', 'Snow Coverage Mask'):
        tree.interface.items_tree[label].description = 'Unused by this family; coverage uses Metallic Gloss B squared.'
    tree.interface.items_tree['Metallic Gloss B'].description = 'Actual source MetallicGlossMap blue channel, squared inside the kernel.'
    layout(tree)


def make_native_hair_snow(image: bpy.types.Image, source_shader_sha256: str,
                          layout: Callable[[bpy.types.NodeTree], None]) -> bpy.types.ShaderNodeTree:
    """Mapped Hair uses triplanar snow, MapB squared and no roughness override."""
    tree = make_native_skin_snow(image, source_shader_sha256, layout)
    tree.name = 'EF Native Hair Snow - Compiled Equations'
    tree['source_formula'] = ('HGRP/CharacterNPR_Hair mapped ForwardLit blob126/131: triplanar; '
                              '.4*Ny+.6+B; position-Y intensity boost; MetallicGlossMap.B squared')
    tree['snow_family'] = 'Hair'
    nonlinear = equation_node(tree, 'Native nonlinear snow intensity').outputs[0]
    tree.interface.new_socket(name='Metallic Gloss B', in_out='INPUT', socket_type='NodeSocketFloat')
    entry = equation_node(tree, 'Explicit source varyings and state')
    mask = math_node(tree, 'MULTIPLY', entry.outputs['Metallic Gloss B'], entry.outputs['Metallic Gloss B'],
                     'Hair metallic-gloss B squared', SECTION)
    _replace_coverage(tree, math_node(tree, 'MULTIPLY',
        _coverage(tree, nonlinear, 0.4000000059604645, 0.6000000238418579), mask,
        'Hair mapped snow coverage', SECTION))
    output = equation_node(tree, 'Native snow material channels')
    for label in ('Roughness', 'Metallic'):
        tree.links.new(entry.outputs[label], output.inputs[label])
    _finish(tree, layout)
    return tree


def make_native_fur_snow(image: bpy.types.Image, source_shader_sha256: str,
                         layout: Callable[[bpy.types.NodeTree], None]) -> bpy.types.ShaderNodeTree:
    """Fur samples UV snow once and masks coverage with MetallicGlossMap.B squared."""
    tree = make_native_skin_snow(image, source_shader_sha256, layout)
    tree.name = 'EF Native Fur Snow - Compiled Equations'
    tree['source_formula'] = ('HGRP/CharacterNPR _CHARACTER_FUR ForwardLit blob514: UV snow; '
                              'raw snow intensity; .65*Ny+.35+B; MetallicGlossMap.B squared; alpha darkening')
    tree['snow_family'] = 'Fur'
    tree.interface.new_socket(name='UV', in_out='INPUT', socket_type='NodeSocketVector')
    tree.interface.items_tree['UV'].description = 'Source BaseMap UV after BaseMap_ST; global snow tiling is applied inside this group.'
    tree.interface.items_tree['Projection Position'].description = 'Unused by Fur: snow is sampled with transformed BaseMap UV.'
    tree.interface.new_socket(name='Metallic Gloss B', in_out='INPUT', socket_type='NodeSocketFloat')
    entry = equation_node(tree, 'Explicit source varyings and state')
    source = entry.outputs
    texture = node(tree, 'ShaderNodeTexImage', 'Fur source UV snow', SECTION)
    texture.image = image
    texture.extension = 'REPEAT'
    texture.interpolation = 'Linear'
    scaled = node(tree, 'ShaderNodeVectorMath', 'Fur snow UV tiling', SECTION)
    scaled.operation = 'SCALE'
    tree.links.new(source['UV'], scaled.inputs[0])
    tree.links.new(source['Global Tiling'], scaled.inputs['Scale'])
    tree.links.new(scaled.outputs[0], texture.inputs['Vector'])
    channels = equation_node(tree, 'Native snow XY normal and Z coverage')
    tree.links.new(texture.outputs['Color'], channels.inputs[0])
    output = equation_node(tree, 'Native snow material channels')
    tree.links.new(texture.outputs['Color'], output.inputs['Triplanar Sample'])
    tree.interface.items_tree['Triplanar Sample'].description = 'Fur: single UV snow sample; no triplanar projection.'
    intensity = source['Snow Intensity']
    nonlinear = math_node(tree, 'MULTIPLY', intensity,
        math_node(tree, 'SUBTRACT', 2.0, intensity, 'Fur intensity complement', SECTION),
        'Fur raw intensity curve', SECTION)
    mask = math_node(tree, 'MULTIPLY', source['Metallic Gloss B'], source['Metallic Gloss B'],
                     'Fur metallic-gloss B squared', SECTION)
    coverage = math_node(tree, 'MULTIPLY', _coverage(tree, nonlinear, 0.6499999761581421,
                                                   0.3499999940395355), mask,
                         'Fur masked snow coverage', SECTION)
    _replace_coverage(tree, coverage)
    luminance = node(tree, 'ShaderNodeVectorMath', 'Fur albedo luminance', SECTION)
    luminance.operation = 'DOT_PRODUCT'
    tree.links.new(source['Albedo'], luminance.inputs[0])
    luminance.inputs[1].default_value = (0.2126729041337967, 0.7151522040367126, 0.07217500358819962)
    nonmetal = math_node(tree, 'SUBTRACT', 1.0, source['Metallic'], 'Fur nonmetal', SECTION)
    value = math_node(tree, 'MULTIPLY', luminance.outputs['Value'], nonmetal, 'Fur nonmetal luminance', SECTION)
    bright = math_node(tree, 'SUBTRACT', 1.0,
        smoothstep(tree, 0.3499999940395355, 0.10000000149011612, value, 'Fur darkening luminance'),
        'Fur bright-surface darkening', SECTION)
    ny = equation_node(tree, 'Native orientation normal Y').outputs[0]
    orientation = math_node(tree, 'ADD', math_node(tree, 'MULTIPLY', ny, .25,
        'Fur darkening normal scale', SECTION), .75, 'Fur darkening normal bias', SECTION)
    factor = math_node(tree, 'MULTIPLY', math_node(tree, 'MULTIPLY', intensity, bright,
        'Fur intensity darkening', SECTION), math_node(tree, 'MULTIPLY', texture.outputs['Alpha'],
        orientation, 'Fur alpha orientation darkening', SECTION), 'Fur precoverage darkening', SECTION)
    scale = scalar_mix(tree, factor, 1.0, .5, 'Fur precoverage color scale')
    rgb = combine_vector(tree, (scale, scale, scale), 'Fur precoverage RGB scale')
    for label, target in (('Albedo', 'Native snow albedo'), ('Shadow Color', 'Native snow shadow color')):
        darkened = color_mix(tree, 'MULTIPLY', 1.0, source[label], rgb, 'Fur darkened ' + label, SECTION)
        tree.links.new(darkened, equation_node(tree, target).inputs[1])
    _finish(tree, layout)
    return tree
