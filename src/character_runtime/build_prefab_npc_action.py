"""Attach an authenticated CPU clip to an existing prefab NPC material export."""
from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
import sys
import time

import bpy
from mathutils import Matrix

sys.path.insert(0, str(Path(__file__).resolve().parent))
from character_animation_binding import attach_animation_with_bind_worlds, source_matrix, validate_attached_animation
from character_authoring_validation import character_invariants, material_signature_details, shape_signatures, verify_preserved_shapes


def sha(path: Path) -> str:
    with path.open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def read_object(path: Path) -> dict[str, object]:
    raw = json.loads(path.read_text(encoding="utf-8-sig"))
    if not isinstance(raw, dict):
        raise TypeError(f"Expected JSON object: path={path}")
    return raw


def request_path(request: dict[str, object], base: Path, field: str) -> Path:
    value = request[field]
    if not isinstance(value, str) or not value.strip():
        raise TypeError(f"Expected explicit file path: field={field}")
    return (base / value).resolve()


def prefab_bind_worlds(audit: dict[str, object], hierarchy: list[dict[str, object]]) -> tuple[dict[str, Matrix], str]:
    """Resolve one complete source hierarchy by relative paths and exact parent identities."""
    transforms = audit["transforms"]
    if not isinstance(transforms, list) or not transforms or not hierarchy:
        raise TypeError("Prefab transforms and animation hierarchy must be nonempty arrays")
    by_path = {row["path"]: row for row in transforms}
    by_identity = {(row["object"]["source"].casefold(), str(row["object"]["path_id"])): row for row in transforms}
    if len(by_path) != len(transforms) or len(by_identity) != len(transforms):
        raise ValueError("Prefab source contains duplicated Transform paths or identities")
    root = hierarchy[0]["path"]
    if hierarchy[0]["parent_index"] != -1 or not isinstance(root, str):
        raise ValueError("Animation hierarchy must begin with its single source root")
    relatives: list[str] = []
    for index, node in enumerate(hierarchy):
        path, parent = node["path"], node["parent_index"]
        if not isinstance(path, str) or (index and not path.startswith(root + "/")):
            raise ValueError(f"Animation path is outside its source root: index={index}")
        if index and (type(parent) is not int or parent < 0 or parent >= index):
            raise ValueError(f"Animation parent is not a prior hierarchy node: index={index}")
        relatives.append(path[len(root):])
    candidates: list[str] = []
    for prefix in by_path:
        paths = tuple(prefix + relative for relative in relatives)
        if not all(path in by_path for path in paths):
            continue
        matches = True
        for index, path in enumerate(paths[1:], 1):
            parent = by_path[path]["parent"]["resolved"]
            identity = (parent["source"].casefold(), str(parent["path_id"])) if parent else None
            actual_parent = by_identity.get(identity)
            if actual_parent is None or actual_parent["path"] != paths[hierarchy[index]["parent_index"]]:
                matches = False
                break
        if matches:
            candidates.append(prefix)
    if len(candidates) != 1:
        raise ValueError(f"Expected one exact prefab hierarchy for the selected Avatar: matches={candidates}")
    prefix = candidates[0]
    worlds: dict[str, Matrix] = {}
    for index, node in enumerate(hierarchy):
        local = source_matrix(by_path[prefix + relatives[index]], prefix + relatives[index])
        parent = node["parent_index"]
        worlds[node["path"]] = local if parent < 0 else worlds[hierarchy[parent]["path"]] @ local
    return worlds, prefix


def map_prefab_bones(rig: bpy.types.Object, hierarchy: list[dict[str, object]]) -> dict[str, str]:
    """Map only exact Avatar paths; do not infer missing attachment animation."""
    paths = tuple(node["path"] for node in hierarchy)
    mapping: dict[str, str] = {}
    for bone in rig.data.bones:
        parent = bone.parent
        matches = tuple(path for path in paths if path.rsplit("/", 1)[-1] == bone.name
                        and (parent is None or path.startswith(mapping[parent.name] + "/")))
        if len(matches) != 1:
            raise ValueError(f"Prefab bone has no unique source Avatar path: bone={bone.name}, matches={matches}")
        mapping[bone.name] = matches[0]
    return mapping


def persistent_invariants(names: tuple[str, ...]) -> dict[str, object]:
    """Ignore unsaved orphan material trees while retaining every persistent source graph."""
    original = character_invariants(names)
    details = material_signature_details()
    trees = tuple(row for row in details["trees"] if not row["owner"].startswith("material:")
                  or bpy.data.materials[row["owner"][len("material:"):]].users > 0
                  or bpy.data.materials[row["owner"][len("material:"):]].use_fake_user)
    digest = hashlib.sha256()
    for row in trees:
        digest.update(row["owner"].encode())
        digest.update(row["sha256"].encode())
    return {**original, "material_graph_static_inputs_and_drivers": digest.hexdigest()}


def main(path: Path) -> None:
    started = time.perf_counter()
    request = read_object(path)
    if request.get("format") != "EndfieldPrefabNpcActionBuild/1":
        raise ValueError("Expected EndfieldPrefabNpcActionBuild/1")
    fields = ("source_blend", "static_source_audit", "static_build_receipt", "preparation_receipt",
              "animation_receipt", "npc_animation_receipt", "output")
    paths = {field: request_path(request, path.parent, field) for field in fields}
    output = paths["output"]
    if output.exists() or output.is_symlink():
        raise FileExistsError(output)
    audit = read_object(paths["static_source_audit"])
    static = read_object(paths["static_build_receipt"])
    preparation = read_object(paths["preparation_receipt"])
    animation = read_object(paths["animation_receipt"])
    npc = read_object(paths["npc_animation_receipt"])
    if static["sha256"] != sha(paths["source_blend"]):
        raise ValueError("Static prefab blend differs from its build receipt")
    artifacts = {str(Path(row["path"]).resolve()): row["sha256"] for row in static["source_artifact_refs"]}
    if artifacts.get(str(paths["static_source_audit"])) != sha(paths["static_source_audit"]):
        raise ValueError("Static prefab audit differs from the material build source")
    if (npc["status"] != "exact_cpu_base_clip_exported" or npc["avatar_identity_matches"] is not True
            or npc["animation_receipt"]["sha256"] != sha(paths["animation_receipt"])
            or preparation["npc_id"] != npc["npc_id"] or animation["character_id"] != npc["npc_id"]
            or preparation["source_identity"] != npc["catalog_source_identity"]
            or preparation["part_name"] != npc["part_name"]):
        raise ValueError("Static prefab and CPU clip selection or source identities disagree")
    avatar = tuple(row["avatar"]["resolved"] for row in audit["animators"] if row["avatar"]["resolved"])
    if len(avatar) != 1 or avatar[0]["source"].casefold() != animation["avatar_cab"].casefold() or int(avatar[0]["path_id"]) != int(animation["avatar_path_id"]):
        raise ValueError("Static prefab Animator Avatar differs from the decoded CPU clip Avatar")
    source_path = Path(npc["source"]["path"]).resolve()
    if sha(source_path) != npc["source"]["sha256"]:
        raise ValueError("NPC CPU source evidence changed after decoding")
    source = read_object(source_path)
    for field in ("static_source_audit", "preparation_receipt"):
        evidence = source[field]
        if Path(evidence["path"]).resolve() != paths[field] or evidence["sha256"] != sha(paths[field]):
            raise ValueError(f"NPC decoded source evidence belongs to another prefab: field={field}")
    hierarchy_path, frames_path = Path(animation["source_hierarchy"]), Path(animation["source_local_frames"])
    hashes = {str(Path(row["path"]).resolve()): row["sha256"] for row in animation["outputs"]}
    for artifact in (hierarchy_path, frames_path):
        if hashes.get(str(artifact.resolve())) != sha(artifact):
            raise ValueError(f"Decoded source animation changed: path={artifact}")
    hierarchy = json.loads(hierarchy_path.read_text(encoding="utf-8-sig"))
    worlds, prefix = prefab_bind_worlds(audit, hierarchy)
    input_hashes = {str(artifact): sha(artifact) for artifact in (*tuple(value for field, value in paths.items() if field != "output"), source_path, hierarchy_path, frames_path)}
    bpy.ops.wm.open_mainfile(filepath=str(paths["source_blend"]), load_ui=False, use_scripts=False)
    rigs = tuple(obj for obj in bpy.data.objects if obj.type == "ARMATURE")
    if len(rigs) != 1 or rigs[0].animation_data and rigs[0].animation_data.action:
        raise ValueError("Prefab Action attachment requires one static source armature")
    rig = rigs[0]
    mapping = map_prefab_bones(rig, hierarchy)
    names = tuple(obj.name for obj in bpy.data.objects if obj.type == "MESH")
    invariants, shapes = persistent_invariants(names), shape_signatures(names)
    for bone_name, source_path_name in mapping.items():
        rig.data.bones[bone_name]["source_transform_path"] = source_path_name
    calibration = {"format": "EndfieldNpcBindWorldCalibration/1", "calibrated": True,
                   "source": "Exact prefab Transform hierarchy and parent identities; source-driven bind worlds.",
                   "source_prefab_root": prefix, "source_audit_sha256": sha(paths["static_source_audit"]),
                   "source_nodes": len(hierarchy), "matched_prefab_transforms": len(worlds)}
    binding = attach_animation_with_bind_worlds(rig, hierarchy_path, frames_path, animation["clip_name"],
                                              float(animation["sample_rate"]), 2e-4, worlds, calibration)
    output.mkdir(parents=True, exist_ok=False)
    bpy.data.texts.new("SOURCE NPC CPU ACTION.json").write(json.dumps(source, sort_keys=True))
    bpy.context.preferences.filepaths.save_version = 0
    target = output / "character.blend"
    bpy.ops.wm.save_as_mainfile(filepath=str(target), compress=True)
    bpy.ops.wm.open_mainfile(filepath=str(target), load_ui=False, use_scripts=False)
    validation = validate_attached_animation(bpy.data.objects[binding["armature"]], binding,
                                            tuple(range(1, int(binding["frames"]) + 1)), 2e-4)
    actual = persistent_invariants(names)
    changed = tuple(field for field in invariants if invariants[field] != actual[field])
    if changed:
        raise AssertionError(f"Prefab Action changed source geometry, materials or rig: fields={changed}")
    verify_preserved_shapes(shapes, names)
    if any(sha(Path(artifact)) != digest for artifact, digest in input_hashes.items()):
        raise AssertionError("Read-only prefab Action inputs changed during construction")
    report = {"format": "EndfieldPrefabNpcActionReceipt/1", "status": "saved_reopened_all_frames_verified",
              "npc_id": npc["npc_id"], "template_id": npc["template_id"], "part_name": npc["part_name"],
              "state_name": npc["state_name"], "binding": binding, "reopen_validation": validation,
              "source_material_geometry_shapes_preserved": True, "input_hashes": input_hashes,
              "file": str(target), "bytes": target.stat().st_size, "sha256": sha(target),
              "seconds": time.perf_counter() - started, "runtime_animator_reconstructed": False,
              "pending": ["Offline exact CPU base clip; runtime Animator state, transitions and IK are not reconstructed."]}
    (output / "receipt.json").write_text(json.dumps(report, indent=2, allow_nan=False), encoding="utf-8")
    print(json.dumps({"status": report["status"], "file": str(target), "seconds": report["seconds"]}), flush=True)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--request", type=Path, required=True)
    main(parser.parse_args(sys.argv[sys.argv.index("--") + 1:]).request.resolve())
