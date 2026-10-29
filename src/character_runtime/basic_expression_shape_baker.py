"""Bake a small, source-bound facial control set into synchronized shape keys."""
from __future__ import annotations

from dataclasses import dataclass
import hashlib
import json
import math
from pathlib import Path
from typing import cast
import zlib

import bpy
from mathutils import Matrix, Quaternion, Vector

from character_expression_shape_keys import (
    BakedExpression, ExpressionDriverControl, FacialBoneTarget, bake_facial_target,
    isolated_pose_evaluation, validate_expression_drivers,
)


@dataclass(frozen=True)
class BasicExpressionDefinition:
    source_name: str
    label: str
    aliases: tuple[str, ...]
    category: str
    conflict_notes: tuple[str, ...]


@dataclass(frozen=True)
class SourceRigData:
    avatar_identity: tuple[str, int]
    source_rows_by_path: dict[str, dict[str, object]]
    source_global_by_path: dict[str, Matrix]
    base_pose_by_bone_id: dict[int, dict[str, object]]
    path_by_bone_id: dict[int, str]
    source_to_blender_rows: tuple[dict[str, object], ...]


@dataclass(frozen=True)
class DecodedSource:
    character_id: str
    table_sha256: str
    audit_sha256: str
    definitions: tuple[BasicExpressionDefinition, ...]
    control_rows: dict[str, dict[str, object]]
    mapping_records: dict[int, dict[str, object]]
    rig_data: SourceRigData


BASIC_EXPRESSION_DEFINITIONS = (
    BasicExpressionDefinition("mouth_a_normal_ctrl", "Mouth A", ("A", "あ"), "vowel", ("Mutually exclusive with the other vowel keys by author convention.", "Can conflict visually with soft smile.")),
    BasicExpressionDefinition("mouth_i_normal_ctrl", "Mouth I", ("I", "い"), "vowel", ("Mutually exclusive with the other vowel keys by author convention.", "Can conflict visually with soft smile.")),
    BasicExpressionDefinition("mouth_u_normal_ctrl", "Mouth U", ("U", "う"), "vowel", ("Mutually exclusive with the other vowel keys by author convention.", "Can conflict visually with soft smile.")),
    BasicExpressionDefinition("mouth_e_normal_ctrl", "Mouth E", ("E", "え"), "vowel", ("Mutually exclusive with the other vowel keys by author convention.", "Can conflict visually with soft smile.")),
    BasicExpressionDefinition("mouth_o_normal_ctrl", "Mouth O", ("O", "お"), "vowel", ("Mutually exclusive with the other vowel keys by author convention.", "Can conflict visually with soft smile.")),
    BasicExpressionDefinition("eye_thinkcloseeyes_a_L_ctrl", "Blink L", ("Blink Left", "左闭眼"), "eye", ("Can conflict with another left-eye closure or wink control.")),
    BasicExpressionDefinition("eye_thinkcloseeyes_a_R_ctrl", "Blink R", ("Blink Right", "右闭眼"), "eye", ("Can conflict with another right-eye closure or wink control.")),
    BasicExpressionDefinition("mouth_happy_s_ctrl", "Soft Smile", ("Smile", "轻笑"), "mouth", ("Can conflict visually with vowel keys.")),
    BasicExpressionDefinition("brow_happy_s_L_ctrl", "Happy Brow L", ("Brow Happy Left", "左眉轻扬"), "brow", ("Can conflict with another left-brow mood control.")),
    BasicExpressionDefinition("brow_happy_s_R_ctrl", "Happy Brow R", ("Brow Happy Right", "右眉轻扬"), "brow", ("Can conflict with another right-brow mood control.")),
)

IDENTITY = Matrix.Identity(4)
BASE_POSITION_LIMIT_METERS = 0.006
BASE_ROTATION_DOT_ERROR_LIMIT = 0.005
POSE_ROUNDTRIP_LIMIT = 2.0e-4


def _record(value: object, context: str) -> dict[str, object]:
    if not isinstance(value, dict) or any(not isinstance(key, str) for key in value):
        raise TypeError(f"Expected JSON object at {context}")
    return cast(dict[str, object], value)


def _array(value: object, context: str) -> list[object]:
    if not isinstance(value, list):
        raise TypeError(f"Expected JSON array at {context}")
    return value


def _text(value: object, context: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise TypeError(f"Expected nonempty string at {context}")
    return value


def _integer(value: object, context: str) -> int:
    if isinstance(value, bool):
        raise TypeError(f"Expected integer at {context}")
    if isinstance(value, int):
        return value
    if isinstance(value, str):
        try:
            return int(value, 10)
        except ValueError as error:
            raise TypeError(f"Expected decimal integer at {context}") from error
    raise TypeError(f"Expected integer at {context}")


def _number(value: object, context: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise TypeError(f"Expected number at {context}")
    result = float(value)
    if not math.isfinite(result):
        raise ValueError(f"Expected finite number at {context}")
    return result


def _vector3(value: object, context: str) -> Vector:
    if isinstance(value, dict):
        data = _record(value, context)
        return Vector(tuple(_number(data[axis], f"{context}.{axis}") for axis in ("x", "y", "z")))
    values = _array(value, context)
    if len(values) != 3:
        raise ValueError(f"Expected three vector components at {context}; got={len(values)}")
    return Vector(tuple(_number(item, f"{context}[{index}]") for index, item in enumerate(values)))


def _source_quaternion(value: object, context: str) -> Quaternion:
    values = _array(value, context)
    if len(values) != 4:
        raise ValueError(f"Expected four quaternion components at {context}; got={len(values)}")
    x, y, z, w = (_number(item, f"{context}[{index}]") for index, item in enumerate(values))
    return Quaternion((w, x, y, z))


def _convert_source_quaternion(rotation: Quaternion) -> Quaternion:
    return Quaternion((rotation.w, rotation.x, -rotation.y, -rotation.z))


def _morph_euler_quaternion(value: Vector) -> Quaternion:
    x, y, z = (math.radians(float(component)) * 0.5 for component in value)
    qx = Quaternion((math.cos(x), math.sin(x), 0.0, 0.0))
    qy = Quaternion((math.cos(y), 0.0, math.sin(y), 0.0))
    qz = Quaternion((math.cos(z), 0.0, 0.0, math.sin(z)))
    return qz @ qy @ qx


def _fbx_local_matrix(position: Vector, rotation: Quaternion, scale: Vector) -> Matrix:
    return Matrix.LocRotScale(Vector((-position.x, position.y, position.z)), rotation.normalized(), scale)


def _matrix_error(left: Matrix, right: Matrix) -> float:
    return max(abs(left[row][column] - right[row][column]) for row in range(4) for column in range(4))


def _matrix_json(matrix: Matrix) -> list[list[float]]:
    return [[float(component) for component in row] for row in matrix]


def _euler_orientation_error(source: Quaternion, euler: Vector) -> float:
    dot = abs(_convert_source_quaternion(source).dot(_morph_euler_quaternion(euler)))
    return 1.0 - min(1.0, dot)


def _morph_target_matrix(base_pose: dict[str, object], mapping: dict[str, object], context: str) -> Matrix:
    base_position = _vector3(base_pose["position"], f"{context}.base.position")
    mapping_position = _vector3(mapping["position"], f"{context}.mapping.position")
    base_rotation = _vector3(base_pose["rotation"], f"{context}.base.rotation")
    mapping_rotation = _vector3(mapping["rotation"], f"{context}.mapping.rotation")
    base_scale = _vector3(base_pose["scale"], f"{context}.base.scale")
    mapping_scale = _vector3(mapping["scale"], f"{context}.mapping.scale")
    target_scale = base_scale + mapping_scale
    return _fbx_local_matrix(base_position + mapping_position,
                             _morph_euler_quaternion(base_rotation + mapping_rotation), target_scale)


def _read_json(path: Path, context: str) -> tuple[dict[str, object], bytes, Path]:
    resolved = path.resolve(strict=True)
    if not resolved.is_file():
        raise FileNotFoundError(f"Expected {context} file: {resolved}")
    payload = resolved.read_bytes()
    return _record(json.loads(payload.decode("utf-8-sig")), context), payload, resolved


def _avatar_identity(table: dict[str, object], audit: dict[str, object]) -> tuple[str, int]:
    if table.get("schema") != "EndfieldCharacterExpressionSource/2":
        raise ValueError("Basic expression baking requires EndfieldCharacterExpressionSource/2")
    selected = _record(table.get("selected_animator_avatar"), "selected Animator Avatar")
    if selected.get("class_name") != "Avatar":
        raise ValueError("Expression source selection is not an Avatar")
    identity = (_text(selected.get("cab"), "selected Avatar CAB").casefold(),
                _integer(selected.get("path_id"), "selected Avatar PathID"))
    animators = _array(audit.get("animators"), "source audit.animators")
    matching: set[tuple[str, int]] = set()
    for index, value in enumerate(animators):
        animator = _record(value, f"source audit.animators[{index}]")
        avatar = animator.get("avatar")
        if not isinstance(avatar, dict) or avatar.get("resolved") is None:
            continue
        resolved = _record(avatar["resolved"], f"source audit.animators[{index}].avatar.resolved")
        if resolved.get("type") == "Avatar":
            matching.add((_text(resolved.get("source"), "resolved Avatar CAB").casefold(),
                          _integer(resolved.get("path_id"), "resolved Avatar PathID")))
    if identity not in matching:
        raise ValueError(f"Expression Avatar does not match the source audit Animator: identity={identity}, available={sorted(matching)}")
    return identity


def _build_source_rig_data(
    table: dict[str, object],
    audit: dict[str, object],
    armature: bpy.types.Object,
    provenance: dict[str, object],
    audit_payload: bytes,
) -> SourceRigData:
    expected_audit_sha = _text(provenance.get("source_audit_sha256"), "provenance.source_audit_sha256")
    actual_audit_sha = hashlib.sha256(audit_payload).hexdigest()
    if actual_audit_sha != expected_audit_sha:
        raise ValueError(f"Source audit changed after expression extraction: expected={expected_audit_sha}, actual={actual_audit_sha}")
    avatar_identity = _avatar_identity(table, audit)
    if table.get("avatar_bone_bindings_status") != "exact_avatar_m_id_to_tos_paths_and_source_base_pose_verified":
        raise ValueError("Expression source lacks exact Avatar skeleton-to-TOS path bindings")
    raw = _record(table.get("raw"), "raw")
    asset = _record(raw.get("asset_prefix"), "raw.asset_prefix")
    asset_data = _record(asset.get("data"), "raw.asset_prefix.data")
    base_pose = tuple(
        _record(value, f"basePoseConfig[{index}]")
        for index, value in enumerate(_array(asset_data.get("basePoseConfig"), "basePoseConfig"))
    )
    all_names = tuple(
        _text(value, f"allBoneNames[{index}]")
        for index, value in enumerate(_array(asset_data.get("allBoneNames"), "allBoneNames"))
    )
    bindings = tuple(_record(value, f"avatar_bone_bindings[{index}]") for index, value in enumerate(
        _array(table.get("avatar_bone_bindings"), "avatar_bone_bindings")))
    nodes_document = _record(table.get("avatar_node_map"), "avatar_node_map")
    nodes = tuple(_record(value, f"avatar_node_map.nodes[{index}]") for index, value in enumerate(
        _array(nodes_document.get("nodes"), "avatar_node_map.nodes")))
    if len(base_pose) != len(all_names) or len(base_pose) != len(bindings):
        raise ValueError(f"Expression source base-pose binding lengths differ: base={len(base_pose)}, names={len(all_names)}, bindings={len(bindings)}")
    if nodes_document.get("status") != "exact_avatar_m_id_to_tos_paths_decoded":
        raise ValueError("Expression source Avatar node map is not exact and decoded")
    nodes_by_index = {_integer(node.get("index"), "Avatar node index"): node for node in nodes}
    source_transforms = tuple(_record(value, f"source audit.transforms[{index}]") for index, value in enumerate(
        _array(audit.get("transforms"), "source audit.transforms")))
    source_rows_by_path: dict[str, dict[str, object]] = {}
    for row in source_transforms:
        raw_path = _text(row.get("path"), "source transform.path")
        if "/" not in raw_path:
            continue
        path = raw_path.split("/", maxsplit=1)[1]
        if path in source_rows_by_path:
            raise ValueError(f"Static source audit has duplicate transform path: {path}")
        source_rows_by_path[path] = row
    source_global: dict[str, Matrix] = {}
    for path in sorted(source_rows_by_path, key=lambda item: (item.count("/"), item)):
        row = source_rows_by_path[path]
        position = _vector3(row.get("position"), f"{path}.position")
        rotation = _convert_source_quaternion(_source_quaternion(row.get("rotation"), f"{path}.rotation"))
        scale = _vector3(row.get("scale"), f"{path}.scale")
        local = _fbx_local_matrix(position, rotation, scale)
        parent = path.rsplit("/", maxsplit=1)[0] if "/" in path else None
        source_global[path] = source_global[parent] @ local if parent in source_global else local

    base_pose_by_bone_id: dict[int, dict[str, object]] = {}
    path_by_bone_id: dict[int, str] = {}
    binding_rows: list[dict[str, object]] = []
    for index, (base, name, binding) in enumerate(zip(base_pose, all_names, bindings, strict=True)):
        bone_id = _integer(base.get("boneID"), f"basePoseConfig[{index}].boneID")
        if bone_id != _integer(binding.get("bone_id"), f"avatar_bone_bindings[{index}].bone_id"):
            raise ValueError(f"Avatar boneID differs from exact binding at basePose row {index}")
        if name != _text(binding.get("all_bone_name"), f"avatar_bone_bindings[{index}].all_bone_name"):
            raise ValueError(f"Avatar bone name differs from exact binding at basePose row {index}")
        path = _text(binding.get("avatar_tos_path"), f"avatar_bone_bindings[{index}].avatar_tos_path")
        terminal = _text(binding.get("terminal_name"), f"avatar_bone_bindings[{index}].terminal_name")
        if path.rsplit("/", maxsplit=1)[-1] != name or terminal != name:
            raise ValueError(f"Avatar TOS path does not exactly terminate at basePose bone name: {path}, {name}")
        node = nodes_by_index.get(bone_id)
        if node is None or _integer(node.get("avatar_skeleton_id"), f"avatar_node_map.nodes[{bone_id}].avatar_skeleton_id") != _integer(binding.get("avatar_skeleton_id"), "binding.avatar_skeleton_id"):
            raise ValueError(f"Avatar node map does not resolve exact boneID {bone_id}")
        if _text(node.get("avatar_tos_path"), f"avatar_node_map.nodes[{bone_id}].avatar_tos_path") != path:
            raise ValueError(f"Avatar node map TOS path differs from binding: boneID={bone_id}")
        source_path = path
        source_row = source_rows_by_path.get(source_path)
        if source_row is None:
            raise ValueError(f"Static Transform audit is missing exact Avatar path: {path}")
        if name not in armature.data.bones:
            raise ValueError(f"Selected rig is missing exact Avatar-bound bone name: {armature.name}/{name}")
        source_position = _vector3(source_row.get("position"), f"{source_path}.position")
        base_position = _vector3(base.get("position"), f"basePoseConfig[{index}].position")
        position_error = float((source_position - base_position).length)
        source_rotation = _source_quaternion(source_row.get("rotation"), f"{source_path}.rotation")
        base_rotation = _vector3(base.get("rotation"), f"basePoseConfig[{index}].rotation")
        rotation_error = _euler_orientation_error(source_rotation, base_rotation)
        source_matrix = source_global.get(source_path)
        if source_matrix is None:
            raise ValueError(f"Static global matrix is missing exact Avatar path: {path}")
        blender_bone = armature.data.bones[name]
        head_error = float((source_matrix.translation - blender_bone.head_local).length)
        rest_correction = source_matrix.inverted() @ blender_bone.matrix_local
        rest_reconstruction_error = _matrix_error(source_matrix @ rest_correction, blender_bone.matrix_local)
        pose_rest_error = _matrix_error(armature.pose.bones[name].matrix, blender_bone.matrix_local)
        if position_error > BASE_POSITION_LIMIT_METERS or rotation_error > BASE_ROTATION_DOT_ERROR_LIMIT:
            raise ValueError(f"Source base-pose does not match its static Transform: bone={name}, base_position_m={position_error}, base_rotation_dot_error={rotation_error}")
        if rest_reconstruction_error > 1.0e-5 or pose_rest_error > 2.0e-5:
            raise ValueError(f"Per-bone rest correction does not reconstruct the imported identity rig: bone={name}, source_to_blender_K_error={rest_reconstruction_error}, pose_rest_error={pose_rest_error}")
        base_pose_by_bone_id[bone_id] = base
        path_by_bone_id[bone_id] = path
        binding_rows.append({
            "base_pose_row": index,
            "bone_id": bone_id,
            "avatar_tos_path": path,
            "blender_bone": name,
            "base_position_error_meters": position_error,
            "base_rotation_dot_error": rotation_error,
            "source_to_blender_rest_head_error_meters": head_error,
            "source_to_blender_rest_correction_K": _matrix_json(rest_correction),
            "source_rest_times_K_matrix_error": rest_reconstruction_error,
            "identity_pose_matrix_error": pose_rest_error,
        })
    return SourceRigData(avatar_identity, source_rows_by_path, source_global,
                         base_pose_by_bone_id, path_by_bone_id, tuple(binding_rows))


def _decode_source(
    table: dict[str, object],
    audit: dict[str, object],
    table_payload: bytes,
    audit_payload: bytes,
    armature: bpy.types.Object,
    definitions: tuple[BasicExpressionDefinition, ...],
) -> DecodedSource:
    raw = _record(table.get("raw"), "raw")
    asset = _record(raw.get("asset_prefix"), "raw.asset_prefix")
    asset_data = _record(asset.get("data"), "raw.asset_prefix.data")
    control_names = tuple(_text(value, f"morphMappingNames[{index}]") for index, value in enumerate(
        _array(asset_data.get("morphMappingNames"), "morphMappingNames")))
    configs = tuple(_record(value, f"morphMappingCfgs[{index}]") for index, value in enumerate(
        _array(asset_data.get("morphMappingCfgs"), "morphMappingCfgs")))
    controls = tuple(_record(value, f"control_index[{index}]") for index, value in enumerate(
        _array(table.get("control_index"), "control_index")))
    selected_names = frozenset(definition.source_name for definition in definitions)
    references = tuple(_record(value, f"raw.managed_references[{index}]") for index, value in enumerate(
        _array(raw.get("managed_references"), "raw.managed_references")))
    if len(control_names) != len(configs) or len(control_names) != len(controls):
        raise ValueError(f"Expression control arrays differ in length: names={len(control_names)}, configs={len(configs)}, index={len(controls)}")
    reference_by_rid: dict[int, dict[str, object]] = {}
    reference_index_by_rid: dict[int, int] = {}
    for index, record in enumerate(references):
        rid = _integer(record.get("rid"), f"managed_references[{index}].rid")
        if rid in reference_by_rid:
            raise ValueError(f"Duplicate managed-reference RID: {rid}")
        reference_by_rid[rid] = record
        reference_index_by_rid[rid] = index
    control_by_name: dict[str, dict[str, object]] = {}
    for index, (name, config, control) in enumerate(zip(control_names, configs, controls, strict=True)):
        if control.get("index") != index or control.get("name") != name:
            raise ValueError(f"Control index/name does not match raw arrays at index {index}")
        rid = _integer(config.get("rid"), f"morphMappingCfgs[{index}].rid")
        if rid != _integer(control.get("rid"), f"control_index[{index}].rid"):
            raise ValueError(f"Control RID differs from raw morphMappingCfgs at index {index}")
        record = reference_by_rid.get(rid)
        if record is None or record.get("class") != control.get("class_name"):
            raise ValueError(f"Control class differs from its exact managed-reference record: {name}")
        pointer = _text(control.get("raw_payload_pointer"), f"control_index[{index}].raw_payload_pointer")
        expected_pointer = f"/raw/managed_references/{reference_index_by_rid[rid]}"
        if pointer != expected_pointer:
            raise ValueError(f"Control payload pointer differs from exact registry location: {name}, pointer={pointer}, expected={expected_pointer}")
        mapping_data = _record(record.get("data"), f"managed reference {rid}.data")
        if name in selected_names:
            if record.get("class") != "SkeletalMorphMappingData":
                raise ValueError(f"Selected source control is not an exact skeletal mapping record: {name}")
            if _integer(mapping_data.get("id"), f"managed reference {rid}.data.id") != index:
                raise ValueError(f"Skeletal mapping ID differs from control index: {name}")
            if (_integer(mapping_data.get("nameHash"), f"managed reference {rid}.data.nameHash") & 0xFFFFFFFF) != (zlib.crc32(name.encode("utf-8")) & 0xFFFFFFFF):
                raise ValueError(f"Skeletal mapping name hash differs from exact source control name: {name}")
        control_by_name[name] = control

    provenance = _record(table.get("provenance"), "provenance")
    expected_audit_sha = _text(provenance.get("source_audit_sha256"), "provenance.source_audit_sha256")
    actual_audit_sha = hashlib.sha256(audit_payload).hexdigest()
    if expected_audit_sha != actual_audit_sha:
        raise ValueError(f"Source audit SHA-256 mismatch: expected={expected_audit_sha}, actual={actual_audit_sha}")
    rig_data = _build_source_rig_data(table, audit, armature, provenance, audit_payload)
    return DecodedSource(_text(table.get("character_id"), "character_id"),
                         hashlib.sha256(table_payload).hexdigest(), actual_audit_sha,
                         definitions, control_by_name, reference_by_rid, rig_data)


def _mapping_for_control(source: DecodedSource, definition: BasicExpressionDefinition) -> tuple[int, int, dict[str, object], list[dict[str, object]]] | None:
    control = source.control_rows.get(definition.source_name)
    if control is None:
        return None
    index = _integer(control.get("index"), f"{definition.source_name}.index")
    rid = _integer(control.get("rid"), f"{definition.source_name}.rid")
    record = source.mapping_records[rid]
    data = _record(record.get("data"), f"{definition.source_name}.data")
    raw_bones = [_record(value, f"{definition.source_name}.bones[{bone_index}]") for bone_index, value in enumerate(
        _array(data.get("bones"), f"{definition.source_name}.bones"))]
    if not raw_bones:
        raise ValueError(f"Source control has no skeletal endpoints: {definition.source_name}")
    return index, rid, data, raw_bones


def _extract_pose_targets(
    armature: bpy.types.Object,
    source: DecodedSource,
    definition: BasicExpressionDefinition,
    mapping_bones: list[dict[str, object]],
) -> tuple[tuple[FacialBoneTarget, ...], tuple[dict[str, object], ...]]:
    with isolated_pose_evaluation(armature):
        return _extract_active_pose_targets(armature, source, definition, mapping_bones)


def _extract_pose_target_batch(
    armature: bpy.types.Object,
    source: DecodedSource,
    mappings: tuple[tuple[BasicExpressionDefinition, list[dict[str, object]]], ...],
) -> dict[str, tuple[tuple[FacialBoneTarget, ...], tuple[dict[str, object], ...]]]:
    """Resolve all static endpoints before changing any source shape keys."""
    if not mappings:
        return {}
    with isolated_pose_evaluation(armature):
        return {definition.source_name: _extract_active_pose_targets(armature, source, definition, bones)
                for definition, bones in mappings}


def _extract_active_pose_targets(
    armature: bpy.types.Object,
    source: DecodedSource,
    definition: BasicExpressionDefinition,
    mapping_bones: list[dict[str, object]],
) -> tuple[tuple[FacialBoneTarget, ...], tuple[dict[str, object], ...]]:
    target_local_by_path: dict[str, Matrix] = {}
    mapping_rows: list[dict[str, object]] = []
    seen_bone_ids: set[int] = set()
    for index, mapping in enumerate(mapping_bones):
        bone_id = _integer(mapping.get("boneID"), f"{definition.source_name}.bones[{index}].boneID")
        if bone_id in seen_bone_ids:
            raise ValueError(f"Source control repeats a boneID: {definition.source_name}/{bone_id}")
        seen_bone_ids.add(bone_id)
        base = source.rig_data.base_pose_by_bone_id.get(bone_id)
        avatar_path = source.rig_data.path_by_bone_id.get(bone_id)
        if base is None or avatar_path is None:
            raise ValueError(f"Source control boneID lacks exact base-pose/Avatar binding: {definition.source_name}/{bone_id}")
        mapping_hash = _integer(mapping.get("boneNameHash"), f"{definition.source_name}.bones[{index}].boneNameHash") & 0xFFFFFFFF
        base_hash = _integer(base.get("boneNameHash"), f"basePoseConfig[{bone_id}].boneNameHash") & 0xFFFFFFFF
        if mapping_hash != base_hash:
            raise ValueError(f"Morph control hash differs from exact base-pose hash: {definition.source_name}/{bone_id}")
        terminal = avatar_path.rsplit("/", maxsplit=1)[-1]
        if (zlib.crc32(terminal.encode("utf-8")) & 0xFFFFFFFF) != mapping_hash:
            raise ValueError(f"Morph control hash does not match exact Avatar terminal name: {definition.source_name}/{terminal}")
        mapping_scale = _vector3(mapping.get("scale"), f"{definition.source_name}.bones[{index}].scale")
        if any(not math.isfinite(value) for value in mapping_scale):
            raise ValueError(f"Nonfinite scale in source control: {definition.source_name}/{terminal}")
        target_local_by_path[avatar_path] = _morph_target_matrix(base, mapping, f"{definition.source_name}.bones[{index}]")
        mapping_rows.append({
            "bone_index_in_control": index,
            "bone_id": bone_id,
            "avatar_tos_path": avatar_path,
            "blender_bone": terminal,
            "position_delta_source": [float(value) for value in _vector3(mapping.get("position"), "mapping.position")],
            "rotation_euler_delta_source": [float(value) for value in _vector3(mapping.get("rotation"), "mapping.rotation")],
            "scale_delta_source": [float(value) for value in mapping_scale],
        })

    target_global_by_path: dict[str, Matrix] = {}
    for path in sorted(source.rig_data.source_global_by_path, key=lambda item: (item.count("/"), item)):
        source_row = source.rig_data.source_rows_by_path[path]
        source_local = _fbx_local_matrix(
            _vector3(source_row.get("position"), f"{path}.position"),
            _convert_source_quaternion(_source_quaternion(source_row.get("rotation"), f"{path}.rotation")),
            _vector3(source_row.get("scale"), f"{path}.scale"),
        )
        local_target = target_local_by_path.get(path)
        local = local_target if local_target is not None else source_local
        parent_path = path.rsplit("/", maxsplit=1)[0] if "/" in path else None
        parent_target = target_global_by_path.get(parent_path) if parent_path else None
        target_global_by_path[path] = local if parent_target is None else parent_target @ local

    targets: list[FacialBoneTarget] = []
    target_reports: list[dict[str, object]] = []
    for avatar_path in sorted(target_local_by_path, key=lambda item: (item.count("/"), item)):
        bone_name = avatar_path.rsplit("/", maxsplit=1)[-1]
        bone = armature.data.bones[bone_name]
        source_path = avatar_path
        source_global = source.rig_data.source_global_by_path[source_path]
        target_global = target_global_by_path[source_path]
        roll_offset = source_global.inverted() @ bone.matrix_local
        desired_pose = target_global @ roll_offset
        skin_delta_source = target_global @ source_global.inverted()
        skin_delta_blender = desired_pose @ bone.matrix_local.inverted()
        skin_delta_error = _matrix_error(skin_delta_blender, skin_delta_source)
        if skin_delta_error > 2.0e-6:
            raise ValueError(f"Source/Blender skin-delta invariant failed: control={definition.source_name}, bone={bone_name}, error={skin_delta_error}")
        pose_bone = armature.pose.bones[bone_name]
        pose_bone.matrix = desired_pose
        bpy.context.view_layer.update()
        actual_pose = pose_bone.matrix.copy()
        pose_error = _matrix_error(actual_pose, desired_pose)
        if pose_error > POSE_ROUNDTRIP_LIMIT:
            raise ValueError(f"Blender could not realize source control pose matrix: control={definition.source_name}, bone={bone_name}, error={pose_error}")
        basis = pose_bone.matrix_basis.copy()
        targets.append(FacialBoneTarget(bone_name, basis))
        target_reports.append({
            "bone_name": bone_name,
            "avatar_tos_path": avatar_path,
            "source_to_blender_rest_roll_offset": _matrix_json(roll_offset),
            "target_transform_global_fbx": _matrix_json(target_global),
            "desired_pose_matrix_armature_space": _matrix_json(desired_pose),
            "matrix_basis": _matrix_json(basis),
            "pose_matrix_roundtrip_error": pose_error,
            "source_vs_blender_skin_delta_invariant_error": skin_delta_error,
        })
    for pose_bone in armature.pose.bones:
        pose_bone.matrix_basis = IDENTITY
    bpy.context.view_layer.update()
    remaining = max(_matrix_error(pose_bone.matrix_basis, IDENTITY) for pose_bone in armature.pose.bones)
    if remaining > 2.0e-5:
        raise ValueError(f"Armature did not return to identity after endpoint extraction: control={definition.source_name}, error={remaining}")
    return tuple(targets), tuple(mapping_rows + [{"pose_bone_targets": target_reports, "identity_restore_max_error": remaining}])


def _visible_fur_proxy_names() -> frozenset[str]:
    names: set[str] = set()
    for obj in bpy.context.scene.objects:
        if obj.type not in {"CURVE", "CURVES"} or obj.hide_render or not obj.visible_get():
            continue
        target_name = obj.get("fur_skin_proxy")
        if isinstance(target_name, str) and target_name.strip():
            names.add(target_name)
    return frozenset(names)


def _selected_rig_meshes(armature: bpy.types.Object) -> tuple[bpy.types.Object, ...]:
    from character_authoring_identity import renderer_reference, source_character_meshes

    audit_renderers = tuple(_record(value, "source audit renderer") for value in ())
    del audit_renderers
    candidates = source_character_meshes()
    proxy_names = _visible_fur_proxy_names()
    selected: list[bpy.types.Object] = []
    seen: set[str] = set()
    for obj in candidates:
        if obj.name in seen:
            raise ValueError(f"Source mesh helper returned a duplicate object: {obj.name}")
        seen.add(obj.name)
        if obj.type != "MESH":
            continue
        if obj.name not in proxy_names and (obj.hide_render or not obj.visible_get()):
            continue
        modifiers = tuple(modifier for modifier in obj.modifiers if modifier.type == "ARMATURE" and modifier.show_viewport)
        if len(modifiers) != 1 or modifiers[0].object != armature:
            raise ValueError(f"Selected source mesh is not bound to exactly the requested rig: object={obj.name}, rig={armature.name}, modifiers={[(item.name, item.object.name if item.object else None) for item in modifiers]}")
        renderer_reference(obj)
        selected.append(obj)
    if not selected:
        raise ValueError(f"No visible source meshes or referenced fur proxy are bound to the requested rig: {armature.name}")
    return tuple(selected)


def _make_mesh_data_single_user(
    meshes: tuple[bpy.types.Object, ...],
) -> tuple[dict[str, bpy.types.Mesh], tuple[dict[str, object], ...]]:
    originals: dict[str, bpy.types.Mesh] = {}
    rows: list[dict[str, object]] = []
    try:
        for obj in meshes:
            original = obj.data
            if original.users <= 1:
                continue
            originals[obj.name] = original
            rows.append({"object_name": obj.name, "original_mesh_data": original.name, "original_user_count": original.users})
            obj.data = original.copy()
            if obj.data.users != 1:
                raise ValueError(f"Copied mesh data is still shared after single-user isolation: {obj.name}, users={obj.data.users}")
    except Exception:
        _restore_original_mesh_data(meshes, originals)
        raise
    return originals, tuple(rows)


def _restore_original_mesh_data(
    meshes: tuple[bpy.types.Object, ...],
    originals: dict[str, bpy.types.Mesh],
) -> None:
    for obj in meshes:
        original = originals.get(obj.name)
        if original is None or obj.data == original:
            continue
        copied = obj.data
        obj.data = original
        if copied.users == 0:
            bpy.data.meshes.remove(copied)


def _create_controller(name: str, labels: tuple[str, ...]) -> bpy.types.Object:
    if bpy.data.objects.get(name) is not None:
        raise FileExistsError(f"Expression controller object already exists: {name}")
    controller = bpy.data.objects.new(name, None)
    bpy.context.collection.objects.link(controller)
    controller.empty_display_type = "PLAIN_AXES"
    controller.empty_display_size = 0.15
    controller["expression_controls_format"] = "EndfieldBasicExpressionControls/1"
    for label in labels:
        controller[label] = 0.0
    return controller


def _attach_driver(key: bpy.types.ShapeKey, controller: bpy.types.Object, label: str) -> None:
    driver = key.driver_add("value").driver
    driver.type = "SCRIPTED"
    driver.expression = "v"
    variable = driver.variables.new()
    variable.name = "v"
    variable.type = "SINGLE_PROP"
    variable.targets[0].id_type = "OBJECT"
    variable.targets[0].id = controller
    variable.targets[0].data_path = f'["{label}"]'


def _cleanup_new_shapes(meshes: tuple[bpy.types.Object, ...], previous_names: dict[str, frozenset[str]]) -> None:
    for obj in meshes:
        keys = obj.data.shape_keys
        if keys is None:
            continue
        new_keys = tuple(key for key in keys.key_blocks if key.name not in previous_names[obj.name])
        for key in reversed(new_keys):
            if key.id_data.animation_data:
                driver_path = key.path_from_id("value")
                if any(curve.data_path == driver_path for curve in key.id_data.animation_data.drivers):
                    key.driver_remove("value")
            obj.shape_key_remove(key)


def _control_report(
    definition: BasicExpressionDefinition,
    source: DecodedSource,
    controller: bpy.types.Object | None,
    mapping_info: tuple[int, int, dict[str, object], list[dict[str, object]]] | None,
    mapping_rows: tuple[dict[str, object], ...],
    baked: tuple[BakedExpression, ...],
) -> dict[str, object]:
    if mapping_info is None:
        return {
            "source_control": definition.source_name,
            "label": definition.label,
            "aliases_are_authoring_labels_only": list(definition.aliases),
            "category": definition.category,
            "conflict_notes": list(definition.conflict_notes),
            "status": "missing_exact_source_control",
            "master_object": None,
            "master_key": None,
            "driven_objects": [],
        }
    if controller is None:
        raise ValueError(f"Present source control has no expression controller: {definition.source_name}")
    control_index, rid, mapping_data, mapping_bones = mapping_info
    mesh_rows = [
        {"object_name": item.object_name, "shape_key_name": item.shape_key_name,
         "moved_vertices": item.moved_vertices, "maximum_delta_meters": item.maximum_delta}
        for item in baked
    ]
    status = "baked_and_synchronized" if mesh_rows else "source_control_has_no_visible_mesh_delta_above_tolerance"
    return {
        "source_control": definition.source_name,
        "label": definition.label,
        "aliases_are_authoring_labels_only": list(definition.aliases),
        "category": definition.category,
        "conflict_notes": list(definition.conflict_notes),
        "status": status,
        "source_control_index": control_index,
        "managed_reference_rid": str(rid),
        "part_type_raw_uninterpreted": _integer(mapping_data.get("partType"), f"{definition.source_name}.partType"),
        "bone_count": len(mapping_bones),
        "candidate_transform_combination": "position=basePose.position+mapping.position; rotationEuler=basePose.rotation+mapping.rotation; scale=basePose.scale*(1+mapping.scale)",
        "candidate_runtime_semantics_verified": False,
        "bone_mappings_and_pose_basis": list(mapping_rows),
        "baked_meshes": mesh_rows,
        "master_object": controller.name,
        "master_key": definition.label,
        "master_property_path": f'["{definition.label}"]',
        "driven_objects": [{"object_name": item["object_name"], "shape_key_name": item["shape_key_name"]} for item in mesh_rows],
    }


def apply_basic_expressions(
    table_path: Path,
    audit_path: Path,
    rig_name: str,
    controller_name: str,
    tolerance: float,
) -> dict[str, object]:
    """Generate source-bound endpoints, bake them, and attach one shared property per control."""
    if not math.isfinite(tolerance) or tolerance <= 0.0:
        raise ValueError(f"Shape-key tolerance must be positive and finite: {tolerance}")
    armature = bpy.data.objects.get(rig_name)
    if armature is None or armature.type != "ARMATURE":
        raise ValueError(f"Explicit facial rig does not exist as an Armature: {rig_name}")
    if armature.data.pose_position != "POSE" or armature.animation_data or armature.constraints or any(bone.constraints for bone in armature.pose.bones):
        raise ValueError(f"Basic expression baking requires a static source rig without Action or constraints: {rig_name}")
    basis_errors = {bone.name: _matrix_error(bone.matrix_basis, IDENTITY) for bone in armature.pose.bones}
    nonidentity = {name: value for name, value in basis_errors.items() if value > tolerance}
    if nonidentity:
        raise ValueError(f"Basic expression baking requires the audited identity pose: rig={rig_name}, residuals={list(nonidentity.items())[:5]}")

    table, table_payload, table_resolved = _read_json(table_path, "expression source table")
    audit, audit_payload, audit_resolved = _read_json(audit_path, "source audit")
    if audit.get("format") != "EndfieldCharacterSourceAudit/1":
        raise ValueError(f"Unsupported source audit format: {audit_resolved}")
    source = _decode_source(table, audit, table_payload, audit_payload, armature,
                            BASIC_EXPRESSION_DEFINITIONS)
    meshes = _selected_rig_meshes(armature)
    from character_authoring_validation import character_invariants, shape_signatures, verify_invariants, verify_preserved_shapes

    mesh_names = tuple(obj.name for obj in meshes)
    before_invariants = character_invariants(mesh_names)
    before_shapes = shape_signatures(mesh_names)
    previous_keys = {
        obj.name: frozenset(key.name for key in obj.data.shape_keys.key_blocks) if obj.data.shape_keys else frozenset()
        for obj in meshes
    }
    try:
        original_mesh_data, copied_mesh_data_rows = _make_mesh_data_single_user(meshes)
        verify_invariants(before_invariants, mesh_names)
        verify_preserved_shapes(before_shapes, mesh_names)
    except Exception:
        _restore_original_mesh_data(meshes, original_mesh_data)
        raise
    present_labels = tuple(definition.label for definition in source.definitions if definition.source_name in source.control_rows)
    controller: bpy.types.Object | None = None
    try:
        unlinked: list[tuple[BasicExpressionDefinition, tuple[int, int, dict[str, object], list[dict[str, object]]], tuple[dict[str, object], ...], tuple[BakedExpression, ...]]] = []
        downstream_modifiers = tuple(
            (modifier, modifier.show_viewport)
            for obj in meshes
            for modifier in obj.modifiers
            if modifier.type != "ARMATURE" and modifier.show_viewport
        )
        try:
            for modifier, _ in downstream_modifiers:
                modifier.show_viewport = False
            bpy.context.view_layer.update()
            mappings: list[tuple[BasicExpressionDefinition, tuple[int, int, dict[str, object], list[dict[str, object]]]]] = []
            for definition in source.definitions:
                mapping_info = _mapping_for_control(source, definition)
                if mapping_info is None:
                    continue
                mappings.append((definition, mapping_info))
            poses = _extract_pose_target_batch(armature, source,
                tuple((definition, mapping_info[3]) for definition, mapping_info in mappings))
            for definition, mapping_info in mappings:
                targets, endpoint_rows = poses[definition.source_name]
                baked_rows = bake_facial_target(armature, meshes, targets, definition.source_name, tolerance)
                unlinked.append((definition, mapping_info, endpoint_rows, tuple(baked_rows)))
        finally:
            for modifier, original_state in downstream_modifiers:
                modifier.show_viewport = original_state
            bpy.context.view_layer.update()

        controller = _create_controller(controller_name, present_labels)
        control_results = []
        for definition in source.definitions:
            mapping_info = _mapping_for_control(source, definition)
            if mapping_info is None:
                control_results.append(_control_report(definition, source, controller, None, (), ()))
                continue
            baked = next(item[3] for item in unlinked if item[0] == definition)
            for baked_mesh in baked:
                obj = bpy.data.objects[baked_mesh.object_name]
                key = obj.data.shape_keys.key_blocks.get(baked_mesh.shape_key_name)
                if key is None:
                    raise ValueError(f"Baked shape key disappeared before driver attachment: {obj.name}/{baked_mesh.shape_key_name}")
                _attach_driver(key, controller, definition.label)
                key.value = 0.0
            endpoint_rows = next(item[2] for item in unlinked if item[0] == definition)
            control_results.append(_control_report(definition, source, controller, mapping_info, endpoint_rows, baked))

        for pose_bone in armature.pose.bones:
            pose_bone.matrix_basis = IDENTITY
        bpy.context.view_layer.update()
        verify_invariants(before_invariants, mesh_names)
        verify_preserved_shapes(before_shapes, mesh_names)
        receipt = {
            "format": "EndfieldBasicExpressionBake/1",
            "status": "basic_source_endpoints_baked_and_synchronized",
            "character_id": source.character_id,
            "source_table": {"file": table_resolved.name, "sha256": source.table_sha256},
            "source_audit": {"file": audit_resolved.name, "sha256": source.audit_sha256},
            "avatar_identity": {"cab": source.rig_data.avatar_identity[0], "path_id": str(source.rig_data.avatar_identity[1])},
            "rig": {
                "object_name": armature.name,
                "bone_count": len(armature.data.bones),
                "matrix_world": _matrix_json(armature.matrix_world),
                "static_identity_pose_max_matrix_error": max(basis_errors.values(), default=0.0),
            },
            "selection": {
                "mesh_object_names": [obj.name for obj in meshes],
                "visible_source_mesh_count": sum(not obj.hide_render and obj.visible_get() for obj in meshes),
                "hidden_explicit_fur_proxy_names": sorted(_visible_fur_proxy_names()),
                "source_character_mesh_helper_used": True,
                "all_meshes_bound_to_explicit_rig": True,
                "isolated_shared_mesh_data": list(copied_mesh_data_rows),
            },
            "source_to_blender_rest_alignment": {
                "binding_count": len(source.rig_data.source_to_blender_rows),
                "position_rule": "(-x,+y,+z), source Unity to imported FBX local",
                "rotation_rule": "(x,y,z,w) -> (x,-y,-z,w), source Unity to imported FBX local",
                "local_transform_path": "Exact Avatar m_ID -> m_TOS path -> source Transform path -> terminal Blender bone name; no fuzzy name lookup",
                "max_base_position_error_meters": max(float(row["base_position_error_meters"]) for row in source.rig_data.source_to_blender_rows),
                "max_base_rotation_dot_error": max(float(row["base_rotation_dot_error"]) for row in source.rig_data.source_to_blender_rows),
                "max_rest_head_error_meters": max(float(row["source_to_blender_rest_head_error_meters"]) for row in source.rig_data.source_to_blender_rows),
                "rows": list(source.rig_data.source_to_blender_rows),
            },
            "controller": {"object_name": controller.name, "neutral_default": 0.0, "properties": list(present_labels)},
            "controls": control_results,
            "preservation": {"original_shape_keys_retained": True, "bone_rest_and_material_invariants_unchanged": True,
                             "native_shape_keys_modified": False, "body_action_modified": False,
                             "unknown_2_3_blendshape_route_used": False},
            "semantics_boundary": {
                "mapping_vectors": "Candidate endpoint reconstruction from static source audit and raw SkeletalMorphMappingData.",
                "runtime_morph_mixing_verified": False,
                "unknown_blendshape_hash_values_routed": False,
                "aliases_are_authoring_labels_only": True,
                "mutual_exclusion": "Metadata notes only; control values are not clamped or made one-hot.",
                "body_animation_action": "Independent armature Action; this module does not create or replace one.",
                "material_emotion": "Independent material controls; this module does not alter them.",
            },
        }
        return receipt
    except Exception:
        for pose_bone in armature.pose.bones:
            pose_bone.matrix_basis = IDENTITY
        bpy.context.view_layer.update()
        _cleanup_new_shapes(meshes, previous_keys)
        _restore_original_mesh_data(meshes, original_mesh_data)
        if controller is not None and bpy.data.objects.get(controller.name) is controller:
            bpy.data.objects.remove(controller, do_unlink=True)
        bpy.context.view_layer.update()
        raise


def validate_basic_expressions(receipt: dict[str, object], tolerance: float) -> dict[str, object]:
    """Check saved driver links at neutral, half, and full values, then restore all controls."""
    if not math.isfinite(tolerance) or tolerance <= 0.0:
        raise ValueError(f"Validation tolerance must be positive and finite: {tolerance}")
    if receipt.get("format") != "EndfieldBasicExpressionBake/1":
        raise ValueError("Unsupported basic expression receipt")
    controller_data = _record(receipt.get("controller"), "receipt.controller")
    controller_name = _text(controller_data.get("object_name"), "receipt.controller.object_name")
    controller = bpy.data.objects.get(controller_name)
    if controller is None:
        raise ValueError(f"Saved expression controller is missing: {controller_name}")
    rows = tuple(_record(value, f"receipt.controls[{index}]") for index, value in enumerate(_array(receipt.get("controls"), "receipt.controls")))
    active = tuple(row for row in rows if row.get("status") == "baked_and_synchronized")
    original_values = {str(row["label"]): float(controller[str(row["label"])]) for row in active}
    controls: list[ExpressionDriverControl] = []
    for row in active:
        label = _text(row.get("label"), "control.label")
        driven = tuple(_record(item, f"{label}.driven_objects[{index}]") for index, item in enumerate(_array(row.get("driven_objects"), f"{label}.driven_objects")))
        keys: list[bpy.types.ShapeKey] = []
        for target in driven:
            object_name = _text(target.get("object_name"), f"{label}.driven.object_name")
            key_name = _text(target.get("shape_key_name"), f"{label}.driven.shape_key_name")
            obj = bpy.data.objects.get(object_name)
            if obj is None or obj.type != "MESH" or obj.data.shape_keys is None:
                raise ValueError(f"Driven mesh or shape keys are missing: {object_name}/{key_name}")
            key = obj.data.shape_keys.key_blocks.get(key_name)
            if key is None:
                raise ValueError(f"Driven shape key is missing: {object_name}/{key_name}")
            keys.append(key)
        controls.append(ExpressionDriverControl(label, tuple(keys)))
    checks = [{"label": check.label, "requested": check.requested,
               "driven_key_count": check.driven_key_count, "maximum_driver_error": check.maximum_driver_error}
              for check in validate_expression_drivers(controller, tuple(controls), tolerance)]
    return {"status": "all_shared_expression_drivers_verified_at_0_0_5_1_and_restored", "controller": controller_name,
            "checked_control_count": len(active), "checks": checks, "controls_restored": original_values}
