"""Project authenticated decals onto clipped copies of existing receiver triangles.

Run inside Blender. Original meshes/materials remain untouched. HGRP/DecalLit
DXBC confirms the local unit box, X/Z UV projection and packed RGBA tint.
Animated water decals are recorded as deferred, never counted as restored.
"""

from __future__ import annotations

import argparse
import array
import hashlib
import itertools
import json
import math
import sys
from collections import Counter, defaultdict
from dataclasses import dataclass
from pathlib import Path

import bpy
import numpy as np
from mathutils import Matrix, Vector

sys.path.insert(0, str(Path(__file__).parent))
from endfield_projected_decal_contract import ProjectedDecal


@dataclass(frozen=True)
class Receiver:
    mesh: bpy.types.Mesh
    matrix: Matrix
    minimum: tuple[float, ...]
    maximum: tuple[float, ...]
    name: str


@dataclass(frozen=True)
class MeshArrays:
    positions: np.ndarray
    triangles: np.ndarray
    loops: np.ndarray
    materials: np.ndarray
    uv: np.ndarray
    normals: np.ndarray


@dataclass(frozen=True)
class TextureSpec:
    property: str
    file: Path
    scale: tuple[float, ...]
    offset: tuple[float, ...]


@dataclass(frozen=True)
class MaterialSpec:
    container: str
    name: str
    floats: dict[str, float]
    textures: tuple[TextureSpec, ...]


def require_object(value: object) -> dict[str, object]:
    if not isinstance(value, dict) or not all(isinstance(key, str) for key in value):
        raise TypeError("Expected a JSON object with string keys")
    return value


def require_list(value: object) -> list[object]:
    if not isinstance(value, list):
        raise TypeError("Expected a JSON array")
    return value


def require_text(value: object) -> str:
    if not isinstance(value, str) or not value:
        raise TypeError("Expected a nonempty string")
    return value


def vector(value: object, length: int) -> tuple[float, ...]:
    values = require_list(value)
    if len(values) != length or not all(isinstance(v, (int, float)) and math.isfinite(v) for v in values):
        raise ValueError(f"Invalid finite vector, expected length={length}")
    return tuple(float(v) for v in values)


def read_decals(path: Path) -> tuple[ProjectedDecal, ...]:
    payload = require_object(json.loads(path.read_text(encoding="utf-8")))
    if payload["format"] != "EndfieldProjectedDecals/1":
        raise ValueError(f"DECAL_MANIFEST_FORMAT path={path}")
    result = []
    for raw in require_list(payload["records"]):
        row = require_object(raw)
        for name in ("entity_id", "packed_flags", "material_byte_offset"):
            if type(row[name]) is not int:
                raise TypeError(f"DECAL_INTEGER_FIELD name={name}")
        result.append(ProjectedDecal(
            row["entity_id"], require_text(row["entity_base"]), require_text(row["source_path"]),
            vector(row["matrix"], 16), vector(row["inverse_matrix"], 16),
            vector(row["uv_scale"], 2), vector(row["uv_offset"], 2), vector(row["color"], 4),
            vector([row["intensity"]], 1)[0], vector([row["sector_angle"]], 1)[0],
            row["packed_flags"], require_text(row["material_id"]), require_text(row["material_path"]),
            row["material_byte_offset"], require_text(row["raw_hex"])))
    if len({r.entity_id for r in result}) != len(result):
        raise ValueError("DECAL_DUPLICATE_ENTITY_IDS")
    return tuple(result)


def read_materials(path: Path) -> dict[str, MaterialSpec]:
    payload = require_object(json.loads(path.read_text(encoding="utf-8")))
    if payload["format"] != "EndfieldProjectedDecalMaterials/1":
        raise ValueError("DECAL_MATERIAL_LIBRARY_FORMAT")
    result = {}
    for raw in require_list(payload["Materials"]):
        entry = require_object(raw)
        record = require_object(entry["Record"])
        floats = {key: vector([value], 1)[0] for key, value in require_object(record["Floats"]).items()}
        textures = []
        for raw_slot in require_list(entry["TextureFiles"]):
            slot = require_object(raw_slot)
            file = Path(require_text(slot["File"]))
            if not file.is_file():
                raise FileNotFoundError(file)
            textures.append(TextureSpec(require_text(slot["Property"]), file,
                                        vector(slot["Scale"], 2), vector(slot["Offset"], 2)))
        container = require_text(entry["Container"]).casefold()
        if container in result:
            raise ValueError(f"DECAL_DUPLICATE_MATERIAL container={container}")
        result[container] = MaterialSpec(container, require_text(record["Name"]), floats, tuple(textures))
    return result


def shader_matrix(values: tuple[float, ...]) -> Matrix:
    return Matrix(tuple(tuple(values[c * 4 + r] for c in range(4)) for r in range(4)))


def mesh_arrays(mesh: bpy.types.Mesh) -> MeshArrays:
    mesh.calc_loop_triangles()
    positions = np.empty(len(mesh.vertices) * 3, dtype=np.float32)
    mesh.vertices.foreach_get("co", positions)
    triangles = np.empty(len(mesh.loop_triangles) * 3, dtype=np.int32)
    mesh.loop_triangles.foreach_get("vertices", triangles)
    loops = np.empty_like(triangles)
    mesh.loop_triangles.foreach_get("loops", loops)
    materials = np.empty(len(mesh.loop_triangles), dtype=np.int32)
    mesh.loop_triangles.foreach_get("material_index", materials)
    uv = np.zeros(len(mesh.loops) * 2, dtype=np.float32)
    surface_uv = mesh.uv_layers.active
    if any(material and material.get("endfield_terrain_surface_source") for material in mesh.materials):
        surface_uv = mesh.uv_layers.get("TerrainWorldUV")
        if surface_uv is None:
            raise ValueError(f"Terrain receiver lacks authored atlas UV: mesh={mesh.name}")
    if surface_uv is not None:
        surface_uv.data.foreach_get("uv", uv)
    normals = np.empty(len(mesh.corner_normals) * 3, dtype=np.float32)
    mesh.corner_normals.foreach_get("vector", normals)
    return MeshArrays(positions.reshape(-1, 3), triangles.reshape(-1, 3), loops.reshape(-1, 3),
                      materials, uv.reshape(-1, 2), normals.reshape(-1, 3))


def grid_cells(minimum: tuple[float, ...], maximum: tuple[float, ...]) -> tuple[tuple[int, ...], ...]:
    spans = [range(math.floor(a / 32), math.floor(b / 32) + 1) for a, b in zip(minimum, maximum, strict=True)]
    return tuple(itertools.product(*spans))


def scene_terrain_objects() -> tuple[bpy.types.Object, ...]:
    """Use the shared terrain tile contract independently of collection names."""
    return tuple(obj for obj in bpy.context.scene.objects if obj.type == "MESH" and "unity_tile" in obj)


def receiver_index() -> tuple[list[Receiver], dict[tuple[int, ...], list[int]]]:
    terrain = set(scene_terrain_objects())
    receivers = []
    index: dict[tuple[int, ...], list[int]] = defaultdict(list)
    for item in bpy.context.evaluated_depsgraph_get().object_instances:
        obj = item.object
        direct_entity = ("entity_id" in obj.original and obj.original.get("source_asset")
                         and "instance_count" not in obj.original and not obj.hide_render)
        if obj.type != "MESH" or not (item.is_instance or obj.original in terrain or direct_entity):
            continue
        if not len(obj.data.polygons):
            continue
        matrix = item.matrix_world.copy()
        corners = [matrix @ Vector(c) for c in obj.bound_box]
        low = tuple(min(v[i] for v in corners) for i in range(3))
        high = tuple(max(v[i] for v in corners) for i in range(3))
        cells = grid_cells(low, high)
        receiver_id = len(receivers)
        receivers.append(Receiver(obj.data, matrix, low, high, obj.name))
        for cell in cells:
            index[cell].append(receiver_id)
    return receivers, dict(index)


def clip_triangle(vertices: np.ndarray) -> list[np.ndarray]:
    """Clip actual triangles and interpolate receiver UV/normals at boundaries."""
    polygon = list(vertices)
    for axis, side in itertools.product(range(3), (-1.0, 1.0)):
        clipped = []
        if not polygon:
            break
        for a, b in zip(polygon, polygon[1:] + polygon[:1], strict=True):
            da, db = 0.5 - side * a[axis], 0.5 - side * b[axis]
            if da >= 0:
                clipped.append(a)
            if (da >= 0) != (db >= 0):
                clipped.append(a + (b - a) * (da / (da - db)))
        polygon = clipped
    return polygon


def input_socket(nodes: bpy.types.Nodes, links: bpy.types.NodeLinks,
                 socket: bpy.types.NodeSocket) -> bpy.types.NodeSocket:
    if socket.is_linked:
        return socket.links[0].from_socket
    if socket.type == "RGBA":
        constant = nodes.new("ShaderNodeRGB")
    else:
        constant = nodes.new("ShaderNodeValue")
    constant.outputs[0].default_value = socket.default_value
    return constant.outputs[0]


def multiply(nodes: bpy.types.Nodes, links: bpy.types.NodeLinks,
             a: bpy.types.NodeSocket, b: bpy.types.NodeSocket | float) -> bpy.types.NodeSocket:
    node = nodes.new("ShaderNodeMath")
    node.operation = "MULTIPLY"
    links.new(a, node.inputs[0])
    if isinstance(b, float):
        node.inputs[1].default_value = b
    else:
        links.new(b, node.inputs[1])
    return node.outputs[0]


def preserve_receiver_alpha(material: bpy.types.Material, source: bpy.types.Material) -> None:
    """Keep the receiver's cutout coverage when applying a projected layer."""
    if material.get("projected_receiver_alpha_preserved"):
        raise ValueError(f"DECAL_ALPHA_ALREADY_PRESERVED material={material.name}")
    projected_bsdf = [n for n in material.node_tree.nodes if n.type == "BSDF_PRINCIPLED"]
    if len(projected_bsdf) != 1:
        raise ValueError(f"DECAL_ALPHA_SHADER_UNSUPPORTED material={material.name}")
    nodes, links = material.node_tree.nodes, material.node_tree.links
    # Projected meshes store the interpolated receiver UVs in their UVMap layer.
    if source.get("endfield_terrain_surface_source"):
        for node in nodes:
            if node.type in ("UVMAP", "NORMAL_MAP") and node.uv_map == "TerrainWorldUV":
                node.uv_map = "UVMap"
    if source.use_nodes:
        original_bsdf = [n for n in source.node_tree.nodes if n.type == "BSDF_PRINCIPLED"]
        if len(original_bsdf) != 1:
            raise ValueError(f"DECAL_ALPHA_SHADER_UNSUPPORTED material={source.name}")
        receiver_alpha = original_bsdf[0].inputs["Alpha"]
        if receiver_alpha.is_linked:
            link = receiver_alpha.links[0]
            coverage = nodes[link.from_node.name].outputs[link.from_socket.name]
        else:
            coverage = float(receiver_alpha.default_value)
    else:
        coverage = float(source.diffuse_color[3])
    target = projected_bsdf[0].inputs["Alpha"]
    opacity = input_socket(nodes, links, target)
    links.new(multiply(nodes, links, opacity, coverage), target)
    material["projected_receiver_alpha_preserved"] = True


def projected_material(source: bpy.types.Material, spec: MaterialSpec) -> bpy.types.Material:
    if source.get("endfield_runtime_role") == "no_non_shadow_draw_for_submesh":
        # Retain the same non-drawing role as the receiver, including its geometry.
        # A projected layer must never turn an unsubmitted submesh into a surface.
        outputs = [node for node in source.node_tree.nodes if node.type == "OUTPUT_MATERIAL" and node.is_active_output] if source.use_nodes else []
        if len(outputs) != 1 or len(outputs[0].inputs["Surface"].links) != 1 or outputs[0].inputs["Surface"].links[0].from_node.type != "BSDF_TRANSPARENT":
            raise ValueError(f"DECAL_UNDRAWN_RECEIVER_ROLE_MODIFIED material={source.name}")
        original = source.original
        if bpy.data.materials.get(original.name) != original:
            raise ValueError(f"DECAL_UNDRAWN_RECEIVER_NOT_IN_MAIN material={source.name}")
        return original
    material = source.copy()
    material.name = "Projected_" + spec.name + "_" + source.name
    material.use_nodes = True
    nodes, links = material.node_tree.nodes, material.node_tree.links
    principled = [n for n in nodes if n.type == "BSDF_PRINCIPLED"]
    if len(principled) != 1:
        raise ValueError(f"DECAL_RECEIVER_SHADER_UNSUPPORTED material={source.name}")
    bsdf = principled[0]
    original_color = input_socket(nodes, links, bsdf.inputs["Base Color"])
    uv = nodes.new("ShaderNodeUVMap")
    uv.uv_map = "DecalUV"
    tint = nodes.new("ShaderNodeVertexColor")
    tint.layer_name = "DecalTint"
    slots = {s.property: s for s in spec.textures}
    if "_BaseColorMap" not in slots:
        raise ValueError(f"DECAL_BASE_TEXTURE_MISSING material={spec.name}")
    images = {}
    for key in ("_BaseColorMap", "_SampleTex1"):
        if key not in slots:
            continue
        slot = slots[key]
        texture = nodes.new("ShaderNodeTexImage")
        texture.label = key
        image_name = "ProjectedImage_" + hashlib.sha256((str(slot.file) + key).encode()).hexdigest()[:24]
        owned_image = bpy.data.images.get(image_name)
        if owned_image is None:
            owned_image = bpy.data.images.load(str(slot.file), check_existing=True).copy()
            owned_image.name = image_name
            owned_image.alpha_mode = "STRAIGHT" if key == "_BaseColorMap" else "CHANNEL_PACKED"
            owned_image.colorspace_settings.name = "sRGB" if key == "_BaseColorMap" and spec.floats["_BaseTexChannel"] == 0 else "Non-Color"
        texture.image = owned_image
        if not texture.image.packed_file:
            texture.image.pack()
        # DXBC selects instance UV unchanged when _SampleTex1UVSet is zero;
        # the serialized sample texture ST is inactive in that branch.
        if key == "_SampleTex1" and spec.floats["_SampleTex1UVSet"] != 0:
            raise ValueError(f"DECAL_SAMPLE_UV_SET_UNSUPPORTED material={spec.name}")
        if key == "_BaseColorMap" and (slot.scale != (1.0, 1.0) or slot.offset != (0.0, 0.0)):
            raise ValueError(f"DECAL_TEXTURE_ST_UNSUPPORTED material={spec.name} property={key}")
        links.new(uv.outputs["UV"], texture.inputs["Vector"])
        images[key] = texture
    base = images["_BaseColorMap"]
    mask = multiply(nodes, links, base.outputs["Alpha"], tint.outputs["Alpha"])
    mask = multiply(nodes, links, mask, spec.floats["_DecalOpacity"])
    color = nodes.new("ShaderNodeMixRGB")
    color.blend_type = "MULTIPLY"
    color.inputs[0].default_value = 1
    links.new(tint.outputs["Color"], color.inputs[1])
    if spec.floats["_BaseTexChannel"] == 0:
        links.new(base.outputs["Color"], color.inputs[2])
    else:
        color.inputs[2].default_value = (1, 1, 1, 1)
    mix = nodes.new("ShaderNodeMixRGB")
    mix.inputs[0].default_value = spec.floats["_BlendAlbedo"]
    links.new(original_color, mix.inputs[1])
    links.new(color.outputs[0], mix.inputs[2])
    links.new(mix.outputs[0], bsdf.inputs["Base Color"])
    links.new(mask, bsdf.inputs["Alpha"])
    material.surface_render_method = "BLENDED"
    material.use_transparency_overlap = False
    material.use_transparent_shadow = False
    material.use_backface_culling = False
    nro = base if spec.floats["_BaseTexChannel"] != 0 else images.get("_SampleTex1")
    if nro is not None and (spec.floats["_UseSampleTex1"] > 0 or spec.floats["_BaseTexChannel"] != 0):
        separate = nodes.new("ShaderNodeSeparateColor")
        links.new(nro.outputs["Color"], separate.inputs[0])
        if spec.floats["_AffectRoughness"] > 0:
            scale = nodes.new("ShaderNodeMapRange")
            scale.inputs["To Min"].default_value = spec.floats["_RoughnessMin"]
            scale.inputs["To Max"].default_value = spec.floats["_RoughnessMax"]
            links.new(separate.outputs["Blue"], scale.inputs["Value"])
            links.new(scale.outputs["Result"], bsdf.inputs["Roughness"])
        if spec.floats["_AffectNormal"] > 0:
            components = []
            for channel in ("Red", "Green"):
                signed = nodes.new("ShaderNodeMath")
                signed.operation = "MULTIPLY_ADD"
                signed.inputs[1].default_value = 2
                signed.inputs[2].default_value = -1
                links.new(separate.outputs[channel], signed.inputs[0])
                components.append(signed.outputs[0])
            x2, y2 = [multiply(nodes, links, c, c) for c in components]
            subtract = nodes.new("ShaderNodeMath")
            subtract.operation = "SUBTRACT"
            subtract.inputs[0].default_value = 1
            sum_node = nodes.new("ShaderNodeMath")
            sum_node.operation = "ADD"
            links.new(x2, sum_node.inputs[0]); links.new(y2, sum_node.inputs[1])
            links.new(sum_node.outputs[0], subtract.inputs[1])
            clamp = nodes.new("ShaderNodeMath"); clamp.operation = "MAXIMUM"
            links.new(subtract.outputs[0], clamp.inputs[0]); clamp.inputs[1].default_value = 0
            sqrt = nodes.new("ShaderNodeMath"); sqrt.operation = "SQRT"
            links.new(clamp.outputs[0], sqrt.inputs[0])
            blue = nodes.new("ShaderNodeMath"); blue.operation = "MULTIPLY_ADD"
            links.new(sqrt.outputs[0], blue.inputs[0]); blue.inputs[1].default_value = 0.5; blue.inputs[2].default_value = 0.5
            combine = nodes.new("ShaderNodeCombineColor")
            links.new(separate.outputs["Red"], combine.inputs["Red"])
            links.new(separate.outputs["Green"], combine.inputs["Green"])
            links.new(blue.outputs[0], combine.inputs["Blue"])
            normal = nodes.new("ShaderNodeNormalMap")
            normal.uv_map = "DecalUV"
            normal.inputs["Strength"].default_value = spec.floats["_NormalScale"]
            links.new(combine.outputs[0], normal.inputs["Color"])
            links.new(normal.outputs["Normal"], bsdf.inputs["Normal"])
    material["projected_decal_material"] = spec.container
    material["receiver_material"] = source.name
    preserve_receiver_alpha(material, source)
    from blender_hgrp_surface import refresh_surface_adapters
    refresh_surface_adapters(material)
    return material


def project_decal(decal: ProjectedDecal, spec: MaterialSpec, receivers: list[Receiver],
                  grid: dict[tuple[int, ...], list[int]], arrays: dict[int, MeshArrays],
                  materials: dict[tuple[str, str], bpy.types.Material],
                  collection: bpy.types.Collection, bias: float) -> tuple[int, int]:
    basis = Matrix(((1, 0, 0, 0), (0, 0, -1, 0), (0, 1, 0, 0), (0, 0, 0, 1)))
    world = basis @ shader_matrix(decal.matrix)
    inverse = shader_matrix(decal.inverse_matrix) @ basis.inverted()
    corners = [world @ Vector(c) for c in itertools.product((-0.5, 0.5), repeat=3)]
    low = tuple(min(v[i] for v in corners) for i in range(3))
    high = tuple(max(v[i] for v in corners) for i in range(3))
    candidates = sorted({i for cell in grid_cells(low, high) for i in grid.get(cell, ())})
    vertices, faces, uv_surface, uv_decal, normals, material_ids = [], [], [], [], [], []
    used_materials: list[bpy.types.Material] = []
    hits = 0
    for candidate in candidates:
        receiver = receivers[candidate]
        if any(a > b or c < d for a, b, c, d in zip(low, receiver.maximum, high, receiver.minimum, strict=True)):
            continue
        key = receiver.mesh.as_pointer()
        if key not in arrays:
            arrays[key] = mesh_arrays(receiver.mesh)
        data = arrays[key]
        transform = np.array(inverse @ receiver.matrix, dtype=np.float64)
        projected = data.positions @ transform[:3, :3].T + transform[:3, 3]
        triangle_positions = projected[data.triangles]
        overlaps = np.all(triangle_positions.min(axis=1) < 0.5, axis=1) & np.all(triangle_positions.max(axis=1) > -0.5, axis=1)
        selection = np.flatnonzero(overlaps)
        if not len(selection):
            continue
        normal_matrix = np.array(receiver.matrix.to_3x3().inverted().transposed(), dtype=np.float64)
        fully_inside = np.all(np.abs(triangle_positions) <= 0.5, axis=(1, 2))
        receiver_matrix = np.array(receiver.matrix, dtype=np.float64)
        world_positions = data.positions @ receiver_matrix[:3, :3].T + receiver_matrix[:3, 3]
        selected_loops = data.loops[selection]
        attributes = np.concatenate((triangle_positions[selection], data.uv[selected_loops],
            data.normals[selected_loops] @ normal_matrix.T, world_positions[data.triangles[selection]]), axis=2)
        inside = fully_inside[selection]
        fragments = [attributes[inside]]
        fragment_slots = [data.materials[selection[inside]]]
        hits += int(inside.sum())
        clipped_triangles, clipped_slots = [], []
        for row in np.flatnonzero(~inside):
            polygon = clip_triangle(attributes[row])
            if len(polygon) >= 3:
                hits += 1
                for i in range(1, len(polygon) - 1):
                    clipped_triangles.append((polygon[0], polygon[i], polygon[i + 1]))
                    clipped_slots.append(data.materials[selection[row]])
        if clipped_triangles:
            fragments.append(np.array(clipped_triangles))
            fragment_slots.append(np.array(clipped_slots))
        triangles = np.concatenate(fragments, axis=0)
        source_slots = np.concatenate(fragment_slots)
        cross = np.cross(triangles[:, 1, 8:11] - triangles[:, 0, 8:11], triangles[:, 2, 8:11] - triangles[:, 0, 8:11])
        nondegenerate = np.einsum("ij,ij->i", cross, cross) >= 1e-24
        triangles, source_slots = triangles[nondegenerate], source_slots[nondegenerate]
        face_normals = cross[nondegenerate]
        face_normals /= np.linalg.norm(face_normals, axis=1)[:, None]
        for source_slot in np.unique(source_slots):
            source = receiver.mesh.materials[int(source_slot)]
            if source is None:
                raise ValueError(f"DECAL_RECEIVER_MATERIAL_MISSING mesh={receiver.mesh.name}")
            material_key = (source.name, spec.container)
            if material_key not in materials:
                materials[material_key] = projected_material(source, spec)
            mat = materials[material_key]
            if mat not in used_materials:
                used_materials.append(mat)
            slot = used_materials.index(mat)
            values = triangles[source_slots == source_slot].reshape(-1, 11)
            transformed_normals = values[:, 5:8]
            lengths = np.linalg.norm(transformed_normals, axis=1)
            # Blender's split-normal API uses zero vectors to request automatic normals.
            unit_normals = np.divide(transformed_normals, lengths[:, None],
                                     out=np.zeros_like(transformed_normals), where=lengths[:, None] > 0)
            offsets = np.repeat(face_normals[source_slots == source_slot], 3, axis=0)
            start = len(vertices)
            vertices.extend((values[:, 8:11] + offsets * bias).tolist())
            normals.extend(unit_normals.tolist())
            uv_surface.extend(values[:, 3:5].tolist())
            uv_decal.extend(((values[:, (0, 2)] + 0.5) * np.array(decal.uv_scale) + np.array(decal.uv_offset)).tolist())
            faces.extend(np.arange(start, len(vertices)).reshape(-1, 3).tolist())
            material_ids.extend([slot] * (len(values) // 3))
    if not faces:
        return 0, 0
    mesh = bpy.data.meshes.new(f"ProjectedDecal_{decal.entity_id}")
    origin = world.translation
    mesh.from_pydata([tuple(Vector(point) - origin) for point in vertices], [], faces)
    for material in used_materials:
        mesh.materials.append(material)
    mesh.polygons.foreach_set("material_index", material_ids)
    mesh.uv_layers.new(name="UVMap").data.foreach_set("uv", np.array(uv_surface, dtype=np.float32).ravel())
    mesh.uv_layers.new(name="DecalUV").data.foreach_set("uv", np.array(uv_decal, dtype=np.float32).ravel())
    mesh.uv_layers["UVMap"].active_render = True
    mesh.uv_layers.active_index = 0
    tint = mesh.color_attributes.new(name="DecalTint", type="FLOAT_COLOR", domain="CORNER")
    color = (*[c * decal.intensity for c in decal.color[:3]], decal.color[3])
    tint.data.foreach_set("color", np.tile(np.array(color, dtype=np.float32), len(mesh.loops)))
    mesh.normals_split_custom_set(normals)
    mesh.update()
    obj = bpy.data.objects.new(f"DECAL_{decal.entity_id}_{decal.entity_base}", mesh)
    obj.location = origin
    collection.objects.link(obj)
    obj["projected_decal_entity_id"] = decal.entity_id
    obj["entity_base"] = decal.entity_base
    obj["material_id"] = decal.material_id
    obj["source_path"] = decal.source_path
    obj["receiver_triangle_count"] = hits
    obj["undrawn_receiver_triangle_count"] = sum(
        material_ids.count(index) for index, material in enumerate(used_materials)
        if material.get("endfield_runtime_role") == "no_non_shadow_draw_for_submesh")
    obj["authored_draw_order"] = spec.floats["_DrawOrder"]
    return len(faces), hits


def terrain_hash() -> str:
    digest = hashlib.sha256()
    for obj in sorted(scene_terrain_objects(), key=lambda o: o.name):
        digest.update(obj.name.encode())
        coordinates = array.array("f", [0.0]) * (len(obj.data.vertices) * 3)
        obj.data.vertices.foreach_get("co", coordinates)
        digest.update(coordinates.tobytes())
        indices = array.array("i", [0]) * len(obj.data.loops)
        obj.data.loops.foreach_get("vertex_index", indices)
        digest.update(indices.tobytes())
    return digest.hexdigest()


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    for name in ("blend", "decal-manifest", "materials", "output", "audit"):
        parser.add_argument("--" + name, type=Path, required=True)
    parser.add_argument("--bounds", type=float, nargs=4, required=True)
    parser.add_argument("--surface-offset", type=float, required=True)
    args = parser.parse_args(sys.argv[sys.argv.index("--") + 1:])
    if args.output.exists() or args.audit.exists():
        raise FileExistsError("Projected decal output/audit must be new")
    if not math.isfinite(args.surface_offset) or not 0 < args.surface_offset < 0.01:
        raise ValueError("DECAL_SURFACE_OFFSET_OUT_OF_RANGE expected=(0,0.01)")
    decals = read_decals(args.decal_manifest)
    xmin, xmax, zmin, zmax = args.bounds
    decals = tuple(r for r in decals if xmin <= r.matrix[12] < xmax and zmin <= r.matrix[14] < zmax)
    specs = read_materials(args.materials)
    bpy.ops.wm.open_mainfile(filepath=str(args.blend))
    before_hash = terrain_hash()
    original_meshes = {o.name: (o.data, len(o.data.vertices), len(o.data.polygons)) for o in bpy.data.objects if o.type == "MESH"}
    receivers, grid = receiver_index()
    collection = bpy.data.collections.new("Endfield_Projected_Decals")
    bpy.context.scene.collection.children.link(collection)
    arrays: dict[int, MeshArrays] = {}
    materials: dict[tuple[str, str], bpy.types.Material] = {}
    restored = []
    outcomes = []
    decals = tuple(sorted(decals, key=lambda r: (specs[r.material_path.casefold()].floats["_DrawOrder"], r.entity_id)))
    minimum_order = min((specs[r.material_path.casefold()].floats["_DrawOrder"] for r in decals), default=0)
    for index, decal in enumerate(decals):
        spec = specs[decal.material_path.casefold()]
        if spec.floats.get("_UseWaterFlow", 0) > 0 or spec.floats.get("_UseWetness", 0) > 0:
            outcomes.append({"entity_id": decal.entity_id, "entity_base": decal.entity_base, "status": "deferred_water_effect"})
            continue
        for feature in ("_UseSludgeDecal", "_UseDecalFootPrint", "_UVNoiseStrength", "_UVArcStrengthX", "_UVArcStrengthY"):
            if spec.floats.get(feature, 0) != 0:
                raise ValueError(f"DECAL_FEATURE_UNSUPPORTED entity_id={decal.entity_id} feature={feature}")
        if decal.packed_flags != 0:
            raise ValueError(f"DECAL_FLAGS_UNSUPPORTED entity_id={decal.entity_id} flags={decal.packed_flags}")
        bias = args.surface_offset + (spec.floats["_DrawOrder"] - minimum_order) * 0.00002
        polygons, source_triangles = project_decal(decal, spec, receivers, grid, arrays, materials, collection, bias)
        status = "restored" if polygons else "no_receiver_surface"
        outcomes.append({"entity_id": decal.entity_id, "entity_base": decal.entity_base,
                         "status": status, "polygons": polygons, "receiver_triangles": source_triangles})
        if polygons:
            restored.append(decal)
        if index % 25 == 0:
            print(json.dumps({"event": "decal_projection", "done": index + 1, "total": len(decals), "restored": len(restored)}), flush=True)
    for name, (mesh, vertices, polygons) in original_meshes.items():
        obj = bpy.data.objects[name]
        if obj.data != mesh or len(mesh.vertices) != vertices or len(mesh.polygons) != polygons:
            raise ValueError(f"DECAL_ORIGINAL_GEOMETRY_CHANGED object={name}")
    if terrain_hash() != before_hash:
        raise ValueError("DECAL_TERRAIN_CHANGED")
    unresolved = bpy.data.objects["Unresolved_Entity_Positions"]
    remove = Counter(tuple(round(x, 4) for x in (r.matrix[12], -r.matrix[14], r.matrix[13])) for r in restored)
    kept = []
    for vertex in unresolved.data.vertices:
        key = tuple(round(x, 4) for x in vertex.co)
        if remove[key]:
            remove[key] -= 1
        else:
            kept.append(tuple(vertex.co))
    if any(remove.values()):
        raise ValueError(f"DECAL_UNRESOLVED_POINTS_NOT_FOUND remaining={sum(remove.values())}")
    mesh = bpy.data.meshes.new("unresolved_after_projected_decals")
    mesh.from_pydata(kept, [], [])
    unresolved.data = mesh
    unresolved["unresolved_count"] = len(kept)
    base_counts_available = "unresolved_base_counts_json" in unresolved
    if base_counts_available:
        remaining_counts = Counter(json.loads(unresolved["unresolved_base_counts_json"]))
        remaining_counts.subtract(Counter(r.entity_base for r in restored))
        if any(count < 0 for count in remaining_counts.values()) or sum(remaining_counts.values()) != len(kept):
            raise ValueError("DECAL_UNRESOLVED_BASE_COUNTS_MISMATCH")
        unresolved["unresolved_base_counts_json"] = json.dumps({base: count for base, count in remaining_counts.items() if count})
    scene = bpy.context.scene
    scene["resolved_instance_count"] += len(restored)
    if "base_layer_resolved_instance_count" in scene:
        scene["base_layer_resolved_instance_count"] += len(restored)
    if "base_layer_unresolved_instance_count" in scene:
        scene["base_layer_unresolved_instance_count"] = len(kept)
    scene["unresolved_instance_count"] = len(kept)
    scene["projected_decal_count"] = len(restored)
    scene["projected_decal_manifest"] = str(args.decal_manifest)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    bpy.ops.wm.save_as_mainfile(filepath=str(args.output), compress=True)
    bpy.ops.wm.open_mainfile(filepath=str(args.output))
    if terrain_hash() != before_hash or len(bpy.data.collections["Endfield_Projected_Decals"].objects) != len(restored):
        raise ValueError("DECAL_REOPEN_VALIDATION_FAILED")
    if len(bpy.data.libraries):
        raise ValueError("DECAL_EXTERNAL_BLEND_LIBRARY")
    args.audit.parent.mkdir(parents=True, exist_ok=True)
    args.audit.write_text(json.dumps({"format": "EndfieldProjectedDecalBuildAudit/1", "input": str(args.blend),
        "output": str(args.output), "eligible": len(decals), "restored": len(restored), "unresolved": len(kept),
        "terrain_unchanged": True, "original_geometry_unchanged": True, "surface_offset": args.surface_offset,
        "unresolved_base_counts_available": base_counts_available,
        "status_counts": dict(Counter(r["status"] for r in outcomes)), "outcomes": outcomes}, indent=2), encoding="utf-8")
    print(json.dumps({"event": "projected_decals_verified", "restored": len(restored), "output": str(args.output)}), flush=True)


if __name__ == "__main__":
    main()
