"""Rebuild only source water in a saved scene and compare measured bottom misses."""
from __future__ import annotations

import json
from pathlib import Path
import sys

import bpy

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

import blender_scene_water_build as water_build
from blender_scene_water_depth import WaterDepthAudit, sample_water_depths


def main() -> None:
    blend, plan_path, baseline_path, result_path = (Path(value) for value in sys.argv[sys.argv.index("--") + 1:])
    baseline = json.loads(baseline_path.read_text(encoding="utf-8"))
    plan = json.loads(plan_path.read_text(encoding="utf-8-sig"))
    missing_before = {(row["entity_id"], tuple(row["world"])): row for call in baseline["sampling_calls"] for row in call["missing"]}
    missing_after: set[tuple[int, tuple[float, ...]]] = set()
    seen_before: set[tuple[int, tuple[float, ...]]] = set()
    recovered: list[dict[str, object]] = []
    sample_count = 0

    def capture(graph: bpy.types.Depsgraph, waters: tuple[bpy.types.Object, ...],
                maximum_depth: float, surface_offset: float) -> WaterDepthAudit:
        nonlocal sample_count
        audit = sample_water_depths(graph, waters, maximum_depth, surface_offset)
        for water, result in zip(waters, audit.waters, strict=True):
            entity = int(water["entity_id"])
            for index, (vertex, hit) in enumerate(zip(water.data.vertices, result.hits, strict=True)):
                sample_count += 1
                point = water.matrix_world @ vertex.co
                key = (entity, tuple(point))
                if hit is None:
                    missing_after.add(key)
                if key in missing_before:
                    seen_before.add(key)
                    if hit is not None:
                        recovered.append({"entity_id": entity, "vertex_index": index,
                            "world": list(point), "depth": hit.depth,
                            "bottom_object": hit.source.object_name, "bottom_polygon": hit.polygon})
        return audit

    bpy.ops.wm.open_mainfile(filepath=str(blend))
    for obj in tuple(bpy.context.scene.objects):
        if (obj.get("endfield_category") == "water" or obj.get("endfield_water_volume_source")
                or obj.get("endfield_water_volume_source_key")):
            bpy.data.objects.remove(obj, do_unlink=True)
    original = water_build.sample_water_depths
    water_build.sample_water_depths = capture
    try:
        rows = water_build.build_scene_water(plan["water_geometry"], plan["water_materials"], plan["bounds"])
    finally:
        water_build.sample_water_depths = original
    introduced = missing_after - missing_before.keys()
    if introduced:
        raise ValueError(f"Boundary repair introduced bottom misses: samples={sorted(introduced)}")
    # Restoring the initial hit avoids an unnecessary shore split; query the five
    # old post-split coordinates directly instead of requiring that split again.
    target_points = sorted(key[1] for key in missing_before if key[0] == 1353507845)
    mesh = bpy.data.meshes.new("Original boundary query positions")
    mesh.from_pydata(target_points, [], [])
    query = bpy.data.objects.new("Original boundary queries", mesh)
    bpy.context.scene.collection.objects.link(query)
    bpy.context.view_layer.update()
    target_hits = sample_water_depths(bpy.context.evaluated_depsgraph_get(), (query,), 150, 0.005).waters[0].hits
    target = []
    for point, hit in zip(target_points, target_hits, strict=True):
        if hit is None or point[0] != 256 or hit.source.object_name != "Terrain_35_50":
            raise ValueError(f"Expected original x=256 terrain boundary hit: point={point}, hit={hit}")
        target.append({"world": point, "depth": hit.depth, "bottom_object": hit.source.object_name,
                       "bottom_polygon": hit.polygon})
    if len(target) != 5:
        raise ValueError(f"Expected five original source boundary coordinates: samples={target}")
    result_path.write_text(json.dumps({"format": "SourceWaterBottomBoundaryRegression/1",
        "blend": str(blend), "baseline": str(baseline_path), "sample_count": sample_count,
        "missing_before": len(missing_before), "missing_after": len(missing_after),
        "new_missing": [], "recovered": recovered, "original_five_boundary_hits": target,
        "baseline_positions_present_in_rebuild": len(seen_before), "water_results": rows,
        "bvh_epsilon": 0, "saved_blend_unchanged": True}, indent=2), encoding="utf-8")


if __name__ == "__main__":
    main()
