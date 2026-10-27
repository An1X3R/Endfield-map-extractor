"""Exercise original masked mesh particles through the production material factory."""
from __future__ import annotations

import argparse
import json
import math
from pathlib import Path
import sys

import bpy
from mathutils import Matrix, Vector


REPO = Path(__file__).resolve().parents[1]
TARGET_ENTITY = -8381094429167414857
TARGET_MATERIAL = ("cab-4aea24dc41d2cc46f19b50c32e82f406", 5042206994469934683)
TARGET_SYSTEMS = {-859354280327288640, 4570648031582120128}
SNAPSHOT_AGE = 3.6


def _finite(value: float, context: str) -> float:
    if not math.isfinite(value):
        raise ValueError(f"Non-finite sample value: field={context}, value={value!r}")
    return float(value)


def main(plan_path: Path, before_audit_path: Path, output: Path) -> None:
    output.mkdir(parents=True, exist_ok=True)
    blend_path = output / "butterfly_masked_vertex_offset.blend"
    image_path = output / "butterfly_masked_vertex_offset.png"
    audit_path = output / "butterfly_masked_vertex_offset_audit.json"
    for path in (blend_path, image_path, audit_path):
        if path.exists():
            raise FileExistsError(path)
    sys.path.insert(0, str(REPO / "src"))
    from blender_export_prepared_scene import build_effect_previews, effect_preview_camera
    from blender_scene_effects import apply_material_vertex_offset as original_apply_offset
    from blender_scene_effect_vertex_offset import validate_vertex_offset_source
    from blender_scene_environment import setup_environment
    from endfield_scene_material_handoff import load_plan_material_catalog

    plan = json.loads(plan_path.read_text(encoding="utf-8"))
    materials, textures = load_plan_material_catalog(plan)
    source_material = materials[TARGET_MATERIAL]
    source_record_json = json.dumps(source_material, sort_keys=True, allow_nan=False)
    source_floats = source_material["Floats"]
    source_keywords = source_material["RenderState"]["ValidKeywords"]
    if (source_material["Shader"].get("Name") != "HGRP/Effect/VFXBaseV2" or
            source_floats.get("_UseVertexOffset") != 1 or
            source_floats.get("_UseVertexOffsetMask") != 1 or
            source_floats.get("_OffsetSwitchDir") != 2 or
            set(source_keywords) != {"_USE_FOG", "_USE_VERTOFFSET", "_USE_VERTOFFSETMASK"}):
        raise ValueError(f"Source material no longer matches the verified masked-offset profile: material={TARGET_MATERIAL}")
    offset_slot = next(slot for slot in source_material["Textures"] if slot["Property"] == "_OffsetTex")
    mask_slot = next(slot for slot in source_material["Textures"] if slot["Property"] == "_OffsetMaskTex")
    if offset_slot["Texture"] is None or mask_slot["Texture"] is None:
        raise ValueError("Masked offset sample requires both source texture references")
    inconsistent = {**source_material, "Floats": {**source_floats,
        "_UseVertexOffsetMask": 0., "_OffsetSwitchDir": 0.}}
    try:
        validate_vertex_offset_source(inconsistent)
    except NotImplementedError as error:
        if "compiled variant" not in str(error):
            raise
    else:
        raise AssertionError("Masked compiled variant escaped through unmasked saved controls")

    before_data = json.loads(before_audit_path.read_text(encoding="utf-8"))
    before_rows = [
        row for row in before_data["effects"]
        if row.get("entity_id") == TARGET_ENTITY and row.get("system_path_id") in TARGET_SYSTEMS
    ]
    before_by_system = {str(row["system_path_id"]): row for row in before_rows}
    if set(before_by_system) != {str(system) for system in TARGET_SYSTEMS}:
        raise ValueError(f"Previous audit does not contain both target systems: rows={before_rows}")

    bpy.ops.object.select_all(action="SELECT")
    bpy.ops.object.delete(use_global=False)
    scene = bpy.context.scene
    scene.frame_set(1)
    scene.render.engine = "BLENDER_EEVEE_NEXT"
    scene.render.resolution_x = 1024
    scene.render.resolution_y = 768
    scene.render.resolution_percentage = 100
    scene.render.image_settings.file_format = "PNG"
    scene.render.image_settings.color_mode = "RGBA"
    scene.render.image_settings.color_depth = "8"
    scene.view_settings.view_transform = "AgX"
    preview_plan = {**plan, "scene_color_snapshot": {"time_seconds": SNAPSHOT_AGE}}
    camera = effect_preview_camera(preview_plan["effect_instances"], scene)

    displacement_by_system: dict[str, dict[str, object]] = {}

    def capture_offset(material: bpy.types.Material, source_mesh: bpy.types.Mesh,
                       world: Matrix, camera_world: Vector, age: float) -> bpy.types.Mesh:
        system_id = int(source_mesh.name.rsplit("_", 1)[1])
        if system_id not in TARGET_SYSTEMS or system_id in displacement_by_system:
            raise ValueError(f"Unexpected or duplicate system in offset capture: system={system_id}, mesh={source_mesh.name}")
        before_vertices = tuple(vertex.co.copy() for vertex in source_mesh.vertices)
        before_normals = tuple(vertex.normal.copy() for vertex in source_mesh.vertices)
        displaced = original_apply_offset(material, source_mesh, world, camera_world, age)
        if displaced is source_mesh or len(displaced.vertices) != len(before_vertices):
            raise ValueError(f"Source offset stage did not produce a copied mesh: system={system_id}")
        matrix = world.to_3x3()
        world_deltas = tuple(matrix @ (after.co - before)
                             for after, before in zip(displaced.vertices, before_vertices, strict=True))
        max_displacement = max((delta.length for delta in world_deltas), default=0.0)
        changed = sum(delta.length > 1e-8 for delta in world_deltas)
        perpendicular_errors: list[float] = []
        for before, local_normal, world_delta in zip(before_vertices, before_normals, world_deltas, strict=True):
            base_world = world @ before
            to_camera = camera_world - base_world
            distance = to_camera.length
            if distance <= 1e-12 or local_normal.length_squared <= 1e-16:
                raise ValueError(f"Offset validation has a degenerate camera or mesh normal: system={system_id}")
            camera_push = (to_camera / distance) * (min(max(distance - 1.0, 0.0),
                                                        source_floats["_VertCameraOffset"]) + 0.001)
            geometry_delta = world_delta - camera_push
            expected_direction = matrix @ local_normal.normalized()
            perpendicular_errors.append(geometry_delta.cross(expected_direction).length)
        max_perpendicular_error = max(perpendicular_errors, default=0.0)
        if max_displacement <= 1e-7 or changed == 0 or max_perpendicular_error > 1e-5:
            raise ValueError(
                f"Masked source offset did not produce verified mesh-normal movement: system={system_id}, "
                f"max_displacement={max_displacement}, changed={changed}, "
                f"max_perpendicular_error={max_perpendicular_error}")
        displacement_by_system[str(system_id)] = {
            "system_path_id": system_id,
            "source_mesh": source_mesh.name,
            "source_material": [material.get("endfield_fx_source"), material.get("endfield_fx_path_id")],
            "vertices": len(before_vertices),
            "changed_vertices": changed,
            "max_displacement_m": _finite(max_displacement, "max_displacement_m"),
            "max_mesh_normal_alignment_error_m": _finite(max_perpendicular_error, "max_mesh_normal_alignment_error_m"),
            "mesh_normal_direction_verified": True,
        }
        return displaced

    import blender_scene_effects

    blender_scene_effects.apply_material_vertex_offset = capture_offset
    builder_rows = build_effect_previews(preview_plan, materials, textures)
    blender_scene_effects.apply_material_vertex_offset = original_apply_offset
    after_rows = [row for row in builder_rows if row.get("system_path_id") in TARGET_SYSTEMS]
    if {int(row["system_path_id"]) for row in after_rows} != TARGET_SYSTEMS:
        raise ValueError(f"FX builder did not return both source systems: rows={after_rows}")
    if any(row.get("status") not in {"static_preview", "static_preview_partial"} for row in after_rows):
        raise ValueError(f"FX builder did not produce both source particle meshes: rows={after_rows}")
    if set(displacement_by_system) != {str(system) for system in TARGET_SYSTEMS}:
        raise ValueError(f"Offset stage did not process both source systems: systems={displacement_by_system}")
    if json.dumps(source_material, sort_keys=True, allow_nan=False) != source_record_json:
        raise ValueError("Source material record changed during the preview build")

    geometry_objects = [obj for obj in scene.objects if obj.get("endfield_fx_system_path_id")]
    if len(geometry_objects) != len(TARGET_SYSTEMS):
        raise ValueError(f"Expected two source geometry objects: count={len(geometry_objects)}")
    corners = [obj.matrix_world @ Vector(corner) for obj in geometry_objects for corner in obj.bound_box]
    lower = Vector(tuple(min(point[axis] for point in corners) for axis in range(3)))
    upper = Vector(tuple(max(point[axis] for point in corners) for axis in range(3)))
    center = (lower + upper) * 0.5
    radius = max((point - center).length for point in corners)
    normal_sum = Vector((0.0, 0.0, 0.0))
    for obj in geometry_objects:
        for vertex in obj.data.vertices:
            normal_sum += (obj.matrix_world.to_3x3() @ vertex.normal).normalized()
    if normal_sum.length_squared <= 1e-12:
        raise ValueError("Source particle mesh normals cancel; a shared front view cannot be framed")
    view_direction = normal_sum.normalized()
    camera.location = center + view_direction * max(2.6, radius * 3.5)
    camera.rotation_euler = (center - camera.location).to_track_quat("-Z", "Y").to_euler()
    camera.data.lens = 52.0
    camera.data.clip_start = 0.01
    camera.data.clip_end = 100.0
    scene.camera = camera
    setup_environment(scene)
    bpy.context.view_layer.update()
    scene.render.filepath = str(image_path)
    bpy.ops.render.render(write_still=True)
    if not image_path.is_file() or image_path.stat().st_size <= 0:
        raise FileNotFoundError(f"Masked-offset preview render was not written: {image_path}")
    bpy.ops.file.pack_all()
    bpy.ops.wm.save_as_mainfile(filepath=str(blend_path))
    if not blend_path.is_file() or blend_path.stat().st_size <= 0:
        raise FileNotFoundError(f"Masked-offset preview Blend was not written: {blend_path}")
    bpy.ops.wm.open_mainfile(filepath=str(blend_path), load_ui=False, use_scripts=False)
    reopened_objects = [obj for obj in bpy.context.scene.objects if obj.get("endfield_fx_system_path_id")]
    reopened_systems = {int(obj["endfield_fx_system_path_id"]) for obj in reopened_objects}
    if reopened_systems != TARGET_SYSTEMS:
        raise ValueError(f"Saved preview lost source systems: systems={reopened_systems}")
    reopened_materials = [material for material in bpy.data.materials
                          if str(material.get("endfield_fx_source", "")).casefold() == TARGET_MATERIAL[0] and
                          material.get("endfield_fx_path_id") == str(TARGET_MATERIAL[1])]
    if len(reopened_materials) != 1:
        raise ValueError(f"Saved preview lost the source material: count={len(reopened_materials)}")
    saved_record = reopened_materials[0].get("endfield_fx_source_material_json")
    if type(saved_record) is not str or json.loads(saved_record) != source_material:
        raise ValueError("Saved Blend changed the original source material controls")
    for node_contract in ("SourceOffsetTexture/1", "SourceOffsetMaskTexture/1"):
        if not any(node.get("endfield_fx_vertex_offset_node_contract") == node_contract
                   for node in reopened_materials[0].node_tree.nodes):
            raise ValueError(f"Saved Blend lost offset texture input: node_contract={node_contract}")

    source_shader_evidence = {
        "shader": "HGRP/Effect/VFXBaseV2",
        "asset": {"source": "CAB-9fe1ba4f3ce8edb2f54eb3e35709a336", "path_id": -1430105248647086886},
        "metadata_path": r"F:\Endfield_Map_Exports\cache\v22_fx_20260929\current_shader_probe\metadata\e9dd0a379806dc341a764f33_-1430105248647086886_unnamed.txt",
        "variant_inventory_path": r"F:\Endfield_Map_Exports\cache\v22_fx_20260929\v22_vfx_variant_inventory.json",
        "platform": 4,
        "variant_index": 1599,
        "variant_keywords": ["HG_ENABLE_MV", "_USE_FOG", "_USE_VERTOFFSET", "_USE_VERTOFFSETMASK"],
        "vertex_stage_bytes": 10784,
        "vertex_stage_sha256": "9cbaf0ef5a81393337294eac5f5a5fe21a1ef6bf4b0be503974880923711bb9f",
        "masked_no_fog_variant_index": 1351,
        "masked_no_fog_vertex_stage_same_bytes": True,
        "source_formula": {
            "offset_scalar": "(OffsetIntensity + OffsetDir.w * Custom1.Y) * (offset_red * (1 + BiOffset) - BiOffset)",
            "mask_weight": "1 - (0.2 * OffsetMaskPower) * (1 - mask_red)",
            "direction_source": "mesh normal because OffsetSwitchDir=2",
            "extra_camera_push": "(clamp(distance_to_camera - 1, 0, VertCameraOffset) + 0.001) toward the reference camera",
        },
        "fog": {"source_enabled": source_floats.get("_UseFog"), "reconstructed": False},
    }
    audit = {
        "format": "EndfieldMaskedVertexOffsetContinuation/1",
        "scene_id": "dung02_cdg014",
        "scope": "Two original particle systems under one anchor; no full-scene rebuild.",
        "source_material_identity": {"source": TARGET_MATERIAL[0], "path_id": TARGET_MATERIAL[1]},
        "source_material_controls": {
            "_UseVertexOffset": source_floats["_UseVertexOffset"],
            "_UseVertexOffsetMask": source_floats["_UseVertexOffsetMask"],
            "_OffsetSwitchDir": source_floats["_OffsetSwitchDir"],
            "_OffsetIntensity": source_floats["_OffsetIntensity"],
            "_OffsetMaskPower": source_floats["_OffsetMaskPower"],
            "_OffsetDir": source_material["Colors"]["_OffsetDir"],
            "_OffsetSpeed": source_material["Colors"]["_OffsetSpeed"],
            "_OffsetMaskSpeed": source_material["Colors"]["_OffsetMaskSpeed"],
            "offset_texture": offset_slot["Texture"],
            "mask_texture": mask_slot["Texture"],
            "keywords": source_keywords,
        },
        "before": before_by_system,
        "after": {str(row["system_path_id"]): row for row in after_rows},
        "displacement": displacement_by_system,
        "source_material_record_unchanged": True,
        "source_material_offset_controls_still_enabled": True,
        "saved_blend_preserves_source_material_record": True,
        "saved_blend_preserves_both_offset_images": True,
        "saved_blend_reopened": True,
        "shader_evidence": source_shader_evidence,
        "outputs": {"blend": str(blend_path), "render": str(image_path)},
        "limitations": [
            "This is a deterministic static preview at particle age 3.6 seconds, not a native Unity render.",
            "Game fog remains unreconstructed; the matching compiled masked-offset vertex stage is shared with the no-fog variant.",
            "Only the two target particle systems are included; the full scene is not updated by this sample.",
        ],
    }
    audit_path.write_text(json.dumps(audit, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps({"status": "passed", "blend": str(blend_path), "image": str(image_path),
                      "audit": str(audit_path), "systems": displacement_by_system}, ensure_ascii=False))


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Build a bounded masked VFXBaseV2 vertex-offset regression sample.")
    parser.add_argument("--plan", type=Path, required=True)
    parser.add_argument("--before-audit", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args(sys.argv[sys.argv.index("--") + 1:])
    main(args.plan, args.before_audit, args.output)
