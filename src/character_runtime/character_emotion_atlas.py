"""Source Skin emotion-atlas blending before shadow, lighting, and weather.

This changes face color only. Bone expression, phonemes, and gameplay animation
are independent systems. Controls are instance inputs suitable for keyframes.
"""
from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
import hashlib
import json
import math

import bpy

from character_node_graph import color_mix, math_node, node
from patch_character_hgrp import by_label, source_keywords


SECTION = 'Source Face Emotion Atlas'
LABEL = 'Compiled face emotion atlas'


@dataclass(frozen=True)
class SourceTextureDefault:
    slot: str
    default: str
    evidence: str
    scope: str


@dataclass(frozen=True)
class EmotionTextureSampler:
    image: bpy.types.Image


@dataclass(frozen=True)
class EmotionDefaultSampler:
    source_default: SourceTextureDefault


EmotionSampler = EmotionTextureSampler | EmotionDefaultSampler


def _verified_source_default(tree: bpy.types.NodeTree, slot: str) -> SourceTextureDefault | None:
    """Read the exact null-pointer default recorded from its referenced Shader TypeTree."""
    raw = tree.get('source_null_texture_defaults')
    if raw is None:
        return None
    if not isinstance(raw, str):
        raise TypeError(f'Source null texture defaults must be JSON text: {tree.name}')
    values = json.loads(raw)
    if not isinstance(values, dict):
        raise TypeError(f'Source null texture defaults must be an object: {tree.name}')
    value = values.get(slot)
    if value is None:
        return None
    if not isinstance(value, dict):
        raise TypeError(f'Source texture default record must be an object: {tree.name}/{slot}')
    record = SourceTextureDefault(
        slot=value.get('slot'),
        default=value.get('default'),
        evidence=value.get('evidence'),
        scope=value.get('scope'),
    )
    if not all(isinstance(item, str) and item for item in (record.slot, record.default, record.evidence, record.scope)):
        raise ValueError(f'Source texture default record is incomplete: {tree.name}/{slot}')
    return record


def build_emotion_atlas(name: str, sampler_source: EmotionSampler) -> bpy.types.ShaderNodeTree:
    """Build the source 2x2 sampler or exact default-color path and color blend."""
    if not name.strip():
        raise ValueError('Emotion atlas requires a nonempty node-group name')
    image = sampler_source.image if isinstance(sampler_source, EmotionTextureSampler) else None
    if image is not None:
        if image.packed_file is None or len(image.pixels) == 0:
            raise ValueError('Emotion atlas requires packed source pixels')
        if image.colorspace_settings.name != 'sRGB' or image.alpha_mode != 'CHANNEL_PACKED':
            raise ValueError('Verified emotion texture requires sRGB color and independent packed alpha')
    else:
        source_default = sampler_source.source_default
        if (source_default.slot != '_EmotionMap' or source_default.default != 'black'
                or source_default.evidence != 'Exact material-referenced Shader property TypeTree'
                or source_default.scope != 'Unity null texture pointer uses the verified shader default; no source image was extracted'):
            raise ValueError(f'Emotion default is not an exact verified black Shader property: {source_default}')
    tree = bpy.data.node_groups.new(name, 'ShaderNodeTree')
    for label, kind in (('BaseMap UV', 'NodeSocketVector'), ('Tinted Base Color', 'NodeSocketColor'),
                        ('Emotion Index', 'NodeSocketFloat'), ('Emotion Blend', 'NodeSocketFloat')):
        tree.interface.new_socket(name=label, in_out='INPUT', socket_type=kind)
    for label, kind in (('Color', 'NodeSocketColor'), ('Atlas UV', 'NodeSocketVector'),
                        ('Blend Weight', 'NodeSocketFloat')):
        tree.interface.new_socket(name=label, in_out='OUTPUT', socket_type=kind)
    entry = node(tree, 'NodeGroupInput', 'Source emotion inputs', SECTION)
    output = node(tree, 'NodeGroupOutput', 'Source emotion result', SECTION)
    uv = node(tree, 'ShaderNodeSeparateXYZ', 'Interpolated BaseMap UV', SECTION)
    tree.links.new(entry.outputs['BaseMap UV'], uv.inputs[0])
    half_index = math_node(tree, 'MULTIPLY', entry.outputs['Emotion Index'], .5, 'Half emotion index', SECTION)
    column = math_node(tree, 'MULTIPLY', .5, math_node(tree, 'MODULO', entry.outputs['Emotion Index'], 2., 'Source index remainder two', SECTION), 'Source atlas column', SECTION)
    row = math_node(tree, 'MULTIPLY', .5, math_node(tree, 'FLOOR', half_index, 0., 'Source atlas row integer', SECTION), 'Source atlas row', SECTION)
    coordinates = node(tree, 'ShaderNodeCombineXYZ', 'Source two by two atlas UV', SECTION)
    for axis, offset in (('X', column), ('Y', row)):
        half_uv = math_node(tree, 'MULTIPLY', .5, uv.outputs[axis], 'Half source UV ' + axis, SECTION)
        tree.links.new(math_node(tree, 'ADD', half_uv, offset, 'Offset atlas UV ' + axis, SECTION), coordinates.inputs[axis])
    if image is not None:
        sample = node(tree, 'ShaderNodeTexImage', 'Exact source EmotionMap', SECTION)
        sample.image = image
        sample.interpolation = 'Linear'
        sample.extension = 'REPEAT'
        tree.links.new(coordinates.outputs[0], sample.inputs['Vector'])
        sample_color = sample.outputs['Color']
        sample_alpha = sample.outputs['Alpha']
        tree['source_sampler_kind'] = 'source_texture'
        tree['sampling_boundary'] = 'Source linear repeat sampling; Blender derivatives select mip level, native global mip bias is not captured.'
    else:
        source_default = sampler_source.source_default
        sample = node(tree, 'ShaderNodeRGB', 'Exact Shader default _EmotionMap: black', SECTION)
        sample.outputs[0].default_value = (0., 0., 0., 1.)
        alpha = node(tree, 'ShaderNodeValue', 'Exact Shader default _EmotionMap alpha: one', SECTION)
        alpha.outputs[0].default_value = 1.
        sample_color = sample.outputs[0]
        sample_alpha = alpha.outputs[0]
        tree['source_sampler_kind'] = 'verified_shader_default'
        tree['source_shader_texture_default'] = json.dumps({
            'slot': source_default.slot,
            'default': source_default.default,
            'evidence': source_default.evidence,
            'scope': source_default.scope,
            'rgba': [0., 0., 0., 1.],
        }, sort_keys=True)
        tree['sampling_boundary'] = 'No EmotionMap image exists at the source null pointer; exact referenced Shader black default supplies RGBA (0, 0, 0, 1).'
    weight = math_node(tree, 'MULTIPLY', sample_alpha, entry.outputs['Emotion Blend'], 'Source emotion alpha times blend', SECTION)
    delta = color_mix(tree, 'SUBTRACT', 1., sample_color, entry.outputs['Tinted Base Color'], 'Emotion minus tinted base color', SECTION)
    weighted = color_mix(tree, 'MULTIPLY', 1., delta, weight, 'Source weighted emotion difference', SECTION)
    result = color_mix(tree, 'ADD', 1., entry.outputs['Tinted Base Color'], weighted, 'Source emotion color interpolation', SECTION)
    for label, socket in (('Color', result), ('Atlas UV', coordinates.outputs[0]), ('Blend Weight', weight)):
        tree.links.new(socket, output.inputs[label])
    tree['source_scope'] = 'Exact Skin emotion atlas layout and RGB interpolation; compiled fragment 84794-84798, programBlob138/subProgram185.'
    return tree


def _source_number(values: dict[str, object], key: str) -> float:
    value = values[key]
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
        raise ValueError(f'Emotion source property must be finite numeric: {key}={value}')
    return float(value)


def attach_emotion_atlas(
    tree: bpy.types.ShaderNodeTree, layout: Callable[[bpy.types.NodeTree], None],
) -> bpy.types.Node:
    """Insert before all consumers of the original BaseMap times BaseColor."""
    if tree.get('source_shader') != 'HGRP/CharacterNPR_Skin' or '_EMOTION_MAP' not in source_keywords(tree):
        raise ValueError('Emotion attachment requires the verified Skin emotion keyword')
    if any(item.label == LABEL for item in tree.nodes):
        raise ValueError(f'Emotion atlas is already attached: {tree.name}')
    values = json.loads(tree['source_floats'])
    if _source_number(values, '_UseEmotionMap') != 1.:
        raise ValueError('Emotion source switch and compiled keyword disagree')
    index = _source_number(values, '_EmotionIndex')
    blend = _source_number(values, '_EmotionBlend')
    if not 0. <= index <= 3. or not 0. <= blend <= 1.:
        raise ValueError(f'Emotion source controls outside verified atlas range: index={index}, blend={blend}')
    slots: dict[str, bpy.types.Node] = {}
    for slot in ('_BaseMap', '_EmotionMap'):
        found = tuple(n for n in tree.nodes if n.type == 'TEX_IMAGE' and n.get('source_slot') == slot)
        if slot == '_BaseMap':
            if len(found) != 1 or found[0].image is None:
                raise ValueError(f'Emotion source BaseMap missing or ambiguous: {tree.name}/{slot}')
            slots[slot] = found[0]
            continue
        if len(found) > 1:
            raise ValueError(f'Emotion source texture is ambiguous: {tree.name}/{slot}')
        if len(found) == 1:
            if found[0].image is None:
                raise ValueError(f'Non-null EmotionMap source texture has no image: {tree.name}/{slot}')
            slots[slot] = found[0]
            continue
        source_default = _verified_source_default(tree, slot)
        if source_default is None:
            raise ValueError(f'EmotionMap is absent without an exact null Shader default: {tree.name}/{slot}')
        if source_default.default != 'black':
            raise ValueError(f'EmotionMap null Shader default is unsupported: {tree.name}/{slot}={source_default.default!r}')
    base = by_label(tree, 'Source base sample times BaseColor')
    base_links = tuple(base.outputs['Color'].links)
    uv_links = tuple(slots['_BaseMap'].inputs['Vector'].links)
    if not base_links or len(uv_links) != 1:
        raise ValueError('Emotion attachment requires an active source color and explicit BaseMap UV')
    if any(item.name in ('Emotion Index', 'Emotion Blend') for item in tree.interface.items_tree):
        raise ValueError('Emotion controls already exist without a verified attachment')
    inputs = tuple(n for n in tree.nodes if n.type == 'GROUP_INPUT')
    if len(inputs) != 1:
        raise ValueError('Emotion attachment requires one source group-input node')
    for label, value, maximum in (('Emotion Index', index, 3.), ('Emotion Blend', blend, 1.)):
        socket = tree.interface.new_socket(name=label, in_out='INPUT', socket_type='NodeSocketFloat')
        socket.default_value = value
        socket.min_value = 0.
        socket.max_value = maximum
        socket.description = 'Source material parameter; editable and animatable per material instance.'
        for owner in (*bpy.data.node_groups, *(material.node_tree for material in bpy.data.materials if material.use_nodes)):
            for item in owner.nodes:
                if item.type == 'GROUP' and item.node_tree == tree:
                    item.inputs[label].default_value = value
    image_binding = slots.get('_EmotionMap')
    if image_binding is None:
        source_default = _verified_source_default(tree, '_EmotionMap')
        if source_default is None:
            raise ValueError(f'EmotionMap null Shader default disappeared: {tree.name}')
        sampler_source: EmotionSampler = EmotionDefaultSampler(source_default)
    else:
        sampler_source = EmotionTextureSampler(image_binding.image)
    kernel = build_emotion_atlas('EF Source Skin Emotion Atlas', sampler_source)
    instance = node(tree, 'ShaderNodeGroup', LABEL, SECTION)
    instance.node_tree = kernel
    tree.links.new(uv_links[0].from_socket, instance.inputs['BaseMap UV'])
    tree.links.new(base.outputs['Color'], instance.inputs['Tinted Base Color'])
    for label in ('Emotion Index', 'Emotion Blend'):
        tree.links.new(inputs[0].outputs[label], instance.inputs[label])
    for link in base_links:
        target = link.to_socket
        tree.links.remove(link)
        tree.links.new(instance.outputs['Color'], target)
    tree['emotion_atlas_scope'] = 'Source 2x2 color overlay before shadow-LUT and lighting; base/surface opacity unchanged; no facial bone or phoneme claims.'
    binding: dict[str, object] = {
        'group': tree.name, 'source_identity': tree['source_identity'],
        'kernel': kernel.name, 'instance': instance.name,
        'source_kind': 'source_texture' if image_binding is not None else 'verified_shader_default',
        'downstream': [[link.to_node.name, link.to_socket.identifier] for link in instance.outputs['Color'].links],
    }
    if image_binding is not None:
        if image_binding.image.packed_file is None:
            raise ValueError(f'EmotionMap source pixels are not packed: {tree.name}')
        binding['image'] = image_binding.image.name
        binding['image_sha256'] = hashlib.sha256(image_binding.image.packed_file.data).hexdigest()
    else:
        binding['source_default'] = {
            'slot': source_default.slot,
            'default': source_default.default,
            'evidence': source_default.evidence,
            'scope': source_default.scope,
            'rgba': [0., 0., 0., 1.],
        }
    tree['emotion_atlas_binding'] = json.dumps(binding, sort_keys=True)
    layout(kernel)
    layout(tree)
    return instance


def attach_visible_emotion_atlases(layout: Callable[[bpy.types.NodeTree], None]) -> dict[str, object]:
    """Attach source-enabled visible Skin consumers, preserving material defaults."""
    from review_character_hgrp import visible_source_group_names

    visible = visible_source_group_names()
    rows = []
    for tree in bpy.data.node_groups:
        if tree.name not in visible or tree.get('source_shader') != 'HGRP/CharacterNPR_Skin':
            continue
        if '_EMOTION_MAP' not in source_keywords(tree):
            continue
        if 'emotion_atlas_binding' not in tree:
            attach_emotion_atlas(tree, layout)
        rows.append(json.loads(tree['emotion_atlas_binding']))
    report = {'status': 'attached' if rows else 'not_applicable', 'bindings': rows,
              'scope': 'Source-enabled face material color controls; not skeletal endpoints or native expression scheduling.'}
    verify_emotion_atlases(report)
    return report


def verify_emotion_atlases(report: dict[str, object]) -> dict[str, object]:
    """Follow the live atlas sampler, property sockets and downstream color links."""
    rows = report['bindings']
    if not isinstance(rows, list):
        raise TypeError('Emotion atlas bindings must be an array')
    for row in rows:
        tree = bpy.data.node_groups.get(row['group'])
        if tree is None or tree.get('source_identity') != row['source_identity']:
            raise AssertionError('Emotion source material identity changed')
        instance = tree.nodes.get(row['instance'])
        if instance is None or instance.node_tree is None or instance.node_tree.name != row['kernel']:
            raise AssertionError('Emotion atlas instance is missing or changed')
        source_kind = row.get('source_kind', 'source_texture')
        if source_kind == 'source_texture':
            sampler = by_label(instance.node_tree, 'Exact source EmotionMap')
            image = None if sampler is None else sampler.image
            if (image is None or image.name != row['image'] or image.packed_file is None
                    or image.colorspace_settings.name != 'sRGB' or image.alpha_mode != 'CHANNEL_PACKED'
                    or hashlib.sha256(image.packed_file.data).hexdigest() != row['image_sha256']):
                raise AssertionError('Emotion atlas packed texture or interpretation changed')
        elif source_kind == 'verified_shader_default':
            raw_default = row.get('source_default')
            if not isinstance(raw_default, dict):
                raise AssertionError('Emotion Shader default evidence is missing')
            expected_default = _verified_source_default(tree, '_EmotionMap')
            if (expected_default is None or expected_default.slot != '_EmotionMap'
                    or expected_default.default != 'black'
                    or expected_default.evidence != 'Exact material-referenced Shader property TypeTree'
                    or expected_default.scope != 'Unity null texture pointer uses the verified shader default; no source image was extracted'):
                raise AssertionError('Emotion null pointer no longer has a verified black Shader default')
            expected_rgba = [0., 0., 0., 1.]
            if (raw_default.get('slot') != expected_default.slot
                    or raw_default.get('default') != expected_default.default
                    or raw_default.get('evidence') != expected_default.evidence
                    or raw_default.get('scope') != expected_default.scope
                    or raw_default.get('rgba') != expected_rgba):
                raise AssertionError('Emotion Shader default receipt differs from its source evidence')
            kernel_default = instance.node_tree.get('source_shader_texture_default')
            if not isinstance(kernel_default, str) or json.loads(kernel_default) != raw_default:
                raise AssertionError('Emotion Shader default kernel evidence differs from its binding receipt')
            if any(item.label == 'Exact source EmotionMap' for item in instance.node_tree.nodes):
                raise AssertionError('Emotion Shader default branch unexpectedly contains a source image sampler')
            color_node = by_label(instance.node_tree, 'Exact Shader default _EmotionMap: black')
            alpha_node = by_label(instance.node_tree, 'Exact Shader default _EmotionMap alpha: one')
            if (color_node is None or color_node.type != 'RGB' or alpha_node is None or alpha_node.type != 'VALUE'
                    or tuple(color_node.outputs[0].default_value) != tuple(expected_rgba)
                    or alpha_node.outputs[0].default_value != 1.):
                raise AssertionError('Emotion Shader default graph no longer outputs exact black RGBA')
            weight_node = by_label(instance.node_tree, 'Source emotion alpha times blend')
            delta_node = by_label(instance.node_tree, 'Emotion minus tinted base color')
            alpha_links = () if weight_node is None else tuple(weight_node.inputs[0].links)
            color_links = () if delta_node is None else tuple(delta_node.inputs[1].links)
            if (len(alpha_links) != 1 or alpha_links[0].from_node != alpha_node
                    or alpha_links[0].from_socket != alpha_node.outputs[0]
                    or len(color_links) != 1 or color_links[0].from_node != color_node
                    or color_links[0].from_socket != color_node.outputs[0]):
                raise AssertionError('Emotion Shader default is disconnected from the source blend formula')
            if instance.node_tree.get('source_sampler_kind') != 'verified_shader_default':
                raise AssertionError('Emotion Shader default sampler provenance changed')
        else:
            raise AssertionError(f'Unsupported EmotionMap source kind: {source_kind!r}')
        entry = next(node for node in tree.nodes if node.type == 'GROUP_INPUT')
        for label in ('Emotion Index', 'Emotion Blend'):
            links = tuple(instance.inputs[label].links)
            if len(links) != 1 or links[0].from_node != entry or links[0].from_socket != entry.outputs[label]:
                raise AssertionError('Emotion property input is disconnected: ' + label)
        actual = [[link.to_node.name, link.to_socket.identifier] for link in instance.outputs['Color'].links]
        if not actual or sorted(actual) != sorted(row['downstream']):
            raise AssertionError('Emotion atlas output is disconnected from its source consumers')
    return {'status': 'passed', 'bindings': len(rows)}
