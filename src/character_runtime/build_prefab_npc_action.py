"""Attach an authenticated source clip to an existing prefab NPC material export."""
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
from character_animation_binding import attach_animation_with_bind_worlds, fields, rows, source_matrix, validate_attached_animation
from character_geometry import _nodes, _worlds, geometry_bind_worlds
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


def project_prefab_animation(audit: dict[str, object], hierarchy_path: Path,
                             frames_path: Path, output: Path) -> tuple[Path, Path, dict[str, object]]:
    """Bind exact shared paths and retain prefab-only nodes at their source local pose."""
    hierarchy = json.loads(hierarchy_path.read_text(encoding="utf-8"))
    frames = json.loads(frames_path.read_text(encoding="utf-8"))
    source_paths = {row["path"]: index for index, row in enumerate(hierarchy)}
    if len(source_paths) != len(hierarchy) or not hierarchy or not frames:
        raise ValueError("Animation source paths must be unique with nonempty stored frames")
    source_root = hierarchy[0]["path"]
    animators = audit["animators"]
    if len(animators) != 1:
        raise ValueError("Prefab animation projection requires one authenticated Animator")
    cab = animators[0]["object"]["source"].casefold()
    roots = tuple(row for row in audit["transforms"]
                  if row["parent"]["resolved"] is None and row["object"]["source"].casefold() == cab)
    if len(roots) != 1:
        raise ValueError(f"Prefab Animator source requires one root Transform: cab={cab}, roots={len(roots)}")
    prefix = roots[0]["path"]
    transforms = sorted((row for row in audit["transforms"]
                         if row["path"] == prefix or row["path"].startswith(prefix + "/")),
                        key=lambda row: (row["path"].count("/"), row["path"]))
    actual_paths = tuple(source_root + row["path"][len(prefix):] for row in transforms)
    indices = {path: index for index, path in enumerate(actual_paths)}
    if len(indices) != len(transforms):
        raise ValueError("Prefab source paths are duplicated")
    projected = []
    for index, (path, row) in enumerate(zip(actual_paths, transforms, strict=True)):
        parent_path = path.rsplit("/", 1)[0] if index else None
        parent = indices[parent_path] if parent_path is not None else -1
        if parent >= index:
            raise ValueError(f"Prefab source hierarchy is not parent-first: path={path}")
        rest = {field: row[field] for field in ("position", "rotation", "scale")}
        if path in source_paths:
            original = hierarchy[source_paths[path]]
            source_parent = original["parent_index"]
            expected = hierarchy[source_parent]["path"] if source_parent >= 0 else None
            if expected != parent_path:
                raise ValueError(f"Shared Avatar/prefab parent paths differ: path={path}, avatar={expected}, prefab={parent_path}")
            projected.append({**original, "index": index, "parent_index": parent})
        else:
            projected.append({"index": index, "path": path, "parent_index": parent, "rest_local": rest,
                              "position_source": "prefab_static", "rotation_source": "prefab_static",
                              "scale_source": "prefab_static"})
    static_sources = {"source_static", "prefab_static"}
    if not any(row["position_source"] not in static_sources or row["rotation_source"] not in static_sources for row in projected):
        raise ValueError("No source animation tracks reach the selected prefab hierarchy")
    projected_frames = []
    for index, frame in enumerate(frames):
        if len(frame["locals"]) != len(hierarchy):
            raise ValueError(f"Animation source frame hierarchy size differs: frame={index}")
        locals_ = [frame["locals"][source_paths[row["path"]]] if row["path"] in source_paths
                   else row["rest_local"] for row in projected]
        projected_frames.append({"time": frame["time"], "locals": locals_})
    output.mkdir(parents=True, exist_ok=False)
    target_hierarchy, target_frames = output / "source_hierarchy.json", output / "source_local_frames.json"
    target_hierarchy.write_text(json.dumps(projected, allow_nan=False), encoding="utf-8")
    target_frames.write_text(json.dumps(projected_frames, allow_nan=False), encoding="utf-8")
    proof = {"source_prefab_root": prefix, "source_avatar_nodes": len(hierarchy),
             "prefab_nodes": len(projected), "shared_paths": len(set(source_paths) & set(indices)),
             "avatar_paths_absent_from_prefab": sorted(set(source_paths) - set(indices)),
             "prefab_static_paths_absent_from_avatar": sorted(set(indices) - set(source_paths)),
             "policy": "Exact relative paths and parent identities only; absent Avatar nodes have no prefab consumer, and prefab-only nodes retain their source local pose."}
    (output / "projection.json").write_text(json.dumps(proof, indent=2), encoding="utf-8")
    return target_hierarchy, target_frames, proof


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


def map_prefab_bones(rig: bpy.types.Object, hierarchy: list[dict[str, object]], prefab_root: str) -> dict[str, str]:
    """Map only exact Avatar paths; do not infer missing attachment animation."""
    paths = tuple(node["path"] for node in hierarchy)
    mapping: dict[str, str] = {}
    for bone in rig.data.bones:
        parent = bone.parent
        if parent is None and bone.name == prefab_root.rsplit("/", 1)[-1]:
            mapping[bone.name] = hierarchy[0]["path"]
            continue
        matches = tuple(path for path in paths if path.rsplit("/", 1)[-1] == bone.name
                        and (parent is None or path.startswith(mapping[parent.name] + "/")))
        if len(matches) != 1:
            raise ValueError(f"Prefab bone has no unique source Avatar path: bone={bone.name}, matches={matches}")
        mapping[bone.name] = matches[0]
    return mapping


def prefab_geometry_bind_worlds(geometry: dict[str, object], hierarchy: list[dict[str, object]],
                               prefix: str) -> tuple[dict[str, Matrix], dict[str, object]]:
    """Use the authenticated portable geometry's exact static import bind space."""
    if geometry.get("format") != "EndfieldCharacterGeometry/1":
        raise ValueError("Prefab action requires EndfieldCharacterGeometry/1 source evidence")
    nodes = _nodes(geometry["nodes"])
    roots = tuple(node for node in nodes if node.parent is None)
    if len(roots) != 1 or roots[0].path != prefix:
        raise ValueError(f"Static geometry root differs from exact prefab hierarchy: prefix={prefix}")
    node_paths = {node.path: node for node in nodes}
    source_worlds = _worlds(nodes)
    meshes = tuple(fields(value, "geometry mesh") for value in rows(geometry["meshes"], "geometry meshes"))
    binds, _ = geometry_bind_worlds(source_worlds, meshes)
    root = hierarchy[0]["path"]
    worlds: dict[str, Matrix] = {}
    for node in hierarchy:
        path = prefix + node["path"][len(root):]
        if path not in node_paths:
            raise ValueError(f"Projected prefab path is absent from authenticated static geometry: path={path}")
        parent = node["parent_index"]
        expected_parent = prefix + hierarchy[parent]["path"][len(root):] if parent >= 0 else None
        if node_paths[path].parent != expected_parent:
            raise ValueError(f"Static geometry parent differs from projected prefab hierarchy: path={path}")
        worlds[node["path"]] = binds.get(path, source_worlds[path])
    return worlds, {"mesh_inverse_bind_constrained_bone_count": len(binds),
                    "matrix_source": "Authenticated portable geometry import nodes and shared inverse-bind constraint solver; unconstrained nodes retain their exact geometry world."}


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
    source_formats = {"exact_cpu_base_clip_exported": "EndfieldNpcAnimationSource/1",
                      "exact_enemy_controller_clip_exported": "EndfieldEnemyAnimationSource/1"}
    if (npc["status"] not in source_formats or npc["avatar_identity_matches"] is not True
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
    if source.get("format") != source_formats[npc["status"]]:
        raise ValueError("Decoded animation evidence format differs from its source kind")
    for field in ("static_source_audit", "preparation_receipt"):
        evidence = source[field]
        if Path(evidence["path"]).resolve() != paths[field] or evidence["sha256"] != sha(paths[field]):
            raise ValueError(f"NPC decoded source evidence belongs to another prefab: field={field}")
    hierarchy_path, frames_path = Path(animation["source_hierarchy"]), Path(animation["source_local_frames"])
    hashes = {str(Path(row["path"]).resolve()): row["sha256"] for row in animation["outputs"]}
    for artifact in (hierarchy_path, frames_path):
        if hashes.get(str(artifact.resolve())) != sha(artifact):
            raise ValueError(f"Decoded source animation changed: path={artifact}")
    geometry_paths = tuple(Path(artifact) for artifact in artifacts if Path(artifact).name == "model.geometry.json")
    if len(geometry_paths) != 1:
        raise ValueError(f"Static build requires one authenticated portable geometry artifact: matches={len(geometry_paths)}")
    geometry_path = geometry_paths[0]
    geometry_sha256 = sha(geometry_path)
    if artifacts[str(geometry_path)] != geometry_sha256:
        raise ValueError(f"Static portable geometry differs from its material build receipt: path={geometry_path}")
    geometry = read_object(geometry_path)
    input_hashes = {str(artifact): sha(artifact) for artifact in (*tuple(value for field, value in paths.items() if field != "output"), source_path, hierarchy_path, frames_path, geometry_path)}
    hierarchy_path, frames_path, projection = project_prefab_animation(audit, hierarchy_path, frames_path, output / "prefab_animation")
    hierarchy = json.loads(hierarchy_path.read_text(encoding="utf-8"))
    _, prefix = prefab_bind_worlds(audit, hierarchy)
    worlds, skin_calibration = prefab_geometry_bind_worlds(geometry, hierarchy, prefix)
    bpy.ops.wm.open_mainfile(filepath=str(paths["source_blend"]), load_ui=False, use_scripts=False)
    rigs = tuple(obj for obj in bpy.data.objects if obj.type == "ARMATURE")
    if len(rigs) != 1 or rigs[0].animation_data and rigs[0].animation_data.action:
        raise ValueError("Prefab Action attachment requires one static source armature")
    rig = rigs[0]
    mapping = map_prefab_bones(rig, hierarchy, prefix)
    maximum_rest_head_error = max((rig.data.bones[bone].head_local - worlds[path].translation).length
                                  for bone, path in mapping.items())
    if maximum_rest_head_error > 2e-4:
        raise ValueError(f"Static rig heads differ from authenticated geometry bind worlds: maximum_error={maximum_rest_head_error}")
    names = tuple(obj.name for obj in bpy.data.objects if obj.type == "MESH")
    invariants, shapes = persistent_invariants(names), shape_signatures(names)
    for bone_name, source_path_name in mapping.items():
        rig.data.bones[bone_name]["source_transform_path"] = source_path_name
    calibration = {"format": "EndfieldNpcBindWorldCalibration/1", "calibrated": True,
                   "source": "Exact prefab hierarchy and authenticated static geometry import bind worlds.",
                   "geometry_source": str(geometry_path), "geometry_source_sha256": geometry_sha256,
                   "maximum_static_rig_head_error": maximum_rest_head_error,
                   "source_prefab_root": prefix, "source_audit_sha256": sha(paths["static_source_audit"]),
                   "source_nodes": len(hierarchy), "matched_prefab_transforms": len(worlds),
                   "hierarchy_projection": projection, **skin_calibration}
    binding = attach_animation_with_bind_worlds(rig, hierarchy_path, frames_path, animation["clip_name"],
                                              float(animation["sample_rate"]), 2e-4, worlds, calibration)
    bpy.data.texts.new("SOURCE NPC ACTION.json").write(json.dumps(source, sort_keys=True))
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
              "pending": ["Offline exact source clip; runtime Animator state, transitions and IK are not reconstructed."]}
    (output / "receipt.json").write_text(json.dumps(report, indent=2, allow_nan=False), encoding="utf-8")
    print(json.dumps({"status": report["status"], "file": str(target), "seconds": report["seconds"]}), flush=True)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--request", type=Path, required=True)
    main(parser.parse_args(sys.argv[sys.argv.index("--") + 1:]).request.resolve())
