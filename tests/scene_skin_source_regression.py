"""Verify real SceneProbe skin streams, native renderer bindings and static pose evaluation."""
from __future__ import annotations

import argparse
from collections import Counter
import hashlib
import json
import math
from pathlib import Path
from typing import TypeAlias, cast

from endfield_scene_prefab_bounds import _multiply, _node_matrix
from endfield_scene_skin_contract import _inverse_matrix

Json: TypeAlias = None | bool | int | float | str | list["Json"] | dict[str, "Json"]
Object: TypeAlias = dict[str, Json]
Key: TypeAlias = tuple[str, int]
Matrix: TypeAlias = tuple[float, ...]
Point: TypeAlias = tuple[float, float, float]


def obj(value: Json) -> Object:
    if not isinstance(value, dict):
        raise ValueError(f"Expected object: {type(value).__name__}")
    return value


def rows(value: Json) -> list[Object]:
    if not isinstance(value, list):
        raise ValueError("Expected array")
    return [obj(row) for row in value]


def read(path: Path) -> Object:
    return obj(json.loads(path.read_text(encoding="utf-8-sig")))


def key(value: Json) -> Key:
    row = obj(value)
    if not isinstance(row.get("Source"), str) or type(row.get("PathId")) is not int:
        raise ValueError("Source-qualified reference missing")
    return cast(str, row["Source"]).casefold(), cast(int, row["PathId"])


def fingerprint(path: Path) -> Object:
    with path.open("rb") as stream:
        digest = hashlib.file_digest(stream, "sha256").hexdigest()
    return {"path": str(path), "bytes": path.stat().st_size, "sha256": digest}


def local_matrix(node: Object) -> Matrix:
    quaternion = cast(list[float], node["LocalRotation"])
    if abs(sum(value*value for value in quaternion)-1) > 1e-4:
        raise ValueError(f"Invalid serialized transform: {key(node)}")
    return _node_matrix(node)


def world_matrix(transform: Key, transforms: dict[Key, Object], chain: frozenset[Key]) -> Matrix:
    if transform in chain or transform not in transforms:
        raise ValueError(f"Missing/cyclic source transform: {transform}")
    node = transforms[transform]
    parent = key(node["Parent"])
    previous = tuple(float(row == column) for column in range(4) for row in range(4)) if parent[1] == 0 else world_matrix(parent, transforms, chain | {transform})
    return _multiply(previous, local_matrix(node))


def vertices(path: Path) -> tuple[Point, ...]:
    with path.open(encoding="utf-8-sig") as stream:
        result = [tuple(float(component) for component in line.split()[1:4]) for line in stream if line.startswith("v ")]
    return tuple((-point[0], point[1], point[2]) for point in result)


def transform_point(matrix: Matrix, point: Point) -> Point:
    return cast(Point, tuple(sum(matrix[column*4+row]*point[column] for column in range(3))+matrix[12+row] for row in range(3)))


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--manifest", type=Path, required=True)
    parser.add_argument("--source-audit", type=Path, required=True)
    parser.add_argument("--inputs", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    manifest, audit, inputs = read(args.manifest), read(args.source_audit), read(args.inputs)
    if manifest.get("MeshSkinStreamVersion") != 1:
        raise ValueError("SceneProbe skin stream version missing")
    for source in rows(inputs["inputs"]):
        for field in ("original_cache_path", "sample_path"):
            if fingerprint(Path(str(source[field])))["sha256"] != source["sha256"]:
                raise ValueError(f"Source input changed: {source[field]}")
    source_renderers = rows(obj(audit["skinned_source_support"])["unique_renderers"])
    nodes = {key(row): row for row in rows(manifest["Nodes"])}
    transforms = {key(row["TransformAsset"]): row for row in nodes.values() if row.get("TransformAsset") is not None}
    exports = {key(row["Asset"]): row for row in rows(manifest["MeshExports"])}
    checked_meshes: dict[Key, Object] = {}
    renderer_results: list[Object] = []
    for source in source_renderers:
        previous = obj(source["source_renderer"])
        current = nodes[key(previous)]
        skin = obj(current["Skin"])
        bones = rows(skin["Bones"])
        previous_bones = rows(source["bone_pointers"])
        if len(bones) != len(previous_bones) or any(weight != 0 for weight in cast(list[float], skin["BlendShapeWeights"])):
            raise ValueError(f"Bone count or nonzero morph weights: {key(previous)}")
        for bone, pointer in zip(bones, previous_bones, strict=True):
            if (not bone["IsResolved"] or bone["FileId"] != pointer["m_FileID"]
                    or obj(bone["Target"])["PathId"] != pointer["m_PathID"]
                    or pointer["m_FileID"] == 0 and key(bone["Target"])[0] != key(previous)[0]):
                raise ValueError(f"Native bone binding changed: {key(previous)}")
        export = exports[key(previous["Mesh"])]
        if export.get("SkinGeometryError") or not export.get("SkinGeometryFile"):
            raise ValueError(f"Source skin stream unresolved: {export}")
        stream = read(args.manifest.parent / str(export["SkinGeometryFile"]))
        if (stream["Format"] != "EndfieldSceneMeshSkin/1" or key(stream["Asset"]) != key(previous["Mesh"])
                or stream["CoordinateSpace"] != "unity_mesh_local_xyz_unreflected"
                or stream["MatrixConvention"] != "column_major_4x4_column_vectors"
                or stream["BindPoseSource"] != "serialized_type_tree_e_row_column"):
            raise ValueError("Skin identity or coordinate convention changed")
        bindposes = tuple(tuple(float(value) for value in cast(list[float], row)) for row in cast(list[Json], stream["BindPoses"]))
        offsets = tuple(cast(list[int], stream["Offsets"]))
        indices = tuple(cast(list[int], stream["Indices"]))
        weights = tuple(float(value) for value in cast(list[float], stream["Weights"]))
        count = int(str(stream["VertexCount"]))
        if (len(bindposes) != len(bones) or len(offsets) != count+1 or offsets[0] != 0
                or offsets[-1] != len(weights) or len(weights) != len(indices) or any(a >= b for a, b in zip(offsets, offsets[1:]))
                or not all(math.isfinite(value) for bind in bindposes for value in bind) or any(len(bind) != 16 for bind in bindposes)
                or any(not math.isfinite(weight) or weight <= 0 for weight in weights)
                or any(index < 0 or index >= len(bones) for index in indices) or stream["BlendShapeCount"] != 0):
            raise ValueError(f"Invalid skin arrays: {key(previous['Mesh'])}")
        for bind in bindposes: _inverse_matrix(bind)
        if any(abs(bind[index]) > 1e-5 for bind in bindposes for index in (3, 7, 11)) or any(abs(bind[15]-1) > 1e-5 for bind in bindposes):
            raise ValueError("Bind poses violate column-major affine convention")
        sums = tuple(sum(weights[offsets[index]:offsets[index+1]]) for index in range(count))
        if any(abs(value-1) > 5e-5 for value in sums):
            raise ValueError("Source weight sums exceed format quantization range")
        if stream["WeightSource"] == "serialized_single_bone_layout_unit_weight":
            if (stream["SerializedBonesPerVertex"] != 1 or obj(stream["WeightChannel"])["Components"] != 0
                    or any(value != 1 for value in weights) or any(b-a != 1 for a, b in zip(offsets, offsets[1:]))):
                raise ValueError("Invalid native single-bone layout")
        elif obj(stream["WeightChannel"])["Components"] != 4 or obj(stream["IndexChannel"])["Components"] != 4:
            raise ValueError("Explicit source skin channel layout changed")
        mesh_key = key(previous["Mesh"])
        obj_path = args.manifest.parent / str(export["File"])
        old_export = rows(source["mesh_exports"])[0]
        if fingerprint(obj_path)["sha256"] != fingerprint(Path(str(old_export["file"])))["sha256"]:
            raise ValueError(f"Existing source OBJ changed: {mesh_key}")
        for field in ("Uv0File", "Uv1File", "Uv2File"):
            old_file, new_file = obj(old_export["record"]).get(field), export.get(field)
            if bool(old_file) != bool(new_file) or old_file and fingerprint(Path(str(old_export["manifest"])).parent / str(old_file))["sha256"] != fingerprint(args.manifest.parent / str(new_file))["sha256"]:
                raise ValueError(f"Existing source UV changed: {mesh_key}/{field}")
        points = vertices(obj_path)
        if len(points) != count:
            raise ValueError("OBJ/source vertex count mismatch")
        renderer_world = world_matrix(key(current["TransformAsset"]), transforms, frozenset())
        inverse = _inverse_matrix(renderer_world)
        matrices = tuple(_multiply(_multiply(inverse, world_matrix(key(bone["Target"]), transforms, frozenset())), bind)
                         for bone, bind in zip(bones, bindposes, strict=True))
        posed = tuple(cast(Point, tuple(sum(transform_point(matrices[indices[influence]], point)[axis]*weights[influence]
                                           for influence in range(offsets[vertex], offsets[vertex+1])) for axis in range(3)))
                      for vertex, point in enumerate(points))
        if not all(math.isfinite(value) for point in posed for value in point):
            raise ValueError("Nonfinite evaluated serialized pose")
        checked_meshes[mesh_key] = {"asset": previous["Mesh"], "skin_file": fingerprint(args.manifest.parent / str(export["SkinGeometryFile"])),
            "vertices": count, "bones": len(bones), "weight_source": stream["WeightSource"],
            "serialized_bones_per_vertex": stream["SerializedBonesPerVertex"], "minimum_weight_sum": min(sums),
            "maximum_weight_sum": max(sums), "parser_weight_mismatch_vertices": stream["ParserWeightMismatchVertexCount"],
            "obj_and_uv_unchanged": True, "blend_shape_count": stream["BlendShapeCount"]}
        renderer_results.append({"renderer": current["RendererAsset"], "source_key": source["example_source_key"],
            "pending_instances": source["source_pending_instances"], "bones": len(bones), "source_bone_bindings_preserved": True,
            "maximum_serialized_pose_displacement": max(math.dist(before, after) for before, after in zip(points, posed, strict=True)),
            "renderer_local_posed_bounds": {"minimum": [min(point[axis] for point in posed) for axis in range(3)], "maximum": [max(point[axis] for point in posed) for axis in range(3)]}})
    report = {"format": "EndfieldSceneSkinSourceRegression/1", "verified": True, "manifest": fingerprint(args.manifest),
        "source_audit": fingerprint(args.source_audit), "inputs": fingerprint(args.inputs), "test_script": fingerprint(Path(__file__)),
        "target_unique_meshes": len(checked_meshes), "target_unique_renderers": len(renderer_results),
        "target_pending_instances": sum(int(str(row["pending_instances"])) for row in renderer_results),
        "weight_layout_counts": dict(Counter(str(row["weight_source"]) for row in checked_meshes.values())),
        "meshes": list(checked_meshes.values()), "renderers": renderer_results,
        "scope": "Authenticated cache-source static serialized pose only; no animation sampling or game-runtime pose claim. CPU LBS verifies the new source streams using existing hierarchy matrix helpers; Blender integration is a separate task."}
    args.output.write_text(json.dumps(report, ensure_ascii=False, indent=2)+"\n", encoding="utf-8")
    print(json.dumps({key: report[key] for key in ("verified", "target_unique_meshes", "target_unique_renderers", "target_pending_instances", "weight_layout_counts")}, ensure_ascii=False))


if __name__ == "__main__":
    main()
