"""Exercise partial scene delivery using real exported assets and missing dependencies."""
from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys
import struct

import bpy

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from blender_export_prepared_scene import build_instances
from endfield_scene_export_plan import load_scene_export_plan
from endfield_scene_material_handoff import load_plan_material_catalog
from endfield_scene_material_contract import reference_key, supported_slots


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--plan", type=Path, required=True)
    parser.add_argument("--audit", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args(sys.argv[sys.argv.index("--") + 1:])
    plan = load_scene_export_plan(args.plan)
    materials, textures = load_plan_material_catalog(plan)
    source = next(row for row in plan["instances"] if row["material_refs"] and row["material_refs"][0] is not None)
    missing_mesh = source | {"mesh_file": str(args.output.parent / "intentionally_absent_mesh.obj")}
    missing_ref = dict(source["material_refs"][0]) | {"Source": "regression-absent-material"}
    missing_material = source | {"material_refs": [missing_ref], "submesh_material_passes": [[missing_ref]],
                                 "selected_submeshes": [0]}
    collection = bpy.data.collections.new("MissingResourceRegression")
    bpy.context.scene.collection.children.link(collection)
    skipped = build_instances([missing_mesh, missing_material, source], collection, materials, textures)
    assert len(skipped) == 2, skipped
    assert skipped[0]["missing_mesh"] is True
    assert skipped[1]["missing_materials"]
    assert len(collection.objects) == 1, "A missing dependency prevented the following valid asset from being built"
    source_material = materials[reference_key(source["material_refs"][0])]
    texture_key = reference_key(supported_slots(source_material)[0]["Texture"])
    texture_skips = build_instances([source], collection, materials,
                                   {key: value for key, value in textures.items() if key != texture_key})
    assert len(texture_skips) == 1 and texture_skips[0]["missing_textures"]
    assert len(collection.objects) == 1, "An incomplete texture dependency produced a partial mesh"
    invalid = source | {"matrix": []}
    try:
        build_instances([invalid], collection, materials, textures)
    except (ValueError, TypeError, struct.error):
        pass
    else:
        raise AssertionError("Invalid placement data was silently skipped")
    audit = json.loads(args.audit.read_text(encoding="utf-8"))
    assert audit["reopen_verified"] is True
    assert audit["skipped_resources"]
    assert audit["effect_summary"]["preview_components"] > 0
    assert all(row["reason"] for row in audit["skipped_resources"])
    args.output.write_text(json.dumps({"passed": True, "geometry_missing_dependencies": len(skipped),
        "scene_missing_dependencies": len(audit["skipped_resources"]),
        "retained_effect_components": audit["effect_summary"]["preview_components"],
        "missing_texture_skipped": True,
        "invalid_placement_rejected": True}, indent=2), encoding="utf-8")


if __name__ == "__main__":
    main()
