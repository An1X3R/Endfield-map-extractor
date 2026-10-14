"""Verify material persistence follows linked scene objects, not orphan startup meshes."""
from __future__ import annotations

from pathlib import Path
import sys

import bpy

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'src' / 'character_runtime'))

from audit_character_fbx import clear_scene_objects
from character_authoring_validation import material_signature_details


def owner_rows(details: dict[str, object], owner: str, field: str) -> tuple[dict[str, object], ...]:
    rows = details[field]
    if not isinstance(rows, list):
        raise TypeError(f'Expected material signature rows: {field}')
    return tuple(row for row in rows if row['owner'] == owner)


clear_scene_objects()
material = bpy.data.materials['Material']
mesh = bpy.data.meshes['Cube']
assert mesh.users == 0 and material.users != 0
group = bpy.data.node_groups.new('Orphan Material Group', 'ShaderNodeTree')
group_node = material.node_tree.nodes.new('ShaderNodeGroup')
group_node.node_tree = group
signature = material_signature_details()
assert not owner_rows(signature, 'material:Material', 'trees')
assert len(owner_rows(signature, 'material:Material', 'excluded_materials')) == 1
assert not owner_rows(signature, 'node_group:Orphan Material Group', 'trees')
assert len(owner_rows(signature, 'node_group:Orphan Material Group', 'excluded_node_groups')) == 1

object_value = bpy.data.objects.new('Persistent Material Owner', mesh)
bpy.context.scene.collection.objects.link(object_value)
signature = material_signature_details()
assert len(owner_rows(signature, 'material:Material', 'trees')) == 1
assert not owner_rows(signature, 'material:Material', 'excluded_materials')
assert len(owner_rows(signature, 'node_group:Orphan Material Group', 'trees')) == 1
assert not owner_rows(signature, 'node_group:Orphan Material Group', 'excluded_node_groups')

instanced_material = bpy.data.materials.new('Collection Instance Material')
instanced_material.use_nodes = True
instanced_group = bpy.data.node_groups.new('Collection Instance Group', 'ShaderNodeTree')
instanced_group_node = instanced_material.node_tree.nodes.new('ShaderNodeGroup')
instanced_group_node.node_tree = instanced_group
instanced_mesh = bpy.data.meshes.new('Collection Instance Mesh')
instanced_mesh.materials.append(instanced_material)
instanced_content = bpy.data.collections.new('Collection Instance Content')
instanced_object = bpy.data.objects.new('Collection Instance Content Object', instanced_mesh)
instanced_content.objects.link(instanced_object)
instance = bpy.data.objects.new('Collection Instance', None)
instance.instance_type = 'COLLECTION'
instance.instance_collection = instanced_content
bpy.context.scene.collection.objects.link(instance)
signature = material_signature_details()
assert len(owner_rows(signature, 'material:Collection Instance Material', 'trees')) == 1
assert len(owner_rows(signature, 'node_group:Collection Instance Group', 'trees')) == 1

assert signature['sha256']
print('material persistence regression passed')
