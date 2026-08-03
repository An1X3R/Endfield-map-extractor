"""Merge reviewed global-coordinate Endfield partition blends.

The script appends each source scene into a dedicated partition collection,
retaining the source collection hierarchy and object world transforms.  It
never opens an input for writing and refuses to overwrite the requested output.
"""

from __future__ import annotations

import argparse
import ast
import datetime
import hashlib
import json
import math
import os
from pathlib import Path
import re
import sys
import traceback
from typing import Any, Mapping, Sequence

import bpy


MERGE_FORMAT = "EndfieldBlenderSelectionMerge/1"
AXIS_CONTRACT = "(X,Y,Z)=(Unity X,-Unity Z,Unity Y)"
BOUNDS_SEMANTICS = "half-open [min,max)"
SOURCE_MAPS = {
    "map01": "map01 / Valley IV",
    "map02": "map02 / Wuling",
}


def blender_arguments() -> list[str]:
    if "--" not in sys.argv:
        return []
    return sys.argv[sys.argv.index("--") + 1 :]


def arguments() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Merge global-coordinate Endfield selection partitions"
    )
    parser.add_argument("--output", required=True, type=Path)
    parser.add_argument("--input", required=True, action="append", type=Path)
    parser.add_argument("--map-id", required=True, choices=sorted(SOURCE_MAPS))
    parser.add_argument("--xmin", required=True, type=float)
    parser.add_argument("--xmax", required=True, type=float)
    parser.add_argument("--zmin", required=True, type=float)
    parser.add_argument("--zmax", required=True, type=float)
    return parser.parse_args(blender_arguments())


def utc_timestamp() -> str:
    return datetime.datetime.now(datetime.timezone.utc).isoformat()


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest().upper()


def checked_bounds(options: argparse.Namespace) -> tuple[float, float, float, float]:
    bounds = (options.xmin, options.xmax, options.zmin, options.zmax)
    if not all(math.isfinite(value) for value in bounds):
        raise RuntimeError("merged bounds must be finite")
    if bounds[1] <= bounds[0] or bounds[3] <= bounds[2]:
        raise RuntimeError("merged bounds must be positive half-open ranges")
    return bounds


def bounds_dict(bounds: Sequence[float]) -> dict[str, float]:
    return dict(zip(("xmin", "xmax", "zmin", "zmax"), map(float, bounds)))


def parse_scene_bounds(value: Any) -> tuple[float, float, float, float] | None:
    if not isinstance(value, str) or value == "complete":
        return None
    try:
        parsed = ast.literal_eval(value)
    except (SyntaxError, ValueError):
        try:
            parsed = json.loads(value)
        except json.JSONDecodeError:
            return None
    if isinstance(parsed, Mapping):
        try:
            return tuple(float(parsed[name]) for name in ("xmin", "xmax", "zmin", "zmax"))
        except (KeyError, TypeError, ValueError):
            return None
    if isinstance(parsed, (list, tuple)) and len(parsed) == 4:
        try:
            return tuple(float(item) for item in parsed)
        except (TypeError, ValueError):
            return None
    return None


def contains(
    outer: tuple[float, float, float, float],
    inner: tuple[float, float, float, float],
) -> bool:
    return (
        outer[0] <= inner[0]
        and inner[1] <= outer[1]
        and outer[2] <= inner[2]
        and inner[3] <= outer[3]
    )


def overlaps(
    left: tuple[float, float, float, float],
    right: tuple[float, float, float, float],
) -> bool:
    return (
        left[0] < right[1]
        and left[1] > right[0]
        and left[2] < right[3]
        and left[3] > right[2]
    )


def safe_name(value: str) -> str:
    cleaned = re.sub(r"[^0-9A-Za-z_.-]+", "_", value).strip("_.")
    return cleaned[:48] or "partition"


def finite_scene_transforms(scene: bpy.types.Scene) -> list[str]:
    return [
        obj.name
        for obj in scene.objects
        if not all(math.isfinite(float(value)) for row in obj.matrix_world for value in row)
    ]


def recursive_collection_count(root: bpy.types.Collection) -> int:
    seen: set[int] = set()

    def visit(collection: bpy.types.Collection) -> None:
        pointer = collection.as_pointer()
        if pointer in seen:
            return
        seen.add(pointer)
        for child in collection.children:
            visit(child)

    for child in root.children:
        visit(child)
    return len(seen)


def numeric_scene_counts(scene: bpy.types.Scene) -> dict[str, int | float]:
    result: dict[str, int | float] = {}
    for key in scene.keys():
        value = scene.get(key)
        if not str(key).endswith("_count") or isinstance(value, bool):
            continue
        if isinstance(value, int):
            result[str(key)] = int(value)
        elif isinstance(value, float) and math.isfinite(value):
            result[str(key)] = float(value)
    return result


def add_counts(
    aggregate: dict[str, int | float], values: Mapping[str, int | float]
) -> None:
    for key, value in values.items():
        aggregate[key] = aggregate.get(key, 0) + value


def append_partition_scene(
    target_scene: bpy.types.Scene,
    input_path: Path,
    index: int,
    expected_map: str,
    merged_bounds: tuple[float, float, float, float],
) -> tuple[dict[str, Any], tuple[float, float, float, float]]:
    with bpy.data.libraries.load(str(input_path), link=False) as (data_from, data_to):
        if not data_from.scenes:
            raise RuntimeError(f"input blend has no scene: {input_path}")
        data_to.scenes = [data_from.scenes[0]]
    source_scene = data_to.scenes[0]
    if source_scene is None:
        raise RuntimeError(f"failed to append source scene: {input_path}")

    source_map = source_scene.get("source_map")
    expected_source_map = SOURCE_MAPS[expected_map]
    if not isinstance(source_map, str) or source_map.casefold() != expected_source_map.casefold():
        raise RuntimeError(
            f"partition map mismatch for {input_path}: expected {expected_map}, got {source_map!r}"
        )
    source_bounds = parse_scene_bounds(source_scene.get("unity_region_xz"))
    if source_bounds is None:
        raise RuntimeError(f"partition has no bounded unity_region_xz metadata: {input_path}")
    if not all(math.isfinite(value) for value in source_bounds):
        raise RuntimeError(f"partition has nonfinite bounds: {input_path}")
    if not contains(merged_bounds, source_bounds):
        raise RuntimeError(
            f"partition bounds are outside merged bounds: {source_bounds} not in {merged_bounds}"
        )
    nonfinite = finite_scene_transforms(source_scene)
    if nonfinite:
        raise RuntimeError(
            f"partition contains nonfinite object transforms: {input_path}: {nonfinite[:10]}"
        )

    original_object_count = len(source_scene.objects)
    original_collection_count = recursive_collection_count(source_scene.collection)
    count_properties = numeric_scene_counts(source_scene)
    root = bpy.data.collections.new(
        f"PARTITION_{index:02d}_{safe_name(input_path.stem)}"
    )
    target_scene.collection.children.link(root)
    root["endfield_partition_source_name"] = input_path.name
    root["endfield_partition_source_sha256"] = sha256_file(input_path)
    root["endfield_partition_bounds_json"] = json.dumps(
        bounds_dict(source_bounds), ensure_ascii=True, sort_keys=True
    )
    root["endfield_axis_contract"] = AXIS_CONTRACT

    for child in list(source_scene.collection.children):
        root.children.link(child)
    for obj in list(source_scene.collection.objects):
        if root.objects.get(obj.name) is None:
            root.objects.link(obj)

    if target_scene.world is None and source_scene.world is not None:
        target_scene.world = source_scene.world
    bpy.data.scenes.remove(source_scene, do_unlink=True)

    retained_object_count = len(root.all_objects)
    if retained_object_count != original_object_count:
        raise RuntimeError(
            f"partition object preservation failed for {input_path}: "
            f"expected {original_object_count}, retained {retained_object_count}"
        )
    return (
        {
            "index": index,
            "path": str(input_path),
            "bytes": input_path.stat().st_size,
            "sha256": root["endfield_partition_source_sha256"],
            "sourceMap": source_map,
            "bounds": bounds_dict(source_bounds),
            "objectCount": original_object_count,
            "collectionCount": original_collection_count,
            "sceneCounts": count_properties,
            "rootCollection": root.name,
        },
        source_bounds,
    )


def run(options: argparse.Namespace) -> dict[str, Any]:
    output_path = options.output.resolve()
    input_paths = [path.resolve() for path in options.input]
    bounds = checked_bounds(options)
    if len(input_paths) < 2:
        raise RuntimeError("at least two --input partition blends are required")
    if len(set(input_paths)) != len(input_paths):
        raise RuntimeError("duplicate input partition paths are not allowed")
    if output_path.exists():
        raise RuntimeError(f"refusing to overwrite merged output: {output_path}")
    if output_path in input_paths:
        raise RuntimeError("merged output cannot be one of its inputs")
    for path in input_paths:
        if not path.is_file():
            raise RuntimeError(f"input partition blend is missing: {path}")

    output_path.parent.mkdir(parents=True, exist_ok=True)
    bpy.ops.wm.read_factory_settings(use_empty=True)
    target_scene = bpy.context.scene
    aggregate_counts: dict[str, int | float] = {}
    inputs = []
    input_bounds = []
    for index, input_path in enumerate(input_paths, 1):
        record, part_bounds = append_partition_scene(
            target_scene, input_path, index, options.map_id, bounds
        )
        for previous in input_bounds:
            if overlaps(previous, part_bounds):
                raise RuntimeError(
                    f"partition half-open regions overlap: {previous} and {part_bounds}"
                )
        input_bounds.append(part_bounds)
        inputs.append(record)
        add_counts(aggregate_counts, record["sceneCounts"])

    requested_area = (bounds[1] - bounds[0]) * (bounds[3] - bounds[2])
    partition_area = sum(
        (part[1] - part[0]) * (part[3] - part[2]) for part in input_bounds
    )
    if not math.isclose(partition_area, requested_area, rel_tol=0.0, abs_tol=1e-5):
        raise RuntimeError(
            f"partition bounds do not exactly cover merged bounds: "
            f"partitionArea={partition_area}, requestedArea={requested_area}"
        )

    for key, value in aggregate_counts.items():
        target_scene[key] = value
    target_scene["source_game"] = "Arknights: Endfield"
    target_scene["source_map"] = SOURCE_MAPS[options.map_id]
    target_scene["unity_region_xz"] = str(tuple(float(value) for value in bounds))
    target_scene["endfield_map_id"] = options.map_id
    target_scene["endfield_axis_contract"] = AXIS_CONTRACT
    target_scene["endfield_bounds_semantics"] = BOUNDS_SEMANTICS
    target_scene["endfield_selection_bounds_json"] = json.dumps(
        bounds_dict(bounds), ensure_ascii=True, sort_keys=True
    )
    target_scene["endfield_merge_format"] = MERGE_FORMAT
    target_scene["endfield_merge_utc"] = utc_timestamp()
    target_scene["endfield_merged_input_count"] = len(inputs)
    target_scene["endfield_partition_scene_counts_json"] = json.dumps(
        [record["sceneCounts"] for record in inputs], ensure_ascii=True, sort_keys=True
    )
    target_scene["endfield_aggregate_scene_counts_json"] = json.dumps(
        aggregate_counts, ensure_ascii=True, sort_keys=True
    )
    target_scene["endfield_merged_object_count"] = len(target_scene.objects)
    target_scene["endfield_merged_collection_count"] = len(bpy.data.collections)
    target_scene["endfield_merged_mesh_count"] = len(bpy.data.meshes)
    target_scene["endfield_merged_material_count"] = len(bpy.data.materials)

    if output_path.exists():
        raise RuntimeError(f"refusing to overwrite merged output: {output_path}")
    bpy.ops.wm.save_as_mainfile(filepath=str(output_path), check_existing=False)
    if not output_path.is_file():
        raise RuntimeError(f"Blender reported success without merged output: {output_path}")
    result = {
        "format": MERGE_FORMAT,
        "status": "completed",
        "output": {
            "path": str(output_path),
            "bytes": output_path.stat().st_size,
            "sha256": sha256_file(output_path),
        },
        "mapId": options.map_id,
        "sourceMap": SOURCE_MAPS[options.map_id],
        "bounds": bounds_dict(bounds),
        "boundsSemantics": BOUNDS_SEMANTICS,
        "axisContract": AXIS_CONTRACT,
        "inputs": inputs,
        "aggregateSceneCounts": aggregate_counts,
        "scene": {
            "objectCount": len(target_scene.objects),
            "collectionCount": len(bpy.data.collections),
            "meshCount": len(bpy.data.meshes),
            "materialCount": len(bpy.data.materials),
        },
    }
    return result


def main() -> int:
    result = run(arguments())
    print(json.dumps(result, ensure_ascii=False, sort_keys=True), flush=True)
    return 0


if __name__ == "__main__":
    try:
        exit_code = main()
    except SystemExit as error:
        exit_code = int(error.code or 0)
    except BaseException as error:
        print(f"SELECTION_MERGE_ERROR: {type(error).__name__}: {error}", file=sys.stderr, flush=True)
        traceback.print_exc()
        sys.stdout.flush()
        sys.stderr.flush()
        os._exit(2)
    if exit_code:
        sys.stdout.flush()
        sys.stderr.flush()
        os._exit(exit_code)
