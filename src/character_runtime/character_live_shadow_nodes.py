"""Build reusable live Sun-ray shadow attributes for a receiver mesh."""
from __future__ import annotations

from math import isfinite

import bpy
from mathutils import Vector


MAIN_SHADOW_ATTRIBUTE = "ef_hgrp_shadow_r"
CHARACTER_SHADOW_ATTRIBUTE = "ef_hgrp_shadow_g"


def _finite_number(value: object, name: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError(f"{name} must be a numeric value, got {type(value).__name__}")
    result = float(value)
    if not isfinite(result):
        raise ValueError(f"{name} must be finite, got {result}")
    return result


def _registered_collection(collection: bpy.types.Collection, name: str) -> None:
    if not isinstance(collection, bpy.types.Collection):
        raise TypeError(f"{name} must be an existing bpy.types.Collection")
    registered = bpy.data.collections.get(collection.name)
    if registered is None or registered.as_pointer() != collection.as_pointer():
        raise ValueError(f"{name} is not registered in bpy.data.collections")


def _socket_value(
    tree: bpy.types.NodeTree,
    destination: bpy.types.NodeSocket,
    value: bpy.types.NodeSocket | float,
) -> None:
    if isinstance(value, bpy.types.NodeSocket):
        tree.links.new(value, destination)
    else:
        destination.default_value = value


def _node(
    tree: bpy.types.NodeTree,
    identifier: str,
    label: str,
    x: float,
    y: float,
) -> bpy.types.Node:
    if len(label) > 63:
        raise ValueError(f"Node label exceeds Blender's 63-character limit: {label}")
    result = tree.nodes.new(identifier)
    result.label = label
    result.location = (x, y)
    return result


def _vector_math(
    tree: bpy.types.NodeTree,
    operation: str,
    first: bpy.types.NodeSocket,
    second: bpy.types.NodeSocket | float | None,
    label: str,
    x: float,
    y: float,
) -> bpy.types.NodeSocket:
    result = _node(tree, "ShaderNodeVectorMath", label, x, y)
    result.operation = operation

    if operation == "SCALE":
        if second is None:
            raise ValueError("SCALE requires both a vector and a scalar input")
        _socket_value(tree, result.inputs["Vector"], first)
        _socket_value(tree, result.inputs["Scale"], second)
    elif operation == "ADD":
        if second is None:
            raise ValueError("ADD requires two vector inputs")
        _socket_value(tree, result.inputs[0], first)
        _socket_value(tree, result.inputs[1], second)
    elif operation in {"LENGTH", "NORMALIZE"}:
        if second is not None:
            raise ValueError(f"{operation} accepts exactly one vector input")
        _socket_value(tree, result.inputs[0], first)
    else:
        raise ValueError(f"Unsupported vector math operation: {operation}")

    output_name = "Value" if operation == "LENGTH" else "Vector"
    return result.outputs[output_name]


def _scalar_math(
    tree: bpy.types.NodeTree,
    operation: str,
    first: bpy.types.NodeSocket | float,
    second: bpy.types.NodeSocket | float,
    label: str,
    x: float,
    y: float,
) -> bpy.types.NodeSocket:
    result = _node(tree, "ShaderNodeMath", label, x, y)
    result.operation = operation
    _socket_value(tree, result.inputs[0], first)
    _socket_value(tree, result.inputs[1], second)
    return result.outputs["Value"]


def _shadow_branch(
    tree: bpy.types.NodeTree,
    collection: bpy.types.Collection,
    collection_label: str,
    attribute_name: str,
    input_geometry: bpy.types.NodeSocket,
    source_position: bpy.types.NodeSocket,
    ray_direction: bpy.types.NodeSocket,
    local_ray_length: bpy.types.NodeSocket,
    y: float,
) -> bpy.types.NodeSocket:
    collection_info = _node(
        tree,
        "GeometryNodeCollectionInfo",
        collection_label,
        -220.0,
        y,
    )
    collection_info.transform_space = "RELATIVE"
    collection_info.inputs["Collection"].default_value = collection

    realize = _node(tree, "GeometryNodeRealizeInstances", "Realize caster instances", 20.0, y)
    realize.inputs["Realize All"].default_value = True
    tree.links.new(collection_info.outputs["Instances"], realize.inputs["Geometry"])

    raycast = _node(tree, "GeometryNodeRaycast", "Cast toward current Sun", 280.0, y)
    raycast.data_type = "FLOAT"
    raycast.inputs["Ray Length"].default_value = 1.0
    tree.links.new(realize.outputs["Geometry"], raycast.inputs["Target Geometry"])
    tree.links.new(source_position, raycast.inputs["Source Position"])
    tree.links.new(ray_direction, raycast.inputs["Ray Direction"])
    tree.links.new(local_ray_length, raycast.inputs["Ray Length"])

    unoccluded = _node(tree, "GeometryNodeSwitch", "1 minus Is Hit", 540.0, y)
    unoccluded.input_type = "FLOAT"
    unoccluded.inputs["False"].default_value = 1.0
    unoccluded.inputs["True"].default_value = 0.0
    tree.links.new(raycast.outputs["Is Hit"], unoccluded.inputs["Switch"])

    store = _node(tree, "GeometryNodeStoreNamedAttribute", f"Store {attribute_name}", 800.0, y)
    store.data_type = "FLOAT"
    store.domain = "POINT"
    store.inputs["Name"].default_value = attribute_name
    tree.links.new(input_geometry, store.inputs["Geometry"])
    tree.links.new(unoccluded.outputs["Output"], store.inputs["Value"])
    return store.outputs["Geometry"]


def build_sun_shadow_nodes(
    name: str,
    light: bpy.types.Object,
    main_caster_collection: bpy.types.Collection,
    character_caster_collection: bpy.types.Collection,
    bias_meters: float,
    ray_length_meters: float,
    subdivision_level: int,
) -> bpy.types.NodeTree:
    """Create live hard-shadow attributes in the receiver object's local space.

    Both caster collections must exist as Blender datablocks but may be empty.
    The receiver object must have a nonsingular transform and the Sun's world
    +Z axis must remain nonzero. Ray distances and bias are supplied in world
    units, then converted to receiver-local distances in the node graph.
    """
    if not isinstance(name, str) or not name.strip():
        raise ValueError("name must be a non-empty string")
    if len(name) > 63:
        raise ValueError("name must not exceed Blender's 63-character node-tree limit")
    if not isinstance(light, bpy.types.Object):
        raise TypeError("light must be a Blender object containing a Sun light")
    registered_light = bpy.data.objects.get(light.name)
    if registered_light is None or registered_light.as_pointer() != light.as_pointer():
        raise ValueError("light is not registered in bpy.data.objects")
    if not isinstance(light.data, bpy.types.Light) or light.data.type != "SUN":
        raise ValueError(f"{light.name} must use a Sun light datablock")
    _registered_collection(main_caster_collection, "main_caster_collection")
    _registered_collection(character_caster_collection, "character_caster_collection")

    bias = _finite_number(bias_meters, "bias_meters")
    ray_length = _finite_number(ray_length_meters, "ray_length_meters")
    if bias < 0.0:
        raise ValueError(f"bias_meters must be non-negative, got {bias}")
    if ray_length <= 0.0:
        raise ValueError(f"ray_length_meters must be positive, got {ray_length}")
    if isinstance(subdivision_level, bool) or not isinstance(subdivision_level, int):
        raise ValueError("subdivision_level must be an integer from 0 through 2")
    if subdivision_level < 0 or subdivision_level > 2:
        raise ValueError(f"subdivision_level must be from 0 through 2, got {subdivision_level}")
    world_sun_axis = light.matrix_world.to_3x3() @ Vector((0.0, 0.0, 1.0))
    if not all(isfinite(component) for component in world_sun_axis) or world_sun_axis.length <= 1e-12:
        raise ValueError(f"{light.name} has a zero or invalid world +Z axis")

    tree = bpy.data.node_groups.new(name, "GeometryNodeTree")
    tree.interface.new_socket(name="Geometry", in_out="INPUT", socket_type="NodeSocketGeometry")
    tree.interface.new_socket(name="Geometry", in_out="OUTPUT", socket_type="NodeSocketGeometry")

    group_input = _node(tree, "NodeGroupInput", "Receiver geometry", -1500.0, 460.0)
    group_output = _node(tree, "NodeGroupOutput", "Receiver with shadow fields", 1100.0, 460.0)

    subdivide = _node(tree, "GeometryNodeSubdivideMesh", "Optional receiver subdivision", -1240.0, 460.0)
    subdivide.inputs["Level"].default_value = subdivision_level
    tree.links.new(group_input.outputs["Geometry"], subdivide.inputs["Mesh"])

    relative_sun = _node(tree, "GeometryNodeObjectInfo", "Sun transform relative to receiver", -1500.0, 40.0)
    relative_sun.transform_space = "RELATIVE"
    relative_sun.inputs["Object"].default_value = light
    local_axis = _node(tree, "FunctionNodeTransformDirection", "Sun +Z in receiver coordinates", -1230.0, 40.0)
    local_axis.inputs["Direction"].default_value = (0.0, 0.0, 1.0)
    tree.links.new(relative_sun.outputs["Transform"], local_axis.inputs["Transform"])

    original_sun = _node(tree, "GeometryNodeObjectInfo", "Sun transform in world coordinates", -1500.0, -300.0)
    original_sun.transform_space = "ORIGINAL"
    original_sun.inputs["Object"].default_value = light
    world_axis = _node(tree, "FunctionNodeTransformDirection", "Sun +Z in world coordinates", -1230.0, -300.0)
    world_axis.inputs["Direction"].default_value = (0.0, 0.0, 1.0)
    tree.links.new(original_sun.outputs["Transform"], world_axis.inputs["Transform"])

    local_axis_length = _vector_math(tree, "LENGTH", local_axis.outputs["Direction"], None, "Receiver-local Sun axis length", -960.0, 40.0)
    world_axis_length = _vector_math(tree, "LENGTH", world_axis.outputs["Direction"], None, "World Sun axis length", -960.0, -300.0)
    unit_conversion = _scalar_math(tree, "DIVIDE", local_axis_length, world_axis_length, "World-to-local distance ratio", -700.0, -110.0)
    local_direction = _vector_math(tree, "NORMALIZE", local_axis.outputs["Direction"], None, "Normalized local ray direction", -700.0, 120.0)
    local_bias = _scalar_math(tree, "MULTIPLY", unit_conversion, bias, "Receiver-local bias distance", -450.0, -80.0)
    local_range = _scalar_math(tree, "MULTIPLY", unit_conversion, ray_length, "Receiver-local ray length", -450.0, -280.0)
    bias_offset = _vector_math(tree, "SCALE", local_direction, local_bias, "Offset source along ray", -200.0, 120.0)

    position = _node(tree, "GeometryNodeInputPosition", "Receiver-local position", -450.0, 520.0)
    source_position = _vector_math(tree, "ADD", position.outputs["Position"], bias_offset, "Biased source position", 40.0, 440.0)

    main_output = _shadow_branch(
        tree,
        main_caster_collection,
        "Main environment casters",
        MAIN_SHADOW_ATTRIBUTE,
        subdivide.outputs["Mesh"],
        source_position,
        local_direction,
        local_range,
        -140.0,
    )
    character_output = _shadow_branch(
        tree,
        character_caster_collection,
        "Character casters",
        CHARACTER_SHADOW_ATTRIBUTE,
        main_output,
        source_position,
        local_direction,
        local_range,
        -760.0,
    )
    tree.links.new(character_output, group_output.inputs["Geometry"])

    tree["shadow_attributes"] = f"{MAIN_SHADOW_ATTRIBUTE},{CHARACTER_SHADOW_ATTRIBUTE}"
    tree["distance_space"] = "World units converted to receiver-local units using relative/original Sun axis lengths"
    tree["bias_meters"] = bias
    tree["ray_length_meters"] = ray_length
    tree["sun_object"] = light.name
    tree["main_caster_collection"] = main_caster_collection.name
    tree["character_caster_collection"] = character_caster_collection.name
    tree["shadow_sampling"] = "Single hard ray per receiver point; no PCF"
    tree["subdivision_level"] = subdivision_level
    return tree
