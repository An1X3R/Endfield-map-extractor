"""Clip submerged source water against verified terrain heightfield triangles."""
from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass
import math
import struct
from types import MappingProxyType

import bpy
from mathutils import Matrix, Vector


_EPS = 1e-9
_WELD = 1e-6
_CELL = 16.0


@dataclass(frozen=True)
class _Vertex:
    x: float
    y: float
    z: float
    uvs: tuple[tuple[float, float], ...]


@dataclass(frozen=True)
class _TerrainTriangle:
    xy: tuple[tuple[float, float], ...]
    bounds: tuple[float, float, float, float]
    plane: tuple[float, float, float]


@dataclass(frozen=True)
class WaterShoreIndex:
    terrain: tuple[_TerrainTriangle, ...]
    buckets: Mapping[tuple[int, int], tuple[int, ...]]


def _area(polygon: tuple[_Vertex, ...]) -> float:
    if len(polygon) < 3:
        return 0.0
    return abs(sum(a.x * b.y - b.x * a.y for a, b in zip(polygon, (*polygon[1:], polygon[0])))) * 0.5


def _clean(vertices: list[_Vertex]) -> tuple[_Vertex, ...]:
    result: list[_Vertex] = []
    for vertex in vertices:
        if not result or math.dist((vertex.x, vertex.y), (result[-1].x, result[-1].y)) > _EPS:
            result.append(vertex)
    if len(result) > 1 and math.dist((result[0].x, result[0].y), (result[-1].x, result[-1].y)) <= _EPS:
        result.pop()
    return tuple(result) if len(result) >= 3 else ()


def _lerp(a: _Vertex, b: _Vertex, t: float) -> _Vertex:
    return _Vertex(
        a.x + (b.x - a.x) * t,
        a.y + (b.y - a.y) * t,
        a.z + (b.z - a.z) * t,
        tuple((u[0] + (v[0] - u[0]) * t, u[1] + (v[1] - u[1]) * t)
              for u, v in zip(a.uvs, b.uvs, strict=True)),
    )


def _split(
    polygon: tuple[_Vertex, ...], signed: Callable[[_Vertex], float]
) -> tuple[tuple[_Vertex, ...], tuple[_Vertex, ...]]:
    if not polygon:
        return (), ()
    inside: list[_Vertex] = []
    outside: list[_Vertex] = []
    for a, b in zip(polygon, (*polygon[1:], polygon[0])):
        da, db = signed(a), signed(b)
        if da >= -_EPS:
            inside.append(a)
        if da <= _EPS:
            outside.append(a)
        if (da > _EPS and db < -_EPS) or (da < -_EPS and db > _EPS):
            middle = _lerp(a, b, da / (da - db))
            inside.append(middle)
            outside.append(middle)
    return _clean(inside), _clean(outside)


def _triangle_bounds(points: tuple[tuple[float, float], ...]) -> tuple[float, float, float, float]:
    return (min(point[0] for point in points), max(point[0] for point in points),
            min(point[1] for point in points), max(point[1] for point in points))


def _terrain_triangles(objects: tuple[bpy.types.Object, ...]) -> tuple[_TerrainTriangle, ...]:
    triangles: list[_TerrainTriangle] = []
    for obj in objects:
        if obj.type != "MESH" or obj.get("endfield_source_heightfield") is not True:
            raise ValueError(f"WATER_SHORE_UNVERIFIED_HEIGHTFIELD object={obj.name}")
        mesh = obj.data
        mesh.calc_loop_triangles()
        matrix = obj.matrix_world
        for source in mesh.loop_triangles:
            vertices = tuple(matrix @ mesh.vertices[index].co for index in source.vertices)
            xy = tuple((point.x, point.y) for point in vertices)
            determinant = ((xy[1][0] - xy[0][0]) * (xy[2][1] - xy[0][1])
                           - (xy[2][0] - xy[0][0]) * (xy[1][1] - xy[0][1]))
            if abs(determinant) <= _EPS:
                raise ValueError(f"WATER_SHORE_HEIGHTFIELD_TRIANGLE_DEGENERATE object={obj.name} face={source.polygon_index}")
            if determinant < 0:
                xy = (xy[0], xy[2], xy[1])
                vertices = (vertices[0], vertices[2], vertices[1])
                determinant = -determinant
            a = ((vertices[1].z - vertices[0].z) * (xy[2][1] - xy[0][1])
                 - (vertices[2].z - vertices[0].z) * (xy[1][1] - xy[0][1])) / determinant
            b = ((xy[1][0] - xy[0][0]) * (vertices[2].z - vertices[0].z)
                 - (xy[2][0] - xy[0][0]) * (vertices[1].z - vertices[0].z)) / determinant
            c = vertices[0].z - a * xy[0][0] - b * xy[0][1]
            triangles.append(_TerrainTriangle(xy, _triangle_bounds(xy), (a, b, c)))
    return tuple(triangles)


def _buckets(triangles: tuple[_TerrainTriangle, ...]) -> dict[tuple[int, int], tuple[int, ...]]:
    index: dict[tuple[int, int], list[int]] = {}
    for number, triangle in enumerate(triangles):
        xmin, xmax, ymin, ymax = triangle.bounds
        for x in range(math.floor(xmin / _CELL), math.floor(xmax / _CELL) + 1):
            for y in range(math.floor(ymin / _CELL), math.floor(ymax / _CELL) + 1):
                index.setdefault((x, y), []).append(number)
    return {key: tuple(value) for key, value in index.items()}


def prepare_water_shore_index(
    terrain_objects: tuple[bpy.types.Object, ...]
) -> WaterShoreIndex:
    """Build immutable terrain geometry and its spatial index once for many waters."""
    if not isinstance(terrain_objects, tuple):
        raise ValueError("WATER_SHORE_TERRAIN_OBJECTS_MUST_BE_TUPLE")
    terrain = _terrain_triangles(terrain_objects) if terrain_objects else ()
    buckets = _buckets(terrain) if terrain else {}
    return WaterShoreIndex(terrain, MappingProxyType(buckets))


def _candidates(
    polygon: tuple[_Vertex, ...], index: Mapping[tuple[int, int], tuple[int, ...]]
) -> tuple[int, ...]:
    xmin, xmax, ymin, ymax = _triangle_bounds(tuple((vertex.x, vertex.y) for vertex in polygon))
    return tuple(sorted({number
                         for x in range(math.floor(xmin / _CELL), math.floor(xmax / _CELL) + 1)
                         for y in range(math.floor(ymin / _CELL), math.floor(ymax / _CELL) + 1)
                         for number in index.get((x, y), ())}))


def _partition(
    polygon: tuple[_Vertex, ...], terrain: _TerrainTriangle
) -> tuple[tuple[_Vertex, ...], tuple[tuple[_Vertex, ...], ...]]:
    covered = polygon
    uncovered: list[tuple[_Vertex, ...]] = []
    for a, b in zip(terrain.xy, (*terrain.xy[1:], terrain.xy[0])):
        covered, outside = _split(
            covered,
            lambda point: (b[0] - a[0]) * (point.y - a[1]) - (b[1] - a[1]) * (point.x - a[0]),
        )
        if _area(outside) > _EPS:
            uncovered.append(outside)
        if not covered:
            break
    return covered, tuple(uncovered)


def _water_triangles(obj: bpy.types.Object) -> tuple[tuple[tuple[_Vertex, ...], int], ...]:
    mesh = obj.data
    mesh.calc_loop_triangles()
    uv_layers = tuple(mesh.uv_layers)
    result: list[tuple[tuple[_Vertex, ...], int]] = []
    for triangle in mesh.loop_triangles:
        points = tuple(
            _Vertex(mesh.vertices[index].co.x, mesh.vertices[index].co.y, mesh.vertices[index].co.z,
                    tuple(tuple(layer.data[loop].uv) for layer in uv_layers))
            for index, loop in zip(triangle.vertices, triangle.loops, strict=True)
        )
        if _area(points) <= _EPS:
            a, b, c = (Vector((point.x, point.y, point.z)) for point in points)
            if ((b - a).cross(c - a)).length * 0.5 <= _EPS:
                continue
            raise ValueError(f"WATER_SHORE_SURFACE_TRIANGLE_DEGENERATE object={obj.name} face={triangle.polygon_index}")
        result.append((points, mesh.polygons[triangle.polygon_index].material_index))
    return tuple(result)


def _pieces(
    polygon: tuple[_Vertex, ...], terrain: tuple[_TerrainTriangle, ...],
    index: Mapping[tuple[int, int], tuple[int, ...]], clearance: float,
) -> tuple[tuple[tuple[_Vertex, ...], ...], bool]:
    uncovered = [polygon]
    kept: list[tuple[_Vertex, ...]] = []
    dry = False
    for number in _candidates(polygon, index):
        triangle = terrain[number]
        next_uncovered: list[tuple[_Vertex, ...]] = []
        for fragment in uncovered:
            xmin, xmax, ymin, ymax = _triangle_bounds(tuple((point.x, point.y) for point in fragment))
            txmin, txmax, tymin, tymax = triangle.bounds
            if xmax <= txmin or xmin >= txmax or ymax <= tymin or ymin >= tymax:
                next_uncovered.append(fragment)
                continue
            overlap, outside = _partition(fragment, triangle)
            next_uncovered.extend(outside)
            if overlap:
                a, b, c = triangle.plane
                wet, removed = _split(overlap, lambda point: point.z - (a * point.x + b * point.y + c) - clearance)
                if _area(wet) > _EPS:
                    kept.append(wet)
                if _area(removed) > _EPS:
                    dry = True
        uncovered = next_uncovered
        if not uncovered:
            break
    kept.extend(uncovered)
    return tuple(kept), dry


def _replace_mesh(
    obj: bpy.types.Object, faces: tuple[tuple[tuple[_Vertex, ...], int], ...]
) -> None:
    original = obj.data
    uv_names = tuple(layer.name for layer in original.uv_layers)
    material_slots = tuple(original.materials)
    coordinates: list[tuple[float, float, float]] = []
    triangles: list[tuple[int, int, int]] = []
    loop_uvs: list[tuple[tuple[float, float], ...]] = []
    material_indices: list[int] = []
    welded: dict[tuple[int, int, int], int] = {}
    for polygon, material_index in faces:
        for middle in range(1, len(polygon) - 1):
            corners = (polygon[0], polygon[middle], polygon[middle + 1])
            if _area(corners) <= _EPS:
                continue
            indices: list[int] = []
            for vertex in corners:
                coordinate = struct.unpack("<3f", struct.pack("<3f", vertex.x, vertex.y, vertex.z))
                key = tuple(round(value / _WELD) for value in coordinate)
                if key not in welded:
                    welded[key] = len(coordinates)
                    coordinates.append(coordinate)
                indices.append(welded[key])
            if len(set(indices)) != 3:
                continue
            triangles.append(tuple(indices))
            loop_uvs.extend(vertex.uvs for vertex in corners)
            material_indices.append(material_index)
    replacement = bpy.data.meshes.new(original.name + "_ShoreClipped")
    replacement.from_pydata(coordinates, [], triangles)
    replacement.update()
    for material in material_slots:
        replacement.materials.append(material)
    for layer_index, name in enumerate(uv_names):
        layer = replacement.uv_layers.new(name=name)
        for loop_index, values in enumerate(loop_uvs):
            layer.data[loop_index].uv = values[layer_index]
    for polygon, material_index in zip(replacement.polygons, material_indices, strict=True):
        polygon.material_index = material_index
    obj.data = replacement
    if original.users == 0:
        bpy.data.meshes.remove(original)


def _validate_water_clip(obj: bpy.types.Object, clearance: float) -> None:
    if obj.type != "MESH" or obj.data.users != 1:
        raise ValueError(f"WATER_SHORE_PRIVATE_MESH_REQUIRED object={obj.name}")
    if not math.isfinite(clearance) or clearance <= 0:
        raise ValueError(f"WATER_SHORE_CLEARANCE_INVALID value={clearance}")


def _clip_water_with_index(
    obj: bpy.types.Object, shore_index: WaterShoreIndex, clearance: float
) -> int:
    if not obj.data.polygons:
        return 0
    if any(abs(obj.matrix_world[row][column] - Matrix.Identity(4)[row][column]) > 1e-8
           for row in range(4) for column in range(4)):
        raise ValueError(f"WATER_SHORE_WORLD_MESH_REQUIRED object={obj.name}")
    if not shore_index.terrain:
        return 0
    faces: list[tuple[tuple[_Vertex, ...], int]] = []
    cut = 0
    for polygon, material_index in _water_triangles(obj):
        fragments, dry = _pieces(polygon, shore_index.terrain, shore_index.buckets, clearance)
        faces.extend((fragment, material_index) for fragment in fragments)
        cut += int(dry)
    if cut:
        _replace_mesh(obj, tuple(faces))
    return cut


def clip_water_to_prepared_heightfield(
    obj: bpy.types.Object, shore_index: WaterShoreIndex, clearance: float
) -> int:
    """Clip one water mesh using terrain geometry prepared for reuse."""
    if not isinstance(shore_index, WaterShoreIndex):
        raise ValueError("WATER_SHORE_PREPARED_INDEX_REQUIRED")
    _validate_water_clip(obj, clearance)
    return _clip_water_with_index(obj, shore_index, clearance)


def clip_water_to_heightfield(
    obj: bpy.types.Object, terrain_objects: tuple[bpy.types.Object, ...], clearance: float
) -> int:
    """Remove only terrain-covered dry fragments; retain regions without terrain data."""
    if not isinstance(terrain_objects, tuple):
        raise ValueError(f"WATER_SHORE_PRIVATE_MESH_REQUIRED object={obj.name}")
    _validate_water_clip(obj, clearance)
    if not terrain_objects or not obj.data.polygons:
        return 0
    shore_index = prepare_water_shore_index(terrain_objects)
    return _clip_water_with_index(obj, shore_index, clearance)
