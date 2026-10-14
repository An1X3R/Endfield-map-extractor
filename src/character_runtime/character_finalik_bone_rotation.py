"""Source FinalIK normal-basis bone calibration and world rotation.

Only the proven normal Unity LookRotation branch is supported. Degenerate
orientation fallbacks and Transform parent-space writes remain separate.
"""
from __future__ import annotations

from dataclasses import dataclass
import math
from typing import cast

from character_finalik_bend_direction import cross, squared_length, unit
from character_finalik_grounder import Vector3
from character_finalik_trigonometric_inputs import vector
from character_runtime_ik_weights import _input_float32, _round_float32

Quaternion = tuple[float, float, float, float]


@dataclass(frozen=True)
class FinalIKBoneCalibration:
    target_to_local_space: Quaternion
    default_local_bend_normal: Vector3


def quaternion(value: Quaternion, field: str) -> Quaternion:
    if not isinstance(value, tuple) or len(value) != 4:
        raise ValueError(f'Expected four quaternion components: field={field}')
    result = cast(Quaternion, tuple(_input_float32(item) for item in value))
    if abs(sum(item * item for item in result) - 1.) > 1e-4:
        raise ValueError(f'Expected native unit world quaternion: field={field}')
    return result


def multiply(left: Quaternion, right: Quaternion) -> Quaternion:
    """Port GameAssembly1832FBC20 ordered float32 Hamilton product."""
    f = _round_float32
    ax, ay, az, aw = left
    bx, by, bz, bw = right
    return (f(f(f(f(bw * ax) + f(aw * bx)) + f(ay * bz)) - f(az * by)),
            f(f(f(f(ay * bw) + f(aw * by)) + f(az * bx)) - f(ax * bz)),
            f(f(f(f(az * bw) + f(aw * bz)) + f(ax * by)) - f(ay * bx)),
            f(f(f(f(aw * bw) - f(ax * bx)) - f(ay * by)) - f(az * bz)))


def inverse(value: Quaternion) -> Quaternion:
    """UnityPlayer Inverse_Injected conjugates its unit quaternion input."""
    return (-value[0], -value[1], -value[2], value[3])


def rotate_bend_normal(rotation: Quaternion, local_normal: Vector3) -> Vector3:
    """Port182E11E90 quaternion-vector multiplication in original f32 order."""
    x, y, z, w = quaternion(rotation, 'rotation')
    vx, vy, vz = vector(local_normal, 'local_normal')
    f = _round_float32
    x2, y2, z2 = f(x + x), f(y + y), f(z + z)
    xx, yy, zz = f(x2 * x), f(y2 * y), f(z2 * z)
    xy, xz, yz = f(y2 * x), f(z2 * x), f(z2 * y)
    wx, wy, wz = f(x2 * w), f(y2 * w), f(z2 * w)
    return (f(f(f(f(1. - f(zz + yy)) * vx) + f(f(xy - wz) * vy)) + f(f(wy + xz) * vz)),
            f(f(f(f(1. - f(zz + xx)) * vy) + f(f(wz + xy) * vx)) + f(f(yz - wx) * vz)),
            f(f(f(f(wx + yz) * vy) + f(f(xz - wy) * vx)) + f(f(1. - f(yy + xx)) * vz)))


def look_rotation(direction: Vector3, up_direction: Vector3) -> Quaternion:
    """Port UnityPlayer normal LookRotation basis/matrix-quaternion path."""
    f = _round_float32
    forward = unit(vector(direction, 'direction'), 'direction')
    right = unit(cross(vector(up_direction, 'up_direction'), forward), 'up_direction')
    up = cross(forward, right)
    if abs(f(squared_length(up) - 1.)) > f(1e-6):
        raise ValueError('Native LookRotation nonorthogonal fallback is not verified')
    matrix = right + up + forward
    trace = f(f(matrix[4] + matrix[0]) + matrix[8])
    if trace > 0.:
        root = f(math.sqrt(f(trace + 1.)))
        scale = f(.5 / root)
        result = (f(f(matrix[5] - matrix[7]) * scale),
                  f(f(matrix[6] - matrix[2]) * scale),
                  f(f(matrix[1] - matrix[3]) * scale), f(root * .5))
    else:
        index = 1 if matrix[4] > matrix[0] else 0
        index = 2 if matrix[8] > matrix[index * 4] else index
        middle, last = (index + 1) % 3, (index + 2) % 3
        root = f(math.sqrt(f(f(f(matrix[index * 4] - matrix[middle * 4]) - matrix[last * 4]) + 1.)))
        scale = f(.5 / root)
        # Each component has one explicit source index; constructing this local
        # result does not modify the caller's matrix or calibration.
        components = [0., 0., 0., 0.]
        components[index] = f(root * .5)
        components[3] = f(f(matrix[last + middle * 3] - matrix[middle + last * 3]) * scale)
        components[middle] = f(f(matrix[middle + index * 3] + matrix[index + middle * 3]) * scale)
        components[last] = f(f(matrix[last + index * 3] + matrix[index + last * 3]) * scale)
        result = cast(Quaternion, tuple(components))
    norm = f(math.sqrt(f(f(f(f(result[0] * result[0]) + f(result[1] * result[1]))
        + f(result[2] * result[2])) + f(result[3] * result[3]))))
    return cast(Quaternion, tuple(f(component / norm) for component in result))


def calibrate_bone_rotation(initial_position: Vector3, child_position: Vector3,
                            initial_rotation: Quaternion, bend_normal: Vector3) -> FinalIKBoneCalibration:
    """Port TrigonometricBone.Initiate's targetToLocalSpace+80 write."""
    position = vector(initial_position, 'initial_position')
    child = vector(child_position, 'child_position')
    rotation = quaternion(initial_rotation, 'initial_rotation')
    normal = vector(bend_normal, 'bend_normal')
    direction = cast(Vector3, tuple(_round_float32(end - start)
        for start, end in zip(position, child, strict=True)))
    target = look_rotation(direction, normal)
    return FinalIKBoneCalibration(inverse(multiply(inverse(rotation), target)),
        rotate_bend_normal(inverse(rotation), normal))


def get_bone_world_rotation(direction: Vector3, bend_normal: Vector3,
                            calibration: FinalIKBoneCalibration) -> Quaternion:
    """Port TrigonometricBone.GetRotation; output is a world quaternion."""
    if not isinstance(calibration, FinalIKBoneCalibration):
        raise TypeError('Expected explicit FinalIKBoneCalibration')
    target_to_local = quaternion(calibration.target_to_local_space, 'target_to_local_space')
    return multiply(look_rotation(direction, bend_normal), target_to_local)
