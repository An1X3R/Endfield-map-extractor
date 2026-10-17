"""Build compact stock Blender surfaces from retained character source evidence.

Only pre-lighting albedo, original UV/atlas controls, source coverage, and packed
surface channels survive. Image samplers stay outside semantic calculation
groups. Face SDF and Principled lobes are explicit host approximations, not HGRP
lighting. Geometry, rigging, actions, source materials, and source images are not
edited. Callers decide whether unused original datablocks belong in an export.
"""
from __future__ import annotations

from dataclasses import dataclass, replace
from collections.abc import Mapping
import json
import math
from typing import Literal, cast

import bpy
from mathutils import Vector

from character_node_graph import color_mix, math_node, node
from character_simple_layout import layout_simple_character_material
from rebuild_character_materials import decode_normal


SemanticFamily = Literal['clothing', 'skin', 'face', 'hair', 'eyes', 'stockings', 'overlay', 'fur', 'fur_cards', 'retained_vfx', 'retained_non_character']
SUPPORTED_SHADERS = frozenset(('HGRP/CharacterNPR', 'HGRP/CharacterNPR_Skin',
    'HGRP/CharacterNPR_Hair', 'HGRP/CharacterNPR_Eye', 'HGRP/CharacterNPR_OverlayShadow'))
HEAD_SHADOW_FAMILIES = frozenset(('face', 'eyes', 'hair', 'overlay'))
LIMITATIONS = (
    'Stock Principled lights clothing/body/stockings and faces; faces retain source SDF and an adjustable head-oriented softened normal. Native light loops and special lobes are omitted.',
    'Face SDF uses original RG thresholds and mask with a compact mirrored-UV host shadow; native backlight and face-specular equations are omitted.',
    'Inactive LOD faces share the single existing explicit face basis when they lack a separate source SDF anchor.',
    'Face illumination follows Blender lights and environment; only its stylized SDF mask follows the designated Sun. Eye diffuse fill still follows only that Sun color and relative energy.',
    'Hair uses dielectric F0 from source packed G, zero physical metallic and an explicit host roughness of 0.48. Packed R remains a native tangent-blend parameter and packed alpha a native second-lobe gain; neither is interpreted as stock metallic or GGX roughness.',
    'Hair blends 75 percent designated-Sun source-albedo diffuse fill with 25 percent stock dielectric surface to avoid excessive card self-shadowing. This host style approximation retains a restrained physical highlight, not the native dual-strand lobes; other lamps affect only its stock contribution.',
    'Overlay passes retain the source multiplicative transparent transmission bridge; native framebuffer multiply, stencil and depth queues remain approximated.',
    'Head surfaces retain explicit source no-cast flags; mixed caster ownership uses separate material variants. Missing renderer metadata retains stock casting. Native shadow-only proxy reconstruction remains outside compact shading.',
    'Weather is intentionally absent from this kernel and must be composed explicitly by the caller.',
    'Original full materials are left unedited; callers must remove unused originals explicitly for a compact standalone export.',
)


@dataclass(frozen=True)
class SimpleMaterialOptions:
    face_light_direction: tuple[float, float, float]
    face_shadow_strength: float


@dataclass(frozen=True)
class SimpleMaterialRow:
    source_material: str
    material: str
    semantic_families: tuple[SemanticFamily, ...]
    source_identities: tuple[str, ...]
    source_shaders: tuple[str, ...]
    top_level_nodes: int
    calculation_nodes: int
    texture_samplers: int
    expression_drivers: int
    disposition: Literal['simplified', 'retained_vfx', 'retained_non_character']


@dataclass(frozen=True)
class SimpleMaterialReport:
    format: str
    materials: tuple[SimpleMaterialRow, ...]
    replaced_slots: int
    limitations: tuple[str, ...]


@dataclass(frozen=True)
class SimpleMaterialSurface:
    material: bpy.types.Material
    bsdf: bpy.types.Node
    channels: bpy.types.Node
    family: SemanticFamily
    source_group: bpy.types.ShaderNodeTree
    source_instance: bpy.types.Node


@dataclass(frozen=True)
class SourcePass:
    instance: bpy.types.Node
    family: SemanticFamily
    identity: str
    source_shadow_modes: tuple[int, ...]


def _json_object(value: object, label: str) -> dict[str, object]:
    if not isinstance(value, str):
        raise TypeError(f'Expected retained JSON string: {label}')
    result: object = json.loads(value)
    if not isinstance(result, dict) or any(not isinstance(key, str) for key in result):
        raise TypeError(f'Expected retained JSON object: {label}')
    return cast(dict[str, object], result)


def _number(values: dict[str, object], key: str) -> float:
    value = values[key]
    if type(value) not in (int, float) or not math.isfinite(cast(float, value)):
        raise TypeError(f'Expected finite source scalar: {key}={value!r}')
    return float(cast(float, value))


def _family(tree: bpy.types.ShaderNodeTree) -> SemanticFamily:
    shader = tree.get('source_shader')
    if shader == 'HGRP/CharacterNPR_VFX':
        return 'retained_vfx'
    if shader == 'HGRP/Lit':
        return 'retained_non_character'
    if shader not in SUPPORTED_SHADERS:
        raise ValueError(f'Unsupported simplified character shader: group={tree.name}, shader={shader!r}')
    if shader == 'HGRP/CharacterNPR_Skin':
        values = _json_object(tree.get('source_floats'), tree.name + '.source_floats')
        return 'face' if _number(values, '_UseSDFLightmap') == 1. else 'skin'
    if shader == 'HGRP/CharacterNPR_Hair':
        return 'hair'
    if shader == 'HGRP/CharacterNPR_Eye':
        return 'eyes'
    if shader == 'HGRP/CharacterNPR_OverlayShadow':
        return 'overlay'
    values = _json_object(tree.get('source_floats'), tree.name + '.source_floats')
    if values.get('_UseCharacterFur') == 1.:
        return 'fur'
    return 'stockings' if values.get('_SilkStockings') == 1. or values.get('_Pantyhose') == 1. else 'clothing'


def _validate_fur_source(tree: bpy.types.ShaderNodeTree) -> None:
    """Require the existing audited shell-alpha translation instead of inventing coverage."""
    contract = _json_object(tree.get('source_fur_shell_coverage'), tree.name + '.source_fur_shell_coverage')
    if (contract.get('status') != 'source_inputs_host_coverage_approximation'
        or contract.get('uv_layer') != 'UV1' or contract.get('layer_component') != 'X'):
        raise ValueError(f'Simplified Fur needs the retained UV1.x shell coverage contract: {tree.name}')
    outputs = tuple(item for item in tree.nodes if item.type == 'GROUP_OUTPUT' and item.is_active_output)
    if len(outputs) != 1 or not outputs[0].inputs['Coverage'].is_linked:
        raise ValueError(f'Simplified Fur requires its connected source shell Coverage output: {tree.name}')


def _source_passes(material: bpy.types.Material) -> tuple[SourcePass, ...]:
    if not material.use_nodes:
        raise ValueError(f'Character material requires source nodes: {material.name}')
    candidates = tuple(item for item in material.node_tree.nodes if item.type == 'GROUP'
        and item.node_tree is not None and item.node_tree.get('source_shader') is not None)
    stack_value: object = json.loads(material.get('source_material_stack', '[]'))
    if not isinstance(stack_value, list) or not stack_value:
        raise ValueError(f'Character material has no retained source material stack: {material.name}')
    result: list[SourcePass] = []
    for identity in stack_value:
        matches = tuple(item for item in candidates if json.loads(item.node_tree['source_identity']) == identity)
        # Compiled Skin rain retains duplicate dry/wet graphs for one identity.
        dry = tuple(item for item in matches if 'exact dry' in item.node_tree.name)
        selected = dry if dry else matches
        if len(selected) != 1:
            raise ValueError(f'Source character pass is absent or ambiguous: material={material.name}, identity={identity}, matches={len(selected)}')
        source = selected[0]
        family = _family(source.node_tree)
        values = _json_object(source.node_tree.get('source_floats'), source.node_tree.name + '.source_floats')
        if values.get('_AvatarCustomizeEnable') == 1.:
            raise ValueError(f'Simplified character material does not support active customization: {source.node_tree.name}')
        if family == 'fur':
            _validate_fur_source(source.node_tree)
        result.append(SourcePass(source, family, source.node_tree['source_identity'], ()))
    if (not any(item.family in {'retained_vfx', 'retained_non_character'} for item in result)
        and any(item.family == 'overlay' for item in result) and not all(item.family == 'overlay' for item in result)):
        raise ValueError(f'Mixed ordinary/overlay material stacks need an explicit simplified composition: {material.name}')
    return tuple(result)


def _label(tree: bpy.types.ShaderNodeTree, label: str) -> bpy.types.Node:
    matches = tuple(item for item in tree.nodes if item.label == label)
    if len(matches) != 1:
        raise ValueError(f'Expected one retained source node: tree={tree.name}, label={label}, matches={len(matches)}')
    return matches[0]


def source_head_shadow_mode(obj: bpy.types.Object) -> int | None:
    """Read explicit renderer evidence without inferring a missing casting flag."""
    raw = obj.get('source_renderer_pipeline_json')
    if raw is None:
        return None
    mode = _number(_json_object(raw, obj.name + '.source_renderer_pipeline_json'), 'm_CastShadows')
    if mode not in {0., 1., 2., 3.}:
        raise ValueError(f'Unsupported renderer shadow mode: object={obj.name}, mode={mode}')
    return int(mode)


def _geometry_passes(scene: bpy.types.Scene, material: bpy.types.Material,
    passes: tuple[SourcePass, ...]) -> tuple[SourcePass, ...]:
    """Resolve source Fur-card roles and head casting evidence without name tables."""
    owners = tuple(obj for obj in scene.objects if obj.type == 'MESH'
        and isinstance(obj.get('source_geometry_path'), str) and obj.get('source_renderer_identity') is not None
        and any(slot.material == material for slot in obj.material_slots))
    cards = tuple(obj for obj in owners if 'furcard' in obj['source_geometry_path'].rsplit('/', 1)[-1].casefold().split('_'))
    if not cards:
        if all(item.family in HEAD_SHADOW_FAMILIES for item in passes):
            head_owners = tuple(obj for obj in scene.objects if obj.type == 'MESH'
                and any(slot.material == material for slot in obj.material_slots))
            modes = tuple(source_head_shadow_mode(obj) for obj in head_owners)
            recorded = tuple(sorted({mode for mode in modes if mode is not None}))
            if any(mode is None for mode in modes):
                recorded += (-1,)
            return tuple(replace(item, source_shadow_modes=recorded) for item in passes)
        return passes
    companions = tuple(obj for obj in owners if 'eyebrow' in obj['source_geometry_path'].rsplit('/', 1)[-1].casefold().split('_'))
    if len(cards) + len(companions) != len(owners):
        raise ValueError(f'Source Fur-card material is shared with ordinary geometry; split its source material assignment before simplification: {material.name}')
    # Geometry names do not imply alpha testing; opaque LOD cards retain their source surface.
    if all(item.family == 'clothing' and item.instance.node_tree.get('source_shader') == 'HGRP/CharacterNPR'
           and _json_object(item.instance.node_tree['source_floats'],
                            item.instance.node_tree.name + '.source_floats').get('_EnableAlphaTest') == 0.
           for item in passes):
        return passes
    for source_pass in passes:
        source = source_pass.instance.node_tree
        values = _json_object(source['source_floats'], source.name + '.source_floats')
        mask = _json_object(source.get('source_uv2_alpha_mask'), source.name + '.source_uv2_alpha_mask')
        if (source.get('source_shader') != 'HGRP/CharacterNPR' or source_pass.family != 'clothing'
            or values.get('_EnableAlphaTest') != 1. or values.get('_Cull') != 0.
            or mask.get('status') != 'source_contract_host_coverage_approximation' or mask.get('uv_layer') != 'UV1'):
            raise ValueError(f'Source Fur-card geometry requires the retained double-sided alpha-tested UV1 coverage contract: {source.name}')
    modes = tuple(sorted({_number(_json_object(obj.get('source_renderer_pipeline_json'),
        obj.name + '.source_renderer_pipeline_json'), 'm_CastShadows') for obj in owners}))
    if any(mode not in {0., 1., 2., 3.} for mode in modes):
        raise ValueError(f'Source Fur-card renderer has an unsupported shadow-casting mode: {material.name}, modes={modes}')
    return tuple(SourcePass(item.instance, 'fur_cards', item.identity, tuple(int(mode) for mode in modes)) for item in passes)


def _copy_driver(source: bpy.types.NodeSocket, target: bpy.types.NodeSocket) -> None:
    owner = source.id_data
    animation = owner.animation_data
    if animation is None:
        return
    path = source.path_from_id('default_value')
    if animation.action is not None:
        raise ValueError(f'Keyframed source socket requires an explicit material animation adapter: {owner.name}/{path}')
    for curve in animation.drivers:
        if curve.data_path != path:
            continue
        copied = target.driver_add('default_value', curve.array_index) if source.type in {'RGBA', 'VECTOR'} else target.driver_add('default_value')
        copied.driver.type = curve.driver.type
        copied.driver.use_self = curve.driver.use_self
        for variable in curve.driver.variables:
            new = copied.driver.variables.new()
            new.name = variable.name
            new.type = variable.type
            for original, destination in zip(variable.targets, new.targets, strict=True):
                destination.id_type = original.id_type
                destination.id = original.id
                destination.data_path = original.data_path
                destination.bone_target = original.bone_target
                destination.transform_type = original.transform_type
                destination.transform_space = original.transform_space
                destination.rotation_mode = original.rotation_mode
        copied.driver.expression = curve.driver.expression


def _constant(tree: bpy.types.ShaderNodeTree, source: bpy.types.NodeSocket) -> bpy.types.NodeSocket:
    if source.type in {'VALUE', 'INT', 'BOOLEAN'}:
        result = node(tree, 'ShaderNodeValue', source.name, 'Simple source controls')
        result.outputs[0].default_value = float(source.default_value)
        target = result.outputs[0]
    elif source.type == 'RGBA':
        result = node(tree, 'ShaderNodeRGB', source.name, 'Simple source controls')
        result.outputs[0].default_value = tuple(source.default_value)
        target = result.outputs[0]
    elif source.type == 'VECTOR':
        result = node(tree, 'ShaderNodeCombineXYZ', source.name, 'Simple source controls')
        for socket, value in zip(result.inputs, source.default_value, strict=True):
            socket.default_value = value
        if source.id_data.animation_data is not None:
            path = source.path_from_id('default_value')
            if any(curve.data_path == path for curve in source.id_data.animation_data.drivers):
                raise ValueError(f'Driven vector interface requires explicit component routing: {path}')
        return result.outputs[0]
    else:
        raise ValueError(f'Unsupported source constant socket: {source.node.name}/{source.name}/{source.type}')
    _copy_driver(source, target)
    return target


def _copy_node(tree: bpy.types.ShaderNodeTree, original: bpy.types.Node) -> bpy.types.Node:
    result = tree.nodes.new(original.bl_idname)
    excluded = {'rna_type', 'name', 'label', 'location', 'width', 'height', 'dimensions', 'parent', 'inputs', 'outputs', 'internal_links', 'select'}
    for prop in original.bl_rna.properties:
        if prop.identifier in excluded or prop.is_readonly or prop.type == 'COLLECTION':
            continue
        if prop.type == 'POINTER' and prop.identifier not in {'image', 'object', 'node_tree'}:
            continue
        setattr(result, prop.identifier, getattr(original, prop.identifier))
    result.label = original.label
    result.name = 'Source / ' + original.name
    for key, value in original.items():
        result[key] = value
    return result


def _slice_socket_retained(tree: bpy.types.ShaderNodeTree, socket: bpy.types.NodeSocket,
    callers: tuple[bpy.types.Node, ...], cache: dict[tuple[int, tuple[int, ...]], bpy.types.Node],
    constants: dict[tuple[int, tuple[int, ...]], bpy.types.NodeSocket],
    retained_groups: Mapping[int, bpy.types.ShaderNodeTree]) -> bpy.types.NodeSocket:
    """Inline only the requested upstream graph, preserving source driver targets."""
    context = tuple(item.as_pointer() for item in callers)
    if not socket.is_output:
        if socket.is_linked:
            if len(socket.links) != 1:
                raise ValueError(f'Ambiguous source socket: {socket.node.name}/{socket.name}')
            return _slice_socket_retained(tree, socket.links[0].from_socket, callers, cache, constants, retained_groups)
        key = (socket.as_pointer(), context)
        if key not in constants:
            constants[key] = _constant(tree, socket)
        return constants[key]
    original = socket.node
    if original.type == 'GROUP_INPUT':
        if not callers:
            raise ValueError(f'Unowned source group input: {original.id_data.name}/{socket.name}')
        actual = next(value for value in callers[-1].inputs if value.identifier == socket.identifier)
        return _slice_socket_retained(tree, actual, callers[:-1], cache, constants, retained_groups)
    if original.type == 'GROUP':
        replacement = retained_groups.get(original.node_tree.as_pointer())
        if replacement is not None:
            key = (original.as_pointer(), context)
            if key not in cache:
                result = _copy_node(tree, original)
                result.node_tree = replacement
                cache[key] = result
                for index, value in enumerate(original.inputs):
                    if not hasattr(value, 'default_value'):
                        continue
                    result.inputs[index].default_value = value.default_value
                    if value.is_linked:
                        tree.links.new(_slice_socket_retained(tree, value, callers, cache, constants, retained_groups), result.inputs[index])
                    else:
                        _copy_driver(value, result.inputs[index])
            return next(value for value in cache[key].outputs if value.identifier == socket.identifier)
        outputs = tuple(item for item in original.node_tree.nodes if item.type == 'GROUP_OUTPUT' and item.is_active_output)
        if len(outputs) != 1:
            raise ValueError(f'Expected one active source group output: {original.node_tree.name}')
        actual = next(value for value in outputs[0].inputs if value.identifier == socket.identifier)
        return _slice_socket_retained(tree, actual, callers + (original,), cache, constants, retained_groups)
    key = (original.as_pointer(), context)
    if key not in cache:
        if original.type in {'SCRIPT', 'CUSTOM', 'BSDF_PRINCIPLED', 'EMISSION', 'VALTORGB', 'CURVE_RGB', 'CURVE_VEC'}:
            raise ValueError(f'Unexpected lighting node in pre-lighting source slice: {original.id_data.name}/{original.name}')
        result = _copy_node(tree, original)
        cache[key] = result
        for index, value in enumerate(original.inputs):
            if not hasattr(value, 'default_value'):
                continue
            target = result.inputs[index]
            target.default_value = value.default_value
            if value.is_linked:
                tree.links.new(_slice_socket_retained(tree, value, callers, cache, constants, retained_groups), target)
            else:
                _copy_driver(value, target)
        for index, value in enumerate(original.outputs):
            if hasattr(value, 'default_value'):
                result.outputs[index].default_value = value.default_value
                _copy_driver(value, result.outputs[index])
    index = tuple(original.outputs).index(socket)
    return cache[key].outputs[index]


def _slice_socket(tree: bpy.types.ShaderNodeTree, socket: bpy.types.NodeSocket,
    callers: tuple[bpy.types.Node, ...], cache: dict[tuple[int, tuple[int, ...]], bpy.types.Node],
    constants: dict[tuple[int, tuple[int, ...]], bpy.types.NodeSocket]) -> bpy.types.NodeSocket:
    return _slice_socket_retained(tree, socket, callers, cache, constants, {})


def _sample(source: bpy.types.ShaderNodeTree, slot: str) -> bpy.types.Node:
    matches = tuple(item for item in source.nodes if item.type == 'TEX_IMAGE' and item.get('source_slot') == slot)
    if len(matches) != 1 or matches[0].image is None:
        raise ValueError(f'Required source texture is absent or ambiguous: tree={source.name}, slot={slot}, matches={len(matches)}')
    return matches[0]


def softened_face_normal(tree: bpy.types.ShaderNodeTree, original_normal: bpy.types.NodeSocket,
                         head_forward: bpy.types.NodeSocket, normal_detail: bpy.types.NodeSocket) -> bpy.types.NodeSocket:
    """Preserve a head-oriented diffuse shape while accepting real scene illumination."""
    forward = node(tree, 'ShaderNodeVectorMath', 'Unit head forward', 'Simple face normal')
    forward.operation = 'NORMALIZE'
    tree.links.new(head_forward, forward.inputs[0])
    detail = node(tree, 'ShaderNodeVectorMath', 'Retained face curvature', 'Simple face normal')
    detail.operation = 'SCALE'
    tree.links.new(original_normal, detail.inputs[0])
    tree.links.new(normal_detail, detail.inputs['Scale'])
    smooth = node(tree, 'ShaderNodeVectorMath', 'Head-oriented soft lighting', 'Simple face normal')
    smooth.operation = 'SCALE'
    tree.links.new(forward.outputs[0], smooth.inputs[0])
    weight = math_node(tree, 'SUBTRACT', 1., normal_detail, 'Face smoothing weight', 'Simple face normal')
    tree.links.new(weight, smooth.inputs['Scale'])
    combined = node(tree, 'ShaderNodeVectorMath', 'Blend face normals', 'Simple face normal')
    combined.operation = 'ADD'
    tree.links.new(detail.outputs[0], combined.inputs[0])
    tree.links.new(smooth.outputs[0], combined.inputs[1])
    normalized = node(tree, 'ShaderNodeVectorMath', 'Unit softened face normal', 'Simple face normal')
    normalized.operation = 'NORMALIZE'
    tree.links.new(combined.outputs[0], normalized.inputs[0])
    return normalized.outputs[0]


def _semantic_group(family: SemanticFamily, source: bpy.types.ShaderNodeTree) -> bpy.types.ShaderNodeTree:
    """Decode surface channels and optionally soften the head-oriented face normal."""
    tree = bpy.data.node_groups.new('EF Simple ' + family.title() + ' / ' + source.name.removeprefix('EF Character '), 'ShaderNodeTree')
    tree['endfield_simple_calculation'] = True
    tree['source_identity'] = source['source_identity']
    for name, kind in (('Albedo', 'NodeSocketColor'), ('Coverage', 'NodeSocketFloat'),
        ('Packed Surface', 'NodeSocketColor'), ('Packed Smoothness', 'NodeSocketFloat'),
        ('Packed Normal', 'NodeSocketColor'), ('Packed Normal Alpha', 'NodeSocketFloat'),
        ('Normal Strength', 'NodeSocketFloat'), ('Face Shade', 'NodeSocketFloat')):
        tree.interface.new_socket(name=name, in_out='INPUT', socket_type=kind)
    for name, kind in (('Base Color', 'NodeSocketColor'), ('Coverage', 'NodeSocketFloat'),
        ('Roughness', 'NodeSocketFloat'), ('Metallic', 'NodeSocketFloat'),
        ('Specular IOR Level', 'NodeSocketFloat'), ('Normal', 'NodeSocketVector')):
        tree.interface.new_socket(name=name, in_out='OUTPUT', socket_type=kind)
    if family == 'hair':
        roughness_control = tree.interface.new_socket(name='Hair Roughness', in_out='INPUT', socket_type='NodeSocketFloat')
        roughness_control.default_value = .48
        roughness_control.min_value = 0.
        roughness_control.max_value = 1.
    if family == 'face':
        tree.interface.new_socket(name='Face Forward', in_out='INPUT', socket_type='NodeSocketVector')
        for label, value, maximum in (('Face Brightness', 1., 2.), ('Face Normal Detail', .35, 1.)):
            control = tree.interface.new_socket(name=label, in_out='INPUT', socket_type='NodeSocketFloat')
            control.default_value = value
            control.min_value = 0.
            control.max_value = maximum
    entry = node(tree, 'NodeGroupInput', 'Source sampled channels', 'Simple inputs')
    output = node(tree, 'NodeGroupOutput', 'Stock Blender surface channels', 'Simple output')
    values = _json_object(source['source_floats'], source.name + '.source_floats')
    color = entry.outputs['Albedo']
    if family == 'face':
        color = color_mix(tree, 'MULTIPLY', 1., color, entry.outputs['Face Shade'], 'Compact face shadow', 'Simple face')
        color = color_mix(tree, 'MULTIPLY', 1., color, entry.outputs['Face Brightness'], 'Face albedo brightness', 'Simple face')
    packed = values.get('_UseMetallicGlossMap') == 1.
    if packed:
        channels = node(tree, 'ShaderNodeSeparateColor',
            'R strand blend G spec B AO' if family == 'hair' else 'R metal G spec B AO', 'Simple packing')
        tree.links.new(entry.outputs['Packed Surface'], channels.inputs[0])
        color = color_mix(tree, 'MULTIPLY', 1., color, channels.outputs['Blue'], 'Source packed ambient occlusion', 'Simple packing')
        if family == 'hair':
            output.inputs['Metallic'].default_value = 0.
            tree.links.new(entry.outputs['Hair Roughness'], output.inputs['Roughness'])
            specular = math_node(tree, 'MULTIPLY', channels.outputs['Green'], .5, 'Source dielectric F0 0.04 times G at stock IOR 1.5', 'Simple hair response')
            tree.links.new(specular, output.inputs['Specular IOR Level'])
        else:
            tree.links.new(channels.outputs['Red'], output.inputs['Metallic'])
            tree.links.new(channels.outputs['Green'], output.inputs['Specular IOR Level'])
            roughness = math_node(tree, 'SUBTRACT', 1., entry.outputs['Packed Smoothness'], 'One minus source smoothness', 'Simple packing')
            tree.links.new(roughness, output.inputs['Roughness'])
    elif family == 'hair':
        tree.links.new(entry.outputs['Hair Roughness'], output.inputs['Roughness'])
        output.inputs['Metallic'].default_value = 0.
        output.inputs['Specular IOR Level'].default_value = .5 * _number(values, '_Specular')
    elif family == 'overlay':
        output.inputs['Roughness'].default_value = 1.
        output.inputs['Metallic'].default_value = 0.
        output.inputs['Specular IOR Level'].default_value = 0.
    else:
        for name, value in (('Roughness', 1. - _number(values, '_Smoothness')),
            ('Metallic', _number(values, '_Metallic')), ('Specular IOR Level', _number(values, '_Specular'))):
            output.inputs[name].default_value = value
    tree.links.new(color, output.inputs['Base Color'])
    tree.links.new(entry.outputs['Coverage'], output.inputs['Coverage'])
    normal_slots = {item.get('source_slot') for item in source.nodes if item.type == 'TEX_IMAGE'}
    normal_slot = '_SplitNormalMap' if family == 'hair' and '_SplitNormalMap' in normal_slots else '_BumpMap'
    if (values.get('_UseBumpMap') == 1. or family == 'hair') and normal_slot in normal_slots:
        # Reuse the established exact source channel decode without its textures.
        normal = _decode_sample(tree, entry, normal_slot)
        tree.links.new(normal, output.inputs['Normal'])
    else:
        geometry = node(tree, 'ShaderNodeNewGeometry', 'Unmodified surface normal', 'Simple normal')
        tree.links.new(geometry.outputs['Normal'], output.inputs['Normal'])
    if family == 'face':
        original_normal = output.inputs['Normal'].links[0].from_socket
        normal = softened_face_normal(tree, original_normal, entry.outputs['Face Forward'], entry.outputs['Face Normal Detail'])
        tree.links.new(normal, output.inputs['Normal'])
    for index, item in enumerate(tree.nodes):
        item.location = (index % 6 * 200., -(index // 6) * 180.)
    return tree


def _decode_sample(tree: bpy.types.ShaderNodeTree, entry: bpy.types.Node, slot: str) -> bpy.types.NodeSocket:
    """Adapt the existing decoder's named sample interface without a sampler."""
    adapter_tree = bpy.data.node_groups.new('EF Simple packed normal input', 'ShaderNodeTree')
    for label in ('Color', 'Alpha'):
        adapter_tree.interface.new_socket(name=label, in_out='INPUT', socket_type='NodeSocketColor' if label == 'Color' else 'NodeSocketFloat')
        adapter_tree.interface.new_socket(name=label, in_out='OUTPUT', socket_type='NodeSocketColor' if label == 'Color' else 'NodeSocketFloat')
    adapter_in = adapter_tree.nodes.new('NodeGroupInput')
    adapter_out = adapter_tree.nodes.new('NodeGroupOutput')
    for label in ('Color', 'Alpha'):
        adapter_tree.links.new(adapter_in.outputs[label], adapter_out.inputs[label])
    adapter = tree.nodes.new('ShaderNodeGroup')
    adapter.node_tree = adapter_tree
    tree.links.new(entry.outputs['Packed Normal'], adapter.inputs['Color'])
    tree.links.new(entry.outputs['Packed Normal Alpha'], adapter.inputs['Alpha'])
    normal = decode_normal(tree, adapter, entry.outputs['Normal Strength'], slot == '_BumpMap', slot)
    # Replace adapter outputs with their inputs; no retained helper group needed.
    for label, original in (('Color', entry.outputs['Packed Normal']), ('Alpha', entry.outputs['Packed Normal Alpha'])):
        for link in tuple(adapter.outputs[label].links):
            tree.links.new(original, link.to_socket)
    tree.nodes.remove(adapter)
    bpy.data.node_groups.remove(adapter_tree)
    return normal


def _face_shade(tree: bpy.types.ShaderNodeTree, source: SourcePass, options: SimpleMaterialOptions,
    cache: dict[tuple[int, tuple[int, ...]], bpy.types.Node], constants: dict[tuple[int, tuple[int, ...]], bpy.types.NodeSocket],
    face_channels: bpy.types.Node) -> bpy.types.NodeSocket:
    """Use source SDF RG and MaskG with a bounded host light threshold."""
    original = source.instance.node_tree
    callers = (source.instance,)
    light = Vector(options.face_light_direction).normalized()
    light_direction = node(tree, 'ShaderNodeCombineXYZ', 'Compact face designated Sun direction', 'Simple face SDF')
    light_direction['endfield_simple_face_light'] = True
    for axis, value in enumerate(light):
        light_direction.inputs[axis].default_value = value
    light_unit = node(tree, 'ShaderNodeVectorMath', 'Normalize designated Sun direction', 'Simple face SDF')
    light_unit.operation = 'NORMALIZE'
    tree.links.new(light_direction.outputs[0], light_unit.inputs[0])
    native_nodes = tuple(item for item in original.nodes if item.label == 'Native Face SDF / Explicit Adjustable Basis')
    if not native_nodes:
        owners = tuple(group for group in bpy.data.node_groups if any(item.label == 'Native Face SDF / Explicit Adjustable Basis' for item in group.nodes))
        if len(owners) != 1:
            raise ValueError(f'Compact face SDF requires one explicit existing face basis: {original.name}, owners={len(owners)}')
        native_nodes = tuple(item for item in owners[0].nodes if item.label == 'Native Face SDF / Explicit Adjustable Basis')
    if len(native_nodes) != 1:
        raise ValueError(f'Compact face SDF source basis is ambiguous: {original.name}')
    native = native_nodes[0]
    right = _slice_socket(tree, native.inputs['Face Right'], callers, cache, constants)
    forward = _slice_socket(tree, native.inputs['Face Forward'], callers, cache, constants)
    tree.links.new(forward, face_channels.inputs['Face Forward'])
    side = node(tree, 'ShaderNodeVectorMath', 'Compact SDF light side', 'Simple face SDF')
    side.operation = 'DOT_PRODUCT'
    tree.links.new(right, side.inputs[0]); tree.links.new(light_unit.outputs[0], side.inputs[1])
    ahead = node(tree, 'ShaderNodeVectorMath', 'Compact SDF forward light', 'Simple face SDF')
    ahead.operation = 'DOT_PRODUCT'
    tree.links.new(forward, ahead.inputs[0]); tree.links.new(light_unit.outputs[0], ahead.inputs[1])
    original_sample = _sample(original, '_SDFLightmap')
    uv = _slice_socket(tree, original_sample.inputs['Vector'], callers, cache, constants)
    components = node(tree, 'ShaderNodeSeparateXYZ', 'Source face UV', 'Simple face SDF')
    tree.links.new(uv, components.inputs[0])
    mirrored = math_node(tree, 'SUBTRACT', 1., components.outputs['X'], 'Mirror face U', 'Simple face SDF')
    choose = math_node(tree, 'GREATER_THAN', side.outputs['Value'], 0., 'Choose lit face side', 'Simple face SDF')
    mirror = node(tree, 'ShaderNodeMixRGB', 'Mirrored source SDF U', 'Simple face SDF')
    tree.links.new(choose, mirror.inputs[0]); tree.links.new(mirrored, mirror.inputs[1]); tree.links.new(components.outputs['X'], mirror.inputs[2])
    mapped = node(tree, 'ShaderNodeCombineXYZ', 'Compact SDF coordinates', 'Simple face SDF')
    tree.links.new(mirror.outputs[0], mapped.inputs['X']); tree.links.new(components.outputs['Y'], mapped.inputs['Y'])
    sample = _copy_node(tree, original_sample)
    sample.label = 'Compact face SDF / original source image'
    tree.links.new(mapped.outputs[0], sample.inputs['Vector'])
    channels = node(tree, 'ShaderNodeSeparateColor', 'Source SDF RG thresholds', 'Simple face SDF')
    tree.links.new(sample.outputs['Color'], channels.inputs[0])
    threshold = math_node(tree, 'MULTIPLY', .5, math_node(tree, 'ADD', channels.outputs['Red'], channels.outputs['Green'], 'SDF R plus G', 'Simple face SDF'), 'Source SDF half threshold', 'Simple face SDF')
    cutoff = math_node(tree, 'MULTIPLY_ADD', ahead.outputs['Value'], -.5, 'Compact SDF cutoff', 'Simple face SDF')
    cutoff.node.inputs[2].default_value = .5
    lit = math_node(tree, 'GREATER_THAN', threshold, cutoff, 'Compact SDF lit mask', 'Simple face SDF')
    mask = _slice_socket(tree, _sample(original, '_SDFMask').outputs['Color'], callers, cache, constants)
    mask_channels = node(tree, 'ShaderNodeSeparateColor', 'Source mask G preserves surface', 'Simple face SDF')
    tree.links.new(mask, mask_channels.inputs[0])
    amount = math_node(tree, 'MULTIPLY', math_node(tree, 'SUBTRACT', 1., mask_channels.outputs['Green'], 'Face mask complement', 'Simple face SDF'), options.face_shadow_strength, 'Face artist shadow strength', 'Simple face SDF')
    shade = math_node(tree, 'SUBTRACT', 1., math_node(tree, 'MULTIPLY', math_node(tree, 'SUBTRACT', 1., lit, 'SDF shadow mask', 'Simple face SDF'), amount, 'Bounded face shadow', 'Simple face SDF'), 'Face brightness', 'Simple face SDF')
    return shade


def _build_pass(material: bpy.types.Material, source: SourcePass, options: SimpleMaterialOptions) -> SimpleMaterialSurface:
    tree = material.node_tree
    original = source.instance.node_tree
    values = _json_object(original['source_floats'], original.name + '.source_floats')
    cache: dict[tuple[int, tuple[int, ...]], bpy.types.Node] = {}
    constants: dict[tuple[int, tuple[int, ...]], bpy.types.NodeSocket] = {}
    callers = (source.instance,)
    calculations = _semantic_group(source.family, original)
    channels = node(tree, 'ShaderNodeGroup', 'Simple ' + source.family.title() + ' / source channels', 'Simple semantic calculations')
    channels.node_tree = calculations
    channels['endfield_simple_channels'] = True
    channels['source_group'] = original.name
    channels['source_instance'] = source.instance.name
    channels['semantic_family'] = source.family
    if source.family == 'overlay':
        color = _label(original, 'Source overlay tint').outputs[0]
    elif original.get('source_fur_dye') is not None:
        color = _label(original, 'Source Fur dye ScreenBlend host approximation').outputs[0]
    else:
        emotional = tuple(item for item in original.nodes if item.label == 'Compiled face emotion atlas')
        color = emotional[0].outputs['Color'] if emotional else _label(original, 'Source base sample times BaseColor').outputs[0]
    tree.links.new(_slice_socket(tree, color, callers, cache, constants), channels.inputs['Albedo'])
    output = next(item for item in original.nodes if item.type == 'GROUP_OUTPUT' and item.is_active_output)
    tree.links.new(_slice_socket(tree, output.inputs['Coverage'], callers, cache, constants), channels.inputs['Coverage'])
    if values.get('_UseMetallicGlossMap') == 1.:
        packed = _sample(original, '_MetallicGlossMap')
        for target, source_output in (('Packed Surface', 'Color'), ('Packed Smoothness', 'Alpha')):
            tree.links.new(_slice_socket(tree, packed.outputs[source_output], callers, cache, constants), channels.inputs[target])
    normal_slot = '_SplitNormalMap' if source.family == 'hair' else '_BumpMap'
    normals = tuple(item for item in original.nodes if item.type == 'TEX_IMAGE' and item.get('source_slot') == normal_slot)
    if normals and (values.get('_UseBumpMap') == 1. or source.family == 'hair'):
        for target, source_output in (('Packed Normal', 'Color'), ('Packed Normal Alpha', 'Alpha')):
            tree.links.new(_slice_socket(tree, normals[0].outputs[source_output], callers, cache, constants), channels.inputs[target])
        channels.inputs['Normal Strength'].default_value = _number(values, '_BumpScale')
    channels.inputs['Face Shade'].default_value = 1.
    if source.family == 'face':
        tree.links.new(_face_shade(tree, source, options, cache, constants, channels), channels.inputs['Face Shade'])
        for label, title, value in (('Face Brightness', 'Face Brightness / 面部亮度', 1.),
                                    ('Face Normal Detail', 'Face Normal Detail / 面部立体度', .35)):
            control = node(tree, 'ShaderNodeValue', title, 'Simple face artist controls')
            control.name = 'EF Simple ' + label
            control.outputs[0].default_value = value
            tree.links.new(control.outputs[0], channels.inputs[label])
    bsdf = node(tree, 'ShaderNodeBsdfPrincipled', 'Stock Blender ' + source.family.title(), 'Simple stock surface')
    bsdf.name = 'EF Simple Surface'
    bsdf['endfield_simple_surface'] = True
    bsdf['semantic_family'] = source.family
    channels['surface_node'] = bsdf.name
    for label in ('Base Color', 'Roughness', 'Metallic', 'Specular IOR Level', 'Normal'):
        tree.links.new(channels.outputs[label], bsdf.inputs[label])
    if source.family == 'hair':
        roughness = node(tree, 'ShaderNodeValue', 'Hair Roughness / 发丝粗糙度', 'Simple hair artist controls')
        roughness.name = 'EF Simple Hair Roughness'
        roughness.outputs[0].default_value = .48
        tree.links.new(roughness.outputs[0], channels.inputs['Hair Roughness'])
        bsdf.inputs['Anisotropic'].default_value = .45
        tangent = node(tree, 'ShaderNodeTangent', 'UV0 strand direction / host approximation', 'Simple hair')
        tangent.direction_type = 'UV_MAP'; tangent.uv_map = 'UV0'
        tree.links.new(tangent.outputs[0], bsdf.inputs['Tangent'])
    if source.family == 'eyes':
        bsdf.inputs['Coat Weight'].default_value = .3
        bsdf.inputs['Coat Roughness'].default_value = .12
    if source.family == 'stockings':
        bsdf.inputs['Sheen Weight'].default_value = .2
        bsdf.inputs['Anisotropic'].default_value = .25
    if source.family in {'skin', 'face'}:
        bsdf.inputs['Subsurface Weight'].default_value = .04
    if values.get('_UseEmission') == 1.:
        emission = tuple(item for item in original.nodes if item.type == 'BSDF_PRINCIPLED')
        if len(emission) != 1:
            raise ValueError(f'Expected source emission Principled baseline: {original.name}')
        for label in ('Emission Color', 'Emission Strength'):
            tree.links.new(_slice_socket(tree, emission[0].inputs[label], callers, cache, constants), bsdf.inputs[label])
    channels['surface_closure'] = bsdf.name
    if source.family in {'eyes', 'hair', 'fur_cards'}:
        flat = node(tree, 'ShaderNodeEmission', 'Source-style flat ' + source.family + ' diffuse', 'Simple NPR diffuse fill')
        flat['endfield_simple_flat_surface'] = True
        sun_color = node(tree, 'ShaderNodeRGB', 'Designated Sun color / flat diffuse approximation', 'Simple NPR diffuse fill')
        sun_color.outputs[0].default_value = (1., 1., 1., 1.)
        sun_color['endfield_simple_sun_color'] = True
        tint = color_mix(tree, 'MULTIPLY', 1., channels.outputs['Base Color'], sun_color.outputs[0], 'Flat source albedo times designated Sun color', 'Simple NPR diffuse fill')
        tint.node['endfield_simple_flat_color'] = True
        tree.links.new(tint, flat.inputs['Color'])
        channels['surface_closure'] = flat.name
        channels['unlit_node'] = flat.name
        if source.family in {'hair', 'fur_cards'}:
            closure = node(tree, 'ShaderNodeMixShader', '75 percent diffuse fill / 25 percent stock surface response', 'Simple source-card host approximation')
            fill = node(tree, 'ShaderNodeValue',
                'Hair Diffuse Fill / 发丝柔光填充' if source.family == 'hair' else 'Fur Card Diffuse Fill / 毛片柔光填充', 'Simple source-card artist controls')
            fill.name = 'EF Simple Hair Diffuse Fill' if source.family == 'hair' else 'EF Simple Fur Card Diffuse Fill'
            fill.outputs[0].default_value = .75
            tree.links.new(fill.outputs[0], closure.inputs[0])
            tree.links.new(bsdf.outputs[0], closure.inputs[1])
            tree.links.new(flat.outputs[0], closure.inputs[2])
            channels['surface_closure'] = closure.name
        else:
            bsdf.label = 'Editable weather surface channels / ' + source.family
            bsdf.hide = True
    return SimpleMaterialSurface(material, bsdf, channels, source.family, original, source.instance)


def _build_material(source: bpy.types.Material, passes: tuple[SourcePass, ...], options: SimpleMaterialOptions) -> tuple[bpy.types.Material, SimpleMaterialRow]:
    retained = tuple(item.family for item in passes if item.family in {'retained_vfx', 'retained_non_character'})
    if retained:
        tree = source.node_tree
        textures = sum(item.type == 'TEX_IMAGE' for item in tree.nodes)
        drivers = len(tree.animation_data.drivers) if tree.animation_data else 0
        row = SimpleMaterialRow(source.name, source.name, tuple(item.family for item in passes),
            tuple(item.identity for item in passes), tuple(item.instance.node_tree['source_shader'] for item in passes),
            len(tree.nodes), 0, textures, drivers,
            'retained_vfx' if 'retained_vfx' in retained else 'retained_non_character')
        return source, row
    material = bpy.data.materials.new('EF Simple / ' + source.name.removeprefix('EFCHAR '))
    material.use_nodes = True
    material.node_tree.nodes.clear()
    material['endfield_simple_material'] = True
    material['source_full_material'] = source.name
    material['source_material_stack'] = source['source_material_stack']
    material['lighting_precision'] = 'simple'
    material['fidelity'] = 'Stock Blender surface approximation with compact face SDF; original source maps and atlas controls.'
    material.surface_render_method = 'DITHERED'
    material.use_backface_culling = source.use_backface_culling
    material.use_transparency_overlap = source.use_transparency_overlap
    tree = material.node_tree
    previous = node(tree, 'ShaderNodeBsdfTransparent', 'Transparent pass foundation', 'Simple pass composition').outputs[0]
    if all(source.family == 'overlay' for source in passes):
        factor: bpy.types.NodeSocket | None = None
        for source_pass in passes:
            source_output = next(item for item in source_pass.instance.node_tree.nodes if item.type == 'GROUP_OUTPUT' and item.is_active_output)
            value = _slice_socket(tree, source_output.inputs['Multiply Factor'], (source_pass.instance,), {}, {})
            factor = value if factor is None else color_mix(tree, 'MULTIPLY', 1., factor, value, 'Multiply ordered source overlay factors', 'Simple overlay transmission')
        tree.links.new(factor, previous.node.inputs['Color'])
        material.surface_render_method = 'BLENDED'
        material['overlay_scope'] = 'Original source multiplicative transmission; native stencil/depth/framebuffer behavior remains approximate.'
    else:
        for source_pass in passes:
            surface = _build_pass(material, source_pass, options)
            mix = node(tree, 'ShaderNodeMixShader', 'Source ordered compact pass', 'Simple pass composition')
            tree.links.new(surface.channels.outputs['Coverage'], mix.inputs[0])
            closure = tree.nodes[surface.channels['surface_closure']]
            tree.links.new(previous, mix.inputs[1]); tree.links.new(closure.outputs[0], mix.inputs[2])
            previous = mix.outputs[0]
    if all(item.family in HEAD_SHADOW_FAMILIES | {'fur_cards'} and item.source_shadow_modes == (0,) for item in passes):
        path = node(tree, 'ShaderNodeLightPath', 'Source renderers disable shadow casting', 'Source renderer compatibility')
        transparent = node(tree, 'ShaderNodeBsdfTransparent', 'No shadow from source non-casting cards', 'Source renderer compatibility')
        shadow = node(tree, 'ShaderNodeMixShader', 'Preserve unanimous source m_CastShadows zero', 'Source renderer compatibility')
        tree.links.new(path.outputs['Is Shadow Ray'], shadow.inputs[0])
        tree.links.new(previous, shadow.inputs[1]); tree.links.new(transparent.outputs[0], shadow.inputs[2])
        previous = shadow.outputs[0]
        material['source_shadow_casting'] = 'disabled_by_unanimous_source_renderers'
    output = node(tree, 'ShaderNodeOutputMaterial', 'Simple material output', 'Simple output')
    tree.links.new(previous, output.inputs['Surface'])
    layout_simple_character_material(material)
    textures = tuple(item for item in tree.nodes if item.type == 'TEX_IMAGE')
    calculations = sum(len(item.node_tree.nodes) for item in tree.nodes if item.get('endfield_simple_channels'))
    drivers = len(tree.animation_data.drivers) if tree.animation_data else 0
    row = SimpleMaterialRow(source.name, material.name, tuple(item.family for item in passes),
        tuple(item.identity for item in passes), tuple(item.instance.node_tree['source_shader'] for item in passes),
        len(tree.nodes), calculations, len(textures), drivers, 'simplified')
    return material, row


def apply_simple_character_materials(scene: bpy.types.Scene, options: SimpleMaterialOptions) -> SimpleMaterialReport:
    """Replace character slots after validating every source family; retain originals."""
    if (len(options.face_light_direction) != 3 or any(not math.isfinite(value) for value in options.face_light_direction)
        or Vector(options.face_light_direction).length < 1e-6):
        raise ValueError(f'Face light direction must be finite and nonzero: {options.face_light_direction}')
    if not math.isfinite(options.face_shadow_strength) or not 0. <= options.face_shadow_strength <= 1.:
        raise ValueError(f'Face shadow strength must be in [0, 1]: {options.face_shadow_strength}')
    bindings = tuple((obj, slot) for obj in scene.objects if obj.type == 'MESH' for slot in obj.material_slots
        if slot.material is not None and 'source_material_stack' in slot.material)
    slots = tuple(slot for _, slot in bindings)
    materials = {slot.material.name: slot.material for slot in slots}
    if not materials:
        raise ValueError('No retained source character material slots exist in the selected scene')
    if any(material.get('endfield_simple_material') for material in materials.values()):
        raise ValueError('Selected scene already contains simplified materials; restore original slots before rebuilding')
    plans = tuple((material, _geometry_passes(scene, material, _source_passes(material))) for material in materials.values())
    builds = tuple(_build_material(material, passes, options) for material, passes in plans)
    replacements = {row.source_material: material for material, row in builds}
    split_plans = tuple((material, passes) for material, passes in plans
        if all(item.family in HEAD_SHADOW_FAMILIES and 0 in item.source_shadow_modes
               and len(item.source_shadow_modes) > 1 for item in passes))
    split_builds = tuple(_build_material(material, tuple(replace(item, source_shadow_modes=(0,))
        for item in passes), options) for material, passes in split_plans)
    no_cast = {row.source_material: material for material, row in split_builds}
    for obj, slot in bindings:
        source_name = slot.material.name
        slot.material = (no_cast[source_name] if source_name in no_cast and source_head_shadow_mode(obj) == 0
                         else replacements[source_name])
    builds += split_builds
    limits = list(LIMITATIONS)
    retained = tuple(row.material for _, row in builds if row.disposition != 'simplified')
    if retained:
        limits.append('Special-effect and non-character source materials are retained verbatim, not simplified or routed through surface weather: ' + ', '.join(retained))
    if any(source_pass.family == 'fur' for _, passes in plans for source_pass in passes):
        limits.append('Fur shells retain the exact existing source Coverage dependency graph and UV1.x shell mask. Stock surface lighting omits native Fur transmission and vertex expansion.')
    if any(source_pass.family == 'fur_cards' for _, passes in plans for source_pass in passes):
        limits.append('Fur-card diffuse style is inferred from the authored source geometry role token furcard plus double-sided alpha-tested UV1 coverage; shared eyebrow strand-card owners may use the same material. It blends 75 percent designated-Sun albedo fill with 25 percent stock surface. Generic packed surface channels and exact retained coverage remain unchanged; fill approximates native soft card diffuse.')
        limits.append('Fur-card materials whose source renderer owners unanimously set m_CastShadows=0 retain visible coverage and use a transparent shadow-ray closure in Eevee and Cycles.')
        mixed = tuple(material.name for material, passes in plans if any(item.family == 'fur_cards' and len(item.source_shadow_modes) > 1 for item in passes))
        if mixed:
            limits.append('Shared Fur-card materials with conflicting renderer shadow modes retain stock shadow casting; per-owner no-cast versus shadow-only proxy state needs a separate material split: ' + ', '.join(mixed))
    curves = tuple(obj.name for obj in scene.objects if obj.type == 'CURVES' and obj.get('source_object') is not None)
    if curves:
        limits.append('Existing source-derived physical Curves geometry, deformation and materials are retained verbatim; their native weather is not changed by mesh-surface options: ' + ', '.join(curves))
    return SimpleMaterialReport('EndfieldSimpleCharacterMaterials/1', tuple(row for _, row in builds), len(slots), tuple(limits))


def simple_material_surfaces(scene: bpy.types.Scene) -> tuple[SimpleMaterialSurface, ...]:
    """Expose explicit top-level BSDF/channel hooks for weather composition."""
    materials = {slot.material.name: slot.material for obj in scene.objects if obj.type == 'MESH'
        for slot in obj.material_slots if slot.material is not None and slot.material.get('endfield_simple_material')}
    result: list[SimpleMaterialSurface] = []
    for material in materials.values():
        for channels in material.node_tree.nodes:
            if not channels.get('endfield_simple_channels'):
                continue
            bsdf = material.node_tree.nodes.get(channels['surface_node'])
            if bsdf is None or not bsdf.get('endfield_simple_surface'):
                raise ValueError(f'Simple surface hook is absent or ambiguous: {material.name}/{channels.name}')
            source_name = channels['source_group']
            source = bpy.data.node_groups.get(source_name)
            if source is None:
                raise ValueError(f'Retained source group is unavailable for weather composition: {source_name}')
            full_material = bpy.data.materials.get(material['source_full_material'])
            source_instance = None if full_material is None else full_material.node_tree.nodes.get(channels['source_instance'])
            if source_instance is None or source_instance.node_tree != source:
                raise ValueError(f'Retained source instance is unavailable for weather composition: {material.name}/{channels["source_instance"]}')
            result.append(SimpleMaterialSurface(material, bsdf, channels, cast(SemanticFamily, channels['semantic_family']), source, source_instance))
    return tuple(result)


def clone_source_socket(destination: bpy.types.ShaderNodeTree, source: bpy.types.NodeSocket,
    caller_instances: tuple[bpy.types.Node, ...]) -> bpy.types.NodeSocket:
    """Inline a selected source weather channel with original samplers and drivers.

    The caller supplies every owning group instance from outermost to innermost.
    This deliberately does not copy shader closures, color ramps, curve widgets,
    or keyed material actions. Unsupported source nodes fail explicitly.
    """
    return _slice_socket(destination, source, caller_instances, {}, {})


def clone_source_sockets(destination: bpy.types.ShaderNodeTree, sources: tuple[bpy.types.NodeSocket, ...],
    caller_instances: tuple[bpy.types.Node, ...], retained_groups: Mapping[int, bpy.types.ShaderNodeTree]) -> tuple[bpy.types.NodeSocket, ...]:
    """Share upstream dependencies and explicitly retain selected replacement groups."""
    cache: dict[tuple[int, tuple[int, ...]], bpy.types.Node] = {}
    constants: dict[tuple[int, tuple[int, ...]], bpy.types.NodeSocket] = {}
    return tuple(_slice_socket_retained(destination, source, caller_instances, cache, constants, retained_groups) for source in sources)


def bind_simple_face_light(scene: bpy.types.Scene, light: bpy.types.Object) -> int:
    """Bind face SDF shadow and eye/card diffuse fill to the designated Sun."""
    if light.type != 'LIGHT' or light.data.type != 'SUN' or light.name not in scene.objects:
        raise ValueError(f'Compact face shading requires a designated Sun in the scene: {light.name}')
    if not math.isfinite(light.data.energy) or light.data.energy <= 0.:
        raise ValueError(f'Bind a positive designated Sun energy as the flat-diffuse reference before dimming it: {light.name}, energy={light.data.energy}')
    materials = {surface.material.name: surface.material for surface in simple_material_surfaces(scene)}
    count = 0
    for material in materials.values():
        for item in material.node_tree.nodes:
            if item.get('endfield_simple_sun_color'):
                for axis in range(3):
                    item.outputs[0].driver_remove('default_value', axis)
                    driver = item.outputs[0].driver_add('default_value', axis).driver
                    driver.type = 'AVERAGE'
                    variable = driver.variables.new()
                    variable.name = 'designated_sun_color'
                    variable.type = 'SINGLE_PROP'
                    variable.targets[0].id_type = 'LIGHT'
                    variable.targets[0].id = light.data
                    variable.targets[0].data_path = f'color[{axis}]'
            if item.get('endfield_simple_flat_surface'):
                item.inputs['Strength'].driver_remove('default_value')
                driver = item.inputs['Strength'].driver_add('default_value').driver
                driver.type = 'SCRIPTED'
                variable = driver.variables.new()
                variable.name = 'designated_sun_energy'
                variable.type = 'SINGLE_PROP'
                variable.targets[0].id_type = 'LIGHT'
                variable.targets[0].id = light.data
                variable.targets[0].data_path = 'energy'
                driver.expression = 'designated_sun_energy / ' + repr(float(light.data.energy))
                item['reference_sun_energy'] = float(light.data.energy)
            if not item.get('endfield_simple_face_light'):
                continue
            for axis, socket in enumerate(item.inputs):
                socket.driver_remove('default_value')
                driver = socket.driver_add('default_value').driver
                driver.type = 'AVERAGE'
                variable = driver.variables.new()
                variable.name = 'designated_sun_direction'
                variable.type = 'SINGLE_PROP'
                variable.targets[0].id = light
                variable.targets[0].data_path = f'matrix_world[2][{axis}]'
            item['designated_sun'] = light.name
            count += 1
    return count


def simple_surface_color_inputs(surface: SimpleMaterialSurface) -> tuple[bpy.types.NodeSocket, ...]:
    """Return all live color destinations, including eye and card diffuse fill."""
    unlit_name = surface.channels.get('unlit_node')
    if unlit_name is None:
        return (surface.bsdf.inputs['Base Color'],)
    unlit = surface.material.node_tree.nodes.get(unlit_name)
    if unlit is None or unlit.type != 'EMISSION':
        raise ValueError(f'Compact flat surface closure is missing: {surface.material.name}/{unlit_name}')
    multipliers = tuple(item for item in surface.material.node_tree.nodes if item.get('endfield_simple_flat_color')
        and item.outputs[0].is_linked and any(link.to_node == unlit for link in item.outputs[0].links))
    if len(multipliers) != 1:
        raise ValueError(f'Compact flat Sun color multiplier is missing: {surface.material.name}/{unlit_name}')
    return (surface.bsdf.inputs['Base Color'], multipliers[0].inputs[1])
