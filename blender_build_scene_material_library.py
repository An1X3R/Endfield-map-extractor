"""Build a reference-selected static material library from existing SceneProbe exports."""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

import bpy

sys.path.insert(0, str(Path(__file__).resolve().parent))
from blender_scene_materials import create_material
from blender_hgrp_surface import configure_scene_transmission
from endfield_scene_material_contract import load_catalog, reference_key, supported_slots, surface_limitations


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--manifest", type=Path, action="append", required=True)
    parser.add_argument("--selection", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--audit", type=Path, required=True)
    options = parser.parse_args(sys.argv[sys.argv.index("--") + 1:])
    for path in (options.output, options.audit):
        if path.exists():
            raise FileExistsError(path)
    selection = json.loads(options.selection.read_text(encoding="utf-8-sig"))
    if not isinstance(selection, dict) or selection.get("format") != "EndfieldMaterialSelection/1" or not isinstance(selection.get("references"), list):
        raise ValueError("Selection requires EndfieldMaterialSelection/1 and references array")
    keys = tuple(reference_key(row) for row in selection["references"])
    if not keys or len(keys) != len(set(keys)):
        raise ValueError("Selection must contain nonempty unique material references")
    materials, textures = load_catalog(tuple(options.manifest))
    # Preflight the entire selection before constructing any Blender datablocks.
    for key in keys:
        if key not in materials:
            raise ValueError(f"Selected material not found: reference={key}")
        for slot in supported_slots(materials[key]):
            if reference_key(slot["Texture"]) not in textures:
                raise ValueError(f"Required texture missing: material={key}, property={slot['Property']}, reference={slot['Texture']}")
    bpy.ops.wm.read_factory_settings(use_empty=True)
    rows = []
    for index, key in enumerate(keys):
        record = materials[key]
        slots = supported_slots(record)
        material = create_material(record, textures, True)
        material.use_fake_user = True
        ignored = [slot["Property"] for slot in record["Textures"] if slot["Texture"] is not None and slot not in slots]
        material["endfield_unhandled_texture_properties"] = json.dumps(ignored)
        material["endfield_surface_limitations"] = json.dumps(surface_limitations(record))
        material["endfield_shader_reference"] = json.dumps(record["Shader"])
        bpy.ops.mesh.primitive_uv_sphere_add(segments=24, ring_count=12, location=((index % 6) * 3, (index // 6) * 3, 0))
        obj = bpy.context.object
        obj.name = record["Name"]
        obj.data.materials.append(material)
        for face in obj.data.polygons:
            face.use_smooth = True
        rows.append({"reference": {"Source": key[0], "PathId": key[1]}, "name": material.name,
                     "supported_properties": [slot["Property"] for slot in slots],
                     "unhandled_properties": ignored, "limitations": surface_limitations(record),
                     "shader": record["Shader"]})
    if any(material.get("endfield_refraction_adapter") for material in bpy.data.materials):
        configure_scene_transmission(bpy.context.scene)
    options.output.parent.mkdir(parents=True, exist_ok=True)
    options.audit.parent.mkdir(parents=True, exist_ok=True)
    bpy.ops.wm.save_as_mainfile(filepath=str(options.output), check_existing=False)
    bpy.ops.wm.open_mainfile(filepath=str(options.output))
    for row in rows:
        material = bpy.data.materials[row["name"]]
        if material.get("endfield_surface_contract") not in ("EndfieldStaticSurface/1", "EndfieldHGRPSurface/2"):
            raise ValueError(f"Reopened material contract missing: {row['name']}")
        for node in material.node_tree.nodes:
            if node.type == "TEX_IMAGE" and (node.image is None or node.image.packed_file is None):
                raise ValueError(f"Reopened texture not packed: material={row['name']}, node={node.name}")
    options.audit.write_text(json.dumps({"format": "EndfieldStaticMaterialLibraryAudit/1", "output": str(options.output),
        "status": "static_approximation_reopen_verified", "manifests": [str(p) for p in options.manifest],
        "materials": rows, "scene_slot_assignment": "not_applied"}, indent=2), encoding="utf-8")
    print(json.dumps({"event": "material_library_verified", "materials": len(rows), "output": str(options.output)}), flush=True)


if __name__ == "__main__":
    main()
