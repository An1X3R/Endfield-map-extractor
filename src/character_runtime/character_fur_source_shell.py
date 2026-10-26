"""Expand retained source shell vertices from verified bind guides without changing Curves defaults."""
from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import TYPE_CHECKING, Callable, Mapping, cast

import bpy
import numpy as np

from audit_character_materials import source_geometry_digest
from character_special_vfx import attach_special_vfx, source_from_material, verify_special_vfx
from patch_character_hgrp import source_keywords

if TYPE_CHECKING:
    from rebuild_character_materials import MaterialSource, SourcePacket


def _object(value: object, label: str) -> Mapping[str, object]:
    if not isinstance(value, dict) or any(not isinstance(key, str) for key in value):
        raise TypeError(f'Source shell requires a JSON object: {label}')
    return cast(Mapping[str, object], value)


def _text(value: object, label: str) -> str:
    if not isinstance(value, str) or not value:
        raise TypeError(f'Source shell requires a nonempty string: {label}')
    return value


def _identity(value: object) -> tuple[str, int]:
    row = _object(value, 'source identity')
    path_id = row.get('path_id')
    if isinstance(path_id, bool) or not isinstance(path_id, (str, int)):
        raise TypeError('Source shell requires an explicit decimal PathID')
    return _text(row.get('source'), 'identity.source').lower(), int(path_id)


def _attribute(curves: bpy.types.Object, name: str, kind: str, field: str, width: int) -> np.ndarray:
    attribute = curves.data.attributes.get(name)
    if attribute is None or attribute.domain != 'POINT' or attribute.data_type != kind:
        raise ValueError(f'Source shell bind guide attribute is absent or incompatible: {curves.name}/{name}')
    values = np.empty(len(curves.data.points) * width, dtype=np.int32 if kind == 'INT' else np.float32)
    attribute.data.foreach_get(field, values)
    return values.reshape(-1, width)


def _channel(entry: Mapping[str, object], vertex_count: int) -> tuple[np.ndarray, Mapping[str, object]]:
    rows = entry.get('raw_channels')
    if not isinstance(rows, list):
        raise TypeError('Source shell entry requires prepared raw channel records')
    channel, = (_object(row, 'raw channel') for row in rows
                if isinstance(row, dict) and row.get('semantic') == 'TEXCOORD1')
    path = Path(_text(channel.get('path'), 'TEXCOORD1.path')).resolve(strict=True)
    raw = path.read_bytes()
    if (len(raw) != vertex_count * 8 or len(raw) != channel.get('bytes')
            or hashlib.sha256(raw).hexdigest() != channel.get('sha256')):
        raise ValueError(f'Source shell TEXCOORD1 bytes or SHA-256 differs from preparation: {path}')
    values = np.frombuffer(raw, dtype='<f4').reshape(vertex_count, 2)
    if not np.isfinite(values).all() or np.any(values < 0.0) or np.any(values > 1.0):
        raise ValueError(f'Source shell TEXCOORD1 has invalid layer values: {path}')
    return values, channel


def _positions(mesh: bpy.types.Mesh) -> np.ndarray:
    positions = np.empty(len(mesh.vertices) * 3, dtype=np.float32)
    mesh.vertices.foreach_get('co', positions)
    return positions.reshape(-1, 3)


def _dye_branch(tree: bpy.types.ShaderNodeTree, source: MaterialSource,
                samples: Mapping[str, bpy.types.Node]) -> Mapping[str, object]:
    """Insert missing archive dye at the original base-color fanout without replacing lighting."""
    from rebuild_character_materials import fur_dye_color
    if '_CHARACTER_FUR_DYE' not in source.keywords:
        return {'status': 'not_selected'}
    base, = (item for item in tree.nodes if item.type == 'MIX_RGB' and item.blend_type == 'MULTIPLY'
             and item.label == 'Source base sample times BaseColor')
    if (len(base.inputs[1].links) != 1 or base.inputs[1].links[0].from_socket != samples['_BaseMap'].outputs['Color']
            or len(base.inputs[2].links) != 1 or base.inputs[2].links[0].from_node.type != 'GROUP_INPUT'
            or base.inputs[2].links[0].from_socket.name != 'Base Tint'):
        raise ValueError(f'Source Fur dye archive base-color boundary is incompatible: {tree.name}')
    destinations = tuple(link.to_socket for link in base.outputs[0].links)
    connected = samples['_FurDyeMap'].outputs['Color'].is_linked
    dyed = fur_dye_color(tree, source, samples, base.outputs[0])
    if not connected:
        if not destinations:
            raise ValueError(f'Source Fur dye archive base color has no lighting consumers: {tree.name}')
        for destination in destinations:
            tree.links.new(dyed, destination)
    fur_dye_color(tree, source, samples, base.outputs[0])
    return json.loads(tree['source_fur_dye'])


def build_source_fur_shell(entry: Mapping[str, object], packet: SourcePacket,
                          layout: Callable[[bpy.types.NodeTree], None]) -> dict[str, object]:
    """Copy original topology/skin, apply all source-indexed bind positions, and bind actual source coverage."""
    from character_fur_curves_profile import verify_profile_fur_curves
    from rebuild_character_materials import fur_shell_coverage, fur_surface_coordinates, surface_stack, texture_colorspace
    if entry.get('format') != 'EndfieldOptionalFurCurvesSnow/1' or entry.get('status') != 'connected':
        raise ValueError('Source shell requires one completed optional Fur Curves preparation entry')
    verify_profile_fur_curves(dict(entry))
    source = bpy.data.objects[_text(entry.get('source_mesh'), 'source_mesh')]
    bind = _object(entry.get('bind_guides'), 'bind_guides')
    guides = bpy.data.objects[_text(bind.get('object_name'), 'bind_guides.object_name')]
    interpolation = _object(entry.get('interpolation'), 'interpolation')
    curves = bpy.data.objects[_text(interpolation.get('object_name'), 'interpolation.object_name')]
    if source.data.shape_keys is not None:
        raise ValueError(f'Source shell rest-position expansion does not yet support shape-key meshes: {source.name}')
    identity = _identity(entry.get('source_group_identity'))
    material_source, = (item for item in packet.materials if item.identity == identity)
    group = bpy.data.node_groups[_text(entry.get('source_group'), 'source_group')]
    if (material_source.shader != 'HGRP/CharacterNPR' or '_CHARACTER_FUR' not in material_source.keywords
            or set(source_keywords(group)) != set(material_source.keywords)
            or tuple(json.loads(group['source_identity'])) != identity):
        raise ValueError(f'Source shell group identity, shader or keywords differs from its source packet: {identity}')
    indices = _attribute(guides, 'source_vertex_index', 'INT', 'value', 1).reshape(-1)
    points = _attribute(guides, 'position', 'FLOAT_VECTOR', 'vector', 3)
    layers = _attribute(guides, 'source_shell_layer', 'FLOAT', 'value', 1).reshape(-1)
    count = len(source.data.vertices)
    if len(indices) != count or not np.array_equal(np.sort(indices), np.arange(count)):
        raise ValueError(f'Source shell requires each original vertex exactly once; use an explicitly prepared root_stride=1: {source.name}')
    if not np.isfinite(points).all():
        raise ValueError(f'Source shell bind positions contain non-finite coordinates: {guides.name}')
    channel, channel_record = _channel(entry, count)
    if np.max(np.abs(layers - channel[indices, 0])) > 1e-6:
        raise ValueError(f'Source shell guide layers do not match original TEXCOORD1: {guides.name}')
    slot_material = bpy.data.materials[_text(entry.get('source_material'), 'source_material')]
    slots = tuple(index for index, slot in enumerate(source.material_slots) if slot.material == slot_material)
    if not slots or not any(polygon.material_index in slots for polygon in source.data.polygons):
        raise ValueError(f'Source shell material does not occupy an actual object material slot: {source.name}/{slot_material.name}')
    before = source_geometry_digest((source.name,))
    candidate = source.copy()
    if 'source_renderer_identity' in candidate:
        del candidate['source_renderer_identity']
    candidate.data = source.data.copy()
    candidate.name = source.name + ' Expanded Source Fur Shell'
    bpy.data.collections[_text(entry.get('collection'), 'collection')].objects.link(candidate)
    transform = np.asarray(candidate.matrix_world.inverted() @ guides.matrix_world, dtype=np.float64)
    expanded = np.empty((count, 3), dtype=np.float32)
    expanded[indices] = points @ transform[:3, :3].T + transform[:3, 3]
    original = _positions(source.data)
    candidate.data.vertices.foreach_set('co', expanded.reshape(-1))
    candidate.data.update()
    vertex_indices = np.asarray([loop.vertex_index for loop in candidate.data.loops], dtype=np.int32)
    expected_uv = channel[vertex_indices]
    uv = candidate.data.uv_layers.get('UV1')
    if uv is not None:
        actual = np.empty(len(candidate.data.loops) * 2, dtype=np.float32)
        uv.data.foreach_get('uv', actual)
        if not np.array_equal(actual.reshape(-1, 2), expected_uv):
            raise ValueError(f'Existing source shell UV1 differs from prepared raw TEXCOORD1: {source.name}')
    else:
        uv = candidate.data.uv_layers.new(name='UV1')
        uv.data.foreach_set('uv', expected_uv.reshape(-1))
    preview = group.copy()
    preview.name = group.name + ' Expanded Source Shell'
    samples = {str(item['source_slot']): item for item in preview.nodes
               if item.type == 'TEX_IMAGE' and item.get('source_slot')}
    for binding in material_source.textures:
        sample = samples.get(binding.name)
        texture = packet.textures[binding.identity]
        if (sample is None or sample.image is None or sample.get('source_sha256') != texture.sha256
                or tuple(json.loads(sample['source_identity'])) != binding.identity
                or sample.image.packed_file is None
                or hashlib.sha256(sample.image.packed_file.data).hexdigest() != texture.sha256):
            raise ValueError(f'Source shell group texture binding differs from source packet: {identity}/{binding.name}')
        if binding.name in ('_BaseMap', '_FurMap', '_FurDirMap'):
            vector, = sample.inputs['Vector'].links
            mapping = vector.from_node
            if (mapping.type != 'MAPPING' or mapping.vector_type != 'POINT'
                    or any(mapping.inputs[name].is_linked for name in ('Location', 'Scale', 'Rotation'))
                    or not np.array_equal(np.asarray(mapping.inputs['Scale'].default_value[:2], dtype=np.float32), np.asarray(binding.scale, dtype=np.float32))
                    or not np.array_equal(np.asarray(mapping.inputs['Location'].default_value[:2], dtype=np.float32), np.asarray(binding.offset, dtype=np.float32))
                    or tuple(mapping.inputs['Rotation'].default_value) != (0.0, 0.0, 0.0)):
                raise ValueError(f'Source shell coverage requires unchanged actual source ST: {identity}/{binding.name}; '
                                 f'host_scale={tuple(mapping.inputs["Scale"].default_value[:2])}; source_scale={binding.scale}; '
                                 f'host_offset={tuple(mapping.inputs["Location"].default_value[:2])}; source_offset={binding.offset}')
            uv_link, = mapping.inputs['Vector'].links
            if uv_link.from_node.type != 'UVMAP' or uv_link.from_node.uv_map != f'UV{binding.uv_index}':
                raise ValueError(f'Source shell coverage texture uses the wrong source UV layer: {identity}/{binding.name}')
        sample['source_color_space'] = texture.color_space
        if binding.name == '_FurDyeMap' and sample.image.colorspace_settings.name != texture_colorspace(binding, texture):
            sample.image = sample.image.copy()
            sample.image.name += ' Verified Fur dye color role'
            sample.image.colorspace_settings.name = texture_colorspace(binding, texture)
    dye = _dye_branch(preview, material_source, samples)
    fur_surface_coordinates(preview, material_source, samples)
    output, = (item for item in preview.nodes if item.type == 'GROUP_OUTPUT' and item.is_active_output)
    coverage = fur_shell_coverage(preview, material_source, samples)
    preview.links.new(coverage, output.inputs['Coverage'])
    from character_fur_transmission import attach_fur_transmission
    transmission = attach_fur_transmission(preview, material_source)
    if 'source_special_vfx' not in preview:
        special = source_from_material(material_source, samples)
        if special is not None:
            if special.floats['_AlphaPremultiply'] != 0.0:
                raise ValueError('Source shell archive attachment requires separately established premultiplied BaseMap alpha')
            lit, = output.inputs['Shader'].links
            uv_node = preview.nodes.new('ShaderNodeUVMap')
            uv_node.uv_map = 'UV0'
            geometry = preview.nodes.new('ShaderNodeNewGeometry')
            multiplier = preview.nodes.new('ShaderNodeValue')
            multiplier.outputs[0].default_value = 1.0
            shader = attach_special_vfx(preview, special, lit.from_socket, uv_node.outputs[0],
                geometry.outputs['Normal'], geometry.outputs['Incoming'], multiplier.outputs[0], bpy.context.scene)
            preview.links.new(shader, output.inputs['Shader'])
    material = surface_stack((material_source,), {identity: preview}, layout)
    material.use_transparency_overlap = True
    for slot in slots:
        candidate.material_slots[slot].link = 'OBJECT'
        candidate.material_slots[slot].material = material
    layout(preview)
    if source_geometry_digest((source.name,)) != before:
        raise AssertionError(f'Source shell helper modified original geometry or skin: {source.name}')
    result: dict[str, object] = {
        'format': 'EndfieldExpandedSourceFurShell/1', 'status': 'built',
        'source_mesh': source.name, 'shell_mesh': candidate.name, 'curves': curves.name,
        'source_mesh_identity': dict(_object(entry.get('source_mesh_identity'), 'source_mesh_identity')),
        'bind_guides': guides.name, 'source_group_identity': dict(_object(entry['source_group_identity'], 'source identity')),
        'source_group': preview.name, 'material': material.name, 'material_slots': list(slots),
        'source_vertex_count': count, 'layer_count': len(np.unique(channel[:, 0])),
        'raw_texcoord1': dict(channel_record), 'source_geometry_preserved': True,
        'shell_positions_sha256': hashlib.sha256(expanded.tobytes()).hexdigest(),
        'uv1_sha256': hashlib.sha256(expected_uv.tobytes()).hexdigest(),
        'max_source_displacement_meters': float(np.linalg.norm(expanded - original, axis=1).max()),
        'coverage': json.loads(preview['source_fur_shell_coverage']),
        'normal_uv': json.loads(preview['source_fur_normal_uv']) if 'source_fur_normal_uv' in preview else {'status': 'not_selected'},
        'coverage_node': coverage.node.name,
        'dye': dict(dye),
        'transmission': dict(transmission),
        'special_vfx': json.loads(preview['source_special_vfx']) if 'source_special_vfx' in preview else {'status': 'not_selected'},
        'native_shader_semantics_verified': False,
        'scope': 'Original source shell topology and skin with source-indexed guide expansion in host world meters. Native clip-space expansion, shading varyings, filtering and depth passes are not reproduced.',
    }
    candidate['source_fur_shell'] = json.dumps(result, sort_keys=True)
    return result


def activate_source_fur_shell(receipt: Mapping[str, object]) -> dict[str, object]:
    """Select the explicit shell representation while retaining original geometry and Curves."""
    if receipt.get('format') != 'EndfieldExpandedSourceFurShell/1' or receipt.get('status') != 'built':
        raise ValueError('Source shell activation requires a newly built shell receipt')
    layers = receipt.get('layer_count')
    if type(layers) is not int or layers < 1:
        raise ValueError(f'Source shell activation requires a positive layer count: {layers!r}')
    candidate = bpy.data.objects[_text(receipt.get('shell_mesh'), 'shell_mesh')]
    # A camera ray can cross both sides of every layer before reaching the substrate.
    transparent_bounces = 2 * layers + 8
    for scene in candidate.users_scene:
        scene.cycles.transparent_max_bounces = max(scene.cycles.transparent_max_bounces, transparent_bounces)
    for key in ('source_mesh', 'curves'):
        obj = bpy.data.objects[_text(receipt.get(key), key)]
        obj.hide_render = True
        obj.hide_set(True)
    candidate.hide_render = False
    candidate.hide_set(False)
    result = {**receipt, 'status': 'active', 'cycles_transparent_bounces_required': transparent_bounces,
              'retained_source_digest': source_geometry_digest((_text(receipt.get('source_mesh'), 'source_mesh'),)),
              'shell_geometry_digest': source_geometry_digest((candidate.name,))}
    candidate['source_fur_shell'] = json.dumps(result, sort_keys=True)
    return result


def verify_source_fur_shell(receipt: Mapping[str, object]) -> dict[str, object]:
    """Validate saved source-indexed positions, UV1, actual material overrides and additive special VFX."""
    if receipt.get('format') != 'EndfieldExpandedSourceFurShell/1' or receipt.get('status') != 'active':
        raise ValueError('Saved source shell receipt is not active')
    candidate = bpy.data.objects[_text(receipt.get('shell_mesh'), 'shell_mesh')]
    source = bpy.data.objects[_text(receipt.get('source_mesh'), 'source_mesh')]
    curves = bpy.data.objects[_text(receipt.get('curves'), 'curves')]
    if (candidate.type != 'MESH' or candidate.hide_render or candidate.hide_get()
            or not source.hide_render or not curves.hide_render or candidate.parent != source.parent
            or json.loads(candidate['source_fur_shell']) != dict(receipt)
            or source_geometry_digest((source.name,)) != receipt.get('retained_source_digest')
            or source_geometry_digest((candidate.name,)) != receipt.get('shell_geometry_digest')):
        raise AssertionError(f'Saved source shell visibility, parent or retained source changed: {candidate.name}')
    if hashlib.sha256(_positions(candidate.data).tobytes()).hexdigest() != receipt.get('shell_positions_sha256'):
        raise AssertionError(f'Saved source shell positions changed: {candidate.name}')
    values = np.empty(len(candidate.data.loops) * 2, dtype=np.float32)
    candidate.data.uv_layers['UV1'].data.foreach_get('uv', values)
    if hashlib.sha256(values.tobytes()).hexdigest() != receipt.get('uv1_sha256'):
        raise AssertionError(f'Saved source shell UV1 changed: {candidate.name}')
    for index in cast(list[int], receipt['material_slots']):
        slot = candidate.material_slots[index]
        if slot.link != 'OBJECT' or slot.material.name != receipt.get('material'):
            raise AssertionError(f'Saved source shell object material override changed: {candidate.name}/{index}')
    group = bpy.data.node_groups[_text(receipt.get('source_group'), 'source_group')]
    output, = (item for item in group.nodes if item.type == 'GROUP_OUTPUT' and item.is_active_output)
    coverage, = output.inputs['Coverage'].links
    if coverage.from_node.name != receipt.get('coverage_node'):
        raise AssertionError(f'Saved source shell coverage is disconnected from its material output: {group.name}')
    material = bpy.data.materials[_text(receipt.get('material'), 'material')]
    instance, = (item for item in material.node_tree.nodes if item.type == 'GROUP' and item.node_tree == group)
    from character_material_authoring import relocated_source_output, socket_dependencies
    output, = (item for item in material.node_tree.nodes
               if item.type == 'OUTPUT_MATERIAL' and item.is_active_output)
    surface_link, = output.inputs['Surface'].links
    active_outputs = socket_dependencies((surface_link.from_socket,)).output_sockets
    for output_name in ('Shader', 'Coverage'):
        source_output = instance.outputs[output_name]
        candidates = [source_output] if source_output.is_linked else []
        if instance.get('endfield_authoring_parameters'):
            candidates.append(relocated_source_output(instance, source_output))
        if not any(candidate.as_pointer() in active_outputs for candidate in candidates):
            raise AssertionError(
                f'Saved source shell {output_name} does not reach the active material surface: {material.name}'
            )
    if json.loads(group['source_fur_shell_coverage']) != receipt.get('coverage'):
        raise AssertionError(f'Saved source shell coverage source metadata changed: {group.name}')
    normal_uv = receipt.get('normal_uv')
    if isinstance(normal_uv, dict) and normal_uv.get('space') == 'base_map_st':
        normal = group.nodes[str(normal_uv['sample_node'])]
        if (json.loads(group['source_fur_normal_uv']) != normal_uv
                or len(normal.inputs['Vector'].links) != 1
                or normal.inputs['Vector'].links[0].from_node.name != normal_uv['coordinate_node']):
            raise AssertionError(f'Saved Fur normal surface coordinates changed: {group.name}')
    if 'transmission' in receipt:
        from character_fur_transmission import verify_fur_transmission
        verify_fur_transmission(group, _object(receipt['transmission'], 'transmission'))
    dye = _object(receipt.get('dye'), 'dye')
    if dye.get('status') != 'not_selected':
        sample, = (item for item in group.nodes if item.type == 'TEX_IMAGE' and item.get('source_slot') == '_FurDyeMap')
        mixed = group.nodes[_text(dye.get('node'), 'dye.node')]
        if (json.loads(group['source_fur_dye']) != dict(dye) or mixed.type != 'MIX_RGB' or mixed.blend_type != 'SCREEN'
                or len(sample.outputs['Color'].links) != 1 or sample.outputs['Color'].links[0].to_socket != mixed.inputs[2]
                or mixed.inputs[0].is_linked or abs(mixed.inputs[0].default_value - float(dye['intensity'])) > 1e-7
                or len(mixed.inputs[1].links) != 1
                or mixed.inputs[1].links[0].from_node.label != 'Source base sample times BaseColor'
                or not mixed.outputs[0].is_linked or sample.image.colorspace_settings.name != dye.get('host_color_space')):
            raise AssertionError(f'Saved source shell dye image is not connected through its actual ScreenBlend: {group.name}')
        from character_material_authoring import socket_dependencies
        material_output, = (item for item in material.node_tree.nodes
                            if item.type == 'OUTPUT_MATERIAL' and item.is_active_output)
        surface_link, = material_output.inputs['Surface'].links
        active_outputs = socket_dependencies((surface_link.from_socket,)).output_sockets
        reaches_shader = mixed.outputs[0].as_pointer() in active_outputs
        if not reaches_shader and instance.get('endfield_authoring_parameters'):
            reaches_shader = any(
                node.type == 'TEX_IMAGE'
                and node.get('endfield_authoring_texture')
                and node.get('endfield_authoring_source_group') == group.name
                and node.get('source_slot') == '_FurDyeMap'
                and node.image == sample.image
                and any(socket.as_pointer() in active_outputs for socket in node.outputs)
                for node in material.node_tree.nodes
            )
        if not reaches_shader:
            raise AssertionError(f'Saved source shell dye is disconnected from ordinary lighting: {group.name}')
    special = _object(receipt.get('special_vfx'), 'special_vfx')
    if special.get('format') == 'EndfieldCharacterSpecialVfx/1':
        verify_special_vfx(group, special)
    return {'status': 'passed', 'shell_mesh': candidate.name, 'source_vertex_count': len(candidate.data.vertices),
            'layer_count': receipt['layer_count'], 'uv1_and_object_slots_verified': True}


def verify_prepared_fur_shells(receipt: Mapping[str, object]) -> dict[str, object]:
    """Validate an explicit shell build against its unchanged prepared candidate set."""
    from character_fur_curves_profile import read_fur_curves_preparation
    if receipt.get('format') != 'EndfieldPreparedSourceFurShells/1' or receipt.get('status') != 'completed':
        raise ValueError('Prepared source shell build is not complete')
    reference = _object(receipt.get('preparation'), 'preparation')
    preparation = read_fur_curves_preparation(Path(_text(reference.get('path'), 'preparation.path')))
    entries, skipped = receipt.get('entries'), receipt.get('skipped_hidden_sources')
    if (preparation is None or preparation.sha256 != reference.get('sha256')
            or not isinstance(entries, list) or not isinstance(skipped, list)
            or len(entries) + len(skipped) != receipt.get('source_mesh_candidates')
            or len(entries) + len(skipped) != len(preparation.profiles)):
        raise AssertionError('Prepared source shell candidates differ from their saved preparation')
    identities = [_identity(_object(entry, 'source entry').get('source_mesh_identity')) for entry in entries + skipped]
    expected = {(profile.mesh_identity[0], int(profile.mesh_identity[1])) for profile in preparation.profiles}
    if len(set(identities)) != len(identities) or set(identities) != expected:
        raise AssertionError('Prepared source shell mesh identities differ from the saved candidate set')
    checks = [verify_source_fur_shell(_object(entry, 'shell entry')) for entry in entries]
    for value in skipped:
        row = _object(value, 'hidden source')
        obj = bpy.data.objects[_text(row.get('source_mesh'), 'hidden source_mesh')]
        if not (obj.hide_render or obj.hide_get()):
            raise AssertionError(f'Skipped source shell candidate unexpectedly became visible: {obj.name}')
    return {'status': 'passed', 'entries': checks, 'skipped_hidden_source_count': len(skipped)}
