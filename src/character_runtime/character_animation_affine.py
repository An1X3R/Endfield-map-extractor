"""Represent sampled affine bone targets with two editable TRS objects."""
from __future__ import annotations

from dataclasses import dataclass
from itertools import permutations, product

import bpy
from mathutils import Matrix, Quaternion
import numpy as np
from numpy.typing import NDArray


SPACE_PROPERTY = 'source_affine_target_space'
PARENT_SPACE = 'source_parent_bone/1'
ARMATURE_SPACE = 'armature/1'


@dataclass(frozen=True)
class AffineTarget:
    bone_name: str
    left: bpy.types.Object
    right: bpy.types.Object
    constraint_name: str


@dataclass(frozen=True)
class AffineFactors:
    left: Matrix
    right: Matrix
    left_axes: NDArray[np.float64]
    right_axes: NDArray[np.float64]


def trs_residual(matrix: Matrix) -> float:
    position, rotation, scale = matrix.decompose()
    reconstructed = Matrix.LocRotScale(position, rotation, scale)
    return max(abs(matrix[row][column] - reconstructed[row][column])
               for row in range(4) for column in range(4))


def split_affine(matrix: Matrix, previous: AffineFactors | None) -> AffineFactors:
    values = np.asarray(matrix, dtype=np.float64)
    if not np.isfinite(values).all() or not np.allclose(values[3], (0., 0., 0., 1.), atol=1e-7):
        raise ValueError('Affine target must be finite with a homogeneous last row')
    left, scale, right = np.linalg.svd(values[:3, :3])
    if np.min(scale) < 1e-9:
        raise ValueError('Singular affine target cannot be represented by invertible TRS helpers')
    left_sign = float(np.linalg.det(left))
    right_sign = float(np.linalg.det(right))
    left[:, 2] *= left_sign
    right[2, :] *= right_sign
    scale[2] *= left_sign * right_sign
    # Paired axis permutations/signs preserve the product and avoid SVD relabeling jumps.
    reference_left = np.eye(3) if previous is None else previous.left_axes
    reference_right = np.eye(3) if previous is None else previous.right_axes
    choices: list[tuple[float, NDArray[np.float64], NDArray[np.float64], NDArray[np.float64]]] = []
    for order in permutations(range(3)):
        for signs in product((-1., 1.), repeat=3):
            candidate_left = left[:, order] * np.asarray(signs)
            candidate_right = right[list(order), :] * np.asarray(signs)[:, None]
            if np.linalg.det(candidate_left) < 0. or np.linalg.det(candidate_right) < 0.:
                continue
            cost = float(np.sum((candidate_left-reference_left)**2) + np.sum((candidate_right-reference_right)**2))
            choices.append((cost, candidate_left, scale[list(order)], candidate_right))
    _, left, scale, right = min(choices, key=lambda choice: choice[0])
    left_matrix = np.eye(4)
    left_matrix[:3, :3] = left * scale
    left_matrix[:3, 3] = values[:3, 3]
    right_matrix = np.eye(4)
    right_matrix[:3, :3] = right
    if np.max(np.abs(left_matrix @ right_matrix - values)) > 1e-8:
        raise ArithmeticError('SVD affine factorization failed its product check')
    return AffineFactors(Matrix(left_matrix.tolist()), Matrix(right_matrix.tolist()), left.copy(), right.copy())


def create_affine_targets(armature: bpy.types.Object, bone_names: tuple[str, ...]) -> tuple[AffineTarget, ...]:
    if not bone_names:
        return ()
    collection_name = armature.name + ' Source Affine Targets'
    if bpy.data.collections.get(collection_name) is not None:
        raise ValueError(f'Affine target collection already exists: {collection_name}')
    collection = bpy.data.collections.new(collection_name)
    bpy.context.scene.collection.children.link(collection)
    result = []
    for name in bone_names:
        left = bpy.data.objects.new(armature.name + ' :: ' + name + ' :: affine left', None)
        right = bpy.data.objects.new(armature.name + ' :: ' + name + ' :: affine right', None)
        for obj in (left, right):
            collection.objects.link(obj)
            obj.empty_display_size = .015
            obj.hide_render = True
            obj.rotation_mode = 'QUATERNION'
            obj['source_affine_bone'] = name
            obj['source_affine_armature'] = armature.name
        constraint = armature.pose.bones[name].constraints.new('COPY_TRANSFORMS')
        constraint.name = 'Source affine target'
        constraint.target = right
        constraint.owner_space = 'WORLD'
        constraint.target_space = 'WORLD'
        constraint.mix_mode = 'REPLACE'
        helper = AffineTarget(name, left, right, constraint.name)
        bind_affine_parent(armature, helper)
        result.append(helper)
    return tuple(result)


def bind_affine_parent(armature: bpy.types.Object, helper: AffineTarget) -> None:
    """Bind the source-parent frame; existing helper keys must be rebaked afterward."""
    parent = armature.data.bones[helper.bone_name].parent
    if parent is not None and parent.use_relative_parent:
        raise ValueError(f'Affine target requires ordinary bone parenting: armature={armature.name}, '
                         f'bone={helper.bone_name}, parent={parent.name}, use_relative_parent=True')
    helper.left.parent = armature
    helper.left.parent_type = 'OBJECT' if parent is None else 'BONE'
    helper.left.parent_bone = '' if parent is None else parent.name
    helper.left.matrix_parent_inverse = (Matrix.Identity(4) if parent is None
                                        else Matrix.Translation((0., -parent.length, 0.)))
    helper.right.parent = helper.left
    helper.right.parent_type = 'OBJECT'
    helper.right.parent_bone = ''
    helper.right.matrix_parent_inverse = Matrix.Identity(4)
    for obj in (helper.left, helper.right):
        obj[SPACE_PROPERTY] = ARMATURE_SPACE if parent is None else PARENT_SPACE


def affine_target_matrix(helper: AffineTarget, targets: dict[str, Matrix]) -> Matrix:
    """Express the source target relative to its editable source-parent pose."""
    target = targets[helper.bone_name]
    if helper.left.parent_type == 'BONE':
        return targets[helper.left.parent_bone].inverted() @ target
    return target


def assign_affine_target(helper: AffineTarget, factors: AffineFactors) -> None:
    for obj, matrix in ((helper.left, factors.left), (helper.right, factors.right)):
        obj.location, obj.rotation_quaternion, obj.scale = matrix.decompose()


def key_affine_target(helper: AffineTarget, factors: AffineFactors, frame: int,
                      previous_rotations: dict[str, Quaternion]) -> dict[str, Quaternion]:
    assign_affine_target(helper, factors)
    next_rotations = dict(previous_rotations)
    for obj in (helper.left, helper.right):
        rotation = obj.rotation_quaternion.copy()
        if obj.name in previous_rotations:
            rotation.make_compatible(previous_rotations[obj.name])
        next_rotations[obj.name] = rotation.copy()
        obj.rotation_quaternion = rotation
        for channel in ('location', 'rotation_quaternion', 'scale'):
            obj.keyframe_insert(data_path=channel, frame=frame, group=helper.bone_name)
        action = obj.animation_data.action
        if action is None:
            raise RuntimeError(f'Affine helper did not create an Action: {obj.name}')
        action.name = obj.name + ' :: sampled source target'
        action.use_fake_user = True
    return next_rotations


def finish_affine_targets(helpers: tuple[AffineTarget, ...], frames: int) -> list[dict[str, object]]:
    result: list[dict[str, object]] = []
    for helper in helpers:
        objects = []
        for obj in (helper.left, helper.right):
            action = obj.animation_data.action if obj.animation_data else None
            if action is None or len(action.fcurves) != 10 or any(len(curve.keyframe_points) != frames for curve in action.fcurves):
                raise ValueError(f'Affine helper Action dimensions do not match the source: {obj.name}')
            for curve in action.fcurves:
                for point in curve.keyframe_points:
                    point.interpolation = 'LINEAR'
            objects.append({'object': obj.name, 'action': action.name})
        result.append({'bone_name': helper.bone_name, 'constraint': helper.constraint_name,
                       'parent_bone': helper.left.parent_bone or None,
                       'target_space': helper.left[SPACE_PROPERTY],
                       'collection': helper.left.users_collection[0].name, 'objects': objects})
    return result


def validate_affine_targets(armature: bpy.types.Object, records: list[object], frames: int) -> None:
    for record in records:
        if not isinstance(record, dict):
            raise TypeError('Affine helper receipt row must be an object')
        bone = armature.pose.bones[str(record['bone_name'])]
        constraint = bone.constraints.get(str(record['constraint']))
        objects = record['objects']
        if not isinstance(objects, list) or len(objects) != 2:
            raise ValueError('Affine target requires exactly two recorded objects')
        resolved = []
        for row in objects:
            if not isinstance(row, dict):
                raise TypeError('Affine target object record must be an object')
            obj = bpy.data.objects[str(row['object'])]
            action = obj.animation_data.action if obj.animation_data else None
            if action is None or action.name != row['action'] or len(action.fcurves) != 10 or any(len(curve.keyframe_points) != frames for curve in action.fcurves):
                raise ValueError(f'Saved affine helper Action is missing or differs: {obj.name}')
            resolved.append(obj)
        parent = bone.parent
        if parent is not None and parent.bone.use_relative_parent:
            raise ValueError(f'Saved affine target parent uses relative parenting: {bone.name}/{parent.name}')
        parent_type = 'OBJECT' if parent is None else 'BONE'
        parent_name = '' if parent is None else parent.name
        target_space = ARMATURE_SPACE if parent is None else PARENT_SPACE
        identity = Matrix.Identity(4)
        parent_inverse = Matrix.Identity(4) if parent is None else Matrix.Translation((0., -parent.bone.length, 0.))
        if (resolved[0].parent != armature or resolved[0].parent_type != parent_type
                or resolved[0].parent_bone != parent_name
                or any(obj.get(SPACE_PROPERTY) != target_space for obj in resolved)
                or record.get('target_space') != target_space or record.get('parent_bone') != (parent_name or None)
                or max(abs(resolved[0].matrix_parent_inverse[row][column] - parent_inverse[row][column])
                       for row in range(4) for column in range(4)) > 1e-6
                or resolved[1].parent != resolved[0] or resolved[1].parent_type != 'OBJECT'
                or max(abs(resolved[1].matrix_parent_inverse[row][column] - identity[row][column])
                       for row in range(4) for column in range(4)) > 1e-6
                or constraint is None or constraint.type != 'COPY_TRANSFORMS' or constraint.target != resolved[1]
                or constraint.mute or constraint.influence != 1. or constraint.owner_space != 'WORLD'
                or constraint.target_space != 'WORLD' or constraint.mix_mode != 'REPLACE'):
            raise ValueError(f'Saved affine target constraint differs: {bone.name}')
