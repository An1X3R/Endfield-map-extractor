"""Attach explicit source Fur snow to retained physical Curves preview materials."""
from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
import hashlib
import json
from pathlib import Path

import bpy
import numpy as np

from attach_character_weather_surface import (
    SurfacePort, WeatherProjection, WeatherSurfacePorts, create_weather_instance,
    connect_weather_consumers, linked_source,
)
from character_fur_geometry import _point_values, vertex_authored_normals, vertex_uvs
from character_hair_fur_snow_profile import HairFurSnowProfile
from character_material_profile import profile_shader_identity
from character_node_graph import node
from character_fur_wet_profile import attach_fur_wet_before_snow, verify_fur_wet_before_snow
from character_wetfilm_profile import file_digest, verify_weather_controls, weather_control_source_record
from patch_character_hgrp import source_keywords

POSITION = 'EF_FUR_WEATHER_SOURCE_POSITION'
NORMAL = 'EF_FUR_WEATHER_SOURCE_NORMAL'
BASE_UV = 'EF_FUR_WEATHER_BASE_UV'
PACKED_UV = 'EF_FUR_WEATHER_PACKED_UV'


@dataclass(frozen=True)
class CurveTextureBinding:
    texture: bpy.types.ShaderNodeTexImage
    mapping: bpy.types.ShaderNodeMapping
    uv_layer: str


def texture_binding(group: bpy.types.ShaderNodeTree, slot: str) -> CurveTextureBinding:
    textures = tuple(item for item in group.nodes if item.type == 'TEX_IMAGE' and item.get('source_slot') == slot)
    if len(textures) != 1 or textures[0].image is None or textures[0].image.packed_file is None:
        raise ValueError('Fur Curves require one packed actual source texture: ' + slot)
    if textures[0].projection != 'FLAT':
        raise ValueError('Fur Curves only support the verified FLAT source texture projection: ' + slot)
    mapping = linked_source(textures[0].inputs['Vector']).node
    if mapping.type != 'MAPPING' or mapping.vector_type != 'POINT':
        raise ValueError('Fur Curves require the existing POINT texture ST mapping: ' + slot)
    if any(mapping.inputs[name].is_linked for name in ('Location', 'Rotation', 'Scale')):
        raise ValueError('Fur Curves cannot copy dynamically linked texture ST inputs: ' + slot)
    uv = linked_source(mapping.inputs['Vector']).node
    if uv.type != 'UVMAP' or not uv.uv_map:
        raise ValueError('Fur Curves require the actual named source UV layer: ' + slot)
    return CurveTextureBinding(textures[0], mapping, uv.uv_map)


def material_groups(material: bpy.types.Material) -> frozenset[str]:
    if not material.use_nodes or material.node_tree is None:
        raise ValueError('Fur source material requires an actual node tree')
    pending = [material.node_tree]
    seen: set[str] = set()
    while pending:
        tree = pending.pop()
        if tree.name in seen:
            continue
        seen.add(tree.name)
        pending.extend(item.node_tree for item in tree.nodes if item.type == 'GROUP' and item.node_tree is not None)
    return frozenset(seen)


def curve_attribute_hashes(data: bpy.types.Curves, names: tuple[str, ...]) -> dict[str, str]:
    result: dict[str, str] = {}
    kinds = {'FLOAT': (1, 'value', np.float32), 'FLOAT_VECTOR': (3, 'vector', np.float32),
             'FLOAT2': (2, 'vector', np.float32), 'FLOAT_COLOR': (4, 'color', np.float32),
             'INT': (1, 'value', np.int32)}
    for name in names:
        attribute = data.attributes[name]
        if attribute.domain != 'POINT' or attribute.data_type not in kinds:
            raise ValueError('Unsupported retained Curves attribute: ' + name)
        width, field, kind = kinds[attribute.data_type]
        values = np.empty(len(attribute.data) * width, dtype=kind)
        attribute.data.foreach_get(field, values)
        result[name] = hashlib.sha256(values.tobytes()).hexdigest()
    return result


def source_attributes(source: bpy.types.Object, guides: bpy.types.Object,
                      curves: bpy.types.Object, base: CurveTextureBinding,
                      packed: CurveTextureBinding) -> dict[str, np.ndarray]:
    lengths = tuple(curve.points_length for curve in guides.data.curves)
    if not lengths or len(set(lengths)) != 1 or lengths[0] < 2:
        raise ValueError('Fur weather requires the verified equal-length source shell guides')
    indices = _point_values(guides.data, 'source_vertex_index', 'INT', 'value', 1).reshape(-1)
    if np.any(indices < 0) or np.any(indices >= len(source.data.vertices)):
        raise ValueError('Fur weather source vertex index is outside the selected mesh')
    roots = np.repeat(indices.reshape(len(lengths), lengths[0])[:, 0], lengths[0])
    samples = _point_values(curves.data, 'fur_skin_sample_indices', 'FLOAT_VECTOR', 'vector', 3)
    weights = _point_values(curves.data, 'fur_skin_sample_weights', 'FLOAT_VECTOR', 'vector', 3)
    if np.any(samples != np.floor(samples)) or np.any(samples < 0) or np.any(samples >= len(indices)) or np.any(weights < 0) or np.max(np.abs(weights.sum(axis=1)-1)) > 1e-6:
        raise ValueError('Fur weather requires valid existing guide sample indices and barycentric weights')
    matrix = np.asarray(source.matrix_world, dtype=np.float64)
    local = np.asarray([tuple(vertex.co) for vertex in source.data.vertices], dtype=np.float64)
    position = local @ matrix[:3, :3].T + matrix[:3, 3]
    normal = vertex_authored_normals(source.data, 1e-6) @ np.linalg.inv(matrix[:3, :3])
    normal /= np.linalg.norm(normal, axis=1)[:, None]
    # Preserve the source root orientation; do not substitute physical strand normals.
    values = {POSITION: position[roots][:, (0, 2, 1)] * (1., 1., -1.),
              NORMAL: normal[roots][:, (0, 2, 1)] * (1., 1., -1.)}
    for name, binding in ((BASE_UV, base), (PACKED_UV, packed)):
        uv = vertex_uvs(source.data, binding.uv_layer, 1e-6)[roots]
        values[name] = np.column_stack((uv, np.zeros(len(uv))))
    result: dict[str, np.ndarray] = {}
    for name, value in values.items():
        result[name] = (value[samples.astype(np.int32)] * weights[:, :, None]).sum(axis=1).astype(np.float32)
    return result


def add_attributes(curves: bpy.types.Object, values: dict[str, np.ndarray]) -> None:
    for name, value in values.items():
        if name in curves.data.attributes:
            raise ValueError('Fur weather attribute already attached: ' + name)
        attribute = curves.data.attributes.new(name, 'FLOAT_VECTOR', 'POINT')
        attribute.data.foreach_set('vector', value.reshape(-1))


def sample_texture(tree: bpy.types.ShaderNodeTree, binding: CurveTextureBinding,
                   attribute_name: str, slot: str) -> bpy.types.ShaderNodeTexImage:
    uv = node(tree, 'ShaderNodeAttribute', 'Source Curves UV ' + slot, 'Fur Curves Weather')
    uv.attribute_name = attribute_name
    mapping = node(tree, 'ShaderNodeMapping', 'Source ST ' + slot, 'Fur Curves Weather')
    mapping.vector_type = binding.mapping.vector_type
    for name in ('Location', 'Rotation', 'Scale'):
        mapping.inputs[name].default_value = binding.mapping.inputs[name].default_value
    texture = node(tree, 'ShaderNodeTexImage', 'Actual source texture ' + slot, 'Fur Curves Weather')
    texture.image = binding.texture.image
    texture.interpolation = binding.texture.interpolation
    texture.extension = binding.texture.extension
    texture.projection = binding.texture.projection
    texture['source_slot'] = slot
    tree.links.new(uv.outputs['Vector'], mapping.inputs['Vector'])
    tree.links.new(mapping.outputs[0], texture.inputs['Vector'])
    return texture


def scalar_source(tree: bpy.types.ShaderNodeTree, socket: bpy.types.NodeSocket,
                  label: str) -> bpy.types.NodeSocket:
    if socket.is_linked:
        return linked_source(socket)
    value = node(tree, 'ShaderNodeValue', label, 'Fur Curves Weather')
    value.outputs[0].default_value = socket.default_value
    return value.outputs[0]


def attach_fur_curve_snow(source_mesh: bpy.types.Object, source_material: bpy.types.Material,
                          source_group: bpy.types.ShaderNodeTree, bind_guides: bpy.types.Object,
                          driven_curves: bpy.types.Object, controls: bpy.types.ShaderNodeTree,
                          profile: HairFurSnowProfile,
                          layout: Callable[[bpy.types.NodeTree], None]) -> dict[str, object]:
    """Add weather attributes and a private material without changing retained curve geometry."""
    if source_mesh.type != 'MESH' or bind_guides.type != 'CURVES' or driven_curves.type != 'CURVES':
        raise ValueError('Fur snow requires explicit source Mesh and bind/driven Curves')
    if bind_guides.parent != source_mesh or driven_curves.parent != source_mesh or driven_curves.get('source_guides') != bind_guides.name:
        raise ValueError('Fur snow Curves do not share the selected source parent/guide identity')
    if driven_curves.data.users != 1:
        raise ValueError('Fur snow requires dedicated driven Curves data; shared data would modify archived objects')
    if driven_curves.data == bind_guides.data or driven_curves.matrix_world != bind_guides.matrix_world:
        raise ValueError('Fur snow requires distinct retained Curves data in the same source frame')
    if source_material not in tuple(slot.material for slot in source_mesh.material_slots) or source_group.name not in material_groups(source_material):
        raise ValueError('Fur snow source material/group is not attached to the selected source mesh')
    if source_group.get('source_shader') != 'HGRP/CharacterNPR':
        raise ValueError('Fur snow source group is not the required shader family')
    family = tuple(item for item in profile.families if item.family == 'HGRP/CharacterNPR')
    keywords = tuple(sorted(source_keywords(source_group)))
    if len(family) != 1 or keywords not in family[0].keyword_sets or '_CHARACTER_FUR' not in keywords:
        raise ValueError('Fur snow exact source keyword set is outside the configured verified kernel')
    if len(driven_curves.data.materials) != 1 or driven_curves.data.materials[0] is None:
        raise ValueError('Fur snow requires the existing unambiguous Curves preview material')
    if driven_curves.get('fur_curve_weather'):
        raise ValueError('Fur Curves snow is already attached')
    base = texture_binding(source_group, '_BaseMap')
    packed = texture_binding(source_group, '_MetallicGlossMap')
    if packed.texture.image.colorspace_settings.name != 'Non-Color':
        raise ValueError('Fur snow packed B mask must use actual linear source data')
    weather_source = weather_control_source_record(controls)
    verify_weather_controls(controls, weather_source)
    original_names = tuple(attribute.name for attribute in driven_curves.data.attributes)
    original_hashes = curve_attribute_hashes(driven_curves.data, original_names)
    values = source_attributes(source_mesh, bind_guides, driven_curves, base, packed)
    previous = driven_curves.data.materials[0]
    material = previous.copy()
    material.name = previous.name + ' Source Fur Snow'
    tree = material.node_tree
    surfaces = tuple(item for item in tree.nodes if item.type == 'BSDF_PRINCIPLED')
    if len(surfaces) != 1:
        raise ValueError('Fur snow requires one retained physical Curves surface')
    surface = surfaces[0]
    albedo = linked_source(surface.inputs['Base Color'])
    geometry = node(tree, 'ShaderNodeNewGeometry', 'Current physical Curves shading geometry', 'Fur Curves Weather')
    normal = linked_source(surface.inputs['Normal']) if surface.inputs['Normal'].is_linked else geometry.outputs['Normal']
    ports = WeatherSurfacePorts(SurfacePort(albedo, (surface.inputs['Base Color'],)),
        SurfacePort(albedo, ()),
        SurfacePort(scalar_source(tree, surface.inputs['Roughness'], 'Retained curve roughness'), (surface.inputs['Roughness'],)),
        SurfacePort(scalar_source(tree, surface.inputs['Metallic'], 'Retained curve metallic'), (surface.inputs['Metallic'],)),
        SurfacePort(normal, (surface.inputs['Normal'],)))
    actual_base = sample_texture(tree, base, BASE_UV, '_BaseMap')
    actual_packed = sample_texture(tree, packed, PACKED_UV, '_MetallicGlossMap')
    channels = node(tree, 'ShaderNodeSeparateColor', 'Actual packed source MetallicGlossMap channels', 'Fur Curves Weather')
    channels.mode = 'RGB'
    tree.links.new(actual_packed.outputs['Color'], channels.inputs['Color'])
    tree['source_floats'] = source_group['source_floats']
    image = bpy.data.images.load(str(profile.texture), check_existing=False)
    image.colorspace_settings.name = 'Non-Color'
    image.alpha_mode = 'CHANNEL_PACKED'
    image.pack()
    from character_hair_fur_snow import make_native_fur_snow
    kernel = make_native_fur_snow(image, family[0].shader_sha256, layout)
    projection = WeatherProjection(POSITION, NORMAL, profile.projection.orientation,
        profile.projection.tiling, 'Explicit source root authored rest normals/positions; physical Curves host preview, not captured native varyings')
    instance, state = create_weather_instance(tree, kernel, controls, projection, ports, 'Compiled source Fur snow on Curves preview')
    tree.links.new(state.outputs['Native Snow Intensity'], instance.inputs['Snow Intensity'])
    tree.links.new(linked_source(actual_base.inputs['Vector']), instance.inputs['UV'])
    tree.links.new(channels.outputs['Blue'], instance.inputs['Metallic Gloss B'])
    consumers = connect_weather_consumers(tree, instance, ports)
    wet_binding = attach_fur_wet_before_snow(tree, instance, state, family[0].shader_sha256, layout)
    add_attributes(driven_curves, values)
    driven_curves.data.materials[0] = material
    driven_curves['fur_curve_weather'] = 'EndfieldFurCurveWeather/2'
    layout(tree)
    if curve_attribute_hashes(driven_curves.data, original_names) != original_hashes:
        raise AssertionError('Fur snow attachment altered a retained curve point/radius/color/skin attribute')
    return {'format': 'EndfieldFurCurveWeather/2', 'status': 'connected', 'source_mesh': source_mesh.name,
        'source_material': source_material.name, 'source_group': source_group.name,
        'source_identity': source_group['source_identity'], 'keywords': list(keywords),
        'bind_guides': bind_guides.name, 'curves': driven_curves.name, 'material': material.name,
        'previous_material': previous.name, 'controls': controls.name, 'weather_source': weather_source,
        'instance': instance.name, 'state': state.name, 'surface': surface.name,
        'wet_binding': wet_binding,
        'original_attributes': original_hashes,
        'new_attributes': curve_attribute_hashes(driven_curves.data, tuple(values)),
        'channels': consumers, 'textures': [{'slot': slot, 'source_uv_layer': binding.uv_layer,
            'image': binding.texture.image.name, 'packed_sha256': hashlib.sha256(binding.texture.image.packed_file.data).hexdigest(),
            'mapping': {name: list(binding.mapping.inputs[name].default_value) for name in ('Location', 'Rotation', 'Scale')}}
            for slot, binding in (('_BaseMap', base), ('_MetallicGlossMap', packed))],
        'snow_image': image.name, 'snow_sha256': profile.texture_sha256,
        'shader_sha256': family[0].shader_sha256, 'source_scope': family[0].scope,
        'source_shader_identity': family[0].shader_identity,
        'profile_path': str(profile.path), 'profile_sha256': file_digest(profile.path),
        'source_files': [{'path': str(path), 'sha256': file_digest(path)} for path in (profile.path, profile.texture, profile.manifest)],
        'shadow_color_consumer': False, 'native_curve_lighting_verified': False,
        'projection_provenance': projection.provenance}


def verify_fur_curve_snow(receipt: dict[str, object]) -> dict[str, object]:
    """Verify the saved dedicated material, source identities, attributes and actual live links."""
    version = receipt['format']
    if version not in ('EndfieldFurCurveWeather/1', 'EndfieldFurCurveWeather/2') or receipt['status'] != 'connected':
        raise ValueError('Expected a completed explicit Fur Curves snow attachment version 1 or 2')
    if version == 'EndfieldFurCurveWeather/1' and 'wet_binding' in receipt:
        raise ValueError('Fur Curves version 1 cannot carry a version 2 wet binding')
    if version == 'EndfieldFurCurveWeather/2' and not isinstance(receipt.get('wet_binding'), dict):
        raise ValueError('Fur Curves version 2 requires its explicit wet binding receipt')
    profile_path = Path(receipt['profile_path']).resolve(strict=True)
    if file_digest(profile_path) != receipt['profile_sha256']:
        raise AssertionError('Saved Fur Curves weather profile changed after build')
    source_shader_identity = profile_shader_identity(profile_path, 'HGRP/CharacterNPR')
    if receipt['source_shader_identity'] != source_shader_identity or receipt['shader_sha256'] != source_shader_identity['object_sha256']:
        raise AssertionError('Saved Fur Curves Shader object identity differs from its per-export profile')
    curves = bpy.data.objects[receipt['curves']]
    source = bpy.data.objects[receipt['source_mesh']]
    guides = bpy.data.objects[receipt['bind_guides']]
    group = bpy.data.node_groups[receipt['source_group']]
    material = bpy.data.materials[receipt['material']]
    if curves.parent != source or guides.parent != source or curves.get('source_guides') != guides.name or curves.get('fur_curve_weather') != version or curves.data.materials[0] != material or material == bpy.data.materials[receipt['previous_material']]:
        raise AssertionError('Saved Fur Curves material or parent/source guide identity differs')
    if group['source_identity'] != receipt['source_identity'] or list(sorted(source_keywords(group))) != receipt['keywords']:
        raise AssertionError('Saved Fur source material identity/keywords differ')
    if curve_attribute_hashes(curves.data, tuple(receipt['original_attributes'])) != receipt['original_attributes'] or curve_attribute_hashes(curves.data, tuple(receipt['new_attributes'])) != receipt['new_attributes']:
        raise AssertionError('Saved Fur weather or retained geometry/color/width attributes differ')
    tree = material.node_tree
    instance, state, surface = (tree.nodes[receipt[name]] for name in ('instance', 'state', 'surface'))
    controls = bpy.data.node_groups[receipt['controls']]
    verify_weather_controls(controls, receipt['weather_source'])
    wet_audit = verify_fur_wet_before_snow(tree, receipt['wet_binding']) if version == 'EndfieldFurCurveWeather/2' else None
    if state.node_tree != controls or linked_source(instance.inputs['Snow Intensity']) != state.outputs['Native Snow Intensity']:
        raise AssertionError('Saved Fur Curves shared weather control tree differs or is disconnected')
    if instance.node_tree.get('snow_family') != 'Fur' or instance.node_tree.get('source_shader_sha256') != receipt['shader_sha256']:
        raise AssertionError('Saved Fur Curves kernel source identity differs')
    image = bpy.data.images[receipt['snow_image']]
    if image.packed_file is None or hashlib.sha256(image.packed_file.data).hexdigest() != receipt['snow_sha256'] or image.colorspace_settings.name != 'Non-Color' or image.alpha_mode != 'CHANNEL_PACKED':
        raise AssertionError('Saved Fur snow source texture differs')
    for label, attribute in (('Projection Position', POSITION), ('Projection Normal', NORMAL)):
        projection = linked_source(instance.inputs[label]).node
        if projection.type != 'ATTRIBUTE' or projection.attribute_name != attribute:
            raise AssertionError('Saved Fur snow actual projection attribute link differs: ' + label)
    for slot_name, attribute in (('_BaseMap', BASE_UV), ('_MetallicGlossMap', PACKED_UV)):
        row = next(row for row in receipt['textures'] if row['slot'] == slot_name)
        actual = next(item for item in tree.nodes if item.type == 'TEX_IMAGE' and item.get('source_slot') == slot_name)
        mapping = linked_source(actual.inputs['Vector']).node
        uv = linked_source(mapping.inputs['Vector']).node
        if actual.projection != 'FLAT' or mapping.type != 'MAPPING' or mapping.vector_type != 'POINT' or any(mapping.inputs[name].is_linked for name in ('Location', 'Rotation', 'Scale')):
            raise AssertionError('Saved Fur Curves source ST/projection is outside the verified static mapping')
        if slot_name == '_MetallicGlossMap' and actual.image.colorspace_settings.name != 'Non-Color':
            raise AssertionError('Saved Fur packed mask no longer uses linear source data')
        if actual.image.name != row['image'] or actual.image.packed_file is None or hashlib.sha256(actual.image.packed_file.data).hexdigest() != row['packed_sha256'] or uv.type != 'ATTRIBUTE' or uv.attribute_name != attribute or {name: list(mapping.inputs[name].default_value) for name in ('Location', 'Rotation', 'Scale')} != row['mapping']:
            raise AssertionError('Saved Fur Curves actual source texture/ST/UV differs: ' + slot_name)
        if slot_name == '_BaseMap' and linked_source(instance.inputs['UV']) != linked_source(actual.inputs['Vector']):
            raise AssertionError('Saved Fur snow UV is not the actual BaseMap ST output')
    mask = linked_source(instance.inputs['Metallic Gloss B'])
    if mask.name != 'Blue' or linked_source(mask.node.inputs['Color']).node.get('source_slot') != '_MetallicGlossMap':
        raise AssertionError('Saved Fur snow packed B mask is disconnected')
    from character_material_authoring import relocated_source_output
    for label in ('Base Color', 'Roughness', 'Metallic'):
        output = {'Base Color': 'Albedo', 'Roughness': 'Roughness', 'Metallic': 'Metallic'}[label]
        if linked_source(surface.inputs[label]) != relocated_source_output(instance, instance.outputs[output]):
            raise AssertionError('Saved Fur snow live consumer is disconnected: ' + label)
    final = linked_source(surface.inputs['Normal']).node
    if final.label != 'Native weather normal to HGRP surface frame result':
        raise AssertionError('Saved Fur snow live normal conversion is disconnected')
    for row in receipt['source_files']:
        if file_digest(Path(row['path'])) != row['sha256']:
            raise AssertionError('Saved Fur snow source file changed: ' + row['path'])
    summary = {'status': 'passed', 'contract': version, 'live_surface_channels': 4,
               'retained_attributes': len(receipt['original_attributes']),
               'new_attributes': len(receipt['new_attributes']), 'shared_controls': controls.name,
               'native_curve_lighting_verified': False, 'shadow_color_consumer': False}
    if wet_audit is not None:
        summary['live_wet_to_snow_channels'] = wet_audit['live_wet_to_snow_channels']
        summary['fur_tt_consumer'] = wet_audit['fur_tt_consumer']
    return summary
