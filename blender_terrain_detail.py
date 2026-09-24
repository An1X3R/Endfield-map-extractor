"""Source-driven terrain detail reconstruction shared by Cycles and Eevee.

Layer selection, height blending and tint follow retained VTBakePage programs.
Smooth triplanar sampling replaces their stochastic projection in Blender.
"""
from __future__ import annotations

import hashlib
import json
from pathlib import Path

import bpy
from mathutils import Vector as MathVector

from blender_scene_materials import texture_image
from blender_scene_terrain import terrain_world_normal_group
from endfield_terrain_detail_contract import TerrainDetail, load_terrain_detail

Scalar = bpy.types.NodeSocket | float
Vector = bpy.types.NodeSocket | tuple[float, float, float]
EPSILON = 2 ** -14


def detail_identity(path: Path) -> str:
    """Keep relative texture bindings distinct when manifests have equal contents."""
    return hashlib.sha256(str(path.resolve()).casefold().encode() + b"\0" + path.read_bytes()).hexdigest()


def bind(tree: bpy.types.NodeTree, value: Scalar | Vector, socket: bpy.types.NodeSocket) -> None:
    if isinstance(value, bpy.types.NodeSocket):
        tree.links.new(value, socket)
    else:
        socket.default_value = value


def math_node(tree: bpy.types.NodeTree, operation: str, values: tuple[Scalar, ...]) -> bpy.types.NodeSocket:
    node = tree.nodes.new("ShaderNodeMath")
    node.operation = operation
    for value, socket in zip(values, node.inputs):
        bind(tree, value, socket)
    return node.outputs[0]


def vector_node(tree: bpy.types.NodeTree, operation: str, values: tuple[Vector, ...]) -> bpy.types.NodeSocket:
    node = tree.nodes.new("ShaderNodeVectorMath")
    node.operation = operation
    for value, socket in zip(values, node.inputs):
        bind(tree, value, socket)
    return node.outputs[0]


def scale(tree: bpy.types.NodeTree, vector: Vector, factor: Scalar) -> bpy.types.NodeSocket:
    node = tree.nodes.new("ShaderNodeVectorMath")
    node.operation = "SCALE"
    bind(tree, vector, node.inputs[0])
    bind(tree, factor, node.inputs[3])
    return node.outputs[0]


def components(tree: bpy.types.NodeTree, value: Vector) -> tuple[bpy.types.NodeSocket, ...]:
    node = tree.nodes.new("ShaderNodeSeparateXYZ")
    bind(tree, value, node.inputs[0])
    return tuple(node.outputs)


def combine(tree: bpy.types.NodeTree, values: tuple[Scalar, Scalar, Scalar]) -> bpy.types.NodeSocket:
    node = tree.nodes.new("ShaderNodeCombineXYZ")
    for value, socket in zip(values, node.inputs):
        bind(tree, value, socket)
    return node.outputs[0]


def total(tree: bpy.types.NodeTree, values: list[Scalar]) -> Scalar:
    result: Scalar = 0.0
    for value in values:
        result = math_node(tree, "ADD", (result, value))
    return result


def vector_total(tree: bpy.types.NodeTree, values: list[Vector]) -> Vector:
    result: Vector = (0.0, 0.0, 0.0)
    for value in values:
        result = vector_node(tree, "ADD", (result, value))
    return result


def image_sample(tree: bpy.types.NodeTree, path: Path, colorspace: str,
                 uv: bpy.types.NodeSocket, interpolation: str, extension: str) -> bpy.types.Node:
    node = tree.nodes.new("ShaderNodeTexImage")
    node.image = texture_image(path, colorspace, "CHANNEL_PACKED")
    node.interpolation = interpolation
    node.extension = extension
    tree.links.new(uv, node.inputs["Vector"])
    return node


def detail_group(path: Path, detail: TerrainDetail) -> bpy.types.ShaderNodeTree:
    digest = detail_identity(path)
    name = "EF_Terrain_Detail_" + digest[:24]
    existing = bpy.data.node_groups.get(name)
    if existing is not None:
        if existing.get("endfield_contract") != "HGTerrainDetail/1":
            raise ValueError(f"Conflicting terrain detail group: {name}")
        return existing
    tree = bpy.data.node_groups.new(name, "ShaderNodeTree")
    for label, socket_type in (("Position", "NodeSocketVector"), ("Base Normal", "NodeSocketVector")):
        tree.interface.new_socket(name=label, in_out="INPUT", socket_type=socket_type)
    for label, socket_type in (("Base Color", "NodeSocketColor"), ("Roughness", "NodeSocketFloat"),
                               ("World Normal", "NodeSocketVector"), ("Occlusion", "NodeSocketFloat")):
        tree.interface.new_socket(name=label, in_out="OUTPUT", socket_type=socket_type)
    entry = tree.nodes.new("NodeGroupInput")
    output = tree.nodes.new("NodeGroupOutput")
    mul = lambda a, b: math_node(tree, "MULTIPLY", (a, b))
    add = lambda a, b: math_node(tree, "ADD", (a, b))
    sub = lambda a, b: math_node(tree, "SUBTRACT", (a, b))
    div = lambda a, b: math_node(tree, "DIVIDE", (a, b))
    maximum = lambda a, b: math_node(tree, "MAXIMUM", (a, b))
    px, py, pz = components(tree, entry.outputs["Position"])
    x, y, z = px, pz, mul(py, -1.0)
    bx, by, bz = components(tree, entry.outputs["Base Normal"])
    nx, ny, nz = bx, bz, mul(by, -1.0)
    axis_weights = [math_node(tree, "POWER", (math_node(tree, "ABSOLUTE", (v,)), 8.0)) for v in (nx, ny, nz)]
    axis_sum = maximum(total(tree, axis_weights), EPSILON)
    axis_weights = [div(weight, axis_sum) for weight in axis_weights]
    source_origin = detail["position"]
    index_uv = combine(tree, (div(sub(x, source_origin[0]), float(detail["size"])),
                              div(sub(z, source_origin[2]), float(detail["size"])), 0.0))
    index = image_sample(tree, path.parent / detail["index"], "Non-Color", index_uv, "Closest", "EXTEND")
    indices = (*components(tree, index.outputs["Color"]), index.outputs["Alpha"])
    indices = [mul(value, 63.75) for value in indices]
    x0, x1, z0, z1 = detail["bounds"]
    local_uv = combine(tree, (div(sub(x, x0), x1-x0), sub(1.0, div(sub(z, z0), z1-z0)), 0.0))
    # Clamp control filtering within each 8 m index cell (16 source texels).
    control_coords = []
    for value, origin in ((x, source_origin[0]), (z, source_origin[2])):
        cell = div(sub(value, origin), 8.0)
        fraction = math_node(tree, "FRACT", (cell,))
        fraction = math_node(tree, "MINIMUM", (maximum(fraction, 1/32), 31/32))
        control_coords.append(add(mul(add(math_node(tree, "FLOOR", (cell,)), fraction), 8.0), origin))
    control_uv = combine(tree, (div(sub(control_coords[0], x0), x1-x0),
                                sub(1.0, div(sub(control_coords[1], z0), z1-z0)), 0.0))
    control = image_sample(tree, path.parent / detail["control"], "Non-Color", control_uv, "Linear", "EXTEND")
    tint = image_sample(tree, path.parent / detail["tint"], "sRGB", local_uv, "Linear", "EXTEND")
    initial: list[Scalar] = list(components(tree, control.outputs["Color"]))
    initial.append(maximum(sub(1.0, total(tree, initial)), 0.0))
    selectors: list[list[bpy.types.NodeSocket]] = []
    heights: list[Scalar] = []
    colors: list[Vector] = []
    normals: list[Vector] = []
    roughness: list[Scalar] = []
    occlusion: list[Scalar] = []
    for layer in detail["layers"]:
        st = layer["splat_st"]
        tiling = st[0] / detail["size"]
        uv_axes = [combine(tree, (add(mul(u, tiling), st[2]), add(mul(v, tiling), st[3]), 0.0))
                   for u, v in ((z, y), (x, z), (x, y))]
        samples = {}
        for field, role in (("diffuse", "sRGB"), ("normal", "Non-Color")):
            nodes = [image_sample(tree, path.parent / layer[field], role, uv, "Linear", "REPEAT") for uv in uv_axes]
            rgb = vector_total(tree, [scale(tree, node.outputs["Color"], weight) for node, weight in zip(nodes, axis_weights)])
            alpha = total(tree, [mul(node.outputs["Alpha"], weight) for node, weight in zip(nodes, axis_weights)])
            samples[field] = (rgb, alpha)
        diffuse, height = samples["diffuse"]
        packed, ao = samples["normal"]
        mask_scale, mask_offset = layer["mask_scale"], layer["mask_offset"]
        heights.append(add(mul(height, mask_scale[2]), mask_offset[2]))
        colors.append(vector_node(tree, "MULTIPLY", (diffuse, tuple(layer["diffuse_scale"][:3]))))
        red, green, blue = components(tree, packed)
        dx, dy = sub(mul(red, 2.0), 1.0), sub(mul(green, 2.0), 1.0)
        dz = maximum(math_node(tree, "SQRT", (maximum(sub(1.0, add(mul(dx, dx), mul(dy, dy))), 0.0),)), EPSILON)
        normals.append(combine(tree, (mul(dx, layer["packed_info"][3]), mul(dy, layer["packed_info"][3]), dz)))
        roughness.append(add(mul(blue, mask_scale[3]), mask_offset[3]))
        occlusion.append(add(mul(ao, mask_scale[1]), mask_offset[1]))
        selectors.append([math_node(tree, "COMPARE", (index_value, float(layer["ordinal"]), 0.01)) for index_value in indices])
    slot_heights = [total(tree, [mul(selectors[i][slot], heights[i]) for i in range(len(heights))]) for slot in range(4)]
    borders = [maximum(total(tree, [mul(selectors[i][slot], layer["diffuse_scale"][3])
                                    for i, layer in enumerate(detail["layers"])]), EPSILON) for slot in range(4)]
    weighted_heights = [mul(weight, height) for weight, height in zip(initial, slot_heights)]
    peak = maximum(maximum(weighted_heights[0], weighted_heights[1]), maximum(weighted_heights[2], weighted_heights[3]))
    adjusted = [mul(weight, add(maximum(add(sub(height, peak), border), 0.0), EPSILON))
                for weight, height, border in zip(initial, weighted_heights, borders)]
    denominator = maximum(total(tree, adjusted), EPSILON)
    weights = [div(value, denominator) for value in adjusted]
    layer_weights = [total(tree, [mul(selector, weight) for selector, weight in zip(row, weights)]) for row in selectors]
    color = vector_total(tree, [scale(tree, value, weight) for value, weight in zip(colors, layer_weights)])
    mean = vector_total(tree, [scale(tree, tuple(layer["packed_info"][:3]), weight)
                              for layer, weight in zip(detail["layers"], layer_weights)])
    tint_factor = math_node(tree, "POWER", (tint.outputs["Alpha"], 4.0))
    final_color = vector_node(tree, "ADD", (color, scale(tree, vector_node(tree, "SUBTRACT", (tint.outputs["Color"], mean)), tint_factor)))
    detail_normal = vector_total(tree, [scale(tree, value, weight) for value, weight in zip(normals, layer_weights)])
    dx, dy, dz = components(tree, detail_normal)
    tangent = combine(tree, (ny, 0.0, mul(nx, -1.0)))
    bitangent = combine(tree, (mul(mul(nx, nz), -1.0), mul(add(mul(nx, nx), mul(ny, ny)), -1.0), mul(mul(ny, nz), -1.0)))
    normal = vector_node(tree, "NORMALIZE", (vector_total(tree, [scale(tree, tangent, dx), scale(tree, bitangent, dy),
                                                                 scale(tree, entry.outputs["Base Normal"], dz)]),))
    ax, ay, az = components(tree, normal)
    # The VT consumer retains XZ and reconstructs the positive Unity up component.
    normal = combine(tree, (ax, ay, math_node(tree, "ABSOLUTE", (az,))))
    bind(tree, final_color, output.inputs["Base Color"])
    bind(tree, total(tree, [mul(value, weight) for value, weight in zip(roughness, layer_weights)]), output.inputs["Roughness"])
    bind(tree, total(tree, [mul(value, weight) for value, weight in zip(occlusion, layer_weights)]), output.inputs["Occlusion"])
    bind(tree, normal, output.inputs["World Normal"])
    tree["endfield_contract"] = "HGTerrainDetail/1"
    tree["endfield_source_sha256"] = digest
    tree["endfield_projection"] = detail["projection"]
    tree["endfield_limitations"] = "Smooth triplanar and half-texel control border are adaptations; POM, cliff C, runtime stochastic projection pending; AO exposed, not multiplied into direct lighting"
    return tree


def apply_detail_material(material: bpy.types.Material, path: Path, detail: TerrainDetail) -> str:
    tree = material.node_tree
    digest = detail_identity(path)
    if material.get("endfield_terrain_detail_contract") == "HGTerrainDetail/1":
        groups = [node for node in tree.nodes if node.type == "GROUP" and node.node_tree
                  and node.node_tree.get("endfield_source_sha256") == digest]
        if len(groups) != 1 or not groups[0].outputs["Base Color"].is_linked:
            raise ValueError(f"Terrain detail reapplication contract mismatch: {material.name}")
        return "already_adapted"
    surface_path = Path(material["endfield_terrain_surface_source"])
    surface = json.loads(surface_path.read_text(encoding="utf-8"))
    if surface["map"].casefold() != detail["map"].casefold():
        raise ValueError(f"Terrain detail map mismatch: {material.name}, {surface['map']}, {detail['map']}")
    images = {}
    for role, keys in (("albedo", ("albedo", "albedo_rgba")), ("normal", ("normal", "normal_rgba"))):
        paths = {(surface_path.parent / surface[key]).resolve() for key in keys if key in surface}
        matches = [node for node in tree.nodes if node.type == "TEX_IMAGE" and node.image and
                   Path(node.image.get("endfield_terrain_crop_source", bpy.path.abspath(node.image.filepath))).resolve() in paths]
        if len(matches) != 1:
            raise ValueError(f"Terrain detail requires a unique {role} atlas: {material.name}, count={len(matches)}")
        images[role] = matches[0]
    destinations = [link.to_socket for link in images["albedo"].outputs["Color"].links]
    if not destinations:
        raise ValueError(f"Terrain base color has no consumers: {material.name}")
    normals = [node for node in tree.nodes if node.type == "GROUP" and node.node_tree
               and node.node_tree.get("endfield_contract") == "HGTerrainWorldNormal/1"]
    if len(normals) > 1:
        raise ValueError(f"Multiple terrain world normal decoders: {material.name}")
    if normals:
        normal = normals[0]
    else:
        normal = tree.nodes.new("ShaderNodeGroup")
        normal.node_tree = terrain_world_normal_group()
        tree.links.new(images["normal"].outputs["Color"], normal.inputs["Packed RG"])
    normal_destinations = [link.to_socket for link in normal.outputs["World Normal"].links]
    node = tree.nodes.new("ShaderNodeGroup")
    node.node_tree = detail_group(path, detail)
    node.label = "Source terrain layers and height blend"
    geometry = tree.nodes.new("ShaderNodeNewGeometry")
    tree.links.new(geometry.outputs["Position"], node.inputs["Position"])
    tree.links.new(normal.outputs["World Normal"], node.inputs["Base Normal"])
    for target in destinations:
        tree.links.new(node.outputs["Base Color"], target)
    for target in normal_destinations:
        tree.links.new(node.outputs["World Normal"], target)
    retained = 0
    for shader in (item for item in tree.nodes if item.type == "BSDF_PRINCIPLED" and item.outputs["BSDF"].is_linked):
        if shader.inputs["Roughness"].is_linked:
            retained += 1
        else:
            tree.links.new(node.outputs["Roughness"], shader.inputs["Roughness"])
    material["endfield_terrain_detail_contract"] = "HGTerrainDetail/1"
    material["endfield_terrain_detail_source"] = str(path.resolve())
    material["endfield_terrain_detail_roughness_overrides_retained"] = retained
    material["endfield_terrain_cliff_projection_status"] = node.node_tree["endfield_limitations"]
    return "adapted"


def apply_scene_detail(scene: bpy.types.Scene, path: Path) -> list[dict[str, str]]:
    detail = load_terrain_detail(path)
    terrain = [obj for obj in scene.objects if obj.type == "MESH" and "unity_tile" in obj]
    if not terrain:
        raise ValueError("Scene contains no terrain tiles for detail reconstruction")
    x0, x1, z0, z1 = detail["bounds"]
    for obj in terrain:
        for corner in obj.bound_box:
            point = obj.matrix_world @ MathVector(corner)
            if not (x0-0.01 <= point.x <= x1+0.01 and z0-0.01 <= -point.y <= z1+0.01):
                raise ValueError(f"Terrain geometry exceeds detail resource bounds: {obj.name}, {tuple(point)}")
    materials = {material for obj in scene.objects if obj.type == "MESH"
                 for material in obj.data.materials if material and material.get("endfield_terrain_surface_source")}
    return [{"material": material.name, "status": apply_detail_material(material, path, detail)}
            for material in sorted(materials, key=lambda item: item.name)]
