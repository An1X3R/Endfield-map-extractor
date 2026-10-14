"""Attach compiled Skin R/G consumers to explicit preview inputs, without an atlas."""
from __future__ import annotations

import bpy

from character_node_graph import math_node, node
from character_skin_shadow import build_body_skin_shadow, build_face_skin_shadow
from character_weather_nodes import vector_math
from patch_character_hgrp import by_label, source_keywords


LABEL = 'Compiled Skin screen-shadow consumption'


def _source(socket: bpy.types.NodeSocket) -> bpy.types.NodeSocket:
    if len(socket.links) != 1:
        raise ValueError(f'Skin shadow binding needs one source: {socket.node.name}/{socket.name}')
    return socket.links[0].from_socket


def _face_ambient_receiver(diffuse: bpy.types.Node) -> None:
    """Keep the SDF ambient-color receiver separate from its ordinary Shadow A."""
    original = diffuse.node_tree
    if original is None or 'Face Ambient Shadow A' in diffuse.inputs:
        raise ValueError('Face diffuse kernel is absent or already specialized')
    kernel = original.copy()
    kernel.name = 'EF Current Skin SDF Diffuse'
    entry = by_label(kernel, 'Explicit pipeline inputs')
    ambient = by_label(kernel, 'Shadow A plus ramp alpha')
    if _source(ambient.inputs[0]) != entry.outputs['Shadow A']:
        raise ValueError('Face ambient attachment requires the verified generic Shadow A route')
    kernel.interface.new_socket(name='Face Ambient Shadow A', in_out='INPUT', socket_type='NodeSocketFloat')
    kernel.links.new(entry.outputs['Face Ambient Shadow A'], ambient.inputs[0])
    kernel['sdf_ambient_scope'] = 'Current compiled SDF ambient weight uses alpha*(1-MaskG+LocalVisibility*MaskG); other Shadow A consumers retain alpha*LocalVisibility.'
    diffuse.node_tree = kernel


def attach_skin_shadow(
    tree: bpy.types.ShaderNodeTree, directional_strength: float, ignore_main_shadow: float,
) -> dict[str, str]:
    """Retain raw preview inputs while replacing their Skin receiver arithmetic."""
    if tree.get('source_shader') != 'HGRP/CharacterNPR_Skin':
        raise ValueError(f'Skin shadow consumer requires the exact source shader: {tree.name}')
    if any(item.label == LABEL for item in tree.nodes):
        raise ValueError(f'Skin shadow consumer already attached: {tree.name}')
    if not 0. <= directional_strength <= 1. or not 0. <= ignore_main_shadow <= 1.:
        raise ValueError(f'Skin shadow controls out of range: strength={directional_strength}, ignore={ignore_main_shadow}')
    face = '_SDFLIGHTMAP' in source_keywords(tree)
    shared = by_label(tree, 'Explicit HGRP state and preview inputs')
    diffuse = by_label(tree, 'Verified local HGRP diffuse equations')
    base = tuple(n for n in tree.nodes if n.type == 'TEX_IMAGE' and n.get('source_slot') == '_BaseMap')
    if len(base) != 1 or base[0].image is None:
        raise ValueError(f'Skin shadow requires one actual BaseMap: {tree.name}')
    ramp_alpha = _source(diffuse.inputs['Ramp Alpha'])
    kernel = build_face_skin_shadow('EF Skin SDF Screen Shadow Consumer') if face else build_body_skin_shadow('EF Skin Body Screen Shadow Consumer')
    section = 'Skin Shadow Receiver'
    instance = node(tree, 'ShaderNodeGroup', LABEL, section)
    instance.node_tree = kernel
    cast_consumers = tuple(link.to_socket for link in shared.outputs['Cast Shadow'].links)
    bindings = {'Raw Screen Shadow R': shared.outputs['Cast Shadow'], 'Raw Screen Shadow G': shared.outputs['Preview Atlas Visibility'], 'Base Alpha': base[0].outputs['Alpha'], 'Ramp Alpha': ramp_alpha}
    for name, source in bindings.items():
        tree.links.new(source, instance.inputs[name])
    instance.inputs['Directional Strength'].default_value = directional_strength
    instance.inputs['Ignore Main Shadow'].default_value = ignore_main_shadow
    if face:
        sdf = by_label(tree, 'Native Face SDF / Explicit Adjustable Basis')
        mask = by_label(tree, 'Actual SDFMask G surface blend')
        camera = _source(sdf.inputs['Camera Direction'])
        right = _source(sdf.inputs['Face Right'])
        forward = _source(sdf.inputs['Face Forward'])
        x = vector_math(tree, 'DOT_PRODUCT', camera, right, 'Shadow camera face horizontal X').outputs['Value']
        z = vector_math(tree, 'DOT_PRODUCT', camera, forward, 'Shadow camera face horizontal Z').outputs['Value']
        for value in (x, z):
            value.node['endfield_layout_section'] = section
        length = math_node(tree, 'SQRT', math_node(tree, 'ADD', math_node(tree, 'MULTIPLY', x, x, 'Shadow camera X squared', section), math_node(tree, 'MULTIPLY', z, z, 'Shadow camera Z squared', section), 'Shadow camera XZ squared', section), 0., 'Shadow camera XZ length', section)
        cosine = math_node(tree, 'DIVIDE', z, length, 'Source face camera horizontal cosine', section)
        for name, source in (('Mask G', mask.outputs['Green']), ('Mask B', mask.outputs['Blue']), ('Face/View Dot', cosine)):
            tree.links.new(source, instance.inputs[name])
    for target in cast_consumers:
        tree.links.new(instance.outputs['Cast Shadow'], target)
    tree.links.new(instance.outputs['Shadow A'], diffuse.inputs['Shadow A'])
    tree.links.new(instance.outputs['Shadow B'], diffuse.inputs['Shadow B'])
    if face:
        _face_ambient_receiver(diffuse)
        tree.links.new(instance.outputs['Ambient Shadow A'], diffuse.inputs['Face Ambient Shadow A'])
    instance['input_provenance'] = 'Existing manual Cast Shadow and Preview Atlas Visibility controls supply raw R/G. No generated occlusion or native atlas is claimed.'
    return {'group': tree.name, 'kernel': kernel.name, 'branch': 'sdf_face' if face else 'body', 'producer': 'explicit_manual_preview'}
