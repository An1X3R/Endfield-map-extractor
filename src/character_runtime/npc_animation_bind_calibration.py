"""Read authenticated NPC source bind worlds and validate mounted host paths."""
from __future__ import annotations

import hashlib
import json
import math
from pathlib import Path
import re

import bpy
from mathutils import Matrix


def bind_npc_source_bone_paths(armature: bpy.types.Object, hierarchy_path: Path) -> dict[str, object]:
    """Map exact source Avatar bones and preserve nested NPC attachment offsets."""
    if armature.type != "ARMATURE":
        raise TypeError(f"Expected imported NPC armature: {armature.name}")
    raw_nodes = json.loads(hierarchy_path.read_text(encoding="utf-8"))
    if not isinstance(raw_nodes, list) or not raw_nodes:
        raise TypeError(f"Expected nonempty source hierarchy: {hierarchy_path}")
    source_paths = tuple(row.get("path") for row in raw_nodes
                         if isinstance(row, dict) and isinstance(row.get("path"), str))
    if len(source_paths) != len(raw_nodes) or len(set(source_paths)) != len(source_paths):
        raise ValueError(f"Source hierarchy paths are missing or duplicated: {hierarchy_path}")
    source_segments = tuple((tuple(path.split("/")), path) for path in source_paths)
    assigned: dict[str, str] = {}
    exact: list[str] = []
    inherited: list[str] = []

    def original_bone_name(name: str) -> str:
        return re.sub(r"\.\d{3}$", "", name)

    def bone_segments(bone: bpy.types.Bone) -> tuple[str, ...]:
        names: list[str] = []
        cursor: bpy.types.Bone | None = bone
        while cursor is not None:
            names.append(original_bone_name(cursor.name))
            cursor = cursor.parent
        return tuple(reversed(names))

    for bone in armature.data.bones:
        segments = bone_segments(bone)
        candidates = tuple(path for source, path in source_segments
                           if len(source) >= len(segments) and source[-len(segments):] == segments)
        if len(candidates) > 1:
            raise ValueError(f"Ambiguous source hierarchy path for NPC bone: {bone.name}")
        if candidates:
            source_path = candidates[0]
            parent = bone.parent
            if parent is not None:
                parent_path = assigned.get(parent.name)
                if parent_path is None or not source_path.startswith(parent_path + "/"):
                    raise ValueError(f"Source path disagrees with imported NPC bone ancestry: {bone.name}")
            bone["source_transform_path"] = source_path
            bone["source_transform_path_mode"] = "exact_avatar_path"
            assigned[bone.name] = source_path
            exact.append(bone.name)
            continue
        parent = bone.parent
        while parent is not None and parent.name not in assigned:
            parent = parent.parent
        if parent is None:
            raise ValueError(f"NPC attachment bone has no mapped host ancestor: {bone.name}")
        source_path = assigned[parent.name]
        bone["source_transform_path"] = source_path
        bone["source_transform_path_mode"] = "static_local_follow_host_ancestor"
        assigned[bone.name] = source_path
        inherited.append(bone.name)

    return {
        "exact_avatar_path_bones": len(exact),
        "static_local_follow_host_ancestor_bones": len(inherited),
        "inherited_bones_by_source_ancestor": {
            source_path: sum(1 for name in inherited if assigned[name] == source_path)
            for source_path in sorted({assigned[name] for name in inherited})
        },
        "unmapped_bones": 0,
    }


def read_npc_bind_worlds(assembly: dict[str, object], hierarchy_path: Path) -> tuple[dict[str, Matrix], dict[str, object]]:
    """Read the exact Unity source bind worlds emitted by the NPC inverse-bind probe."""
    avatar = assembly.get("avatar")
    skeletons = assembly.get("skeleton_validation")
    if not isinstance(avatar, dict) or not isinstance(skeletons, list):
        raise TypeError("NPC assembly lacks exact Avatar identity or skeleton validation rows")
    identity = (avatar.get("source"), avatar.get("path_id"), avatar.get("name"), avatar.get("raw_data_sha256"))
    if any(not isinstance(value, str) or not value for value in identity):
        raise TypeError("NPC assembly Avatar identity must include source, path ID, name, and raw-data SHA256")
    candidates = tuple(row for row in skeletons if isinstance(row, dict)
        and (row.get("avatar_source"), row.get("avatar_path_id"), row.get("avatar_name"),
             row.get("avatar_raw_data_sha256")) == identity)
    if len(candidates) != 1:
        raise ValueError(f"Expected one calibrated host Avatar skeleton: matches={len(candidates)}")
    skeleton = candidates[0]
    if skeleton.get("bind_world_calibration_format") != "EndfieldNpcBindWorldCalibration/1":
        raise ValueError("NPC assembly does not contain the source inverse-bind calibration schema")
    convention = skeleton.get("matrix_convention")
    expected_convention = "4x4 row arrays in Unity source coordinates; column-vector transforms; source hierarchy uses parentWorld * local, local=T*R*S."
    if convention != expected_convention:
        raise ValueError(f"Unsupported NPC bind-world matrix convention: {convention!r}")
    paths = skeleton.get("paths")
    parents = skeleton.get("parents")
    bind_rows = skeleton.get("source_bind_worlds")
    if not isinstance(paths, list) or not isinstance(parents, list) or not isinstance(bind_rows, list):
        raise TypeError("NPC bind-world calibration requires paths, parents, and matrix rows")
    hierarchy = json.loads(hierarchy_path.read_text(encoding="utf-8"))
    if not isinstance(hierarchy, list) or not hierarchy:
        raise TypeError(f"Expected nonempty animation hierarchy: {hierarchy_path}")
    expected_paths = tuple(row.get("path") for row in hierarchy if isinstance(row, dict))
    expected_parents = tuple(row.get("parent_index") for row in hierarchy if isinstance(row, dict))
    if (len(expected_paths) != len(hierarchy) or len(expected_parents) != len(hierarchy)
            or len(paths) != len(expected_paths) or tuple(parents) != expected_parents
            or len(bind_rows) != len(expected_paths) or not isinstance(expected_paths[0], str)
            or not expected_paths[0] or paths[0] != ""):
        raise ValueError("NPC bind-world hierarchy dimensions or root do not match the decoded animation hierarchy")
    calibrated_paths = tuple(expected_paths[0] if source_path == ""
        else f"{expected_paths[0]}/{source_path}" for source_path in paths)
    if any(not isinstance(path, str) or not path for path in expected_paths) or calibrated_paths != expected_paths:
        raise ValueError("NPC Avatar paths do not map exactly under the decoded animation root")
    allowed_provenance = {"mesh_inverse_bind_constraint_graph", "avatar_default_pose_local_chain_unconstrained"}
    reflection = Matrix.Diagonal((-1., 1., 1., 1.))
    bind_worlds: dict[str, Matrix] = {}
    mesh_constrained = 0
    local_chain = 0
    for index, raw_row in enumerate(bind_rows):
        if not isinstance(raw_row, dict):
            raise TypeError(f"NPC source bind-world row must be an object: index={index}")
        if (raw_row.get("index") != index or raw_row.get("path") != paths[index]
                or raw_row.get("parent_index") != expected_parents[index]):
            raise ValueError(f"NPC source bind-world row identity mismatch: index={index}")
        provenance = raw_row.get("provenance")
        if provenance not in allowed_provenance:
            raise ValueError(f"Unsupported NPC bind-world provenance: index={index}, source={provenance!r}")
        rows = raw_row.get("matrix_rows")
        if not isinstance(rows, list) or len(rows) != 4:
            raise TypeError(f"NPC source bind-world matrix must contain four rows: index={index}")
        numeric_rows: list[tuple[float, ...]] = []
        for row_index, matrix_row in enumerate(rows):
            if not isinstance(matrix_row, list) or len(matrix_row) != 4 or any(
                    type(value) not in (int, float) or not math.isfinite(value) for value in matrix_row):
                raise ValueError(f"NPC source bind-world matrix row is invalid: index={index}, row={row_index}")
            numeric_rows.append(tuple(float(value) for value in matrix_row))
        source_world = Matrix(numeric_rows)
        if abs(source_world.determinant()) < 1e-12:
            raise ValueError(f"NPC source bind-world matrix is singular: index={index}, path={expected_paths[index]}")
        bind_worlds[calibrated_paths[index]] = reflection @ source_world @ reflection
        if provenance == "mesh_inverse_bind_constraint_graph":
            mesh_constrained += 1
        else:
            local_chain += 1
    if len(bind_worlds) != len(expected_paths) or mesh_constrained == 0:
        raise ValueError("NPC bind-world calibration is incomplete or contains no source mesh inverse-bind evidence")
    mapping_digest = hashlib.sha256(json.dumps(bind_rows, sort_keys=True, separators=(",", ":"),
        allow_nan=False).encode("utf-8")).hexdigest()
    calibration = {
        "format": "EndfieldNpcBindWorldCalibration/1",
        "calibrated": True,
        "source_avatar": {"source": identity[0], "path_id": identity[1], "name": identity[2],
                          "raw_data_sha256": identity[3]},
        "source_bind_world_count": len(bind_rows),
        "mesh_inverse_bind_constrained_bone_count": mesh_constrained,
        "avatar_local_chain_unconstrained_bone_count": local_chain,
        "matrix_convention": convention,
        "source_to_blender_transform": "H @ sourceWorld @ H; H=diag(-1,1,1,1), preserving source axis conversion.",
        "animation_hierarchy_root_prefix": expected_paths[0],
        "mapping_sha256": mapping_digest,
    }
    return bind_worlds, calibration


def validate_npc_attachment_paths(armature: bpy.types.Object, assembly: dict[str, object],
                                  source_bone_paths: dict[str, object],
                                  hierarchy_path: Path) -> dict[str, object]:
    configuration = assembly.get("avatar_mesh_configuration")
    if not isinstance(configuration, dict) or not isinstance(configuration.get("selected_slots"), list):
        raise TypeError("NPC assembly lacks selected-slot attachment evidence")
    selected_slots = configuration["selected_slots"]
    if any(not isinstance(slot, dict) for slot in selected_slots):
        raise TypeError("NPC selected-slot rows must be objects")
    parent_values = tuple(slot.get("parent_bone_transform_name") for slot in selected_slots
        if slot.get("use_self_avatar") is True)
    if any(value is not None and not isinstance(value, str) for value in parent_values):
        raise TypeError("NPC self-Avatar parent paths must be strings or null")
    source_parents = set(parent_values)
    source_parents.discard(None)
    if any(not isinstance(path, str) or not path for path in source_parents):
        raise ValueError("NPC self-Avatar slots require exact nonempty parent-bone transform paths")
    hierarchy = json.loads(hierarchy_path.read_text(encoding="utf-8"))
    if not isinstance(hierarchy, list) or not hierarchy or not isinstance(hierarchy[0], dict):
        raise TypeError("NPC animation hierarchy requires an explicit root path")
    root_path = hierarchy[0].get("path")
    if not isinstance(root_path, str) or not root_path:
        raise ValueError("NPC animation hierarchy root path is missing")
    expected_parents = {root_path if path == "" else f"{root_path}/{path}" for path in source_parents}
    followers: dict[str, int] = {}
    for bone in armature.data.bones:
        mode = bone.get("source_transform_path_mode")
        path = bone.get("source_transform_path")
        if mode == "static_local_follow_host_ancestor":
            if path not in expected_parents:
                raise ValueError(f"NPC mounted bone does not follow a configured host attachment path: {bone.name}/{path}")
            followers[path] = followers.get(path, 0) + 1
        elif mode != "exact_avatar_path":
            raise ValueError(f"NPC bone has an unsupported source-path mode: {bone.name}/{mode}")
    if source_bone_paths["static_local_follow_host_ancestor_bones"] != sum(followers.values()):
        raise ValueError("NPC attachment path audit disagrees with actual rig bone properties")
    if set(followers) != expected_parents:
        raise ValueError(f"NPC mounted bones do not cover configured parent paths: expected={sorted(expected_parents)}, actual={sorted(followers)}")
    return {"configured_host_attachment_parent_paths": sorted(expected_parents),
            "mounted_bones_by_host_parent": dict(sorted(followers.items())),
            "mounted_bones_use_host_bind_world": True}


def verify_npc_calibration_identity(assembly: dict[str, object], source_probe: dict[str, object],
                                    calibration_assembly: dict[str, object],
                                    calibration_probe: dict[str, object]) -> None:
    """Require the exact static assembly input, Avatar and selected slot configuration."""
    for label, probe in (("source", source_probe), ("calibration", calibration_probe)):
        if not isinstance(probe, dict) or probe.get("status") != "source_geometry_exported":
            raise ValueError(f"NPC {label} probe must report source_geometry_exported")
        digest = probe.get("input_sha256")
        if not isinstance(digest, str) or re.fullmatch(r"[0-9a-fA-F]{64}", digest) is None:
            raise ValueError(f"NPC {label} probe requires a valid input_sha256")
    for label, document in (("source", assembly), ("calibration", calibration_assembly)):
        if not isinstance(document, dict):
            raise TypeError(f"NPC {label} assembly must be an object")
        avatar = document.get("avatar")
        selection = document.get("selection")
        if not isinstance(avatar, dict) or not isinstance(selection, dict):
            raise TypeError(f"NPC {label} assembly requires Avatar and selection objects")
        if any(not isinstance(avatar.get(field), str) or not avatar[field]
               for field in ("source", "path_id", "name", "raw_data_sha256")):
            raise TypeError(f"NPC {label} Avatar requires source, path_id, name and raw_data_sha256")
        if (re.fullmatch(r"-?[0-9]+", avatar["path_id"]) is None
                or re.fullmatch(r"[0-9a-fA-F]{64}", avatar["raw_data_sha256"]) is None):
            raise ValueError(f"NPC {label} Avatar path_id or raw_data_sha256 is invalid")
        if not isinstance(selection.get("character_id"), str) or not selection["character_id"]:
            raise TypeError(f"NPC {label} selection requires character_id")
        if "avatar_mesh_configuration" not in document:
            raise ValueError(f"NPC {label} assembly is missing avatar_mesh_configuration")
        configuration = document["avatar_mesh_configuration"]
        if configuration is not None and not isinstance(configuration, dict):
            raise TypeError(f"NPC {label} avatar_mesh_configuration must be an object or null")
    if (calibration_probe.get("status") != "source_geometry_exported"
            or calibration_probe.get("input_sha256") != source_probe.get("input_sha256")
            or calibration_assembly.get("avatar") != assembly.get("avatar")
            or calibration_assembly.get("selection") != assembly.get("selection")
            or calibration_assembly.get("avatar_mesh_configuration") != assembly.get("avatar_mesh_configuration")):
        raise ValueError("NPC bind-world calibration does not match the exact static assembly input and Avatar selection")
