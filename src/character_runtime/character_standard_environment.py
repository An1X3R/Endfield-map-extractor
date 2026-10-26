"""Optional uniform host environment specular for mapped Standard materials.

The split-sum response is independently expressed as scalar node arithmetic.
Uniform radiance needs no directional prefilter. No native cubemap, scene
exposure capture, anisotropic IBL, or clearcoat environment lobe is claimed.
"""
from __future__ import annotations

from collections.abc import Callable
import math
from typing import TypedDict

import bpy

from character_node_graph import color_mix, math_node, node
from character_standard_lighting import LABEL as MAIN_LABEL
from patch_character_hgrp import by_label, source_keywords


Scalar = bpy.types.NodeSocket | float
SECTION = 'Host Environment Specular'
LABEL = 'Uniform host Standard environment specular'
CONTROL_MARKER = 'endfield_uniform_environment_controls'
SCOPE = ('Uniform linear host environment radiance with isotropic split-sum response; '
         'not a captured native cubemap or complete special-specular reconstruction.')


class EnvironmentAttachment(TypedDict):
    group: str
    controls: str
    kernel: str
    lighting_fidelity: str
    scope: str
    weighted_by_existing_npr_blend: bool


def create_standard_environment_controls(
    name: str, radiance: tuple[float, float, float], exposure: float,
) -> bpy.types.ShaderNodeTree:
    """Create one explicitly named, shared, editable linear-radiance controller."""
    if not name.strip() or name in bpy.data.node_groups:
        raise ValueError(f'Environment control name must be new and nonempty: {name}')
    if len(radiance) != 3 or not all(math.isfinite(value) and value >= 0. for value in radiance):
        raise ValueError(f'Host environment radiance requires three finite nonnegative channels: {radiance}')
    if not math.isfinite(exposure) or exposure < 0.:
        raise ValueError(f'Host environment exposure must be finite and nonnegative: {exposure}')
    tree = bpy.data.node_groups.new(name, 'ShaderNodeTree')
    tree[CONTROL_MARKER] = 1
    tree['scope'] = SCOPE
    for label, kind in (('Environment Radiance', 'NodeSocketColor'), ('Host Exposure', 'NodeSocketFloat')):
        tree.interface.new_socket(name=label, in_out='OUTPUT', socket_type=kind)
    output = node(tree, 'NodeGroupOutput', 'Shared host environment outputs', SECTION)
    color = node(tree, 'ShaderNodeRGB', 'Uniform environment radiance - linear host setting', SECTION)
    color.outputs[0].default_value = (*radiance, 1.)
    level = node(tree, 'ShaderNodeValue', 'Host exposure - explicit setting', SECTION)
    level.outputs[0].default_value = exposure
    tree.links.new(color.outputs[0], output.inputs['Environment Radiance'])
    tree.links.new(level.outputs[0], output.inputs['Host Exposure'])
    return tree


def _math(tree: bpy.types.NodeTree, operation: str, left: Scalar, right: Scalar, label: str) -> bpy.types.NodeSocket:
    return math_node(tree, operation, left, right, label, SECTION)


def _sum_terms(
    tree: bpy.types.NodeTree, terms: tuple[tuple[float, Scalar], ...], constant: float, label: str,
) -> Scalar:
    result: Scalar = constant
    for index, (coefficient, value) in enumerate(terms):
        weighted = _math(tree, 'MULTIPLY', coefficient, value, f'{label} term {index}')
        result = _math(tree, 'ADD', result, weighted, f'{label} sum {index}')
    return result


def _dfg(tree: bpy.types.NodeTree, nv: bpy.types.NodeSocket, rough: bpy.types.NodeSocket) -> tuple[bpy.types.NodeSocket, bpy.types.NodeSocket]:
    nv2 = _math(tree, 'MULTIPLY', nv, nv, 'Environment NoV squared')
    nv3 = _math(tree, 'MULTIPLY', nv2, nv, 'Environment NoV cubed')
    r2 = _math(tree, 'MULTIPLY', rough, rough, 'Environment roughness squared')
    r4 = _math(tree, 'MULTIPLY', r2, r2, 'Environment roughness fourth')
    r6 = _math(tree, 'MULTIPLY', r4, r2, 'Environment roughness sixth')
    x0 = _sum_terms(tree, ((3.32707, nv),), .0365463, 'DFG X numerator constant')
    x1 = _sum_terms(tree, ((-9.04756, nv),), 9.0632, 'DFG X numerator roughness')
    xd0 = _sum_terms(tree, ((3.59685, nv2), (-1.36772, nv3)), 1., 'DFG X denominator constant')
    xd1 = _sum_terms(tree, ((-16.3174, nv2), (9.22949, nv3)), 9.04401, 'DFG X denominator roughness')
    xd2 = _sum_terms(tree, ((19.7886, nv2), (-20.2123, nv3)), 5.56589, 'DFG X denominator sixth')
    y0 = _sum_terms(tree, ((-1.28514, nv),), .99044, 'DFG Y numerator constant')
    y1 = _sum_terms(tree, ((-.755907, nv),), 1.29678, 'DFG Y numerator roughness')
    yd0 = _sum_terms(tree, ((2.92338, nv), (59.4188, nv3)), 1., 'DFG Y denominator constant')
    yd1 = _sum_terms(tree, ((-27.0302, nv), (222.592, nv3)), 20.3225, 'DFG Y denominator roughness')
    yd2 = _sum_terms(tree, ((626.13, nv), (316.627, nv3)), 121.563, 'DFG Y denominator sixth')
    results: list[bpy.types.NodeSocket] = []
    for label, n0, n1, d0, d1, d2 in (
        ('X', x0, x1, xd0, xd1, xd2), ('Y', y0, y1, yd0, yd1, yd2),
    ):
        numerator = _math(tree, 'ADD', n0, _math(tree, 'MULTIPLY', n1, r2, f'DFG {label} numerator weight'), f'DFG {label} numerator')
        denominator = _math(tree, 'ADD', d0, _math(tree, 'MULTIPLY', d1, r2, f'DFG {label} denominator square weight'), f'DFG {label} denominator base')
        denominator = _math(tree, 'ADD', denominator, _math(tree, 'MULTIPLY', d2, r6, f'DFG {label} denominator sixth weight'), f'DFG {label} denominator')
        guarded = _math(tree, 'MAXIMUM', denominator, 1e-6, f'DFG {label} positive denominator guard')
        results.append(_math(tree, 'DIVIDE', numerator, guarded, f'DFG {label}'))
    return results[0], results[1]


def build_standard_environment_brdf(name: str) -> bpy.types.ShaderNodeTree:
    """Build a bounded isotropic response to explicit uniform environment radiance."""
    if not name.strip():
        raise ValueError('Environment BRDF group name is empty')
    tree = bpy.data.node_groups.new(name, 'ShaderNodeTree')
    colors = ('Environment Radiance', 'Ambient Color', 'F0', 'Mapped Specular Color')
    scalars = ('NdotV', 'Perceptual Roughness', 'Iridescent Mode', 'Ambient Diffuse Intensity', 'Host Exposure', 'CP0 W')
    for label in (*colors, *scalars):
        tree.interface.new_socket(name=label, in_out='INPUT', socket_type='NodeSocketColor' if label in colors else 'NodeSocketFloat')
    for label, kind in (('Contribution', 'NodeSocketColor'), ('DFG X', 'NodeSocketFloat'), ('DFG Y', 'NodeSocketFloat')):
        tree.interface.new_socket(name=label, in_out='OUTPUT', socket_type=kind)
    source = node(tree, 'NodeGroupInput', 'Explicit host environment and material inputs', SECTION)
    output = node(tree, 'NodeGroupOutput', 'Uniform environment result', SECTION)
    values = source.outputs
    bounded: dict[str, bpy.types.NodeSocket] = {}
    for label in ('NdotV', 'Perceptual Roughness', 'Iridescent Mode'):
        lower = _math(tree, 'MAXIMUM', values[label], 0., label + ' nonnegative')
        bounded[label] = _math(tree, 'MINIMUM', lower, 1., label + ' upper bound')
    dfg_x, dfg_y = _dfg(tree, bounded['NdotV'], bounded['Perceptual Roughness'])
    env_f0 = color_mix(tree, 'MIX', bounded['Iridescent Mode'], values['F0'], values['Mapped Specular Color'], 'Environment SpecRamp interpolation', SECTION)
    weighted = color_mix(tree, 'MULTIPLY', 1., env_f0, dfg_x, 'Environment F0 times DFG X', SECTION)
    response = color_mix(tree, 'ADD', 1., weighted, dfg_y, 'Environment split-sum response', SECTION)
    total = _math(tree, 'ADD', dfg_x, dfg_y, 'Environment integrated reflectance')
    guard = _math(tree, 'MAXIMUM', total, 1e-6, 'Environment reflectance denominator guard')
    boost = _math(tree, 'DIVIDE', _math(tree, 'SUBTRACT', 1., total, 'Environment unreturned energy'), guard, 'Environment multiple-scattering compensation')
    energy = color_mix(tree, 'MULTIPLY', 1., env_f0, boost, 'Environment F0 compensation', SECTION)
    energy = color_mix(tree, 'ADD', 1., energy, (1., 1., 1., 1.), 'Environment compensated energy', SECTION)
    result = color_mix(tree, 'MULTIPLY', 1., response, energy, 'Compensated environment BRDF', SECTION)
    for label in ('Environment Radiance', 'Ambient Color'):
        result = color_mix(tree, 'MULTIPLY', 1., result, values[label], 'Environment times ' + label, SECTION)
    exposure = _math(tree, 'MINIMUM', _math(tree, 'MAXIMUM', values['Host Exposure'], .5, 'Environment exposure floor'), 1.5, 'Environment exposure ceiling')
    strength = _math(tree, 'MULTIPLY', values['Ambient Diffuse Intensity'], exposure, 'Environment ambient exposure')
    strength = _math(tree, 'MULTIPLY', strength, values['CP0 W'], 'Environment dark-scale weight')
    result = color_mix(tree, 'MULTIPLY', 1., result, strength, 'Uniform host environment contribution', SECTION)
    for label, socket in (('Contribution', result), ('DFG X', dfg_x), ('DFG Y', dfg_y)):
        tree.links.new(socket, output.inputs[label])
    tree['scope'] = SCOPE
    return tree


def _source(item: bpy.types.Node, label: str) -> bpy.types.NodeSocket:
    links = tuple(item.inputs[label].links)
    if len(links) != 1:
        raise ValueError(f'Environment requires one live input: {item.label}/{label}')
    return links[0].from_socket


def validate_standard_environment_controls(controls: bpy.types.ShaderNodeTree) -> None:
    """Reject invalid edited host settings before attachment, saving, or rendering."""
    if controls.get(CONTROL_MARKER) != 1:
        raise ValueError(f'Environment attachment requires explicit host controls: {controls.name}')
    color = by_label(controls, 'Uniform environment radiance - linear host setting')
    exposure = by_label(controls, 'Host exposure - explicit setting')
    if color.type != 'RGB' or exposure.type != 'VALUE':
        raise ValueError(f'Host environment controls lost their editable constant nodes: {controls.name}')
    rgb = tuple(float(value) for value in color.outputs[0].default_value[:3])
    value = float(exposure.outputs[0].default_value)
    if not all(math.isfinite(channel) and channel >= 0. for channel in rgb):
        raise ValueError(f'Edited host environment radiance is invalid: controls={controls.name}, radiance={rgb}')
    if not math.isfinite(value) or value < 0.:
        raise ValueError(f'Edited host environment exposure is invalid: controls={controls.name}, exposure={value}')


def attach_standard_environment_specular(
    tree: bpy.types.ShaderNodeTree, controls: bpy.types.ShaderNodeTree,
    layout: Callable[[bpy.types.NodeTree], None],
) -> EnvironmentAttachment:
    """Append optional radiance only to the existing NPR color bus."""
    if tree.get('source_shader') != 'HGRP/CharacterNPR':
        raise ValueError(f'Environment attachment requires a Standard source material: {tree.name}')
    keywords = set(source_keywords(tree))
    if '_CHARACTER_FUR' in keywords:
        raise ValueError(f'Environment attachment requires a non-Fur Standard source material: {tree.name}')
    if not {'_METALLICSPECGLOSSMAP', '_SPEC_RAMP_ON'} <= keywords:
        raise ValueError(f'Environment attachment requires mapped Standard metal/spec-ramp source inputs: {tree.name}')
    if any(item.label == LABEL for item in tree.nodes):
        raise ValueError(f'Environment specular is already attached: {tree.name}')
    validate_standard_environment_controls(controls)
    main = by_label(tree, MAIN_LABEL)
    if 'Iridescent Mode' not in main.inputs:
        raise ValueError(f'Environment attachment requires mapped Standard SpecRamp inputs: {tree.name}')
    diffuse = by_label(tree, 'Verified local HGRP diffuse equations')
    emission = by_label(tree, 'Artist-lit NPR diffuse - shared engines')
    state = by_label(tree, 'Explicit HGRP state and preview inputs')
    original = _source(emission, 'Color')
    inputs = {label: _source(main, label) for label in ('Specular Albedo', 'Specular Level', 'Metallic', 'Perceptual Roughness')}
    group = node(tree, 'ShaderNodeGroup', LABEL, SECTION)
    group.node_tree = build_standard_environment_brdf('EF Uniform Host Environment BRDF')
    shared = node(tree, 'ShaderNodeGroup', 'Shared uniform host environment controls', SECTION)
    shared.node_tree = controls
    for label in ('Environment Radiance', 'Host Exposure'):
        tree.links.new(shared.outputs[label], group.inputs[label])
    dielectric = _math(tree, 'MULTIPLY', .04, inputs['Specular Level'], 'Environment dielectric F0')
    f0 = color_mix(tree, 'MIX', inputs['Metallic'], dielectric, inputs['Specular Albedo'], 'Environment source metallic F0', SECTION)
    tree.links.new(f0, group.inputs['F0'])
    tree.links.new(main.outputs['Specular Color'], group.inputs['Mapped Specular Color'])
    tree.links.new(inputs['Perceptual Roughness'], group.inputs['Perceptual Roughness'])
    if main.inputs['Iridescent Mode'].is_linked:
        tree.links.new(_source(main, 'Iridescent Mode'), group.inputs['Iridescent Mode'])
    else:
        group.inputs['Iridescent Mode'].default_value = main.inputs['Iridescent Mode'].default_value
    geometry = node(tree, 'ShaderNodeNewGeometry', 'Environment geometric normal and view', SECTION)
    dot = node(tree, 'ShaderNodeVectorMath', 'Environment geometric N dot V', SECTION)
    dot.operation = 'DOT_PRODUCT'
    tree.links.new(geometry.outputs['Normal'], dot.inputs[0])
    tree.links.new(geometry.outputs['Incoming'], dot.inputs[1])
    tree.links.new(dot.outputs['Value'], group.inputs['NdotV'])
    tree.links.new(_source(diffuse, 'Ambient Color'), group.inputs['Ambient Color'])
    tree.links.new(diffuse.outputs['Ambient Diffuse Intensity'], group.inputs['Ambient Diffuse Intensity'])
    tree.links.new(state.outputs['CP0 W Dark Scale'], group.inputs['CP0 W'])
    result = color_mix(tree, 'ADD', 1., original, group.outputs['Contribution'], 'NPR bus plus optional host environment', SECTION)
    tree.links.new(result, emission.inputs['Color'])
    group['lighting_fidelity'] = 'uniform_host_environment_approximation'
    group['scope'] = SCOPE
    layout(group.node_tree)
    layout(controls)
    layout(tree)
    return {'group': tree.name, 'controls': controls.name, 'kernel': group.node_tree.name,
            'lighting_fidelity': 'uniform_host_environment_approximation', 'scope': SCOPE,
            'weighted_by_existing_npr_blend': True}
