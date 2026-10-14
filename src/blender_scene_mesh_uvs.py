"""Attach actual ordinary mesh UV streams or explicitly audited absent-channel zeros."""
from __future__ import annotations

import json
import math
import struct
from dataclasses import dataclass
from pathlib import Path
from typing import Sequence

import bpy

from blender_scene_effect_meshes import _source_vertices, UnsupportedMeshVertexMapping
from endfield_scene_mesh_uv_contract import SourceUvStream


@dataclass(frozen=True)
class ObjCorner:
    vertex: int
    uv: tuple[float, float] | None


@dataclass(frozen=True)
class ObjGroup:
    name: str
    faces: tuple[tuple[ObjCorner, ...], ...]


def _source_groups(path: Path, count: int) -> tuple[ObjGroup, ...]:
    """Retain the exact SceneProbe OBJ face indices, including split submesh groups."""
    uvs: list[tuple[float, float]] = []
    groups: dict[str, list[tuple[ObjCorner, ...]]] = {}
    group = ""
    with path.open("r", encoding="utf-8-sig") as stream:
        for line_number, line in enumerate(stream, 1):
            fields = line.split()
            if not fields:
                continue
            if fields[0] == "g":
                group = " ".join(fields[1:])
            elif fields[0] == "vt":
                if len(fields) != 3:
                    raise ValueError(f"Source OBJ UV0 record invalid: path={path}, line={line_number}")
                value = (float(fields[1]), float(fields[2]))
                if not all(math.isfinite(component) for component in value):
                    raise ValueError(f"Source OBJ UV0 non-finite: path={path}, line={line_number}")
                uvs.append(value)
            elif fields[0] == "f":
                corners: list[ObjCorner] = []
                for field in fields[1:]:
                    indices = field.split("/")
                    vertex = int(indices[0]) - 1
                    uv = int(indices[1]) - 1 if len(indices) > 1 and indices[1] else None
                    if vertex < 0 or vertex >= count or (uv is not None and (uv < 0 or uv >= len(uvs))):
                        raise ValueError(f"Source OBJ face index invalid: path={path}, line={line_number}, corner={field}")
                    corners.append(ObjCorner(vertex, uvs[uv] if uv is not None else None))
                if len(corners) < 3:
                    raise ValueError(f"Source OBJ face requires at least three corners: path={path}, line={line_number}")
                groups.setdefault(group, []).append(tuple(corners))
    return tuple(ObjGroup(name, tuple(faces)) for name, faces in groups.items())


def _face_orders(face: tuple[ObjCorner, ...]) -> tuple[tuple[ObjCorner, ...], ...]:
    """OBJ import and reflection correction can reverse or rotate polygon corners."""
    reverse = tuple(reversed(face))
    return tuple(order[offset:] + order[:offset] for order in (face, reverse) for offset in range(len(face)))


def _group_vertex_indices(mesh: bpy.types.Mesh, source: tuple[tuple[float, float, float], ...],
                          group: ObjGroup) -> tuple[int, ...] | None:
    """Prove each vertex with indexed faces and UV0 rather than nearest-position guesses."""
    if len(mesh.polygons) != len(group.faces):
        return None
    uv0 = mesh.uv_layers.get("UVMap")
    candidates: list[set[int] | None] = [None] * len(mesh.vertices)
    for polygon, face in zip(mesh.polygons, group.faces, strict=True):
        if len(polygon.vertices) != len(face):
            return None
        orders = tuple(order for order in _face_orders(face) if all(
            max(abs(mesh.vertices[mesh.loops[loop].vertex_index].co[axis] - source[corner.vertex][axis])
                for axis in range(3)) <= 1e-4
            and (corner.uv is None or (uv0 is not None and
                 max(abs(uv0.data[loop].uv[axis] - corner.uv[axis]) for axis in range(2)) <= 1e-5))
            for loop, corner in zip(polygon.loop_indices, order, strict=True)))
        if not orders:
            return None
        for offset, vertex in enumerate(polygon.vertices):
            possible = {order[offset].vertex for order in orders}
            previous = candidates[vertex]
            candidates[vertex] = possible if previous is None else previous & possible
    if any(values is not None and not values for values in candidates):
        return None
    ambiguous = [(index, sorted(values) if values is not None else [])
                 for index, values in enumerate(candidates) if values is None or len(values) != 1]
    if ambiguous:
        raise UnsupportedMeshVertexMapping(
            f"Ordinary source UV indexed group mapping ambiguous: mesh={mesh.name}, group={group.name}, "
            f"vertices_and_source_candidates={ambiguous[:8]}, unresolved_count={len(ambiguous)}")
    indices = tuple(next(iter(values)) for values in candidates if values is not None)
    if len(set(indices)) != len(indices):
        raise UnsupportedMeshVertexMapping(
            f"Ordinary source UV group merged distinct imported vertices: mesh={mesh.name}, group={group.name}")
    if any(tuple(indices[vertex] for vertex in polygon.vertices) not in
           tuple(tuple(corner.vertex for corner in order) for order in _face_orders(face))
           for polygon, face in zip(mesh.polygons, group.faces, strict=True)):
        return None
    return indices


def _source_vertex_indices(mesh: bpy.types.Mesh, source: tuple[tuple[float, float, float], ...],
                           path: Path) -> tuple[int, ...]:
    """Match a split OBJ group without trusting Blender names or assuming full vertex order."""
    groups = _source_groups(path, len(source))
    mappings: dict[tuple[int, ...], list[str]] = {}
    for group in groups:
        indices = _group_vertex_indices(mesh, source, group)
        if indices is not None:
            mappings.setdefault(indices, []).append(group.name)
    if len(mappings) != 1:
        raise UnsupportedMeshVertexMapping(
            f"Ordinary source UV mesh lacks a unique indexed OBJ group: mesh={mesh.name}, path={path}, "
            f"imported_vertices={len(mesh.vertices)}, imported_faces={len(mesh.polygons)}, "
            f"matching_groups={tuple(mappings.values())}, source_groups={tuple(group.name for group in groups)}")
    return next(iter(mappings))


def apply_source_uv_streams(mesh: bpy.types.Mesh, streams: Sequence[SourceUvStream]) -> None:
    """Keep UVMap unchanged; a zero layer never pretends to contain exported UV data."""
    for stream in streams:
        channel, components, count = stream["channel"], stream["components"], stream["vertex_count"]
        name = f"EF_SOURCE_UV{channel}"
        if name in mesh.uv_layers:
            raise ValueError(f"Ordinary source UV layer already exists: mesh={mesh.name}, channel={channel}")
        if components == 0:
            if stream["status"] != "absent_source_zero_reference" or stream["file"] is not None:
                raise ValueError(f"Absent ordinary UV channel must retain its explicit zero-reference audit: stream={stream}")
            values = [0.0] * (len(mesh.loops) * 2)
        elif components == 2:
            if stream["status"] != "source_stream" or stream["file"] is None:
                raise ValueError(f"Actual ordinary UV channel lacks its source file: stream={stream}")
            source = _source_vertices(Path(stream["source_obj_file"]))
            if len(source) != count:
                raise ValueError(f"Ordinary UV metadata differs from source OBJ: vertices={len(source)}, metadata={count}")
            indices = _source_vertex_indices(mesh, source, Path(stream["source_obj_file"]))
            data = Path(stream["file"]).read_bytes()
            if len(data) != count * 8:
                raise ValueError(f"Ordinary UV stream length differs: file={stream['file']}, expected={count * 8}, actual={len(data)}")
            source_uv = struct.unpack(f"<{count * 2}f", data)
            if not all(math.isfinite(value) for value in source_uv):
                raise ValueError(f"Ordinary source UV stream is non-finite: file={stream['file']}")
            values = [source_uv[indices[loop.vertex_index] * 2 + axis] for loop in mesh.loops for axis in range(2)]
        else:
            raise ValueError(f"Ordinary source UV components unsupported: channel={channel}, components={components}")
        layer = mesh.uv_layers.new(name=name)
        layer.data.foreach_set("uv", values)
    mesh["endfield_source_uv_streams_json"] = json.dumps(list(streams))
