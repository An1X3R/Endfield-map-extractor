"""Import one verified NPC assembly, retain HGRP materials, and save one source Action."""
from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
import sys
import time
from types import MappingProxyType

import bpy


def sha(path: Path) -> str:
    with path.open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


sys.path.insert(0, str(Path(__file__).resolve().parent))
from npc_animation_bind_calibration import (
    bind_npc_source_bone_paths, read_npc_bind_worlds,
    validate_npc_attachment_paths, verify_npc_calibration_identity,
)


def main(request_path: Path) -> None:
    started = time.perf_counter()
    raw = json.loads(request_path.read_text(encoding="utf-8-sig"))
    if not isinstance(raw, dict) or raw.get("format") != "EndfieldNpcActionBuild/1":
        raise ValueError("Expected EndfieldNpcActionBuild/1")

    def path(field: str) -> Path:
        value = raw[field]
        if not isinstance(value, str) or not value:
            raise TypeError(f"Expected request path: field={field}")
        return (request_path.parent / value).resolve()

    for field in ("repository_src", "material_module_dir", "npc_material_module_dir"):
        sys.path.insert(0, str(path(field)))
    from build_npc_material_sample import read_instances, bind_instances, refresh_source_transforms
    from rebuild_character_materials import (read_json, object_value, array_value, read_material_sources,
        SourcePacket, load_images, material_group, surface_stack, make_environment)
    from blender_scene_materials import layout_shader_node_tree
    from character_npr_nodes import ArtistLighting, make_artist_lighting
    from audit_character_fbx import clear_scene_objects, enable_fbx_importer, import_character_fbx, inspect_skin_weights, test_weighted_joint_deformation
    from preview_character import reset_static_bind_pose
    from character_authoring_validation import character_invariants, verify_invariants, shape_signatures, verify_preserved_shapes
    from character_animation_binding import attach_animation_with_bind_worlds, validate_attached_animation
    assembly_path, fbx = path("assembly"), path("fbx")
    calibration_assembly_path = path("bind_world_calibration_assembly")
    output = path("output")
    if output.exists() or output.is_symlink():
        raise FileExistsError(output)
    source = assembly_path.parent
    assembly = object_value(read_json(assembly_path), "NPC assembly")
    instances = read_instances(assembly)
    shader = object_value(read_json(source / "shader_identity.json"), "NPC shader evidence")
    if shader["format"] != "EndfieldCharacterShaderIdentity/1":
        raise ValueError("Unsupported actual NPC shader evidence")
    for field, artifact in (("assembly_sha256", assembly_path), ("materials_sha256", source / "materials.json")):
        if str(shader[field]).casefold() != sha(artifact):
            raise ValueError(f"Actual shader source changed: field={field}")
    for item in array_value(shader["shaders"], "shader rows"):
        row = object_value(item, "shader row")
        if object_value(row["shader"], "shader identity")["type"] != "Shader" or row["object_bytes"] != row["type_tree_bytes_read"]:
            raise ValueError("NPC source Shader TypeTree is incomplete")
    probe = object_value(read_json(source / "receipt.json"), "assembly probe receipt")
    if probe["status"] != "source_geometry_exported" or str(probe["fbx_sha256"]).casefold() != sha(fbx):
        raise ValueError("NPC FBX bytes differ from authenticated assembly export")
    calibration_assembly = object_value(read_json(calibration_assembly_path), "NPC bind-world calibration assembly")
    calibration_probe = object_value(read_json(calibration_assembly_path.parent / "receipt.json"),
                                      "NPC bind-world probe receipt")
    verify_npc_calibration_identity(assembly, probe, calibration_assembly, calibration_probe)
    animation_path = path("animation_receipt")
    animation = object_value(read_json(animation_path), "animation receipt")
    npc_receipt = object_value(read_json(path("npc_animation_receipt")), "NPC action receipt")
    npc_source = object_value(read_json(Path(npc_receipt["source"]["path"])), "NPC source")
    if (npc_receipt["status"] != "exact_cpu_base_clip_exported"
            or not (npc_receipt["avatar_identity_matches"] is True or
                (npc_receipt["avatar_identity_matches"] is False
                 and npc_receipt.get("avatar_binding_status") == "source_humanoid_to_selected_avatar"
                 and npc_source.get("avatar_binding_status") == "source_humanoid_to_selected_avatar"))
            or npc_receipt["animation_receipt"]["sha256"] != sha(animation_path)
            or npc_receipt["source"]["sha256"] != sha(Path(npc_receipt["source"]["path"]))
            or npc_receipt["npc_id"] != assembly["selection"]["character_id"]
            or animation["character_id"] != npc_receipt["npc_id"]):
        raise ValueError("NPC Action receipt does not authenticate this exact static selection")
    if str(assembly["avatar"]["source"]).casefold() != str(animation["avatar_cab"]).casefold() or int(assembly["avatar"]["path_id"]) != animation["avatar_path_id"]:
        raise ValueError("NPC static geometry and Action use different Avatars")
    assembly_input = Path(npc_source["assembly_input"]["path"])
    if npc_source["assembly_input"]["sha256"] != sha(assembly_input) or probe["input_sha256"].casefold() != sha(assembly_input):
        raise ValueError("Action and FBX do not share the same authenticated assembly input")
    outputs = {str(Path(row["path"]).resolve()): row["sha256"] for row in animation["outputs"]}
    hierarchy, frames = Path(animation["source_hierarchy"]), Path(animation["source_local_frames"])
    for artifact in (hierarchy, frames):
        if outputs.get(str(artifact.resolve())) != sha(artifact):
            raise ValueError(f"Decoded source animation changed: path={artifact}")
    bind_worlds, bind_world_calibration = read_npc_bind_worlds(calibration_assembly, hierarchy)
    input_paths = (assembly_path, calibration_assembly_path, calibration_assembly_path.parent / "receipt.json",
                   fbx, animation_path, path("npc_animation_receipt"), source / "materials.json",
                   source / "material_sources.json", source / "source_texture_artifacts.json", source / "shader_identity.json")
    input_hashes = {str(artifact): sha(artifact) for artifact in input_paths}
    records = tuple(object_value(row, "material") for row in array_value(read_json(source / "materials.json"), "materials"))
    visible = frozenset(binding.identity for instance in instances if not instance.configured_disabled
        for binding in instance.bindings if binding.identity is not None)
    materials, textures = read_material_sources(source, records, shader, visible)
    sources = {material.identity: material for material in materials}
    packet = SourcePacket(materials, textures, (), (), MappingProxyType({}), MappingProxyType(input_hashes), visible)
    enable_fbx_importer()
    clear_scene_objects()
    imported, recoveries = import_character_fbx(fbx)
    meshes = tuple(obj for obj in imported if obj.type == "MESH")
    rigs = tuple(obj for obj in imported if obj.type == "ARMATURE")
    if len(rigs) != 1 or {obj.name for obj in meshes} != {instance.export_name for instance in instances}:
        raise ValueError("NPC action sample requires one exact rig and the authenticated Mesh inventory")
    group_counts: dict[str, dict[str, int]] = {}
    for mesh in meshes:
        weights, counts = inspect_skin_weights(mesh)
        group_counts[mesh.name] = counts
        if weights["binding_valid"] is not True or weights["finite"] is not True or weights["normalized"] is not True:
            raise ValueError(f"NPC source skin is incomplete: mesh={mesh.name}")
    deformation = test_weighted_joint_deformation(rigs, meshes, group_counts)
    if deformation["passed"] is not True:
        raise ValueError("NPC weighted-joint deformation and restored rest check failed")
    reset_static_bind_pose(bpy.context.scene, fbx, meshes)
    lighting = make_artist_lighting(ArtistLighting((.7, -1., .6), .9, .15, 0., .25, .75), layout_shader_node_tree)
    environment = make_environment(layout_shader_node_tree)
    images = load_images(packet)
    groups = {material.identity: material_group(material, packet, environment, images, lighting, layout_shader_node_tree) for material in materials}
    for material in materials:
        archive = surface_stack((material,), groups, layout_shader_node_tree)
        archive.use_fake_user = True
    objects = bind_instances(instances, 0, sources, groups, layout_shader_node_tree)
    refresh_source_transforms(objects)
    for material in bpy.data.materials:
        # Preserve imported data-slot materials even when effective object slots override them.
        material.use_fake_user = True
    names = tuple(obj.name for obj in objects)
    invariants, shapes = character_invariants(names), shape_signatures(names)
    action_name = str(animation["clip_name"])
    source_bone_paths = bind_npc_source_bone_paths(rigs[0], hierarchy)
    mounted_binding = validate_npc_attachment_paths(rigs[0], assembly, source_bone_paths, hierarchy)
    bind_world_calibration = {**bind_world_calibration, "mounted_attachment_paths": mounted_binding}
    binding = attach_animation_with_bind_worlds(rigs[0], hierarchy, frames, action_name,
        float(animation["sample_rate"]), 2e-4, bind_worlds, bind_world_calibration)
    verify_invariants(invariants, names)
    verify_preserved_shapes(shapes, names)
    bpy.data.texts.new("SOURCE NPC ASSEMBLY.json").write(json.dumps(assembly, sort_keys=True))
    bpy.data.texts.new("SOURCE NPC CPU ACTION.json").write(json.dumps(npc_source, sort_keys=True))
    output.mkdir(parents=True, exist_ok=False)
    bpy.context.preferences.filepaths.save_version = 0
    target = output / "npc_action.blend"
    image_names = tuple(image.name for image in images.values())
    bpy.ops.wm.save_as_mainfile(filepath=str(target))
    bpy.ops.wm.open_mainfile(filepath=str(target), use_scripts=False, load_ui=False)
    validation = validate_attached_animation(bpy.data.objects[binding["armature"]], binding,
                                             tuple(range(1, int(binding["frames"]) + 1)), 2e-4)
    verify_invariants(invariants, names)
    verify_preserved_shapes(shapes, names)
    if any(bpy.data.images[name].packed_file is None for name in image_names):
        raise AssertionError("NPC source images are not packed after reopen")
    if any(sha(Path(artifact)) != digest for artifact, digest in input_hashes.items()):
        raise AssertionError("Read-only NPC source changed during Action attachment")
    report = {"format": "EndfieldNpcActionBuildReceipt/1", "status": "saved_reopened_all_frames_verified",
        "npc_id": npc_receipt["npc_id"], "template_id": npc_receipt["template_id"], "part_name": npc_receipt["part_name"],
        "state_name": npc_receipt["state_name"], "cpu_animation_template": npc_receipt["cpu_animation_template"],
        "source_meshes": names, "actual_shader_families": sorted({material.shader for material in materials}),
        "unresolved_archives": sum(any(binding.identity is None for binding in instance.bindings) for instance in instances),
        "packed_image_roles": len(images), "geometry_uv_weights_rest_and_original_shapes_preserved": True,
        "source_skin_deformation": deformation, "nested_skin_recoveries": recoveries,
        "source_bone_paths": source_bone_paths, "binding": binding, "reopen_validation": validation, "source_input_hashes": input_hashes,
        "bind_world_calibration": bind_world_calibration,
        "file": str(target), "bytes": target.stat().st_size, "sha256": sha(target), "save_count": 1,
        "seconds": time.perf_counter() - started, "runtime_animator_reconstructed": False,
        "pending": [
            "Only the selected source CPU base clip was built against the authenticated target Avatar; other clips, complete Unity retargeting, moving root semantics and Animator/controller states require separate verification.",
            "Unity runtime IK weights, joint limits, twist distribution, controller application and game-runtime pose parity remain unverified.",
        ]}
    (output / "receipt.json").write_text(json.dumps(report, indent=2, allow_nan=False), encoding="utf-8")
    print(json.dumps({"status": report["status"], "file": str(target), "seconds": report["seconds"]}), flush=True)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--request", required=True, type=Path)
    main(parser.parse_args(sys.argv[sys.argv.index("--") + 1:]).request.resolve())
