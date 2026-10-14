"""Source Grounder post-solver foot rotation with explicit floor observations.

The source calf quaternion is read after limb solving. Saved foot rotation is
captured before solving; the external Grounding solver supplies rotationOffset
and floorFeetThetaByFoot/Logic. Terrain queries and smoothing are not inferred.
"""
from __future__ import annotations

from dataclasses import dataclass
import math
from typing import cast

from character_animation_goals import LocalTransform, GoalRig, _worlds, _write_rotation
from character_finalik_bone_rotation import Quaternion, inverse, multiply, quaternion
from character_finalik_end_rotation import slerp_world_rotation
from character_runtime_ik_weights import _input_float32, _round_float32

Euler = tuple[float, float, float]


@dataclass(frozen=True)
class GrounderFootLimits:
    max_angle_fore: float
    max_angle_back: float
    max_angle_in: float
    max_angle_out: float


@dataclass(frozen=True)
class GrounderFootPostInputs:
    current_after_solver: Quaternion
    saved_before_solver: Quaternion
    calf_after_solver: Quaternion
    grounding_rotation_offset: Quaternion
    grounder_weight: float
    maintain_pelvis_foot_weight: float
    floor_feet_theta_by_foot: float
    floor_feet_theta_by_logic: float
    limits: GrounderFootLimits
    enabled: bool


@dataclass(frozen=True)
class GrounderFootPostResult:
    rotation: Quaternion
    world_writes: tuple[Quaternion, ...]
    relative_euler_before_degrees: Euler | None
    relative_euler_after_degrees: Euler | None
    limit_relaxation: float | None


def euler_radians(rotation: Quaternion) -> Euler:
    """Port UnityPlayer1801AA1F0/1801AACA0 rotation-order-4 conversion.

    Native CRT transcendental rounding is compared independently, not asserted
    bitwise identical to Python's rounded libm implementation.
    """
    q = quaternion(rotation, 'Euler.rotation')
    f = _round_float32
    norm = f(math.sqrt(f(f(f(f(q[0] * q[0]) + f(q[1] * q[1])) + f(q[2] * q[2])) + f(q[3] * q[3]))))
    x, y, z, w = (f(component / norm) for component in q)
    xx, yy, zz, ww = (f(component * component) for component in (x, y, z, w))
    yz, wx, wz, xy, wy, xz = f(y * z), f(w * x), f(w * z), f(x * y), f(w * y), f(x * z)
    negative_sine_x = f(f(yz - wx) * 2.)
    euler_x = f(-f(math.asin(min(1., max(-1., negative_sine_x)))))
    if abs(f(yz - wx)) < f(.4999989867210388):
        denominator_z = f(f(f(yy - zz) - xx) + ww)
        denominator_y = f(f(f(zz - xx) - yy) + ww)
        euler_z = f(math.atan2(f(f(wz + xy) * 2.), denominator_z))
        euler_y = f(math.atan2(f(f(wy + xz) * 2.), denominator_y))
    else:
        a, b, c, d = f(yz + wx), f(wz + xy), f(wx - yz), f(xy - wz)
        euler_y = f(math.atan2(f(f(d * c) + f(a * b)), f(f(a * c) - f(d * b))))
        euler_z = 0.
    return euler_x, euler_y, euler_z


def rotation_from_euler_radians(value: Euler) -> Quaternion:
    """Port UnityPlayer1800F6270 order 4: qY * qX * qZ."""
    if not isinstance(value, tuple) or len(value) != 3:
        raise ValueError('Expected three explicit Euler radians')
    f = _round_float32
    half = tuple(f(_input_float32(component) * .5) for component in value)
    sx, sy, sz = (f(math.sin(component)) for component in half)
    cx, cy, cz = (f(math.cos(component)) for component in half)
    return multiply(multiply((0., sy, 0., cy), (sx, 0., 0., cx)), (0., 0., sz, cz))


def positive_euler_degrees(value: Euler) -> Euler:
    """Port managed Quaternion.Internal_MakePositive182F815E0."""
    f = _round_float32
    degrees = tuple(f(_input_float32(component) * f(57.295780181884766)) for component in value)
    return cast(Euler, tuple(f(component + 360.) if component < f(-.005729577969759703)
                            else f(component - 360.) if component > f(359.9942626953125)
                            else component for component in degrees))


def signed_degrees(value: float) -> float:
    """Port Grounder.NormalizeAngle's floor-and-clamp float32 expression."""
    f = _round_float32
    shifted = f(_input_float32(value) + 180.)
    multiple = f(math.floor(f(shifted / 360.)))
    remainder = f(shifted - f(multiple * 360.))
    return f(min(360., max(0., remainder)) - 180.)


def foot_limit_relaxation(foot_theta: float, logic_theta: float, grounder_weight: float) -> float:
    """Use exact runtime Grounding theta fields, not static geometry fields."""
    f = _round_float32
    theta = max(abs(f(_input_float32(foot_theta) * .5)), abs(_input_float32(logic_theta)))
    source_factor = min(1., max(0., f(f(theta - 25.) / -20.)))
    weight = min(1., max(0., _input_float32(grounder_weight)))
    return min(1., max(0., f(f(f(source_factor - 1.) * weight) + 1.)))


def clamp_relative_euler(value: Euler, limits: GrounderFootLimits, factor: float) -> Euler:
    """Clamp relative Y/Z, retaining relative X around the calf forward axis."""
    if not isinstance(limits, GrounderFootLimits):
        raise TypeError('Expected exact source GrounderFootLimits')
    angles = tuple(_input_float32(angle) for angle in (
        limits.max_angle_fore, limits.max_angle_back, limits.max_angle_in, limits.max_angle_out))
    if any(angle < 0. for angle in angles):
        raise ValueError('Source max foot rotation limits must be nonnegative')
    f = _round_float32
    weight = min(1., max(0., _input_float32(factor)))
    fore, back, inward, outward = angles
    minimum_y = f(f(f(-45. - (-inward)) * weight) + (-inward))
    maximum_y = f(f(f(45. - outward) * weight) + outward)
    minimum_z = f(f(f(-60. - (-fore)) * weight) + (-fore))
    maximum_z = f(f(f(60. - back) * weight) + back)
    return value[0], min(maximum_y, max(minimum_y, value[1])), min(maximum_z, max(minimum_z, value[2]))


def prepare_grounder_foot_post(inputs: GrounderFootPostInputs) -> GrounderFootPostResult:
    """Match OnPostSolverUpdate: offset * saved foot, clamp in solved calf space."""
    if not isinstance(inputs, GrounderFootPostInputs) or type(inputs.enabled) is not bool:
        raise TypeError('Grounder post requires explicit typed source inputs and enabled state')
    current = quaternion(inputs.current_after_solver, 'current_after_solver')
    weight = _input_float32(inputs.grounder_weight)
    if not inputs.enabled or weight <= 0.:
        return GrounderFootPostResult(current, (), None, None, None)
    saved = quaternion(inputs.saved_before_solver, 'saved_before_solver')
    calf = quaternion(inputs.calf_after_solver, 'calf_after_solver')
    offset = quaternion(inputs.grounding_rotation_offset, 'grounding_rotation_offset')
    scale = _round_float32(_input_float32(inputs.maintain_pelvis_foot_weight) * weight)
    delta = slerp_world_rotation((0., 0., 0., 1.), offset, scale)
    desired = multiply(delta, saved)
    relative = multiply(inverse(calf), desired)
    before = cast(Euler, tuple(signed_degrees(component) for component in positive_euler_degrees(euler_radians(relative))))
    factor = foot_limit_relaxation(inputs.floor_feet_theta_by_foot, inputs.floor_feet_theta_by_logic, weight)
    after = clamp_relative_euler(before, inputs.limits, factor)
    radians = cast(Euler, tuple(_round_float32(component * _round_float32(.01745329238474369)) for component in after))
    rotation = multiply(calf, rotation_from_euler_radians(radians))
    return GrounderFootPostResult(rotation, (rotation,), before, after, factor)


def write_grounder_foot_post(rig: GoalRig, locals_: tuple[LocalTransform, ...],
                             foot_index: int, result: GrounderFootPostResult) -> tuple[LocalTransform, ...]:
    """Write the native world result using the existing source rig conversion."""
    if type(foot_index) is not int or foot_index not in rig.required_order or len(locals_) != len(rig.parents):
        raise ValueError('Grounder foot post requires an exact compiled source foot index')
    if not result.world_writes:
        return locals_
    worlds = _worlds(rig, locals_)
    parent = rig.parents[foot_index]
    parent_rotation = (0., 0., 0., 1.) if parent < 0 else worlds[parent].rotation
    return _write_rotation(locals_, foot_index, result.rotation, parent_rotation)
