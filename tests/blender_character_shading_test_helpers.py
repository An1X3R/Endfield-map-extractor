"""Refresh real packaged test materials and inspect their final output dependencies."""
from __future__ import annotations

from collections.abc import Sequence

import bpy

from blender_scene_materials import layout_shader_node_tree
from character_material_authoring import (
    expose_source_instance, relocated_source_output, shader_dependencies,
    verify_material_authoring, MaterialPackagingRow,
)


def materials_for_source(name: str) -> tuple[bpy.types.Material, ...]:
    """Resolve visible real mesh-slot materials using the exact archived source tree."""
    materials = {slot.material for obj in bpy.context.scene.objects if obj.type == 'MESH' and not obj.hide_render
                 for slot in obj.material_slots if slot.material is not None and slot.material.use_nodes}
    matches = tuple(material for material in materials if any(
        item.type == 'GROUP' and item.node_tree.name == name and item.get('endfield_authoring_parameters')
        for item in material.node_tree.nodes))
    if not matches:
        raise AssertionError(f'No visible packaged material owns the test source: {name}')
    return matches


def refresh_materials(materials: Sequence[bpy.types.Material]) -> None:
    """Repackage real source instances after patching, preserving outer image editing."""
    rows: list[MaterialPackagingRow] = []
    for material in materials:
        instances = tuple(item for item in material.node_tree.nodes if item.type == 'GROUP'
                          and item.get('endfield_authoring_parameters'))
        for instance in instances:
            for label in ('Shader', 'Coverage', 'Multiply Factor'):
                output = instance.outputs[label]
                relocated = relocated_source_output(instance, output)
                for destination in tuple(link.to_socket for link in relocated.links):
                    material.node_tree.links.new(output, destination)
            rows.append(expose_source_instance(material, instance, layout_shader_node_tree))
    verify_material_authoring({'format': 'EndfieldMaterialAuthoring/1', 'materials': rows,
                               'scope': 'Real Material Output shading regression after archived-source patching.'})


def active_labels(material: bpy.types.Material) -> frozenset[str]:
    """Follow exact group sockets from the actual active Material Output."""
    dependencies = shader_dependencies(material)
    return frozenset(item.label for tree in (material.node_tree, *bpy.data.node_groups)
                     for item in tree.nodes
                     if (tree.as_pointer(), item.as_pointer()) in dependencies.nodes)
