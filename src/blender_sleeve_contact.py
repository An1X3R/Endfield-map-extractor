"""Opt-in editable post-armature contact approximation for explicitly selected cloth weights."""
from __future__ import annotations

import json
import math
from typing import NamedTuple

import bpy
from mathutils import Vector


class ContactCapsule(NamedTuple):
    name: str
    bone: str
    start: tuple[float, float, float]
    end: tuple[float, float, float]
    radius: float
    source_groups: tuple[str, ...]


class ContactSettings(NamedTuple):
    margin: float
    maximum_displacement: float
    full_weight: float
    strength: float


class ContactFrame(NamedTuple):
    frame: int
    capsule: str
    radius: float
    strength: float


def _vector(tree: bpy.types.NodeTree, operation: str, left: bpy.types.NodeSocket,
            right: bpy.types.NodeSocket, name: str) -> bpy.types.NodeSocket:
    node = tree.nodes.new("ShaderNodeVectorMath")
    node.operation = operation
    node.label = name
    tree.links.new(left, node.inputs[0])
    tree.links.new(right, node.inputs[1])
    return node.outputs["Value"] if operation == "DOT_PRODUCT" else node.outputs["Vector"]


def _math(tree: bpy.types.NodeTree, operation: str, left: bpy.types.NodeSocket | float,
          right: bpy.types.NodeSocket | float, name: str) -> bpy.types.NodeSocket:
    node = tree.nodes.new("ShaderNodeMath")
    node.operation = operation
    node.label = name
    for index, value in enumerate((left, right)):
        if isinstance(value, (float, int)):
            node.inputs[index].default_value = value
        else:
            tree.links.new(value, node.inputs[index])
    return node.outputs[0]


def _scale(tree: bpy.types.NodeTree, vector: bpy.types.NodeSocket,
           scale: bpy.types.NodeSocket, name: str) -> bpy.types.NodeSocket:
    node = tree.nodes.new("ShaderNodeVectorMath")
    node.operation = "SCALE"
    node.label = name
    tree.links.new(vector, node.inputs[0])
    tree.links.new(scale, node.inputs["Scale"])
    return node.outputs[0]


def _endpoint(tree: bpy.types.NodeTree, anchor: bpy.types.Object,
              point: tuple[float, float, float], name: str) -> bpy.types.NodeSocket:
    endpoint = bpy.data.objects.new(name, None)
    anchor.users_collection[0].objects.link(endpoint)
    endpoint.parent = anchor
    endpoint.location = point
    endpoint.empty_display_size = 0.004
    endpoint.hide_render = True
    node = tree.nodes.new("GeometryNodeObjectInfo")
    node.transform_space = "RELATIVE"
    node.inputs["Object"].default_value = endpoint
    node.label = name
    return node.outputs["Location"]


def _capsule_nodes(tree: bpy.types.NodeTree, geometry: bpy.types.NodeSocket,
                   armature: bpy.types.Object, capsule: ContactCapsule,
                   group_name: str, inputs: bpy.types.Node,
                   collection: bpy.types.Collection) -> bpy.types.NodeSocket:
    anchor = bpy.data.objects.new(tree.name + " " + capsule.name, None)
    collection.objects.link(anchor)
    anchor.empty_display_size = capsule.radius
    anchor.hide_render = True
    anchor["endfield_contact_status"] = "authored_capsule_proxy_not_native_collider"
    constraint = anchor.constraints.new("COPY_TRANSFORMS")
    constraint.target = armature
    constraint.subtarget = capsule.bone
    start = _endpoint(tree, anchor, capsule.start, anchor.name + " Start")
    end = _endpoint(tree, anchor, capsule.end, anchor.name + " End")
    position = tree.nodes.new("GeometryNodeInputPosition").outputs[0]
    segment = _vector(tree, "SUBTRACT", end, start, "Capsule centerline")
    relative = _vector(tree, "SUBTRACT", position, start, "Point relative to capsule start")
    numerator = _vector(tree, "DOT_PRODUCT", relative, segment, "Centerline projection numerator")
    denominator = _vector(tree, "DOT_PRODUCT", segment, segment, "Centerline squared length")
    fraction = _math(tree, "DIVIDE", numerator, denominator, "Centerline parameter")
    fraction = _math(tree, "MAXIMUM", fraction, 0.0, "Centerline start clamp")
    fraction = _math(tree, "MINIMUM", fraction, 1.0, "Centerline end clamp")
    closest = _vector(tree, "ADD", start, _scale(tree, segment, fraction, "Closest segment offset"),
                      "Closest capsule centerline point")
    radial = _vector(tree, "SUBTRACT", position, closest, "Capsule radial direction")
    length = tree.nodes.new("ShaderNodeVectorMath")
    length.operation = "LENGTH"
    tree.links.new(radial, length.inputs[0])
    radius = _math(tree, "ADD", inputs.outputs[capsule.name + " Radius"],
                   inputs.outputs["Margin"], "Proxy radius plus authored clearance")
    depth = _math(tree, "SUBTRACT", radius, length.outputs["Value"], "Contact penetration")
    depth = _math(tree, "MAXIMUM", depth, 0.0, "Outside capsule is unchanged")
    depth = _math(tree, "MINIMUM", depth, inputs.outputs["Maximum Displacement"], "Bound correction distance")
    attribute = tree.nodes.new("GeometryNodeInputNamedAttribute")
    attribute.data_type = "FLOAT"
    attribute.inputs["Name"].default_value = group_name
    falloff = tree.nodes.new("ShaderNodeMapRange")
    falloff.interpolation_type = "SMOOTHERSTEP"
    falloff.clamp = True
    falloff.label = "Source sleeve weights with smooth contact falloff"
    tree.links.new(attribute.outputs["Attribute"], falloff.inputs["Value"])
    tree.links.new(inputs.outputs["Full Weight"], falloff.inputs["From Max"])
    depth = _math(tree, "MULTIPLY", depth, falloff.outputs[0], "Weighted local contact displacement")
    depth = _math(tree, "MULTIPLY", depth, inputs.outputs["Strength"], "Explicit contact preview strength")
    depth = _math(tree, "MULTIPLY", depth, inputs.outputs[capsule.name + " Strength"],
                  "Explicit per-capsule contact strength")
    normal = tree.nodes.new("ShaderNodeVectorMath")
    normal.operation = "NORMALIZE"
    tree.links.new(radial, normal.inputs[0])
    result = tree.nodes.new("GeometryNodeSetPosition")
    tree.links.new(geometry, result.inputs["Geometry"])
    tree.links.new(_scale(tree, normal.outputs[0], depth, "Contact offset"), result.inputs["Offset"])
    return result.outputs["Geometry"]


def add_sleeve_contact(mesh: bpy.types.Object, armature: bpy.types.Object,
                       capsules: tuple[ContactCapsule, ...], settings: ContactSettings,
                       collection: bpy.types.Collection, name: str) -> bpy.types.NodesModifier:
    """Attach an explicit contact candidate without changing source vertices, weights, or poses."""
    if mesh.type != "MESH" or armature.type != "ARMATURE" or not capsules or not name:
        raise ValueError("Sleeve contact requires a mesh, armature, explicit capsules and a nonempty name")
    if mesh.modifiers.get(name) is not None:
        raise ValueError(f"Sleeve contact modifier already exists: mesh={mesh.name}, name={name}")
    if not any(modifier.type == "ARMATURE" and modifier.object == armature for modifier in mesh.modifiers):
        raise ValueError(f"Sleeve contact requires the selected armature modifier: mesh={mesh.name}, armature={armature.name}")
    if (not all(math.isfinite(value) for value in settings) or settings.margin < 0
            or settings.maximum_displacement <= 0 or not 0 < settings.full_weight <= 1
            or not 0 <= settings.strength <= 1):
        raise ValueError(f"Invalid explicit sleeve contact settings: settings={settings}")
    if len({capsule.name for capsule in capsules}) != len(capsules):
        raise ValueError("Sleeve contact capsule names must be unique")
    selections: list[tuple[tuple[int, float], ...]] = []
    for capsule in capsules:
        if (not capsule.name or capsule.bone not in armature.pose.bones or not capsule.source_groups
                or len(capsule.start) != 3 or len(capsule.end) != 3
                or not all(math.isfinite(value) for value in (*capsule.start, *capsule.end, capsule.radius))
                or capsule.radius <= 0 or (Vector(capsule.end) - Vector(capsule.start)).length <= 1e-6):
            raise ValueError(f"Invalid sleeve contact capsule: capsule={capsule}")
        if any(group not in mesh.vertex_groups for group in capsule.source_groups):
            raise ValueError(f"Sleeve contact source groups are missing: mesh={mesh.name}, capsule={capsule}")
        if mesh.vertex_groups.get(name + " " + capsule.name) is not None:
            raise ValueError(f"Sleeve contact destination group already exists: capsule={capsule.name}")
        indices = {mesh.vertex_groups[group_name].index for group_name in capsule.source_groups}
        weights = tuple((vertex.index, min(1.0, sum(value.weight for value in vertex.groups if value.group in indices)))
                        for vertex in mesh.data.vertices)
        weights = tuple((index, weight) for index, weight in weights if weight > 0.0)
        if not weights:
            raise ValueError(f"Sleeve contact source groups contain no weighted vertices: capsule={capsule.name}")
        selections.append(weights)
    tree = bpy.data.node_groups.new(name, "GeometryNodeTree")
    tree["endfield_contact_status"] = "opt_in_blender_contact_approximation_not_native_solver"
    tree["endfield_contact_inputs_json"] = json.dumps([capsule._asdict() for capsule in capsules])
    tree.interface.new_socket(name="Geometry", in_out="INPUT", socket_type="NodeSocketGeometry")
    tree.interface.new_socket(name="Geometry", in_out="OUTPUT", socket_type="NodeSocketGeometry")
    for label, value in (("Margin", settings.margin), ("Maximum Displacement", settings.maximum_displacement),
                         ("Full Weight", settings.full_weight), ("Strength", settings.strength),
                         *((capsule.name + " Radius", capsule.radius) for capsule in capsules),
                         *((capsule.name + " Strength", 1.0) for capsule in capsules)):
        socket = tree.interface.new_socket(name=label, in_out="INPUT", socket_type="NodeSocketFloat")
        socket.default_value = value
        socket.min_value = 1e-5 if label == "Full Weight" else 0.0
        socket.max_value = 1.0 if label == "Full Weight" or label.endswith("Strength") else 1e6
    inputs = tree.nodes.new("NodeGroupInput")
    geometry = inputs.outputs["Geometry"]
    for capsule, weights in zip(capsules, selections, strict=True):
        group = mesh.vertex_groups.new(name=name + " " + capsule.name)
        for index, weight in weights:
            group.add((index,), weight, "REPLACE")
        geometry = _capsule_nodes(tree, geometry, armature, capsule, group.name, inputs, collection)
    output = tree.nodes.new("NodeGroupOutput")
    tree.links.new(geometry, output.inputs["Geometry"])
    for index, node in enumerate(tree.nodes):
        node.location = ((index % 8) * 220, -(index // 8) * 180)
    modifier = mesh.modifiers.new(name=name, type="NODES")
    modifier.node_group = tree
    return modifier


def bake_sleeve_contact_controls(modifier: bpy.types.NodesModifier,
                                 samples: tuple[ContactFrame, ...], clip_identity: str) -> None:
    """Bake explicit controls for a tested clip; other poses and subframes remain unverified."""
    if modifier.node_group is None or not samples or not clip_identity:
        raise ValueError("Contact baking requires a node group, samples and an explicit clip identity")
    sockets = {item.name: item.identifier for item in modifier.node_group.interface.items_tree
               if item.item_type == "SOCKET" and item.in_out == "INPUT"}
    frames: dict[str, set[int]] = {}
    for sample in samples:
        if (type(sample.frame) is not int or sample.frame < 1
                or not all(math.isfinite(value) for value in (sample.radius, sample.strength))
                or sample.radius <= 0 or not 0 <= sample.strength <= 1
                or sample.capsule + " Radius" not in sockets
                or sample.capsule + " Strength" not in sockets):
            raise ValueError(f"Invalid explicit contact control sample: sample={sample}")
        existing = frames.setdefault(sample.capsule, set())
        if sample.frame in existing:
            raise ValueError(f"Duplicate contact control sample: sample={sample}")
        existing.add(sample.frame)
    if any(value != next(iter(frames.values())) for value in frames.values()):
        raise ValueError(f"Contact control frame grids differ: frames={frames}")
    expected = {name.removesuffix(" Radius") for name in sockets if name.endswith(" Radius")}
    if set(frames) != expected:
        raise ValueError(f"Contact baking must sample every capsule: expected={expected}, actual={set(frames)}")
    for sample in sorted(samples):
        for suffix, value in ((" Radius", sample.radius), (" Strength", sample.strength)):
            identifier = sockets[sample.capsule + suffix]
            modifier[identifier] = value
            modifier.keyframe_insert(data_path=f'["{identifier}"]', frame=sample.frame)
    action = modifier.id_data.animation_data.action
    paths = {modifier.path_from_id(f'["{sockets[name]}"]')
             for name in sockets if name.endswith((" Radius", " Strength"))}
    for curve in action.fcurves:
        if curve.data_path in paths:
            for point in curve.keyframe_points:
                point.interpolation = "LINEAR"
    modifier.node_group["endfield_contact_clip_identity"] = clip_identity
    modifier.node_group["endfield_contact_bake_scope"] = "tested_clip_integer_frames_only_not_new_pose_stability"
    modifier.node_group["endfield_contact_samples_json"] = json.dumps([sample._asdict() for sample in samples])
