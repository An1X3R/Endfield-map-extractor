"""Mount fixed effect snapshots to exact source bones without resimulating particles."""
from __future__ import annotations

from collections.abc import Sequence
import json
import math

import bpy
from mathutils import Matrix


FORMAT = 'EndfieldEffectSnapshotAttachment/1'
MARKER = 'endfield_effect_snapshot_attachment'


def _resolve_bone(rig: bpy.types.Object, reference: str) -> bpy.types.PoseBone:
    if rig.type != 'ARMATURE' or rig.pose is None:
        raise TypeError(f'Effect attachment requires an Armature: {rig.name}')
    if not isinstance(reference, str) or not reference.strip():
        raise ValueError('Effect attachment requires a nonempty exact bone name or source path')
    matches = (tuple(bone for bone in rig.pose.bones if bone.bone.get('source_transform_path') == reference)
               if '/' in reference else tuple(bone for bone in rig.pose.bones if bone.name == reference))
    if len(matches) != 1:
        raise ValueError(f'Effect attachment bone must resolve exactly once: rig={rig.name}, '
                         f'reference={reference}, matches={len(matches)}')
    return matches[0]


def _source_flag(value: object, name: str) -> int:
    if type(value) is not int or value not in (0, 1):
        raise ValueError(f'Effect attachment requires a source 0/1 value: field={name}, value={value!r}')
    return value


def _require_invertible(matrix: Matrix, context: str) -> None:
    if any(not math.isfinite(value) for row in matrix for value in row) or abs(matrix.determinant()) < 1e-12:
        raise ValueError(f'Effect attachment requires a finite invertible transform: {context}')


def _constraint_types(follow_rotation: int, follow_scale: int) -> tuple[str, ...]:
    if follow_rotation == 1 and follow_scale == 1:
        return ('COPY_TRANSFORMS',)
    return ('COPY_LOCATION',) + (('COPY_ROTATION',) if follow_rotation == 1 else ()) + (
        ('COPY_SCALE',) if follow_scale == 1 else ())


def attach_effect_snapshot(rig: bpy.types.Object, bone_reference: str, follow_rotation: int,
                           follow_scale: int, objects: Sequence[bpy.types.Object]) -> bpy.types.Object:
    """Keep the current snapshot world geometry while mounting future source-bone edits."""
    bone = _resolve_bone(rig, bone_reference)
    rotation = _source_flag(follow_rotation, 'followRotation')
    scale = _source_flag(follow_scale, 'followScale')
    scene = bpy.context.scene
    snapshots = tuple(objects)
    if scene.objects.get(rig.name) != rig or not snapshots or len({obj.as_pointer() for obj in snapshots}) != len(snapshots):
        raise ValueError(f'Effect attachment requires a scene rig and nonempty unique snapshots: rig={rig.name}')
    ancestors: set[bpy.types.Object] = set()
    current: bpy.types.Object | None = rig
    while current is not None:
        ancestors.add(current)
        current = current.parent
    bpy.context.view_layer.update()
    original_worlds: dict[str, Matrix] = {}
    original_bases: dict[str, Matrix] = {}
    for obj in snapshots:
        if scene.objects.get(obj.name) != obj or obj in ancestors:
            raise ValueError(f'Effect snapshot is outside the scene or would create a rig dependency cycle: {obj.name}')
        if obj.parent is not None and obj.parent.get(MARKER) is not None:
            raise ValueError(f'Effect snapshot is already mounted: object={obj.name}, anchor={obj.parent.name}')
        original_worlds[obj.name] = obj.matrix_world.copy()
        original_bases[obj.name] = obj.matrix_basis.copy()
        _require_invertible(original_worlds[obj.name], obj.name + '.matrix_world')
        _require_invertible(original_bases[obj.name], obj.name + '.matrix_basis')
    anchor = bpy.data.objects.new(rig.name + ' :: ' + bone.name + ' :: effect snapshot', None)
    scene.collection.objects.link(anchor)
    anchor.hide_render = True
    anchor.empty_display_size = .025
    anchor.rotation_mode = 'QUATERNION'
    anchor[MARKER] = FORMAT
    anchor['effect_attachment_rig'] = rig.name
    anchor['effect_attachment_bone'] = bone.name
    anchor['effect_attachment_reference'] = bone_reference
    anchor['source_follow_rotation'] = rotation
    anchor['source_follow_scale'] = scale
    anchor['effect_snapshot_objects'] = json.dumps([obj.name for obj in snapshots])
    anchor['scope'] = 'Fixed particle snapshot follows the source mount; historical particle simulation is not replayed.'
    for kind in _constraint_types(rotation, scale):
        constraint = anchor.constraints.new(kind)
        constraint.name = 'Source effect mount :: ' + kind
        constraint.target = rig
        constraint.subtarget = bone.name
        constraint.owner_space = 'WORLD'
        constraint.target_space = 'WORLD'
        if kind == 'COPY_TRANSFORMS':
            constraint.mix_mode = 'REPLACE'
        elif kind == 'COPY_ROTATION':
            constraint.mix_mode = 'REPLACE'
        else:
            constraint.use_offset = False
        if kind != 'COPY_TRANSFORMS':
            constraint.use_x = True
            constraint.use_y = True
            constraint.use_z = True
        if kind in ('COPY_TRANSFORMS', 'COPY_LOCATION'):
            constraint.head_tail = 0.
    bpy.context.view_layer.update()
    _require_invertible(anchor.matrix_world, anchor.name)
    anchor_inverse = anchor.matrix_world.inverted()
    for obj in snapshots:
        obj.parent = anchor
        obj.parent_type = 'OBJECT'
        obj.parent_bone = ''
        obj.matrix_parent_inverse = anchor_inverse @ original_worlds[obj.name] @ original_bases[obj.name].inverted()
        obj.matrix_basis = original_bases[obj.name]
    bpy.context.view_layer.update()
    for obj in snapshots:
        error = max(abs(obj.matrix_world[row][column] - original_worlds[obj.name][row][column])
                    for row in range(4) for column in range(4))
        if error > 2e-5:
            raise ValueError(f'Effect snapshot changed during keep-world mounting: object={obj.name}, error={error}')
    validate_effect_attachment(anchor, rig, snapshots)
    return anchor


def validate_effect_attachment(anchor: bpy.types.Object, rig: bpy.types.Object,
                               objects: Sequence[bpy.types.Object]) -> None:
    """Require the saved source flags, exact target bone, and all snapshot parent links."""
    if anchor.type != 'EMPTY' or anchor.get(MARKER) != FORMAT or not anchor.hide_render or anchor.parent is not None:
        raise ValueError(f'Effect snapshot anchor metadata or object state differs: {anchor.name}')
    reference = anchor.get('effect_attachment_reference')
    if not isinstance(reference, str):
        raise TypeError(f'Effect snapshot anchor lacks an exact source bone reference: {anchor.name}')
    bone = _resolve_bone(rig, reference)
    if anchor.get('effect_attachment_rig') != rig.name or anchor.get('effect_attachment_bone') != bone.name:
        raise ValueError(f'Effect snapshot anchor source identity differs: {anchor.name}')
    rotation = _source_flag(anchor.get('source_follow_rotation'), 'followRotation')
    scale = _source_flag(anchor.get('source_follow_scale'), 'followScale')
    expected = _constraint_types(rotation, scale)
    if tuple(constraint.type for constraint in anchor.constraints) != expected:
        raise ValueError(f'Effect snapshot source-follow constraints differ: anchor={anchor.name}, expected={expected}')
    for constraint in anchor.constraints:
        if (constraint.target != rig or constraint.subtarget != bone.name or constraint.mute
                or constraint.influence != 1. or constraint.owner_space != 'WORLD' or constraint.target_space != 'WORLD'
                or (constraint.type in ('COPY_TRANSFORMS', 'COPY_ROTATION') and constraint.mix_mode != 'REPLACE')
                or (constraint.type in ('COPY_LOCATION', 'COPY_SCALE') and constraint.use_offset)
                or (constraint.type in ('COPY_TRANSFORMS', 'COPY_LOCATION') and constraint.head_tail != 0.)
                or (constraint.type != 'COPY_TRANSFORMS' and not (constraint.use_x and constraint.use_y and constraint.use_z))):
            raise ValueError(f'Effect snapshot constraint target or settings differ: anchor={anchor.name}, constraint={constraint.name}')
    snapshots = tuple(objects)
    if anchor.get('effect_snapshot_objects') != json.dumps([obj.name for obj in snapshots]):
        raise ValueError(f'Effect snapshot object identities differ: {anchor.name}')
    for obj in snapshots:
        if obj.parent != anchor or obj.parent_type != 'OBJECT' or obj.parent_bone:
            raise ValueError(f'Effect snapshot parent link differs: anchor={anchor.name}, object={obj.name}')
