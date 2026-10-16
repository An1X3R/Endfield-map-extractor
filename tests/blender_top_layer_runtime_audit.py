"""Check runtime-input provenance on real source top-layer materials and reopen it."""
from __future__ import annotations

import json
from pathlib import Path
import sys

import bpy

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from blender_scene_materials import create_material
from endfield_scene_export_plan import load_scene_export_plan
from endfield_scene_material_contract import top_layer_runtime_limitations
from endfield_scene_material_handoff import load_plan_material_catalog
from endfield_shader_contract import has_verified_lit_top_layer


def main() -> None:
    plan_path, output, result_path = (Path(value) for value in sys.argv[sys.argv.index("--") + 1:])
    if output.exists() or result_path.exists():
        raise FileExistsError("Top-layer audit regression outputs must be new")
    plan = load_scene_export_plan(plan_path)
    records, textures = load_plan_material_catalog(plan)
    selected = tuple(record for record in records.values() if has_verified_lit_top_layer(record))
    if not selected:
        raise ValueError("Source plan has no supported top-layer materials")
    rows = []
    for record in selected:
        material = create_material(record, textures, True)
        material.use_fake_user = True
        expected = "; ".join(top_layer_runtime_limitations(record))
        if material["endfield_top_layer_limitations"] != expected or "uv_feed" in expected:
            raise ValueError(f"Source top-layer runtime inputs are mislabeled: material={record['Name']}")
        principled = next(node for node in material.node_tree.nodes if node.type == "BSDF_PRINCIPLED")
        if not all(principled.inputs[name].is_linked for name in ("Base Color", "Normal", "Roughness", "Metallic")):
            raise ValueError(f"Top-layer surface lost its existing source outputs: material={record['Name']}")
        rows.append({"name": material.name, "source": record["Source"], "path_id": record["PathId"],
                     "limitations": expected, "required_uv_selectors": {
                         name: record["Floats"][name] for name in ("_BaseUVSet", "_BasePbrMapUVSet", "_LayerBlendUVType")}})
    bpy.ops.wm.save_as_mainfile(filepath=str(output))
    bpy.ops.wm.open_mainfile(filepath=str(output))
    for row in rows:
        material = bpy.data.materials[row["name"]]
        if material["endfield_top_layer_limitations"] != row["limitations"]:
            raise ValueError(f"Saved source runtime audit differs: material={row['name']}")
        for node in material.node_tree.nodes:
            if node.type == "TEX_IMAGE" and (node.image is None or not node.image.packed_file):
                raise ValueError(f"Saved source texture is not packed: material={row['name']}, node={node.name}")
    result_path.write_text(json.dumps({"format": "TopLayerRuntimeAuditRegression/1", "source_plan": str(plan_path),
        "materials": rows, "reopened": True, "validation_scope": "source runtime-input labels and packed save/reopen",
        "pending_removed": False, "native_program_equivalence_claimed": False}, indent=2), encoding="utf-8")


if __name__ == "__main__":
    main()
