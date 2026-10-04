"""Source-driven terrain detail reconstruction shared by Cycles and Eevee.

Layer selection, height blending and tint follow retained VTBakePage programs.
Smooth triplanar sampling replaces their stochastic projection in Blender.
"""
from __future__ import annotations

import hashlib
import json
import math
from pathlib import Path

import bpy
import numpy as np
from numpy.typing import NDArray

from blender_scene_materials import texture_image
from blender_scene_terrain import terrain_world_normal_group
from endfield_scene_material_contract import resolve_export_path
from endfield_terrain_detail_contract import DetailLayer, TerrainDetail, load_terrain_detail

Scalar = bpy.types.NodeSocket | float
Vector = bpy.types.NodeSocket | tuple[float, float, float]
EPSILON = 2 ** -14
MAX_LAYERS_PER_MATERIAL = 8


def detail_identity(path: Path, ordinals: tuple[int, ...]) -> str:
    """Identify both source bindings and the exact shader layer subset."""
    return hashlib.sha256(str(path.resolve()).casefold().encode() + b"\0" + path.read_bytes()
                          + b"\0" + bytes(ordinals)).hexdigest()


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
    node.image = texture_image(resolve_export_path(path), colorspace, "CHANNEL_PACKED")
    node.interpolation = interpolation
    node.extension = extension
    tree.links.new(uv, node.inputs["Vector"])
    return node


def detail_group(path: Path, detail: TerrainDetail, layers: list[DetailLayer]) -> bpy.types.ShaderNodeTree:
    ordinals = tuple(int(layer["ordinal"]) for layer in layers)
    digest = detail_identity(path, ordinals)
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
    for layer in layers:
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
                                    for i, layer in enumerate(layers)]), EPSILON) for slot in range(4)]
    weighted_heights = [mul(weight, height) for weight, height in zip(initial, slot_heights)]
    peak = maximum(maximum(weighted_heights[0], weighted_heights[1]), maximum(weighted_heights[2], weighted_heights[3]))
    adjusted = [mul(weight, add(maximum(add(sub(height, peak), border), 0.0), EPSILON))
                for weight, height, border in zip(initial, weighted_heights, borders)]
    denominator = maximum(total(tree, adjusted), EPSILON)
    weights = [div(value, denominator) for value in adjusted]
    layer_weights = [total(tree, [mul(selector, weight) for selector, weight in zip(row, weights)]) for row in selectors]
    color = vector_total(tree, [scale(tree, value, weight) for value, weight in zip(colors, layer_weights)])
    mean = vector_total(tree, [scale(tree, tuple(layer["packed_info"][:3]), weight)
                              for layer, weight in zip(layers, layer_weights)])
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
    tree["endfield_layer_ordinals"] = list(ordinals)
    tree["endfield_projection"] = detail["projection"]
    tree["endfield_limitations"] = "Smooth triplanar and half-texel control border are adaptations; POM, cliff C, runtime stochastic projection pending; AO exposed, not multiplied into direct lighting"
    return tree


def apply_detail_material(material: bpy.types.Material, path: Path, detail: TerrainDetail,
                          layers: list[DetailLayer]) -> str:
    tree = material.node_tree
    ordinals = tuple(int(layer["ordinal"]) for layer in layers)
    digest = detail_identity(path, ordinals)
    if material.get("endfield_terrain_detail_contract") == "HGTerrainDetail/1":
        groups = [node for node in tree.nodes if node.type == "GROUP" and node.node_tree
                  and node.node_tree.get("endfield_source_sha256") == digest]
        if len(groups) != 1 or not groups[0].outputs["Base Color"].is_linked:
            raise ValueError(f"Terrain detail reapplication contract mismatch: {material.name}")
        return "already_adapted"
    surface_path = resolve_export_path(Path(material["endfield_terrain_surface_source"]))
    surface = json.loads(surface_path.read_text(encoding="utf-8"))
    if surface["map"].casefold() != detail["map"].casefold():
        raise ValueError(f"Terrain detail map mismatch: {material.name}, {surface['map']}, {detail['map']}")
    images = {}
    for role, keys in (("albedo", ("albedo", "albedo_rgba")), ("normal", ("normal", "normal_rgba"))):
        paths = {resolve_export_path(surface_path.parent / surface[key]) for key in keys if key in surface}
        matches = [node for node in tree.nodes if node.type == "TEX_IMAGE" and node.image and
                   resolve_export_path(Path(node.image.get("endfield_terrain_crop_source", bpy.path.abspath(node.image.filepath)))) in paths]
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
    node.node_tree = detail_group(path, detail, layers)
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


def index_ordinals(path: Path, detail: TerrainDetail) -> NDArray[np.int16]:
    """Read source RGBA ordinals with row zero at Blender's bottom image edge."""
    image = texture_image(resolve_export_path(path.parent / detail["index"]), "Non-Color", "CHANNEL_PACKED")
    side = detail["size"] // 8
    if tuple(image.size) != (side, side):
        raise ValueError(f"Terrain index dimensions disagree with source extent: {image.name}, size={tuple(image.size)}, expected={side}")
    pixels = np.empty(side * side * 4, dtype=np.float32)
    image.pixels.foreach_get(pixels)
    if not np.isfinite(pixels).all():
        raise ValueError(f"Terrain index contains nonfinite pixels: {image.name}")
    values = np.rint(pixels * 255).astype(np.int16)
    if (np.max(np.abs(pixels * 255 - values)) > 0.001 or np.any(values < 0)
            or np.any(values > 255) or np.any(values % 4)):
        raise ValueError(f"Terrain index has invalid source ordinal encoding: {image.name}")
    return (values // 4).reshape(side, side, 4)


def polygon_cell_area(points: list[tuple[float, float]], left: float, right: float,
                      bottom: float, top: float) -> float:
    """Clip a terrain face to one index texel and ignore zero-area edge contact."""
    clipped = points
    for axis, sign, bound in ((0, 1.0, left), (0, -1.0, right),
                              (1, 1.0, bottom), (1, -1.0, top)):
        if not clipped:
            return 0.0
        result: list[tuple[float, float]] = []
        previous = clipped[-1]
        previous_distance = sign * (previous[axis] - bound)
        for current in clipped:
            current_distance = sign * (current[axis] - bound)
            if (previous_distance < 0) != (current_distance < 0):
                fraction = previous_distance / (previous_distance - current_distance)
                result.append((previous[0] + fraction * (current[0] - previous[0]),
                               previous[1] + fraction * (current[1] - previous[1])))
            if current_distance >= 0:
                result.append(current)
            previous, previous_distance = current, current_distance
        clipped = result
    return abs(sum(a[0] * b[1] - b[0] * a[1] for a, b in zip(clipped, clipped[1:] + clipped[:1]))) * 0.5


def face_layers(obj: bpy.types.Object, polygon: bpy.types.MeshPolygon, detail: TerrainDetail,
                indices: NDArray[np.int16]) -> frozenset[int]:
    """Collect every nearest-sampled index texel with positive face overlap."""
    points = []
    x0, x1, z0, z1 = detail["bounds"]
    for vertex in polygon.vertices:
        world = obj.matrix_world @ obj.data.vertices[vertex].co
        point = (float(world.x), float(-world.y))
        if not (x0-0.01 <= point[0] <= x1+0.01 and z0-0.01 <= point[1] <= z1+0.01):
            raise ValueError(f"Terrain face exceeds detail resource bounds: object={obj.name}, face={polygon.index}, point={point}")
        points.append(point)
    origin_x, origin_z = detail["position"][0], detail["position"][2]
    width = indices.shape[1]
    height = indices.shape[0]
    low_x = max(0, min(width-1, math.floor((min(point[0] for point in points)-origin_x) / 8)))
    high_x = max(0, min(width-1, math.floor((max(point[0] for point in points)-origin_x) / 8)))
    low_z = max(0, min(height-1, math.floor((min(point[1] for point in points)-origin_z) / 8)))
    high_z = max(0, min(height-1, math.floor((max(point[1] for point in points)-origin_z) / 8)))
    needed: set[int] = set()
    for row in range(low_z, high_z + 1):
        for column in range(low_x, high_x + 1):
            if polygon_cell_area(points, origin_x + column*8, origin_x + (column+1)*8,
                                 origin_z + row*8, origin_z + (row+1)*8) > 1e-10:
                needed.update(int(value) for value in indices[row, column])
    if not needed:
        raise ValueError(f"Terrain face has no positive index texel coverage: object={obj.name}, face={polygon.index}")
    return frozenset(needed)


def packed_layer_groups(faces: list[frozenset[int]]) -> list[tuple[int, ...]]:
    """Deterministically combine face requirements under the sampler budget."""
    groups: list[set[int]] = []
    for required in sorted(set(faces), key=lambda values: (-len(values), tuple(sorted(values)))):
        if len(required) > MAX_LAYERS_PER_MATERIAL:
            raise ValueError(f"One terrain face exceeds the layer sampler budget: layers={sorted(required)}, limit={MAX_LAYERS_PER_MATERIAL}")
        candidates = [(len(group | required), index) for index, group in enumerate(groups)
                      if len(group | required) <= MAX_LAYERS_PER_MATERIAL]
        if candidates:
            groups[min(candidates)[1]].update(required)
        else:
            groups.append(set(required))
    return sorted(tuple(sorted(group)) for group in groups)


def apply_scene_detail(scene: bpy.types.Scene, path: Path) -> list[dict[str, str]]:
    path = resolve_export_path(path)
    detail = load_terrain_detail(path)
    surface = resolve_export_path(Path(detail["source_surface"])) if "source_surface" in detail else None
    index = index_ordinals(path, detail)
    layer_lookup = {layer["ordinal"]: layer for layer in detail["layers"]}
    pending: dict[bpy.types.Material, list[tuple[bpy.types.Object, int, frozenset[int]]]] = {}
    adapted: dict[bpy.types.Material, tuple[int, ...]] = {}
    for obj in sorted(scene.objects, key=lambda item: item.name):
        if obj.type != "MESH" or "unity_tile" not in obj:
            continue
        mesh = obj.data
        for polygon in mesh.polygons:
            if polygon.material_index >= len(mesh.materials):
                raise ValueError(f"Terrain face has an invalid material slot: object={obj.name}, face={polygon.index}")
            material = mesh.materials[polygon.material_index]
            if (material is None or not material.get("endfield_terrain_surface_source")
                    or (surface is not None and resolve_export_path(Path(material["endfield_terrain_surface_source"])) != surface)):
                continue
            required = face_layers(obj, polygon, detail, index)
            absent = required - layer_lookup.keys()
            if absent:
                raise ValueError(f"Terrain source index references unprepared layers: object={obj.name}, face={polygon.index}, ordinals={sorted(absent)}")
            base_name = material.get("endfield_terrain_detail_base_material")
            if base_name is None:
                if material.get("endfield_terrain_detail_contract"):
                    raise ValueError(f"Terrain material was adapted without sampler partition metadata: {material.name}")
                pending.setdefault(material, []).append((obj, polygon.index, required))
            else:
                ordinals = tuple(int(value) for value in material["endfield_terrain_detail_layers"])
                if (len(ordinals) > MAX_LAYERS_PER_MATERIAL or not required.issubset(ordinals)
                        or material.get("endfield_terrain_detail_source") != str(path.resolve())
                        or bpy.data.materials.get(base_name) is None):
                    raise ValueError(f"Terrain partition no longer covers its source face: material={material.name}, object={obj.name}, face={polygon.index}, required={sorted(required)}")
                adapted[material] = ordinals
    if not pending and not adapted:
        raise ValueError("Scene contains no terrain tiles for detail reconstruction")
    results = {material: apply_detail_material(material, path, detail,
               [layer_lookup[ordinal] for ordinal in ordinals]) for material, ordinals in adapted.items()}
    for source, faces in sorted(pending.items(), key=lambda item: item[0].name):
        groups = packed_layer_groups([required for _, _, required in faces])
        if any(obj.data.users != 1 for obj, _, _ in faces):
            raise ValueError(f"Terrain material partition requires an unshared mesh: material={source.name}")
        materials: dict[tuple[int, ...], bpy.types.Material] = {}
        for ordinals in groups:
            identity = hashlib.sha256((source.name + detail_identity(path, ordinals)).encode()).hexdigest()[:24]
            name = "EF_Terrain_Detail_Part_" + identity
            material = bpy.data.materials.get(name)
            if material is None:
                material = source.copy()
                material.name = name
                material["endfield_terrain_detail_base_material"] = source.name
                material["endfield_terrain_detail_layers"] = list(ordinals)
            elif (material.get("endfield_terrain_detail_base_material") != source.name
                  or tuple(material.get("endfield_terrain_detail_layers", ())) != ordinals):
                raise ValueError(f"Terrain partition material name collision: {name}")
            results[material] = apply_detail_material(material, path, detail,
                                [layer_lookup[ordinal] for ordinal in ordinals])
            materials[ordinals] = material
        for obj, polygon_index, required in faces:
            ordinals = min((group for group in groups if required.issubset(group)), key=lambda group: (len(group), group))
            material = materials[ordinals]
            slots = obj.data.materials
            slot = next((index for index, item in enumerate(slots) if item == material), None)
            if slot is None:
                slots.append(material)
                slot = len(slots) - 1
            obj.data.polygons[polygon_index].material_index = slot
    return [{"material": material.name, "status": status}
            for material, status in sorted(results.items(), key=lambda item: item[0].name)]
