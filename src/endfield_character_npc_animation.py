"""Derive NPC action candidates from source templates and exact model directories."""
from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
import math
from pathlib import PurePosixPath
import re
from typing import Literal
import zlib

from endfield_character_animation_families import _has_animation_assets, _prefixes_from_reference
from endfield_character_npc_catalog import IndependentNpcRecord
from endfield_character_npc_assembly import NpcSourceObject, _integer, _object, _rows, _text
from endfield_character_sources import CharacterSourceContext


class NpcAnimationBindingError(ValueError):
    """An NPC action lacks an exact CPU-template or selected Avatar relationship."""


@dataclass(frozen=True)
class NpcAnimationPointer:
    cab: str
    path_id: int


@dataclass(frozen=True)
class NpcCpuAction:
    state_name: str
    pointer: NpcAnimationPointer


@dataclass(frozen=True)
class NpcCpuBinding:
    """Keep the CPU config namespace distinct from the clip or Avatar rig type."""
    template: str
    template_namespace: Literal["Humanoid", "Generic"]
    tag_id: int
    avatar: NpcAnimationPointer
    actions: tuple[NpcCpuAction, ...]


NPC_GENERIC_AVATAR_POSE_TOLERANCE = 1e-6


@dataclass(frozen=True)
class NpcAvatarLocalPose:
    translation: tuple[float, float, float]
    rotation: tuple[float, float, float, float]
    scale: tuple[float, float, float]


@dataclass(frozen=True)
class NpcAvatarRigNode:
    path: str
    parent_path: str | None
    default_pose: NpcAvatarLocalPose
    avatar_skeleton_pose: NpcAvatarLocalPose


@dataclass(frozen=True)
class NpcGenericAvatarEquivalence:
    source_avatar_sha256: str
    target_avatar_sha256: str
    clip_count: int
    transform_binding_count: int
    driven_path_count: int
    validated_path_count: int
    target_only_path_count: int
    max_default_pose_delta: float
    max_avatar_skeleton_pose_delta: float

    def to_document(self) -> dict[str, str | int | float]:
        """Return the bounded proof fields consumed by the action catalog."""
        return {
            "format": "NpcGenericAvatarEquivalence/1",
            "source_avatar_sha256": self.source_avatar_sha256,
            "target_avatar_sha256": self.target_avatar_sha256,
            "tolerance": NPC_GENERIC_AVATAR_POSE_TOLERANCE,
            "clip_count": self.clip_count,
            "transform_binding_count": self.transform_binding_count,
            "driven_path_count": self.driven_path_count,
            "validated_path_count": self.validated_path_count,
            "target_only_path_count": self.target_only_path_count,
            "max_default_pose_delta": self.max_default_pose_delta,
            "max_avatar_skeleton_pose_delta": self.max_avatar_skeleton_pose_delta,
        }


def _generic_object(value: object, field: str) -> dict[str, object]:
    if not isinstance(value, dict) or any(not isinstance(key, str) for key in value):
        raise NpcAnimationBindingError(f"Expected source object: field={field}")
    return value


def _generic_rows(value: object, field: str) -> tuple[dict[str, object], ...]:
    if not isinstance(value, list):
        raise NpcAnimationBindingError(f"Expected source array: field={field}")
    return tuple(_generic_object(row, field) for row in value)


def _generic_integer(value: object, field: str) -> int:
    if type(value) is not int:
        raise NpcAnimationBindingError(f"Expected source integer: field={field}, value={value!r}")
    return value


def _generic_text(value: object, field: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise NpcAnimationBindingError(f"Expected nonempty source text: field={field}")
    return value


def _generic_finite_number(value: object, field: str) -> float:
    if type(value) not in (int, float) or not math.isfinite(value):
        raise NpcAnimationBindingError(f"Expected finite source number: field={field}")
    return float(value)


def _generic_local_pose(value: dict[str, object], field: str) -> NpcAvatarLocalPose:
    def vector(name: str, axes: str) -> tuple[float, ...]:
        row = _generic_object(value.get(name), f"{field}.{name}")
        return tuple(_generic_finite_number(row.get(axis), f"{field}.{name}.{axis}") for axis in axes)

    translation = vector("t", "XYZ")
    rotation = vector("q", "XYZW")
    scale = vector("s", "XYZ")
    return NpcAvatarLocalPose(
        (translation[0], translation[1], translation[2]),
        (rotation[0], rotation[1], rotation[2], rotation[3]),
        (scale[0], scale[1], scale[2]),
    )


def _generic_avatar_rig(avatar: NpcSourceObject, role: str) -> tuple[dict[str, NpcAvatarRigNode], str]:
    if avatar.metadata.get("type") != "Avatar":
        raise NpcAnimationBindingError(f"{role} source must resolve to an actual Avatar")
    digest = _generic_text(avatar.metadata.get("rawDataSha256"), f"{role}.rawDataSha256").casefold()
    if len(digest) != 64 or any(character not in "0123456789abcdef" for character in digest):
        raise NpcAnimationBindingError(f"{role} Avatar has an invalid source SHA-256")
    data = _generic_object(avatar.value.get("m_Avatar"), f"{role}.m_Avatar")
    skeleton = _generic_object(data.get("m_AvatarSkeleton"), f"{role}.m_AvatarSkeleton")
    raw_ids = skeleton.get("m_ID")
    if not isinstance(raw_ids, list):
        raise NpcAnimationBindingError(f"{role} Avatar skeleton IDs are absent")
    ids = tuple(_generic_integer(value, f"{role}.m_ID[{index}]") for index, value in enumerate(raw_ids))
    nodes = _generic_rows(skeleton.get("m_Node"), f"{role}.m_Node")
    default_poses = _generic_rows(_generic_object(data.get("m_DefaultPose"), f"{role}.m_DefaultPose").get("m_X"),
                                  f"{role}.m_DefaultPose.m_X")
    skeleton_poses = _generic_rows(_generic_object(data.get("m_AvatarSkeletonPose"), f"{role}.m_AvatarSkeletonPose").get("m_X"),
                                   f"{role}.m_AvatarSkeletonPose.m_X")
    tos = _generic_object(avatar.value.get("m_TOS"), f"{role}.m_TOS")
    if not ids or len(set(ids)) != len(ids) or len(ids) != len(nodes) or len(nodes) != len(default_poses) or len(nodes) != len(skeleton_poses):
        raise NpcAnimationBindingError(f"{role} Avatar skeleton, path IDs and local poses disagree")

    paths: list[str] = []
    parent_indices: list[int] = []
    for index, (path_id, node) in enumerate(zip(ids, nodes, strict=True)):
        key = str(path_id)
        raw_path = tos.get(key)
        if path_id == 0 and raw_path in (None, ""):
            path = ""
        else:
            path = _generic_text(raw_path, f"{role}.m_TOS[{key}]")
        if path and (path.startswith("/") or path.endswith("/") or "//" in path
                     or any(component in (".", "..") for component in path.split("/"))):
            raise NpcAnimationBindingError(f"{role} Avatar has a malformed transform path: path={path!r}")
        paths.append(path)
        parent_indices.append(_generic_integer(node.get("m_ParentId"), f"{role}.m_Node[{index}].m_ParentId"))
    if len(set(paths)) != len(paths) or paths.count("") != 1:
        raise NpcAnimationBindingError(f"{role} Avatar transform paths are duplicated or lack one root path")

    result: dict[str, NpcAvatarRigNode] = {}
    root_count = 0
    for index, path in enumerate(paths):
        parent_index = parent_indices[index]
        if parent_index == -1:
            parent_path: str | None = None
            root_count += 1
            if path != "":
                raise NpcAnimationBindingError(f"{role} Avatar root path is not the empty transform path")
        else:
            if parent_index < 0 or parent_index >= len(paths) or parent_index == index:
                raise NpcAnimationBindingError(f"{role} Avatar has an invalid parent index: path={path!r}")
            parent_path = paths[parent_index]
            if path.rpartition("/")[0] != parent_path or not path:
                raise NpcAnimationBindingError(f"{role} Avatar path and parent disagree: path={path!r}, parent={parent_path!r}")
        result[path] = NpcAvatarRigNode(path, parent_path,
            _generic_local_pose(default_poses[index], f"{role}.defaultPose[{path}]"),
            _generic_local_pose(skeleton_poses[index], f"{role}.avatarSkeletonPose[{path}]"))
    if root_count != 1:
        raise NpcAnimationBindingError(f"{role} Avatar has an invalid root count: count={root_count}")
    return result, digest


def _generic_clip_transform_paths(clips: tuple[NpcSourceObject, ...], avatar_paths: frozenset[str],
                                  source_tos: dict[str, str], target_tos: dict[str, str]) -> tuple[frozenset[str], int]:
    driven_paths: set[str] = set()
    binding_count = 0
    if not clips:
        raise NpcAnimationBindingError("Generic Avatar equivalence requires exact selected AnimationClip sources")
    for clip_index, clip in enumerate(clips):
        if clip.metadata.get("type") != "AnimationClip":
            raise NpcAnimationBindingError(f"Generic source clip is not an AnimationClip: index={clip_index}")
        digest = _generic_text(clip.metadata.get("rawDataSha256"), f"clip[{clip_index}].rawDataSha256").casefold()
        if len(digest) != 64 or any(character not in "0123456789abcdef" for character in digest):
            raise NpcAnimationBindingError(f"Generic source clip has an invalid source SHA-256: index={clip_index}")
        clip_data = _generic_object(clip.value.get("m_ClipBindingConstant"), f"clip[{clip_index}].m_ClipBindingConstant")
        rows = _generic_rows(clip_data.get("genericBindings"), f"clip[{clip_index}].genericBindings")
        clip_paths: set[str] = set()
        for row_index, binding in enumerate(rows):
            type_id = _generic_text(binding.get("typeID"), f"clip[{clip_index}].genericBindings[{row_index}].typeID")
            if type_id != "Transform":
                continue
            path_id = _generic_integer(binding.get("path"), f"clip[{clip_index}].genericBindings[{row_index}].path")
            if not 0 <= path_id <= 0xffffffff:
                raise NpcAnimationBindingError(f"Generic Transform binding path hash is outside uint32: clip={clip_index}, row={row_index}")
            key = str(path_id)
            source_path = source_tos.get(key)
            target_path = target_tos.get(key)
            if source_path is None or target_path is None or source_path != target_path:
                raise NpcAnimationBindingError(
                    f"Generic Transform binding hash does not resolve to one exact path in both Avatars: clip={clip_index}, row={row_index}, path_hash={path_id}")
            if source_path not in avatar_paths:
                raise NpcAnimationBindingError(f"Generic Transform binding path is absent from the source rig: path={source_path!r}")
            clip_paths.add(source_path)
            binding_count += 1
        if not clip_paths:
            raise NpcAnimationBindingError(f"Generic AnimationClip has no Transform path bindings: index={clip_index}")
        driven_paths.update(clip_paths)
    return frozenset(driven_paths), binding_count


def _generic_pose_delta(source: NpcAvatarLocalPose, target: NpcAvatarLocalPose) -> float:
    translation_delta = max(abs(left - right) for left, right in zip(source.translation, target.translation, strict=True))
    scale_delta = max(abs(left - right) for left, right in zip(source.scale, target.scale, strict=True))
    direct_rotation_delta = max(abs(left - right) for left, right in zip(source.rotation, target.rotation, strict=True))
    opposite_rotation_delta = max(abs(left + right) for left, right in zip(source.rotation, target.rotation, strict=True))
    rotation_delta = min(direct_rotation_delta, opposite_rotation_delta)
    return max(translation_delta, scale_delta, rotation_delta)


def validate_npc_generic_target(source_avatar: NpcSourceObject, target_avatar: NpcSourceObject,
                                clips: tuple[NpcSourceObject, ...]) -> NpcGenericAvatarEquivalence:
    """Admit Generic cross-Avatar clips only when driven paths and ancestors share source poses."""
    source_nodes, source_digest = _generic_avatar_rig(source_avatar, "CPU")
    target_nodes, target_digest = _generic_avatar_rig(target_avatar, "target")
    source_paths = frozenset(source_nodes)
    target_paths = frozenset(target_nodes)
    if not source_paths <= target_paths:
        missing_target = tuple(sorted(source_paths - target_paths)[:8])
        raise NpcAnimationBindingError(
            f"Generic target Avatar lacks source transform paths: source_only={missing_target}")
    # Extra target accessories retain their local rest poses under the matched parents.
    parent_mismatches = tuple(path for path in sorted(source_paths)
        if source_nodes[path].parent_path != target_nodes[path].parent_path)
    if parent_mismatches:
        raise NpcAnimationBindingError(f"Generic Avatar parent paths differ: paths={parent_mismatches[:8]}")

    source_tos = _generic_object(source_avatar.value.get("m_TOS"), "CPU.m_TOS")
    target_tos = _generic_object(target_avatar.value.get("m_TOS"), "target.m_TOS")
    source_path_ids = {key: ("" if key == "0" and value == "" else _generic_text(value, f"CPU.m_TOS[{key}]"))
                       for key, value in source_tos.items()}
    target_path_ids = {key: ("" if key == "0" and value == "" else _generic_text(value, f"target.m_TOS[{key}]"))
                       for key, value in target_tos.items()}
    source_path_ids.setdefault("0", "")
    target_path_ids.setdefault("0", "")
    driven_paths, binding_count = _generic_clip_transform_paths(clips, source_paths, source_path_ids, target_path_ids)
    validated_paths: set[str] = set()
    for driven_path in driven_paths:
        path: str | None = driven_path
        while path is not None:
            validated_paths.add(path)
            path = source_nodes[path].parent_path

    maximum_default_delta = 0.0
    maximum_skeleton_delta = 0.0
    for path in sorted(validated_paths):
        source_node = source_nodes[path]
        target_node = target_nodes[path]
        for pose_name, source_pose, target_pose in (
            ("default pose", source_node.default_pose, target_node.default_pose),
            ("Avatar skeleton pose", source_node.avatar_skeleton_pose, target_node.avatar_skeleton_pose),
        ):
            delta = _generic_pose_delta(source_pose, target_pose)
            if pose_name == "default pose":
                maximum_default_delta = max(maximum_default_delta, delta)
            else:
                maximum_skeleton_delta = max(maximum_skeleton_delta, delta)
            if delta > NPC_GENERIC_AVATAR_POSE_TOLERANCE:
                raise NpcAnimationBindingError(
                    f"Generic Avatar {pose_name} differs on a driven path or ancestor: path={path!r}, max_delta={delta:.9g}, tolerance={NPC_GENERIC_AVATAR_POSE_TOLERANCE}")
    return NpcGenericAvatarEquivalence(source_digest, target_digest, len(clips), binding_count,
        len(driven_paths), len(validated_paths), len(target_paths - source_paths), maximum_default_delta, maximum_skeleton_delta)


def _cpu_animation_template(template: str) -> tuple[Literal["Humanoid", "Generic"], str]:
    humanoid = re.fullmatch(r"NPC/AnimationConfig/Humanoid/([A-Za-z0-9_]+)/([A-Za-z0-9_]+)", template)
    if humanoid is not None:
        family, actor = humanoid.groups()
        return "Humanoid", f"assets/beyond/dynamicassets/gameplay/npc/animation/humanoid/{family}/data_npc_cpuAnimData_{actor}.asset".casefold()
    generic = re.fullmatch(r"NPC/AnimationConfig/Generic/([A-Za-z0-9_]+)", template)
    if generic is not None:
        actor = generic.group(1)
        return "Generic", f"assets/beyond/dynamicassets/gameplay/npc/animation/generic/data_npc_cpuAnimData_{actor}.asset".casefold()
    raise NpcAnimationBindingError("NPC CPU source requires an exact Humanoid/<family>/<actor> or Generic/<actor> template")


def npc_cpu_animation_resource_path(template: str) -> str:
    """Locate a candidate source asset; its serialized tag and Avatar still require verification."""
    return _cpu_animation_template(template)[1]


def validate_npc_humanoid_target(source_avatar: NpcSourceObject, target_avatar: NpcSourceObject) -> None:
    """Validate separate source and target Human rigs before selected-clip binding."""
    indices: list[tuple[int, ...]] = []
    for role, avatar in (("CPU", source_avatar), ("target", target_avatar)):
        if avatar.metadata.get("type") != "Avatar":
            raise NpcAnimationBindingError(f"{role} source must resolve to an actual Avatar")
        human = _object(_object(avatar.value.get("m_Avatar"), "m_Avatar").get("m_Human"), "m_Human")
        skeleton = _object(human.get("m_Skeleton"), "Human skeleton")
        nodes = _rows(skeleton.get("m_Node"), "Human nodes")
        poses = _rows(_object(human.get("m_SkeletonPose"), "Human pose").get("m_X"), "Human poses")
        raw = human.get("m_HumanBoneIndex")
        if not isinstance(raw, list) or len(raw) != 25 or len(nodes) != len(poses) or not nodes:
            raise NpcAnimationBindingError(f"{role} Avatar has no supported Human mapping")
        mapped = tuple(_integer(value, "Human bone index") for value in raw)
        if any(index < -1 or index >= len(nodes) for index in mapped) or mapped[0] < 0:
            raise NpcAnimationBindingError(f"{role} Human mapping has an invalid bone index or no hips")
        indices.append(mapped)
    missing = tuple(slot for slot, (source, target) in enumerate(zip(*indices, strict=True)) if source >= 0 and target < 0)
    if missing:
        raise NpcAnimationBindingError(f"Target Avatar lacks source Human slots: slots={missing}")


def _source_pointer(source: NpcSourceObject, value: object, field: str) -> NpcAnimationPointer:
    pointer = _object(value, field)
    file_id = _integer(pointer.get("m_FileID"), field + ".m_FileID")
    path_id = _integer(pointer.get("m_PathID"), field + ".m_PathID")
    matches = tuple(row for row in _rows(source.metadata.get("pptrReferences"), "pptrReferences")
                    if row.get("path") == field and row.get("fileId") == file_id and row.get("pathId") == path_id)
    if file_id < 0 or path_id == 0 or len(matches) != 1:
        raise NpcAnimationBindingError(f"CPU source pointer is null, absent or ambiguous: field={field}")
    cab = source.metadata.get("sourceFile") if file_id == 0 else matches[0].get("expectedTargetSourceFile")
    return NpcAnimationPointer(_text(cab, field + ".cab"), path_id)


def bind_npc_cpu_actions(template: str, source: NpcSourceObject,
                         selected_avatar: NpcAnimationPointer) -> NpcCpuBinding:
    """Bind source pointers without treating the config namespace as the clip rig type."""
    template_namespace, resource_path = _cpu_animation_template(template)
    if source.metadata.get("type") != "MonoBehaviour" or str(source.metadata.get("container")).casefold() != resource_path:
        raise NpcAnimationBindingError("CPU source container does not match the selected template candidate")
    tag_id = _integer(_object(source.value.get("configTag"), "configTag").get("tagId"), "configTag.tagId")
    expected = zlib.crc32(template.encode("utf-8"))
    if tag_id & 0xffffffff != expected:
        raise NpcAnimationBindingError(f"CPU template GameplayTag differs from the exact PrefabInfo reference: expected={expected}, actual={tag_id}")
    avatar = _source_pointer(source, source.value.get("avatar"), "$.avatar")
    if avatar.cab.casefold() != selected_avatar.cab.casefold() or avatar.path_id != selected_avatar.path_id:
        raise NpcAnimationBindingError("CPU Avatar differs from AvatarTemplet.sizeAvatar; cross-Avatar retargeting is not verified")
    actions = tuple(NpcCpuAction(_text(row.get("stateName"), "stateName"),
        _source_pointer(source, row.get("clip"), f"$.allBaseClips[{index}].clip"))
        for index, row in enumerate(_rows(source.value.get("allBaseClips"), "allBaseClips")))
    if not actions or len({action.state_name for action in actions}) != len(actions):
        raise NpcAnimationBindingError("CPU base actions are empty or have duplicate state names")
    return NpcCpuBinding(template, template_namespace, tag_id, avatar, actions)


def npc_avatar_source_transforms(avatar: NpcSourceObject, root_path: str) -> tuple[dict[str, object], ...]:
    """Expose actual Avatar default locals, without fabricating GameObject or Animator rows."""
    if avatar.metadata.get("type") != "Avatar" or not root_path or "/" in root_path:
        raise NpcAnimationBindingError("NPC hierarchy requires an actual Avatar and one explicit export root")
    data = _object(avatar.value.get("m_Avatar"), "m_Avatar")
    skeleton = _object(data.get("m_AvatarSkeleton"), "m_AvatarSkeleton")
    ids = skeleton.get("m_ID")
    nodes = _rows(skeleton.get("m_Node"), "m_Node")
    poses = _rows(_object(data.get("m_DefaultPose"), "m_DefaultPose").get("m_X"), "m_X")
    tos = _object(avatar.value.get("m_TOS"), "m_TOS")
    if not isinstance(ids, list) or len(ids) != len(nodes) or len(nodes) != len(poses) or not nodes:
        raise NpcAnimationBindingError("Avatar skeleton and default-pose dimensions disagree")
    paths: list[str] = []
    result: list[dict[str, object]] = []
    for index, (raw_id, node, pose) in enumerate(zip(ids, nodes, poses, strict=True)):
        key = str(_integer(raw_id, "Avatar path ID"))
        path = "" if index == 0 and key == "0" else _text(tos.get(key), "Avatar TOS path")
        parent = _integer(node.get("m_ParentId"), "Avatar parent")
        if (index == 0 and (path != "" or parent != -1)) or (index > 0 and (parent < 0 or parent >= index or path.rpartition("/")[0] != paths[parent])):
            raise NpcAnimationBindingError(f"Avatar path and source parent disagree: index={index}, path={path}")
        paths.append(path)
        transform: dict[str, object] = {"path": root_path + ("/" + path if path else "")}
        for source_field, target_field, axes in (("t", "position", "XYZ"), ("q", "rotation", "XYZW"), ("s", "scale", "XYZ")):
            vector = _object(pose.get(source_field), source_field)
            values = tuple(vector.get(axis) for axis in axes)
            if any(type(value) not in (int, float) or not math.isfinite(value) for value in values):
                raise NpcAnimationBindingError(f"Avatar default pose is nonfinite: node={index}, field={source_field}")
            transform[target_field] = list(values)
        result.append(transform)
    if len(set(paths)) != len(paths):
        raise NpcAnimationBindingError("Avatar source paths are duplicated")
    return tuple(result)


def resolve_npc_animation_prefixes(context: CharacterSourceContext, npc: IndependentNpcRecord,
                                   model_resource_paths: tuple[str, ...],
                                   check_cancelled: Callable[[], None]) -> tuple[str, ...]:
    """Return existing action directories; these candidates do not prove Avatar binding."""
    reference = npc.resource_reference.cpu_animation_template
    candidates: set[str] = set()
    if reference is not None and reference.replace("\\", "/").casefold().startswith("npc/animationconfig/humanoid/"):
        candidates.update(_prefixes_from_reference(reference))
    for resource_path in model_resource_paths:
        container = resource_path.replace("\\", "/").casefold().partition("##")[0]
        path = PurePosixPath(container)
        if (container.startswith("assets/beyond/arts/entity/") and path.suffix in (".fbx", ".asset")
                and path.parent.name in ("models", "meshes")):
            candidates.add(str(path.parent.parent / "animations") + "/")
    return tuple(prefix for prefix in sorted(candidates)
                 if _has_animation_assets(context.resource_database, prefix, check_cancelled))
