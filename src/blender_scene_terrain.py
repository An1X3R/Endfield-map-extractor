"""Adapt retained HGTerrain base-normal atlases without changing receiver geometry."""
from __future__ import annotations

import json
import hashlib
from pathlib import Path
from typing import TypedDict
from collections.abc import Mapping

import bpy
import numpy as np
from numpy.typing import NDArray

from blender_hgrp_surface import packed_normal_group


class TerrainVertexNormals(TypedDict):
    object: str
    status: str
    corners: int
    max_storage_error: float
    max_storage_angle_degrees: float
    max_roundtrip_error: float


def normal_atlas_binding(material: bpy.types.Material) -> tuple[bpy.types.ShaderNodeTexImage, str, NDArray[np.float64], NDArray[np.float64]]:
    """Resolve the existing atlas and its explicit original or cropped UV binding."""
    surface = Path(material["endfield_terrain_surface_source"])
    manifest = json.loads(surface.read_text(encoding="utf-8"))
    paths = {(surface.parent / manifest[key]).resolve() for key in ("normal", "normal_rgba") if key in manifest}
    nodes = [node for node in material.node_tree.nodes if node.type == "TEX_IMAGE" and node.image
             and Path(node.image.get("endfield_terrain_crop_source", bpy.path.abspath(node.image.filepath))).resolve() in paths]
    if len(nodes) != 1:
        raise ValueError(f"Terrain mesh normals require one source atlas: {material.name}, count={len(nodes)}")
    texture = nodes[0]
    if (texture.image.colorspace_settings.name != "Non-Color" or texture.interpolation != "Linear"
            or texture.extension not in ("CLIP", "EXTEND", "REPEAT")):
        raise ValueError(f"Unsupported terrain normal atlas sampling: {material.name}")
    if len(texture.inputs["Vector"].links) != 1:
        raise ValueError(f"Terrain normal atlas has no unique coordinate binding: {material.name}")
    coordinate = texture.inputs["Vector"].links[0].from_node
    origin, factor = np.zeros(2, dtype=np.float64), np.ones(2, dtype=np.float64)
    if material.get("endfield_terrain_crop_contract") == "TerrainAtlasDensity/1":
        if (coordinate.type != "VECT_MATH" or coordinate.operation != "MULTIPLY"
                or len(coordinate.inputs[0].links) != 1 or coordinate.inputs[1].is_linked):
            raise ValueError(f"Unsupported terrain crop scale: {material.name}")
        factor = np.array(coordinate.inputs[1].default_value[:2], dtype=np.float64)
        offset = coordinate.inputs[0].links[0].from_node
        if (offset.type != "VECT_MATH" or offset.operation != "SUBTRACT"
                or len(offset.inputs[0].links) != 1 or offset.inputs[1].is_linked):
            raise ValueError(f"Unsupported terrain crop offset: {material.name}")
        origin = np.array(offset.inputs[1].default_value[:2], dtype=np.float64)
        coordinate = offset.inputs[0].links[0].from_node
    if coordinate.type != "UVMAP" or not coordinate.uv_map:
        raise ValueError(f"Terrain mesh normals require an explicit UV map: {material.name}")
    return texture, coordinate.uv_map, origin, factor


def sample_atlas_normals(pixels: NDArray[np.float32], uv: NDArray[np.float64], extension: str) -> NDArray[np.float64]:
    """Bilinearly decode retained RG samples into normalized Blender world normals."""
    if pixels.ndim != 3 or pixels.shape[2] != 4 or uv.ndim != 2 or uv.shape[1] != 2:
        raise ValueError(f"Invalid terrain normal sampling arrays: pixels={pixels.shape}, uv={uv.shape}")
    if not np.isfinite(uv).all() or extension not in ("CLIP", "EXTEND", "REPEAT"):
        raise ValueError(f"Invalid terrain atlas coordinates or extension: {extension}")
    height, width = pixels.shape[:2]
    xy = uv * np.array((width, height)) - .5
    low = np.floor(xy).astype(np.int64)
    fraction = xy - low
    sampled = np.zeros((len(uv), 2), dtype=np.float64)
    for dx, dy in ((0, 0), (1, 0), (0, 1), (1, 1)):
        x, y = low[:, 0] + dx, low[:, 1] + dy
        weight = (fraction[:, 0] if dx else 1-fraction[:, 0]) * (fraction[:, 1] if dy else 1-fraction[:, 1])
        if extension == "REPEAT":
            x, y = x % width, y % height
        else:
            if extension == "CLIP":
                weight = weight * ((x >= 0) & (x < width) & (y >= 0) & (y < height))
            x, y = np.clip(x, 0, width-1), np.clip(y, 0, height-1)
        sampled += pixels[y, x, :2] * weight[:, None]
    if not np.isfinite(sampled).all():
        raise ValueError("Terrain atlas contains nonfinite normal samples")
    signed = sampled * 2 - 1
    normals = np.column_stack((signed[:, 0], -signed[:, 1], np.sqrt(np.maximum(0, 1-np.sum(signed*signed, axis=1)))))
    return normals / np.linalg.norm(normals, axis=1)[:, None]


def atlas_pixels(image: bpy.types.Image) -> NDArray[np.float32]:
    width, height = image.size
    if width < 1 or height < 1:
        raise ValueError(f"Terrain normal atlas has no image data: {image.name}")
    pixels = np.empty(width*height*4, dtype=np.float32)
    image.pixels.foreach_get(pixels)
    return pixels.reshape(height, width, 4)


def adapt_terrain_mesh_normals(obj: bpy.types.Object,
                               images: Mapping[bpy.types.Image, NDArray[np.float32]]) -> TerrainVertexNormals:
    """Align mesh shading normals with the same source field used by its material."""
    if obj.type != "MESH" or "unity_tile" not in obj:
        raise ValueError(f"Source mesh normals require a retained terrain tile: {obj.name}")
    mesh = obj.data
    if mesh.users != 1 or obj.modifiers:
        raise ValueError(f"Terrain normal adaptation requires an unmodified, unshared source mesh: {obj.name}")
    expected = np.empty((len(mesh.loops), 3), dtype=np.float64)
    if not len(expected):
        raise ValueError(f"Terrain tile has no face corners: {obj.name}")
    transform = np.asarray(obj.matrix_world.to_3x3(), dtype=np.float64)
    if not np.isfinite(transform).all() or abs(np.linalg.det(transform)) < 1e-12:
        raise ValueError(f"Terrain mesh has a singular or nonfinite transform: {obj.name}")
    for slot in sorted({polygon.material_index for polygon in mesh.polygons}):
        material = mesh.materials[slot]
        if material is None or not material.get("endfield_terrain_surface_source"):
            raise ValueError(f"Terrain face has no retained normal source: {obj.name}, slot={slot}")
        texture, uv_name, origin, factor = normal_atlas_binding(material)
        layer = mesh.uv_layers.get(uv_name)
        if layer is None:
            raise ValueError(f"Terrain mesh lacks source UVs: {obj.name}, uv={uv_name}")
        indices = np.array([index for polygon in mesh.polygons if polygon.material_index == slot
                            for index in polygon.loop_indices], dtype=np.int64)
        coordinates = np.empty(len(mesh.loops)*2, dtype=np.float32)
        layer.data.foreach_get("uv", coordinates)
        uv = (coordinates.reshape(-1, 2)[indices].astype(np.float64)-origin)*factor
        image = texture.image
        world_normals = sample_atlas_normals(images[image], uv, texture.extension)
        local_normals = world_normals @ transform
        expected[indices] = local_normals / np.linalg.norm(local_normals, axis=1)[:, None]
    if not np.isfinite(expected).all():
        raise ValueError(f"Terrain source normals are nonfinite: {obj.name}")
    digest = hashlib.sha256(expected.astype(np.float32).tobytes()).hexdigest()
    contract = mesh.get("endfield_terrain_vertex_normal_contract")
    if contract is not None and contract != "HGTerrainMeshNormals/1":
        raise ValueError(f"Terrain source-normal contract changed: {obj.name}")
    source_changed = contract is not None and mesh.get("endfield_terrain_vertex_normal_sha256") != digest
    if contract is None or source_changed:
        if contract is None and mesh.has_custom_normals:
            raise ValueError(f"Terrain mesh has unrecognized custom normals: {obj.name}")
        for polygon in mesh.polygons:
            polygon.use_smooth = True
        mesh.normals_split_custom_set(expected.tolist())
        mesh["endfield_terrain_vertex_normal_contract"] = "HGTerrainMeshNormals/1"
        mesh["endfield_terrain_vertex_normal_sha256"] = digest
    actual = np.empty(len(mesh.loops)*3, dtype=np.float32)
    mesh.corner_normals.foreach_get("vector", actual)
    if not np.isfinite(actual).all() or not np.isfinite(expected).all():
        raise ValueError(f"Terrain source or stored normals are nonfinite: {obj.name}")
    error = float(np.max(np.abs(actual.reshape(-1, 3)-expected)))
    actual_unit = actual.reshape(-1, 3).astype(np.float64)
    lengths = np.linalg.norm(actual_unit, axis=1)
    if np.any(lengths < 1e-12):
        raise ValueError(f"Terrain stored normals contain zero vectors: {obj.name}")
    actual_unit /= lengths[:, None]
    angle = float(np.degrees(np.arccos(np.clip(np.sum(actual_unit*expected, axis=1), -1, 1))).max())
    # Compare native encoding on identical topology, not an arbitrary source-angle tolerance.
    reference = mesh.copy()
    try:
        reference.normals_split_custom_set(expected.tolist())
        encoded = np.empty_like(actual)
        reference.corner_normals.foreach_get("vector", encoded)
    finally:
        bpy.data.meshes.remove(reference)
    roundtrip_error = float(np.max(np.abs(actual-encoded)))
    if (not mesh.has_custom_normals or any(not polygon.use_smooth for polygon in mesh.polygons)
            or not np.isfinite(encoded).all() or roundtrip_error > 1e-6):
        raise ValueError(f"Terrain stored normals disagree with native source encoding: {obj.name}, max_roundtrip_error={roundtrip_error}")
    status = "source_updated" if source_changed else "already_adapted" if contract else "adapted"
    return {"object": obj.name, "status": status, "corners": len(mesh.loops),
            "max_storage_error": error, "max_storage_angle_degrees": angle, "max_roundtrip_error": roundtrip_error}


def adapt_scene_terrain_mesh_normals(scene: bpy.types.Scene) -> list[TerrainVertexNormals]:
    terrain = [obj for obj in scene.objects if obj.type == "MESH" and "unity_tile" in obj
               and any(material and material.get("endfield_terrain_surface_source") for material in obj.data.materials)]
    materials = {material for obj in terrain for material in obj.data.materials
                 if material and material.get("endfield_terrain_surface_source")}
    images = {normal_atlas_binding(material)[0].image for material in materials}
    pixels = {image: atlas_pixels(image) for image in images}
    return [adapt_terrain_mesh_normals(obj, pixels) for obj in sorted(terrain, key=lambda item: item.name)]


def terrain_world_normal_group() -> bpy.types.ShaderNodeTree:
    """Decode source RG into Unity X/Z, reconstruct up, then convert to Blender."""
    name = "EF_HGRP_Terrain_World_Normal_v1"
    existing = bpy.data.node_groups.get(name)
    if existing is not None:
        if existing.get("endfield_contract") != "HGTerrainWorldNormal/1":
            raise ValueError(f"Conflicting terrain normal group: {name}")
        return existing
    tree = bpy.data.node_groups.new(name, "ShaderNodeTree")
    tree.interface.new_socket(name="Packed RG", in_out="INPUT", socket_type="NodeSocketColor")
    tree.interface.new_socket(name="World Normal", in_out="OUTPUT", socket_type="NodeSocketVector")
    entry = tree.nodes.new("NodeGroupInput")
    output = tree.nodes.new("NodeGroupOutput")
    decode = tree.nodes.new("ShaderNodeGroup")
    decode.node_tree = packed_normal_group()
    decode.inputs["Packed Alpha"].default_value = 1
    decode.inputs["Multiply Red By Alpha"].default_value = 0
    decode.inputs["Normal Strength"].default_value = 1
    tree.links.new(entry.outputs["Packed RG"], decode.inputs["Packed RGB"])
    signed = tree.nodes.new("ShaderNodeVectorMath")
    signed.operation = "SUBTRACT"
    signed.inputs[1].default_value = (0.5, 0.5, 0.5)
    tree.links.new(decode.outputs["Encoded Normal"], signed.inputs[0])
    basis = tree.nodes.new("ShaderNodeVectorMath")
    basis.operation = "MULTIPLY"
    basis.inputs[1].default_value = (2, -2, 2)
    tree.links.new(signed.outputs[0], basis.inputs[0])
    unit = tree.nodes.new("ShaderNodeVectorMath")
    unit.operation = "NORMALIZE"
    tree.links.new(basis.outputs[0], unit.inputs[0])
    tree.links.new(unit.outputs[0], output.inputs["World Normal"])
    tree["endfield_contract"] = "HGTerrainWorldNormal/1"
    tree["endfield_source_semantics"] = "VTBakePage RG signed XZ; up=sqrt(max(0,1-dot(XZ,XZ))); Unity to Blender (X,-Z,Y)"
    tree["endfield_scope"] = "Base atlas normal only; layer micro-normal and cliff projection remain separate"
    return tree


def upstream_nodes(socket: bpy.types.NodeSocket) -> set[bpy.types.Node]:
    result: set[bpy.types.Node] = set()
    pending = [link.from_node for link in socket.links]
    while pending:
        node = pending.pop()
        if node in result:
            continue
        result.add(node)
        pending.extend(link.from_node for item in node.inputs for link in item.links)
    return result


def adapt_terrain_material(material: bpy.types.Material) -> str:
    """Replace the verified legacy normal branch, preserving projected decal layering."""
    if material.get("projected_decal_material"):
        shaders = [node for node in material.node_tree.nodes if node.type == "BSDF_PRINCIPLED"
                   and node.outputs["BSDF"].is_linked]
        if shaders and all(any(node.type == "NORMAL_MAP" and node.uv_map == "DecalUV"
                               for node in upstream_nodes(shader.inputs["Normal"])) for shader in shaders):
            material["endfield_terrain_normal_contract"] = "ProjectedDecalNormalOverride/1"
            return "decal_normal_override_preserved"
    if material.get("endfield_terrain_normal_contract") == "HGTerrainWorldNormal/1":
        groups = [node for node in material.node_tree.nodes if node.type == "GROUP" and node.node_tree
                  and node.node_tree.get("endfield_contract") == "HGTerrainWorldNormal/1"]
        if len(groups) != 1 or not groups[0].outputs["World Normal"].is_linked:
            raise ValueError(f"Terrain normal contract has no active decoder: {material.name}")
        return "already_adapted"
    surface = Path(material["endfield_terrain_surface_source"])
    manifest = json.loads(surface.read_text(encoding="utf-8"))
    if manifest.get("format") not in ("EndfieldTerrainSurfaceAtlas/1", "EndfieldTerrainSurfaceAtlas/2"):
        raise ValueError(f"Unsupported terrain atlas: {surface}")
    paths = {(surface.parent / manifest[key]).resolve() for key in ("normal", "normal_rgba") if key in manifest}
    tree = material.node_tree
    images = [node for node in tree.nodes if node.type == "TEX_IMAGE" and node.image
              and Path(node.image.get("endfield_terrain_crop_source", bpy.path.abspath(node.image.filepath))).resolve() in paths]
    if len(images) != 1 or images[0].image.colorspace_settings.name != "Non-Color":
        raise ValueError(f"Terrain normal atlas must have one linear data binding: {material.name}")
    source = images[0]
    normals = [node for node in tree.nodes if node.type == "NORMAL_MAP"
               and source in upstream_nodes(node.inputs["Color"])]
    if len(normals) != 1:
        raise ValueError(f"Terrain adapter requires one legacy tangent-normal branch: {material.name}")
    old = normals[0]
    outputs = list(old.outputs["Normal"].links)
    if len(outputs) == 1 and outputs[0].to_node.type == "MIX_RGB":
        mix = outputs[0].to_node
        if (outputs[0].to_socket != mix.inputs[2] or len(mix.inputs[1].links) != 1
                or mix.inputs[1].links[0].from_node.type != "NEW_GEOMETRY"):
            raise ValueError(f"Unrecognized terrain slope blend: {material.name}")
        old = mix
    destinations = [link.to_socket for link in old.outputs[0].links]
    if not destinations:
        raise ValueError(f"Terrain normal branch has no consumers: {material.name}")
    unused = {old} | {node for item in old.inputs for node in upstream_nodes(item)}
    unused = {node for node in unused if node.type not in ("TEX_IMAGE", "UVMAP")}
    group = tree.nodes.new("ShaderNodeGroup")
    group.node_tree = terrain_world_normal_group()
    group.label = "HGTerrain world base normal"
    group.location = old.location.copy()
    tree.links.new(source.outputs["Color"], group.inputs["Packed RG"])
    for destination in destinations:
        tree.links.new(group.outputs["World Normal"], destination)
    while unused:
        removable = {node for node in unused if not any(item.is_linked for item in node.outputs)}
        if not removable:
            break
        for node in removable:
            unused.remove(node)
            tree.nodes.remove(node)
    material["endfield_terrain_normal_contract"] = "HGTerrainWorldNormal/1"
    material["endfield_terrain_normal_semantics"] = group.node_tree["endfield_source_semantics"]
    material["endfield_terrain_normal_green_inverted"] = False
    material["endfield_terrain_slope_normal_fallback"] = "Removed: base normal is decoded directly in world space"
    material["endfield_terrain_cliff_projection_status"] = "Layer detail and cliff projection pending; world base normal restored"
    return "adapted"


def adapt_scene_terrain(scene: bpy.types.Scene) -> list[dict[str, str]]:
    materials = {material for obj in scene.objects if obj.type == "MESH"
                 for material in obj.data.materials if material and material.get("endfield_terrain_surface_source")}
    reports = [{"material": material.name, "status": adapt_terrain_material(material)}
               for material in sorted(materials, key=lambda item: item.name)]
    scene["endfield_terrain_mesh_normal_audit"] = json.dumps(adapt_scene_terrain_mesh_normals(scene))
    return reports
