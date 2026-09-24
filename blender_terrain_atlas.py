"""Crop terrain atlases for local reviews without reducing world-space density."""
from __future__ import annotations

import hashlib
import json
import math
from pathlib import Path
from typing import TypedDict

import bpy
import numpy as np


class AtlasCrop(TypedDict):
    surface: str
    material: str
    image: str
    source_size: list[int]
    crop_pixels: list[int]
    packed_size: list[int]


def crop_window(uv_min: tuple[float, float], uv_max: tuple[float, float],
                size: tuple[int, int], padding: int) -> tuple[int, int, int, int]:
    """Return an integer pixel window including the interpolation border."""
    if padding < 1 or min(size) <= 0 or not all(math.isfinite(v) for v in (*uv_min, *uv_max)):
        raise ValueError(f"Invalid terrain crop inputs: minimum={uv_min}, maximum={uv_max}, size={size}, padding={padding}")
    if any(a < -0.00001 or b > 1.00001 or b <= a for a, b in zip(uv_min, uv_max, strict=True)):
        raise ValueError(f"Terrain UV range is outside its atlas: minimum={uv_min}, maximum={uv_max}")
    return (max(0, math.floor(uv_min[0] * size[0]) - padding),
            max(0, math.floor(uv_min[1] * size[1]) - padding),
            min(size[0], math.ceil(uv_max[0] * size[0]) + padding),
            min(size[1], math.ceil(uv_max[1] * size[1]) + padding))


def crop_terrain_atlases(scene: bpy.types.Scene) -> list[AtlasCrop]:
    """Replace only local terrain image bindings; preserve meshes and every UV layer."""
    consumers: dict[bpy.types.Material, list[bpy.types.Object]] = {}
    for obj in scene.objects:
        if obj.type == "MESH":
            for material in obj.data.materials:
                if material and material.get("endfield_terrain_surface_source"):
                    consumers.setdefault(material, []).append(obj)
    reports: list[AtlasCrop] = []
    for material, objects in consumers.items():
        if material.get("endfield_terrain_crop_contract"):
            if material["endfield_terrain_crop_contract"] != "TerrainAtlasDensity/1":
                raise ValueError(f"Unknown terrain crop contract: {material.name}")
            packed = [node.image for node in material.node_tree.nodes if node.type == "TEX_IMAGE" and node.image
                      and node.image.get("endfield_terrain_density_preserved")]
            if len(packed) != 2 or any(not image.packed_file for image in packed):
                raise ValueError(f"Previously cropped terrain has missing packed images: {material.name}")
            continue
        surface = Path(material["endfield_terrain_surface_source"])
        manifest = json.loads(surface.read_text(encoding="utf-8"))
        if manifest.get("format") not in ("EndfieldTerrainSurfaceAtlas/1", "EndfieldTerrainSurfaceAtlas/2"):
            raise ValueError(f"Unsupported terrain surface atlas: {surface}")
        sources = {(surface.parent / manifest[channel]).resolve()
                   for channel in ("albedo", "normal", "albedo_rgba", "normal_rgba") if channel in manifest}
        tree = material.node_tree
        images = [node for node in tree.nodes if node.type == "TEX_IMAGE" and node.image
                  and Path(bpy.path.abspath(node.image.filepath)).resolve() in sources]
        if len(images) != 2:
            raise ValueError(f"Terrain crop requires the two source atlas nodes: material={material.name}, found={len(images)}")
        uv_names = {node.inputs["Vector"].links[0].from_node.uv_map for node in images
                    if len(node.inputs["Vector"].links) == 1 and node.inputs["Vector"].links[0].from_node.type == "UVMAP"}
        if len(uv_names) != 1:
            raise ValueError(f"Terrain crop requires matching explicit UV bindings: {material.name}")
        uv_name = next(iter(uv_names))
        ranges = []
        for obj in objects:
            uv = obj.data.uv_layers.get(uv_name)
            if uv is None:
                raise ValueError(f"Terrain atlas consumer lacks UV layer: object={obj.name}, material={material.name}, uv={uv_name}")
            indices = {slot for slot, candidate in enumerate(obj.data.materials) if candidate == material}
            loops = [loop for polygon in obj.data.polygons if polygon.material_index in indices for loop in polygon.loop_indices]
            if loops:
                ranges.extend(tuple(uv.data[index].uv) for index in loops)
        if not ranges:
            continue
        uv_min = (min(row[0] for row in ranges), min(row[1] for row in ranges))
        uv_max = (max(row[0] for row in ranges), max(row[1] for row in ranges))
        plans = []
        for node in images:
            vector = node.inputs["Vector"]
            if len(vector.links) != 1 or vector.links[0].from_node.type != "UVMAP" or vector.links[0].from_node.uv_map != uv_name:
                raise ValueError(f"Terrain crop requires an untransformed UV binding: {material.name}, {node.name}")
            path = Path(bpy.path.abspath(node.image.filepath))
            if not path.is_file():
                raise FileNotFoundError(path)
            source = bpy.data.images.load(str(path), check_existing=False)
            source.colorspace_settings.name = node.image.colorspace_settings.name
            source.alpha_mode = "CHANNEL_PACKED"
            size = tuple(source.size)
            x0, y0, x1, y1 = crop_window(uv_min, uv_max, size, 2)
            pixels = np.empty(size[0] * size[1] * 4, dtype=np.float32)
            source.pixels.foreach_get(pixels)
            cropped = pixels.reshape(size[1], size[0], 4)[y0:y1, x0:x1].copy()
            digest = hashlib.sha256(str(path.resolve()).encode() + source.colorspace_settings.name.encode()
                                    + cropped.tobytes()).hexdigest()[:20]
            image = bpy.data.images.get("EF_TerrainCrop_" + digest)
            if image is None:
                image = bpy.data.images.new("EF_TerrainCrop_" + digest, width=x1-x0, height=y1-y0, alpha=True)
                image.colorspace_settings.name = source.colorspace_settings.name
                image.alpha_mode = "CHANNEL_PACKED"
                image.pixels.foreach_set(cropped.ravel())
                image.pack()
                image["endfield_terrain_density_preserved"] = True
                image["endfield_terrain_crop_source"] = str(path)
                image["endfield_terrain_crop_pixels"] = json.dumps([x0, y0, x1, y1])
            elif tuple(image.size) != (x1-x0, y1-y0) or not image.packed_file:
                raise ValueError(f"Terrain crop image cache collision: {image.name}")
            plans.append((node, vector.links[0].from_socket, image, size, (x0, y0, x1, y1)))
            bpy.data.images.remove(source)
        for node, coordinate, image, size, (x0, y0, x1, y1) in plans:
            offset = tree.nodes.new("ShaderNodeVectorMath")
            offset.operation = "SUBTRACT"
            offset.label = "Terrain atlas crop origin"
            offset.inputs[1].default_value = (x0 / size[0], y0 / size[1], 0)
            scale = tree.nodes.new("ShaderNodeVectorMath")
            scale.operation = "MULTIPLY"
            scale.label = "Terrain atlas crop density"
            scale.inputs[1].default_value = (size[0] / (x1-x0), size[1] / (y1-y0), 1)
            tree.links.new(coordinate, offset.inputs[0])
            tree.links.new(offset.outputs["Vector"], scale.inputs[0])
            tree.links.new(scale.outputs["Vector"], node.inputs["Vector"])
            node.image = image
            reports.append({"surface": str(surface), "material": material.name, "image": image.name,
                            "source_size": list(size), "crop_pixels": [x0,y0,x1,y1], "packed_size": list(image.size)})
        material["endfield_terrain_crop_contract"] = "TerrainAtlasDensity/1"
    return reports
