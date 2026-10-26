"""Verify fresh real Hair shares diffuse cast visibility through packaged output."""
# ruff: noqa: E402
from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys

import bpy

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT / 'src'), str(ROOT / 'src' / 'character_runtime'), str(ROOT / 'tests')]

from attach_character_live_shadows import bind_shadow_attributes
from blender_character_shading_test_helpers import active_labels, materials_for_source, refresh_materials
from blender_scene_materials import layout_shader_node_tree
from character_hair_lighting import (
    HairShaderFrame, apply_hair_specular_branch, bind_source_line_map,
    hair_cast_shadow_source, line_map_binding_from_material, source_parameters,
)
from character_live_shadow_nodes import CHARACTER_SHADOW_ATTRIBUTE, MAIN_SHADOW_ATTRIBUTE
from character_material_authoring import shader_dependencies
from patch_character_hgrp import attach_diffuse, by_label
from rebuild_character_materials import make_environment, material_group, read_packet, texture_colorspace


def main(blend: Path, prepared: Path, selection: Path, output: Path) -> None:
    """Rebuild one actual Hair source and verify its active mesh-slot output."""
    bpy.ops.wm.open_mainfile(filepath=str(blend.resolve(strict=True)), use_scripts=False)
    old = next(tree for tree in bpy.data.node_groups
               if tree.get('source_shader') == 'HGRP/CharacterNPR_Hair' and tree.get('hair_lighting_patch'))
    materials = materials_for_source(old.name)
    packet = read_packet(prepared / 'static_probe', prepared / 'shader_identity/shader_identity.json',
                         selection, prepared / 'renderer_visibility.json')
    identity = tuple(json.loads(old['source_identity']))
    sources = tuple(source for source in packet.materials if source.identity == identity)
    if len(sources) != 1:
        raise AssertionError(f'Real source packet has no unique tested Hair material: {identity}')
    source = sources[0]
    images: dict[tuple[tuple[str, int], str, str], bpy.types.Image] = {}
    for binding in source.textures:
        samples = tuple(node for node in old.nodes if node.type == 'TEX_IMAGE'
                        and node.get('source_slot') == binding.name)
        if len(samples) != 1 or samples[0].image is None or samples[0].image.packed_file is None:
            raise AssertionError(f'Real Hair source texture missing or unpacked: {binding.name}')
        texture = packet.textures[binding.identity]
        images[(texture.identity, texture_colorspace(binding, texture), 'CHANNEL_PACKED')] = samples[0].image
    lighting = bpy.data.node_groups['EF Character Artist Lighting - APPROXIMATION']
    tree = material_group(source, packet, make_environment(layout_shader_node_tree), images,
                          lighting, layout_shader_node_tree, None)
    shared = by_label(old, 'Explicit HGRP state and preview inputs').node_tree
    diffuse = by_label(old, 'Verified local HGRP diffuse equations').node_tree
    attach_diffuse(tree, shared, diffuse)
    frame = HairShaderFrame(**json.loads(old['hair_lighting_shader_frame']))
    binding = line_map_binding_from_material(json.loads(source.tree_json))
    bind_source_line_map(tree, binding)
    receipt = apply_hair_specular_branch(tree, lighting, source_parameters(tree, frame, binding))
    if receipt.cast_shadow_preview is not None or not receipt.cast_shadow_binding:
        raise AssertionError('Fresh Hair receipt still claims a hardcoded cast preview')
    controls = by_label(tree, 'Explicit HGRP state and preview inputs')
    if hair_cast_shadow_source(tree) != controls.outputs['Cast Shadow']:
        raise AssertionError('Fresh Hair does not share its diffuse cast-visibility producer')
    bind_shadow_attributes(tree)
    live = hair_cast_shadow_source(tree)
    if live.node.type != 'ATTRIBUTE' or live.node.attribute_name != MAIN_SHADOW_ATTRIBUTE:
        raise AssertionError('Existing automatic shadow bridge did not reach Hair diffuse')
    consumers = (('HGRP inverse cast-shadow preview', 1), ('HGRP cast-shadow light Y', 1),
                 ('HGRP adjusted light direction times cast shadow', 3))
    active_consumer_labels: set[str] = set()
    for consumer_index, (label, index) in enumerate(consumers):
        socket = by_label(tree, label).inputs[index]
        if len(socket.links) != 1 or socket.links[0].from_socket != live:
            raise AssertionError(f'Fresh Hair strand cast consumer bypasses live visibility: {label}')
        marker = 'Regression fresh Hair cast consumer ' + str(consumer_index)
        socket.node.label = marker
        active_consumer_labels.add(marker)
    for material in materials:
        instances = tuple(node for node in material.node_tree.nodes if node.type == 'GROUP'
                          and node.node_tree == old and node.get('endfield_authoring_parameters'))
        if len(instances) != 1:
            raise AssertionError(f'Real Hair authoring instance missing or ambiguous: {material.name}')
        instances[0].node_tree = tree
    refresh_materials(materials)
    for material in materials:
        labels = active_labels(material)
        if not active_consumer_labels <= labels or 'Live ' + MAIN_SHADOW_ATTRIBUTE not in labels:
            raise AssertionError(f'Actual Material Output lost the Hair cast visibility bridge: {material.name}')
        dependencies = shader_dependencies(material)
        legacy = tuple(group.name for group in bpy.data.node_groups for node in group.nodes
                       if node.label == 'Hair HGRP Cast Shadow Preview - Explicit'
                       and (group.as_pointer(), node.as_pointer()) in dependencies.nodes)
        if any(tree.name + ' / Stage ' in name for name in legacy):
            raise AssertionError(f'Fresh tested Hair still consumes fixed visibility: {material.name}/{legacy}')
    fields: list[dict[str, object]] = []
    graph = bpy.context.evaluated_depsgraph_get()
    meshes = tuple(obj for obj in bpy.context.scene.objects if obj.type == 'MESH' and not obj.hide_render
                   and any(slot.material in materials for slot in obj.material_slots))
    if not meshes:
        raise AssertionError('Real Hair source has no visible receiver meshes')
    for obj in meshes:
        evaluated = obj.evaluated_get(graph)
        mesh = evaluated.to_mesh()
        for name in (MAIN_SHADOW_ATTRIBUTE, CHARACTER_SHADOW_ATTRIBUTE):
            attribute = mesh.attributes.get(name)
            if attribute is None or len(attribute.data) == 0:
                raise AssertionError(f'Real Hair automatic visibility field absent: {obj.name}/{name}')
            values = tuple(float(item.value) for item in attribute.data)
            fields.append({'mesh': obj.name, 'attribute': name, 'minimum': min(values),
                           'maximum': max(values), 'occluded_vertices': sum(value < .5 for value in values),
                           'vertices': len(values)})
        evaluated.to_mesh_clear()
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps({'status': 'passed', 'source': source.name,
                                 'active_materials': [material.name for material in materials],
                                 'live_cast_consumers_verified': len(consumers),
                                 'automatic_hair_shadow_fields': fields,
                                 'native_runtime_atlas_reconstructed': False}, indent=2), encoding='utf-8')
    print(json.dumps({'status': 'passed', 'audit': str(output), 'fields': fields}))


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    for name in ('blend', 'prepared', 'selection', 'output'):
        parser.add_argument('--' + name, type=Path, required=True)
    args = parser.parse_args(sys.argv[sys.argv.index('--') + 1:])
    main(args.blend, args.prepared, args.selection, args.output)
