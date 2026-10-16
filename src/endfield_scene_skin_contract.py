"""Validate native scene skin streams and serialize the authored static bone pose."""
from __future__ import annotations

from dataclasses import dataclass
import hashlib
import json
import math
from pathlib import Path
from typing import Mapping, TypedDict, cast

from endfield_scene_effects_plan import _PrefabCandidate
from endfield_scene_io import SceneSourceCacheSlot, cached_scene_source_value
from endfield_scene_material_contract import reference_key, require_object, resolve_export_path


class SourceSceneSkin(TypedDict):
    file: str
    sha256: str
    vertex_count: int
    bone_matrices: list[list[float]]
    status: str


@dataclass(frozen=True)
class SceneSkinPayload:
    asset_key: tuple[str, int]
    vertex_count: int
    bind_poses: tuple[tuple[float, ...], ...]
    offsets: tuple[int, ...]
    indices: tuple[int, ...]
    weights: tuple[float, ...]
    blend_shape_count: int
    sha256: str


_SKIN_CACHE = SceneSourceCacheSlot("native-scene-skin-v1", SceneSkinPayload, 128, 64 * 1024 * 1024)


def _numbers(value: object, label: str) -> tuple[float, ...]:
    if not isinstance(value, list) or any(type(item) not in (int, float) or not math.isfinite(item) for item in value):
        raise ValueError(f"Scene skin requires finite numeric array: field={label}")
    return tuple(float(item) for item in value)


def _integers(value: object, label: str) -> tuple[int, ...]:
    if not isinstance(value, list) or any(type(item) is not int for item in value):
        raise ValueError(f"Scene skin requires integer array: field={label}")
    return tuple(value)


def _read_skin(path: Path) -> SceneSkinPayload:
    payload = path.read_bytes()
    data = require_object(json.loads(payload), "scene skin stream")
    if (data.get("Format") != "EndfieldSceneMeshSkin/1"
            or data.get("CoordinateSpace") != "unity_mesh_local_xyz_unreflected"
            or data.get("MatrixConvention") != "column_major_4x4_column_vectors"):
        raise ValueError(f"Scene skin coordinate contract is unsupported: file={path}")
    asset_key = reference_key(data.get("Asset"))
    count = data.get("VertexCount")
    shape_count = data.get("BlendShapeCount")
    if type(count) is not int or count <= 0 or type(shape_count) is not int or shape_count < 0:
        raise ValueError(f"Scene skin counts invalid: file={path}, vertices={count}, shapes={shape_count}")
    raw_binds = data.get("BindPoses")
    if not isinstance(raw_binds, list) or not raw_binds:
        raise ValueError(f"Scene skin has no source bind poses: file={path}")
    binds = tuple(_numbers(value, "BindPoses") for value in raw_binds)
    if any(len(value) != 16 for value in binds):
        raise ValueError(f"Scene skin bind pose must contain 16 values: file={path}")
    if any(max(abs(value[index]) for index in (3, 7, 11)) > 1e-5 or abs(value[15] - 1.0) > 1e-5 for value in binds):
        raise ValueError(f"Scene skin bind pose is not column-major affine: file={path}")
    offsets, indices = _integers(data.get("Offsets"), "Offsets"), _integers(data.get("Indices"), "Indices")
    weights = _numbers(data.get("Weights"), "Weights")
    if (len(offsets) != count + 1 or offsets[0] != 0 or offsets[-1] != len(weights)
            or len(indices) != len(weights) or any(a >= b for a, b in zip(offsets, offsets[1:]))
            or any(index < 0 or index >= len(binds) for index in indices) or any(weight <= 0 for weight in weights)):
        raise ValueError(f"Scene skin CSR weights or bone indices invalid: file={path}")
    sums = tuple(sum(weights[offsets[index]:offsets[index + 1]]) for index in range(count))
    if any(abs(value - 1.0) > 0.001 for value in sums):
        raise ValueError(f"Scene skin authored weights do not sum to one: file={path}, range={(min(sums), max(sums))}")
    return SceneSkinPayload(asset_key, count, binds, offsets, indices, weights, shape_count, hashlib.sha256(payload).hexdigest())


def load_scene_skin(path: Path) -> SceneSkinPayload:
    resolved = resolve_export_path(path)
    return cached_scene_source_value(_SKIN_CACHE, (resolved,), (), lambda: _read_skin(resolved))


def _inverse_matrix(matrix: tuple[float, ...]) -> tuple[float, ...]:
    """Invert a source column-major matrix with pivoted elimination."""
    rows = [[matrix[column * 4 + row] for column in range(4)] + [float(row == column) for column in range(4)]
            for row in range(4)]
    for column in range(4):
        pivot = max(range(column, 4), key=lambda row: abs(rows[row][column]))
        if abs(rows[pivot][column]) < 1e-12:
            raise ValueError("Prefab skinned renderer source transform is singular")
        rows[column], rows[pivot] = rows[pivot], rows[column]
        scale = rows[column][column]
        rows[column] = [value / scale for value in rows[column]]
        for row in range(4):
            if row != column:
                factor = rows[row][column]
                rows[row] = [left - factor * right for left, right in zip(rows[row], rows[column], strict=True)]
    return tuple(rows[row][column + 4] for column in range(4) for row in range(4))


def source_scene_skin(candidate: _PrefabCandidate, node: Mapping[str, object]) -> SourceSceneSkin:
    """Keep native bone identity and reject missing streams instead of exporting bind pose."""
    from endfield_scene_prefab_bounds import _multiply, _node_matrix
    from endfield_scene_prefab_geometry_plan import _owner_chain

    if candidate.data.get("MeshSkinStreamVersion") != 1:
        raise ValueError(f"Prefab skin source streams require exact resource refresh: prefab={candidate.path}")
    mesh_key = reference_key(require_object(node.get("Mesh"), "skinned mesh"))
    exports = [require_object(value, "mesh export") for value in cast(list[object], candidate.data["MeshExports"])]
    export = next((value for value in exports if reference_key(require_object(value["Asset"], "mesh export asset")) == mesh_key), None)
    if export is None or not isinstance(export.get("SkinGeometryFile"), str):
        raise ValueError(f"Prefab native skin geometry unavailable: mesh={mesh_key}, evidence={export}")
    path = resolve_export_path(candidate.manifest.parent / str(export["SkinGeometryFile"]))
    payload = load_scene_skin(path)
    if payload.asset_key != mesh_key:
        raise ValueError(f"Prefab skin stream references a different source mesh: expected={mesh_key}, actual={payload.asset_key}")
    skin = require_object(node.get("Skin"), "native skinned renderer")
    weights = _numbers(skin.get("BlendShapeWeights"), "BlendShapeWeights")
    if any(value != 0.0 for value in weights):
        raise ValueError(f"Prefab nonzero source blend shapes require shape geometry: mesh={mesh_key}, weights={weights}")
    bones = skin.get("Bones")
    if not isinstance(bones, list) or len(bones) != len(payload.bind_poses):
        raise ValueError(f"Prefab bone pointers differ from bind poses: mesh={mesh_key}")
    nodes = [require_object(value, "skin hierarchy node") for value in cast(list[object], candidate.data["Nodes"])]
    transforms = {reference_key(require_object(value["TransformAsset"], "source transform")): reference_key(value)
                  for value in nodes if value.get("TransformAsset") is not None}

    def root_matrix(owner: tuple[str, int]) -> tuple[float, ...]:
        result = tuple(float(row == column) for column in range(4) for row in range(4))
        for member in _owner_chain(candidate, owner)[1:]:
            result = _multiply(result, _node_matrix(member))
        return result

    renderer_matrix = root_matrix(reference_key(node))
    inverse = _inverse_matrix(renderer_matrix)
    matrices: list[list[float]] = []
    for value, bind in zip(bones, payload.bind_poses, strict=True):
        pointer = require_object(value, "source bone pointer")
        target = reference_key(require_object(pointer.get("Target"), "source bone transform"))
        if pointer.get("IsResolved") is not True or target not in transforms:
            raise ValueError(f"Prefab skin bone transform unresolved: mesh={mesh_key}, bone={target}")
        matrix = _multiply(_multiply(inverse, root_matrix(transforms[target])), bind)
        if not all(math.isfinite(value) for value in matrix):
            raise ValueError(f"Prefab skin pose matrix is non-finite: mesh={mesh_key}, bone={target}")
        matrices.append(list(matrix))
    return {"file": str(path), "sha256": payload.sha256, "vertex_count": payload.vertex_count,
            "bone_matrices": matrices, "status": "source_serialized_static_skin_pose_runtime_animation_unverified"}
