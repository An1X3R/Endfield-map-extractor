"""Read source-qualified mesh vertex streams for deterministic particle previews."""
from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
import json
import math
from pathlib import Path, PureWindowsPath
from typing import cast


class ParticleMeshGeometryError(ValueError):
    """A shape mesh identity, relative path, or vertex stream is invalid."""


@dataclass(frozen=True)
class ParticleMeshGeometry:
    source: str
    path_id: int
    vertices: tuple[tuple[float, float, float], ...]
    normals: tuple[tuple[float, float, float], ...]
    colors: tuple[tuple[float, float, float, float], ...]


def _record(value: object, context: str) -> Mapping[str, object]:
    if not isinstance(value, dict) or any(type(key) is not str for key in value):
        raise ParticleMeshGeometryError(f"Expected object: field={context}")
    return cast(dict[str, object], value)


def _field(row: Mapping[str, object], name: str, context: str) -> object:
    if name not in row:
        raise ParticleMeshGeometryError(f"Required field missing: field={context}.{name}")
    return row[name]


def _integer(value: object, context: str) -> int:
    if type(value) is not int:
        raise ParticleMeshGeometryError(f"Expected integer: field={context}, value={value!r}")
    return value


def _identity(value: object, context: str) -> tuple[str, int]:
    asset = _record(value, context)
    source = _field(asset, "Source", context)
    path_id = _integer(_field(asset, "PathId", context), f"{context}.PathId")
    if type(source) is not str or not source or _field(asset, "Type", context) != "Mesh":
        raise ParticleMeshGeometryError(f"Expected source-qualified Mesh: field={context}, asset={asset!r}")
    return source, path_id


def _numbers(value: object, length: int, context: str) -> tuple[float, ...]:
    if not isinstance(value, list) or len(value) != length:
        raise ParticleMeshGeometryError(f"Vertex stream length mismatch: field={context}, expected={length}")
    result: list[float] = []
    for index, item in enumerate(value):
        if type(item) not in (int, float) or not math.isfinite(float(item)):
            raise ParticleMeshGeometryError(f"Expected finite stream value: field={context}[{index}], value={item!r}")
        result.append(float(item))
    return tuple(result)


def parse_particle_mesh_geometry(value: object, asset: tuple[str, int], vertex_count: int) -> ParticleMeshGeometry:
    """Validate exact source identity and retain original vertices, normals, and colors."""
    geometry = _record(value, "ShapeMeshGeometry")
    if _field(geometry, "Format", "ShapeMeshGeometry") != "EndfieldShapeMeshGeometry/1":
        raise ParticleMeshGeometryError("Unsupported ShapeMeshGeometry format")
    if _field(geometry, "CoordinateSpace", "ShapeMeshGeometry") != "unity_mesh_local_xyz":
        raise ParticleMeshGeometryError("Shape mesh vertex streams must use unity_mesh_local_xyz")
    actual = _identity(_field(geometry, "Asset", "ShapeMeshGeometry"), "ShapeMeshGeometry.Asset")
    count = _integer(_field(geometry, "VertexCount", "ShapeMeshGeometry"), "ShapeMeshGeometry.VertexCount")
    if actual != asset or count != vertex_count or not 0 < count <= 1_000_000:
        raise ParticleMeshGeometryError(f"Shape mesh identity or vertex count mismatch: expected={asset}/{vertex_count}, actual={actual}/{count}")
    normal_components = _integer(_field(geometry, "NormalComponents", "ShapeMeshGeometry"), "ShapeMeshGeometry.NormalComponents")
    color_components = _integer(_field(geometry, "ColorComponents", "ShapeMeshGeometry"), "ShapeMeshGeometry.ColorComponents")
    if normal_components != 3 or color_components not in (0, 4):
        raise ParticleMeshGeometryError(f"Unsupported shape mesh stream components: normals={normal_components}, colors={color_components}")
    positions = _numbers(_field(geometry, "Vertices", "ShapeMeshGeometry"), count * 3, "ShapeMeshGeometry.Vertices")
    normals = _numbers(_field(geometry, "Normals", "ShapeMeshGeometry"), count * 3, "ShapeMeshGeometry.Normals")
    colors = _numbers(_field(geometry, "Colors", "ShapeMeshGeometry"), count * color_components, "ShapeMeshGeometry.Colors")
    vertices = tuple((positions[index], positions[index + 1], positions[index + 2]) for index in range(0, len(positions), 3))
    directions = tuple((normals[index], normals[index + 1], normals[index + 2]) for index in range(0, len(normals), 3))
    rgba = tuple((colors[index], colors[index + 1], colors[index + 2], colors[index + 3]) for index in range(0, len(colors), 4))
    if any(not math.isfinite(length) or length <= 1e-12
           for normal in directions for length in [sum(component * component for component in normal)]):
        raise ParticleMeshGeometryError(f"Shape mesh has a degenerate source normal: asset={asset}")
    return ParticleMeshGeometry(asset[0], asset[1], vertices, directions, rgba)


def load_particle_shape_geometry(manifest_value: object, manifest_path: Path,
                                 system_value: object) -> ParticleMeshGeometry | None:
    """Load an exact shape-mesh sidecar through its manifest-relative export path."""
    system = _record(system_value, "System")
    parameters = _record(_field(system, "Parameters", "System"), "Parameters")
    shape = _record(_field(parameters, "Shape", "Parameters"), "Shape")
    enabled = _field(shape, "Enabled", "Shape")
    kind = _integer(_field(shape, "Type", "Shape"), "Shape.Type")
    if type(enabled) is not bool:
        raise ParticleMeshGeometryError(f"Expected bool: field=Shape.Enabled, value={enabled!r}")
    if not enabled or kind != 6:
        return None
    references = _record(_field(shape, "References", "Shape"), "Shape.References")
    pointer = _record(_field(references, "Mesh", "Shape.References"), "Shape.References.Mesh")
    if _field(pointer, "ResolutionStatus", "Shape.References.Mesh") != "resolved" or _field(pointer, "IsResolved", "Shape.References.Mesh") is not True:
        raise ParticleMeshGeometryError("Mesh emission requires a resolved Shape.References.Mesh")
    asset = _identity(_field(pointer, "Target", "Shape.References.Mesh"), "Shape.References.Mesh.Target")
    if asset[1] == 0:
        raise ParticleMeshGeometryError("Mesh emission points to a null source mesh")
    manifest = _record(manifest_value, "Manifest")
    export_values = _field(manifest, "MeshExports", "Manifest")
    if not isinstance(export_values, list):
        raise ParticleMeshGeometryError("Manifest.MeshExports must be an array")
    matches = [entry for raw in export_values for entry in [_record(raw, "MeshExport")]
               if _identity(_field(entry, "Asset", "MeshExport"), "MeshExport.Asset") == asset]
    if len(matches) != 1:
        raise ParticleMeshGeometryError(f"Shape mesh requires exactly one source-qualified export: asset={asset}, count={len(matches)}")
    export = matches[0]
    file_value = _field(export, "ShapeGeometryFile", "MeshExport")
    if type(file_value) is not str or not file_value or "\0" in file_value:
        raise ParticleMeshGeometryError(f"Invalid ShapeGeometryFile: asset={asset}, file={file_value!r}")
    relative = PureWindowsPath(file_value)
    if relative.drive or relative.root or ".." in relative.parts:
        raise ParticleMeshGeometryError(f"ShapeGeometryFile must stay relative to its manifest: asset={asset}, file={file_value!r}")
    parent = manifest_path.resolve(strict=True).parent
    path = parent.joinpath(*relative.parts).resolve(strict=True)
    if not path.is_relative_to(parent) or not path.is_file():
        raise ParticleMeshGeometryError(f"Shape geometry is not a file within the manifest directory: path={path}, manifest={manifest_path}")
    geometry_value: object = json.loads(path.read_text(encoding="utf-8-sig"))
    count = _integer(_field(export, "VertexCount", "MeshExport"), "MeshExport.VertexCount")
    return parse_particle_mesh_geometry(geometry_value, asset, count)
