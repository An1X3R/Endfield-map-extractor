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
