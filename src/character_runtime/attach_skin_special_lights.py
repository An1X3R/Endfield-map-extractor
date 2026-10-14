"""Bind compiled Skin Ramped/RimOnly to an existing live host light response.

These external Blender connectors replace one plain response and preserve its
live light links. Explicit parameter values are artist preview overrides, not
captured native light-record data. No file is saved and no Emission bus is built.
"""
from __future__ import annotations

from dataclasses import asdict, dataclass
import json
import math

import bpy

from character_node_graph import node
from patch_character_hgrp import by_label, source_keywords


SECTION = 'Skin Special Light Binding'


@dataclass(frozen=True, slots=True)
class RampedLightParameters:
    ramp_offset: float
    base_response_scale: float
    face_ramp_bias: float
    face_alpha_threshold: float


@dataclass(frozen=True, slots=True)
class RimLightParameters:
    rim_width: float
    rim_color_blend: float


def _source(socket: bpy.types.NodeSocket) -> bpy.types.NodeSocket:
    links = tuple(socket.links)
    if len(links) != 1:
        raise ValueError(f'Special Skin binding needs exactly one source: {socket.node.label}/{socket.name}, links={len(links)}')
    return links[0].from_socket


def _original(tree: bpy.types.ShaderNodeTree, light: bpy.types.Object, kernel: bpy.types.ShaderNodeTree, mode: int) -> bpy.types.Node:
    if tree.get('source_shader') != 'HGRP/CharacterNPR_Skin' or kernel.get('source_mode') != mode:
        raise ValueError(f'Special Skin binding requires exact Skin and mode {mode}: {tree.name}/{kernel.name}')
    if light.type != 'LIGHT' or light.data.type not in ('POINT', 'SPOT', 'SUN'):
        raise ValueError(f'Unsupported host light for special Skin binding: {light.name}/{light.type}')
    face = '_SDFLIGHTMAP' in source_keywords(tree)
    kernel_face = 'SDF Mask' in ' '.join(item.name for item in kernel.interface.items_tree if item.item_type == 'SOCKET' and item.in_out == 'INPUT')
    if face != kernel_face:
        raise ValueError(f'Special Skin face/body kernel mismatch: {tree.name}/{kernel.name}')
    original = by_label(tree, 'Plain Skin response ' + light.name)
    if original.type != 'GROUP' or original.node_tree is None or not original.outputs['Contribution'].links:
        raise ValueError(f'Expected live plain Skin response with consumers: {tree.name}/{light.name}')
    for socket in original.inputs:
        if len(socket.links) > 1:
            raise ValueError(f'Ambiguous existing plain light input: {tree.name}/{socket.name}')
    return original


def _metallic(tree: bpy.types.ShaderNodeTree) -> bpy.types.NodeSocket:
    surfaces = tuple(item for item in tree.nodes if item.type == 'BSDF_PRINCIPLED')
    if len(surfaces) != 1:
        raise ValueError(f'Special Skin binding requires one source surface: {tree.name}')
    return _source(surfaces[0].inputs['Metallic'])


def _light_inputs(tree: bpy.types.ShaderNodeTree, original: bpy.types.Node, instance: bpy.types.Node) -> None:
    for socket in instance.inputs:
        name = 'Visibility' if socket.name == 'Source Shadow Visibility' else socket.name
        if name not in original.inputs:
            continue
        source = original.inputs[name]
        if source.is_linked:
            tree.links.new(_source(source), socket)
        else:
            socket.default_value = source.default_value


def _replace(tree: bpy.types.ShaderNodeTree, original: bpy.types.Node, instance: bpy.types.Node, parameters: dict[str, float]) -> None:
    targets = tuple(link.to_socket for link in original.outputs['Contribution'].links)
    for target in targets:
        tree.links.new(instance.outputs['Contribution'], target)
    tree.nodes.remove(original)
    instance['parameter_provenance'] = 'Explicit editable preview NPR mode parameters; no captured native per-light GPU record.'
    instance['preview_parameters'] = json.dumps(parameters, sort_keys=True)
    instance['visibility_scope'] = 'Preserved live hgrp_visibility input conditions the native mode ramp/envelope; native per-light atlas sampling is not reproduced.'


def _sdf_alpha(tree: bpy.types.ShaderNodeTree, sdf: bpy.types.Node) -> bpy.types.NodeSocket:
    """Expose the existing mirrored sample alpha on a private loaded-kernel copy."""
    if sdf.node_tree is None:
        raise ValueError(f'SDF kernel absent: {tree.name}')
    texture = by_label(sdf.node_tree, 'Actual source SDF Lightmap')
    sources = tuple(item for item in tree.nodes if item.type == 'TEX_IMAGE' and item.get('source_slot') == '_SDFLightmap')
    if len(sources) != 1 or texture.image is None or texture.image != sources[0].image or texture.image.packed_file is None or texture.image.colorspace_settings.name != 'Non-Color':
        raise ValueError(f'Exact packed mirrored SDF lightmap source is missing: {tree.name}')
    if len(texture.inputs['Vector'].links) != 1:
        raise ValueError(f'SDF mirrored UV source is missing: {tree.name}')
    if 'Mirrored SDF Lightmap Alpha' not in sdf.outputs:
        kernel = sdf.node_tree.copy()
        kernel.name = 'EF Skin SDF With Mirrored Alpha'
        kernel.interface.new_socket(name='Mirrored SDF Lightmap Alpha', in_out='OUTPUT', socket_type='NodeSocketFloat').description = 'Current _1530: alpha of the already mirrored _SDFLightmap sample, not SDFMask alpha.'
        output = by_label(kernel, 'Verified local SDF outputs')
        kernel.links.new(by_label(kernel, 'Actual source SDF Lightmap').outputs['Alpha'], output.inputs['Mirrored SDF Lightmap Alpha'])
        sdf.node_tree = kernel
    return sdf.outputs['Mirrored SDF Lightmap Alpha']


def attach_skin_ramped_light(tree: bpy.types.ShaderNodeTree, light: bpy.types.Object, kernel: bpy.types.ShaderNodeTree, parameters: RampedLightParameters) -> dict[str, object]:
    """Replace a plain light with distinct body/face source mode1 consumption."""
    values = asdict(parameters)
    if not all(math.isfinite(value) for value in values.values()):
        raise ValueError(f'Nonfinite explicit Ramped parameters: {parameters}')
    original = _original(tree, light, kernel, 1)
    lit = _source(by_label(tree, 'Source diffuse albedo coefficient').inputs['Color1'])
    shadow = _source(by_label(tree, 'Scaled source shadow albedo').inputs['Color1'])
    metallic = _metallic(tree)
    bindings = {'Lit Albedo':lit, 'Shadow Albedo':shadow, 'Metallic':metallic}
    face = '_SDFLIGHTMAP' in source_keywords(tree)
    if face:
        sdf = by_label(tree, 'Native Face SDF / Explicit Adjustable Basis')
        bindings['SDF Mask G'] = _source(sdf.inputs['SDF Mask G'])
        bindings['SDF Lightmap Alpha'] = _sdf_alpha(tree, sdf)
    instance = node(tree, 'ShaderNodeGroup', 'Ramped Skin response ' + light.name, SECTION)
    instance.node_tree = kernel
    _light_inputs(tree, original, instance)
    for label, value in bindings.items():
        tree.links.new(value, instance.inputs[label])
    for label, value in (('Ramp Offset',parameters.ramp_offset), ('Base Response Scale',parameters.base_response_scale)):
        instance.inputs[label].default_value = value
    if face:
        instance.inputs['Face Ramp Bias'].default_value = parameters.face_ramp_bias
        instance.inputs['Face Alpha Threshold'].default_value = parameters.face_alpha_threshold
    _replace(tree, original, instance, values)
    return {'group':tree.name, 'light':light.name, 'mode':1, 'branch':'sdf_face' if face else 'body', 'parameters':values, 'lit_source':[lit.node.label,lit.name], 'shadow_source':[shadow.node.label,shadow.name], 'sdf_alpha':'Existing mirrored SDFLightmap.a' if face else 'not used'}


def attach_skin_rim_light(tree: bpy.types.ShaderNodeTree, light: bpy.types.Object, kernel: bpy.types.ShaderNodeTree, parameters: RimLightParameters) -> dict[str, object]:
    """Replace a plain light with source mode3, retaining live state and zero spec."""
    values = asdict(parameters)
    if not all(math.isfinite(value) for value in values.values()):
        raise ValueError(f'Nonfinite explicit RimOnly parameters: {parameters}')
    original = _original(tree, light, kernel, 3)
    lit = _source(by_label(tree, 'Source diffuse albedo coefficient').inputs['Color1'])
    bindings = {'Lit Albedo':lit, 'Metallic':_metallic(tree)}
    face = '_SDFLIGHTMAP' in source_keywords(tree)
    if face:
        sdf = by_label(tree, 'Native Face SDF / Explicit Adjustable Basis')
        bindings['Camera Forward'] = _source(sdf.inputs['Camera Direction'])
        shadow = by_label(tree, 'Compiled Skin screen-shadow consumption')
        bindings['Face/View Dot'] = _source(shadow.inputs['Face/View Dot'])
        masks = tuple(item for item in tree.nodes if item.type == 'TEX_IMAGE' and item.get('source_slot') == '_SDFMask')
        if len(masks) != 1 or masks[0].image is None or masks[0].image.colorspace_settings.name != 'Non-Color':
            raise ValueError(f'Actual linear SDFMask alpha source missing: {tree.name}')
        bindings['SDF Mask Alpha'] = masks[0].outputs['Alpha']
    else:
        bindings['Camera Forward'] = by_label(tree, 'Skin volume live preview camera forward').outputs['Vector']
    instance = node(tree, 'ShaderNodeGroup', 'RimOnly Skin response ' + light.name, SECTION)
    instance.node_tree = kernel
    _light_inputs(tree, original, instance)
    for label, value in bindings.items():
        tree.links.new(value, instance.inputs[label])
    instance.inputs['Rim Width'].default_value = parameters.rim_width
    instance.inputs['Rim Color Blend'].default_value = parameters.rim_color_blend
    _replace(tree, original, instance, values)
    return {'group':tree.name, 'light':light.name, 'mode':3, 'branch':'sdf_face' if face else 'body', 'parameters':values, 'lit_source':[lit.node.label,lit.name], 'camera_source':[bindings['Camera Forward'].node.label,bindings['Camera Forward'].name]}
