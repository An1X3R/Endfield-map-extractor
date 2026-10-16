"""Apply exact Avatar hand and foot goals to decoded source animation frames."""
from __future__ import annotations

from dataclasses import asdict, dataclass
import hashlib
import json
import math
from pathlib import Path
from typing import cast

from character_animation_goals import GoalRig, GoalSample, HumanMassBone, LimbSpec, LocalTransform, compile_goal_rig, solve_goal_frame

GOAL_NAMES = ('LeftFoot', 'RightFoot', 'LeftHand', 'RightHand')
GOAL_FIELDS = tuple(name + suffix + '.' + component for name in GOAL_NAMES for suffix, components in (('T', 'xyz'), ('Q', 'xyzw')) for component in components)
ROOT_FIELDS = tuple('RootT.' + axis for axis in 'xyz') + tuple('RootQ.' + axis for axis in 'xyzw')
EXPECTED_FIELDS = frozenset(GOAL_FIELDS + ROOT_FIELDS)
POSITION_TOLERANCE_M = 1e-5
ROTATION_TOLERANCE_DEGREES = 1e-3
LIMB_SLOTS = (
    ('LeftFoot', (1, 3, 5), None),
    ('RightFoot', (2, 4, 6), None),
    ('LeftHand', (14, 16, 18), 12),
    ('RightHand', (15, 17, 19), 13),
)


@dataclass(frozen=True)
class GoalTrackSet:
    tracks: tuple[tuple[str, int], ...]
    applied: bool


@dataclass(frozen=True)
class GoalExportResult:
    status: str
    source_local_frames: Path
    unapplied_channels: Path
    receipt_path: Path
    output_paths: tuple[Path, ...]
    input_paths: tuple[Path, ...]


class GoalFrameError(RuntimeError):
    def __init__(self, character_id: str, frame: int, frame_time: float, cause: Exception):
        self.character_id = character_id
        self.frame = frame
        self.frame_time_s = frame_time
        super().__init__('Source-goal frame failed for ' + character_id + ' at frame ' + str(frame) +
                         ' time ' + repr(frame_time) + 's: ' + str(cause))


def object_value(value: object, context: str) -> dict[str, object]:
    if not isinstance(value, dict) or any(not isinstance(key, str) for key in value):
        raise TypeError('Expected JSON object: ' + context)
    return cast(dict[str, object], value)


def array_value(value: object, context: str) -> list[object]:
    if not isinstance(value, list):
        raise TypeError('Expected JSON array: ' + context)
    return cast(list[object], value)


def field_value(value: dict[str, object], field: str, context: str) -> object:
    if field not in value:
        raise KeyError('Missing JSON field ' + context + '.' + field)
    return value[field]


def integer_value(value: object, context: str) -> int:
    if type(value) is not int:
        raise TypeError('Expected integer: ' + context)
    return value


def number_value(value: object, context: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise TypeError('Expected numeric value: ' + context)
    number = float(value)
    if not math.isfinite(number):
        raise ValueError('Expected finite numeric value: ' + context)
    return number


def text_value(value: object, context: str) -> str:
    if not isinstance(value, str) or not value:
        raise TypeError('Expected nonempty string: ' + context)
    return value


def read_json(path: Path, context: str) -> object:
    try:
        return json.loads(path.read_text(encoding='utf-8-sig'))
    except json.JSONDecodeError as error:
        raise ValueError('Invalid JSON in ' + context + ': ' + str(path)) from error


def write_json(path: Path, value: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, indent=2, allow_nan=False), encoding='utf-8')


def write_frame_json(path: Path, frames: list[dict[str, object]]) -> None:
    """Preserve exact frame values while avoiding pretty-print work and extra bytes."""
    path.write_text(json.dumps(frames, separators=(',', ':'), allow_nan=False), encoding='utf-8')


def file_hash(path: Path) -> str:
    with path.open('rb') as stream:
        return hashlib.file_digest(stream, 'sha256').hexdigest()


def vector3(value: object, context: str) -> tuple[float, float, float]:
    values = array_value(value, context)
    if len(values) != 3:
        raise ValueError('Expected three components: ' + context)
    return tuple(number_value(component, context) for component in values)  # type: ignore[return-value]


def quaternion_xyzw(value: object, context: str) -> tuple[float, float, float, float]:
    if isinstance(value, dict):
        record = object_value(value, context)
        components = tuple(number_value(field_value(record, key, context), context + '.' + key) for key in ('X', 'Y', 'Z', 'W'))
    else:
        values = array_value(value, context)
        if len(values) != 4:
            raise ValueError('Expected four quaternion components: ' + context)
        components = tuple(number_value(component, context) for component in values)
    magnitude = math.hypot(*components)
    if abs(magnitude - 1.0) > 1e-4:
        raise ValueError('Expected unit quaternion: ' + context)
    return cast(tuple[float, float, float, float], components)


def resolve_goal_tracks(bindings_value: object) -> GoalTrackSet:
    bindings = array_value(bindings_value, 'scalar bindings')
    occurrences: dict[str, list[dict[str, object]]] = {name: [] for name in EXPECTED_FIELDS}
    for index, raw_binding in enumerate(bindings):
        binding = object_value(raw_binding, 'scalar bindings[' + str(index) + ']')
        name = binding.get('attribute_name')
        if isinstance(name, str) and name in EXPECTED_FIELDS:
            occurrences[name].append(binding)
    duplicates = sorted(name for name, rows in occurrences.items() if len(rows) > 1)
    if duplicates:
        raise ValueError('Duplicate source-goal scalar bindings: ' + ', '.join(duplicates))
    present_goals = tuple(name for name in GOAL_FIELDS if occurrences[name])
    if not present_goals:
        return GoalTrackSet((), False)
    missing_goals = tuple(name for name in GOAL_FIELDS if not occurrences[name])
    if missing_goals:
        raise ValueError('Incomplete source-goal scalar set; missing: ' + ', '.join(missing_goals))
    missing_root = tuple(name for name in ROOT_FIELDS if not occurrences[name])
    if missing_root:
        raise ValueError('Source-goal set has no complete RootT/RootQ basis; missing: ' + ', '.join(missing_root))
    resolved: list[tuple[str, int]] = []
    seen_tracks: dict[int, str] = {}
    for name in ROOT_FIELDS + GOAL_FIELDS:
        binding = occurrences[name][0]
        custom_type = integer_value(field_value(binding, 'custom_type', name), name + '.custom_type')
        if custom_type != 8:
            raise ValueError('Source-goal scalar is not custom_type 8: ' + name)
        track = integer_value(field_value(binding, 'track', name), name + '.track')
        if track < 0:
            raise ValueError('Source-goal scalar has negative track index: ' + name)
        if track in seen_tracks:
            raise ValueError('Source-goal scalars share a track index: ' + seen_tracks[track] + ' and ' + name)
        seen_tracks[track] = name
        resolved.append((name, track))
    return GoalTrackSet(tuple(resolved), True)


def validate_sample_times(frames_value: object, samples_value: object, hierarchy_count: int, track_count: int,
                          time_domain_value: object) -> tuple[list[dict[str, object]], list[dict[str, object]]]:
    frames_raw = array_value(frames_value, 'source local frames')
    samples_raw = array_value(samples_value, 'float samples')
    time_domain = object_value(time_domain_value, 'frame-domain verification')
    common_count = integer_value(field_value(time_domain, 'common_frame_count', 'frame-domain verification'), 'common frame count')
    float_domain = object_value(field_value(time_domain, 'float', 'frame-domain verification'), 'frame-domain verification.float')
    original_count = integer_value(field_value(float_domain, 'original_frame_count', 'frame-domain verification.float'), 'float original frame count')
    excluded_count = integer_value(field_value(float_domain, 'excluded_tail_frames', 'frame-domain verification.float'), 'float excluded tail count')
    if not frames_raw or common_count != len(frames_raw) or original_count != len(samples_raw) or excluded_count != len(samples_raw) - len(frames_raw):
        raise ValueError('Frame-domain counts disagree with merged frames or full raw float samples')
    frames: list[dict[str, object]] = []
    samples: list[dict[str, object]] = []
    prior_time: float | None = None
    for index, raw_sample in enumerate(samples_raw):
        sample = object_value(raw_sample, 'float_samples[' + str(index) + ']')
        sample_time = number_value(field_value(sample, 'time', 'float_samples[' + str(index) + ']'), 'float sample time')
        if prior_time is not None and sample_time <= prior_time:
            raise ValueError('Source-goal timestamps are duplicate or decreasing at frame ' + str(index))
        prior_time = sample_time
        values = array_value(field_value(sample, 'values', 'float_samples[' + str(index) + ']'), 'float sample values')
        if len(values) < track_count:
            raise ValueError('Float sample values do not contain all bound tracks at frame ' + str(index))
        samples.append(sample)
    for field_name, expected_time in (('source_start_time', samples[0]['time']), ('source_end_time', samples[-1]['time'])):
        domain_time = number_value(field_value(float_domain, field_name, 'frame-domain verification.float'), 'float ' + field_name)
        if domain_time != number_value(expected_time, 'float sample endpoint'):
            raise ValueError('Frame-domain ' + field_name + ' differs from full float sample timeline')
    if len(frames_raw) > len(samples):
        raise ValueError('Merged frame prefix exceeds raw float sample timeline')
    for index, (raw_frame, sample) in enumerate(zip(frames_raw, samples, strict=False)):
        frame = object_value(raw_frame, 'source_local_frames[' + str(index) + ']')
        frame_time = number_value(field_value(frame, 'time', 'source_local_frames[' + str(index) + ']'), 'frame time')
        sample_time = number_value(field_value(sample, 'time', 'float_samples[' + str(index) + ']'), 'float sample time')
        if frame_time != sample_time:
            raise ValueError('Source-local/float sample time mismatch at frame ' + str(index) + ': ' + repr(frame_time) + ' != ' + repr(sample_time))
        locals_value = array_value(field_value(frame, 'locals', 'source_local_frames[' + str(index) + ']'), 'frame locals')
        if len(locals_value) != hierarchy_count:
            raise ValueError('Source local count disagrees with hierarchy at frame ' + str(index))
        frames.append(frame)
    for field_name, expected_time in (('exported_start_time', frames[0]['time']), ('exported_end_time', frames[-1]['time'])):
        domain_time = number_value(field_value(time_domain, field_name, 'frame-domain verification'), 'frame-domain ' + field_name)
        if domain_time != number_value(expected_time, 'merged frame endpoint'):
            raise ValueError('Frame-domain ' + field_name + ' differs from merged frame timeline')
    return frames, samples


def independent_buffer_samples(value: object, domain_value: object, name: str) -> list[dict[str, object]]:
    """Validate one complete decoded buffer without borrowing another buffer's timeline."""
    values = array_value(value, name + ' samples')
    domain = object_value(domain_value, name + ' frame domain')
    count = integer_value(field_value(domain, 'original_frame_count', name), name + ' frame count')
    excluded = integer_value(field_value(domain, 'excluded_tail_frames', name), name + ' excluded tail count')
    if not values or len(values) != count or excluded != 0:
        raise ValueError('Independent Generic buffer must retain every source sample: ' + name)
    samples: list[dict[str, object]] = []
    prior: float | None = None
    for index, value in enumerate(values):
        sample = object_value(value, name + ' sample ' + str(index))
        timestamp = number_value(field_value(sample, 'time', name), name + ' sample time')
        if prior is not None and timestamp <= prior:
            raise ValueError('Independent Generic timestamps are duplicate or decreasing: ' + name + ' frame ' + str(index))
        prior = timestamp
        samples.append(sample)
    for field, sample in (('source_start_time', samples[0]), ('source_end_time', samples[-1])):
        if number_value(field_value(domain, field, name), name + ' ' + field) != number_value(sample['time'], name + ' endpoint'):
            raise ValueError('Independent Generic buffer endpoint differs from its source samples: ' + name + '.' + field)
    return samples


def validate_generic_qvvf_sample_times(frames_value: object, transform_value: object, root_value: object,
                                      float_value: object, hierarchy_count: int,
                                      time_domain_value: object) -> tuple[list[dict[str, object]], list[dict[str, object]]]:
    """Retain the full authenticated Generic QVVF timeline; other buffers remain independent."""
    domain = object_value(time_domain_value, 'Generic frame domain')
    if domain.get('status') != 'generic_full_qvvf_timestamp_domain_verified':
        raise ValueError('Generic QVVF export requires its explicit independent frame-domain status')
    transforms = independent_buffer_samples(transform_value, field_value(domain, 'transform', 'Generic frame domain'), 'transform')
    independent_buffer_samples(root_value, field_value(domain, 'root_motion', 'Generic frame domain'), 'root_motion')
    samples = independent_buffer_samples(float_value, field_value(domain, 'float', 'Generic frame domain'), 'float')
    raw_frames = array_value(frames_value, 'Generic source local frames')
    if (len(raw_frames) != len(transforms)
            or integer_value(field_value(domain, 'frame_count', 'Generic frame domain'), 'Generic frame count') != len(raw_frames)
            or integer_value(field_value(domain, 'exported_frame_count', 'Generic frame domain'), 'Generic exported frame count') != len(raw_frames)):
        raise ValueError('Generic merged frame count differs from the complete source QVVF timeline')
    frames: list[dict[str, object]] = []
    for index, (raw_frame, transform) in enumerate(zip(raw_frames, transforms, strict=True)):
        frame = object_value(raw_frame, 'Generic source frame ' + str(index))
        if number_value(field_value(frame, 'time', 'Generic source frame'), 'Generic frame time') != number_value(transform['time'], 'transform sample time'):
            raise ValueError('Generic merged timestamp differs from its source QVVF sample: frame ' + str(index))
        if len(array_value(field_value(frame, 'locals', 'Generic source frame'), 'Generic frame locals')) != hierarchy_count:
            raise ValueError('Generic source local count differs from its authenticated hierarchy: frame ' + str(index))
        frames.append(frame)
    for field, sample in (('exported_start_time', frames[0]), ('exported_end_time', frames[-1])):
        if number_value(field_value(domain, field, 'Generic frame domain'), field) != number_value(sample['time'], 'Generic frame endpoint'):
            raise ValueError('Generic exported endpoint differs from its complete QVVF timeline: ' + field)
    return frames, samples


def mapped_source_index(path_value: object, animator_root_path: str, path_to_index: dict[str, int], context: str) -> tuple[int, str]:
    path = text_value(path_value, context + ' Avatar TOS path')
    full_path = animator_root_path.rstrip('/') + '/' + path
    if full_path not in path_to_index:
        raise ValueError('Exact Avatar path is absent from source hierarchy: ' + full_path)
    return path_to_index[full_path], full_path


def compile_avatar_rig(avatar_value: object, animator_root_path: str, hierarchy: list[dict[str, object]]) -> tuple[GoalRig, tuple[LimbSpec, ...], float, dict[str, object]]:
    avatar = object_value(avatar_value, 'avatar parsed')
    avatar_fields = object_value(field_value(avatar, 'm_Avatar', 'avatar parsed'), 'avatar parsed.m_Avatar')
    human = object_value(field_value(avatar_fields, 'm_Human', 'avatar parsed.m_Avatar'), 'avatar parsed.m_Avatar.m_Human')
    skeleton = object_value(field_value(human, 'm_Skeleton', 'Avatar Human'), 'Avatar Human.m_Skeleton')
    nodes = array_value(field_value(skeleton, 'm_Node', 'Avatar Human.m_Skeleton'), 'Avatar skeleton nodes')
    ids = array_value(field_value(skeleton, 'm_ID', 'Avatar Human.m_Skeleton'), 'Avatar skeleton IDs')
    axes_array = array_value(field_value(skeleton, 'm_AxesArray', 'Avatar Human.m_Skeleton'), 'Avatar skeleton axes')
    human_bone_indices = array_value(field_value(human, 'm_HumanBoneIndex', 'Avatar Human'), 'Avatar Human.m_HumanBoneIndex')
    tos = object_value(field_value(avatar, 'm_TOS', 'avatar parsed'), 'avatar parsed.m_TOS')
    if len(nodes) != len(ids):
        raise ValueError('Avatar skeleton node and ID arrays have different lengths')
    parents_list: list[int] = []
    for index, raw_node in enumerate(nodes):
        node = object_value(raw_node, 'Avatar skeleton node ' + str(index))
        parents_list.append(integer_value(field_value(node, 'm_ParentId', 'Avatar skeleton node ' + str(index)), 'Avatar parent index'))
    path_to_index: dict[str, int] = {}
    for index, raw_node in enumerate(hierarchy):
        path = text_value(field_value(raw_node, 'path', 'source hierarchy node ' + str(index)), 'source hierarchy path')
        if path in path_to_index:
            raise ValueError('Duplicate path in source hierarchy: ' + path)
        stored_index = integer_value(field_value(raw_node, 'index', 'source hierarchy node ' + str(index)), 'source hierarchy index')
        if stored_index != index:
            raise ValueError('Source hierarchy index does not match serialized array position: ' + path)
        path_to_index[path] = index
    if animator_root_path not in path_to_index:
        raise ValueError('Exact Animator root path is absent from source hierarchy: ' + animator_root_path)

    human_bone_masses = array_value(field_value(human, 'm_HumanBoneMass', 'Avatar Human'), 'Avatar Human.m_HumanBoneMass')
    if not human_bone_indices or len(human_bone_indices) != len(human_bone_masses):
        raise ValueError('Avatar HumanBoneIndex and HumanBoneMass arrays must be nonempty and have matching lengths')
    mapped_slots: dict[int, tuple[int, str, int]] = {}
    human_mass_bones: list[HumanMassBone] = []
    mass_mapping_rows: list[dict[str, object]] = []
    missing_mass_slots: list[dict[str, object]] = []
    for slot, (raw_node, raw_mass) in enumerate(zip(human_bone_indices, human_bone_masses, strict=True)):
        bone_node = integer_value(raw_node, 'Avatar HumanBoneIndex[' + str(slot) + ']')
        mass = number_value(raw_mass, 'Avatar HumanBoneMass[' + str(slot) + ']')
        if bone_node < 0:
            missing_mass_slots.append({'slot': slot, 'avatar_skeleton_node': bone_node, 'mass': mass, 'status': 'missing_avatar_node'})
            continue
        if bone_node >= len(ids):
            raise ValueError('Avatar HumanBoneIndex is out of range for mass slot ' + str(slot))
        bone_id = integer_value(ids[bone_node], 'Avatar skeleton ID ' + str(bone_node))
        avatar_path = text_value(tos.get(str(bone_id)), 'Avatar TOS entry ' + str(bone_id))
        source_index, full_path = mapped_source_index(avatar_path, animator_root_path, path_to_index, 'Human mass slot ' + str(slot))
        mapped_slots[slot] = (bone_node, full_path, source_index)
        human_mass_bones.append(HumanMassBone(slot, source_index, mass))
        mass_mapping_rows.append({'slot': slot, 'avatar_skeleton_node': bone_node, 'avatar_path': full_path,
                                  'source_index': source_index, 'mass': mass})
    if 0 not in mapped_slots:
        raise ValueError('Avatar HumanBoneIndex has no valid Hips slot')
    hips_node, hips_full_path, hips_source_index = mapped_slots[0]
    mapping_rows: list[dict[str, object]] = []
    for limb_name, slots, shoulder_slot in LIMB_SLOTS:
        role_rows: list[tuple[int, int, str, int]] = []
        for slot in slots + (() if shoulder_slot is None else (shoulder_slot,)):
            if slot not in mapped_slots:
                raise ValueError('Avatar HumanBoneIndex lacks required slot ' + str(slot) + ' for ' + limb_name)
            bone_node, full_path, source_index = mapped_slots[slot]
            role_rows.append((slot, bone_node, full_path, source_index))
        mapping_rows.append({'goal': limb_name, 'avatar_slots': [{'slot': slot, 'avatar_skeleton_node': node, 'avatar_path': path, 'source_index': source_index} for slot, node, path, source_index in role_rows]})

    hips_index = mapped_slots[0][2]
    specs: list[LimbSpec] = []
    for limb_name, slots, shoulder_slot in LIMB_SLOTS:
        upper, middle, end = (mapped_slots[slot][2] for slot in slots)
        end_avatar_node = mapped_slots[slots[-1]][0]
        skeleton_node = object_value(nodes[end_avatar_node], 'Avatar end skeleton node')
        axes_id = integer_value(field_value(skeleton_node, 'm_AxesId', 'Avatar end skeleton node'), 'Avatar end axes ID')
        if axes_id < 0 or axes_id >= len(axes_array):
            raise ValueError('Avatar endpoint has no valid axes entry: ' + limb_name)
        axes = object_value(axes_array[axes_id], 'Avatar axes ' + str(axes_id))
        post_rotation = quaternion_xyzw(field_value(axes, 'm_PostQ', 'Avatar axes ' + str(axes_id)), limb_name + ' PostQ')
        length = number_value(field_value(axes, 'm_Length', 'Avatar axes ' + str(axes_id)), limb_name + ' axis length')
        if length <= 0.0:
            raise ValueError('Avatar endpoint axis length must be positive: ' + limb_name)
        bottom_offset = (length, 0.0, 0.0) if limb_name.endswith('Foot') else (0.0, 0.0, 0.0)
        shoulder = None if shoulder_slot is None else mapped_slots[shoulder_slot][2]
        specs.append(LimbSpec(limb_name, upper, middle, end, post_rotation, bottom_offset, shoulder))
    parents = tuple(integer_value(field_value(node, 'parent_index', 'source hierarchy node'), 'source hierarchy parent') for node in hierarchy)
    animator_index = path_to_index[animator_root_path]
    rig = compile_goal_rig(parents, animator_index, hips_index, tuple(specs), tuple(human_mass_bones))
    human_scale = number_value(field_value(human, 'm_Scale', 'Avatar Human'), 'Avatar Human.m_Scale')
    if human_scale <= 0.0:
        raise ValueError('Avatar Human scale must be positive')
    mapping = {'animator_root_path': animator_root_path, 'animator_source_index': animator_index,
               'hips': {'avatar_slot': 0, 'avatar_skeleton_node': mapped_slots[0][0], 'avatar_path': mapped_slots[0][1], 'source_index': hips_index},
               'limbs': mapping_rows, 'human_mass_slots': mass_mapping_rows,
               'missing_mass_slots': missing_mass_slots}
    return rig, tuple(specs), human_scale, mapping


def encode_local(value: LocalTransform) -> dict[str, list[float]]:
    return {'position': list(value.position), 'rotation': list(value.rotation), 'scale': list(value.scale)}


def float_track_value(sample: dict[str, object], track: int, context: str) -> float:
    values = array_value(field_value(sample, 'values', context), context + '.values')
    if track >= len(values):
        raise ValueError('Float sample lacks track ' + str(track) + ' at ' + context)
    return number_value(values[track], context + '.values[' + str(track) + ']')


def goals_from_sample(sample: dict[str, object], tracks: dict[str, int]) -> tuple[GoalSample, ...]:
    goals: list[GoalSample] = []
    for name in GOAL_NAMES:
        position = cast(tuple[float, float, float], tuple(float_track_value(sample, tracks[name + 'T.' + axis], name + 'T') for axis in 'xyz'))
        rotation = cast(tuple[float, float, float, float], tuple(float_track_value(sample, tracks[name + 'Q.' + axis], name + 'Q') for axis in 'xyzw'))
        goals.append(GoalSample(name, position, rotation))
    return tuple(goals)


def transform_from_json(value: object, context: str) -> LocalTransform:
    transform = object_value(value, context)
    return LocalTransform(vector3(field_value(transform, 'position', context), context + '.position'),
                          quaternion_xyzw(field_value(transform, 'rotation', context), context + '.rotation'),
                          vector3(field_value(transform, 'scale', context), context + '.scale'))


def animator_parent_world_scale(rig: GoalRig, locals_: tuple[LocalTransform, ...]) -> float:
    index = rig.parents[rig.animator_index]
    scale = 1.0
    while index >= 0:
        sx, sy, sz = locals_[index].scale
        if min(sx, sy, sz) <= 0.0 or not math.isclose(sx, sy, rel_tol=2e-6, abs_tol=0.0) or not math.isclose(sx, sz, rel_tol=2e-6, abs_tol=0.0):
            raise ValueError('Animator external parent has unsupported nonpositive or nonuniform scale at source node ' + str(index))
        scale *= sx
        index = rig.parents[index]
    if not math.isfinite(scale) or scale <= 0.0:
        raise ValueError('Animator external parent world scale is invalid')
    return scale


def source_code_paths() -> tuple[Path, ...]:
    here = Path(__file__).resolve().parent
    return (Path(__file__).resolve(), here / 'character_animation_goals.py', here / 'export_animation.py',
            here / 'character_animation_wrist_routes.py', here / 'character_animation_wrist_twist.py')


def reconcile_wrist_twist(rig: GoalRig, avatar_value: object, hierarchy_value: object,
                         native_bindings_value: object, base_frames_value: object,
                         animator_root_path: str, frames: list[dict[str, object]]) -> tuple[list[dict[str, object]], dict[str, object]]:
    """Apply explicitly identified axial helper routes to accepted goal frames."""
    from character_animation_wrist_routes import identify_wrist_auxiliary_routes
    from character_animation_wrist_twist import WristTwistFrameError, WristTwistRoute, apply_wrist_twist

    scan = identify_wrist_auxiliary_routes(avatar_value, hierarchy_value, native_bindings_value,
                                          base_frames_value, animator_root_path)
    ambiguous = tuple(hand for hand in scan.hands if hand.status == 'ambiguous')
    if ambiguous:
        raise ValueError('Post-goal wrist route is ambiguous: Animator=' + animator_root_path + '; ' +
                         '; '.join('Avatar hand slot=' + str(hand.hand_avatar_slot) + ': ' + hand.reason for hand in ambiguous))
    routes = tuple(WristTwistRoute(route.name, route.upper, route.middle, route.end, route.proximal,
                                   route.distal, route.axis, route.rest_proximal, route.rest_distal, route.rest_hand)
                   for hand in scan.hands if hand.status == 'identified' for route in hand.candidates)
    summary: dict[str, object] = {'status': 'not_applicable_no_identified_native_axial_helpers',
                                  'routes': asdict(scan)['hands'], 'fits': [], 'diagnostics': [],
                                  'boundary': 'Measured ACL proportions guide an authoring correction. Native runtime twist weights and shoulder strain are not reconstructed.'}
    if not routes:
        return frames, summary
    typed_frames = tuple(tuple(transform_from_json(value, 'post-goal frame ' + str(frame_index) + ' local ' + str(local_index))
                               for local_index, value in enumerate(array_value(field_value(frame, 'locals', 'post-goal frame'), 'post-goal locals')))
                         for frame_index, frame in enumerate(frames))
    # The goal solver's allowlist already proves these ACL helper locals unchanged.
    try:
        result = apply_wrist_twist(rig, routes, typed_frames, typed_frames)
    except WristTwistFrameError as error:
        frame_time = number_value(field_value(frames[error.frame_index], 'time', 'wrist frame'), 'wrist frame time')
        raise ValueError('Post-goal wrist failed: Animator=' + animator_root_path +
                         '; time_s=' + repr(frame_time) + '; ' + str(error)) from error
    changed = {index for route in routes for index in (route.upper, route.middle, route.end, route.proximal, route.distal)}
    corrected_frames: list[dict[str, object]] = []
    for before, corrected in zip(frames, result.frames, strict=True):
        raw_locals = array_value(field_value(before, 'locals', 'post-goal frame'), 'post-goal locals')
        current = dict(before)
        current['locals'] = [encode_local(value) if index in changed else raw_locals[index]
                             for index, value in enumerate(corrected)]
        corrected_frames.append(current)
    summary.update({'status': 'applied_geometric_axial_reconciliation_not_native_parity',
                    'fits': [asdict(fit) for fit in result.fits],
                    'changed_rotation_indices': sorted(changed),
                    'diagnostics': [[asdict(item) for item in frame] for frame in result.diagnostics],
                    'maximum_joint_position_error_m': max(item.joint_position_error_m for frame in result.diagnostics for item in frame),
                    'maximum_hand_rotation_error_degrees': max(item.hand_rotation_error_degrees for frame in result.diagnostics for item in frame)})
    return corrected_frames, summary


def export_source_goals(output: Path, character_id: str, avatar_cab: str, avatar_path_id: int,
                        animator_root_path: str, hierarchy_path: Path, base_frames_path: Path,
                        base_unapplied_path: Path, time_domain_path: Path) -> GoalExportResult:
    avatar_path = output / 'avatar_parsed.json'
    binding_path = output / 'native/scalar_bindings.json'
    samples_path = output / 'native/float_samples.json'
    avatar_value = read_json(avatar_path, 'parsed Avatar')
    bindings_value = read_json(binding_path, 'scalar bindings')
    samples_value = read_json(samples_path, 'float samples')
    hierarchy_raw = array_value(read_json(hierarchy_path, 'source hierarchy'), 'source hierarchy')
    hierarchy = [object_value(value, 'source hierarchy node ' + str(index)) for index, value in enumerate(hierarchy_raw)]
    base_frames_value = read_json(base_frames_path, 'base source-local frames')
    base_unapplied_value = read_json(base_unapplied_path, 'base unapplied channels')
    time_domain = object_value(read_json(time_domain_path, 'frame-domain verification'), 'frame-domain verification')
    goal_tracks = resolve_goal_tracks(bindings_value)
    maximum_track = max((track for _, track in goal_tracks.tracks), default=-1)
    additional_inputs: list[Path] = []
    if time_domain.get('status') == 'generic_full_qvvf_timestamp_domain_verified':
        if goal_tracks.applied:
            raise ValueError('Source-goal channels require the strict shared-buffer frame domain; independent Generic QVVF cannot consume them')
        transform_samples_path = output / 'native/transform_samples.json'
        root_samples_path = output / 'native/root_motion_samples.json'
        frames, samples = validate_generic_qvvf_sample_times(base_frames_value,
            read_json(transform_samples_path, 'transform samples'), read_json(root_samples_path, 'root-motion samples'),
            samples_value, len(hierarchy), time_domain)
        additional_inputs.extend((transform_samples_path, root_samples_path))
    else:
        frames, samples = validate_sample_times(base_frames_value, samples_value, len(hierarchy), maximum_track + 1, time_domain)
    sidecar_dir = output / 'source_goal_ik'
    sidecar_receipt_path = sidecar_dir / 'receipt.json'
    source_path = base_frames_path
    unapplied_path = base_unapplied_path
    output_paths: list[Path] = [sidecar_receipt_path]
    status = 'not_applicable_no_goal_channels'
    summary: dict[str, object] = {'status': status, 'character_id': character_id, 'avatar_cab': avatar_cab, 'avatar_path_id': str(avatar_path_id),
                                  'frame_count': len(frames), 'goal_channel_count': 0, 'missing_goal_channels': list(GOAL_FIELDS),
                                  'base_source_local_frames': str(base_frames_path), 'source_local_frames': str(base_frames_path),
                                  'base_unapplied_channels': str(base_unapplied_path), 'unapplied_channels': str(base_unapplied_path),
                                  'base_source_local_frames_sha256': file_hash(base_frames_path), 'source_local_frames_sha256': file_hash(base_frames_path),
                                  'base_unapplied_channels_sha256': file_hash(base_unapplied_path), 'unapplied_channels_sha256': file_hash(base_unapplied_path),
                                  'inputs': [], 'outputs': [], 'validation_boundary': 'No exact hand/foot goal channels were present; merged source frames and unapplied channel list were left unchanged.'}
    if goal_tracks.applied:
        track_map = dict(goal_tracks.tracks)
        rig, limbs, human_scale, mapping = compile_avatar_rig(avatar_value, animator_root_path, hierarchy)
        changed_indices = {rig.animator_index, rig.hips_index}
        for limb in limbs:
            changed_indices.update((limb.upper, limb.middle, limb.end))
            if limb.shoulder is not None:
                changed_indices.add(limb.shoulder)
        solved_frames: list[dict[str, object]] = []
        diagnostics: list[dict[str, object]] = []
        all_limb_diagnostics = []
        all_mass_diagnostics = []
        for index, (frame, sample) in enumerate(zip(frames, samples[:len(frames)], strict=True)):
            raw_locals = array_value(field_value(frame, 'locals', 'source frame ' + str(index)), 'source frame locals')
            locals_ = tuple(transform_from_json(value, 'frame ' + str(index) + ' local ' + str(local_index)) for local_index, value in enumerate(raw_locals))
            position_tolerance = POSITION_TOLERANCE_M * animator_parent_world_scale(rig, locals_)
            root_translation = cast(tuple[float, float, float], tuple(float_track_value(sample, track_map['RootT.' + axis], 'frame ' + str(index) + ' RootT') for axis in 'xyz'))
            root_rotation = cast(tuple[float, float, float, float], tuple(float_track_value(sample, track_map['RootQ.' + axis], 'frame ' + str(index) + ' RootQ') for axis in 'xyzw'))
            frame_time = number_value(field_value(frame, 'time', 'source frame ' + str(index)), 'source frame time')
            try:
                solved = solve_goal_frame(rig, locals_, human_scale, root_translation, root_rotation, goals_from_sample(sample, track_map))
            except (ValueError, ArithmeticError) as error:
                raise GoalFrameError(character_id, index + 1, frame_time, error) from error
            mass_diagnostic = solved.mass_diagnostic
            for diagnostic in solved.diagnostics:
                scalar_values = (diagnostic.before_error_m, diagnostic.scale_corrected_fk_error_m, diagnostic.after_error_m,
                                 diagnostic.after_rotation_error_degrees, diagnostic.shoulder_swing_degrees,
                                 diagnostic.unreachable_distance_m, diagnostic.source_plane_sine, diagnostic.axis_transport_degrees,
                                 *diagnostic.target, *diagnostic.target_rotation)
                if not all(math.isfinite(value) for value in scalar_values):
                    raise GoalFrameError(character_id, index + 1, frame_time,
                                         ValueError('Nonfinite source-goal diagnostic, limb ' + diagnostic.name))
                if diagnostic.unreachable_distance_m > position_tolerance or diagnostic.after_error_m > position_tolerance:
                    raise GoalFrameError(character_id, index + 1, frame_time,
                                         ValueError('Source goal is unreachable or exceeds positional tolerance, limb ' + diagnostic.name +
                                                    ': unreachable=' + repr(diagnostic.unreachable_distance_m) +
                                                    'm, residual=' + repr(diagnostic.after_error_m) + 'm, tolerance=' + repr(position_tolerance) + 'm'))
                if diagnostic.after_rotation_error_degrees > ROTATION_TOLERANCE_DEGREES:
                    raise GoalFrameError(character_id, index + 1, frame_time,
                                         ValueError('Source-goal rotation residual exceeds tolerance, limb ' + diagnostic.name +
                                                    ': residual=' + repr(diagnostic.after_rotation_error_degrees) + 'deg'))
            mass_values = (mass_diagnostic.before_error_m, mass_diagnostic.after_error_m,
                           *mass_diagnostic.target, *mass_diagnostic.correction_world_m,
                           mass_diagnostic.convergence_tolerance_m, mass_diagnostic.projected_step_m,
                           mass_diagnostic.body_orientation_error_degrees)
            if not all(math.isfinite(value) for value in mass_values):
                raise GoalFrameError(character_id, index + 1, frame_time, ValueError('Nonfinite COM-coupling diagnostic'))
            if type(mass_diagnostic.iterations) is not int or mass_diagnostic.iterations < 1:
                raise GoalFrameError(character_id, index + 1, frame_time,
                                     ValueError('COM refinement level count must be positive: ' + repr(mass_diagnostic.iterations)))
            if type(mass_diagnostic.evaluations) is not int or not 1 <= mass_diagnostic.evaluations <= 127:
                raise GoalFrameError(character_id, index + 1, frame_time,
                                     ValueError('COM refinement evaluation count is outside 1..127: ' + repr(mass_diagnostic.evaluations)))
            if mass_diagnostic.convergence_tolerance_m <= 0.0 or mass_diagnostic.projected_step_m < 0.0:
                raise GoalFrameError(character_id, index + 1, frame_time,
                                     ValueError('COM refinement threshold or final search resolution is invalid: resolution=' +
                                                repr(mass_diagnostic.projected_step_m) + 'm, threshold=' +
                                                repr(mass_diagnostic.convergence_tolerance_m) + 'm'))
            if mass_diagnostic.termination not in ('budget', 'refinement_resolution', 'residual_threshold'):
                raise GoalFrameError(character_id, index + 1, frame_time,
                                     ValueError('Unsupported COM refinement termination: ' + repr(mass_diagnostic.termination)))
            if mass_diagnostic.termination == 'refinement_resolution' and mass_diagnostic.projected_step_m > mass_diagnostic.convergence_tolerance_m:
                raise GoalFrameError(character_id, index + 1, frame_time,
                                     ValueError('Refinement-resolution termination exceeds its threshold: step=' +
                                                repr(mass_diagnostic.projected_step_m) + 'm, threshold=' +
                                                repr(mass_diagnostic.convergence_tolerance_m) + 'm'))
            if mass_diagnostic.termination == 'residual_threshold' and not (
                    mass_diagnostic.after_error_m <= mass_diagnostic.convergence_tolerance_m and
                    mass_diagnostic.projected_step_m > mass_diagnostic.convergence_tolerance_m):
                raise GoalFrameError(character_id, index + 1, frame_time,
                                     ValueError('Residual-threshold termination does not match the recorded COM residual/search step'))
            if mass_diagnostic.termination == 'budget' and not (
                    mass_diagnostic.after_error_m > mass_diagnostic.convergence_tolerance_m and
                    mass_diagnostic.projected_step_m > mass_diagnostic.convergence_tolerance_m and
                    mass_diagnostic.evaluations == 127):
                raise GoalFrameError(character_id, index + 1, frame_time,
                                     ValueError('Budget termination does not match the recorded COM residual/search step/evaluation count'))
            for local_index, (before, after) in enumerate(zip(locals_, solved.locals, strict=True)):
                if local_index not in changed_indices and before != after:
                    raise GoalFrameError(character_id, index + 1, frame_time,
                                         RuntimeError('Source-goal solver modified an unrelated source local, node ' + str(local_index)))
            written_locals = [encode_local(solved.locals[local_index]) if local_index in changed_indices else raw_locals[local_index] for local_index in range(len(raw_locals))]
            solved_frame = dict(frame)
            solved_frame['locals'] = written_locals
            solved_frames.append(solved_frame)
            diagnostics.append({'frame': index + 1, 'time': frame_time, 'mass_center_guidance': {
                'target': list(mass_diagnostic.target), 'before_error_m': mass_diagnostic.before_error_m,
                'approximate_residual_after_m': mass_diagnostic.after_error_m,
                'refinement_levels': mass_diagnostic.iterations, 'termination': mass_diagnostic.termination,
                'correction_world_m': list(mass_diagnostic.correction_world_m),
                'refinement_resolution_threshold_m': mass_diagnostic.convergence_tolerance_m,
                'final_search_resolution_m': mass_diagnostic.projected_step_m,
                'evaluations': mass_diagnostic.evaluations,
                'body_orientation_error_degrees': mass_diagnostic.body_orientation_error_degrees}, 'limbs': [
                {'name': item.name, 'target': list(item.target), 'target_rotation': list(item.target_rotation),
                 'before_error_m': item.before_error_m, 'scale_corrected_fk_error_m': item.scale_corrected_fk_error_m,
                 'after_error_m': item.after_error_m, 'after_rotation_error_degrees': item.after_rotation_error_degrees,
                 'shoulder_swing_degrees': item.shoulder_swing_degrees, 'unreachable_distance_m': item.unreachable_distance_m,
                 'source_plane_sine': item.source_plane_sine, 'axis_transport_degrees': item.axis_transport_degrees}
                for item in solved.diagnostics]})
            all_limb_diagnostics.extend(solved.diagnostics)
            all_mass_diagnostics.append(mass_diagnostic)
        native_rotation_bindings_path = output / 'native/bound_tracks.json'
        additional_inputs.append(native_rotation_bindings_path)
        solved_frames, wrist_summary = reconcile_wrist_twist(rig, avatar_value, hierarchy,
            read_json(native_rotation_bindings_path, 'native wrist rotation bindings'), base_frames_value,
            animator_root_path, solved_frames)
        sidecar_dir.mkdir(parents=True, exist_ok=False)
        source_path = sidecar_dir / 'source_local_frames.json'
        diagnostics_path = sidecar_dir / 'goal_diagnostics.json'
        wrist_path = sidecar_dir / 'wrist_twist_diagnostics.json'
        unapplied_path = sidecar_dir / 'unapplied_scalar_channels.json'
        write_frame_json(source_path, solved_frames)
        write_json(diagnostics_path, diagnostics)
        write_json(wrist_path, wrist_summary)
        unapplied_rows = array_value(base_unapplied_value, 'base unapplied channels')
        filtered_rows = []
        for index, raw_row in enumerate(unapplied_rows):
            row = object_value(raw_row, 'unapplied channel ' + str(index))
            name = row.get('name')
            if name not in GOAL_FIELDS:
                filtered_rows.append(row)
        write_json(unapplied_path, filtered_rows)
        status = 'applied_source_goal_bounded_com_guidance_axis_plane_candidate_not_native_parity'
        output_paths.extend((source_path, diagnostics_path, unapplied_path, wrist_path))
        summary = {'status': status, 'character_id': character_id, 'avatar_cab': avatar_cab, 'avatar_path_id': str(avatar_path_id),
                   'frame_count': len(frames), 'goal_channel_count': len(GOAL_FIELDS), 'missing_goal_channels': [],
                   'sample_rate_assumption': 'Inherited from the source clip header; frame timestamps are preserved exactly.',
                   'human_scale': human_scale, 'mapping': mapping,
                   'wrist_twist': {'status': wrist_summary['status'], 'diagnostics': str(wrist_path),
                                   'boundary': wrist_summary['boundary']},
                   'base_source_local_frames': str(base_frames_path), 'source_local_frames': str(source_path),
                   'base_source_local_frames_sha256': file_hash(base_frames_path), 'source_local_frames_sha256': file_hash(source_path),
                   'base_unapplied_channels': str(base_unapplied_path), 'unapplied_channels': str(unapplied_path),
                   'base_unapplied_channels_sha256': file_hash(base_unapplied_path), 'unapplied_channels_sha256': file_hash(unapplied_path),
                   'consumed_goal_channels': list(GOAL_FIELDS), 'remaining_unapplied_channel_count': len(filtered_rows),
                   'maximum_goal_error_before_m': max(item.before_error_m for item in all_limb_diagnostics),
                   'maximum_goal_error_after_m': max(item.after_error_m for item in all_limb_diagnostics),
                   'maximum_unreachable_distance_m': max(item.unreachable_distance_m for item in all_limb_diagnostics),
                   'approximate_mass_center_guidance': {'target': 'HumanScale-scaled RootT in Animator-parent world space; hips updates are projected into the two-foot reach intersection.',
                                            'termination_counts': {name: sum(item.termination == name for item in all_mass_diagnostics)
                                                                   for name in ('budget', 'refinement_resolution', 'residual_threshold')},
                                            'maximum_initial_mass_center_error_m': max(item.before_error_m for item in all_mass_diagnostics),
                                            'maximum_approximate_mass_center_residual_m': max(item.after_error_m for item in all_mass_diagnostics),
                                            'maximum_refinement_levels': max(item.iterations for item in all_mass_diagnostics),
                                            'maximum_evaluations': max(item.evaluations for item in all_mass_diagnostics),
                                            'maximum_final_search_resolution_m': max(item.projected_step_m for item in all_mass_diagnostics),
                                            'maximum_body_orientation_error_degrees': max(item.body_orientation_error_degrees for item in all_mass_diagnostics),
                                            'maximum_unreachable_goal_distance_m': max(item.unreachable_distance_m for item in all_limb_diagnostics),
                                            'boundary': 'COM is an approximate guide. Budget exhaustion, search-step refinement resolution, and COM residual threshold are distinct stop labels; none claims a mass-center stationary point or native convergence. Goal and bone-length checks remain the hard acceptance criteria.'},
                   'position_tolerance': '1e-5 m multiplied by Animator external-parent world scale; unreachable targets fail instead of being clamped silently.',
                   'inputs': [], 'outputs': [],
                   'formula_boundary': 'Uses exact Avatar HumanBoneIndex and HumanBoneMass slot routes through skeleton ID and TOS, plus m_AxesArray PostQ/Length. Animator Motion translation is HumanScale-adjusted, RootQ is preserved, and approximate hips mass guidance is constrained by both foot reach spheres. Identified native axial wrist helpers receive a geometric post-goal rotation allocation preserving Human joint positions and hand world orientations. Other unrelated source locals and source goals remain unchanged. This is a reconstruction candidate, not proof of native runtime parity.'}
    if not sidecar_dir.exists():
        sidecar_dir.mkdir(parents=True, exist_ok=False)
    code_paths = source_code_paths()
    input_paths = (avatar_path, binding_path, samples_path, hierarchy_path, base_frames_path, base_unapplied_path, time_domain_path, *additional_inputs, *code_paths)
    summary['inputs'] = [{'path': str(path), 'sha256': file_hash(path)} for path in input_paths]
    summary['outputs'] = [{'path': str(path), 'sha256': file_hash(path)} for path in output_paths if path.exists()]
    write_json(sidecar_receipt_path, summary)
    return GoalExportResult(status, source_path, unapplied_path, sidecar_receipt_path, tuple(output_paths), tuple(input_paths))
