"""Provide camera-dependent FogSimple contact fading from explicit host geometry.

This is an opt-in, per-vertex host depth producer, not a native screen-depth pass.
The caller supplies a perspective render camera and independent opaque casters.
Keep camera scale at one and keep casters free of receiver dependency cycles.
"""
from __future__ import annotations

import json
import math
from typing import TypedDict

import bpy
from mathutils import Matrix

from blender_scene_fog_materials import _math, _scale, _vector
from blender_scene_materials import layout_shader_node_tree


DEPTH_GAP_ATTRIBUTE = "ef_fog_view_depth_gap"


class FogDepthAudit(TypedDict):
    status: str
    camera: str | None
    receivers: list[str]
    materials: list[str]
    opaque_casters: int
    subdivision_level: int
    boundary: str


def _opaque_lit_source(obj: bpy.types.Object) -> bool:
    """Select only opaque source Lit draws; omit unproven depth-writing families."""
    if obj.type != "MESH" or obj.hide_render or not obj.material_slots:
        return False
    for slot in obj.material_slots:
        material = slot.material
        if (material is None or material.get("endfield_shader_family") != "lit"
                or "endfield_original_render_state" not in material):
            return False
        source = json.loads(material["endfield_original_render_state"])
        if (not isinstance(source, dict) or type(source.get("CustomRenderQueue")) is not int
                or not isinstance(source.get("ValidKeywords"), list)
                or any(not isinstance(key, str) for key in source["ValidKeywords"])):
            raise ValueError(f"Invalid source render state for fog depth caster: material={material.name}")
        if source["CustomRenderQueue"] > 2500 or "_ALPHATEST_ON" in source["ValidKeywords"]:
            return False
    return True


def apply_scene_fog_depth(scene: bpy.types.Scene, subdivision_level: int) -> FogDepthAudit:
    """Apply the requested depth producer to source-enabled fog in a prepared scene."""
    if type(subdivision_level) is not int or not 0 <= subdivision_level <= 6:
        raise ValueError("Fog depth subdivision must be an integer from 0 to 6")
    receivers = tuple(obj for obj in scene.objects if obj.type == "MESH" and not obj.hide_render
                      and any(slot.material is not None and slot.material.get("endfield_fx_fog_softblend_status")
                              in ("constant_one_screen_depth_unavailable", "host_camera_raycast_per_vertex")
                              for slot in obj.material_slots))
    boundary = ("Selected exported perspective camera and opaque source Lit casters; per-vertex depth, "
                "not native screen depth. Cutouts, terrain and other shader families are excluded.")
    if not receivers:
        return {"status": "no_source_enabled_fog", "camera": None, "receivers": [], "materials": [],
                "opaque_casters": 0, "subdivision_level": subdivision_level, "boundary": boundary}
    camera = scene.camera
    if camera is None or camera.type != "CAMERA" or camera.data.type != "PERSP":
        raise ValueError("Prepared fog depth requires an exported perspective FX camera; include scene effects")
    selected = tuple(obj for obj in scene.objects if _opaque_lit_source(obj))
    casters = bpy.data.collections.new("Endfield_Fog_Opaque_Depth_Context")
    for obj in selected:
        casters.objects.link(obj)
    try:
        materials = attach_fog_depth(camera, receivers, casters, subdivision_level)
    except (ValueError, KeyError, TypeError):
        bpy.data.collections.remove(casters)
        raise
    scene.collection.children.link(casters)
    return {"status": "host_camera_depth_attached", "camera": camera.name,
            "receivers": [obj.name for obj in receivers], "materials": list(materials),
            "opaque_casters": len(selected), "subdivision_level": subdivision_level, "boundary": boundary}


def _node(tree: bpy.types.NodeTree, kind: str, label: str) -> bpy.types.Node:
    node = tree.nodes.new(kind)
    node.name = node.label = label
    node["endfield_layout_section"] = "Host Camera Depth"
    return node


def _dot(tree: bpy.types.NodeTree, label: str, left: bpy.types.NodeSocket,
         right: bpy.types.NodeSocket) -> bpy.types.NodeSocket:
    node = _node(tree, "ShaderNodeVectorMath", label)
    node.operation = "DOT_PRODUCT"
    tree.links.new(left, node.inputs[0])
    tree.links.new(right, node.inputs[1])
    return node.outputs["Value"]


def _direction(tree: bpy.types.NodeTree, label: str, matrix: bpy.types.NodeSocket,
               vector: bpy.types.NodeSocket | tuple[float, float, float]) -> bpy.types.NodeSocket:
    node = _node(tree, "FunctionNodeTransformDirection", label)
    tree.links.new(matrix, node.inputs["Transform"])
    if isinstance(vector, bpy.types.NodeSocket):
        tree.links.new(vector, node.inputs["Direction"])
    else:
        node.inputs["Direction"].default_value = vector
    return node.outputs["Direction"]


def _clip(tree: bpy.types.NodeTree, camera: bpy.types.Object, field: str) -> bpy.types.NodeSocket:
    node = _node(tree, "ShaderNodeValue", "Camera " + field)
    curve = node.outputs[0].driver_add("default_value")
    curve.driver.type = "AVERAGE"
    variable = curve.driver.variables.new()
    variable.name = field
    variable.type = "SINGLE_PROP"
    variable.targets[0].id_type = "CAMERA"
    variable.targets[0].id = camera.data
    variable.targets[0].data_path = field
    return node.outputs[0]


def build_fog_depth_nodes(camera: bpy.types.Object, casters: bpy.types.Collection,
                          subdivision_level: int) -> bpy.types.NodeTree:
    """Store linear view-depth separation using near/far-clipped host rays."""
    tree = bpy.data.node_groups.new("EF Fog Camera Depth", "GeometryNodeTree")
    tree.interface.new_socket(name="Geometry", in_out="INPUT", socket_type="NodeSocketGeometry")
    tree.interface.new_socket(name="Geometry", in_out="OUTPUT", socket_type="NodeSocketGeometry")
    entry = _node(tree, "NodeGroupInput", "Source Fog Geometry")
    output = _node(tree, "NodeGroupOutput", "Fog With Live Depth Gap")
    subdivide = _node(tree, "GeometryNodeSubdivideMesh", "Linear Depth Sampling Subdivision")
    subdivide.inputs["Level"].default_value = subdivision_level
    tree.links.new(entry.outputs["Geometry"], subdivide.inputs["Mesh"])
    position = _node(tree, "GeometryNodeInputPosition", "Receiver Local Position").outputs["Position"]
    relative = _node(tree, "GeometryNodeObjectInfo", "Camera In Receiver Coordinates")
    relative.transform_space = "RELATIVE"
    relative.inputs["Object"].default_value = camera
    original = _node(tree, "GeometryNodeObjectInfo", "Camera World Matrix")
    original.transform_space = "ORIGINAL"
    original.inputs["Object"].default_value = camera
    self_object = _node(tree, "GeometryNodeSelfObject", "Receiver Object")
    world = _node(tree, "GeometryNodeObjectInfo", "Receiver World Matrix")
    world.transform_space = "ORIGINAL"
    tree.links.new(self_object.outputs["Self Object"], world.inputs["Object"])
    delta = _vector(tree, "Camera To Receiver Local", "SUBTRACT", position, relative.outputs["Location"])
    world_delta = _direction(tree, "Camera To Receiver World", world.outputs["Transform"], delta)
    forward = _direction(tree, "Camera World Forward", original.outputs["Transform"], (0., 0., -1.))
    depth = _dot(tree, "Surface View Depth", world_delta, forward)
    safe_depth = _math(tree, "Positive Projection Denominator", "MAXIMUM", depth, 1.e-8, False)
    near = _clip(tree, camera, "clip_start")
    far = _clip(tree, camera, "clip_end")
    near_ratio = _math(tree, "Near Plane Ray Fraction", "DIVIDE", near, safe_depth, False)
    start = _vector(tree, "Near Plane Ray Origin", "ADD", relative.outputs["Location"],
                    _scale(tree, "Near Plane Ray Offset", delta, near_ratio))
    local_length = _node(tree, "ShaderNodeVectorMath", "Local Camera Ray Length")
    local_length.operation = "LENGTH"
    tree.links.new(delta, local_length.inputs[0])
    span = _math(tree, "Camera Clip Span", "SUBTRACT", far, near, False)
    length = _math(tree, "Clip Span Ray Fraction", "DIVIDE", span, safe_depth, False)
    length = _math(tree, "Local Clipped Ray Length", "MULTIPLY", length, local_length.outputs["Value"], False)
    collection = _node(tree, "GeometryNodeCollectionInfo", "Explicit Opaque Depth Casters")
    collection.transform_space = "RELATIVE"
    collection.inputs["Collection"].default_value = casters
    realize = _node(tree, "GeometryNodeRealizeInstances", "Evaluated Caster Meshes")
    tree.links.new(collection.outputs["Instances"], realize.inputs["Geometry"])
    ray = _node(tree, "GeometryNodeRaycast", "Near To Far Camera Ray")
    ray.data_type = "FLOAT"
    tree.links.new(realize.outputs["Geometry"], ray.inputs["Target Geometry"])
    tree.links.new(start, ray.inputs["Source Position"])
    tree.links.new(delta, ray.inputs["Ray Direction"])
    tree.links.new(length, ray.inputs["Ray Length"])
    separation = _vector(tree, "Hit Minus Fog Local", "SUBTRACT", ray.outputs["Hit Position"], position)
    world_separation = _direction(tree, "Hit Minus Fog World", world.outputs["Transform"], separation)
    hit_gap = _dot(tree, "Hit View Depth Gap", world_separation, forward)
    miss_gap = _math(tree, "Far Plane View Depth Gap", "SUBTRACT", far, depth, False)
    select = _node(tree, "GeometryNodeSwitch", "Hit Or Far Plane Depth")
    select.input_type = "FLOAT"
    tree.links.new(ray.outputs["Is Hit"], select.inputs["Switch"])
    tree.links.new(miss_gap, select.inputs["False"])
    tree.links.new(hit_gap, select.inputs["True"])
    visible = _math(tree, "Positive Camera View Depth", "GREATER_THAN", depth, 0., False)
    gap = _math(tree, "Front Camera Depth Gap", "MULTIPLY", select.outputs["Output"], visible, False)
    store = _node(tree, "GeometryNodeStoreNamedAttribute", "Store Host View Depth Gap")
    store.data_type = "FLOAT"
    store.domain = "POINT"
    store.inputs["Name"].default_value = DEPTH_GAP_ATTRIBUTE
    tree.links.new(subdivide.outputs["Mesh"], store.inputs["Geometry"])
    tree.links.new(gap, store.inputs["Value"])
    tree.links.new(store.outputs["Geometry"], output.inputs["Geometry"])
    tree["endfield_fog_depth_contract"] = "HostPerspectiveViewDepth/1"
    tree["endfield_fog_depth_camera"] = camera.name
    tree["endfield_fog_depth_boundary"] = "Selected perspective camera and explicit opaque casters; interpolated point samples, not native per-pixel screen depth. Camera must keep unit scale and no shear."
    layout_shader_node_tree(tree)
    return tree


def attach_fog_depth(camera: bpy.types.Object, receivers: tuple[bpy.types.Object, ...],
                      casters: bpy.types.Collection, subdivision_level: int) -> tuple[str, ...]:
    """Bind private fog materials and a shared live host-depth modifier after preflight."""
    if camera.type != "CAMERA" or camera.data.type != "PERSP":
        raise ValueError("Live fog depth requires an explicit perspective camera")
    rotation = camera.matrix_world.to_3x3()
    gram = rotation.transposed() @ rotation
    identity = Matrix.Identity(3)
    if any(not math.isfinite(gram[r][c]) or abs(gram[r][c] - identity[r][c]) > 1.e-5
           for r in range(3) for c in range(3)) or rotation.determinant() < 0.:
        raise ValueError("Live fog depth camera must have unit world scale and no shear or reflection")
    if not receivers or len(set(receivers)) != len(receivers):
        raise ValueError("Live fog depth requires unique nonempty receiver meshes")
    if type(subdivision_level) is not int or not 0 <= subdivision_level <= 6:
        raise ValueError("Live fog depth subdivision must be 0..6; each level quadruples face samples")
    if set(receivers).intersection(casters.all_objects):
        raise ValueError("Live fog depth receivers cannot belong to the caster collection")
    materials: list[tuple[bpy.types.Object, bpy.types.Material, float]] = []
    for obj in receivers:
        if (obj.type != "MESH" or len(obj.data.materials) != 1
                or any(not math.isfinite(v) for row in obj.matrix_world for v in row)
                or abs(obj.matrix_world.determinant()) < 1.e-12):
            raise ValueError(f"Fog depth receiver needs a nonsingular mesh with one material: {obj.name}")
        material = obj.material_slots[0].material
        if (material is None or material.get("endfield_fx_fog_contract") != "FogSimpleObjectHeight/2"
                or material.node_tree is None or "Fog Soft Depth Blend Reference" not in material.node_tree.nodes
                or material.get("endfield_fx_fog_softblend_status") != "constant_one_screen_depth_unavailable"):
            raise ValueError(f"Fog depth receiver lacks an unbound source soft-blend material: {obj.name}")
        source = json.loads(material["endfield_fx_source_material_json"])
        distance = float(source["Floats"]["_SoftDistance"])
        if not math.isfinite(distance) or distance <= 0.:
            raise ValueError(f"Fog source soft distance must be positive: {obj.name}")
        materials.append((obj, material, distance))
    group = build_fog_depth_nodes(camera, casters, subdivision_level)
    bound: list[str] = []
    for obj, source_material, distance in materials:
        material = source_material.copy()
        material.name = source_material.name + "_LiveDepth"
        obj.material_slots[0].link = "OBJECT"
        obj.material_slots[0].material = material
        tree = material.node_tree
        attribute = _node(tree, "ShaderNodeAttribute", "Live Host View Depth Gap")
        attribute.attribute_name = DEPTH_GAP_ATTRIBUTE
        factor = _math(tree, "Source Soft Depth Saturate", "DIVIDE", attribute.outputs["Fac"], distance, True)
        tree.links.new(factor, tree.nodes["Fog Soft Depth Blend Reference"].inputs[1])
        tree.nodes.remove(tree.nodes["Fog Soft Depth Blend - Reference 1"])
        material["endfield_fx_fog_softblend_status"] = "host_camera_raycast_per_vertex"
        limitations = json.loads(material["endfield_fx_limitations"])
        limitations = [item for item in limitations if "source softblend is held at one" not in item]
        limitations.append(group["endfield_fog_depth_boundary"])
        material["endfield_fx_limitations"] = json.dumps(limitations)
        modifier = obj.modifiers.new("EF Live Fog Camera Depth", "NODES")
        modifier.node_group = group
        layout_shader_node_tree(tree)
        bound.append(material.name)
    return tuple(bound)
