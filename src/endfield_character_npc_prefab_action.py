"""Attach one authenticated prefab CPU reference to its audited static HGRP artifact."""
from __future__ import annotations

from collections.abc import Callable
import hashlib
import json
import math
import os
from pathlib import Path

from endfield_character_cache import source_stamp
from endfield_character_npc_action_adapter import parse_npc_cpu_action_references
from endfield_character_selection import CharacterSelection
from endfield_character_sources import CharacterSourceContext
from endfield_scene_export import run_scene_process
from endfield_scene_resources import write_index
from webui_character_contract import json_object, required_text


def _read(path: Path) -> dict[str, object]:
    return json_object(json.loads(path.read_text(encoding="utf-8-sig")), "prefab Action receipt")


def _sha(path: Path) -> str:
    with path.open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def _runtime_sources(tools: dict[str, Path]) -> tuple[Path, ...]:
    """Fingerprint the configured entry points and their existing shared private consumers."""
    dependencies = tuple(tools["prefab_action_builder"].parent / name for name in (
        "character_animation_binding.py", "character_authoring_validation.py"))
    return tuple(dict.fromkeys((*tools.values(), *(path for path in dependencies if path.is_file()))))


def _evidence_path(value: object, root: Path, field: str) -> Path:
    evidence = json_object(value, field)
    path = Path(required_text(evidence.get("path"), field + ".path")).resolve(strict=True)
    if not path.is_relative_to(root.resolve(strict=True)) or _sha(path) != evidence.get("sha256"):
        raise ValueError(field + ": decoded evidence escaped its output or changed after verification")
    return path


def verify_prefab_action_delivery(receipt: dict[str, object], build_output: Path, output_dir: Path,
                                  selection: CharacterSelection, state_name: str, maximum_stored_frames: int,
                                  inputs: dict[str, Path]) -> list[dict[str, object]]:
    """Require exact selection, calibration, preserved static inputs and every saved frame."""
    expected = {"format": "EndfieldPrefabNpcActionReceipt/1", "status": "saved_reopened_all_frames_verified",
        "npc_id": selection.character_id, "template_id": selection.template_id, "part_name": selection.part_name,
        "state_name": state_name, "source_material_geometry_shapes_preserved": True,
        "runtime_animator_reconstructed": False}
    if any(receipt.get(field) != value for field, value in expected.items()):
        raise ValueError("Prefab Action builder did not authenticate selection and preserved static source")
    binding = json_object(receipt.get("binding"), "prefab Action binding")
    calibration = json_object(binding.get("bind_world_calibration"), "prefab bind-world calibration")
    reopen = json_object(receipt.get("reopen_validation"), "prefab Action reopen validation")
    frames = binding.get("frames")
    error = reopen.get("maximum_matrix_error")
    if (calibration.get("calibrated") is not True or type(frames) is not int
            or not 2 <= frames <= maximum_stored_frames
            or reopen.get("status") != "saved_active_action_sample_matrix_verified"
            or reopen.get("checked_frames") != list(range(1, frames + 1))
            or type(error) not in (float, int) or not math.isfinite(error) or not 0 <= error <= 2e-4):
        raise ValueError("Prefab Action requires calibrated binding and finite all-frame saved-file matrix validation")
    hashes = json_object(receipt.get("input_hashes"), "prefab Action input hashes")
    for field, path in inputs.items():
        if hashes.get(str(path.resolve(strict=True))) != _sha(path):
            raise ValueError("Prefab Action builder source evidence changed or is absent: field=" + field)
    blend = Path(required_text(receipt.get("file"), "prefab Action file")).resolve(strict=True)
    if (not blend.is_relative_to(build_output.resolve(strict=True)) or not blend.is_relative_to(output_dir.resolve(strict=True))
            or blend.suffix.casefold() != ".blend" or type(receipt.get("bytes")) is not int
            or receipt["bytes"] != blend.stat().st_size or receipt.get("sha256") != _sha(blend)):
        raise ValueError("Prefab Action artifact escaped its output or differs from its saved-file receipt")
    return [{"relative_path": blend.relative_to(output_dir.resolve(strict=True)).as_posix(), "role": "character",
             "bytes": blend.stat().st_size, "sha256": receipt["sha256"]}]


def run_prefab_npc_action(context: CharacterSourceContext, selection: CharacterSelection,
                          reference_document: dict[str, object], payload: Path, output_dir: Path,
                          defaults: dict[str, object], tools: dict[str, Path], options: dict[str, object],
                          check_cancelled: Callable[[], None], progress: Callable[[str, float, str], None]
                          ) -> tuple[list[dict[str, object]], list[dict[str, str]]]:
    """Reuse static export, exact reference decoding and source-calibrated Action attachment."""
    catalog = parse_npc_cpu_action_references(reference_document, selection)
    reference_id = required_text(options.get("reference_id"), "NPC reference_id")
    matches = tuple(row for row in catalog.actions if row.reference_id == reference_id)
    if len(matches) != 1:
        raise ValueError("Selected NPC CPU reference is absent or duplicated")
    reference = matches[0]
    frames = options.get("maximum_stored_frames")
    if type(frames) is not int or not 2 <= frames <= 18000:
        raise ValueError("NPC reference maximum_stored_frames must be an integer from 2 to 18000")
    check_cancelled()
    catalog_path = output_dir / "npc_cpu_reference.json"
    write_index(catalog_path, reference_document)
    static_inputs = {"source_blend": payload / "build/character.blend",
        "static_source_audit": payload / "prepared/static_probe/source_audit.json",
        "static_build_receipt": payload / "build/build_receipt.json",
        "preparation_receipt": payload / "prepared/preparation_receipt.json"}
    for path in static_inputs.values():
        path.resolve(strict=True)
    index_paths = (context.provider_index, context.resource_database, context.cab_inventory)
    stamps = tuple(source_stamp(path) for path in index_paths)
    tool_paths = _runtime_sources(tools) + (Path(str(defaults["python"])), context.blender)
    tool_hashes = tuple(_sha(path) for path in tool_paths)
    write_index(output_dir / "npc_reference_tools.json", {str(path): digest for path, digest in zip(tool_paths, tool_hashes)})
    static_hashes = {name: _sha(path) for name, path in static_inputs.items()}
    environment = {**os.environ, "TEMP": str(output_dir / "tmp"), "TMP": str(output_dir / "tmp"),
                   "PYTHONDONTWRITEBYTECODE": "1"}
    animation_output = output_dir / "npc_reference_animation"
    request_path = output_dir / "npc_reference_request.json"
    write_index(request_path, {"format": "EndfieldNpcAnimationRequest/2", "source_kind": "prefab",
        "selection": {"npc_id": selection.character_id, "template_id": selection.template_id,
            "part_name": selection.part_name, "source_identity": selection.catalog_identity},
        "reference": {"catalog_receipt": str(catalog_path), "reference_id": reference_id},
        "repository_src": defaults["repository_src"], "game_root": str(context.game_root),
        "provider_index": str(context.provider_index), "resource_database": str(context.resource_database),
        "cab_inventory": str(context.cab_inventory), "cache_root": str(context.cache_root),
        "static_source_audit": str(static_inputs["static_source_audit"]),
        "preparation_receipt": str(static_inputs["preparation_receipt"]),
        "json_reader": str(tools["json_reader"]), "animation_exporter": str(tools["animation_exporter"]),
        "animation_reader": str(tools["reader"]), "python": defaults["python"], "dotnet": str(tools["dotnet"]),
        "unity_version": defaults["unity_version"], "maximum_stored_frames": frames, "output": str(animation_output)})
    progress("extracting", .78, "Decoding the exact selected NPC CPU reference and Avatar")
    run_scene_process([str(defaults["python"]), str(tools["exporter"]), "--request", str(request_path)],
                      output_dir / "npc_reference_decode.log", environment, check_cancelled)
    npc_receipt_path = animation_output / "npc_animation_receipt.json"
    npc = _read(npc_receipt_path)
    expected = {"format": "EndfieldNpcAnimationReceipt/1", "status": "exact_cpu_base_clip_exported",
        "npc_id": selection.character_id, "template_id": selection.template_id, "part_name": selection.part_name,
        "catalog_source_identity": selection.catalog_identity, "state_name": reference.state_name,
        "reference_id": reference_id, "avatar_identity_matches": True, "runtime_animator_reconstructed": False}
    if any(npc.get(field) != value for field, value in expected.items()):
        raise ValueError("Decoded NPC reference differs from its authenticated CPU selection")
    animation_receipt = _evidence_path(npc.get("animation_receipt"), animation_output, "animation receipt")
    source_path = _evidence_path(npc.get("source"), animation_output, "NPC decoded source")
    source = _read(source_path)
    clip_path = _evidence_path(source.get("clip_source"), animation_output, "NPC decoded clip")
    clip = json_object(_read(clip_path).get("$animestudio"), "NPC clip metadata")
    if (str(clip.get("sourceFile")).casefold() != reference.clip_cab.casefold()
            or str(clip.get("pathId")) != reference.clip_path_id):
        raise ValueError("Decoded NPC clip CAB/PathID differs from the selected CPU reference")
    build_output = output_dir / "npc_reference_build"
    inputs = {**static_inputs, "animation_receipt": animation_receipt, "npc_animation_receipt": npc_receipt_path}
    build_path = output_dir / "npc_reference_build_request.json"
    write_index(build_path, {"format": "EndfieldPrefabNpcActionBuild/1",
        **{field: str(path) for field, path in inputs.items()}, "output": str(build_output)})
    progress("building", .86, "Attaching the NPC Action using exact prefab bind worlds")
    run_scene_process([str(context.blender), "--background", "--factory-startup", "--python-exit-code", "1",
        "--python", str(tools["prefab_action_builder"]), "--", "--request", str(build_path)],
        output_dir / "npc_reference_build.log", environment, check_cancelled)
    check_cancelled()
    progress("auditing", .97, "Verifying preserved HGRP materials and every saved Action frame")
    files = verify_prefab_action_delivery(_read(build_output / "receipt.json"), build_output, output_dir,
                                         selection, reference.state_name, frames, inputs)
    if (tuple(source_stamp(path) for path in index_paths) != stamps
            or tuple(_sha(path) for path in tool_paths) != tool_hashes
            or {name: _sha(path) for name, path in static_inputs.items()} != static_hashes):
        raise ValueError("NPC reference indexes, configured tools or static source changed during export")
    return files, [{"code": "npc_prefab_cpu_reference_scope", "message":
        "所选精确 CPU 引用已解码、按源预制体绑骨并逐帧保存重开；其他引用未逐项验收，运行时 Animator、过渡和原生 IK 尚未还原。"}]
