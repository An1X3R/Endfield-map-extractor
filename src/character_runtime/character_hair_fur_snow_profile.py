"""Bind exact Hair/Fur snow variants to existing live surface consumers."""
from __future__ import annotations

from dataclasses import dataclass
import json
import math
from pathlib import Path
from typing import Callable

import bpy

from attach_character_weather_surface import (
    WeatherProjection, WeatherSurfacePorts, SurfacePort, linked_source,
    outgoing, create_weather_instance, connect_weather_consumers,
)
from character_node_graph import node
from character_standard_snow_profile import _object, _string
from character_wetfilm_profile import (
    file_digest, verified_file, weather_control_source_record, verify_weather_controls,
)
from character_material_profile import ShaderIdentity, profile_shader_identity
from patch_character_hgrp import by_label, source_keywords
from patch_native_character_weather import rest_attributes
from review_character_hgrp import visible_source_group_names


@dataclass(frozen=True)
class SnowFamilySource:
    family: str
    shader_identity: ShaderIdentity
    shader_sha256: str
    keyword_sets: tuple[tuple[str, ...], ...]
    scope: str


@dataclass(frozen=True)
class HairFurSnowProfile:
    path: Path
    families: tuple[SnowFamilySource, ...]
    manifest: Path
    texture: Path
    texture_sha256: str
    projection: WeatherProjection


def read_hair_fur_snow_profile(path: Path) -> HairFurSnowProfile | None:
    document = _object(json.loads(path.read_text(encoding='utf-8')), 'character profile')
    if 'hair_fur_snow' not in document:
        return None
    value = _object(document['hair_fur_snow'], 'hair_fur_snow')
    if value.get('format') != 'EndfieldHairFurSnowProfile/1':
        raise ValueError('Unsupported Hair/Fur snow profile format')
    families: list[SnowFamilySource] = []
    for key, family in (('hair', 'HGRP/CharacterNPR_Hair'), ('fur', 'HGRP/CharacterNPR')):
        if key not in value:
            continue
        raw = _object(value[key], key + ' snow source')
        shader_identity = profile_shader_identity(path, family)
        sets = raw.get('keyword_sets')
        if not isinstance(sets, list) or not sets:
            raise TypeError('Snow source keyword_sets must be a nonempty array: ' + key)
        validated: list[tuple[str, ...]] = []
        for keywords in sets:
            if not isinstance(keywords, list) or any(not isinstance(word, str) or not word for word in keywords):
                raise TypeError('Snow keyword set must be an explicit string array: ' + key)
            if len(keywords) != len(set(keywords)):
                raise ValueError('Snow source keyword set contains duplicates: ' + key)
            if key == 'fur' and '_CHARACTER_FUR' not in keywords:
                raise ValueError('Fur snow requires the exact _CHARACTER_FUR keyword')
            validated.append(tuple(sorted(keywords)))
        families.append(SnowFamilySource(family, shader_identity, shader_identity['object_sha256'], tuple(validated),
            _string(raw.get('source_scope'), key + ' source scope')))
    if not families:
        raise ValueError('Hair/Fur snow profile has no source Shader families')
    manifest = verified_file(path.parent, value.get('texture_manifest'), value.get('texture_manifest_sha256'))
    rows = json.loads(manifest.read_text(encoding='utf-8'))
    if not isinstance(rows, list):
        raise TypeError('Snow texture manifest must be an array')
    selected = tuple(row for row in rows if isinstance(row, dict) and row.get('slot') == 'characterSnowTexture')
    if len(selected) != 1 or type(selected[0].get('color_space')) is not int or selected[0]['color_space'] != 0:
        raise ValueError('Hair/Fur snow requires one exact linear characterSnowTexture')
    texture = verified_file(manifest.parent, selected[0].get('artifact'), selected[0].get('sha256'))
    orientation, tiling = value.get('orientation'), value.get('tiling')
    if type(orientation) not in (int, float) or orientation not in (0, 1):
        raise ValueError('Hair/Fur snow orientation must be explicit 0 or 1')
    if type(tiling) not in (int, float) or not math.isfinite(tiling) or not 0 < tiling < 1000:
        raise ValueError('Hair/Fur snow tiling must be finite and positive below 1000')
    projection = WeatherProjection('EF_EXPERIMENTAL_REST_UNITY_POSITION',
        'EF_EXPERIMENTAL_REST_UNITY_NORMAL', float(orientation), float(tiling),
        _string(value.get('projection_provenance'), 'Hair/Fur projection provenance'))
    return HairFurSnowProfile(path.resolve(strict=True), tuple(families), manifest, texture, file_digest(texture), projection)


def _surface(tree: bpy.types.ShaderNodeTree) -> bpy.types.Node:
    matches = tuple(item for item in tree.nodes if item.type == 'BSDF_PRINCIPLED')
    if len(matches) != 1:
        raise ValueError('Hair/Fur snow requires one existing physical surface: ' + tree.name)
    return matches[0]


def _normal_ports(tree: bpy.types.ShaderNodeTree, surface: bpy.types.Node) -> SurfacePort:
    normal = linked_source(surface.inputs['Normal']) if surface.inputs['Normal'].is_linked else linked_source(by_label(tree, 'Normalize evaluated surface normal').inputs[0])
    targets = [surface.inputs['Normal'], by_label(tree, 'Normalize evaluated surface normal').inputs[0]]
    if tree.get('source_shader') == 'HGRP/CharacterNPR_Hair':
        targets.append(by_label(tree, 'Normalize final weather surface normal').inputs[0])
        targets.extend(item.inputs['Normal'] for item in tree.nodes if item.type == 'GROUP' and item.label.startswith('Compiled additional Hair mode0 '))
    return SurfacePort(normal, tuple(targets))


def read_hair_snow_ports(tree: bpy.types.ShaderNodeTree) -> WeatherSurfacePorts:
    """Keep independent strand/specular normals and tangent metallic untouched."""
    if not tree.get('hair_lighting_patch') or not tree.get('hgrp_scope'):
        raise ValueError('Hair snow needs the already attached live Hair lighting branch: ' + tree.name)
    surface = _surface(tree)
    albedo = linked_source(surface.inputs['Base Color'])
    diffuse = by_label(tree, 'Source diffuse albedo coefficient')
    if linked_source(diffuse.inputs['Color1']) != albedo:
        raise ValueError('Hair physical and live diffuse albedo disagree: ' + tree.name)
    targets = (surface.inputs['Base Color'], diffuse.inputs['Color1'],
        by_label(tree, 'Approx shadow-to-lit blend driven by source ramp alpha').inputs['Color2'])
    shadow = linked_source(by_label(tree, 'Scaled source shadow albedo').inputs['Color1'])
    metallic = node(tree, 'ShaderNodeValue', 'Hair snow native metallic input zero', 'Hair Snow Surface')
    metallic.outputs[0].default_value = 0.0
    return WeatherSurfacePorts(SurfacePort(albedo, targets), SurfacePort(shadow, outgoing(shadow)),
        SurfacePort(linked_source(surface.inputs['Roughness']), (surface.inputs['Roughness'],)),
        SurfacePort(metallic.outputs[0], ()), _normal_ports(tree, surface))


def read_fur_snow_ports(tree: bpy.types.ShaderNodeTree) -> WeatherSurfacePorts:
    """Bind the retained Fur host lighting without substituting Standard HGRP."""
    if tree.get('hgrp_scope'):
        raise ValueError('Fur snow host branch differs from the audited retained lighting: ' + tree.name)
    surface = _surface(tree)
    albedo = linked_source(surface.inputs['Base Color'])
    blend = by_label(tree, 'Approx shadow-to-lit blend driven by source ramp alpha')
    shadow = linked_source(blend.inputs['Color1'])
    return WeatherSurfacePorts(SurfacePort(albedo, (surface.inputs['Base Color'], blend.inputs['Color2'])),
        SurfacePort(shadow, outgoing(shadow)),
        SurfacePort(linked_source(surface.inputs['Roughness']), (surface.inputs['Roughness'],)),
        SurfacePort(linked_source(surface.inputs['Metallic']), (surface.inputs['Metallic'],)),
        _normal_ports(tree, surface))


def _projection_objects(groups: tuple[bpy.types.ShaderNodeTree, ...], projection: WeatherProjection) -> list[dict[str, object]]:
    names = frozenset(tree.name for tree in groups)
    rows: list[dict[str, object]] = []
    for obj in bpy.context.scene.objects:
        if obj.type != 'MESH' or obj.hide_render:
            continue
        if not any(slot.material is not None and slot.material.use_nodes and any(
            item.type == 'GROUP' and item.node_tree is not None and item.node_tree.name in names
            for item in slot.material.node_tree.nodes) for slot in obj.material_slots):
            continue
        attributes = tuple(obj.data.attributes.get(name) for name in (projection.position_attribute, projection.normal_attribute))
        if all(attribute is None for attribute in attributes):
            rows.extend(rest_attributes((obj,)))
        elif any(attribute is None or attribute.domain != 'POINT' or attribute.data_type != 'FLOAT_VECTOR'
                 or len(attribute.data) != len(obj.data.vertices) for attribute in attributes):
            raise ValueError('Hair/Fur existing projection attributes are incomplete: ' + obj.name)
        else:
            rows.append({'object': obj.name, 'vertices': len(obj.data.vertices), 'binding': projection.provenance})
    if not rows:
        raise ValueError('Hair/Fur snow has no visible source mesh consumers')
    return rows


def attach_profile_hair_fur_snow(profile_path: Path, controls: bpy.types.ShaderNodeTree,
                                 layout: Callable[[bpy.types.NodeTree], None]) -> dict[str, object]:
    profile = read_hair_fur_snow_profile(profile_path)
    if profile is None:
        return {'status': 'not_configured', 'bindings': []}
    active = visible_source_group_names()
    selected: list[tuple[bpy.types.ShaderNodeTree, SnowFamilySource]] = []
    skipped: list[dict[str, object]] = []
    for tree in bpy.data.node_groups:
        if tree.name not in active:
            continue
        family = tree.get('source_shader')
        if family not in ('HGRP/CharacterNPR_Hair', 'HGRP/CharacterNPR'):
            continue
        keywords = tuple(sorted(source_keywords(tree)))
        if family != 'HGRP/CharacterNPR_Hair' and not (family == 'HGRP/CharacterNPR' and '_CHARACTER_FUR' in keywords):
            continue
        source = next(item for item in profile.families if item.family == family)
        if keywords not in source.keyword_sets:
            skipped.append({'group': tree.name, 'keywords': list(keywords), 'reason': 'Exact source keyword variant is outside the verified snow kernel.'})
            continue
        if tree.get('weather_surface_binding'):
            raise ValueError('Hair/Fur weather already attached: ' + tree.name)
        selected.append((tree, source))
    if not selected:
        return {'status': 'no_matching_hair_fur_materials', 'bindings': [], 'skipped': skipped}
    source_record = weather_control_source_record(controls)
    verify_weather_controls(controls, source_record)
    hair_clock = None
    if any(source.family == 'HGRP/CharacterNPR_Hair' for _, source in selected):
        from character_hair_wet_profile import ensure_hair_weather_clock
        hair_clock = ensure_hair_weather_clock(profile_path, controls, layout)
    attributes = _projection_objects(tuple(tree for tree, _ in selected), profile.projection)
    from character_hair_fur_snow import make_native_hair_snow, make_native_fur_snow
    image = bpy.data.images.load(str(profile.texture), check_existing=False)
    image.colorspace_settings.name = 'Non-Color'
    image.alpha_mode = 'CHANNEL_PACKED'
    image.pack()
    image['endfield_weather_source_sha256'] = profile.texture_sha256
    kernels: dict[str, bpy.types.ShaderNodeTree] = {}
    bindings: list[dict[str, object]] = []
    for tree, source in selected:
        hair = source.family == 'HGRP/CharacterNPR_Hair'
        if source.family not in kernels:
            kernels[source.family] = (make_native_hair_snow if hair else make_native_fur_snow)(image, source.shader_sha256, layout)
        ports = read_hair_snow_ports(tree) if hair else read_fur_snow_ports(tree)
        instance, state = create_weather_instance(tree, kernels[source.family], controls, profile.projection, ports,
            'Compiled Hair snow before live lighting' if hair else 'Compiled Fur snow before retained host lighting')
        tree.links.new(state.outputs['Native Snow Intensity'], instance.inputs['Snow Intensity'])
        packed = tuple(item for item in tree.nodes if item.type == 'TEX_IMAGE' and item.get('source_slot') == '_MetallicGlossMap')
        if len(packed) != 1 or 'Metallic Gloss B' not in instance.inputs:
            raise ValueError('Mapped Hair/Fur snow requires the exact source MetallicGlossMap B kernel: ' + tree.name)
        tree.links.new(by_label(tree, 'R metallic / G specular / B AO / A smoothness').outputs['Blue'], instance.inputs['Metallic Gloss B'])
        if not hair:
            # Vertex TEXCOORD1 is transformed by BaseMap_ST before snow sampling.
            base = tuple(item for item in tree.nodes if item.type == 'TEX_IMAGE' and item.get('source_slot') == '_BaseMap')
            if len(base) != 1:
                raise ValueError('Fur snow requires the exact source BaseMap UV transform: ' + tree.name)
            tree.links.new(linked_source(base[0].inputs['Vector']), instance.inputs['UV'])
        wet = None
        if hair:
            from character_hair_wet_profile import attach_hair_wet_before_snow
            wet = attach_hair_wet_before_snow(tree, instance, state, source.shader_sha256, layout)
        else:
            from character_fur_wet_profile import attach_fur_wet_before_snow
            wet = attach_fur_wet_before_snow(tree, instance, state, source.shader_sha256, layout)
        channels = connect_weather_consumers(tree, instance, ports)
        # Preserve the exact captured target identities for script-disabled reopen checks.
        target_rows = []
        for channel, port in (('Albedo', ports.albedo), ('Shadow Color', ports.shadow), ('Roughness', ports.roughness), ('Metallic', ports.metallic), ('Normal', ports.normal)):
            target_rows.extend({'channel': channel, 'node': target.node.name, 'socket_index': list(target.node.inputs).index(target)} for target in port.targets)
        tree['weather_surface_binding'] = 'HGRPHairFurSnowSurface/1'
        tree['weather_projection_provenance'] = profile.projection.provenance
        layout(tree)
        bindings.append({'group': tree.name, 'source_identity': tree['source_identity'], 'family': source.family,
            'keywords': list(source_keywords(tree)), 'source_scope': source.scope,
            'shader_sha256': source.shader_sha256, 'instance': instance.name, 'kernel': instance.node_tree.name,
            'state': state.name, 'channels': channels, 'targets': target_rows,
            'wet_before_snow': wet,
            'host_lighting_scope': 'Existing Hair main/strand/additional lighting' if hair else 'Existing retained PBR/artist diffuse; native Fur lighting not reconstructed'})
    return {'status': 'hair_fur_snow_connected', 'controls': controls.name, 'weather_source': source_record,
        'bindings': bindings, 'skipped': skipped, 'projection_attributes': attributes, 'hair_clock': hair_clock,
        'packed_texture': image.name, 'texture_sha256': profile.texture_sha256,
        'source_shader_identities': [source.shader_identity for source in profile.families],
        'profile_path': str(profile.path), 'profile_sha256': file_digest(profile.path),
        'source_files': [{'path': str(path), 'sha256': file_digest(path)} for path in (profile.path, profile.manifest, profile.texture)],
        'curves_scope': 'Independent sampled-color/PBR Curves preview materials are unchanged and do not consume this mesh snow layer.',
        'native_projection_verified': False, 'native_runtime_state_verified': False}


def verify_profile_hair_fur_snow(receipt: dict[str, object]) -> dict[str, object]:
    status = receipt.get('status')
    if status in ('not_configured', 'no_matching_hair_fur_materials'):
        return {'status': status}
    if status != 'hair_fur_snow_connected':
        raise ValueError('Unknown saved Hair/Fur snow status: ' + str(status))
    controls = bpy.data.node_groups.get(_string(receipt.get('controls'), 'Hair/Fur controls'))
    if controls is None:
        raise AssertionError('Saved shared Hair/Fur weather controls are missing')
    verify_weather_controls(controls, _object(receipt.get('weather_source'), 'Hair/Fur weather source'))
    profile_path = Path(_string(receipt.get('profile_path'), 'Hair/Fur profile path')).resolve(strict=True)
    if file_digest(profile_path) != _string(receipt.get('profile_sha256'), 'Hair/Fur profile SHA-256'):
        raise AssertionError(f'Saved Hair/Fur profile changed after build: {profile_path}')
    identity_rows = receipt.get('source_shader_identities')
    if not isinstance(identity_rows, list) or not identity_rows:
        raise TypeError('Saved Hair/Fur Shader identities must be a nonempty array')
    expected_identities = tuple(profile_shader_identity(profile_path, _string(_object(row, 'Hair/Fur Shader identity').get('name'), 'Shader family'))
                                for row in identity_rows)
    if tuple(identity_rows) != expected_identities or len({row['name'] for row in expected_identities}) != len(expected_identities):
        raise AssertionError('Saved Hair/Fur Shader identities differ from the per-export profile')
    image = bpy.data.images.get(_string(receipt.get('packed_texture'), 'Hair/Fur snow texture'))
    if image is None or image.packed_file is None or file_digest_bytes(image.packed_file.data) != receipt.get('texture_sha256') or image.colorspace_settings.name != 'Non-Color' or image.alpha_mode != 'CHANNEL_PACKED':
        raise AssertionError('Saved Hair/Fur source snow texture changed or is not packed linear data')
    links_checked = 0
    for raw in receipt['bindings']:
        row = _object(raw, 'Hair/Fur binding')
        tree = bpy.data.node_groups.get(_string(row.get('group'), 'Hair/Fur group'))
        if tree is None or tree.get('source_identity') != row['source_identity'] or tree.get('source_shader') != row['family'] or list(source_keywords(tree)) != row['keywords']:
            raise AssertionError('Saved Hair/Fur source material identity or keywords differ')
        instance = tree.nodes.get(row['instance'])
        state = tree.nodes.get(row['state'])
        if instance is None or state is None or state.node_tree != controls or instance.node_tree is None or instance.node_tree.name != row['kernel'] or instance.node_tree.get('source_shader_sha256') != row['shader_sha256'] or instance.node_tree.get('snow_family') != ('Hair' if row['family'] == 'HGRP/CharacterNPR_Hair' else 'Fur'):
            raise AssertionError('Saved Hair/Fur snow kernel or shared controls differ')
        if linked_source(instance.inputs['Snow Intensity']) != state.outputs['Native Snow Intensity']:
            raise AssertionError('Saved Hair/Fur snow intensity is disconnected')
        normal_nodes = tuple(item for item in tree.nodes if item.label == 'Native weather normal to HGRP surface frame result')
        if len(normal_nodes) != 1:
            raise AssertionError('Saved Hair/Fur final normal conversion is ambiguous')
        for target_raw in row['targets']:
            target = _object(target_raw, 'Hair/Fur live target')
            consumer = tree.nodes.get(target['node'])
            if consumer is None:
                raise AssertionError('Saved Hair/Fur live consumer is missing')
            expected = normal_nodes[0].outputs[0] if target['channel'] == 'Normal' else instance.outputs[target['channel']]
            if linked_source(consumer.inputs[target['socket_index']]) != expected:
                raise AssertionError('Saved Hair/Fur snow is disconnected from live consumer: ' + tree.name + '/' + consumer.label)
            links_checked += 1
        for label in ('Projection Position', 'Projection Normal'):
            projection_node = linked_source(instance.inputs[label]).node
            if projection_node.type != 'ATTRIBUTE' or not projection_node.attribute_name:
                raise AssertionError('Saved Hair/Fur explicit projection is disconnected')
        if linked_source(instance.inputs['Metallic Gloss B']) != by_label(tree, 'R metallic / G specular / B AO / A smoothness').outputs['Blue']:
            raise AssertionError('Saved mapped Hair/Fur packed B mask differs')
        if row['family'] == 'HGRP/CharacterNPR_Hair' and row.get('wet_before_snow') is not None:
            from character_hair_wet_profile import verify_hair_wet_before_snow
            verify_hair_wet_before_snow(tree, _object(row['wet_before_snow'], 'Hair wet binding'))
        if row['family'] == 'HGRP/CharacterNPR':
            if row.get('wet_before_snow') is not None:
                from character_fur_wet_profile import verify_fur_wet_before_snow
                verify_fur_wet_before_snow(tree, _object(row['wet_before_snow'], 'Fur wet binding'))
            base = tuple(item for item in tree.nodes if item.type == 'TEX_IMAGE' and item.get('source_slot') == '_BaseMap')
            if len(base) != 1 or linked_source(instance.inputs['UV']) != linked_source(base[0].inputs['Vector']) or linked_source(instance.inputs['Metallic Gloss B']) != by_label(tree, 'R metallic / G specular / B AO / A smoothness').outputs['Blue']:
                raise AssertionError('Saved Fur source UV or packed B mask differs')
    for raw in receipt['source_files']:
        row = _object(raw, 'Hair/Fur source fingerprint')
        if file_digest(Path(_string(row.get('path'), 'Hair/Fur source path'))) != row.get('sha256'):
            raise AssertionError('Saved Hair/Fur source file changed: ' + str(row['path']))
    return {'status': 'passed', 'groups': len(receipt['bindings']), 'actual_live_links_checked': links_checked,
        'shared_controls': controls.name, 'curves_unchanged': True, 'native_runtime_state_verified': False}


def file_digest_bytes(value: bytes) -> str:
    import hashlib
    return hashlib.sha256(value).hexdigest()
