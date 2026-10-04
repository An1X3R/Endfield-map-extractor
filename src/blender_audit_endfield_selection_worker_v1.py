"""Reopen, stamp, and audit a bounded Endfield selection blend.

This script is intended to run inside Blender.  The coordinator invokes it
after assembly (and after an optional partition merge) so the resulting blend
contains a small, machine-readable selection contract and has been verified by
an independent reopen.
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
import sys
import traceback
from typing import Any, Iterable, Mapping, Sequence

try:
    import bpy
except ModuleNotFoundError:  # Pure helper tests run outside Blender.
    bpy = None  # type: ignore[assignment]


AUDIT_FORMAT = "EndfieldBlenderSelectionReopenAudit/1"
UNIT_FORMAT = "EndfieldBlenderSelectionUnit/1"
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
        description="Reopen, stamp, save, and audit an Endfield selection blend"
    )
    parser.add_argument("--input", required=True, type=Path)
    parser.add_argument("--output", required=True, type=Path)
    parser.add_argument("--unit-manifest", required=True, type=Path)
    parser.add_argument("--expected-map", required=True, choices=sorted(SOURCE_MAPS))
    parser.add_argument("--xmin", required=True, type=float)
    parser.add_argument("--xmax", required=True, type=float)
    parser.add_argument("--zmin", required=True, type=float)
    parser.add_argument("--zmax", required=True, type=float)
    parser.add_argument("--stamp", action="store_true")
    parser.add_argument("--remove-instances", action="store_true")
    # The coordinator uses this when terrain=false.  It is deliberately kept
    # here even though the first worker contract only required instance removal.
    parser.add_argument("--remove-terrain", action="store_true")
    return parser.parse_args(blender_arguments())


def utc_timestamp() -> str:
    return datetime.datetime.now(datetime.timezone.utc).isoformat()


def read_json(path: Path) -> dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8-sig"))


def write_json(path: Path, payload: Mapping[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    if path.exists() or temporary.exists():
        raise RuntimeError(f"refusing to overwrite audit output: {path}")
    temporary.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    temporary.replace(path)


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest().upper()


def expected_bounds(options: argparse.Namespace) -> tuple[float, float, float, float]:
    bounds = (options.xmin, options.xmax, options.zmin, options.zmax)
    if not all(math.isfinite(value) for value in bounds):
        raise RuntimeError("expected bounds must be finite")
    if bounds[1] <= bounds[0] or bounds[3] <= bounds[2]:
        raise RuntimeError("expected bounds must be positive half-open ranges")
    return bounds


def bounds_dict(bounds: Sequence[float]) -> dict[str, float]:
    return dict(zip(("xmin", "xmax", "zmin", "zmax"), map(float, bounds)))


def bounds_from_mapping(value: Mapping[str, Any]) -> tuple[float, float, float, float]:
    return tuple(float(value[name]) for name in ("xmin", "xmax", "zmin", "zmax"))


def parse_scene_bounds(value: Any) -> tuple[float, float, float, float] | None:
    if isinstance(value, str):
        if value == "complete":
            return None
        try:
            parsed = ast.literal_eval(value)
        except (SyntaxError, ValueError):
            try:
                parsed = json.loads(value)
            except (TypeError, json.JSONDecodeError):
                return None
    else:
        parsed = value
    if isinstance(parsed, Mapping):
        try:
            return bounds_from_mapping(parsed)
        except (KeyError, TypeError, ValueError):
            return None
    if isinstance(parsed, (list, tuple)) and len(parsed) == 4:
        try:
            return tuple(float(item) for item in parsed)
        except (TypeError, ValueError):
            return None
    return None


def json_safe(value: Any) -> Any:
    if value is None or isinstance(value, (bool, int, float, str)):
        return value
    if isinstance(value, Mapping):
        return {str(key): json_safe(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [json_safe(item) for item in value]
    return str(value)


def assertion(
    rows: list[dict[str, Any]],
    name: str,
    passed: bool,
    *,
    expected: Any = None,
    actual: Any = None,
    details: Any = None,
) -> None:
    row: dict[str, Any] = {"name": name, "passed": bool(passed)}
    if expected is not None:
        row["expected"] = json_safe(expected)
    if actual is not None:
        row["actual"] = json_safe(actual)
    if details is not None:
        row["details"] = json_safe(details)
    rows.append(row)


def finite_matrix(obj: bpy.types.Object) -> bool:
    return all(math.isfinite(float(value)) for row in obj.matrix_world for value in row)


def is_footprint_overlap_extra(obj: Any) -> bool:
    return bool(obj.get("endfield_footprint_overlap_extra")) or str(obj.name).startswith("XTRA_")


def is_instancer(obj: bpy.types.Object) -> bool:
    if obj.type != "MESH" or obj.data is None:
        return False
    if obj.name.startswith("INST_"):
        return True
    return "instance_count" in obj and any(modifier.type == "NODES" for modifier in obj.modifiers)


def is_instance_payload(obj: bpy.types.Object) -> bool:
    return (
        is_instancer(obj)
        or is_footprint_overlap_extra(obj)
        or "unresolved_count" in obj
        or "report_only_nonmodel_count" in obj
        or obj.name in {"Unresolved_Entity_Positions", "Report_Only_NonModel_Entity_Positions"}
    )


def is_terrain_payload(obj: bpy.types.Object) -> bool:
    return obj.name.startswith("Terrain_")


def finite_numeric_sequence(value: Any, length: int) -> tuple[float, ...] | None:
    if isinstance(value, (str, bytes, Mapping)):
        return None
    try:
        result = tuple(float(item) for item in value)
    except (TypeError, ValueError):
        return None
    if len(result) != length or not all(math.isfinite(item) for item in result):
        return None
    return result


def point_inside_half_open(
    bounds: tuple[float, float, float, float],
    point: Sequence[float],
) -> bool:
    xmin, xmax, zmin, zmax = bounds
    return xmin <= float(point[0]) < xmax and zmin <= float(point[1]) < zmax


def footprint_is_ordered(footprint: Sequence[float]) -> bool:
    return float(footprint[0]) < float(footprint[1]) and float(footprint[2]) < float(footprint[3])


def footprint_intersects_half_open(
    bounds: tuple[float, float, float, float],
    footprint: Sequence[float],
) -> bool:
    xmin, xmax, zmin, zmax = bounds
    fxmin, fxmax, fzmin, fzmax = map(float, footprint)
    return fxmax > xmin and fxmin < xmax and fzmax > zmin and fzmin < zmax


def inspect_footprint_extras(
    objects: Iterable[bpy.types.Object],
    bounds: tuple[float, float, float, float],
) -> dict[str, Any]:
    rows: list[dict[str, Any]] = []
    invalid_entity_keys: list[dict[str, Any]] = []
    duplicate_entity_keys: list[dict[str, Any]] = []
    invalid_origins: list[dict[str, Any]] = []
    origins_inside_bounds: list[dict[str, Any]] = []
    invalid_footprints: list[dict[str, Any]] = []
    nonintersecting_footprints: list[dict[str, Any]] = []
    seen_entity_keys: dict[tuple[str, int], str] = {}

    for obj in objects:
        if not is_footprint_overlap_extra(obj):
            continue
        name = str(obj.name)
        entity_base = str(obj.get("entity_base") or "")
        raw_entity_id = obj.get("entity_id")
        entity_id: int | None = None
        if not isinstance(raw_entity_id, bool):
            try:
                entity_id = int(raw_entity_id)
            except (TypeError, ValueError):
                pass
        entity_key = (entity_base, entity_id) if entity_base and entity_id is not None else None
        if entity_key is None:
            invalid_entity_keys.append(
                {"object": name, "entityBase": entity_base, "entityId": json_safe(raw_entity_id)}
            )
        elif entity_key in seen_entity_keys:
            duplicate_entity_keys.append(
                {
                    "object": name,
                    "entityBase": entity_base,
                    "entityId": entity_id,
                    "firstObject": seen_entity_keys[entity_key],
                }
            )
        else:
            seen_entity_keys[entity_key] = name

        origin = finite_numeric_sequence(obj.get("origin_unity_xz"), 2)
        if origin is None:
            invalid_origins.append({"object": name, "value": json_safe(obj.get("origin_unity_xz"))})
        elif point_inside_half_open(bounds, origin):
            origins_inside_bounds.append({"object": name, "originUnityXZ": list(origin)})

        footprint = finite_numeric_sequence(obj.get("footprint_unity_xz"), 4)
        if footprint is None or not footprint_is_ordered(footprint):
            invalid_footprints.append(
                {"object": name, "value": json_safe(obj.get("footprint_unity_xz"))}
            )
        elif not footprint_intersects_half_open(bounds, footprint):
            nonintersecting_footprints.append(
                {"object": name, "footprintUnityXZ": list(footprint)}
            )

        rows.append(
            {
                "object": name,
                "entityBase": entity_base,
                "entityId": entity_id,
                "originUnityXZ": list(origin) if origin is not None else None,
                "footprintUnityXZ": list(footprint) if footprint is not None else None,
            }
        )

    return {
        "extraCount": len(rows),
        "extras": rows,
        "invalidEntityKeys": invalid_entity_keys,
        "duplicateEntityKeys": duplicate_entity_keys,
        "invalidOrigins": invalid_origins,
        "originsInsideBounds": origins_inside_bounds,
        "invalidFootprints": invalid_footprints,
        "nonintersectingFootprints": nonintersecting_footprints,
    }


def declared_footprint_extra_count(scene: bpy.types.Scene) -> tuple[int | None, Any]:
    raw = scene.get("footprint_overlap_extra_count")
    if raw is None:
        return 0, raw
    if isinstance(raw, bool):
        return None, raw
    try:
        parsed = int(raw)
        numeric = float(raw)
    except (TypeError, ValueError, OverflowError):
        return None, raw
    if not math.isfinite(numeric) or numeric != float(parsed) or parsed < 0:
        return None, raw
    return parsed, raw


def append_footprint_extra_assertions(
    assertions: list[dict[str, Any]],
    scene: bpy.types.Scene,
    objects: Iterable[bpy.types.Object],
    bounds: tuple[float, float, float, float],
) -> dict[str, Any]:
    footprint_extra_audit = inspect_footprint_extras(objects, bounds)
    declared_extra_count, raw_declared_extra_count = declared_footprint_extra_count(scene)
    assertion(
        assertions,
        "footprint_extra_declared_count_matches",
        declared_extra_count == footprint_extra_audit["extraCount"],
        expected=footprint_extra_audit["extraCount"],
        actual=declared_extra_count,
        details={"rawSceneValue": json_safe(raw_declared_extra_count)},
    )
    entity_key_failures = (
        footprint_extra_audit["invalidEntityKeys"] + footprint_extra_audit["duplicateEntityKeys"]
    )
    assertion(
        assertions,
        "footprint_extra_entity_keys_unique",
        not entity_key_failures,
        expected=0,
        actual=len(entity_key_failures),
        details=entity_key_failures[:100],
    )
    origin_failures = (
        footprint_extra_audit["invalidOrigins"] + footprint_extra_audit["originsInsideBounds"]
    )
    assertion(
        assertions,
        "footprint_extra_origins_outside_half_open_unity_xz",
        not origin_failures,
        expected=0,
        actual=len(origin_failures),
        details={"bounds": bounds_dict(bounds), "samples": origin_failures[:100]},
    )
    assertion(
        assertions,
        "footprint_extra_footprints_finite_and_ordered",
        not footprint_extra_audit["invalidFootprints"],
        expected=0,
        actual=len(footprint_extra_audit["invalidFootprints"]),
        details=footprint_extra_audit["invalidFootprints"][:100],
    )
    footprint_intersection_failures = (
        footprint_extra_audit["invalidFootprints"]
        + footprint_extra_audit["nonintersectingFootprints"]
    )
    assertion(
        assertions,
        "footprint_extra_footprints_intersect_half_open_unity_xz",
        not footprint_intersection_failures,
        expected=0,
        actual=len(footprint_intersection_failures),
        details={"bounds": bounds_dict(bounds), "samples": footprint_intersection_failures[:100]},
    )
    return footprint_extra_audit


def attribute_vectors(obj: bpy.types.Object, name: str) -> Iterable[Sequence[float]]:
    attribute = obj.data.attributes.get(name) if obj.type == "MESH" else None
    if attribute is None:
        return []
    return [tuple(item.vector) for item in attribute.data]


def inspect_instancers(
    objects: Iterable[bpy.types.Object],
    bounds: tuple[float, float, float, float],
) -> dict[str, Any]:
    xmin, xmax, zmin, zmax = bounds
    rows = []
    point_count = 0
    nonfinite_points = []
    outside_points = []
    count_mismatches = []
    nonfinite_attributes = []
    for obj in objects:
        if not is_instancer(obj):
            continue
        vertices = list(obj.data.vertices)
        point_count += len(vertices)
        declared = obj.get("instance_count")
        if declared is not None and int(declared) != len(vertices):
            count_mismatches.append(
                {"object": obj.name, "declared": int(declared), "actual": len(vertices)}
            )
        object_outside = 0
        object_nonfinite = 0
        for index, vertex in enumerate(vertices):
            world = obj.matrix_world @ vertex.co
            values = tuple(float(value) for value in world)
            if not all(math.isfinite(value) for value in values):
                object_nonfinite += 1
                if len(nonfinite_points) < 100:
                    nonfinite_points.append({"object": obj.name, "pointIndex": index, "blenderXYZ": values})
                continue
            unity_x = values[0]
            unity_z = -values[1]
            if not (xmin <= unity_x < xmax and zmin <= unity_z < zmax):
                object_outside += 1
                if len(outside_points) < 100:
                    outside_points.append(
                        {
                            "object": obj.name,
                            "pointIndex": index,
                            "blenderXYZ": values,
                            "derivedUnityXZ": [unity_x, unity_z],
                        }
                    )
        bad_attributes = 0
        for name in ("ef_rotation", "ef_scale"):
            for index, vector in enumerate(attribute_vectors(obj, name)):
                if not all(math.isfinite(float(value)) for value in vector):
                    bad_attributes += 1
                    if len(nonfinite_attributes) < 100:
                        nonfinite_attributes.append(
                            {"object": obj.name, "attribute": name, "pointIndex": index, "value": list(vector)}
                        )
        rows.append(
            {
                "object": obj.name,
                "pointCount": len(vertices),
                "outsideHalfOpenBounds": object_outside,
                "nonfinitePoints": object_nonfinite,
                "nonfiniteAttributes": bad_attributes,
            }
        )
    return {
        "instancerCount": len(rows),
        "pointCount": point_count,
        "instancers": rows,
        "countMismatches": count_mismatches,
        "nonfinitePoints": nonfinite_points,
        "outsidePoints": outside_points,
        "nonfiniteAttributes": nonfinite_attributes,
    }


def remove_objects(objects: Iterable[bpy.types.Object]) -> list[str]:
    removed = []
    for obj in list(objects):
        removed.append(obj.name)
        data = obj.data
        node_groups = [
            modifier.node_group
            for modifier in obj.modifiers
            if modifier.type == "NODES" and modifier.node_group is not None
        ]
        bpy.data.objects.remove(obj, do_unlink=True)
        if isinstance(data, bpy.types.Mesh) and data.users == 0:
            bpy.data.meshes.remove(data)
        for node_group in node_groups:
            if node_group.users == 0:
                bpy.data.node_groups.remove(node_group)
    return removed


def stamp_scene(
    scene: bpy.types.Scene,
    options: argparse.Namespace,
    bounds: tuple[float, float, float, float],
    manifest: Mapping[str, Any],
    manifest_sha256: str,
    removed_instances: Sequence[str],
    removed_terrain: Sequence[str],
) -> None:
    scene["source_game"] = "Arknights: Endfield"
    scene["source_map"] = SOURCE_MAPS[options.expected_map]
    scene["unity_region_xz"] = str(tuple(float(value) for value in bounds))
    scene["endfield_map_id"] = options.expected_map
    scene["endfield_axis_contract"] = AXIS_CONTRACT
    scene["endfield_bounds_semantics"] = BOUNDS_SEMANTICS
    scene["endfield_selection_bounds_json"] = json.dumps(
        bounds_dict(bounds), ensure_ascii=True, sort_keys=True
    )
    scene["endfield_selection_unit_format"] = str(manifest.get("format") or "")
    scene["endfield_selection_job_id"] = str(manifest.get("jobId") or "")
    scene["endfield_selection_unit_id"] = str(manifest.get("unitId") or "")
    scene["endfield_stage119_fingerprint"] = str(manifest.get("stage119Fingerprint") or "")
    scene["endfield_unit_manifest_sha256"] = manifest_sha256
    scene["endfield_layer_status_json"] = json.dumps(
        manifest.get("layerStatus") or {}, ensure_ascii=True, sort_keys=True
    )
    scene["endfield_instances_removed_by_worker"] = bool(options.remove_instances)
    scene["endfield_removed_instance_object_count"] = len(removed_instances)
    scene["endfield_terrain_removed_by_worker"] = bool(options.remove_terrain)
    scene["endfield_removed_terrain_object_count"] = len(removed_terrain)
    scene["endfield_selection_reopen_audited"] = True
    scene["endfield_selection_reopen_audit_utc"] = utc_timestamp()
    if options.remove_instances:
        scene["resolved_instance_count"] = 0
        scene["unresolved_instance_count"] = 0
        scene["report_only_nonmodel_instance_count"] = 0
        scene["instancer_count"] = 0
        scene["footprint_overlap_extra_count"] = 0
        scene["endfield_removed_footprint_extra_count"] = sum(
            1 for name in removed_instances if str(name).startswith("XTRA_")
        )
    if options.remove_terrain:
        scene["terrain_tile_count"] = 0
        scene["terrain_skipped"] = True


def run(options: argparse.Namespace) -> tuple[dict[str, Any], bool]:
    if bpy is None:
        raise RuntimeError("reopen auditor must run inside Blender")
    input_path = options.input.resolve()
    output_path = options.output.resolve()
    manifest_path = options.unit_manifest.resolve()
    if not input_path.is_file():
        raise RuntimeError(f"input blend is missing: {input_path}")
    if not manifest_path.is_file():
        raise RuntimeError(f"unit manifest is missing: {manifest_path}")
    if output_path.exists() or output_path.with_suffix(output_path.suffix + ".tmp").exists():
        raise RuntimeError(f"refusing to overwrite audit output: {output_path}")

    bounds = expected_bounds(options)
    manifest = read_json(manifest_path)
    manifest_sha256 = sha256_file(manifest_path)
    bpy.ops.wm.open_mainfile(filepath=str(input_path))
    scene = bpy.context.scene

    assertions: list[dict[str, Any]] = []
    assertion(assertions, "unit_manifest_format", manifest.get("format") == UNIT_FORMAT, expected=UNIT_FORMAT, actual=manifest.get("format"))
    assertion(assertions, "unit_manifest_map", manifest.get("mapId") == options.expected_map, expected=options.expected_map, actual=manifest.get("mapId"))
    manifest_bounds = None
    try:
        manifest_bounds = bounds_from_mapping(manifest.get("bounds") or {})
    except (KeyError, TypeError, ValueError):
        pass
    assertion(assertions, "unit_manifest_exact_half_open_bounds", manifest_bounds == bounds, expected=bounds_dict(bounds), actual=bounds_dict(manifest_bounds) if manifest_bounds else manifest.get("bounds"))
    assertion(assertions, "unit_manifest_axis_contract", manifest.get("axisContract") == AXIS_CONTRACT, expected=AXIS_CONTRACT, actual=manifest.get("axisContract"))

    original_source_map = scene.get("source_map")
    expected_source_map = SOURCE_MAPS[options.expected_map]
    source_map_ok = (
        isinstance(original_source_map, str)
        and original_source_map.casefold() == expected_source_map.casefold()
    )
    assertion(assertions, "scene_source_map", source_map_ok, expected=expected_source_map, actual=original_source_map)
    original_scene_bounds = parse_scene_bounds(scene.get("unity_region_xz"))
    assertion(assertions, "scene_exact_half_open_bounds", original_scene_bounds == bounds, expected=bounds_dict(bounds), actual=bounds_dict(original_scene_bounds) if original_scene_bounds else scene.get("unity_region_xz"))

    nonfinite_transforms = [obj.name for obj in scene.objects if not finite_matrix(obj)]
    assertion(assertions, "finite_object_transforms", not nonfinite_transforms, expected=0, actual=len(nonfinite_transforms), details=nonfinite_transforms[:100])

    instancer_audit = inspect_instancers(scene.objects, bounds)
    assertion(assertions, "instancer_declared_counts_match", not instancer_audit["countMismatches"], expected=0, actual=len(instancer_audit["countMismatches"]), details=instancer_audit["countMismatches"])
    assertion(assertions, "instancer_points_finite", not instancer_audit["nonfinitePoints"], expected=0, actual=len(instancer_audit["nonfinitePoints"]), details=instancer_audit["nonfinitePoints"])
    assertion(assertions, "instancer_attributes_finite", not instancer_audit["nonfiniteAttributes"], expected=0, actual=len(instancer_audit["nonfiniteAttributes"]), details=instancer_audit["nonfiniteAttributes"])
    assertion(assertions, "instancer_points_inside_half_open_unity_xz", not instancer_audit["outsidePoints"], expected=0, actual=len(instancer_audit["outsidePoints"]), details={"axisContract": AXIS_CONTRACT, "samples": instancer_audit["outsidePoints"]})

    footprint_extra_audit = append_footprint_extra_assertions(
        assertions, scene, scene.objects, bounds
    )

    removed_instances: list[str] = []
    removed_terrain: list[str] = []
    if options.remove_instances:
        removed_instances = remove_objects(obj for obj in list(scene.objects) if is_instance_payload(obj))
        remaining = [obj.name for obj in scene.objects if is_instance_payload(obj)]
        assertion(assertions, "instance_payload_removed", not remaining, expected=0, actual=len(remaining), details=remaining[:100])
        remaining_extras = [
            obj.name for obj in scene.objects
            if is_footprint_overlap_extra(obj) or str(obj.name).startswith("XTRA_")
        ]
        assertion(
            assertions,
            "footprint_extra_payload_removed",
            not remaining_extras,
            expected=0,
            actual=len(remaining_extras),
            details=remaining_extras[:100],
        )
    if options.remove_terrain:
        removed_terrain = remove_objects(obj for obj in list(scene.objects) if is_terrain_payload(obj))
        remaining = [obj.name for obj in scene.objects if is_terrain_payload(obj)]
        assertion(assertions, "terrain_payload_removed", not remaining, expected=0, actual=len(remaining), details=remaining[:100])

    pre_save_failures = [row for row in assertions if not row["passed"]]
    save_performed = False
    if options.stamp and not pre_save_failures:
        stamp_scene(
            scene,
            options,
            bounds,
            manifest,
            manifest_sha256,
            removed_instances,
            removed_terrain,
        )
        bpy.ops.wm.save_as_mainfile(filepath=str(input_path), check_existing=False)
        save_performed = input_path.is_file()
    assertion(
        assertions,
        "blend_stamp_saved",
        save_performed if options.stamp else True,
        expected=bool(options.stamp),
        actual=save_performed,
        details=(
            "stamp/save skipped because an earlier assertion failed"
            if options.stamp and pre_save_failures
            else None
        ),
    )

    failures = [row for row in assertions if not row["passed"]]
    payload = {
        "format": AUDIT_FORMAT,
        "generatedAt": utc_timestamp(),
        "input": {"path": str(input_path), "bytes": input_path.stat().st_size, "sha256": sha256_file(input_path)},
        "unitManifest": {"path": str(manifest_path), "bytes": manifest_path.stat().st_size, "sha256": manifest_sha256},
        "expectedMap": options.expected_map,
        "expectedSourceMap": expected_source_map,
        "bounds": bounds_dict(bounds),
        "boundsSemantics": BOUNDS_SEMANTICS,
        "axisContract": AXIS_CONTRACT,
        "operations": {
            "stampedAndSaved": save_performed,
            "removeInstances": bool(options.remove_instances),
            "removedInstanceObjects": removed_instances,
            "removeTerrain": bool(options.remove_terrain),
            "removedTerrainObjects": removed_terrain,
        },
        "scene": {
            "objectCount": len(scene.objects),
            "collectionCount": len(bpy.data.collections),
            "meshCount": len(bpy.data.meshes),
            "materialCount": len(bpy.data.materials),
            "sourceMapBeforeStamp": original_source_map,
            "unityRegionXZBeforeStamp": scene.get("unity_region_xz") if not options.stamp else str(original_scene_bounds),
        },
        "instancersBeforeRemoval": instancer_audit,
        "footprintExtrasBeforeRemoval": footprint_extra_audit,
        "assertions": assertions,
        "summary": {
            "assertionsPassed": not failures,
            "assertionCount": len(assertions),
            "failedAssertionCount": len(failures),
            "failedAssertions": [row["name"] for row in failures],
        },
    }
    write_json(output_path, payload)
    return payload, not failures


def main() -> int:
    options = arguments()
    payload, passed = run(options)
    print(json.dumps(payload["summary"], ensure_ascii=False, sort_keys=True), flush=True)
    return 0 if passed else 2


if __name__ == "__main__":
    try:
        exit_code = main()
    except SystemExit as error:
        exit_code = int(error.code or 0)
    except BaseException as error:
        print(f"SELECTION_AUDIT_ERROR: {type(error).__name__}: {error}", file=sys.stderr, flush=True)
        traceback.print_exc()
        sys.stdout.flush()
        sys.stderr.flush()
        os._exit(2)
    if exit_code:
        sys.stdout.flush()
        sys.stderr.flush()
        os._exit(exit_code)
