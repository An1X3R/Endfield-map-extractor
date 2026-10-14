"""Allocate post-goal wrist roll using a geometric authoring objective.

ACL auxiliary proportions are measured from original clip samples. They are not
identified native runtime weights. The objective excludes shoulder strain and
preserves elbow/hand positions and hand world rotations, not native parity.
"""
from __future__ import annotations

from dataclasses import dataclass, replace
import math

from character_animation_goals import (
    GoalRig, LocalTransform, Quat, Vec3, _angle, _check_quaternion,
    _check_vector, _dot, _inverse, _mul, _normalize, _qmul, _sub, _unit,
    _worlds, _write_rotation,
)

WristFrames = tuple[tuple[LocalTransform, ...], ...]
_STATIONARY_RESOLUTION_RADIANS = 1e-10
_MAX_STATIONARY_CELLS = 16384
_SOURCE_SIGNAL_BOUND_DEGREES = math.degrees(8.*2.**-23)


@dataclass(frozen=True)
class WristTwistRoute:
    name: str
    upper: int
    middle: int
    end: int
    proximal: int
    distal: int
    axis: Vec3
    rest_proximal: Quat
    rest_distal: Quat
    rest_hand: Quat


@dataclass(frozen=True)
class WristTwistFit:
    name: str
    proximal_fraction: float
    source_signal_squared_degrees: float
    source_signal_bound_degrees: float
    source_root_range_degrees: tuple[float, float]
    source_compound_range_degrees: tuple[float, float]
    fit_rms_degrees: float
    fit_max_error_degrees: float


@dataclass(frozen=True)
class WristTwistDiagnostic:
    name: str
    bend_degrees: float
    continuous_error_degrees: float
    upper_roll_degrees: float
    forearm_roll_degrees: float
    auxiliary_correction_degrees: float
    objective: float
    stationary_intervals: int
    joint_position_error_m: float
    hand_rotation_error_degrees: float


@dataclass(frozen=True)
class WristTwistResult:
    frames: WristFrames
    fits: tuple[WristTwistFit, ...]
    diagnostics: tuple[tuple[WristTwistDiagnostic, ...], ...]


@dataclass(frozen=True)
class _Plan:
    route: WristTwistRoute
    fit: WristTwistFit
    errors_degrees: tuple[float, ...]


class WristTwistUnsupportedError(ValueError):
    """Source samples do not identify the required auxiliary axial relation."""


class WristTwistFrameError(ArithmeticError):
    """Preserve the exact failing frame and route without masking its cause."""

    def __init__(self, frame_index: int, route_name: str, cause: Exception):
        self.frame_index = frame_index
        self.route_name = route_name
        super().__init__('Wrist allocation failed: frame=' + str(frame_index + 1) +
                         '; route=' + route_name + '; cause=' + str(cause))


def _axial_degrees(rotation: Quat, axis: Vec3, context: str) -> float:
    _check_quaternion(rotation, context)
    q = _normalize(rotation)
    component = _dot((q[0], q[1], q[2]), axis)
    norm = math.hypot(component, q[3])
    if norm < 1e-12:
        raise WristTwistUnsupportedError('Undefined axial phase for perpendicular half-turn swing: '+context)
    vector = _mul(axis, component/norm)
    scalar = q[3]/norm
    if scalar < 0.:
        vector = _mul(vector, -1.)
        scalar = -scalar
    return math.degrees(2.*math.atan2(_dot(vector, axis), scalar))


def _unwrap_degrees(angles: tuple[float, ...], context: str) -> tuple[float, ...]:
    if not angles or not all(math.isfinite(v) for v in angles):
        raise ValueError('Axial phase samples must be nonempty and finite: '+context)
    result = [angles[0]]
    for index, angle in enumerate(angles[1:], 1):
        step = (angle-result[-1]+180.)%360.-180.
        if abs(abs(step)-180.) < 1e-9:
            raise WristTwistUnsupportedError('Ambiguous axial half-turn sample step: '+context+'; frame='+str(index+1))
        result.append(result[-1]+step)
    return tuple(result)


def _axis_rotation(axis: Vec3, radians: float) -> Quat:
    vector = _mul(axis, math.sin(radians/2.))
    return (vector[0], vector[1], vector[2], math.cos(radians/2.))


def _validate_route(rig: GoalRig, route: WristTwistRoute) -> None:
    indices = (route.upper, route.middle, route.end, route.proximal, route.distal)
    if len(set(indices)) != 5 or any(i < 0 or i >= len(rig.parents) for i in indices):
        raise ValueError('Wrist route needs five distinct in-range source indices: '+route.name)
    if ((rig.parents[route.middle], rig.parents[route.end], rig.parents[route.proximal], rig.parents[route.distal])
            != (route.upper, route.middle, route.middle, route.proximal)):
        raise ValueError('Wrist route is not the exact Human and auxiliary direct hierarchy: '+route.name)
    if not any((l.name, l.upper, l.middle, l.end) == (route.name, route.upper, route.middle, route.end)
               for l in rig.limbs if l.name.endswith('Hand')):
        raise ValueError('Wrist route differs from the exact mapped Avatar hand: '+route.name)
    _check_vector(route.axis, route.name+' source axis')
    if abs(math.hypot(*route.axis)-1.) > 1e-6:
        raise ValueError('Wrist source axis must be normalized: '+route.name)
    for q in (route.rest_proximal, route.rest_distal, route.rest_hand):
        _check_quaternion(q, route.name+' source rest rotation')


def _compile_plan(route: WristTwistRoute, native: WristFrames, accepted: WristFrames) -> _Plan:
    roots: list[float] = []
    compounds: list[float] = []
    wrists: list[float] = []
    rest_root = _normalize(route.rest_proximal)
    rest_compound = _qmul(rest_root, _normalize(route.rest_distal))
    rest_hand = _normalize(route.rest_hand)
    for index, (source, target) in enumerate(zip(native, accepted, strict=True)):
        context = route.name+'; frame='+str(index+1)
        for i in (route.proximal, route.distal):
            if source[i] != target[i]:
                raise ValueError('Goal frames must retain original ACL auxiliary locals before wrist allocation: '+context+'; index='+str(i))
        roots.append(_axial_degrees(_qmul(source[route.proximal].rotation, _inverse(rest_root)), route.axis, context))
        compound = _qmul(source[route.proximal].rotation, source[route.distal].rotation)
        compounds.append(_axial_degrees(_qmul(compound, _inverse(rest_compound)), route.axis, context))
        wrists.append(_axial_degrees(_qmul(target[route.end].rotation, _inverse(rest_hand)), route.axis, context))
    root_phase, compound_phase, wrist_phase = (
        _unwrap_degrees(tuple(values), route.name) for values in (roots, compounds, wrists))
    signal = sum(v*v for v in compound_phase)
    if max(abs(v) for v in compound_phase) <= _SOURCE_SIGNAL_BOUND_DEGREES:
        raise WristTwistUnsupportedError('Original ACL auxiliary angles do not identify a proportion: '+route.name)
    fraction = sum(a*b for a,b in zip(root_phase, compound_phase, strict=True))/signal
    if not math.isfinite(fraction) or not 0. <= fraction <= 1.:
        raise WristTwistUnsupportedError('Measured ACL auxiliary proportion is outside [0,1]: '+route.name+'; fraction='+str(fraction))
    residuals = tuple(a-fraction*b for a,b in zip(root_phase, compound_phase, strict=True))
    fit = WristTwistFit(route.name, fraction, signal, _SOURCE_SIGNAL_BOUND_DEGREES, (min(root_phase),max(root_phase)),
        (min(compound_phase),max(compound_phase)), math.sqrt(sum(v*v for v in residuals)/len(residuals)),
        max(abs(v) for v in residuals))
    errors = _unwrap_degrees(tuple((w-c+180.)%360.-180. for w,c in zip(wrist_phase,compound_phase,strict=True)), route.name)
    return _Plan(route, fit, errors)


def _minimum_strain(error: float, bend_cosine: float, fraction: float) -> tuple[float, float, int]:
    """Minimize the stated chord objective over the bounded continuous phase."""
    a = max(0., 1.-bend_cosine*bend_cosine)
    g = 1.-fraction
    def value(t: float) -> float:
        return a*math.sin(t/2.)**2+math.sin(fraction*(error-t)/2.)**2+math.sin(g*(error-t)/2.)**2
    def derivative(t: float) -> float:
        return .5*(a*math.sin(t)-fraction*math.sin(fraction*(error-t))-g*math.sin(g*(error-t)))
    def curvature(t: float) -> float:
        return .5*(a*math.cos(t)+fraction*fraction*math.cos(fraction*(error-t))+g*g*math.cos(g*(error-t)))
    lo, hi = sorted((0., error))
    candidates = [lo, hi]
    cells = [(lo, hi)]
    second_bound = .5*(a+fraction**2+g**2)
    third_bound = .5*(a+fraction**3+g**3)
    evaluated = 0
    while cells:
        left, right = cells.pop()
        evaluated += 1
        if evaluated > _MAX_STATIONARY_CELLS:
            raise ArithmeticError('Wrist stationary isolation exceeded '+str(_MAX_STATIONARY_CELLS)+' cells')
        mid, half = (left+right)/2., (right-left)/2.
        if abs(derivative(mid)) > second_bound*half:
            continue
        if right-left <= _STATIONARY_RESOLUTION_RADIANS:
            candidates.append(mid)
            continue
        if abs(curvature(mid)) > third_bound*half:
            dl, dr = derivative(left), derivative(right)
            if dl == 0.:
                candidates.append(left)
            if dr == 0.:
                candidates.append(right)
            if dl*dr < 0.:
                while right-left > _STATIONARY_RESOLUTION_RADIANS:
                    mid = (left+right)/2.
                    dm = derivative(mid)
                    if dl*dm <= 0.:
                        right = mid
                    else:
                        left, dl = mid, dm
                candidates.append((left+right)/2.)
            continue
        cells.extend(((left,mid),(mid,right)))
    optimum = min(candidates, key=lambda t:(value(t),abs(t)))
    return optimum, value(optimum), evaluated


def _solve_route(rig: GoalRig, frame: tuple[LocalTransform, ...], plan: _Plan,
                 error_degrees: float) -> tuple[tuple[LocalTransform, ...], WristTwistDiagnostic]:
    route = plan.route
    before = _worlds(rig, frame)
    first = _unit(_sub(before[route.middle].position, before[route.upper].position))
    second = _unit(_sub(before[route.end].position, before[route.middle].position))
    cosine = min(1.,max(-1.,_dot(first,second)))
    error = math.radians(error_degrees)
    t, cost, cells = _minimum_strain(error, cosine, plan.fit.proximal_fraction)
    alpha = 2.*math.atan2(cosine*math.sin(t/2.), math.cos(t/2.))
    upper_q = _qmul(_axis_rotation(first,alpha), before[route.upper].rotation)
    middle_q = _qmul(_axis_rotation(second,t), before[route.middle].rotation)
    parent = rig.parents[route.upper]
    result = _write_rotation(frame,route.upper,upper_q,before[parent].rotation)
    result = _write_rotation(result,route.middle,middle_q,upper_q)
    result = _write_rotation(result,route.end,before[route.end].rotation,middle_q)
    residual = math.degrees(error-t)
    # Use the sampled hand segment axis for the actual local quaternion writes.
    axis = _unit(frame[route.end].position)
    root_q = _qmul(_axis_rotation(axis,math.radians(residual*plan.fit.proximal_fraction)),frame[route.proximal].rotation)
    compound_q = _qmul(_axis_rotation(axis,math.radians(residual)),
                      _qmul(frame[route.proximal].rotation,frame[route.distal].rotation))
    child_q = _qmul(_inverse(root_q),compound_q)
    result = tuple(replace(v,rotation=root_q) if i == route.proximal else
                   replace(v,rotation=child_q) if i == route.distal else v for i,v in enumerate(result))
    after = _worlds(rig,result)
    position_error = max(math.dist(before[i].position,after[i].position) for i in (route.upper,route.middle,route.end))
    rotation_error = _angle(before[route.end].rotation,after[route.end].rotation)
    parent_scale = before[route.upper].scale
    if position_error > 1e-10*parent_scale or rotation_error > 1e-8:
        raise ArithmeticError('Wrist allocation changed a protected joint/hand pose: '+route.name+
                              '; position_m='+str(position_error)+'; rotation_degrees='+str(rotation_error))
    diagnostic = WristTwistDiagnostic(route.name,math.degrees(math.acos(cosine)),error_degrees,
        math.degrees(alpha),math.degrees(t),residual,cost,cells,position_error,rotation_error)
    return result, diagnostic


def apply_wrist_twist(rig: GoalRig, routes: tuple[WristTwistRoute, ...], native_frames: WristFrames,
                      goal_frames: WristFrames) -> WristTwistResult:
    """Return complete new frames; never modify source or accepted input frames.

    Routes must already have exact Avatar/ACL provenance and axiality checks.
    Missing/ambiguous routes are a caller-visible unsupported state. This pure
    kernel also rejects unidentifiable angular signal rather than guessing half.
    """
    if not routes or len({r.name for r in routes}) != len(routes):
        raise ValueError('Wrist allocation requires nonempty uniquely named exact routes')
    if not native_frames or len(native_frames) != len(goal_frames):
        raise ValueError('Wrist native/goal sample domains must be nonempty and identical')
    if any(len(f) != len(rig.parents) for f in native_frames+goal_frames):
        raise ValueError('Wrist sample local counts differ from the explicit source hierarchy')
    for route in routes:
        _validate_route(rig,route)
    allowed = {i for r in routes for i in (r.upper,r.middle,r.end,r.proximal,r.distal)}
    if len(allowed) != 5*len(routes):
        raise ValueError('Wrist routes overlap driven source indices')
    plans = tuple(_compile_plan(r,native_frames,goal_frames) for r in routes)
    results: list[tuple[LocalTransform, ...]] = []
    diagnostics: list[tuple[WristTwistDiagnostic, ...]] = []
    for index, source in enumerate(goal_frames):
        result = source
        records: list[WristTwistDiagnostic] = []
        for plan in plans:
            try:
                result, diagnostic = _solve_route(rig,result,plan,plan.errors_degrees[index])
            except (ValueError, ArithmeticError) as error:
                raise WristTwistFrameError(index, plan.route.name, error) from error
            records.append(diagnostic)
        if any(a != b for i,(a,b) in enumerate(zip(source,result,strict=True)) if i not in allowed):
            raise ArithmeticError('Wrist allocation changed unrelated source local at frame '+str(index+1))
        if any(a.position != b.position or a.scale != b.scale for a,b in zip(source,result,strict=True)):
            raise ArithmeticError('Wrist allocation changed source translation or scale at frame '+str(index+1))
        results.append(result)
        diagnostics.append(tuple(records))
    return WristTwistResult(tuple(results),tuple(p.fit for p in plans),tuple(diagnostics))
