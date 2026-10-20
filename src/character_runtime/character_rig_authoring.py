"""Add reversible authoring presentation to an imported source character rig."""
from __future__ import annotations

from collections.abc import Mapping
import hashlib
import json
import math
from pathlib import Path
import re
from typing import TypedDict, cast

import bpy
from mathutils import Vector


FORMAT = 'EndfieldCharacterRigAuthoring/1'
SLOT_PATHS_PROPERTY = 'endfield_rig_authoring_slot_paths'
RIG_MARKER_PROPERTY = 'endfield_rig_authoring_format'
OWNER_PROPERTY = 'endfield_rig_authoring_armature'
ROLE_PROPERTY = 'endfield_rig_authoring_role'
SHAPE_PROPERTY = 'endfield_rig_authoring_shape'
HUMAN_CORE_SLOT_COUNT = 25
EXTENDED_HAND_SLOT_START = 25
EXTENDED_HAND_SLOT_STOP = 55
BODY_SLOTS = frozenset(range(0, 18)) | frozenset((20, 21))
FACE_SLOTS = frozenset((22, 23, 24))
HAND_SLOTS = frozenset((18, 19)) | frozenset(range(EXTENDED_HAND_SLOT_START, EXTENDED_HAND_SLOT_STOP))
HAND_ROOT_SLOTS = frozenset((18, 19))
FINGER_NAMES = ('Thumb', 'Index', 'Middle', 'Ring', 'Little')
FINGER_SEGMENTS = ('Proximal', 'Intermediate', 'Distal')
CORE_SLOT_NAMES = (
    'Hips', 'LeftUpperLeg', 'RightUpperLeg', 'LeftLowerLeg', 'RightLowerLeg',
    'LeftFoot', 'RightFoot', 'Spine', 'Chest', 'UpperChest', 'Neck', 'Head',
    'LeftShoulder', 'RightShoulder', 'LeftUpperArm', 'RightUpperArm',
    'LeftLowerArm', 'RightLowerArm', 'LeftHand', 'RightHand', 'LeftToes',
    'RightToes', 'LeftEye', 'RightEye', 'Jaw',
)
EXTENDED_SLOT_NAMES = tuple(
    side + finger + segment
    for side in ('Left', 'Right')
    for finger in FINGER_NAMES
    for segment in FINGER_SEGMENTS
)
SLOT_NAMES = CORE_SLOT_NAMES + EXTENDED_SLOT_NAMES
LEGACY_ROLE_NAMES = ('Body', 'Hands', 'Source Details')
ACCESSORY_ROLES = ('Hair', 'Face', 'Ears', 'Tail', 'Clothing', 'Accessories')
ROLE_NAMES = ('Body', 'Hands') + ACCESSORY_ROLES + ('Source Details',)
AUTHORING_REVISION = 2
ROLE_COLORS = {
    'Body': (0.16, 0.55, 0.95), 'Hands': (0.15, 0.85, 0.55),
    'Hair': (0.75, 0.30, 0.95), 'Face': (1.0, 0.55, 0.30),
    'Ears': (1.0, 0.35, 0.60), 'Tail': (0.10, 0.80, 0.85),
    'Clothing': (0.95, 0.75, 0.15), 'Accessories': (0.55, 0.75, 1.0),
    'Source Details': (0.35, 0.35, 0.35),
}
SEMANTIC_TOKENS = {
    'Hair': frozenset(('hair', 'bang', 'bangs', 'ponytail', 'braid', 'toufa')),
    'Face': frozenset(('face', 'eye', 'eyeball', 'eyebrow', 'eyelash', 'eyelid',
                       'brow', 'nose', 'tongue', 'jaw', 'lip', 'mouth', 'cheek',
                       'tooth', 'teeth', 'iris', 'pupil')),
    'Ears': frozenset(('ear', 'ears', 'erduo')),
    'Tail': frozenset(('tail', 'weiba')),
    'Clothing': frozenset(('clothes', 'clothing', 'cloth', 'collar', 'sleeve',
                           'skirt', 'coat', 'cape', 'dress', 'belt', 'ribbon',
                           'qunbai', 'yibai', 'lalian')),
    'Accessories': frozenset(('accessory', 'accessories', 'prop', 'phone', 'hat',
                              'maozi', 'beidai', 'bag', 'pouch', 'weapon', 'holster',
                              'pendant', 'ornament', 'glasses', 'goggles', 'headset')),
}
TECHNICAL_TOKENS = frozenset(('corrective', 'twist', 'helper', 'end', 'nub',
                              'effector', 'collision', 'collider', 'physics',
                              'rigidbody', 'solver', 'constraint', 'dummy', 'mc'))


class SourceFKControl(TypedDict):
    """A display control mapped directly to one unchanged source bone."""

    bone: str
    path: str
    role: str
    category: str
    control_kind: str
    reason: str
    evidence_segment: str


def _record(value: object, label: str) -> dict[str, object]:
    if not isinstance(value, dict) or not all(isinstance(key, str) for key in value):
        raise TypeError(f'Expected JSON object: {label}')
    return cast(dict[str, object], value)


def _rows(value: object, label: str) -> list[object]:
    if not isinstance(value, list):
        raise TypeError(f'Expected JSON array: {label}')
    return cast(list[object], value)


def _integer(value: object, label: str) -> int:
    if type(value) is not int:
        raise TypeError(f'Expected integer: {label}')
    return value


def _text(value: object, label: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise TypeError(f'Expected nonempty string: {label}')
    return value


def _sha256(payload: bytes) -> str:
    return hashlib.sha256(payload).hexdigest()


def _exact_slot_paths(avatar_value: object, animator_root_path: str) -> dict[int, str]:
    """Resolve Avatar slots through HumanBoneIndex/hand arrays, skeleton IDs and TOS."""
    avatar = _record(avatar_value, 'Avatar')
    avatar_body = _record(avatar.get('m_Avatar'), 'Avatar.m_Avatar')
    human = _record(avatar_body.get('m_Human'), 'Avatar.m_Avatar.m_Human')
    skeleton = _record(human.get('m_Skeleton'), 'Avatar Human.m_Skeleton')
    nodes = _rows(skeleton.get('m_Node'), 'Avatar skeleton nodes')
    ids = _rows(skeleton.get('m_ID'), 'Avatar skeleton IDs')
    core_indices = _rows(human.get('m_HumanBoneIndex'), 'Avatar HumanBoneIndex')
    tos = _record(avatar.get('m_TOS'), 'Avatar TOS')
    if len(nodes) != len(ids) or len(core_indices) != HUMAN_CORE_SLOT_COUNT:
        raise ValueError('Avatar skeleton IDs or 25 source Human slots have an invalid length')

    raw_slots: list[tuple[int, int, str]] = []
    for slot, raw_node_index in enumerate(core_indices):
        node_index = _integer(raw_node_index, f'Avatar HumanBoneIndex[{slot}]')
        if node_index >= 0:
            raw_slots.append((slot, node_index, 'HumanBoneIndex'))
        elif node_index != -1:
            raise ValueError(f'Avatar HumanBoneIndex[{slot}] has invalid negative node index {node_index}')

    for hand_name, first_slot in (('m_LeftHand', EXTENDED_HAND_SLOT_START), ('m_RightHand', 40)):
        hand = _record(human.get(hand_name), f'Avatar Human.{hand_name}')
        hand_indices = _rows(hand.get('m_HandBoneIndex'), f'Avatar Human.{hand_name}.m_HandBoneIndex')
        if len(hand_indices) != len(FINGER_NAMES) * len(FINGER_SEGMENTS):
            raise ValueError(f'Avatar Human.{hand_name} must contain 15 finger slots')
        for index, raw_node_index in enumerate(hand_indices):
            node_index = _integer(raw_node_index, f'Avatar Human.{hand_name}[{index}]')
            if node_index >= 0:
                raw_slots.append((first_slot + index, node_index, hand_name))
            elif node_index != -1:
                raise ValueError(f'Avatar Human.{hand_name}[{index}] has invalid negative node index {node_index}')

    result: dict[int, str] = {}
    root = animator_root_path.rstrip('/')
    for slot, node_index, source in raw_slots:
        if node_index >= len(ids):
            raise ValueError(f'Avatar slot {slot} points outside its skeleton: {node_index}')
        skeleton_id = _integer(ids[node_index], f'Avatar skeleton ID[{node_index}]')
        avatar_path = _text(tos.get(str(skeleton_id)), f'Avatar TOS[{skeleton_id}]')
        full_path = root + '/' + avatar_path.lstrip('/')
        if slot in result:
            raise ValueError(f'Duplicate Avatar slot {slot} from {source}')
        result[slot] = full_path
    return result


def human_slot_paths_from_avatar(avatar_value: object, animator_root_path: str) -> dict[int, str]:
    """Return exact source paths for core and extended source-Avatar hand slots."""
    if not animator_root_path.strip():
        raise ValueError('Animator root path must be nonempty')
    return _exact_slot_paths(avatar_value, animator_root_path)


def read_animation_human_slot_paths(receipt_path: Path) -> dict[int, str]:
    """Read the selected Avatar map only when its animation receipt hashes it."""
    receipt_file = receipt_path.resolve(strict=True)
    receipt_payload = receipt_file.read_bytes()
    receipt = _record(json.loads(receipt_payload.decode('utf-8-sig')), 'animation receipt')
    if (receipt.get('format') != 'EndfieldAnimationExportReceipt/1'
            or receipt.get('status') != 'decoded_bound_and_merged_candidate'):
        raise ValueError(f'Animation receipt is not a verified merged export: {receipt_file}')
    animator_root_path = _text(receipt.get('animator_root_path'), 'receipt.animator_root_path')
    _text(receipt.get('avatar_cab'), 'receipt.avatar_cab')
    avatar_path_id = receipt.get('avatar_path_id')
    if type(avatar_path_id) not in (int, str):
        raise TypeError('Animation receipt avatar_path_id must be an integer or string')

    avatar_file = receipt_file.parent / 'avatar_parsed.json'
    avatar_payload = avatar_file.read_bytes()
    inputs = _rows(receipt.get('inputs'), 'receipt.inputs')
    matched = []
    target = str(avatar_file.resolve())
    for index, raw_input in enumerate(inputs):
        row = _record(raw_input, f'receipt.inputs[{index}]')
        input_path = _text(row.get('path'), f'receipt.inputs[{index}].path')
        if Path(input_path).resolve() == Path(target):
            matched.append(row)
    if len(matched) != 1 or matched[0].get('sha256') != _sha256(avatar_payload):
        raise ValueError(f'Selected Avatar parse is absent from or differs from its receipt: {avatar_file}')
    avatar = _record(json.loads(avatar_payload.decode('utf-8-sig')), 'selected Avatar')
    return _exact_slot_paths(avatar, animator_root_path)


def read_humanoid_bone_map(sidecar_path: Path, animator_path: str) -> dict[int, str]:
    """Read one exact animator map from the static humanoid-bone sidecar."""
    sidecar_file = sidecar_path.resolve(strict=True)
    sidecar = _record(json.loads(sidecar_file.read_text(encoding='utf-8-sig')), 'humanoid bone map')
    if sidecar.get('format') != 'EndfieldHumanoidBoneMap/1':
        raise ValueError(f'Unsupported humanoid bone map format: {sidecar_file}')
    top_status = _text(sidecar.get('status'), 'humanoid bone map.status')
    animators = _rows(sidecar.get('animators'), 'humanoid bone map.animators')
    if top_status == 'no_animators':
        if animators:
            raise ValueError(f'no_animators sidecar must have an empty animators array: {sidecar_file}')
        return {}
    if top_status != 'completed':
        raise ValueError(f'Unexpected humanoid bone map status {top_status!r}: {sidecar_file}')
    if not animators:
        raise ValueError(f'completed humanoid bone map must include animator entries: {sidecar_file}')
    matches = []
    for index, raw_animator in enumerate(animators):
        row = _record(raw_animator, f'humanoid bone map.animators[{index}]')
        path = _text(row.get('animator_path'), f'humanoid bone map.animators[{index}].animator_path')
        if path == animator_path:
            matches.append(row)
    if len(matches) != 1:
        raise ValueError(f'Expected one exact Animator entry for {animator_path}: found {len(matches)}')
    animator_status = _text(matches[0].get('status'), 'humanoid bone map Animator status')
    if animator_status not in ('mapped', 'non_human_avatar', 'avatar_missing', 'avatar_unresolved'):
        raise ValueError(f'Unexpected Animator humanoid-map status {animator_status!r}: {sidecar_file}')
    result: dict[int, str] = {}
    bones = _rows(matches[0].get('bones'), 'humanoid bone map bones')
    if animator_status in ('non_human_avatar', 'avatar_missing', 'avatar_unresolved'):
        if bones:
            raise ValueError(f'Non-mapped Animator status must have an empty bones array: {sidecar_file}')
        return {}
    for index, raw_bone in enumerate(bones):
        row = _record(raw_bone, f'humanoid bone map bones[{index}]')
        slot = _integer(row.get('slot'), f'humanoid bone map bones[{index}].slot')
        path = _text(row.get('path'), f'humanoid bone map bones[{index}].path')
        if slot < 0 or slot >= EXTENDED_HAND_SLOT_STOP or slot in result:
            raise ValueError(f'Invalid or duplicate source Avatar slot {slot} in {sidecar_file}')
        source_field = _text(row.get('source_field'), f'humanoid bone map bones[{index}].source_field')
        source_index = _integer(row.get('source_index'), f'humanoid bone map bones[{index}].source_index')
        if (source_field, source_index) != _slot_provenance(slot):
            raise ValueError(f'Source Avatar slot provenance is inconsistent for slot {slot}: {source_field}[{source_index}]')
        result[slot] = path
    return result


def _json_signature(value: object) -> str:
    payload = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(',', ':')).encode('utf-8')
    return _sha256(payload)


def _rest_signature(rig: bpy.types.Object) -> str:
    rows = []
    for bone in sorted(rig.data.bones, key=lambda item: item.name):
        rows.append({
            'name': bone.name,
            'parent': None if bone.parent is None else bone.parent.name,
            'head': [float(value) for value in bone.head_local],
            'tail': [float(value) for value in bone.tail_local],
            'matrix': [[float(value) for value in row] for row in bone.matrix_local],
            'deform': bool(bone.use_deform),
            'source_transform_path': bone.get('source_transform_path'),
        })
    return _json_signature(rows)


def _action_signature(rig: bpy.types.Object) -> str:
    action = rig.animation_data.action if rig.animation_data is not None else None
    if action is None:
        return _json_signature(None)
    rows = []
    for curve in sorted(action.fcurves, key=lambda item: (item.data_path, item.array_index, item.group.name if item.group else '')):
        rows.append({
            'data_path': curve.data_path,
            'array_index': curve.array_index,
            'group': curve.group.name if curve.group else None,
            'keys': [[float(point.co.x), float(point.co.y), point.interpolation]
                     for point in curve.keyframe_points],
        })
    return _json_signature({'name': action.name, 'curves': rows})


def _constraint_signature(rig: bpy.types.Object) -> str:
    rows = []
    for pose_bone in sorted(rig.pose.bones, key=lambda item: item.name):
        constraints = []
        for constraint in pose_bone.constraints:
            row: dict[str, object] = {
                'name': constraint.name,
                'type': constraint.type,
                'mute': bool(constraint.mute),
            }
            for property_name in ('target', 'subtarget', 'pole_target', 'pole_subtarget',
                                  'chain_count', 'use_tail', 'use_stretch', 'pole_angle'):
                if not hasattr(constraint, property_name):
                    continue
                value = getattr(constraint, property_name)
                row[property_name] = value.name if isinstance(value, bpy.types.Object) else value
            constraints.append(row)
        rows.append({'bone': pose_bone.name, 'constraints': constraints})
    return _json_signature(rows)


def _source_geometry_signature(objects: tuple[bpy.types.Object, ...],
                               source_visibility: Mapping[str, tuple[bool, bool, bool]]) -> str:
    """Hash source-mesh layout and visibility without rescanning vertex payloads."""
    rows = []
    for obj in sorted(objects, key=lambda item: item.name):
        visibility = (source_visibility[obj.name] if obj.name in source_visibility
                      else (bool(obj.hide_render), bool(obj.hide_viewport), bool(obj.hide_get())))
        rows.append({
            'name': obj.name,
            'path': obj.get('source_geometry_path'),
            'hidden': visibility[2],
            'hide_viewport': visibility[1],
            'hide_render': visibility[0],
            'mesh': obj.data.name,
            'vertex_count': len(obj.data.vertices),
            'edge_count': len(obj.data.edges),
            'polygon_count': len(obj.data.polygons),
            'groups': [group.name for group in obj.vertex_groups],
        })
    return _json_signature(rows)


def _registered_source_geometry(report: Mapping[str, object]) -> tuple[bpy.types.Object, ...]:
    """Resolve the source set registered before later optional display layers existed."""
    names = tuple(_text(name, 'report.source_geometry[]')
                  for name in _rows(report.get('source_geometry'), 'report.source_geometry'))
    if len(names) != len(set(names)):
        raise ValueError('Rig authoring source geometry contains duplicate object names')
    objects: list[bpy.types.Object] = []
    for name in names:
        obj = bpy.context.scene.objects.get(name)
        if obj is None or obj.type != 'MESH' or not obj.get('source_geometry_path'):
            raise AssertionError(f'Registered source geometry is absent from the actual scene: {name}')
        objects.append(obj)
    return tuple(objects)


def _potential_source_visibility(objects: tuple[bpy.types.Object, ...]) -> dict[str, tuple[bool, bool, bool]]:
    """Delegate only verified overlay visibility while retaining the original source hash."""
    from character_potential_build import verify_potential_appearance

    source_names = frozenset(obj.name for obj in objects)
    result: dict[str, tuple[bool, bool, bool]] = {}
    for owner in bpy.context.scene.objects:
        saved = owner.get('endfield_potential_build_report')
        if saved is None:
            continue
        potential = _record(json.loads(_text(saved, 'potential build report')), 'potential build report')
        applied = _record(potential.get('applied'), 'potential.applied')
        overlay_names = tuple(_text(name, 'potential.overlay_objects[]')
                              for name in _rows(applied.get('overlay_objects'), 'potential.overlay_objects'))
        if not source_names.intersection(overlay_names):
            continue
        verify_potential_appearance(potential, bpy.context.scene)
        baseline = _record(potential.get('source_overlay_visibility'), 'potential.source_overlay_visibility')
        if set(baseline) != set(overlay_names):
            raise ValueError('Potential source visibility evidence differs from its exact overlay set')
        for name in overlay_names:
            if name not in source_names or name in result:
                raise ValueError(f'Potential overlay is outside or repeats the registered source set: object={name}')
            visibility = _rows(baseline[name], f'potential.source_overlay_visibility.{name}')
            if len(visibility) != 3 or any(type(value) is not bool for value in visibility):
                raise ValueError(f'Potential source visibility requires three booleans: object={name}')
            result[name] = (visibility[0], visibility[1], visibility[2])
    return result


def _slot_paths(value: Mapping[int, str] | None, rig: bpy.types.Object) -> tuple[dict[int, str], str]:
    if value is None:
        saved = rig.get(SLOT_PATHS_PROPERTY)
        if saved is None:
            return {}, 'missing'
        parsed = _record(json.loads(str(saved)), 'saved rig authoring slot paths')
        restored: dict[int, str] = {}
        for raw_slot, raw_path in parsed.items():
            try:
                slot = int(raw_slot)
            except ValueError as error:
                raise ValueError(f'Saved Avatar slot is not an integer: {raw_slot}') from error
            if str(slot) != raw_slot or slot < 0 or slot >= EXTENDED_HAND_SLOT_STOP:
                raise ValueError(f'Saved source Avatar slot is invalid: {raw_slot}')
            restored[slot] = _text(raw_path, f'saved source Avatar slot {slot}')
        return restored, 'restored'

    result: dict[int, str] = {}
    for raw_slot, raw_path in value.items():
        if type(raw_slot) is not int or raw_slot < 0 or raw_slot >= EXTENDED_HAND_SLOT_STOP:
            raise ValueError(f'Source Avatar slot must be an integer in 0..54: {raw_slot!r}')
        if raw_slot in result:
            raise ValueError(f'Duplicate source Avatar slot: {raw_slot}')
        result[raw_slot] = _text(raw_path, f'source Avatar slot {raw_slot}')
    return result, 'provided' if result else 'missing'


def _path_index(rig: bpy.types.Object) -> dict[str, bpy.types.Bone]:
    result: dict[str, bpy.types.Bone] = {}
    for bone in rig.data.bones:
        path = bone.get('source_transform_path')
        if not isinstance(path, str) or not path:
            continue
        if path in result:
            raise ValueError(f'Duplicate source_transform_path on armature {rig.name}: {path}')
        result[path] = bone
    return result


def _bone_role(slot: int) -> str:
    if slot in HAND_SLOTS:
        return 'Hands'
    if slot in FACE_SLOTS:
        return 'Face'
    if slot in BODY_SLOTS:
        return 'Body'
    return 'Source Details'


def _name_tokens(name: str) -> frozenset[str]:
    """Split source identifiers at case, separator and numeric boundaries."""
    separated = re.sub(r'([a-z])([A-Z])', r'\1_\2', name)
    return frozenset(re.findall(r'[a-z]+', separated.lower()))


def _semantic_role(path: str) -> tuple[str, str]:
    """Use the nearest named source segment, without guessing from character IDs."""
    for segment in reversed(path.split('/')[1:]):
        tokens = _name_tokens(segment)
        for role, vocabulary in SEMANTIC_TOKENS.items():
            if tokens & vocabulary:
                return role, segment
        if tokens & frozenset(('head',)) and tokens & frozenset(('up', 'mid', 'dn')):
            return 'Face', segment
    return 'Source Details', ''


def _classify_source_bones(rig: bpy.types.Object, direct_roles: Mapping[str, str],
                           hand_paths: set[str]) -> list[SourceFKControl]:
    """Describe direct source FK targets and keep technical or unknown nodes separate."""
    result: list[SourceFKControl] = []
    for bone in sorted(rig.data.bones, key=lambda item: item.name):
        raw_path = bone.get('source_transform_path')
        path = raw_path if isinstance(raw_path, str) else ''
        tokens = _name_tokens(path.rsplit('/', 1)[-1]) if path else frozenset()
        category, evidence = _semantic_role(path)
        technical = sorted(tokens & TECHNICAL_TOKENS)
        if bone.get('source_unskinned_transform') is True:
            role, reason = 'Source Details', 'source_unskinned_transform'
            category, evidence = role, path.rsplit('/', 1)[-1]
        elif path in direct_roles:
            role, reason = direct_roles[path], 'source_avatar_slot'
            category, evidence = role, path.rsplit('/', 1)[-1]
        elif technical:
            role, reason = 'Source Details', 'technical_name:' + ','.join(technical)
        elif path in hand_paths and tokens & frozenset(('finger', 'thumb', 'index', 'middle', 'ring', 'little', 'pinky')):
            role, reason = 'Hands', 'source_hand_subtree_finger'
            category, evidence = role, path.rsplit('/', 1)[-1]
        elif category != 'Source Details':
            role, reason = category, 'source_path_semantics'
        else:
            role, reason = 'Source Details', 'unclassified_source_path' if path else 'missing_source_path'
        parent_path = bone.parent.get('source_transform_path') if bone.parent is not None else None
        parent_category = _semantic_role(parent_path)[0] if isinstance(parent_path, str) else 'Source Details'
        if role == 'Source Details':
            kind = 'technical' if technical else 'unknown'
        elif role in ACCESSORY_ROLES:
            kind = 'chain_segment' if parent_category == role else 'chain_root'
        else:
            kind = 'source_fk'
        result.append({'bone': bone.name, 'path': path, 'role': role, 'category': category,
                       'control_kind': kind, 'reason': reason, 'evidence_segment': evidence})
    return result


def _slot_provenance(slot: int) -> tuple[str, int]:
    if slot < HUMAN_CORE_SLOT_COUNT:
        return 'm_HumanBoneIndex', slot
    if slot < 40:
        return 'm_LeftHand.m_HandBoneIndex', slot - 25
    return 'm_RightHand.m_HandBoneIndex', slot - 40


def _hand_subtree_paths(path_index: Mapping[str, bpy.types.Bone], slot_paths: Mapping[int, str]) -> set[str]:
    paths: set[str] = set()
    pending = [slot_paths[slot] for slot in sorted(HAND_ROOT_SLOTS & slot_paths.keys())]
    for path in pending:
        if path not in path_index:
            raise ValueError(f'Avatar hand root path is absent from the imported armature: {path}')
    active = list(pending)
    while active:
        path = active.pop()
        if path in paths:
            continue
        paths.add(path)
        bone = path_index[path]
        active.extend(child.get('source_transform_path') for child in bone.children
                      if isinstance(child.get('source_transform_path'), str))
    return paths


def _create_rig_collection(rig: bpy.types.Object) -> bpy.types.Collection:
    name = 'EF Authoring :: ' + rig.name
    collection = bpy.data.collections.get(name)
    if collection is None:
        collection = bpy.data.collections.new(name)
        bpy.context.scene.collection.children.link(collection)
    elif (collection.get(RIG_MARKER_PROPERTY) != FORMAT
          or collection.get(OWNER_PROPERTY) != rig.name):
        raise ValueError(f'Rig authoring collection name is already used by another collection: {name}')
    collection[RIG_MARKER_PROPERTY] = FORMAT
    collection[OWNER_PROPERTY] = rig.name
    if collection.name not in {item.name for item in bpy.context.scene.collection.children}:
        bpy.context.scene.collection.children.link(collection)
    return collection


def _create_child_collection(parent: bpy.types.Collection, rig: bpy.types.Object, role: str) -> bpy.types.Collection:
    name = role
    collection = bpy.data.collections.get(name)
    if collection is None:
        collection = bpy.data.collections.new(name)
    elif (collection.get(RIG_MARKER_PROPERTY) != FORMAT
          or collection.get(OWNER_PROPERTY) != rig.name
          or collection.get(ROLE_PROPERTY) != role):
        name = 'EF ' + rig.name + ' :: ' + role
        collection = bpy.data.collections.get(name)
        if collection is not None and (
                collection.get(RIG_MARKER_PROPERTY) != FORMAT
                or collection.get(OWNER_PROPERTY) != rig.name
                or collection.get(ROLE_PROPERTY) != role):
            raise ValueError(f'Rig authoring child collection name is already used: {name}')
        if collection is None:
            collection = bpy.data.collections.new(name)
    collection[RIG_MARKER_PROPERTY] = FORMAT
    collection[OWNER_PROPERTY] = rig.name
    collection[ROLE_PROPERTY] = role
    if collection.name not in {item.name for item in parent.children}:
        parent.children.link(collection)
    return collection


def _ensure_bone_collections(rig: bpy.types.Object, slot_paths: Mapping[int, str]) -> dict[str, bpy.types.BoneCollection]:
    data = rig.data
    names = {collection.name for collection in data.collections}
    marker = data.get(RIG_MARKER_PROPERTY)
    if marker not in (None, FORMAT):
        raise ValueError(f'Armature has a conflicting authoring marker: {rig.name}')
    collisions = names & set(ROLE_NAMES)
    if collisions and marker != FORMAT:
        raise ValueError(f'Armature already has unowned Bone Collections: {sorted(collisions)}')
    result = {name: data.collections.get(name) or data.collections.new(name) for name in ROLE_NAMES}
    data[RIG_MARKER_PROPERTY] = FORMAT
    for collection in result.values():
        collection.is_visible = collection.name in ('Body', 'Hands') or (not slot_paths and collection.name == 'Source Details')
    return result


def _move_object(obj: bpy.types.Object, collection: bpy.types.Collection) -> None:
    for previous in tuple(obj.users_collection):
        if previous != collection:
            previous.objects.unlink(obj)
    if obj.name not in collection.objects:
        collection.objects.link(obj)


def _expression_control(obj: bpy.types.Object) -> bool:
    return obj.get('expression_controls_format') is not None or obj.get('expression_material_controls') is not None


def _owned_by_rig(obj: bpy.types.Object, rig: bpy.types.Object) -> bool:
    # Imported NPC mesh containers can be siblings of the armature under an Empty root.
    import_root = rig
    while import_root.parent is not None and import_root.parent.type == 'EMPTY':
        if any(child.type == 'ARMATURE' and child != rig
               for child in import_root.parent.children_recursive):
            break
        import_root = import_root.parent
    if import_root != rig:
        ancestor = obj
        while ancestor is not None:
            if ancestor == import_root:
                return True
            ancestor = ancestor.parent
    parent = obj.parent
    visited: set[int] = set()
    while parent is not None:
        pointer = parent.as_pointer()
        if pointer in visited:
            raise ValueError(f'Cycle in object parent hierarchy at {obj.name}')
        visited.add(pointer)
        if parent == rig:
            return True
        parent = parent.parent
    rig_path = rig.get('source_transform_path')
    object_path = obj.get('source_transform_path')
    return (isinstance(rig_path, str) and rig_path
            and isinstance(object_path, str) and object_path.startswith(rig_path.rstrip('/') + '/'))


def _perpendicular_basis(axis: Vector) -> tuple[Vector, Vector]:
    unit = axis.normalized()
    reference = min((Vector((1.0, 0.0, 0.0)), Vector((0.0, 1.0, 0.0)), Vector((0.0, 0.0, 1.0))),
                    key=lambda value: abs(unit.dot(value)))
    first = unit.cross(reference).normalized()
    second = unit.cross(first).normalized()
    return first, second


def _ring_vertices(center: Vector, axis: Vector, radius_a: float, radius_b: float,
                   segments: int) -> tuple[Vector, ...]:
    first, second = _perpendicular_basis(axis)
    return tuple(center + first * (math.cos(index * math.tau / segments) * radius_a)
                 + second * (math.sin(index * math.tau / segments) * radius_b)
                 for index in range(segments))


def _local_skeleton_scale(bone: bpy.types.Bone) -> float:
    neighbor_distances = [float((item.head_local - bone.head_local).length)
                          for item in (bone.parent, *bone.children)
                          if item is not None
                          and float((item.head_local - bone.head_local).length) > 1e-5]
    if neighbor_distances:
        return min(neighbor_distances)
    armature = bone.id_data
    joint_distances = sorted(
        float((item.head_local - item.parent.head_local).length)
        for item in armature.bones
        if item.parent is not None
        and float((item.head_local - item.parent.head_local).length) > 1e-5)
    if not joint_distances:
        raise ValueError(f'Cannot derive a control-widget scale from zero-length armature: {armature.name}')
    return joint_distances[len(joint_distances) // 2]


def _direction_role_priority(role: str, candidate: bpy.types.Bone) -> int | None:
    """Prefer a same-chain child so nearby accessory branches cannot steer body controls."""
    if role == 'Body':
        preferred_roles = ('Body', 'Hands', 'Face')
    elif role == 'Hands' or role in ACCESSORY_ROLES or role == 'Source Details':
        preferred_roles = (role,)
    else:
        raise ValueError(f'Unknown source-bone role for widget direction: {role}')
    candidate_roles = {collection.name for collection in candidate.collections}
    return next((priority for priority, candidate_role in enumerate(preferred_roles)
                 if candidate_role in candidate_roles), None)


def _child_direction(bone: bpy.types.Bone, role: str) -> tuple[Vector, float]:
    """Use the nearest role-matched source joint, never Unity placeholder bone lengths."""
    inverse = bone.matrix_local.inverted()
    queue: list[tuple[bpy.types.Bone, int, float]] = []
    for child in bone.children:
        edge_distance = float((child.head_local - bone.head_local).length)
        queue.append((child, 1, edge_distance if edge_distance > 1e-5 else math.inf))

    while queue:
        depth = queue[0][1]
        level: list[tuple[bpy.types.Bone, int, float]] = []
        remaining: list[tuple[bpy.types.Bone, int, float]] = []
        for item in queue:
            (level if item[1] == depth else remaining).append(item)
        candidates: list[tuple[int, float, str, Vector, float]] = []
        for child, _child_depth, path_spacing in level:
            priority = _direction_role_priority(role, child)
            if priority is not None:
                vector = inverse.to_3x3() @ (child.head_local - bone.head_local)
                distance = float(vector.length)
                if distance > 1e-5:
                    candidates.append((priority, distance, child.name, vector.copy(), path_spacing))
            for grandchild in child.children:
                edge_distance = float((grandchild.head_local - child.head_local).length)
                next_spacing = min(path_spacing, edge_distance) if edge_distance > 1e-5 else path_spacing
                remaining.append((grandchild, depth + 1, next_spacing))
        if candidates:
            _priority, distance, _name, vector, path_spacing = min(
                candidates, key=lambda item: (item[0], item[1], item[2]))
            segment_scale = min(distance, path_spacing)
            return vector.normalized(), segment_scale
        queue = remaining

    if bone.parent is not None:
        parent_vector = inverse.to_3x3() @ (bone.head_local - bone.parent.head_local)
        parent_distance = float(parent_vector.length)
        if parent_distance > 1e-5:
            return parent_vector.normalized(), parent_distance

    axis = Vector((0.0, 1.0, 0.0))
    return axis, _local_skeleton_scale(bone)


def _face_joint_spacing(bone: bpy.types.Bone) -> float:
    """Bound face-widget size by nearby source joints and any available face-rig span."""
    face_bones = tuple(item for item in bone.id_data.bones
                       if any(collection.name == 'Face' for collection in item.collections))
    coordinates = tuple(item.head_local for item in face_bones)
    face_extent = (max(float(max(point[axis] for point in coordinates)
                           - min(point[axis] for point in coordinates))
                       for axis in range(3)) if len(coordinates) > 1 else 0.0)
    minimum_spacing = face_extent * 0.02 if face_extent > 1e-5 else 1e-5
    face_distances = sorted(
        float((item.head_local - bone.head_local).length)
        for item in face_bones
        if item != bone
        and float((item.head_local - bone.head_local).length) >= minimum_spacing)
    source_distances = sorted(
        float((item.head_local - bone.head_local).length)
        for item in bone.id_data.bones
        if item != bone
        and float((item.head_local - bone.head_local).length) >= minimum_spacing)
    spacing_candidates = face_distances or source_distances
    if not spacing_candidates:
        spacing_candidates = sorted(
            float((item.head_local - bone.head_local).length)
            for item in bone.id_data.bones
            if item != bone and float((item.head_local - bone.head_local).length) > 1e-5)
    if not spacing_candidates:
        raise ValueError(f'Cannot derive nearby source-joint spacing for face widget: {bone.name}')
    neighbor_spacing = spacing_candidates[0]
    return min(neighbor_spacing, face_extent * 0.15) if face_extent > 1e-5 else neighbor_spacing


def _hand_branch_ranks(hand_paths: set[str], path_index: Mapping[str, bpy.types.Bone],
                       slot_paths: Mapping[int, str]) -> dict[str, int]:
    roots = tuple(slot_paths[slot] for slot in sorted(HAND_ROOT_SLOTS & slot_paths.keys()))
    result: dict[str, int] = {}
    for root in roots:
        root_bone = path_index[root]
        children = [child for child in root_bone.children
                    if child.get('source_transform_path') in hand_paths]
        def position_key(child: bpy.types.Bone) -> tuple[float, float, float]:
            vector = root_bone.matrix_local.inverted() @ child.head_local
            return (float(vector.x), float(vector.y), float(vector.z))
        for rank, child in enumerate(sorted(children, key=position_key)):
            stack = [child]
            while stack:
                current = stack.pop()
                path = current.get('source_transform_path')
                if isinstance(path, str):
                    result[path] = rank
                stack.extend(item for item in current.children if item.get('source_transform_path') in hand_paths)
    return result


def _shape_geometry(bone: bpy.types.Bone, role: str, hand_root: bool, hand_rank: int,
                    hand_branch_count: int) -> tuple[tuple[tuple[float, float, float], ...], tuple[tuple[int, int], ...]]:
    axis, length = _child_direction(bone, role)
    if length <= 1e-5:
        raise ValueError(f'Cannot create a nonzero control widget for source bone: {bone.name}')
    start = Vector((0.0, 0.0, 0.0))
    scale = _face_joint_spacing(bone) if role == 'Face' else length
    end_scale = 0.3 if role == 'Face' else 0.5 if role == 'Clothing' else 1.0
    end = axis * (scale * end_scale)
    if role == 'Hands' and hand_root:
        radius = scale * 0.22
        fractions = (0.0, 0.34, 0.72, 1.0)
        profiles = ((radius * 0.62, radius), (radius, radius * 1.55),
                    (radius * 0.72, radius * 0.9), (radius * 0.56, radius * 0.7))
        segments = 8
    elif role == 'Hands':
        denominator = max(hand_branch_count - 1, 1)
        normalized_rank = hand_rank / denominator
        finger_scale = 0.72 + 0.5 * (1.0 - abs(2.0 * normalized_rank - 1.0))
        radius = scale * 0.105 * finger_scale
        fractions = (0.0, 0.42, 0.78, 1.0)
        profiles = ((radius * 0.72, radius * 0.9), (radius, radius),
                    (radius * 0.9, radius * 0.86), (radius * 0.66, radius * 0.72))
        segments = 6
    elif role == 'Body':
        radius = scale * 0.11
        fractions = (0.0, 0.16, 0.82, 1.0)
        profiles = ((radius * 0.7, radius * 0.85), (radius, radius * 1.25),
                    (radius * 0.78, radius), (radius * 0.55, radius * 0.68))
        segments = 8
    elif role in ACCESSORY_ROLES:
        radius = scale * (0.10 if role in ('Face', 'Clothing') else 0.18)
        fractions = (0.0, 0.24, 0.72)
        profiles = ((radius, radius), (radius * 0.8, radius), (radius * 0.55, radius * 0.65))
        segments = {'Hair': 4, 'Face': 12, 'Ears': 3, 'Tail': 8,
                    'Clothing': 6, 'Accessories': 5}[role]
    else:
        radius = scale * 0.075
        fractions = (0.0, 0.5, 1.0)
        profiles = ((radius, radius), (radius * 0.72, radius * 0.72), (radius, radius))
        segments = 6

    rings = [_ring_vertices(start.lerp(end, fraction), axis, radii[0], radii[1], segments)
             for fraction, radii in zip(fractions, profiles, strict=True)]
    vertices = tuple(tuple(float(component) for component in vertex) for ring in rings for vertex in ring)
    edges: list[tuple[int, int]] = []
    for ring_index in range(len(rings)):
        base = ring_index * segments
        for index in range(segments):
            edges.append((base + index, base + ((index + 1) % segments)))
    for ring_index in range(len(rings) - 1):
        start_index = ring_index * segments
        next_index = (ring_index + 1) * segments
        for index in range(segments):
            edges.append((start_index + index, next_index + index))
    for end_index in range(segments // 2):
        start_index = end_index
        target_index = (len(rings) - 1) * segments + ((end_index + segments // 2) % segments)
        edges.append((start_index, target_index))
    return vertices, tuple(edges)


def _clear_generated_shapes(rig: bpy.types.Object) -> tuple[bpy.types.Object, ...]:
    generated = tuple(obj for obj in bpy.data.objects
                      if obj.get(SHAPE_PROPERTY) == FORMAT and obj.get(OWNER_PROPERTY) == rig.name)
    generated_set = set(generated)
    for pose_bone in rig.pose.bones:
        if pose_bone.custom_shape in generated_set:
            pose_bone.custom_shape = None
        elif pose_bone.custom_shape is not None:
            raise ValueError(f'Armature has a custom shape not owned by this authoring layer: {pose_bone.name}')
    for obj in generated:
        mesh = obj.data if obj.type == 'MESH' else None
        bpy.data.objects.remove(obj, do_unlink=True)
        if mesh is not None and mesh.users == 0:
            bpy.data.meshes.remove(mesh)
    return generated


def _create_shape(rig: bpy.types.Object, bone: bpy.types.Bone, role: str, hand_root: bool,
                  rank: int, branch_count: int, collection: bpy.types.Collection) -> bpy.types.Object:
    vertices, edges = _shape_geometry(bone, role, hand_root, rank, branch_count)
    mesh = bpy.data.meshes.new('EF Rig Shape Mesh :: ' + bone.name)
    mesh.from_pydata(vertices, edges, [])
    mesh.update()
    obj = bpy.data.objects.new('EF Rig Shape :: ' + rig.name + ' :: ' + bone.name, mesh)
    collection.objects.link(obj)
    obj[SHAPE_PROPERTY] = FORMAT
    obj[OWNER_PROPERTY] = rig.name
    obj['source_bone_name'] = bone.name
    obj['source_transform_path'] = bone.get('source_transform_path', '')
    obj['control_shape_role'] = role
    obj.hide_render = True
    obj.hide_set(True)
    return obj


def _assign_bone_collections(rig: bpy.types.Object, collections: Mapping[str, bpy.types.BoneCollection],
                             roles: Mapping[str, str]) -> None:
    for bone in rig.data.bones:
        for collection in collections.values():
            if bone.name in collection.bones:
                collection.unassign(bone)
        collections[roles[bone.name]].assign(bone)


def apply_rig_authoring(rig: bpy.types.Object, slot_paths: Mapping[int, str] | None) -> dict[str, object]:
    """Create FK display shapes and organize source-authored objects without rig edits."""
    if rig.type != 'ARMATURE' or rig.data is None:
        raise TypeError('Rig authoring requires an Armature object')
    scene_rigs = tuple(obj for obj in bpy.context.scene.objects if obj.type == 'ARMATURE')
    if len(scene_rigs) != 1:
        raise ValueError(f'Rig authoring currently requires the only Armature in the scene; found {len(scene_rigs)}')
    if scene_rigs[0] != rig:
        raise ValueError(f'Rig authoring target is not the only scene Armature: {rig.name}')
    if rig.data.is_editmode:
        raise RuntimeError(f'Exit Edit Mode before authoring the rig display: {rig.name}')
    slots, mapping_origin = _slot_paths(slot_paths, rig)
    path_index = _path_index(rig)
    for slot, path in slots.items():
        if path not in path_index:
            raise ValueError(f'Avatar source slot {slot} path is absent from armature {rig.name}: {path}')

    direct_roles: dict[str, str] = {}
    slot_rows: list[dict[str, object]] = []
    for slot, path in sorted(slots.items()):
        role = _bone_role(slot)
        previous = direct_roles.get(path)
        if previous is not None and previous != role:
            raise ValueError(f'Avatar slots assign one source bone to conflicting collections: {path}')
        direct_roles[path] = role
        source_field, source_index = _slot_provenance(slot)
        slot_rows.append({'slot': slot, 'slot_name': SLOT_NAMES[slot], 'path': path,
                          'bone': path_index[path].name, 'role': role,
                          'source_field': source_field, 'source_index': source_index})

    hand_paths = _hand_subtree_paths(path_index, slots)
    classified = _classify_source_bones(rig, direct_roles, hand_paths)
    roles_by_name = {row['bone']: row['role'] for row in classified}
    classified_by_name = {row['bone']: row for row in classified}
    hand_ranks = _hand_branch_ranks(hand_paths, path_index, slots)
    branch_count = max(hand_ranks.values(), default=0) + 1
    unmapped = [
        {'bone': bone.name, 'path': bone.get('source_transform_path')}
        for bone in sorted(rig.data.bones, key=lambda item: item.name)
        if roles_by_name[bone.name] == 'Source Details'
    ]

    rest_before = _rest_signature(rig)
    actions_before = _action_signature(rig)
    constraints_before = _constraint_signature(rig)
    geometry_objects = tuple(obj for obj in bpy.context.scene.objects
                             if obj.type == 'MESH' and obj.get('source_geometry_path'))
    geometry_before = _source_geometry_signature(geometry_objects, {})
    source_geometry_before = _source_geometry_signature(
        geometry_objects, _potential_source_visibility(geometry_objects))
    rig_collection = _create_rig_collection(rig)
    expression_collection = _create_child_collection(rig_collection, rig, 'Expression Controls')
    helper_collection = _create_child_collection(rig_collection, rig, 'Technical Helpers')
    geometry_collection = _create_child_collection(rig_collection, rig, 'Source Geometry')
    widget_collection = _create_child_collection(rig_collection, rig, 'Rig Widgets')
    widget_collection.hide_render = True

    bone_collections = _ensure_bone_collections(rig, slots)
    _assign_bone_collections(rig, bone_collections, roles_by_name)
    rig.data.display_type = 'STICK'
    rig.data.show_names = False
    rig.data.show_bone_custom_shapes = True
    rig.show_in_front = True

    expression_controls: list[str] = []
    technical_helpers: list[str] = []
    source_geometry: list[str] = []
    for obj in tuple(bpy.data.objects):
        if obj.type == 'EMPTY' and obj.get(SHAPE_PROPERTY) != FORMAT:
            if _expression_control(obj):
                _move_object(obj, expression_collection)
                expression_controls.append(obj.name)
            elif _owned_by_rig(obj, rig):
                _move_object(obj, helper_collection)
                obj.hide_set(True)
                technical_helpers.append(obj.name)
        elif obj in geometry_objects:
            _move_object(obj, geometry_collection)
            source_geometry.append(obj.name)

    _clear_generated_shapes(rig)
    hand_root_paths = {slots[slot] for slot in HAND_ROOT_SLOTS if slot in slots}
    shape_names: dict[str, str] = {}
    for bone in rig.data.bones:
        role = roles_by_name[bone.name]
        rank = hand_ranks.get(bone.get('source_transform_path'), 0)
        hand_root = bone.get('source_transform_path') in hand_root_paths
        shape = _create_shape(rig, bone, role, hand_root, rank, branch_count, widget_collection)
        pose_bone = rig.pose.bones[bone.name]
        pose_bone.custom_shape = shape
        pose_bone.custom_shape_scale_xyz = (1.0, 1.0, 1.0)
        pose_bone.use_custom_shape_bone_size = False
        color = ROLE_COLORS[role]
        pose_bone.color.palette = 'CUSTOM'
        pose_bone.color.custom.normal = color
        pose_bone.color.custom.select = tuple(min(component * 0.6 + 0.4, 1.0) for component in color)
        pose_bone.color.custom.active = (1.0, 1.0, 1.0)
        if (role in ACCESSORY_ROLES and role not in ('Face', 'Clothing')
                and classified_by_name[bone.name]['control_kind'] == 'chain_root'):
            pose_bone.custom_shape_scale_xyz = (1.25, 1.25, 1.25)
        pose_bone['endfield_fk_category'] = role
        pose_bone['endfield_fk_control_kind'] = classified_by_name[bone.name]['control_kind']
        shape_names[bone.name] = shape.name

    saved_slots = {str(slot): path for slot, path in sorted(slots.items())}
    rig[SLOT_PATHS_PROPERTY] = json.dumps(saved_slots, sort_keys=True, separators=(',', ':'))
    rig[RIG_MARKER_PROPERTY] = FORMAT
    if _rest_signature(rig) != rest_before:
        raise AssertionError('Rig rest matrices or source bone identity changed during authoring setup')
    if _action_signature(rig) != actions_before:
        raise AssertionError('Active source Action changed during authoring setup')
    if _constraint_signature(rig) != constraints_before:
        raise AssertionError('Source pose constraints changed during authoring setup')
    if _source_geometry_signature(geometry_objects, {}) != geometry_before:
        raise AssertionError('Source mesh layout or visibility changed during authoring setup')

    report: dict[str, object] = {
        'format': FORMAT,
        'authoring_revision': AUTHORING_REVISION,
        'armature': rig.name,
        'mapping_status': mapping_origin,
        'mapped_slots': slot_rows,
        'mapped_bones': sorted({row['bone'] for row in slot_rows}),
        'hierarchy_classified_hand_bones': sorted(
            row['bone'] for row in classified if row['reason'] == 'source_hand_subtree_finger'),
        'source_fk_controls': classified,
        'unknown_classification': [row for row in classified if row['control_kind'] == 'unknown'],
        'technical_source_bones': [row for row in classified if row['control_kind'] == 'technical'],
        'classification_policy': 'Exact Avatar slots, then named source-path semantics; unknown and technical nodes remain accessible in Source Details. No source-bone renaming, parenting, or binding changes.',
        'control_colors': {role: list(color) for role, color in ROLE_COLORS.items()},
        'unmapped_bones': unmapped,
        'bone_collections': {name: sorted(bone.name for bone in rig.data.bones if bone.name in collection.bones)
                             for name, collection in bone_collections.items()},
        'custom_shape_count': len(shape_names),
        'shape_objects': [shape_names[name] for name in sorted(shape_names)],
        'scene_collections': {
            'root': rig_collection.name,
            'expression_controls': expression_collection.name,
            'technical_helpers': helper_collection.name,
            'source_geometry': geometry_collection.name,
            'rig_widgets': widget_collection.name,
        },
        'expression_controls': sorted(expression_controls),
        'hidden_helpers': sorted(technical_helpers),
        'source_geometry': sorted(source_geometry),
        'rest_signature': rest_before,
        'action_signature': actions_before,
        'constraint_signature': constraints_before,
        'source_geometry_signature': source_geometry_before,
        'verified': True,
    }
    verify_rig_authoring(report)
    rig['endfield_rig_authoring_report'] = json.dumps(report, ensure_ascii=False, sort_keys=True)
    return report


def verify_rig_authoring(report: Mapping[str, object]) -> dict[str, object]:
    """Validate saved authoring links and source-data signatures after reopen."""
    if report.get('format') != FORMAT:
        raise ValueError('Unsupported rig authoring report format')
    rig_name = _text(report.get('armature'), 'rig authoring report.armature')
    rig = bpy.data.objects.get(rig_name)
    if rig is None or rig.type != 'ARMATURE':
        raise AssertionError(f'Authoring report armature is missing: {rig_name}')
    if rig.get(RIG_MARKER_PROPERTY) != FORMAT or rig.data.get(RIG_MARKER_PROPERTY) != FORMAT:
        raise AssertionError(f'Armature authoring marker did not survive save/reopen: {rig_name}')
    if _rest_signature(rig) != report.get('rest_signature'):
        raise AssertionError(f'Armature rest signature changed: {rig_name}')
    if _action_signature(rig) != report.get('action_signature'):
        raise AssertionError(f'Active Action signature changed: {rig_name}')
    if _constraint_signature(rig) != report.get('constraint_signature'):
        raise AssertionError(f'Pose constraint signature changed: {rig_name}')
    source_geometry = _registered_source_geometry(report)
    if _source_geometry_signature(source_geometry, _potential_source_visibility(source_geometry)) != report.get('source_geometry_signature'):
        raise AssertionError(f'Source geometry signature changed: {rig_name}')
    if (rig.data.display_type != 'STICK' or rig.data.show_names
            or not rig.data.show_bone_custom_shapes or not rig.show_in_front):
        raise AssertionError(f'Armature authoring display settings are incomplete: {rig_name}')

    expected_collections = _record(report.get('bone_collections'), 'report.bone_collections')
    revision = report.get('authoring_revision', 1)
    if type(revision) is not int or revision not in (1, AUTHORING_REVISION):
        raise ValueError(f'Unsupported rig authoring revision: {revision!r}')
    expected_roles = LEGACY_ROLE_NAMES if revision == 1 else ROLE_NAMES
    for role in expected_roles:
        collection = rig.data.collections.get(role)
        expected_bones = _rows(expected_collections.get(role), f'report.bone_collections.{role}')
        if collection is None:
            raise AssertionError(f'Bone Collection is missing: {role}')
        actual_bones = sorted(bone.name for bone in rig.data.bones if bone.name in collection.bones)
        if actual_bones != sorted(_text(name, f'report.bone_collections.{role}[]') for name in expected_bones):
            raise AssertionError(f'Bone Collection membership differs from authoring report: {role}')
        if (revision == 1 and role == 'Source Details'
                and collection.is_visible != (not bool(_rows(report.get('mapped_slots'), 'report.mapped_slots')))):
            raise AssertionError('Source Details visibility differs from mapped/unmapped rig state')

    if revision == AUTHORING_REVISION:
        controls = _rows(report.get('source_fk_controls'), 'report.source_fk_controls')
        expected_names = {bone.name for bone in rig.data.bones}
        control_names: set[str] = set()
        for index, value in enumerate(controls):
            row = _record(value, f'report.source_fk_controls[{index}]')
            name = _text(row.get('bone'), f'FK control[{index}].bone')
            role = _text(row.get('role'), f'FK control[{index}].role')
            if name in control_names or name not in expected_names or role not in ROLE_NAMES:
                raise AssertionError(f'Invalid or duplicate direct FK control: {name}, {role}')
            control_names.add(name)
            bone = rig.data.bones[name]
            if row.get('path') != bone.get('source_transform_path', '') or name not in rig.data.collections[role].bones:
                raise AssertionError(f'Direct FK control differs from its source bone: {name}')
            pose_bone = rig.pose.bones[name]
            if (pose_bone.get('endfield_fk_category') != role
                    or pose_bone.get('endfield_fk_control_kind') != row.get('control_kind')
                    or pose_bone.color.palette != 'CUSTOM'
                    or any(abs(actual - expected) > (1.0 / 255.0 + 1e-5) for actual, expected in
                           zip(pose_bone.color.custom.normal, ROLE_COLORS[role], strict=True))):
                raise AssertionError(f'FK control category or display color differs: {name}')
        if control_names != expected_names:
            raise AssertionError(f'Direct FK classification does not cover every source bone: {rig_name}')

    expected_slot_paths: dict[str, str] = {}
    for index, raw_slot in enumerate(_rows(report.get('mapped_slots'), 'report.mapped_slots')):
        slot_row = _record(raw_slot, f'report.mapped_slots[{index}]')
        slot = _integer(slot_row.get('slot'), f'report.mapped_slots[{index}].slot')
        path = _text(slot_row.get('path'), f'report.mapped_slots[{index}].path')
        expected_slot_paths[str(slot)] = path
    saved_slot_paths = _record(json.loads(str(rig.get(SLOT_PATHS_PROPERTY, '{}'))), 'saved rig authoring slot paths')
    if saved_slot_paths != expected_slot_paths:
        raise AssertionError(f'Persisted exact source-Avatar mapping differs from report: {rig_name}')

    shape_names = tuple(_text(name, 'report.shape_objects[]') for name in _rows(report.get('shape_objects'), 'report.shape_objects'))
    if len(shape_names) != len(rig.data.bones) or report.get('custom_shape_count') != len(shape_names):
        raise AssertionError(f'Custom-shape count does not match source bone count: {rig_name}')
    for bone_name, shape_name in zip(sorted(bone.name for bone in rig.data.bones), shape_names, strict=True):
        pose_bone = rig.pose.bones[bone_name]
        shape = bpy.data.objects.get(shape_name)
        if (shape is None or shape.type != 'MESH' or shape.get(SHAPE_PROPERTY) != FORMAT
                or shape.get(OWNER_PROPERTY) != rig.name or pose_bone.custom_shape != shape
                or pose_bone.use_custom_shape_bone_size or not shape.hide_render or not shape.hide_get()):
            raise AssertionError(f'Custom shape assignment is missing or incomplete: {bone_name}')
    scene_collections = _record(report.get('scene_collections'), 'report.scene_collections')
    collection_roles = {
        'root': None,
        'expression_controls': 'Expression Controls',
        'technical_helpers': 'Technical Helpers',
        'source_geometry': 'Source Geometry',
        'rig_widgets': 'Rig Widgets',
    }
    found_collections: dict[str, bpy.types.Collection] = {}
    for key, role in collection_roles.items():
        name = _text(scene_collections.get(key), f'report.scene_collections.{key}')
        collection = bpy.data.collections.get(name)
        if (collection is None or collection.get(RIG_MARKER_PROPERTY) != FORMAT
                or collection.get(OWNER_PROPERTY) != rig.name
                or (role is not None and collection.get(ROLE_PROPERTY) != role)):
            raise AssertionError(f'Rig authoring object collection is missing or unowned: {name}')
        found_collections[key] = collection
    widget_collection = found_collections['rig_widgets']
    if not widget_collection.hide_render:
        raise AssertionError('Rig widget collection participates in render')
    for name in shape_names:
        if name not in widget_collection.objects:
            raise AssertionError(f'Custom shape is outside the hidden widget collection: {name}')
    helper_names = {_text(name, 'report.hidden_helpers[]') for name in _rows(report.get('hidden_helpers'), 'report.hidden_helpers')}
    for name in helper_names:
        helper = bpy.data.objects.get(name)
        if (helper is None or helper.type != 'EMPTY' or not helper.hide_get()
                or name not in found_collections['technical_helpers'].objects):
            raise AssertionError(f'Technical helper is visible or missing: {name}')
    expression_names = {_text(name, 'report.expression_controls[]') for name in _rows(report.get('expression_controls'), 'report.expression_controls')}
    for name in expression_names:
        controller = bpy.data.objects.get(name)
        if (controller is None or controller.type != 'EMPTY' or not _expression_control(controller)
                or name not in found_collections['expression_controls'].objects):
            raise AssertionError(f'Expression controller is missing or unclassified: {name}')
    geometry_names = {obj.name for obj in source_geometry}
    for name in geometry_names:
        geometry = bpy.data.objects.get(name)
        if (geometry is None or geometry.type != 'MESH' or not geometry.get('source_geometry_path')
                or name not in found_collections['source_geometry'].objects):
            raise AssertionError(f'Source geometry is missing or unclassified: {name}')
    if report.get('verified') is not True:
        raise AssertionError('Rig authoring report is not marked verified')
    return {'format': FORMAT, 'status': 'verified', 'armature': rig.name,
            'bones': len(rig.data.bones), 'custom_shapes': len(shape_names),
            'expression_controls': len(expression_names), 'hidden_helpers': len(helper_names),
            'source_geometry': len(geometry_names), 'unmapped_bones': len(_rows(report.get('unmapped_bones'), 'report.unmapped_bones'))}
