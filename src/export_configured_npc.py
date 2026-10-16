"""Export one exact NPC configuration through configurable, verified source tools."""
from __future__ import annotations
from character_runtime.character_geometry_source import geometry_source

import argparse
from collections.abc import Callable
from dataclasses import dataclass
import hashlib
import json
import os
from pathlib import Path
import time

from endfield_character_cache import CharacterDataCache, CharacterSourceStamp, source_stamp
from endfield_character_npc_assembly import NpcAssemblyPreparationError, prepare_npc_assembly, prepare_npc_assembly_group
from endfield_character_npc_catalog import read_independent_npc_catalog
from endfield_character_enemy_index import prepare_enemy_model_index
from endfield_character_npc_action_adapter import NpcActionSource
from endfield_character_sources import CharacterSourceContext, game_source_stamps, validate_character_storage, verify_game_source
from endfield_scene_export import run_scene_process


@dataclass(frozen=True)
class NpcExportRequest:
    context: CharacterSourceContext
    output: Path
    npc_id: str
    template_id: str
    part_name: str
    part_names: tuple[str, ...] | None
    source_identity: str
    unity_version: str
    tools: dict[str, Path]
    action: NpcActionSource | None
    maximum_stored_frames: int | None
    animation_tools: dict[str, Path]
    python: Path | None


def _read(path: Path) -> dict[str, object]:
    value = json.loads(path.read_text(encoding="utf-8-sig"))
    if not isinstance(value, dict) or any(not isinstance(key, str) for key in value):
        raise NpcAssemblyPreparationError(f"Expected JSON object: path={path}")
    return value


def _text(row: dict[str, object], field: str) -> str:
    value = row.get(field)
    if not isinstance(value, str) or not value.strip():
        raise NpcAssemblyPreparationError(f"NPC request requires nonempty text: field={field}")
    return value


def _sha(path: Path) -> str:
    with path.open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def _validate_bind_world_contract(source: Path, verification: dict[str, object],
                                  binding: dict[str, object]) -> dict[str, object]:
    calibration = binding.get("bind_world_calibration")
    top_calibration = verification.get("bind_world_calibration")
    if not isinstance(calibration, dict) or calibration.get("format") != "EndfieldNpcBindWorldCalibration/1":
        raise NpcAssemblyPreparationError("NPC Action lacks the source bind-world calibration contract")
    if calibration.get("calibrated") is not True or calibration != top_calibration:
        raise NpcAssemblyPreparationError("NPC Action bind-world calibration receipt is absent or inconsistent")
    assembly = _read(source / "assembly.json")
    avatar = assembly.get("avatar")
    skeletons = assembly.get("skeleton_validation")
    if not isinstance(avatar, dict) or not isinstance(skeletons, list):
        raise NpcAssemblyPreparationError("NPC assembly lacks Avatar identity or calibrated skeleton rows")
    identity = (avatar.get("source"), avatar.get("path_id"), avatar.get("name"), avatar.get("raw_data_sha256"))
    if any(not isinstance(value, str) or not value for value in identity):
        raise NpcAssemblyPreparationError("NPC assembly Avatar identity fields must be nonempty strings")
    rows = tuple(row for row in skeletons if isinstance(row, dict)
        and (row.get("avatar_source"), row.get("avatar_path_id"), row.get("avatar_name"),
             row.get("avatar_raw_data_sha256")) == identity)
    if len(rows) != 1:
        raise NpcAssemblyPreparationError(f"NPC assembly has no unique calibrated host skeleton: matches={len(rows)}")
    skeleton = rows[0]
    if skeleton.get("bind_world_calibration_format") != calibration["format"]:
        raise NpcAssemblyPreparationError("NPC assembly and Action builder use different bind-world schemas")
    bind_rows = skeleton.get("source_bind_worlds")
    if not isinstance(bind_rows, list) or not bind_rows:
        raise NpcAssemblyPreparationError("NPC assembly has no source inverse-bind matrices")
    digest = hashlib.sha256(json.dumps(bind_rows, sort_keys=True, separators=(",", ":"),
        allow_nan=False).encode("utf-8")).hexdigest()
    provenance = tuple(row.get("provenance") for row in bind_rows if isinstance(row, dict))
    mesh_count = provenance.count("mesh_inverse_bind_constraint_graph")
    local_count = provenance.count("avatar_default_pose_local_chain_unconstrained")
    if (len(provenance) != len(bind_rows) or digest != calibration.get("mapping_sha256")
            or calibration.get("source_bind_world_count") != len(bind_rows)
            or calibration.get("mesh_inverse_bind_constrained_bone_count") != mesh_count
            or calibration.get("avatar_local_chain_unconstrained_bone_count") != local_count
            or mesh_count == 0 or mesh_count + local_count != len(bind_rows)):
        raise NpcAssemblyPreparationError("NPC Action bind-world count, provenance, or matrix hash differs from its assembly")
    source_avatar = calibration.get("source_avatar")
    if source_avatar != {"source": identity[0], "path_id": identity[1], "name": identity[2],
                         "raw_data_sha256": identity[3]}:
        raise NpcAssemblyPreparationError("NPC Action bind-world Avatar identity differs from the selected source Avatar")
    if calibration.get("matrix_convention") != skeleton.get("matrix_convention"):
        raise NpcAssemblyPreparationError("NPC Action bind-world matrix convention differs from source assembly evidence")
    if calibration.get("source_to_blender_transform") != "H @ sourceWorld @ H; H=diag(-1,1,1,1), preserving source axis conversion.":
        raise NpcAssemblyPreparationError("NPC Action bind-world axis conversion is missing or unsupported")
    mounts = calibration.get("mounted_attachment_paths")
    if not isinstance(mounts, dict) or mounts.get("mounted_bones_use_host_bind_world") is not True:
        raise NpcAssemblyPreparationError("NPC mounted Avatar bones were not calibrated against their host bind world")
    return calibration


def read_npc_export_request(path: Path) -> NpcExportRequest:
    """Accept only server-written identities and configured tool locations."""
    raw = _read(path)
    if raw.get("format") != "EndfieldNpcExportRequest/1":
        raise NpcAssemblyPreparationError("Unsupported NPC export request format")

    def selected_path(field: str) -> Path:
        return (path.parent / _text(raw, field)).resolve()

    context = CharacterSourceContext(selected_path("game_root"), selected_path("blender"),
        selected_path("cache_root"), selected_path("output").parent,
        selected_path("provider_index"), selected_path("resource_database"), selected_path("cab_inventory"))
    tools_value = raw.get("tools")
    if not isinstance(tools_value, dict):
        raise NpcAssemblyPreparationError("NPC request requires configured tools")
    tools = {field: (path.parent / _text(tools_value, field)).resolve(strict=True)
             for field in ("json_reader", "assembly_probe", "shader_probe", "fbx_auditor", "material_builder", "material_module_dir", "repository_src")}
    for field in ("json_reader", "assembly_probe", "shader_probe", "fbx_auditor", "material_builder"):
        if not tools[field].is_file():
            raise NpcAssemblyPreparationError(f"NPC tool must be an existing file: field={field}")
    for field in ("material_module_dir", "repository_src"):
        if not tools[field].is_dir():
            raise NpcAssemblyPreparationError(f"NPC tool directory is missing: field={field}")
    action: NpcActionSource | None = None
    maximum: int | None = None
    animation_tools: dict[str, Path] = {}
    python: Path | None = None
    if raw.get("animation") is not None:
        animation = raw["animation"]
        if not isinstance(animation, dict) or not isinstance(animation.get("action"), dict):
            raise NpcAssemblyPreparationError("NPC animation requires an exact server-selected source Action")
        action = NpcActionSource(*(_text(animation["action"], field) for field in NpcActionSource.__dataclass_fields__))
        maximum = animation.get("maximum_stored_frames")
        if type(maximum) is not int or not 2 <= maximum <= 18000:
            raise NpcAssemblyPreparationError("NPC animation requires an explicit stored-frame budget from 2 to 18000")
        configured = raw.get("animation_tools")
        if not isinstance(configured, dict):
            raise NpcAssemblyPreparationError("NPC animation requires configurable reader and Action builder tools")
        animation_tools = {field: (path.parent / _text(configured, field)).resolve(strict=True)
            for field in ("exporter", "reader", "action_builder", "dotnet", "animation_exporter")}
        if any(not tool.is_file() for tool in animation_tools.values()):
            raise NpcAssemblyPreparationError("NPC animation tool must be an existing file")
        python = selected_path("python")
        if not python.is_file():
            raise NpcAssemblyPreparationError("NPC animation Python executable is missing")
    part_name = _text(raw, "part_name")
    part_names_value = raw.get("part_names")
    part_names: tuple[str, ...] | None = None
    if part_names_value is not None:
        if not isinstance(part_names_value, list) or not part_names_value or any(
                not isinstance(value, str) or not value.strip() or value != value.strip() for value in part_names_value):
            raise NpcAssemblyPreparationError("NPC grouped request part_names must be a nonempty array of exact names")
        part_names = tuple(part_names_value)
        if len(set(part_names)) != len(part_names) or part_name not in part_names:
            raise NpcAssemblyPreparationError("NPC grouped request part_names must be unique and include part_name")
    return NpcExportRequest(context, selected_path("output"), _text(raw, "npc_id"),
        _text(raw, "template_id"), part_name, part_names, _text(raw, "source_identity"),
        _text(raw, "unity_version"), tools, action, maximum, animation_tools, python)


def export_npc(request: NpcExportRequest) -> dict[str, object]:
    """Require source assembly, a real deformation audit, and a final saved-file reopen."""
    started = time.perf_counter()
    validate_character_storage(request.context.game_root, request.output)
    validate_character_storage(request.context.game_root, request.context.cache_root)
    if request.output.exists() or request.output.is_symlink():
        raise FileExistsError(f"Refusing to reuse NPC export output: {request.output}")
    before = game_source_stamps(request.context.game_root)
    indexes = (request.context.provider_index, request.context.resource_database, request.context.cab_inventory)
    index_stamps = tuple(source_stamp(path) for path in indexes)
    enemy_index = prepare_enemy_model_index(request.context, request.tools["json_reader"], request.unity_version,
                                           lambda: None, lambda _stage, _fraction, _message: None)
    catalog = read_independent_npc_catalog(request.context.game_root, request.context.provider_index,
        CharacterDataCache(request.context.cache_root), enemy_index, lambda: None)
    if catalog.source_identity != request.source_identity:
        raise NpcAssemblyPreparationError("NPC source catalog changed after selection; refresh the catalog")
    selections = tuple(row for row in catalog.entries if row.npc_id == request.npc_id
                       and row.resource_reference.template_id == request.template_id)
    if len(selections) != 1:
        raise NpcAssemblyPreparationError("NPC ID/template selection must match one current source record")
    tool_paths = {**request.tools, **{"animation_" + name: path for name, path in request.animation_tools.items()}}
    if request.action is not None:
        binding_helper = request.animation_tools["action_builder"].parent / "character_animation_binding.py"
        if not binding_helper.is_file():
            raise NpcAssemblyPreparationError(f"NPC Action binding helper is missing: {binding_helper}")
        tool_paths["animation_binding_helper"] = binding_helper
    tools_before = {field: _sha(path) for field, path in tool_paths.items() if path.is_file()}
    request.output.mkdir(parents=True)
    temporary = request.output / "tmp"
    temporary.mkdir()
    environment = {**os.environ, "TEMP": str(temporary), "TMP": str(temporary), "PYTHONDONTWRITEBYTECODE": "1"}
    timings: list[dict[str, object]] = []

    def execute(stage: str, command: list[str]) -> None:
        begin = time.perf_counter()
        print(json.dumps({"stage": stage, "state": "started"}), flush=True)
        run_scene_process(command, request.output / (stage + ".log"), environment, lambda: None)
        timings.append({"stage": stage, "seconds": time.perf_counter() - begin})
        print(json.dumps({"stage": stage, "state": "completed"}), flush=True)

    if request.part_names is None:
        assembly_input = prepare_npc_assembly(request.context, selections[0], request.part_name,
            request.tools["json_reader"], request.unity_version, request.output / "prepared",
            lambda: None, lambda stage, fraction, message: print(json.dumps(
                {"stage": stage, "fraction": fraction, "message": message}), flush=True))
    else:
        assembly_input = prepare_npc_assembly_group(request.context, selections[0], request.part_names,
            request.tools["json_reader"], request.unity_version, request.output / "prepared",
            lambda: None, lambda stage, fraction, message: print(json.dumps(
                {"stage": stage, "fraction": fraction, "message": message}), flush=True))
    source = request.output / "static_probe"
    execute("assembly", [str(request.tools["assembly_probe"]), "--input", str(assembly_input), "--output", str(source)])
    receipt = _read(source / "receipt.json")
    if receipt.get("format") != "EndfieldNpcAssemblyReceipt/1" or receipt.get("status") != "source_geometry_exported":
        raise NpcAssemblyPreparationError("NPC assembly probe did not verify source geometry")
    execute("shader", [str(request.tools["shader_probe"]), "--input", str(assembly_input),
        "--assembly", str(source / "assembly.json"), "--materials", str(source / "materials.json"),
        "--output", str(source / "shader_identity.json")])
    if request.action is not None:
        return export_npc_action(request, assembly_input, source, execute, catalog.source_identity,
                                 before, indexes, index_stamps, tool_paths, tools_before, timings, started)
    imported = request.output / "imported"
    blender = [str(request.context.blender), "--background", "--factory-startup", "--python-exit-code", "1"]
    execute("fbx_audit", [*blender, "--python", str(request.tools["fbx_auditor"]), "--",
        "--fbx", str(geometry_source(source)), "--output-dir", str(imported)])
    audit = _read(imported / "model_character_audit.json")
    if audit.get("status") != "passed" or audit.get("saved_blend_reopened") is not True:
        raise NpcAssemblyPreparationError("NPC FBX deformation and saved-file audit did not pass")
    final = request.output / "build"
    execute("materials", [*blender, "--python", str(request.tools["material_builder"]), "--",
        "--assembly", str(source / "assembly.json"), "--blend", str(imported / "model_character_audited.blend"),
        "--output", str(final), "--module-dir", str(request.tools["material_module_dir"]),
        "--repository-src", str(request.tools["repository_src"]), "--width", "256", "--height", "384",
        "--samples", "16", "--lod-index", "0"])
    verification = _read(final / "verification.json")
    blend = final / "character_materials.blend"
    if (verification.get("format") != "EndfieldNpcMaterialSample/1" or verification.get("status") != "rendered_reopened"
            or verification.get("source_geometry_preserved") is not True
            or verification.get("source_bind_pose_editable") is not True
            or verification.get("sha256") != _sha(blend)):
        raise NpcAssemblyPreparationError("Final NPC source geometry/material/bind-pose reopen audit did not pass")
    verify_game_source(request.context.game_root, before, lambda: None)
    if tuple(source_stamp(path) for path in indexes) != index_stamps:
        raise NpcAssemblyPreparationError("NPC resource indexes changed during export; refresh the catalog and retry")
    if tools_before != {field: _sha(tool_paths[field]) for field in tools_before}:
        raise NpcAssemblyPreparationError("NPC source tools changed during export")
    result = {"format": "EndfieldNpcExportReceipt/1", "status": "source_assembled_reopened",
        "source_identity": catalog.source_identity, "npc_id": request.npc_id,
        "template_id": request.template_id, "part_name": request.part_name,
        "part_names": list(request.part_names) if request.part_names is not None else [request.part_name],
        "files": [{"relative_path": blend.relative_to(request.output).as_posix(), "role": "character",
                   "bytes": blend.stat().st_size, "sha256": _sha(blend)}],
        "source_mesh_instances": verification["source_mesh_instances"],
        "visible_mesh_instances": verification["visible_mesh_instances"],
        "resolved_materials": verification["resolved_materials"],
        "unresolved_archives": len(verification["unresolved_archives"]),
        "actual_shader_families": verification["actual_shader_families"],
        "material_adapters": verification.get("material_adapters", []),
        "runtime_renderer_fields_known": False, "animation_exported": False,
        "tools_sha256": tools_before, "timings": timings, "seconds": time.perf_counter() - started,
        "limitations": ["Static source LOD0 configuration; runtime platform, visibility and cloth remain unverified.",
                        "HGRP material nodes are editable host approximations; full native rendering is pending.",
                        "Self-avatar slots use their exact mesh-provider Avatar and recorded parent transform; game runtime visibility and Animator binding remain unverified."]}
    (request.output / "npc_export_receipt.json").write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    return result


def validate_npc_action_avatar_binding(receipt: dict[str, object], source: dict[str, object]) -> None:
    """Admit cross-Avatar Generic actions only after rechecking their exact source proof."""
    status = receipt.get("avatar_binding_status")
    if status != source.get("avatar_binding_status"):
        raise NpcAssemblyPreparationError("NPC Action Avatar binding status differs from its source evidence")
    if receipt.get("avatar_identity_matches") is True and status == "exact_avatar":
        return
    if receipt.get("avatar_identity_matches") is not False:
        raise NpcAssemblyPreparationError("NPC Action Avatar identity flag is inconsistent with its binding status")
    if status == "source_humanoid_to_selected_avatar":
        if receipt.get("template_namespace") == source.get("template_namespace") == "Humanoid":
            return
        raise NpcAssemblyPreparationError("Cross-Avatar Humanoid Action requires its authenticated template namespace")
    if (status != "source_generic_equivalent_avatar"
            or receipt.get("template_namespace") != "Generic" or source.get("template_namespace") != "Generic"):
        raise NpcAssemblyPreparationError("NPC Action has no supported authenticated cross-Avatar binding")
    from endfield_character_npc_animation import validate_npc_generic_target
    from endfield_character_npc_assembly import NpcSourceObject

    evidence = receipt["source"]
    if (not isinstance(evidence, dict) or _sha(Path(evidence["path"])) != evidence.get("sha256")
            or _read(Path(evidence["path"])) != source or source.get("format") != "EndfieldNpcAnimationSource/1"):
        raise NpcAssemblyPreparationError("Generic Action source receipt differs from its authenticated JSON")
    objects: list[NpcSourceObject] = []
    for field in ("cpu_avatar_source", "avatar_source", "clip_source"):
        item = source.get(field)
        if not isinstance(item, dict) or _sha(Path(item["path"])) != item.get("sha256"):
            raise NpcAssemblyPreparationError(f"Generic Action source object differs from its authenticated JSON: field={field}")
        path = Path(item["path"])
        value = _read(path)
        metadata = value.get("$animestudio")
        if not isinstance(metadata, dict):
            raise NpcAssemblyPreparationError(f"Generic Action source object lacks exact identity: field={field}")
        objects.append(NpcSourceObject(path, value, metadata))
    cpu_avatar, target_avatar, clip = objects
    proof = validate_npc_generic_target(cpu_avatar, target_avatar, (clip,)).to_document()
    if receipt.get("generic_avatar_equivalence") != proof or source.get("generic_avatar_equivalence") != proof:
        raise NpcAssemblyPreparationError("Generic Action equivalence proof differs from the actual selected clip and source Avatars")
    for field, proof_field in (("cpu_avatar_source_sha256", "source_avatar_sha256"),
                               ("target_avatar_source_sha256", "target_avatar_sha256")):
        if receipt.get(field) != proof[proof_field] or source.get(field) != proof[proof_field]:
            raise NpcAssemblyPreparationError(f"Generic Action Avatar hash differs from its source proof: field={field}")
    identities: list[tuple[str, int]] = []
    for field, avatar in (("cpu_avatar", cpu_avatar), ("selected_avatar", target_avatar)):
        pointer = receipt.get(field)
        if (not isinstance(pointer, dict) or str(pointer.get("cab")).casefold() != str(avatar.metadata.get("sourceFile")).casefold()
                or int(str(pointer.get("path_id"))) != avatar.metadata.get("pathId")):
            raise NpcAssemblyPreparationError(f"Generic Action Avatar pointer differs from its source object: field={field}")
        identities.append((str(pointer["cab"]).casefold(), int(str(pointer["path_id"]))))
    if identities[0] == identities[1]:
        raise NpcAssemblyPreparationError("Generic Action claims different source Avatars for identical pointers")


def export_npc_action(request: NpcExportRequest, assembly_input: Path, source: Path,
                      execute: Callable[[str, list[str]], None], source_identity: str,
                      game_stamps: list[dict[str, object]], indexes: tuple[Path, ...], index_stamps: tuple[CharacterSourceStamp, ...],
                      tool_paths: dict[str, Path], tools_before: dict[str, str],
                      timings: list[dict[str, object]], started: float) -> dict[str, object]:
    """Deliver an exact CPU base Action only after real skin, material and reopen checks."""
    action = request.action
    if action is None or request.python is None or request.maximum_stored_frames is None:
        raise NpcAssemblyPreparationError("NPC Action execution requires complete validated inputs")
    animation_output = request.output / "action"
    animation_request = {"format": "EndfieldNpcAnimationRequest/1", "repository_src": str(request.tools["repository_src"]),
        "game_root": str(request.context.game_root), "provider_index": str(request.context.provider_index),
        "resource_database": str(request.context.resource_database), "cab_inventory": str(request.context.cab_inventory),
        "cache_root": str(request.context.cache_root), "assembly_input": str(assembly_input),
        "avatar_source_directory": str(request.output / "prepared/source_objects"), "npc_id": request.npc_id,
        "template_id": request.template_id, "part_name": request.part_name, "source_identity": source_identity,
        "state_name": action.state_name, "unity_version": request.unity_version,
        "json_reader": str(request.tools["json_reader"]), "animation_exporter": str(request.animation_tools["animation_exporter"]),
        "animation_reader": str(request.animation_tools["reader"]), "python": str(request.python),
        "dotnet": str(request.animation_tools["dotnet"]), "maximum_stored_frames": request.maximum_stored_frames,
        "output": str(animation_output)}
    animation_path = request.output / "animation_request.json"
    animation_path.write_text(json.dumps(animation_request, indent=2), encoding="utf-8")
    execute("animation", [str(request.python), str(request.animation_tools["exporter"]), "--request", str(animation_path)])
    npc = _read(animation_output / "receipt.json")
    evidence = _read(Path(npc["source"]["path"]))
    clip = _read(Path(evidence["clip_source"]["path"]))["$animestudio"]
    validate_npc_action_avatar_binding(npc, evidence)
    if (npc.get("status") != "exact_cpu_base_clip_exported"
            or npc.get("state_name") != action.state_name or clip.get("sourceFile", "").casefold() != action.clip_cab.casefold()
            or str(clip.get("pathId")) != action.clip_path_id or clip.get("rawDataSha256") != action.clip_raw_data_sha256
            or str(clip.get("container")).casefold() != action.resource_path.casefold()):
        raise NpcAssemblyPreparationError("Decoded NPC CPU Action differs from the selected exact clip source")
    final = request.output / "build"
    build_request = {"format": "EndfieldNpcActionBuild/1", "repository_src": str(request.tools["repository_src"]),
        "material_module_dir": str(request.tools["material_module_dir"]),
        "npc_material_module_dir": str(request.tools["material_builder"].parent),
        "assembly": str(source / "assembly.json"), "fbx": str(geometry_source(source)),
        "bind_world_calibration_assembly": str(source / "assembly.json"),
        "animation_receipt": str(animation_output / "animation/receipt.json"),
        "npc_animation_receipt": str(animation_output / "receipt.json"), "output": str(final)}
    build_path = request.output / "action_build_request.json"
    build_path.write_text(json.dumps(build_request, indent=2), encoding="utf-8")
    execute("action_build", [str(request.context.blender), "--background", "--factory-startup", "--python-exit-code", "1",
        "--python", str(request.animation_tools["action_builder"]), "--", "--request", str(build_path)])
    verification = _read(final / "receipt.json")
    blend = final / "npc_action.blend"
    binding = verification.get("binding")
    reopen = verification.get("reopen_validation")
    if not isinstance(binding, dict):
        raise NpcAssemblyPreparationError("NPC Action builder omitted its binding receipt")
    calibration = _validate_bind_world_contract(source, verification, binding)
    if (verification.get("format") != "EndfieldNpcActionBuildReceipt/1"
            or verification.get("status") != "saved_reopened_all_frames_verified"
            or verification.get("geometry_uv_weights_rest_and_original_shapes_preserved") is not True
            or verification.get("runtime_animator_reconstructed") is not False
            or verification.get("state_name") != action.state_name or verification.get("npc_id") != request.npc_id
            or verification.get("template_id") != request.template_id or verification.get("part_name") != request.part_name
            or not isinstance(reopen, dict)
            or type(binding.get("frames")) is not int or not 2 <= binding["frames"] <= request.maximum_stored_frames
            or reopen.get("status") != "saved_active_action_sample_matrix_verified"
            or reopen.get("checked_frames") != list(range(1, int(binding.get("frames", 0)) + 1))
            or verification.get("sha256") != _sha(blend) or verification.get("bytes") != blend.stat().st_size):
        raise NpcAssemblyPreparationError("NPC Action did not pass exact source and all-frame saved-file reopen validation")
    verify_game_source(request.context.game_root, game_stamps, lambda: None)
    if tuple(source_stamp(path) for path in indexes) != index_stamps or tools_before != {key: _sha(tool_paths[key]) for key in tools_before}:
        raise NpcAssemblyPreparationError("NPC inputs or tools changed during Action export")
    result = {"format": "EndfieldNpcExportReceipt/1", "status": "source_assembled_reopened", "source_identity": source_identity,
        "npc_id": request.npc_id, "template_id": request.template_id, "part_name": request.part_name,
        "part_names": list(request.part_names) if request.part_names is not None else [request.part_name],
        "files": [{"relative_path": blend.relative_to(request.output).as_posix(), "role": "character",
                   "bytes": blend.stat().st_size, "sha256": _sha(blend)}],
        "source_mesh_instances": len(verification["source_meshes"]), "actual_shader_families": verification["actual_shader_families"],
        "unresolved_archives": verification["unresolved_archives"], "runtime_renderer_fields_known": False, "animation_exported": True,
        "material_adapters": verification.get("material_adapters", []),
        "action_id": action.action_id, "state_name": action.state_name, "frames": binding["frames"],
        "reopened_frames": len(reopen["checked_frames"]), "maximum_matrix_error": reopen["maximum_matrix_error"],
        "bind_world_calibration": calibration,
        "tools_sha256": tools_before, "timings": timings, "seconds": time.perf_counter() - started,
        "limitations": verification["pending"]}
    (request.output / "npc_export_receipt.json").write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    return result


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--request", type=Path, required=True)
    args = parser.parse_args()
    print(json.dumps(export_npc(read_npc_export_request(args.request)), ensure_ascii=False))


if __name__ == "__main__":
    main()
