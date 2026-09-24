"""Measure water depth against evaluated opaque geometry, including instances.

Cutout and transmissive surfaces cannot define a continuous water bottom.
Unsupported surface graphs are reported rather than guessed to be opaque.
"""
from __future__ import annotations

from collections import Counter
from dataclasses import dataclass
import math

import bpy
from mathutils import Vector
from mathutils.bvhtree import BVHTree


@dataclass(frozen=True)
class BottomSource:
    object_name: str
    parent_name: str | None
    instanced: bool
    matrix_world: tuple[tuple[float, ...], ...]


@dataclass(frozen=True)
class DepthHit:
    depth: float
    source: BottomSource
    polygon: int


@dataclass(frozen=True)
class WaterDepthResult:
    object_name: str
    hits: tuple[DepthHit | None, ...]


@dataclass(frozen=True)
class WaterDepthAudit:
    waters: tuple[WaterDepthResult, ...]
    source_instances: int
    triangles_or_polygons: int
    excluded_surface_polygons: tuple[tuple[str, int], ...]


def bottom_surface_exclusion(material: bpy.types.Material | None) -> str | None:
    """Accept proven opaque surfaces independently of object/material names."""
    if material is None:
        return None
    if material.get("endfield_shader_family") in ("leaf", "card"):
        return "foliage_surface"
    if "endfield_alpha_clip_threshold" in material or material.get("endfield_alpha_blend"):
        return "authored_cutout_or_blend"
    if not material.use_nodes:
        return None if material.diffuse_color[3] == 1 else "transparent_surface"
    for engine in ("CYCLES", "EEVEE"):
        output = material.node_tree.get_output_node(engine)
        if output is None or not output.inputs["Surface"].is_linked:
            return "no_surface_output"
        shader = output.inputs["Surface"].links[0].from_node
        if shader.type != "BSDF_PRINCIPLED":
            return "unsupported_surface_graph"
        alpha = shader.inputs["Alpha"]
        transmission = shader.inputs["Transmission Weight"]
        if alpha.is_linked or alpha.default_value < 1:
            return "cutout_or_variable_alpha"
        if transmission.is_linked or transmission.default_value > 0:
            return "transmissive_surface"
    return None


def sample_water_depths(depsgraph: bpy.types.Depsgraph, waters: tuple[bpy.types.Object, ...],
                        maximum_depth: float, surface_offset: float) -> WaterDepthAudit:
    """Return measured hits; retain missing samples explicitly as None."""
    if (not waters or any(obj.type != "MESH" or not obj.data.vertices for obj in waters)
            or not math.isfinite(maximum_depth) or not math.isfinite(surface_offset)
            or not 0 < surface_offset < maximum_depth):
        raise ValueError("Water depth sampling requires mesh vertices and 0 < surface_offset < maximum_depth")
    excluded_objects = {obj.original for obj in waters}
    water_points = tuple(tuple(obj.matrix_world @ v.co for v in obj.data.vertices) for obj in waters)
    points = tuple(point for row in water_points for point in row)
    low = tuple(min(p[axis] for p in points) for axis in range(3))
    high = tuple(max(p[axis] for p in points) for axis in range(3))
    vertices: list[Vector] = []
    polygons: list[tuple[int, ...]] = []
    provenance: list[tuple[BottomSource, int]] = []
    reasons: Counter[str] = Counter()
    material_cache: dict[int, str | None] = {}
    sources = 0
    for item in depsgraph.object_instances:
        obj = item.object
        if (obj.type != "MESH" or obj.original in excluded_objects
                or obj.original.get("endfield_water_volume_source") or not item.show_self):
            continue
        if item.is_instance:
            if item.parent is not None and item.parent.hide_render:
                continue
        elif obj.hide_render:
            continue
        matrix = item.matrix_world.copy()
        bounds = tuple(matrix @ Vector(corner) for corner in obj.bound_box)
        if any(max(p[a] for p in bounds) < low[a] or min(p[a] for p in bounds) > high[a] for a in (0, 1)):
            continue
        if min(p.z for p in bounds) >= high[2] or max(p.z for p in bounds) < low[2] - maximum_depth:
            continue
        mesh = obj.to_mesh()
        try:
            accepted: list[tuple[int, tuple[int, ...]]] = []
            for face in mesh.polygons:
                material = obj.material_slots[face.material_index].material if face.material_index < len(obj.material_slots) else None
                key = material.as_pointer() if material else 0
                if key not in material_cache:
                    material_cache[key] = bottom_surface_exclusion(material)
                reason = material_cache[key]
                if reason is not None:
                    reasons[reason] += 1
                    continue
                accepted.append((face.index, tuple(face.vertices)))
            if not accepted:
                continue
            start = len(vertices)
            vertices.extend(matrix @ vertex.co for vertex in mesh.vertices)
            source = BottomSource(obj.original.name, item.parent.original.name if item.parent else None,
                                  item.is_instance, tuple(tuple(row) for row in matrix))
            polygons.extend(tuple(start + i for i in face) for _, face in accepted)
            provenance.extend((source, index) for index, _ in accepted)
            sources += 1
        finally:
            obj.to_mesh_clear()
    if not polygons:
        raise ValueError(f"No opaque bottom geometry overlaps the selected water: exclusions={dict(reasons)}")
    tree = BVHTree.FromPolygons(vertices, polygons)
    rows: list[WaterDepthResult] = []
    for obj, positions in zip(waters, water_points, strict=True):
        hits: list[DepthHit | None] = []
        for point in positions:
            _, _, index, distance = tree.ray_cast(point - Vector((0, 0, surface_offset)),
                                                 Vector((0, 0, -1)), maximum_depth - surface_offset)
            if index is None:
                hits.append(None)
            else:
                source, polygon = provenance[index]
                hits.append(DepthHit(distance + surface_offset, source, polygon))
        rows.append(WaterDepthResult(obj.name, tuple(hits)))
    return WaterDepthAudit(tuple(rows), sources, len(polygons), tuple(sorted(reasons.items())))
