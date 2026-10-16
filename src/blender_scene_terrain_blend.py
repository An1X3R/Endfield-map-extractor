"""Provide a camera-independent host contact adapter for source TerrainBlend draws.

The source program uses screen depth and virtual-texture terrain samples. This
adapter deliberately uses the retained heightfield directly beneath a fragment.
It keeps source meshes, UVs and custom normals intact and requires no callbacks.
"""
from __future__ import annotations

from dataclasses import dataclass
import hashlib
import json
import math
from pathlib import Path
from typing import NotRequired, TypedDict

import bpy
import numpy as np
from numpy.typing import NDArray

from blender_hgrp_top_layer import _component, _mix
from blender_scene_effect_layers import _math, _vec
from blender_scene_materials import layout_shader_node_tree, texture_image
from blender_scene_terrain import terrain_world_normal_group
from endfield_scene_material_contract import MaterialRecord, reference_key


CONTRACT = "HostTerrainHeightfieldContact/1"
BOUNDARY = ("Host vertical heightfield contact, not native screen-depth TerrainBlend. "
            "Retained triangulated terrain geometry and linear source A/N atlases only; "
            "native VT/detail, wet modulation, view-depth gate and view-dependent sample positions remain unresolved. "
            "No contact outside exported terrain cells or across source no-data holes.")


@dataclass(frozen=True)
class ContactField:
    surface: Path
    bounds: tuple[float, float, float, float]
    cell_size: tuple[float, float]
    planes: tuple[bpy.types.Image, bpy.types.Image]
    valid_cells: int
    total_cells: int


class TerrainContactAudit(TypedDict):
    status: str
    materials: list[str]
    boundary: str
    surface: NotRequired[str]
    valid_cells: NotRequired[int]
    total_cells: NotRequired[int]
    reason: NotRequired[str]


class UnsupportedTerrainContactContext(ValueError):
    """The retained terrain cannot be represented by this explicit host adapter."""


def _float_image(name: str, pixels: NDArray[np.float32], output: Path) -> bpy.types.Image:
    """Save and pack exact float coefficients, preserving values outside 0..1."""
    image = bpy.data.images.new(name, width=pixels.shape[1], height=pixels.shape[0], alpha=True, float_buffer=True)
    image.colorspace_settings.name = "Non-Color"
    image.alpha_mode = "CHANNEL_PACKED"
    image.use_half_precision = False
    image.pixels.foreach_set(pixels.ravel())
    image.file_format = "OPEN_EXR"
    image.filepath_raw = str(output)
    image.save()
    image.pack()
    image["endfield_terrain_contact_contract"] = CONTRACT
    return image


def build_contact_field(terrain: tuple[bpy.types.Object, ...], output: Path) -> ContactField:
    """Encode actual exported triangle planes and cell coverage without resampling."""
    if not terrain or any(obj.type != "MESH" or obj.modifiers for obj in terrain):
        raise UnsupportedTerrainContactContext("Terrain contact requires nonempty, unmodified retained terrain meshes")
    sources = {Path(material["endfield_terrain_surface_source"]).resolve()
               for obj in terrain for material in obj.data.materials
               if material and material.get("endfield_terrain_surface_source")}
    if len(sources) != 1:
        raise UnsupportedTerrainContactContext(f"Terrain contact requires one explicit surface atlas per binding: sources={sorted(map(str, sources))}")
    surface = next(iter(sources))
    positions = {obj: tuple(obj.matrix_world @ vertex.co for vertex in obj.data.vertices) for obj in terrain}
    polygons = [(obj, polygon) for obj in terrain for polygon in obj.data.polygons]
    if not polygons or any(len(polygon.vertices) != 4 for _, polygon in polygons):
        raise UnsupportedTerrainContactContext("Terrain contact requires the retained regular heightfield quads")
    first_obj, first_polygon = polygons[0]
    first = [positions[first_obj][index] for index in first_polygon.vertices]
    dx = max(v.x for v in first) - min(v.x for v in first)
    dy = max(v.y for v in first) - min(v.y for v in first)
    if min(dx, dy) <= 0 or not all(math.isfinite(v) for rows in positions.values() for row in rows for v in row):
        raise ValueError("Terrain contact grid has nonfinite positions or zero cell size")
    xmin = min(v.x for rows in positions.values() for v in rows)
    xmax = max(v.x for rows in positions.values() for v in rows)
    ymin = min(v.y for rows in positions.values() for v in rows)
    ymax = max(v.y for rows in positions.values() for v in rows)
    width, height = round((xmax-xmin)/dx), round((ymax-ymin)/dy)
    if (width < 1 or height < 1 or width*height > 16777216
            or abs(width*dx-(xmax-xmin)) > 1e-4 or abs(height*dy-(ymax-ymin)) > 1e-4):
        raise UnsupportedTerrainContactContext(f"Terrain contact grid is irregular or exceeds the 16M-cell budget: size={(width, height)}")
    planes = (np.zeros((height, width, 4), dtype=np.float32), np.zeros((height, width, 4), dtype=np.float32))
    cells: set[tuple[int, int]] = set()
    for obj in terrain:
        obj.data.calc_loop_triangles()
        triangles: dict[int, list[tuple[int, ...]]] = {}
        for triangle in obj.data.loop_triangles:
            triangles.setdefault(triangle.polygon_index, []).append(tuple(triangle.vertices))
        for polygon in obj.data.polygons:
            points = [positions[obj][index] for index in polygon.vertices]
            x0, y0 = min(v.x for v in points), min(v.y for v in points)
            ix, iy = round((x0-xmin)/dx), round((y0-ymin)/dy)
            local = {index: ((positions[obj][index].x-x0)/dx, (positions[obj][index].y-y0)/dy)
                     for index in polygon.vertices}
            corners = {index: (round(value[0]), round(value[1])) for index, value in local.items()}
            if (set(corners.values()) != {(0, 0), (1, 0), (0, 1), (1, 1)}
                    or any(abs(local[index][axis]-value[axis]) > 1e-4 for index, value in corners.items() for axis in (0, 1))
                    or abs(x0-(xmin+ix*dx)) > 1e-4 or abs(y0-(ymin+iy*dy)) > 1e-4):
                raise UnsupportedTerrainContactContext(f"Terrain contact polygon is not an aligned grid cell: object={obj.name}, polygon={polygon.index}")
            if (ix, iy) in cells:
                raise UnsupportedTerrainContactContext(f"Terrain contact has overlapping heightfield cells: cell={(ix, iy)}")
            rows = triangles[polygon.index]
            if len(rows) != 2:
                raise UnsupportedTerrainContactContext(f"Terrain contact quad has unexpected triangulation: object={obj.name}, polygon={polygon.index}")
            diagonal = {corners[index] for index in set(rows[0]).intersection(rows[1])}
            if diagonal == {(0, 1), (1, 0)}:
                code, unique = 1., (0, 0)
            elif diagonal == {(0, 0), (1, 1)}:
                code, unique = 2., (1, 0)
            else:
                raise UnsupportedTerrainContactContext(f"Terrain contact has an invalid cell diagonal: object={obj.name}, polygon={polygon.index}")
            ordered = sorted(rows, key=lambda row: unique not in {corners[index] for index in row})
            for destination, indices in zip(planes, ordered, strict=True):
                matrix = np.array([[*local[index], 1.] for index in indices], dtype=np.float64)
                coefficients = np.linalg.solve(matrix, np.array([positions[obj][index].z for index in indices]))
                destination[iy, ix] = (*coefficients, code)
            cells.add((ix, iy))
    digest = hashlib.sha256(planes[0].tobytes()+planes[1].tobytes()).hexdigest()[:20]
    output.mkdir(parents=True, exist_ok=True)
    images = tuple(_float_image(f"EF_ContactPlanes{index}_{digest}", pixels, output/f"terrain_contact_planes_{index}_{digest}.exr")
                   for index, pixels in enumerate(planes))
    return ContactField(surface, (xmin, xmax, ymin, ymax), (dx, dy), (images[0], images[1]), len(cells), width*height)


def _sample(tree: bpy.types.NodeTree, image: bpy.types.Image, coords: bpy.types.NodeSocket,
            label: str, interpolation: str) -> bpy.types.ShaderNodeTexImage:
    node = tree.nodes.new("ShaderNodeTexImage")
    node.name = node.label = label
    node.image, node.interpolation, node.extension = image, interpolation, "CLIP"
    tree.links.new(coords, node.inputs["Vector"])
    return node


def contact_group(field: ContactField) -> bpy.types.ShaderNodeTree:
    """Expose explicit contact controls and sample exact triangle heights per fragment."""
    manifest = json.loads(field.surface.read_text(encoding="utf-8-sig"))
    if manifest.get("format") not in ("EndfieldTerrainSurfaceAtlas/1", "EndfieldTerrainSurfaceAtlas/2"):
        raise ValueError(f"Terrain contact does not support this atlas format: surface={field.surface}")
    atlas = manifest["atlas_world_bounds"]
    if (not isinstance(atlas, list) or len(atlas) != 4 or not all(type(value) in (int, float) and math.isfinite(value) for value in atlas)
            or atlas[1] <= atlas[0] or atlas[3] <= atlas[2]):
        raise ValueError(f"Terrain contact atlas has invalid world bounds: surface={field.surface}, bounds={atlas}")
    images = []
    for channel in ("albedo", "normal"):
        path = field.surface.parent / manifest[channel]
        if not path.is_file():
            raise FileNotFoundError(f"Terrain contact source atlas is missing: channel={channel}, path={path}")
        image = texture_image(path, "Non-Color", "CHANNEL_PACKED")
        if not image.packed_file:
            image.pack()
        images.append(image)
    tree = bpy.data.node_groups.new("EF Host Terrain Heightfield Contact", "ShaderNodeTree")
    for name in ("Contact Width", "Falloff", "Blend Strength", "Normal Strength", "Normal Object Offset", "Terrain Roughness", "Terrain Metallic"):
        socket = tree.interface.new_socket(name=name, in_out="INPUT", socket_type="NodeSocketFloat")
        socket.min_value = 0. if name != "Normal Object Offset" else -1.
        socket.max_value = 1. if name in ("Blend Strength", "Normal Strength", "Terrain Roughness", "Terrain Metallic") else 100.
    for name, kind in (("Terrain Color", "NodeSocketColor"), ("Terrain Normal", "NodeSocketVector"),
                       ("Terrain Weight", "NodeSocketFloat"), ("Normal Weight", "NodeSocketFloat"),
                       ("Height", "NodeSocketFloat"), ("Valid", "NodeSocketFloat"),
                       ("Terrain Roughness", "NodeSocketFloat"), ("Terrain Metallic", "NodeSocketFloat")):
        tree.interface.new_socket(name=name, in_out="OUTPUT", socket_type=kind)
    entry, output = tree.nodes.new("NodeGroupInput"), tree.nodes.new("NodeGroupOutput")
    position = tree.nodes.new("ShaderNodeNewGeometry").outputs["Position"]
    xmin, xmax, ymin, ymax = field.bounds
    offset = _vec(tree, "Heightfield world origin", "SUBTRACT", position, (xmin, ymin, 0))
    coords = _vec(tree, "Heightfield normalized coordinates", "MULTIPLY", offset, (1/(xmax-xmin), 1/(ymax-ymin), 0))
    cell = _vec(tree, "Heightfield cell coordinates", "MULTIPLY", offset, (1/field.cell_size[0], 1/field.cell_size[1], 0))
    fraction = tree.nodes.new("ShaderNodeVectorMath")
    fraction.operation = "FRACTION"
    tree.links.new(cell, fraction.inputs[0])
    fx, fy = (_component(tree, fraction.outputs[0], index) for index in (0, 1))
    samples = [_sample(tree, image, coords, f"Exact retained triangle planes {index}", "Closest") for index, image in enumerate(field.planes)]
    heights = []
    for index, sample in enumerate(samples):
        x = _math(tree, "Triangle X height", "MULTIPLY", _component(tree, sample.outputs["Color"], 0), fx)
        y = _math(tree, "Triangle Y height", "MULTIPLY", _component(tree, sample.outputs["Color"], 1), fy)
        heights.append(_math(tree, f"Triangle {index} height", "ADD", _math(tree, "Triangle XY height", "ADD", x, y), _component(tree, sample.outputs["Color"], 2)))
    code = samples[0].outputs["Alpha"]
    diagonal = _math(tree, "Retained cell diagonal", "GREATER_THAN", code, 1.5)
    lower = _math(tree, "Lower sum triangle", "LESS_THAN", _math(tree, "Cell XY sum", "ADD", fx, fy), 1)
    right = _math(tree, "Lower diagonal triangle", "LESS_THAN", fy, fx)
    select = _mix(tree, "Retained triangle selection", lower, right, diagonal)
    height = _mix(tree, "Exact heightfield height", heights[1], heights[0], select)
    valid = _math(tree, "Exported cell exists", "GREATER_THAN", code, .5)
    for index in (0, 1):
        component = _component(tree, coords, index)
        inside = _math(tree, "Heightfield upper boundary", "LESS_THAN", component, 1)
        inside = _math(tree, "Heightfield lower boundary", "MULTIPLY", inside, _math(tree, "Heightfield lower inclusion", "GREATER_THAN", component, -1e-7))
        valid = _math(tree, "Retained heightfield coverage", "MULTIPLY", valid, inside)
    atlas_coords = _vec(tree, "Terrain atlas world origin", "SUBTRACT", position, (atlas[0], -atlas[3], 0))
    atlas_coords = _vec(tree, "Terrain source A N coordinates", "MULTIPLY", atlas_coords, (1/(atlas[1]-atlas[0]), 1/(atlas[3]-atlas[2]), 0))
    for index in (0, 1):
        component = _component(tree, atlas_coords, index)
        inside = _math(tree, "Atlas upper boundary", "LESS_THAN", component, 1)
        inside = _math(tree, "Atlas lower boundary", "MULTIPLY", inside, _math(tree, "Atlas lower inclusion", "GREATER_THAN", component, -1e-7))
        valid = _math(tree, "Retained atlas coverage", "MULTIPLY", valid, inside)
    albedo, normal = [_sample(tree, image, atlas_coords, "Source terrain "+channel, "Linear") for image, channel in zip(images, ("A", "N"), strict=True)]
    decoder = tree.nodes.new("ShaderNodeGroup")
    decoder.node_tree = terrain_world_normal_group()
    tree.links.new(normal.outputs["Color"], decoder.inputs["Packed RG"])
    distance = _math(tree, "Vertical contact separation", "SUBTRACT", _component(tree, position, 2), height)
    distance = _math(tree, "Absolute contact separation", "ABSOLUTE", distance, 0)
    width = _math(tree, "Positive contact width", "MAXIMUM", entry.outputs["Contact Width"], 1e-6)
    scaled = _math(tree, "Contact width normalization", "DIVIDE", distance, width)
    object_weight = _math(tree, "Source shaped host contact falloff", "POWER", scaled, entry.outputs["Falloff"])
    object_weight = _math(tree, "Host object weight", "MINIMUM", object_weight, 1)
    coverage = _math(tree, "Enabled retained coverage", "MULTIPLY", valid, entry.outputs["Blend Strength"])
    weight = _math(tree, "Terrain contact weight", "MULTIPLY", coverage, _math(tree, "Inverse object weight", "SUBTRACT", 1, object_weight))
    normal_object = _math(tree, "Source normal object offset", "ADD", object_weight, entry.outputs["Normal Object Offset"])
    clamp = tree.nodes.new("ShaderNodeClamp")
    tree.links.new(normal_object, clamp.inputs[0])
    normal_weight = _math(tree, "Terrain normal contact weight", "MULTIPLY", coverage, _math(tree, "Inverse normal object weight", "SUBTRACT", 1, clamp.outputs[0]))
    normal_weight = _math(tree, "Host normal intensity", "MULTIPLY", normal_weight, entry.outputs["Normal Strength"])
    for value, name in ((albedo.outputs["Color"], "Terrain Color"), (decoder.outputs[0], "Terrain Normal"),
                       (weight, "Terrain Weight"), (normal_weight, "Normal Weight"), (height, "Height"), (valid, "Valid"),
                       (entry.outputs["Terrain Roughness"], "Terrain Roughness"), (entry.outputs["Terrain Metallic"], "Terrain Metallic")):
        tree.links.new(value, output.inputs[name])
    tree["endfield_terrain_contact_contract"] = CONTRACT
    tree["endfield_terrain_contact_boundary"] = BOUNDARY
    tree["endfield_terrain_contact_surface"] = str(field.surface)
    tree["endfield_terrain_contact_bounds"] = json.dumps(field.bounds)
    layout_shader_node_tree(tree)
    return tree


def apply_contact_material(material: bpy.types.Material, record: MaterialRecord,
                           group: bpy.types.ShaderNodeTree) -> None:
    """Blend compatible PBR inputs after the existing source TOP layer."""
    if material.get("endfield_terrain_contact_contract"):
        raise ValueError(f"Terrain host contact is already attached: material={material.name}")
    floats = record["Floats"]
    factor, falloff, normal = (floats[name] for name in ("_TerrainBlendFactor", "_TerrainBlendFallOff", "_TerrainBlendNormalFactor"))
    if not all(math.isfinite(value) for value in (factor, falloff, normal)) or min(factor, falloff) <= 0:
        raise ValueError(f"Invalid source terrain contact parameters: material={material.name}, factor={factor}, falloff={falloff}, normal={normal}")
    tree = material.node_tree
    existing = set(tree.nodes)
    shaders = [node for node in tree.nodes if node.type == "BSDF_PRINCIPLED" and node.outputs["BSDF"].is_linked]
    if len(shaders) != 1:
        raise ValueError(f"Terrain host contact requires one live Principled surface: material={material.name}, shaders={len(shaders)}")
    entry = tree.nodes.new("ShaderNodeGroup")
    entry.node_tree = group
    entry.name = entry.label = "Host Terrain Contact Controls"
    for name, value in (("Contact Width", 1/factor), ("Falloff", falloff), ("Blend Strength", 1.),
                       ("Normal Strength", 1.), ("Normal Object Offset", normal), ("Terrain Roughness", .86), ("Terrain Metallic", 0.)):
        entry.inputs[name].default_value = value
    for name, output in (("Base Color", "Terrain Color"), ("Normal", "Terrain Normal"), ("Roughness", "Terrain Roughness"), ("Metallic", "Terrain Metallic")):
        socket = shaders[0].inputs[name]
        source = socket.links[0].from_socket if socket.is_linked else tuple(socket.default_value) if name in ("Base Color", "Normal") else float(socket.default_value)
        if name == "Normal" and not socket.is_linked:
            source = tree.nodes.new("ShaderNodeNewGeometry").outputs["Normal"]
        if isinstance(source, float):
            value = tree.nodes.new("ShaderNodeValue")
            value.outputs[0].default_value = source
            source = value.outputs[0]
        weight = entry.outputs["Normal Weight" if name == "Normal" else "Terrain Weight"]
        blended = _mix(tree, "Host contact "+name, source, entry.outputs[output], weight)
        if name == "Normal":
            blended = _vec(tree, "Unit host contact normal", "NORMALIZE", blended, (0, 0, 0))
        tree.links.new(blended, socket)
    material["endfield_terrain_contact_contract"] = CONTRACT
    material["endfield_terrain_contact_boundary"] = BOUNDARY
    material["endfield_terrain_contact_source_width"] = 1/factor
    material["endfield_surface_fidelity"] = "source_top_layer_with_explicit_host_heightfield_contact_native_runtime_unresolved"
    for node in set(tree.nodes) - existing:
        node["endfield_layout_section"] = "Host Terrain Contact"
    layout_shader_node_tree(tree)


def _unbound_context(scene: bpy.types.Scene, materials: set[bpy.types.Material], status: str,
                     reason: str) -> TerrainContactAudit:
    """Record an explicit unsupported host context without changing surface nodes."""
    for material in materials:
        material["endfield_terrain_contact_boundary"] = BOUNDARY
        material["endfield_terrain_contact_status"] = status
        material["endfield_terrain_contact_context_reason"] = reason
    audit: TerrainContactAudit = {"status": status, "materials": sorted(material.name for material in materials),
                                 "boundary": BOUNDARY, "reason": reason}
    scene["endfield_terrain_contact_audit"] = json.dumps(audit)
    return audit


def apply_scene_terrain_contact(scene: bpy.types.Scene, records: dict[tuple[str, int], MaterialRecord],
                                output: Path) -> TerrainContactAudit:
    """Bind only source-enabled Lit TerrainBlend materials to one retained field."""
    selected = {f"{reference_key(record)[0]}:{reference_key(record)[1]}": record for record in records.values()
                if record["Shader"].get("Name") == "HGRP/Lit"
                and "_TERRAIN_BLEND" in record.get("RenderState", {}).get("ValidKeywords", [])
                and record["Floats"].get("_EnableTerrainBlend") == 1}
    materials = {slot.material for obj in scene.objects if obj.type == "MESH" and not obj.hide_render
                 for slot in obj.material_slots if slot.material and str(slot.material.get("endfield_material_source", "")).casefold() in selected}
    if not materials:
        return {"status": "no_source_enabled_receivers", "materials": [], "boundary": BOUNDARY}
    terrain = tuple(obj for obj in scene.objects if obj.type == "MESH" and "unity_tile" in obj and not obj.hide_render
                    and any(material and material.get("endfield_terrain_surface_source") for material in obj.data.materials))
    if not terrain:
        return _unbound_context(scene, materials, "source_enabled_terrain_context_absent",
                                "Retained terrain cells are absent; no host contact is attached")
    try:
        field = build_contact_field(terrain, output)
    except UnsupportedTerrainContactContext as error:
        return _unbound_context(scene, materials, "unsupported_retained_heightfield_context", str(error))
    group = contact_group(field)
    for material in sorted(materials, key=lambda item: item.name):
        apply_contact_material(material, selected[material["endfield_material_source"].casefold()], group)
        material["endfield_terrain_contact_status"] = "host_heightfield_contact_attached"
        if "endfield_terrain_contact_context_reason" in material:
            del material["endfield_terrain_contact_context_reason"]
    audit: TerrainContactAudit = {"status": "host_heightfield_contact_attached", "materials": sorted(material.name for material in materials),
                                 "surface": str(field.surface), "valid_cells": field.valid_cells, "total_cells": field.total_cells, "boundary": BOUNDARY}
    scene["endfield_terrain_contact_audit"] = json.dumps(audit)
    return audit
