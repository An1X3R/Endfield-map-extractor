"""Current mapped Standard main-light GGX, separate from additive Silk lobes.

The no-special-specular and nonadvanced Silk ForwardLit programs use identical
isotropic arithmetic. Shared Skin arithmetic is reused only for that proven
subexpression. Full Diffuse is the source lighting weight, not NPR Color or the
raw directional-light RGB. Host camera/up and CP13 remain explicit inputs.
"""
from __future__ import annotations

from collections.abc import Callable
import json
import math

import bpy

from character_node_graph import node, math_node, color_mix
from character_weather_nodes import scalar_mix, combine_vector
from character_skin_specular import build_skin_ggx
from patch_character_hgrp import by_label, source_keywords

Scalar = bpy.types.NodeSocket | float
Vector = bpy.types.NodeSocket | tuple[float, float, float]
SECTION = 'Standard Main GGX'
LABEL = 'Compiled Standard ordinary main-light contribution'


def _main_tree(name: str) -> tuple[bpy.types.ShaderNodeTree, bpy.types.Node, bpy.types.Node, bpy.types.Node]:
    if not name.strip():
        raise ValueError('Standard main-light group name is empty')
    tree = bpy.data.node_groups.new(name, 'ShaderNodeTree')
    vectors = ('Normal', 'View', 'Camera Forward', 'Light Direction', 'World Up')
    colors = ('Specular Albedo', 'Full Diffuse')
    scalars = ('Perceptual Roughness', 'Metallic', 'Specular Level',
               'Cast Shadow', 'Cast Blend', 'Ambient Diffuse Intensity', 'CP13 W')
    for label in (*vectors, *colors, *scalars):
        tree.interface.new_socket(name=label, in_out='INPUT', socket_type=(
            'NodeSocketVector' if label in vectors else 'NodeSocketColor' if label in colors else 'NodeSocketFloat'))
    for label, kind in (('Ordinary Specular', 'NodeSocketColor'), ('Specular Color', 'NodeSocketColor'), ('GGX', 'NodeSocketFloat')):
        tree.interface.new_socket(name=label, in_out='OUTPUT', socket_type=kind)
    entry = node(tree, 'NodeGroupInput', 'Explicit Standard material and lighting', SECTION)
    output = node(tree, 'NodeGroupOutput', 'Ordinary main-light outputs', SECTION)
    arithmetic = build_skin_ggx()
    arithmetic.name = name + ' Shared Verified GGX Arithmetic'
    arithmetic['source_scope'] = 'Same H/D/Kelemen arithmetic proved in Standard blob471 and Silk fragment; not the Skin material binder.'
    for label, source in (('Raw Distribution', 'Skin source distribution'),
                          ('NdotV', 'Skin NdotV saturate'), ('Alpha Squared', 'Skin alpha squared')):
        arithmetic.interface.new_socket(name=label, in_out='OUTPUT', socket_type='NodeSocketFloat')
        arithmetic.links.new(by_label(arithmetic, source).outputs[0], by_label(arithmetic, 'Current Skin GGX output').inputs[label])
    ggx = node(tree, 'ShaderNodeGroup', 'Verified common isotropic arithmetic', SECTION)
    ggx.node_tree = arithmetic
    for destination, source in (('Normal', 'Normal'), ('View', 'View'), ('Camera', 'Camera Forward'),
                                ('Light', 'Light Direction'), ('World Up', 'World Up'),
                                ('Cast Shadow', 'Cast Shadow'), ('Perceptual Roughness', 'Perceptual Roughness')):
        tree.links.new(entry.outputs[source], ggx.inputs[destination])
    return tree, entry, output, ggx


def _source_f0(tree: bpy.types.ShaderNodeTree, entry: bpy.types.Node) -> bpy.types.NodeSocket:
    dielectric = math_node(tree, 'MULTIPLY', .04, entry.outputs['Specular Level'], 'Source dielectric F0', SECTION)
    return color_mix(tree, 'MIX', entry.outputs['Metallic'], dielectric, entry.outputs['Specular Albedo'], 'Source metallic F0', SECTION)


def _finish_main_term(tree: bpy.types.ShaderNodeTree, entry: bpy.types.Node, output: bpy.types.Node,
    ggx: bpy.types.Node, specular_color: bpy.types.NodeSocket) -> None:
    half_cast = math_node(tree, 'ADD', .5, math_node(tree, 'MULTIPLY', .5, entry.outputs['Cast Blend'], 'Half cast blend', SECTION), 'Source specular cast weight', SECTION)
    weight = math_node(tree, 'MULTIPLY', half_cast, entry.outputs['Ambient Diffuse Intensity'], 'Source ambient specular weight', SECTION)
    weight = math_node(tree, 'MULTIPLY', weight, entry.outputs['CP13 W'], 'Explicit CP13 W scale', SECTION)
    weight = math_node(tree, 'MULTIPLY', weight, ggx.outputs['GGX'], 'Source ordinary GGX weight', SECTION)
    lit = color_mix(tree, 'MULTIPLY', 1., specular_color, entry.outputs['Full Diffuse'], 'Specular times source Full Diffuse', SECTION)
    contribution = color_mix(tree, 'MULTIPLY', 1., lit, weight, 'Compiled Standard ordinary specular', SECTION)
    for socket, label in ((contribution, 'Ordinary Specular'), (specular_color, 'Specular Color'), (ggx.outputs['GGX'], 'GGX')):
        tree.links.new(socket, output.inputs[label])


def build_standard_mapped_main_light(name: str, spec_ramp: bpy.types.Image) -> bpy.types.ShaderNodeTree:
    """Build only isotropic direct specular; preserve source colors and opacity."""
    # Access initializes a packed image that was not sampled before reopening.
    if spec_ramp.packed_file is None or len(spec_ramp.pixels) == 0:
        raise ValueError('Standard main light requires packed SpecRamp pixels')
    tree, entry, output, ggx = _main_tree(name)
    tree.interface.new_socket(name='Iridescent Mode', in_out='INPUT', socket_type='NodeSocketFloat')
    for label in ('Ramp U', 'Ramp V'):
        tree.interface.new_socket(name=label, in_out='OUTPUT', socket_type='NodeSocketFloat')
    inverse_peak = math_node(tree, 'MINIMUM', 65504., math_node(tree, 'DIVIDE', 1.,
        math_node(tree, 'ADD', ggx.outputs['Alpha Squared'], .0001, 'Ramp peak epsilon', SECTION),
        'Inverse source distribution peak', SECTION), 'Half range distribution peak', SECTION)
    coordinate = math_node(tree, 'DIVIDE', ggx.outputs['Raw Distribution'], inverse_peak, 'Normalized distribution coordinate', SECTION)
    nv2 = math_node(tree, 'MULTIPLY', ggx.outputs['NdotV'], ggx.outputs['NdotV'], 'Source NdotV squared', SECTION)
    ramp_u = scalar_mix(tree, entry.outputs['Iridescent Mode'], coordinate, nv2, 'Source SpecRamp U')
    ramp_v = math_node(tree, 'MULTIPLY', entry.outputs['Perceptual Roughness'],
        math_node(tree, 'SUBTRACT', 1., entry.outputs['Metallic'], 'SpecRamp nonmetal fraction', SECTION), 'Source SpecRamp V', SECTION)
    sample = node(tree, 'ShaderNodeTexImage', 'Exact source SpecRamp - mip zero approximation', SECTION)
    sample.image = spec_ramp
    sample.extension = 'EXTEND'
    sample.interpolation = 'Linear'
    tree.links.new(combine_vector(tree, (ramp_u, ramp_v, 0.), 'SpecRamp coordinates'), sample.inputs['Vector'])
    mapped = color_mix(tree, 'MULTIPLY', 1., _source_f0(tree, entry), sample.outputs['Color'], 'Source F0 times SpecRamp RGB', SECTION)
    _finish_main_term(tree, entry, output, ggx, mapped)
    for socket, label in ((ramp_u, 'Ramp U'), (ramp_v, 'Ramp V')):
        tree.links.new(socket, output.inputs[label])
    tree['source_scope'] = 'Current Standard ForwardLit mapped normal/metal-gloss/spec-ramp isotropic main term, including shared ordinary Silk term; no special anisotropy/clearcoat/fur/pantyhose.'
    tree['sampling_boundary'] = 'Blender linear image filtering is shared-engine; explicit SampleLevel(0) is not independently controlled in Blender.'
    return tree


def build_standard_no_spec_ramp_main_light(name: str) -> bpy.types.ShaderNodeTree:
    """Current no-SpecRamp branch uses F0 directly, not a fabricated white map."""
    tree, entry, output, ggx = _main_tree(name)
    _finish_main_term(tree, entry, output, ggx, _source_f0(tree, entry))
    tree['source_scope'] = 'Current Standard no-SpecRamp nonadvanced Silk ForwardLit blob493 ordinary term; F0 directly weights GGX. Additive Silk remains separate.'
    return tree


def _number(values: dict[str, object], name: str) -> float:
    value = values[name]
    if isinstance(value, bool) or not isinstance(value, (float, int)) or not math.isfinite(value):
        raise ValueError(f'Standard source scalar must be finite: {name}={value}')
    return float(value)


def build_standard_scalar_main_light(name: str, spec_ramp: bpy.types.Image) -> bpy.types.ShaderNodeTree:
    """Reuse proven common arithmetic; scalar source channels are bound separately."""
    tree = build_standard_mapped_main_light(name, spec_ramp)
    tree['source_scope'] = 'Current Standard ForwardLit scalar metal/spec/smoothness with mapped normal/spec-ramp; blob469 uses the same H/D/Kelemen/ramp arithmetic as blob471.'
    return tree


def _upstream(item: bpy.types.Node, name: str) -> bpy.types.NodeSocket:
    links = item.inputs[name].links
    if len(links) != 1:
        raise ValueError(f'Standard requires exactly one live input: {item.label}/{name}')
    return links[0].from_socket


def _static_scalar(socket: bpy.types.NodeSocket, seen: frozenset[int]) -> float:
    """Read a current editable constant through group outputs without guessing."""
    item = socket.node
    key = item.as_pointer()
    if key in seen:
        raise ValueError(f'Cyclic Standard response control: {item.name}')
    visited = seen | {key}
    if item.type == 'VALUE':
        return float(socket.default_value)
    if item.type == 'GROUP':
        active = tuple(n for n in item.node_tree.nodes if n.type == 'GROUP_OUTPUT' and n.is_active_output)
        if len(active) == 1:
            return _static_scalar(_upstream(active[0], socket.name), visited)
    raise ValueError(f'Standard response control is not a verified editable constant: {item.name}/{socket.name}')


def attach_standard_main_light(tree: bpy.types.ShaderNodeTree, camera_forward: Vector,
    world_up: Vector, cp13_w: Scalar, layout: Callable[[bpy.types.NodeTree], None]) -> dict[str, object]:
    """Append isotropic specular; identify unimplemented special lobes explicitly."""
    if tree.get('source_shader') != 'HGRP/CharacterNPR' or any(n.label == LABEL for n in tree.nodes):
        raise ValueError(f'Expected an unpatched Standard source material: {tree.name}')
    values = json.loads(tree['source_floats'])
    expected = {'_UseBumpMap': True, '_UseCharacterFur': False, '_Pantyhose': False}
    for field, enabled in expected.items():
        if (_number(values, field) > .5) != enabled:
            raise ValueError(f'Standard ordinary GGX variant not implemented: {field}={values[field]}')
    keywords = set(source_keywords(tree))
    mapped = _number(values, '_UseMetallicGlossMap') > .5
    ramped = _number(values, '_UseSpecRampMap') > .5
    if mapped != ('_METALLICSPECGLOSSMAP' in keywords):
        raise ValueError('Standard MetallicGlossMap source switch and keyword disagree')
    if ramped != ('_SPEC_RAMP_ON' in keywords):
        raise ValueError('Standard SpecRamp source switch and keyword disagree')
    if '_NORMALMAP' not in keywords:
        raise ValueError(f'Standard mapped keyword proof incomplete: {sorted(keywords)}')
    if keywords & {'_CHARACTER_FUR', '_PANTYHOSE'}:
        raise ValueError(f'Standard special branch is not ordinary GGX: {sorted(keywords)}')
    special_features = tuple(field for field in ('_ClearCoat', '_UseAnisotropy') if _number(values, field) > .5)
    for field, keyword in (('_ClearCoat', '_CLEARCOAT'), ('_UseAnisotropy', '_ANISOTROPY_SPECULAR_ON')):
        if (_number(values, field) > .5) != (keyword in keywords):
            raise ValueError(f'Standard special specular source switch and keyword disagree: {field}')
    silk = _number(values, '_SilkStockings') > .5
    if silk and special_features:
        raise ValueError('Combined Silk and special specular variant has no attachment proof')
    if silk != ('_SILK_STOCKINGS' in keywords) or (silk and _number(values, '_SilkStockingsAdvance') > .5):
        raise ValueError('Standard Silk switch/keyword disagree or advanced Silk is not implemented')
    if silk and not mapped:
        raise ValueError('Scalar Silk ordinary specular has no current variant proof')
    if not ramped and not (silk and mapped and '_DIFF_RAMP_ON' in keywords):
        raise ValueError('No-SpecRamp ordinary binding currently requires proved mapped nonadvanced Silk/DiffRamp variant')
    if not mapped:
        for field in ('_Specular', '_Metallic', '_Smoothness'):
            _number(values, field)
    ramp_nodes = tuple(n for n in tree.nodes if n.type == 'TEX_IMAGE' and n.label.startswith('_SpecRampMap |'))
    if ramped and (len(ramp_nodes) != 1 or ramp_nodes[0].image is None):
        raise ValueError(f'Standard exact source SpecRamp is missing or ambiguous: {tree.name}')
    surface = by_label(tree, 'CharacterNPR shared Eevee / Cycles surface')
    diffuse = by_label(tree, 'Verified local HGRP diffuse equations')
    blend = by_label(tree, 'Editable native-sampling response / baseline')
    emission = by_label(tree, 'Artist-lit NPR diffuse - shared engines')
    response = _static_scalar(_upstream(blend, 'Fac'), frozenset())
    if not math.isfinite(response) or not 0. <= response <= 1.:
        raise ValueError(f'Standard response weight is outside [0,1]: {response}')
    group = node(tree, 'ShaderNodeGroup', LABEL, SECTION)
    if ramped:
        factory = build_standard_mapped_main_light if mapped else build_standard_scalar_main_light
        group.node_tree = factory('EF Standard Ordinary Main GGX', ramp_nodes[0].image)
    else:
        group.node_tree = build_standard_no_spec_ramp_main_light('EF Standard Ordinary Main GGX - No SpecRamp')
    geo = node(tree, 'ShaderNodeNewGeometry', 'Standard source view', SECTION)
    inputs = {'Normal': _upstream(surface, 'Normal'), 'View': geo.outputs['Incoming'],
              'Specular Albedo': _upstream(surface, 'Base Color'),
              'Perceptual Roughness': _upstream(surface, 'Roughness'), 'Metallic': _upstream(surface, 'Metallic'),
              'Specular Level': by_label(tree, 'R metallic / G specular / B AO / A smoothness').outputs['Green'] if mapped else _upstream(surface, 'Specular IOR Level'),
              'Full Diffuse': diffuse.outputs['Full Diffuse'], 'Cast Shadow': _upstream(diffuse, 'Cast Shadow'),
              'Cast Blend': diffuse.outputs['Cast Blend'], 'Ambient Diffuse Intensity': diffuse.outputs['Ambient Diffuse Intensity'],
              'Light Direction': by_label(tree, 'Normalize artist light direction').outputs[0]}
    for label, socket in inputs.items():
        tree.links.new(socket, group.inputs[label])
    for label, value in (('Camera Forward', camera_forward), ('World Up', world_up), ('CP13 W', cp13_w)):
        if isinstance(value, bpy.types.NodeSocket):
            tree.links.new(value, group.inputs[label])
        else:
            group.inputs[label].default_value = value
    if ramped:
        group.inputs['Iridescent Mode'].default_value = _number(values, '_SpecRampIridescentMode')
    added = color_mix(tree, 'ADD', 1., _upstream(emission, 'Color'), group.outputs['Ordinary Specular'],
        'NPR bus plus ordinary Standard GGX', SECTION)
    tree.links.new(added, emission.inputs['Color'])
    fidelity = 'host_isotropic_approximation' if special_features else 'verified_ordinary_isotropic_terms'
    scope = ('Host isotropic SpecRamp/GGX lighting for a source special-specular material; '
             'native anisotropic and clearcoat lobes are not reconstructed.'
             if special_features else group.node_tree['source_scope'])
    group['lighting_fidelity'] = fidelity
    group['unimplemented_specular_features'] = json.dumps(special_features)
    group['source_scope'] = scope
    layout(group.node_tree)
    layout(tree)
    return {'source_identity': tree['source_identity'], 'kernel': group.node_tree.name,
        'material_channels': 'metal-gloss RGBA' if mapped else 'scalar source specular/metallic/smoothness',
        'spec_ramp_enabled': ramped,
        'silk_additive_preserved': silk, 'npr_response_weight': response, 'remaining_physical_response_weight': 1. - response,
        'albedo_opacity_source_controls_unchanged': True,
        'lighting_fidelity': fidelity,
        'unimplemented_specular_features': list(special_features),
        'scope': scope,
        'limitations': ['outer PBR blend retained; ordinary source term is weighted only on NPR branch',
            'dry surface inputs; rain post-composition/environment BRDF are separate',
            'source Full Diffuse and explicit host camera/up/CP13; not a runtime uniform capture',
            'Blender SpecRamp filtering has no independently selected source mip zero']}
