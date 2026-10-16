"""Compose compatible ordered source draws without duplicating scene geometry."""
from __future__ import annotations

import json

import bpy

from blender_scene_materials import layout_shader_node_trees
from endfield_scene_material_contract import MaterialRecord, reference_key
from endfield_scene_slot_contract import validate_ordered_surface_passes


def _surface_group(material: bpy.types.Material, record: MaterialRecord) -> bpy.types.NodeTree:
    """Use Blender's native grouping so every source node RNA value is retained."""
    temporary = material.copy()
    try:
        return _group_copied_surface(temporary, record)
    finally:
        bpy.data.materials.remove(temporary)


def _group_copied_surface(material: bpy.types.Material, record: MaterialRecord) -> bpy.types.NodeTree:
    """Group an owned copy without changing the shared source material cache."""
    tree = material.node_tree
    outputs = [node for node in tree.nodes if node.type == "OUTPUT_MATERIAL"]
    if len(outputs) != 1 or not outputs[0].inputs["Surface"].is_linked:
        raise ValueError(f"Ordered opaque surface requires one connected output: material={material.name}")
    output = outputs[0]
    source = output.inputs["Surface"].links[0].from_node
    if source.type == "GROUP" and source.node_tree.get("endfield_ordered_pass_source"):
        return source.node_tree
    screen = bpy.context.screen
    if screen is None or not screen.areas:
        raise RuntimeError("Ordered surface grouping requires the standard Blender startup screen")
    area = screen.areas[0]
    original_type = area.type
    area.type = "NODE_EDITOR"
    try:
        space = area.spaces.active
        space.tree_type = "ShaderNodeTree"
        space.pin = True
        space.path.start(tree)
        for node in tree.nodes:
            node.select = node != output
        tree.nodes.active = source
        region = next(region for region in area.regions if region.type == "WINDOW")
        with bpy.context.temp_override(area=area, region=region):
            bpy.ops.node.group_make()
        instance = output.inputs["Surface"].links[0].from_node
        if instance.type != "GROUP":
            raise RuntimeError(f"Native source grouping did not produce a group: material={material.name}")
        group = instance.node_tree
        group.name = "EF Source Surface | " + record["Name"]
        group["endfield_ordered_pass_source"] = json.dumps(record)
        return group
    finally:
        area.type = original_type


def create_ordered_material(records: tuple[MaterialRecord, ...],
                            surfaces: tuple[bpy.types.Material, ...]) -> bpy.types.Material:
    """Retain serialized opaque overwrite order as an explicit host surface graph."""
    if not records or len(records) != len(surfaces):
        raise ValueError("Ordered material composition requires one surface per source record")
    validate_ordered_surface_passes(records)
    if len({reference_key(record) for record in records}) == 1:
        return surfaces[0]
    groups = tuple(_surface_group(surface, record) for surface, record in zip(surfaces, records, strict=True))
    material = bpy.data.materials.new("Ordered Source Passes | " + records[0]["Name"])
    material.use_nodes = True
    tree = material.node_tree
    tree.nodes.clear()
    output = tree.nodes.new("ShaderNodeOutputMaterial")
    previous: bpy.types.NodeSocket | None = None
    for index, group in enumerate(groups):
        instance = tree.nodes.new("ShaderNodeGroup")
        instance.node_tree = group
        instance.name = f"Source Material Pass {index}"
        instance.label = records[index]["Name"]
        shader_outputs = [socket for socket in instance.outputs if socket.type == "SHADER"]
        if len(shader_outputs) != 1:
            raise ValueError(f"Source material group needs one shader output: material={records[index]['Name']}")
        current = shader_outputs[0]
        if previous is not None:
            enabled = tree.nodes.new("ShaderNodeValue")
            enabled.name = f"Source Opaque Pass {index} Enable"
            enabled.label = "Opaque source coverage = 1 | host overwrite"
            enabled.outputs[0].default_value = 1.0
            mix = tree.nodes.new("ShaderNodeMixShader")
            mix.name = f"Ordered Source Overwrite {index}"
            tree.links.new(enabled.outputs[0], mix.inputs[0])
            tree.links.new(previous, mix.inputs[1])
            tree.links.new(current, mix.inputs[2])
            current = mix.outputs[0]
        previous = current
    tree.links.new(previous, output.inputs["Surface"])
    material["endfield_ordered_material_refs"] = json.dumps([reference_key(record) for record in records])
    material["endfield_ordered_effective_surface"] = surfaces[-1]
    material["endfield_surface_contract"] = "EndfieldOrderedOpaqueSurface/1"
    material["endfield_surface_fidelity"] = "source_order_opaque_overwrite_host_approximation"
    material["endfield_render_pipeline_status"] = "Compatible opaque depth/cull/queue states validated; native depth, stencil, render scheduling and shader program equivalence are not reconstructed"
    layout_shader_node_trees((tree,))
    return material


def effective_opaque_surface(material: bpy.types.Material) -> bpy.types.Material:
    """Resolve a validated coverage-one overwrite to its retained source surface."""
    if "endfield_ordered_material_refs" not in material:
        return material
    verify_ordered_material(material)
    return material["endfield_ordered_effective_surface"]


def verify_ordered_material(material: bpy.types.Material) -> dict[str, object]:
    """Verify the live overwrite chain after saving, including original source groups."""
    references = json.loads(material["endfield_ordered_material_refs"])
    tree = material.node_tree
    output = tree.get_output_node("ALL")
    if output is None or not output.inputs["Surface"].is_linked:
        raise ValueError(f"Ordered surface output is disconnected: material={material.name}")
    current = output.inputs["Surface"].links[0].from_node
    for index in reversed(range(len(references))):
        if index:
            if current.type != "MIX_SHADER" or current.name != f"Ordered Source Overwrite {index}":
                raise ValueError(f"Ordered surface pass chain changed: material={material.name}, pass={index}")
            if any(not socket.is_linked for socket in current.inputs):
                raise ValueError(f"Ordered surface pass is disconnected: material={material.name}, pass={index}")
            factor = current.inputs[0]
            if (not factor.is_linked or factor.links[0].from_node.type != "VALUE"
                    or factor.links[0].from_socket.default_value != 1):
                raise ValueError(f"Ordered opaque source coverage changed: material={material.name}, pass={index}")
            group = current.inputs[2].links[0].from_node
            previous = current.inputs[1].links[0].from_node
        else:
            group, previous = current, None
        if (group.type != "GROUP" or group.name != f"Source Material Pass {index}"
                or list(reference_key(json.loads(group.node_tree["endfield_ordered_pass_source"]))) != references[index]):
            raise ValueError(f"Ordered source material identity changed: material={material.name}, pass={index}")
        current = previous
    effective = material.get("endfield_ordered_effective_surface")
    expected_source, expected_path_id = references[-1]
    if (not isinstance(effective, bpy.types.Material)
            or effective.get("endfield_material_source", "").lower() != f"{expected_source}:{expected_path_id}".lower()
            or "endfield_ordered_material_refs" in effective):
        raise ValueError(f"Ordered effective source surface changed: material={material.name}")
    return {"material": material.name, "source_material_refs": references,
            "effective_source_material": effective.name,
            "status": "verified_source_order_opaque_overwrite_host_approximation",
            "geometry_duplicated": False}
