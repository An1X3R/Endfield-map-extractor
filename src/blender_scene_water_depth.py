"""Measure water depth against evaluated opaque geometry, including instances.

Cutout and transmissive surfaces cannot define a continuous water bottom.
Unsupported surface graphs are reported rather than guessed to be opaque.
"""
from __future__ import annotations

from collections import Counter
from bisect import bisect_left, bisect_right
from dataclasses import dataclass
import math

import bpy
from mathutils import Vector
from mathutils.bvhtree import BVHTree


class WaterBottomGeometryMissing(ValueError):
    """No proven opaque geometry is available below the selected water."""


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
class BoundarySegment:
    axis: int
    position: float
    start: tuple[float, float]
    end: tuple[float, float]
    source: BottomSource
    polygon: int


def boundary_segments(points: tuple[Vector, ...], source: BottomSource, polygon: int,
                      low: tuple[float, ...], high: tuple[float, ...]) -> tuple[BoundarySegment, ...]:
    """Retain exact closed edges of nonvertical accepted bottom triangles."""
    a, b, c = points
    area = ((float(b.x) - a.x) * (float(c.y) - a.y)
            - (float(b.y) - a.y) * (float(c.x) - a.x))
    if area == 0:
        return ()
    segments: list[BoundarySegment] = []
    for axis in (0, 1):
        other = 1 - axis
        for left, right in ((a, b), (b, c), (c, a)):
            if left[axis] == right[axis] and left[axis] in (low[axis], high[axis]):
                start, end = sorted(((float(left[other]), float(left.z)),
                                     (float(right[other]), float(right.z))))
                segments.append(BoundarySegment(axis, float(left[axis]), start, end, source, polygon))
    return tuple(segments)


def boundary_segment_candidates(segments: tuple[BoundarySegment, ...], points: tuple[Vector, ...],
                                low: tuple[float, ...], high: tuple[float, ...]) -> dict[tuple[float, float], tuple[BoundarySegment, ...]]:
    """Index only queried edge positions, once, using closed interval searches."""
    queries: dict[tuple[int, float], set[float]] = {}
    for point in points:
        for axis in (0, 1):
            if point[axis] in (low[axis], high[axis]):
                queries.setdefault((axis, float(point[axis])), set()).add(float(point[1 - axis]))
    ordered = {key: sorted(values) for key, values in queries.items()}
    candidates: dict[tuple[float, float], list[BoundarySegment]] = {}
    for segment in segments:
        values = ordered.get((segment.axis, segment.position), [])
        for value in values[bisect_left(values, segment.start[0]):bisect_right(values, segment.end[0])]:
            key = (segment.position, value) if segment.axis == 0 else (value, segment.position)
            candidates.setdefault(key, []).append(segment)
    return {key: tuple(values) for key, values in candidates.items()}


def boundary_depth_hit(point: Vector, segments: tuple[BoundarySegment, ...],
                       maximum_depth: float, surface_offset: float) -> DepthHit | None:
    """Intersect an unshifted vertical ray with authenticated source edges."""
    nearest: DepthHit | None = None
    for segment in segments:
        value = float(point[1 - segment.axis])
        fraction = (value - segment.start[0]) / (segment.end[0] - segment.start[0])
        height = segment.start[1] + fraction * (segment.end[1] - segment.start[1])
        depth = float(point.z) - height
        if surface_offset <= depth <= maximum_depth and (nearest is None or depth < nearest.depth):
            nearest = DepthHit(depth, segment.source, segment.polygon)
    return nearest


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
    if "endfield_ordered_material_refs" in material:
        from blender_scene_material_passes import effective_opaque_surface

        material = effective_opaque_surface(material)
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
    segments: list[BoundarySegment] = []
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
            mesh.calc_loop_triangles()
            accepted: list[tuple[int, tuple[int, ...]]] = []
            for triangle in mesh.loop_triangles:
                face = mesh.polygons[triangle.polygon_index]
                material = obj.material_slots[face.material_index].material if face.material_index < len(obj.material_slots) else None
                key = material.as_pointer() if material else 0
                if key not in material_cache:
                    material_cache[key] = bottom_surface_exclusion(material)
                reason = material_cache[key]
                if reason is not None:
                    reasons[reason] += 1
                    continue
                accepted.append((face.index, tuple(triangle.vertices)))
            if not accepted:
                continue
            start = len(vertices)
            vertices.extend(matrix @ vertex.co for vertex in mesh.vertices)
            source = BottomSource(obj.original.name, item.parent.original.name if item.parent else None,
                                  item.is_instance, tuple(tuple(row) for row in matrix))
            polygons.extend(tuple(start + i for i in face) for _, face in accepted)
            provenance.extend((source, index) for index, _ in accepted)
            boundary_vertices = {i for i in range(len(mesh.vertices))
                                 if (vertices[start + i].x in (low[0], high[0])
                                     or vertices[start + i].y in (low[1], high[1]))}
            if boundary_vertices:
                for index, face in accepted:
                    if sum(i in boundary_vertices for i in face) >= 2:
                        segments.extend(boundary_segments(tuple(vertices[start + i] for i in face), source, index, low, high))
            sources += 1
        finally:
            obj.to_mesh_clear()
    if not polygons:
        raise WaterBottomGeometryMissing(f"No opaque bottom geometry overlaps the selected water: exclusions={dict(reasons)}")
    tree = BVHTree.FromPolygons(vertices, polygons, all_triangles=True)
    edge_candidates = boundary_segment_candidates(tuple(segments), points, low, high)
    rows: list[WaterDepthResult] = []
    for obj, positions in zip(waters, water_points, strict=True):
        hits: list[DepthHit | None] = []
        for point in positions:
            _, _, index, distance = tree.ray_cast(point - Vector((0, 0, surface_offset)),
                                                 Vector((0, 0, -1)), maximum_depth - surface_offset)
            hit = None
            if index is not None:
                source, polygon = provenance[index]
                hit = DepthHit(distance + surface_offset, source, polygon)
            edge_hit = boundary_depth_hit(point, edge_candidates.get((float(point.x), float(point.y)), ()),
                                          maximum_depth, surface_offset)
            if edge_hit is not None and (hit is None or edge_hit.depth < hit.depth):
                hit = edge_hit
            hits.append(hit)
        rows.append(WaterDepthResult(obj.name, tuple(hits)))
    return WaterDepthAudit(tuple(rows), sources, len(polygons), tuple(sorted(reasons.items())))
