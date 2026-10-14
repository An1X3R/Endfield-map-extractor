"""Solve source Humanoid goals with deterministic axis/plane rotation transport.

This preserves source goals and bone lengths; it does not reproduce native solver
weights, joint limits, shoulder policies, or post-IK twist redistribution.
The Ruri segment mass model guides hips recovery inside exact leg-reach bounds;
its remaining mass-center error is reported rather than treated as native truth.
"""
from __future__ import annotations

from dataclasses import dataclass, replace
import math
from typing import Literal

Vec3 = tuple[float, float, float]
Quat = tuple[float, float, float, float]


@dataclass(frozen=True)
class LocalTransform:
    position: Vec3
    rotation: Quat
    scale: Vec3


@dataclass(frozen=True)
class LimbSpec:
    name: str
    upper: int
    middle: int
    end: int
    post_rotation: Quat
    bottom_offset: Vec3
    shoulder: int | None


@dataclass(frozen=True)
class GoalSample:
    name: str
    position: Vec3
    rotation: Quat


@dataclass(frozen=True)
class HumanMassBone:
    slot: int
    index: int
    mass: float


@dataclass(frozen=True)
class _MassPoint:
    index: int
    weight: float


@dataclass(frozen=True)
class GoalRig:
    parents: tuple[int, ...]
    animator_index: int
    hips_index: int
    limbs: tuple[LimbSpec, ...]
    required_order: tuple[int, ...]
    mass_points: tuple[_MassPoint, ...]
    orientation_nodes: tuple[int, int, int, int]


@dataclass(frozen=True)
class LimbDiagnostic:
    name: str
    target: Vec3
    target_rotation: Quat
    before_error_m: float
    scale_corrected_fk_error_m: float
    after_error_m: float
    after_rotation_error_degrees: float
    shoulder_swing_degrees: float
    unreachable_distance_m: float
    source_plane_sine: float
    axis_transport_degrees: float


@dataclass(frozen=True)
class SolvedFrame:
    locals: tuple[LocalTransform, ...]
    diagnostics: tuple[LimbDiagnostic, ...]
    mass_diagnostic: MassDiagnostic


@dataclass(frozen=True)
class MassDiagnostic:
    target: Vec3
    before_error_m: float
    after_error_m: float
    iterations: int
    correction_world_m: Vec3
    convergence_tolerance_m: float
    projected_step_m: float
    body_orientation_error_degrees: float
    evaluations: int
    termination: Literal['budget','refinement_resolution','residual_threshold']


class GoalConvergenceError(ArithmeticError):
    """Body recovery encountered an invalid or empty geometric constraint."""


@dataclass(frozen=True)
class _World:
    position: Vec3
    rotation: Quat
    scale: float


@dataclass(frozen=True)
class _BodyTrial:
    locals: tuple[LocalTransform, ...]
    diagnostics: tuple[LimbDiagnostic, ...]
    hips: Vec3
    mass_error_m: float


def _add(a: Vec3, b: Vec3) -> Vec3:
    return (a[0]+b[0], a[1]+b[1], a[2]+b[2])


def _sub(a: Vec3, b: Vec3) -> Vec3:
    return (a[0]-b[0], a[1]-b[1], a[2]-b[2])


def _mul(a: Vec3, factor: float) -> Vec3:
    return (a[0]*factor, a[1]*factor, a[2]*factor)


def _dot(a: Vec3, b: Vec3) -> float:
    return sum(x*y for x, y in zip(a, b, strict=True))


def _cross(a: Vec3, b: Vec3) -> Vec3:
    return (a[1]*b[2]-a[2]*b[1], a[2]*b[0]-a[0]*b[2], a[0]*b[1]-a[1]*b[0])


def _unit(a: Vec3) -> Vec3:
    length = math.hypot(*a)
    if length == 0. or not math.isfinite(length):
        raise ValueError('Source-goal direction or plane has zero/nonfinite magnitude')
    return _mul(a, 1./length)


def _normalize(q: Quat) -> Quat:
    length = math.hypot(*q)
    if length == 0. or not math.isfinite(length):
        raise ValueError('Source-goal quaternion has zero/nonfinite magnitude')
    return (q[0]/length, q[1]/length, q[2]/length, q[3]/length)


def _inverse(q: Quat) -> Quat:
    return (-q[0], -q[1], -q[2], q[3])


def _qmul(a: Quat, b: Quat) -> Quat:
    ax, ay, az, aw = a
    bx, by, bz, bw = b
    return _normalize((aw*bx+ax*bw+ay*bz-az*by,
                       aw*by-ax*bz+ay*bw+az*bx,
                       aw*bz+ax*by-ay*bx+az*bw,
                       aw*bw-ax*bx-ay*by-az*bz))


def _rotate(q: Quat, value: Vec3) -> Vec3:
    vector = (q[0], q[1], q[2])
    first = _cross(vector, value)
    return _add(value, _add(_mul(first, 2.*q[3]), _mul(_cross(vector, first), 2.)))


def _angle(a: Quat, b: Quat) -> float:
    delta = _qmul(_inverse(a), b)
    return math.degrees(2.*math.atan2(math.hypot(*delta[:3]), abs(delta[3])))


def _axis_rotation(axis: Vec3, radians: float) -> Quat:
    direction = _unit(axis)
    sine = math.sin(radians*.5)
    return _normalize((direction[0]*sine,direction[1]*sine,direction[2]*sine,math.cos(radians*.5)))


def _minimum_rotation(source: Vec3, target: Vec3, source_plane: Vec3) -> Quat:
    first, second = _unit(source), _unit(target)
    axis = _cross(first, second)
    sine = math.hypot(*axis)
    cosine = _dot(first, second)
    if sine == 0.:
        return (0.,0.,0.,1.) if cosine > 0. else _axis_rotation(source_plane, math.pi)
    return _axis_rotation(axis, math.atan2(sine, cosine))


def _frame_rotation(axis: Vec3, normal: Vec3) -> Quat:
    x = _unit(axis)
    z = _unit(_sub(normal, _mul(x, _dot(normal, x))))
    y = _unit(_cross(z, x))
    m00, m10, m20 = x
    m01, m11, m21 = y
    m02, m12, m22 = z
    trace = m00+m11+m22
    if trace > 0.:
        s = math.sqrt(trace+1.)*2.
        return _normalize(((m21-m12)/s,(m02-m20)/s,(m10-m01)/s,s*.25))
    if m00 > m11 and m00 > m22:
        s = math.sqrt(1.+m00-m11-m22)*2.
        return _normalize((s*.25,(m01+m10)/s,(m02+m20)/s,(m21-m12)/s))
    if m11 > m22:
        s = math.sqrt(1.+m11-m00-m22)*2.
        return _normalize(((m01+m10)/s,s*.25,(m12+m21)/s,(m02-m20)/s))
    s = math.sqrt(1.+m22-m00-m11)*2.
    return _normalize(((m02+m20)/s,(m12+m21)/s,s*.25,(m10-m01)/s))


def _transport(source_axis: Vec3, source_plane: Vec3, target_axis: Vec3, target_plane: Vec3, rotation: Quat) -> Quat:
    delta = _qmul(_frame_rotation(target_axis,target_plane),_inverse(_frame_rotation(source_axis,source_plane)))
    return _qmul(delta, rotation)


def _project_two_balls(point: Vec3, first: Vec3, first_radius: float,
                       second: Vec3, second_radius: float, source_point: Vec3) -> Vec3:
    """Project hips onto the two outer-reach balls, without relaxing leg lengths.

    Unequal-length legs also have an inner unreachable hole. This projection
    does not solve that nonconvex region; _solve_limb reports it explicitly.
    """
    if min(first_radius,second_radius) <= 0.:
        raise ValueError('Body reach projection requires positive leg lengths')
    if math.dist(point,first) <= first_radius and math.dist(point,second) <= second_radius:
        return point
    def project(center: Vec3, radius: float) -> Vec3:
        offset = _sub(point,center)
        distance = math.hypot(*offset)
        return point if distance <= radius else _add(center,_mul(offset,radius/distance))
    candidate = project(first,first_radius)
    if math.dist(candidate,second) <= second_radius:
        return candidate
    candidate = project(second,second_radius)
    if math.dist(candidate,first) <= first_radius:
        return candidate
    separation = _sub(second,first)
    distance = math.hypot(*separation)
    if distance > first_radius+second_radius:
        raise ArithmeticError('Source foot targets have disjoint no-stretch hips reach balls: '+str(distance-first_radius-second_radius)+' m')
    if distance == 0. or distance < abs(first_radius-second_radius):
        raise ArithmeticError('Nested body reach balls unexpectedly rejected their smaller-ball projection')
    axis = _mul(separation,1./distance)
    along = (first_radius*first_radius-second_radius*second_radius+distance*distance)/(2.*distance)
    circle_center = _add(first,_mul(axis,along))
    radius_squared = first_radius*first_radius-along*along
    if radius_squared < -1e-12*first_radius*first_radius:
        raise ArithmeticError('Body reach circle has a negative squared radius')
    radius = math.sqrt(max(0.,radius_squared))
    if radius == 0.:
        return circle_center
    radial = _sub(point,circle_center)
    radial = _sub(radial,_mul(axis,_dot(radial,axis)))
    if math.hypot(*radial) == 0.:
        # A symmetric circle has equal-distance solutions; the source hips defines its side.
        radial = _sub(source_point,circle_center)
        radial = _sub(radial,_mul(axis,_dot(radial,axis)))
    return _add(circle_center,_mul(_unit(radial),radius))


def _check_vector(value: Vec3, context: str) -> None:
    if len(value) != 3 or not all(math.isfinite(v) for v in value):
        raise ValueError('Expected three finite source-goal components: '+context)


def _check_quaternion(value: Quat, context: str) -> None:
    if len(value) != 4 or not all(math.isfinite(v) for v in value) or abs(math.hypot(*value)-1.) > 1e-4:
        raise ValueError('Expected a finite unit source-goal quaternion: '+context)


def _compile_mass_points(bones: tuple[HumanMassBone, ...]) -> tuple[tuple[_MassPoint, ...],tuple[int,int,int,int]]:
    # Exact coefficients from current CharacterAnimationReader/AvatarMuscleReferential.cs:18-35.
    formula = {0:((1,1/3),(2,1/3),(7,1/3)),1:((1,.5),(3,.5)),2:((2,.5),(4,.5)),
               3:((3,.5),(5,.5)),4:((4,.5),(6,.5)),7:((7,.5),(8,.5)),8:((8,.5),(9,.5)),
               9:((9,.25),(10,.25),(12,.25),(13,.25)),10:((10,.5),(11,.5)),
               12:((12,.5),(14,.5)),13:((13,.5),(15,.5)),14:((14,.5),(16,.5)),
               15:((15,.5),(17,.5)),16:((16,.5),(18,.5)),17:((17,.5),(19,.5))}
    slots = {bone.slot:bone for bone in bones}
    if len(slots) != len(bones) or any(b.slot < 0 or b.slot >= 25 or not math.isfinite(b.mass) for b in bones):
        raise ValueError('Human mass records need unique slots 0..24 and finite source masses')
    total_mass = sum(b.mass for b in bones if b.mass >= 0.)
    if total_mass <= 0.:
        raise ValueError('Human source mass total must be positive')
    weights: dict[int,float] = {}
    for bone in bones:
        if bone.mass <= 0.:
            continue
        for slot,factor in formula.get(bone.slot,((bone.slot,1.),)):
            if slot not in slots:
                raise ValueError('Human mass formula needs unmapped source slot '+str(slot))
            index = slots[slot].index
            weights[index] = weights.get(index,0.)+bone.mass*factor/total_mass
    if any(slot not in slots for slot in (14,15,1,2)):
        raise ValueError('Human body orientation needs exact upper-arm/upper-leg slots')
    return tuple(_MassPoint(index,weight) for index,weight in sorted(weights.items())),tuple(slots[s].index for s in (14,15,1,2))


def compile_goal_rig(parents: tuple[int, ...], animator_index: int, hips_index: int, limbs: tuple[LimbSpec, ...],
                     human_mass_bones: tuple[HumanMassBone, ...]) -> GoalRig:
    names = {'LeftHand','RightHand','LeftFoot','RightFoot'}
    if len(limbs) != 4 or {limb.name for limb in limbs} != names:
        raise ValueError('Source-goal rig requires exactly four named hand/foot goals')
    if not parents or any(p < -1 or p >= len(parents) for p in parents):
        raise ValueError('Source-goal hierarchy contains an out-of-range parent')
    mass_points,orientation_nodes = _compile_mass_points(human_mass_bones)
    if not any(b.slot == 0 and b.index == hips_index for b in human_mass_bones):
        raise ValueError('Human mass hips slot does not match the source body root')
    needed = {animator_index,hips_index,*orientation_nodes,*(p.index for p in mass_points)}
    for limb in limbs:
        _check_quaternion(limb.post_rotation,limb.name+' PostQ')
        _check_vector(limb.bottom_offset,limb.name+' foot offset')
        indices = (limb.upper,limb.middle,limb.end)
        if any(i < 0 or i >= len(parents) for i in indices):
            raise ValueError('Source-goal limb index is out of range: '+limb.name)
        if parents[limb.end] != limb.middle or parents[limb.middle] != limb.upper:
            raise ValueError('Source-goal limb must map its exact direct two-bone hierarchy: '+limb.name)
        needed.update(indices)
        if limb.shoulder is not None:
            if not limb.name.endswith('Hand') or parents[limb.upper] != limb.shoulder:
                raise ValueError('Mapped shoulder is not the direct parent of its hand upper arm: '+limb.name)
            needed.add(limb.shoulder)
    if any(i < 0 or i >= len(parents) for i in needed):
        raise ValueError('Source-goal Animator/hips/shoulder index is out of range')
    ordered: list[int] = []
    active: set[int] = set()
    resolved: set[int] = set()
    def visit(index: int) -> None:
        if index in resolved:
            return
        if index in active:
            raise ValueError('Source-goal hierarchy contains a cycle')
        active.add(index)
        if parents[index] >= 0:
            visit(parents[index])
        active.remove(index)
        resolved.add(index)
        ordered.append(index)
    for index in sorted(needed):
        visit(index)
    if animator_index not in resolved:
        raise ValueError('Source-goal Animator cannot be resolved')
    for index in needed - {animator_index}:
        ancestor = parents[index]
        while ancestor >= 0 and ancestor != animator_index:
            ancestor = parents[ancestor]
        if ancestor != animator_index:
            raise ValueError('Source-goal driven node is outside its Animator hierarchy: '+str(index))
    return GoalRig(parents,animator_index,hips_index,limbs,tuple(ordered),mass_points,orientation_nodes)


def _worlds(rig: GoalRig, locals_: tuple[LocalTransform, ...]) -> dict[int, _World]:
    result: dict[int,_World] = {}
    for index in rig.required_order:
        local = locals_[index]
        _check_vector(local.position,'local position '+str(index))
        _check_quaternion(local.rotation,'local rotation '+str(index))
        _check_vector(local.scale,'local scale '+str(index))
        sx,sy,sz = local.scale
        # Serialized unit scale can carry last-bit float32 differences.
        if min(sx,sy,sz) <= 0. or not math.isclose(sx,sy,rel_tol=2e-6,abs_tol=0.) or not math.isclose(sx,sz,rel_tol=2e-6,abs_tol=0.):
            raise ValueError('Source-goal ancestor/limb has nonuniform or nonpositive scale; affine/shear solving is unsupported at node '+str(index))
        parent = _World((0.,0.,0.),(0.,0.,0.,1.),1.) if rig.parents[index] < 0 else result[rig.parents[index]]
        result[index] = _World(_add(parent.position,_rotate(parent.rotation,_mul(local.position,parent.scale))),
                               _qmul(parent.rotation,_normalize(local.rotation)),parent.scale*sx)
    return result


def _write_rotation(locals_: tuple[LocalTransform, ...], index: int, rotation: Quat, parent_rotation: Quat) -> tuple[LocalTransform, ...]:
    replacement = replace(locals_[index],rotation=_qmul(_inverse(parent_rotation),rotation))
    return tuple(replacement if i == index else value for i,value in enumerate(locals_))


def _shoulder_reach(rig: GoalRig, locals_: tuple[LocalTransform, ...], limb: LimbSpec, target: Vec3,
                    worlds: dict[int,_World]) -> tuple[tuple[LocalTransform, ...],float]:
    if limb.shoulder is None:
        return locals_,0.
    pivot,a,b,c = (worlds[i].position for i in (limb.shoulder,limb.upper,limb.middle,limb.end))
    length = math.dist(a,b)+math.dist(b,c)
    if math.dist(target,a) <= length:
        return locals_,0.
    original,direction = _sub(a,pivot),_sub(target,pivot)
    radius,distance = math.hypot(*original),math.hypot(*direction)
    if min(radius,distance) == 0.:
        raise ValueError('Mapped shoulder/goal has zero radius or distance: '+limb.name)
    if abs(distance-radius) > length:
        return locals_,0.
    direction = _unit(direction)
    side = _unit(_sub(original,_mul(direction,_dot(original,direction))))
    cosine = (distance*distance+radius*radius-length*length)/(2.*distance*radius)
    if not -1.-1e-12 <= cosine <= 1.+1e-12:
        raise ArithmeticError('Mapped shoulder sphere intersection disagrees with its cosine: '+limb.name)
    cosine = min(1.,max(-1.,cosine))
    adjusted = _mul(_add(_mul(direction,cosine),_mul(side,math.sqrt(max(0.,1.-cosine*cosine)))),radius)
    delta = _minimum_rotation(original,adjusted,_cross(original,direction))
    parent = rig.parents[limb.shoulder]
    parent_rotation = (0.,0.,0.,1.) if parent < 0 else worlds[parent].rotation
    return _write_rotation(locals_,limb.shoulder,_qmul(delta,worlds[limb.shoulder].rotation),parent_rotation),_angle((0.,0.,0.,1.),delta)


def _solve_limb(rig: GoalRig, locals_: tuple[LocalTransform, ...], limb: LimbSpec, target: Vec3,
                target_rotation: Quat, worlds: dict[int,_World]) -> tuple[tuple[LocalTransform, ...],float,float,float]:
    a,b,c = (worlds[i].position for i in (limb.upper,limb.middle,limb.end))
    first,second,direction = _sub(b,a),_sub(c,b),_sub(target,a)
    first_length,second_length,distance = math.hypot(*first),math.hypot(*second),math.hypot(*direction)
    if min(first_length,second_length,distance) == 0.:
        raise ValueError('Zero source limb length or goal distance: '+limb.name)
    direction = _unit(direction)
    reachable = min(first_length+second_length,max(abs(first_length-second_length),distance))
    along = (first_length*first_length-second_length*second_length+reachable*reachable)/(2.*reachable)
    height = math.sqrt(max(0.,first_length*first_length-along*along))
    source_plane = _cross(_sub(c,a),first)
    transport = _minimum_rotation(_sub(c,a),direction,source_plane)
    target_plane = _rotate(transport,_unit(source_plane))
    bend = _unit(_cross(target_plane,direction))
    desired_middle = _add(a,_add(_mul(direction,along),_mul(bend,height)))
    desired_end = _add(a,_mul(direction,reachable))
    upper_q = _transport(first,source_plane,_sub(desired_middle,a),target_plane,worlds[limb.upper].rotation)
    parent = rig.parents[limb.upper]
    parent_q = (0.,0.,0.,1.) if parent < 0 else worlds[parent].rotation
    upper = _write_rotation(locals_,limb.upper,upper_q,parent_q)
    # Direct chain validation makes the solved parent rotations sufficient.
    middle_q = _transport(second,source_plane,_sub(desired_end,desired_middle),target_plane,worlds[limb.middle].rotation)
    middle = _write_rotation(upper,limb.middle,middle_q,upper_q)
    return (_write_rotation(middle,limb.end,target_rotation,middle_q),abs(distance-reachable),
            math.hypot(*_cross(first,second))/(first_length*second_length),_angle((0.,0.,0.,1.),transport))


def _mass_center(rig: GoalRig, worlds: dict[int,_World]) -> Vec3:
    total = (0.,0.,0.)
    for point in rig.mass_points:
        total = _add(total,_mul(worlds[point.index].position,point.weight))
    return total


def _body_orientation(rig: GoalRig, worlds: dict[int,_World]) -> Quat:
    left_arm,right_arm,left_leg,right_leg = (worlds[i].position for i in rig.orientation_nodes)
    shoulder_center = _mul(_add(left_arm,right_arm),.5)
    hip_center = _mul(_add(left_leg,right_leg),.5)
    up = _unit(_sub(shoulder_center,hip_center))
    right = _unit(_add(_sub(right_arm,left_arm),_sub(right_leg,left_leg)))
    forward = _unit(_cross(right,up))
    return _frame_rotation(_cross(up,forward),forward)


def _solve_targets(rig: GoalRig, input_: tuple[LocalTransform, ...],
                   targets: tuple[tuple[LimbSpec,Vec3,Quat], ...], original: dict[int,_World]) -> tuple[tuple[LocalTransform, ...],tuple[LimbDiagnostic, ...]]:
    result = input_
    diagnostics = []
    for limb,target,target_q in targets:
        worlds = _worlds(rig,result)
        before_error = math.dist(original[limb.end].position,target)
        corrected_error = math.dist(worlds[limb.end].position,target)
        shoulder_result,shoulder_angle = _shoulder_reach(rig,result,limb,target,worlds)
        if shoulder_result is not result:
            worlds = _worlds(rig,shoulder_result)
        result,unreachable,source_sine,axis_angle = _solve_limb(rig,shoulder_result,limb,target,target_q,worlds)
        final = _worlds(rig,result)[limb.end]
        diagnostics.append(LimbDiagnostic(limb.name,target,target_q,before_error,corrected_error,
            math.dist(final.position,target),_angle(final.rotation,target_q),shoulder_angle,unreachable,source_sine,axis_angle))
    return result,tuple(diagnostics)


def solve_goal_frame(rig: GoalRig, locals_: tuple[LocalTransform, ...], human_scale: float, root_translation: Vec3,
                     root_rotation: Quat, goals: tuple[GoalSample, ...]) -> SolvedFrame:
    if len(locals_) != len(rig.parents) or not math.isfinite(human_scale) or human_scale <= 0.:
        raise ValueError('Source-goal local count or Human scale is invalid')
    if len(goals) != 4 or {g.name for g in goals} != {limb.name for limb in rig.limbs}:
        raise ValueError('Source-goal frame requires exactly one sample for each mapped hand/foot')
    _check_vector(root_translation,'RootT')
    _check_quaternion(root_rotation,'RootQ')
    if any(abs(v-1.) > 2e-6 for v in locals_[rig.animator_index].scale):
        raise ValueError('Source-goal Animator itself must have unit scale; scaled Motion decomposition is unsupported')
    original = _worlds(rig,locals_)
    parent = rig.parents[rig.animator_index]
    animator_parent = _World((0.,0.,0.),(0.,0.,0.,1.),1.) if parent < 0 else original[parent]
    # Motion translation and full RootT use normalized Human length units.
    # QVVF auxiliary foot routes independently confirm scaling Motion once.
    motion = locals_[rig.animator_index].position
    converted_animator = replace(locals_[rig.animator_index],position=_mul(motion,human_scale))
    delta = _rotate(animator_parent.rotation,_mul(_sub(root_translation,motion),animator_parent.scale*(human_scale-1.)))
    hips_parent_index = rig.parents[rig.hips_index]
    hips_parent = _World((0.,0.,0.),(0.,0.,0.,1.),1.) if hips_parent_index < 0 else original[hips_parent_index]
    local_delta = _mul(_rotate(_inverse(hips_parent.rotation),delta),1./hips_parent.scale)
    hips = replace(locals_[rig.hips_index],position=_add(locals_[rig.hips_index].position,local_delta))
    base = tuple(hips if i == rig.hips_index else converted_animator if i == rig.animator_index else value
                 for i,value in enumerate(locals_))
    samples = {goal.name:goal for goal in goals}
    targets = []
    root_q = _normalize(root_rotation)
    for limb in rig.limbs:
        goal = samples[limb.name]
        _check_vector(goal.position,limb.name+'T')
        _check_quaternion(goal.rotation,limb.name+'Q')
        goal_q = _qmul(animator_parent.rotation,_qmul(root_q,_normalize(goal.rotation)))
        relative_goal = _mul(_add(root_translation,_rotate(root_q,goal.position)),human_scale)
        target = _sub(_add(animator_parent.position,_rotate(animator_parent.rotation,_mul(relative_goal,animator_parent.scale))),
                      _rotate(goal_q,_mul(limb.bottom_offset,animator_parent.scale)))
        target_q = _qmul(goal_q,_inverse(_normalize(limb.post_rotation)))
        targets.append((limb,target,target_q))
    targets_ = tuple(targets)
    base_worlds = _worlds(rig,base)
    source_hips = base_worlds[rig.hips_index].position
    balls = []
    for limb,target,_ in targets_:
        if limb.name.endswith('Foot'):
            upper,middle,end = (base_worlds[i].position for i in (limb.upper,limb.middle,limb.end))
            balls.append((_sub(target,_sub(upper,source_hips)),math.dist(upper,middle)+math.dist(middle,end)))
    mass_target = _add(animator_parent.position,_rotate(animator_parent.rotation,
        _mul(root_translation,animator_parent.scale*human_scale)))
    body_target_q = _body_orientation(rig,original)
    # One part per million matches the measured float32 input spatial precision.
    # This search resolution does not relax the separate endpoint requirement.
    tolerance = 1e-6*human_scale*animator_parent.scale
    def evaluate(desired_hips: Vec3) -> _BodyTrial:
        feasible_hips = _project_two_balls(desired_hips,*balls[0],*balls[1],source_hips)
        correction = _sub(feasible_hips,source_hips)
        correction_local = _mul(_rotate(_inverse(hips_parent.rotation),correction),1./hips_parent.scale)
        moved_hips = replace(base[rig.hips_index],position=_add(base[rig.hips_index].position,correction_local))
        seed = tuple(moved_hips if i == rig.hips_index else value for i,value in enumerate(base))
        # Every iteration starts from the same source FK quaternions.
        result,diagnostics = _solve_targets(rig,seed,targets_,original)
        worlds = _worlds(rig,result)
        error = _sub(mass_target,_mass_center(rig,worlds))
        mass_error = math.hypot(*error)
        return _BodyTrial(result,diagnostics,feasible_hips,mass_error)
    trial = evaluate(source_hips)
    evaluations = 1
    initial_mass_error = trial.mass_error_m
    search_step = max(initial_mass_error,tolerance)
    directions = tuple(_rotate(animator_parent.rotation,tuple(1. if i == axis else 0. for i in range(3))) for axis in range(3))
    iterations = 1
    # This is a bounded authoring refinement of an approximate mass model.
    # It does not require a stationary point or claim exact native mass recovery.
    while search_step > tolerance and trial.mass_error_m > tolerance and evaluations+6 <= 128:
        for direction in directions:
            best = trial
            for sign in (-1.,1.):
                candidate = evaluate(_add(trial.hips,_mul(direction,search_step*sign)))
                evaluations += 1
                if candidate.mass_error_m < best.mass_error_m:
                    best = candidate
            trial = best
        search_step *= .5
        iterations += 1
    termination = ('refinement_resolution' if search_step <= tolerance else
                   'residual_threshold' if trial.mass_error_m <= tolerance else 'budget')
    worlds = _worlds(rig,trial.locals)
    return SolvedFrame(trial.locals,trial.diagnostics,MassDiagnostic(mass_target,initial_mass_error,trial.mass_error_m,
        iterations,_sub(trial.hips,source_hips),tolerance,search_step,
        _angle(_body_orientation(rig,worlds),body_target_q),evaluations,termination))
