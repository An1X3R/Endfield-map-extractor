"""Identify exact Avatar-bound axial wrist helper routes from source data."""
from __future__ import annotations

from dataclasses import dataclass
import math
from typing import Literal, cast

from character_animation_goals import _dot, _inverse, _mul, _normalize, _qmul, _sub

Vec3 = tuple[float, float, float]
Quat = tuple[float, float, float, float]
RouteStatus = Literal['identified', 'ambiguous', 'not_applicable']

FLOAT32_EPSILON = 2.0 ** -23
SERIALIZATION_ULPS = 8.0


@dataclass(frozen=True)
class WristAuxiliaryRoute:
    name: str
    upper: int
    middle: int
    end: int
    proximal: int
    distal: int
    proximal_rotation_track: int
    distal_rotation_track: int
    axis: Vec3
    rest_proximal: Quat
    rest_distal: Quat
    rest_hand: Quat
    rest_compound: Quat
    rest_proximal_phase_degrees: float
    rest_distal_phase_degrees: float
    rest_compound_phase_degrees: float
    rest_child_position_fraction: float
    rest_child_perpendicular_m: float
    position_serialization_bound_m: float
    trajectory_frame_count: int
    maximum_proximal_swing_degrees: float
    maximum_distal_swing_degrees: float
    maximum_proximal_center_offset_m: float
    maximum_distal_off_axis_m: float
    maximum_proximal_scale_error: float
    maximum_distal_scale_error: float
    distal_along_range_m: tuple[float, float]


@dataclass(frozen=True)
class WristRouteResolution:
    forearm_avatar_slot: int
    hand_avatar_slot: int
    status: RouteStatus
    candidates: tuple[WristAuxiliaryRoute, ...]
    reason: str
    rejected_candidates: tuple[str, ...]


@dataclass(frozen=True)
class WristRouteScan:
    hands: tuple[WristRouteResolution, ...]


@dataclass(frozen=True)
class _HierarchyNode:
    index: int
    path: str
    parent_index: int
    position: Vec3
    rotation: Quat
    scale: Vec3


@dataclass(frozen=True)
class _RotationBinding:
    track: int
    has_rotation: bool


def _length(vector: Vec3) -> float:
    return math.hypot(*vector)


def _unit(vector: Vec3, context: str) -> Vec3:
    magnitude = _length(vector)
    if magnitude == 0.0:
        raise ValueError('Cannot normalize zero vector: ' + context)
    return cast(Vec3, tuple(component / magnitude for component in vector))


def _axial_swing_residual(value: Quat, axis: Vec3) -> float:
    normalized = _normalize(value)
    vector = cast(Vec3, normalized[:3])
    parallel = _mul(axis, _dot(vector, axis))
    return _length(_sub(vector, parallel))


def _signed_axial_phase_degrees(value: Quat, axis: Vec3) -> float:
    normalized = _normalize(value)
    vector = cast(Vec3, normalized[:3])
    angle = math.degrees(2.0 * math.atan2(_dot(vector, axis), normalized[3]))
    if angle > 180.0:
        angle -= 360.0
    elif angle <= -180.0:
        angle += 360.0
    return angle


def _identity_scale(scale: Vec3) -> bool:
    tolerance = SERIALIZATION_ULPS * FLOAT32_EPSILON
    return all(abs(component - 1.0) <= tolerance for component in scale)


def _deduplicate_reasons(reasons: list[str]) -> tuple[str, ...]:
    return tuple(dict.fromkeys(reasons))


def identify_wrist_auxiliary_routes(
    avatar_value: object,
    hierarchy_value: object,
    native_bindings_value: object,
    source_local_frames_value: object,
    animator_root_path: str,
) -> WristRouteScan:
    """Return uniquely identified two-level axial helpers for Avatar hand slots."""
    from character_animation_goal_export import (
        array_value,
        field_value,
        integer_value,
        number_value,
        object_value,
        quaternion_xyzw,
        text_value,
    )

    def vector3(value: object, context: str) -> Vec3:
        components = array_value(value, context)
        if len(components) != 3:
            raise ValueError('Expected three vector components: ' + context)
        return cast(Vec3, tuple(number_value(component, context) for component in components))

    avatar = object_value(avatar_value, 'avatar parsed')
    avatar_fields = object_value(field_value(avatar, 'm_Avatar', 'avatar parsed'), 'avatar parsed.m_Avatar')
    human = object_value(field_value(avatar_fields, 'm_Human', 'avatar parsed.m_Avatar'), 'Avatar Human')
    skeleton = object_value(field_value(human, 'm_Skeleton', 'Avatar Human'), 'Avatar Human.m_Skeleton')
    skeleton_nodes = array_value(field_value(skeleton, 'm_Node', 'Avatar Human.m_Skeleton'), 'Avatar skeleton nodes')
    skeleton_ids = array_value(field_value(skeleton, 'm_ID', 'Avatar Human.m_Skeleton'), 'Avatar skeleton IDs')
    human_indices = array_value(field_value(human, 'm_HumanBoneIndex', 'Avatar Human'), 'Avatar HumanBoneIndex')
    tos = object_value(field_value(avatar, 'm_TOS', 'avatar parsed'), 'avatar parsed.m_TOS')
    if len(skeleton_nodes) != len(skeleton_ids):
        raise ValueError('Avatar skeleton node and ID arrays have different lengths')
    if len(human_indices) < 25:
        raise ValueError('Avatar HumanBoneIndex has fewer than 25 Humanoid slots')
    if not animator_root_path:
        raise ValueError('Animator root path must be nonempty')

    hierarchy_raw = array_value(hierarchy_value, 'source hierarchy')
    if not hierarchy_raw:
        raise ValueError('Source hierarchy is empty')
    nodes: list[_HierarchyNode] = []
    path_to_index: dict[str, int] = {}
    for index, raw_node in enumerate(hierarchy_raw):
        context = 'source hierarchy node ' + str(index)
        record = object_value(raw_node, context)
        stored_index = integer_value(field_value(record, 'index', context), context + '.index')
        path = text_value(field_value(record, 'path', context), context + '.path')
        parent_index = integer_value(field_value(record, 'parent_index', context), context + '.parent_index')
        if stored_index != index:
            raise ValueError('Source hierarchy index disagrees with array order: ' + path)
        if parent_index < -1 or parent_index >= len(hierarchy_raw) or parent_index == index:
            raise ValueError('Source hierarchy has invalid parent index at ' + path)
        if path in path_to_index:
            raise ValueError('Duplicate source hierarchy path: ' + path)
        rest_local = object_value(field_value(record, 'rest_local', context), context + '.rest_local')
        position = vector3(field_value(rest_local, 'position', context + '.rest_local'), context + '.rest_local.position')
        rotation = cast(Quat, quaternion_xyzw(field_value(rest_local, 'rotation', context + '.rest_local'), context + '.rest_local.rotation'))
        scale = vector3(field_value(rest_local, 'scale', context + '.rest_local'), context + '.rest_local.scale')
        nodes.append(_HierarchyNode(index, path, parent_index, position, rotation, scale))
        path_to_index[path] = index

    def exact_avatar_source_index(slot: int) -> int | None:
        raw_node_index = integer_value(human_indices[slot], 'Avatar HumanBoneIndex[' + str(slot) + ']')
        if raw_node_index == -1:
            return None
        if raw_node_index < -1 or raw_node_index >= len(skeleton_nodes):
            raise ValueError('Avatar HumanBoneIndex is out of range for slot ' + str(slot))
        bone_id = integer_value(skeleton_ids[raw_node_index], 'Avatar skeleton ID ' + str(raw_node_index))
        avatar_path = text_value(field_value(tos, str(bone_id), 'Avatar TOS'), 'Avatar TOS[' + str(bone_id) + ']')
        full_path = animator_root_path.rstrip('/') + '/' + avatar_path
        if full_path not in path_to_index:
            raise ValueError('Exact Avatar path is absent from source hierarchy: ' + full_path)
        return path_to_index[full_path]

    human_source_nodes: set[int] = set()
    slot_source_indices: dict[int, int | None] = {}
    for slot in range(25):
        source_index = exact_avatar_source_index(slot)
        slot_source_indices[slot] = source_index
        if source_index is not None:
            human_source_nodes.add(source_index)

    binding_rows = array_value(native_bindings_value, 'native bound tracks')
    rotation_bindings: dict[str, _RotationBinding] = {}
    for index, raw_binding in enumerate(binding_rows):
        context = 'native bound track ' + str(index)
        binding = object_value(raw_binding, context)
        source_path = text_value(field_value(binding, 'source_transform_path', context), context + '.source_transform_path')
        track = integer_value(field_value(binding, 'track', context), context + '.track')
        has_rotation = field_value(binding, 'has_rotation', context)
        if type(has_rotation) is not bool:
            raise TypeError('Expected boolean: ' + context + '.has_rotation')
        if track < 0:
            raise ValueError('Native bound track index is negative: ' + context)
        if source_path in rotation_bindings:
            raise ValueError('Duplicate native binding for source transform path: ' + source_path)
        rotation_bindings[source_path] = _RotationBinding(track, has_rotation)

    frames_raw = array_value(source_local_frames_value, 'source local frames')
    if not frames_raw:
        raise ValueError('Cannot validate auxiliary rotation trajectory with no source frames')
    frame_locals: list[list[object]] = []
    prior_time: float | None = None
    for frame_index, raw_frame in enumerate(frames_raw):
        context = 'source local frame ' + str(frame_index)
        frame = object_value(raw_frame, context)
        frame_time = number_value(field_value(frame, 'time', context), context + '.time')
        if prior_time is not None and frame_time <= prior_time:
            raise ValueError('Source frame times are duplicate or decreasing at frame ' + str(frame_index))
        prior_time = frame_time
        locals_value = array_value(field_value(frame, 'locals', context), context + '.locals')
        if len(locals_value) != len(nodes):
            raise ValueError('Source local count disagrees with hierarchy at frame ' + str(frame_index))
        frame_locals.append(locals_value)

    pair_specs = (('LeftHand', 14, 16, 18), ('RightHand', 15, 17, 19))
    resolutions: list[WristRouteResolution] = []
    for name, upper_slot, forearm_slot, hand_slot in pair_specs:
        upper_index = slot_source_indices[upper_slot]
        forearm_index = slot_source_indices[forearm_slot]
        hand_index = slot_source_indices[hand_slot]
        if upper_index is None or forearm_index is None or hand_index is None:
            missing_slots = tuple(slot for slot, value in ((upper_slot, upper_index), (forearm_slot, forearm_index), (hand_slot, hand_index)) if value is None)
            resolutions.append(WristRouteResolution(forearm_slot, hand_slot, 'not_applicable', (),
                                                     'Avatar hand chain is missing mapped Humanoid slots: ' + ', '.join(map(str, missing_slots)), ()))
            continue
        if nodes[hand_index].parent_index != forearm_index:
            resolutions.append(WristRouteResolution(forearm_slot, hand_slot, 'not_applicable', (),
                                                     'Avatar-mapped hand is not a direct forearm child in the source hierarchy', ()))
            continue

        hand_position = nodes[hand_index].position
        forearm_hand_length = _length(hand_position)
        if forearm_hand_length == 0.0:
            resolutions.append(WristRouteResolution(forearm_slot, hand_slot, 'not_applicable', (),
                                                     'Avatar forearm-to-hand rest offset has zero length', ()))
            continue
        axis = _unit(hand_position, name + ' rest forearm-to-hand axis')
        position_bound = SERIALIZATION_ULPS * FLOAT32_EPSILON * forearm_hand_length
        candidates: list[WristAuxiliaryRoute] = []
        rejected: list[str] = []
        saw_bound_pair = False

        for proximal in nodes:
            if proximal.parent_index != forearm_index or proximal.index in human_source_nodes:
                continue
            proximal_binding = rotation_bindings.get(proximal.path)
            if proximal_binding is None or not proximal_binding.has_rotation:
                continue
            for distal in nodes:
                if distal.parent_index != proximal.index or distal.index in human_source_nodes:
                    continue
                distal_binding = rotation_bindings.get(distal.path)
                if distal_binding is None or not distal_binding.has_rotation:
                    continue
                saw_bound_pair = True
                prefix = 'source indices ' + str(proximal.index) + '/' + str(distal.index) + ': '
                if proximal_binding.track == distal_binding.track:
                    rejected.append(prefix + 'proximal and distal helpers share one native track index')
                    continue
                if proximal.position != (0.0, 0.0, 0.0):
                    rejected.append(prefix + 'proximal helper rest origin is not zero')
                    continue
                if not _identity_scale(proximal.scale) or not _identity_scale(distal.scale):
                    rejected.append(prefix + 'helper rest scale is not identity within eight float32 ULPs')
                    continue

                proximal_rest_swing = _axial_swing_residual(proximal.rotation, axis)
                distal_rest_swing = _axial_swing_residual(distal.rotation, axis)
                quaternion_bound = SERIALIZATION_ULPS * FLOAT32_EPSILON
                if proximal_rest_swing > quaternion_bound:
                    rejected.append(prefix + 'proximal rest rotation swings the forearm axis')
                    continue
                if distal_rest_swing > quaternion_bound:
                    rejected.append(prefix + 'distal rest rotation swings the forearm axis')
                    continue

                child_along = _dot(distal.position, axis)
                child_perpendicular = _length(_sub(distal.position, _mul(axis, child_along)))
                if not 0.0 < child_along < forearm_hand_length:
                    rejected.append(prefix + 'distal helper rest origin is not inside the forearm-to-hand segment')
                    continue
                if child_perpendicular > position_bound:
                    rejected.append(prefix + 'distal helper rest origin is nonaxial')
                    continue

                maximum_proximal_swing = 0.0
                maximum_distal_swing = 0.0
                maximum_proximal_center = 0.0
                maximum_distal_off_axis = 0.0
                maximum_proximal_scale_error = 0.0
                maximum_distal_scale_error = 0.0
                minimum_distal_along = math.inf
                maximum_distal_along = -math.inf
                trajectory_failure: str | None = None
                for frame_index, locals_value in enumerate(frame_locals):
                    proximal_local = object_value(locals_value[proximal.index], 'frame proximal helper')
                    distal_local = object_value(locals_value[distal.index], 'frame distal helper')
                    proximal_position = vector3(field_value(proximal_local, 'position', 'frame proximal helper'), 'frame proximal position')
                    distal_position = vector3(field_value(distal_local, 'position', 'frame distal helper'), 'frame distal helper')
                    proximal_rotation = cast(Quat, quaternion_xyzw(field_value(proximal_local, 'rotation', 'frame proximal helper'), 'frame proximal rotation'))
                    distal_rotation = cast(Quat, quaternion_xyzw(field_value(distal_local, 'rotation', 'frame distal helper'), 'frame distal helper rotation'))
                    proximal_scale = vector3(field_value(proximal_local, 'scale', 'frame proximal helper'), 'frame proximal scale')
                    distal_scale = vector3(field_value(distal_local, 'scale', 'frame distal helper'), 'frame distal helper scale')
                    proximal_scale_error = max(abs(component - 1.0) for component in proximal_scale)
                    distal_scale_error = max(abs(component - 1.0) for component in distal_scale)
                    proximal_delta = _qmul(_inverse(proximal.rotation), proximal_rotation)
                    distal_delta = _qmul(_inverse(distal.rotation), distal_rotation)
                    proximal_swing = _axial_swing_residual(proximal_delta, axis)
                    distal_swing = _axial_swing_residual(distal_delta, axis)
                    proximal_center = _length(proximal_position)
                    distal_along = _dot(distal_position, axis)
                    distal_off_axis = _length(_sub(distal_position, _mul(axis, distal_along)))
                    maximum_proximal_swing = max(maximum_proximal_swing, proximal_swing)
                    maximum_distal_swing = max(maximum_distal_swing, distal_swing)
                    maximum_proximal_center = max(maximum_proximal_center, proximal_center)
                    maximum_distal_off_axis = max(maximum_distal_off_axis, distal_off_axis)
                    maximum_proximal_scale_error = max(maximum_proximal_scale_error, proximal_scale_error)
                    maximum_distal_scale_error = max(maximum_distal_scale_error, distal_scale_error)
                    minimum_distal_along = min(minimum_distal_along, distal_along)
                    maximum_distal_along = max(maximum_distal_along, distal_along)
                    if trajectory_failure is None and proximal_swing > quaternion_bound:
                        trajectory_failure = 'proximal rotation delta swings off axis at frame ' + str(frame_index)
                    elif trajectory_failure is None and distal_swing > quaternion_bound:
                        trajectory_failure = 'distal rotation delta swings off axis at frame ' + str(frame_index)
                    elif trajectory_failure is None and proximal_scale_error > quaternion_bound:
                        trajectory_failure = 'proximal helper scale is nonunit at frame ' + str(frame_index)
                    elif trajectory_failure is None and distal_scale_error > quaternion_bound:
                        trajectory_failure = 'distal helper scale is nonunit at frame ' + str(frame_index)
                    elif trajectory_failure is None and proximal_center > position_bound:
                        trajectory_failure = 'proximal helper center leaves its zero origin at frame ' + str(frame_index)
                    elif trajectory_failure is None and distal_off_axis > position_bound:
                        trajectory_failure = 'distal helper origin leaves the axial frame at frame ' + str(frame_index)
                    elif trajectory_failure is None and not 0.0 < distal_along < forearm_hand_length:
                        trajectory_failure = 'distal helper origin leaves the forearm-to-hand segment at frame ' + str(frame_index)

                if trajectory_failure is not None:
                    rejected.append(prefix + trajectory_failure)
                    continue

                rest_compound = _qmul(proximal.rotation, distal.rotation)
                candidates.append(WristAuxiliaryRoute(
                    name=name,
                    upper=upper_index,
                    middle=forearm_index,
                    end=hand_index,
                    proximal=proximal.index,
                    distal=distal.index,
                    proximal_rotation_track=proximal_binding.track,
                    distal_rotation_track=distal_binding.track,
                    axis=axis,
                    rest_proximal=proximal.rotation,
                    rest_distal=distal.rotation,
                    rest_hand=nodes[hand_index].rotation,
                    rest_compound=rest_compound,
                    rest_proximal_phase_degrees=_signed_axial_phase_degrees(proximal.rotation, axis),
                    rest_distal_phase_degrees=_signed_axial_phase_degrees(distal.rotation, axis),
                    rest_compound_phase_degrees=_signed_axial_phase_degrees(rest_compound, axis),
                    rest_child_position_fraction=child_along / forearm_hand_length,
                    rest_child_perpendicular_m=child_perpendicular,
                    position_serialization_bound_m=position_bound,
                    trajectory_frame_count=len(frame_locals),
                    maximum_proximal_swing_degrees=math.degrees(2.0 * math.asin(min(1.0, maximum_proximal_swing))),
                    maximum_distal_swing_degrees=math.degrees(2.0 * math.asin(min(1.0, maximum_distal_swing))),
                    maximum_proximal_center_offset_m=maximum_proximal_center,
                    maximum_distal_off_axis_m=maximum_distal_off_axis,
                    maximum_proximal_scale_error=maximum_proximal_scale_error,
                    maximum_distal_scale_error=maximum_distal_scale_error,
                    distal_along_range_m=(minimum_distal_along, maximum_distal_along),
                ))

        if len(candidates) == 1:
            status: RouteStatus = 'identified'
            reason = 'One exact Avatar hand chain has a unique axial two-level native-rotation route; source phase fitting remains downstream.'
        elif candidates:
            status = 'ambiguous'
            reason = str(len(candidates)) + ' axial native-rotation routes match this Avatar hand chain; no route was selected.'
        else:
            status = 'not_applicable'
            if not saw_bound_pair:
                rejected.append('No two-level non-Human helper chain with native rotation bindings was found under the mapped forearm')
            reason = '; '.join(_deduplicate_reasons(rejected))
        resolutions.append(WristRouteResolution(forearm_slot, hand_slot, status, tuple(candidates), reason,
                                                 _deduplicate_reasons(rejected)))

    return WristRouteScan(tuple(resolutions))
