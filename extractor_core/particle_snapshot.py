"""Build bounded, deterministic particle preview samples from typed SceneProbe data.

The samples are an authored static preview. They do not reproduce Unity's
random stream, update order, renderer simulation, or disabled module behavior.
"""
from __future__ import annotations

from dataclasses import dataclass
import hashlib
import math
import random
from collections.abc import Mapping

from extractor_core.particle_curves import ParticleMinMaxCurve, evaluate_minmax_curve, parse_minmax_curve


PARTICLE_PARAMETER_SCHEMA_VERSION = 5
PARTICLE_PARAMETER_MODULE_FIELDS = (
    "Initial", "Emission", "Shape", "SizeOverLifetime", "ColorOverLifetime",
    "Trail", "CustomData", "Uv", "VelocityOverLifetime",
)


class ParticleSnapshotError(ValueError):
    """A required serialized field or preview limit is invalid."""


class UnsupportedParticleSnapshot(ParticleSnapshotError):
    """A serialized feature has no verified preview mapping."""


@dataclass(frozen=True)
class SnapshotParticle:
    particle_id: int
    birth_time: float
    position: tuple[float, float, float]
    velocity: tuple[float, float, float]
    size: tuple[float, float, float]
    color: tuple[float, float, float, float]
    age_fraction: float
    rotation_radians: float
    trail: tuple[SnapshotTrailPoint, ...]
    custom1: tuple[float, float, float, float] | None
    custom2: tuple[float, float, float, float] | None
    uv_rect: tuple[float, float, float, float]


@dataclass(frozen=True)
class SnapshotTrailPoint:
    position: tuple[float, float, float]
    width: float
    color: tuple[float, float, float, float]
    u: float


@dataclass(frozen=True)
class ParticleSnapshot:
    particles: tuple[SnapshotParticle, ...]
    generated: int
    truncated: int
    unsupported_modules: tuple[str, ...]
    preview_scope: str


def _record(value: object, context: str) -> Mapping[str, object]:
    if not isinstance(value, dict) or any(type(key) is not str for key in value):
        raise ParticleSnapshotError(f"Expected object: field={context}")
    return value


def _field(row: Mapping[str, object], key: str, context: str) -> object:
    if key not in row:
        raise ParticleSnapshotError(f"Required field missing: field={context}.{key}")
    return row[key]


def _number(value: object, context: str) -> float:
    if type(value) not in (int, float) or not math.isfinite(float(value)):
        raise ParticleSnapshotError(f"Expected finite number: field={context}, value={value!r}")
    return float(value)


def _integer(value: object, context: str) -> int:
    if type(value) is not int:
        raise ParticleSnapshotError(f"Expected integer: field={context}, value={value!r}")
    return value


def _boolean(value: object, context: str) -> bool:
    if type(value) is not bool:
        raise ParticleSnapshotError(f"Expected bool: field={context}, value={value!r}")
    return value


def _sequence(value: object, context: str) -> list[object]:
    if not isinstance(value, list):
        raise ParticleSnapshotError(f"Expected array: field={context}")
    return value


def _unit(rng: random.Random) -> float:
    return rng.random()


def _curve(row: Mapping[str, object], key: str, time: float, rng: random.Random, context: str) -> float:
    curve = parse_minmax_curve(_field(row, key, context), f"{context}.{key}")
    return evaluate_minmax_curve(curve, time, _unit(rng))


def _trivially_zero_curve(value: object, context: str) -> bool:
    curve = parse_minmax_curve(value, context)
    if curve.mode == 0:
        return curve.scalar == 0
    if curve.mode == 3:
        return curve.scalar == 0 and curve.min_scalar == 0
    return False


def _vector3(value: object, context: str) -> tuple[float, float, float]:
    if not isinstance(value, tuple) or len(value) != 3:
        raise ParticleSnapshotError(f"Expected Vector3 tuple: field={context}")
    return tuple(_number(item, f"{context}[{index}]") for index, item in enumerate(value))  # type: ignore[return-value]


def _rgba(value: object, context: str) -> tuple[float, float, float, float]:
    row = _record(value, context)
    return tuple(_number(_field(row, key, context), f"{context}.{key}") for key in ("R", "G", "B", "A"))  # type: ignore[return-value]


def _gradient(value: object, time: float, context: str) -> tuple[float, float, float, float]:
    row = _record(value, context)
    colors = _sequence(_field(row, "Colors", context), f"{context}.Colors")
    color_times = _sequence(_field(row, "ColorTimes", context), f"{context}.ColorTimes")
    alpha_times = _sequence(_field(row, "AlphaTimes", context), f"{context}.AlphaTimes")
    color_count = _integer(_field(row, "NumColorKeys", context), f"{context}.NumColorKeys")
    alpha_count = _integer(_field(row, "NumAlphaKeys", context), f"{context}.NumAlphaKeys")
    mode = _integer(_field(row, "Mode", context), f"{context}.Mode")
    if mode not in (0, 1) or not (1 <= color_count <= len(colors)) or not (1 <= alpha_count <= len(colors)):
        raise UnsupportedParticleSnapshot(f"Unsupported gradient mode/key counts: field={context}, mode={mode}, colorKeys={color_count}, alphaKeys={alpha_count}")
    if len(color_times) < color_count or len(alpha_times) < alpha_count:
        raise ParticleSnapshotError(f"Gradient time slots missing: field={context}")
    rgba = tuple(_rgba(item, f"{context}.Colors[{index}]") for index, item in enumerate(colors))

    def sample(times: list[object], count: int, channel: int) -> float:
        keys = [(_integer(times[index], f"{context}.Times[{index}]") / 65535.0, rgba[index][channel]) for index in range(count)]
        if any(left[0] > right[0] for left, right in zip(keys, keys[1:])):
            raise ParticleSnapshotError(f"Gradient key times decrease: field={context}")
        if time <= keys[0][0]:
            return keys[0][1]
        for left, right in zip(keys, keys[1:]):
            if time <= right[0]:
                if mode == 1 or right[0] == left[0]:
                    return left[1]
                weight = (time - left[0]) / (right[0] - left[0])
                return left[1] * (1.0 - weight) + right[1] * weight
        return keys[-1][1]

    return (sample(color_times, color_count, 0), sample(color_times, color_count, 1),
            sample(color_times, color_count, 2), sample(alpha_times, alpha_count, 3))


def _color(value: object, time: float, rng: random.Random, context: str) -> tuple[float, float, float, float]:
    row = _record(value, context)
    mode = _integer(_field(row, "MinMaxState", context), f"{context}.MinMaxState")
    factor = _unit(rng)
    if mode == 0:
        return _rgba(_field(row, "MaxColor", context), f"{context}.MaxColor")
    if mode == 1:
        return _gradient(_field(row, "MaxGradient", context), time, f"{context}.MaxGradient")
    if mode == 2:
        minimum = _rgba(_field(row, "MinColor", context), f"{context}.MinColor")
        maximum = _rgba(_field(row, "MaxColor", context), f"{context}.MaxColor")
    elif mode == 3:
        minimum = _gradient(_field(row, "MinGradient", context), time, f"{context}.MinGradient")
        maximum = _gradient(_field(row, "MaxGradient", context), time, f"{context}.MaxGradient")
    else:
        raise UnsupportedParticleSnapshot(f"Unsupported MinMaxGradient mode: field={context}, mode={mode}")
    return tuple(left * (1.0 - factor) + right * factor for left, right in zip(minimum, maximum))  # type: ignore[return-value]


def _shape(value: object, rng: random.Random) -> tuple[tuple[float, float, float], tuple[float, float, float]]:
    shape = _record(value, "Shape")
    if not _boolean(_field(shape, "Enabled", "Shape"), "Shape.Enabled"):
        return (0.0, 0.0, 0.0), (0.0, 0.0, 1.0)
    kind = _integer(_field(shape, "Type", "Shape"), "Shape.Type")
    radius = _record(_field(shape, "Radius", "Shape"), "Shape.Radius")
    arc = _record(_field(shape, "Arc", "Shape"), "Shape.Arc")
    if _integer(_field(radius, "Mode", "Shape.Radius"), "Shape.Radius.Mode") != 0 or _integer(_field(arc, "Mode", "Shape.Arc"), "Shape.Arc.Mode") != 0:
        raise UnsupportedParticleSnapshot("Shape radius/arc animation mode is unsupported")
    if _number(_field(radius, "Spread", "Shape.Radius"), "Shape.Radius.Spread") or _number(_field(arc, "Spread", "Shape.Arc"), "Shape.Arc.Spread"):
        raise UnsupportedParticleSnapshot("Shape radius/arc spread is unsupported")
    for name in ("RandomDirectionAmount", "SphericalDirectionAmount", "RandomPositionAmount"):
        if _number(_field(shape, name, "Shape"), f"Shape.{name}"):
            raise UnsupportedParticleSnapshot(f"Shape direction/position modifier is unsupported: field={name}")
    magnitude = _number(_field(radius, "Value", "Shape.Radius"), "Shape.Radius.Value")
    thickness = _number(_field(shape, "RadiusThickness", "Shape"), "Shape.RadiusThickness")
    if magnitude < 0 or not 0 <= thickness <= 1:
        raise ParticleSnapshotError(f"Shape radius or thickness outside valid range: radius={magnitude}, thickness={thickness}")
    azimuth = math.radians(_number(_field(arc, "Value", "Shape.Arc"), "Shape.Arc.Value")) * _unit(rng)
    angle = math.radians(_number(_field(shape, "Angle", "Shape"), "Shape.Angle"))
    if kind in (0, 2):
        z = _unit(rng) if kind == 2 else 2.0 * _unit(rng) - 1.0
        radial = math.sqrt(max(0.0, 1.0 - z * z))
        direction = (radial * math.cos(azimuth), radial * math.sin(azimuth), z)
        distance = magnitude * ((1.0 - thickness) + thickness * _unit(rng) ** (1.0 / 3.0))
        position = tuple(distance * component for component in direction)
    elif kind == 4:
        normalized_radius = math.sqrt((1.0 - thickness) ** 2 +
                                      (1.0 - (1.0 - thickness) ** 2) * _unit(rng))
        radial = magnitude * normalized_radius
        position = (radial * math.cos(azimuth), radial * math.sin(azimuth), 0.0)
        tilt = angle * normalized_radius
        direction = (math.sin(tilt) * math.cos(azimuth), math.sin(tilt) * math.sin(azimuth), math.cos(tilt))
    elif kind == 5:
        position = tuple(_unit(rng) - 0.5 for _ in range(3))
        direction = (0.0, 0.0, 1.0)
    else:
        raise UnsupportedParticleSnapshot(f"Unsupported shape type: type={kind}")
    scale = _record(_field(shape, "Scale", "Shape"), "Shape.Scale")
    offset = _record(_field(shape, "Position", "Shape"), "Shape.Position")
    rotation = _record(_field(shape, "Rotation", "Shape"), "Shape.Rotation")
    scaled = tuple(component * _number(_field(scale, axis, "Shape.Scale"), f"Shape.Scale.{axis}")
                   for component, axis in zip(position, ("X", "Y", "Z")))
    angles = tuple(math.radians(_number(_field(rotation, axis, "Shape.Rotation"), f"Shape.Rotation.{axis}"))
                   for axis in ("X", "Y", "Z"))

    def rotate(vector: tuple[float, float, float]) -> tuple[float, float, float]:
        x, y, z = vector
        rz, rx, ry = angles[2], angles[0], angles[1]
        x, y = math.cos(rz) * x - math.sin(rz) * y, math.sin(rz) * x + math.cos(rz) * y
        y, z = math.cos(rx) * y - math.sin(rx) * z, math.sin(rx) * y + math.cos(rx) * z
        x, z = math.cos(ry) * x + math.sin(ry) * z, -math.sin(ry) * x + math.cos(ry) * z
        return x, y, z

    transformed = rotate(scaled)
    return (tuple(component + _number(_field(offset, axis, "Shape.Position"), f"Shape.Position.{axis}")
                  for component, axis in zip(transformed, ("X", "Y", "Z"))), rotate(direction))  # type: ignore[return-value]


def _seed(identity: str, seed: int) -> int:
    digest = hashlib.sha256(f"{identity}:{seed}".encode("utf-8")).digest()
    return int.from_bytes(digest[:8], "little")


def _birth_times(system: Mapping[str, object], emission: Mapping[str, object], age: float, limit: int, rng: random.Random) -> tuple[float, ...]:
    length = _number(_field(system, "LengthInSec", "System"), "System.LengthInSec")
    if length <= 0:
        raise ParticleSnapshotError("System length must be positive")
    delay = evaluate_minmax_curve(parse_minmax_curve(_field(system, "StartDelay", "System"), "System.StartDelay"), 0.0, _unit(rng))
    if delay < 0:
        raise ParticleSnapshotError(f"Negative start delay: delay={delay}")
    if age < delay:
        return ()
    elapsed = max(0.0, age - delay)
    looping = _boolean(_field(system, "Looping", "System"), "System.Looping")
    emission_end = elapsed if looping else min(elapsed, length)
    absolute_end = age if looping else min(age, delay + length)
    rate = parse_minmax_curve(_field(emission, "RateOverTime", "Emission"), "Emission.RateOverTime")
    distance_rate = parse_minmax_curve(_field(emission, "RateOverDistance", "Emission"), "Emission.RateOverDistance")
    if distance_rate.mode != 0 or distance_rate.scalar != 0:
        raise UnsupportedParticleSnapshot("RateOverDistance needs an emitter path")
    step = min(0.05, length / 100.0)
    if emission_end / step > 100000:
        raise ParticleSnapshotError(f"Snapshot emission interval exceeds bounded work: age={age}, step={step}")
    births: list[float] = []
    rate_rng = random.Random(rng.getrandbits(64))
    burst_seed = rng.getrandbits(64)
    carry = 0.0
    index = 0
    while index * step < emission_end:
        start = index * step
        # Integrate complete fixed ticks so earlier births never move with age.
        end = (index + 1) * step
        normalized = ((start + end) * 0.5 % length) / length
        hz = evaluate_minmax_curve(rate, normalized, _unit(rate_rng))
        if hz < 0:
            raise ParticleSnapshotError(f"Negative emission rate: rate={hz}")
        previous_carry = carry
        carry += hz * (end - start)
        count = math.floor(carry)
        carry -= count
        for item in range(count):
            time = start + (item + 1 - previous_carry) / hz
            if delay + time <= absolute_end:
                births.append(delay + time)
        if len(births) > limit:
            raise ParticleSnapshotError(f"Emission event limit exceeded: limit={limit}")
        index += 1
    burst_count = _integer(_field(emission, "BurstCount", "Emission"), "Emission.BurstCount")
    bursts = _sequence(_field(emission, "Bursts", "Emission"), "Emission.Bursts")
    if burst_count > len(bursts):
        raise ParticleSnapshotError("Burst count exceeds serialized rows")
    # Check one extra cycle using absolute time to avoid delay-subtraction drift.
    cycles = math.floor(emission_end / length) + 2 if looping else 1
    for cycle in range(cycles):
        for burst_index, value in enumerate(bursts[:burst_count]):
            burst = _record(value, f"Emission.Bursts[{burst_index}]")
            probability = _number(_field(burst, "Probability", "Burst"), "Burst.Probability")
            if not 0.0 <= probability <= 1.0:
                raise ParticleSnapshotError(f"Burst probability outside [0, 1]: burst={burst_index}, probability={probability}")
            repeats = _integer(_field(burst, "CycleCount", "Burst"), "Burst.CycleCount")
            if repeats <= 0:
                raise UnsupportedParticleSnapshot(
                    f"Burst cycle count semantics are unverified: burst={burst_index}, cycleCount={repeats}")
            interval = _number(_field(burst, "RepeatInterval", "Burst"), "Burst.RepeatInterval")
            burst_time = _number(_field(burst, "Time", "Burst"), "Burst.Time")
            if interval < 0 or burst_time < 0:
                raise ParticleSnapshotError(
                    f"Negative burst timing: burst={burst_index}, time={burst_time}, interval={interval}")
            for repeat in range(repeats):
                time = cycle * length + burst_time + repeat * interval
                if delay + time > absolute_end:
                    continue
                burst_rng = random.Random(_seed(f"{cycle}:{burst_index}:{repeat}", burst_seed))
                if _unit(burst_rng) >= probability:
                    continue
                count_curve = parse_minmax_curve(_field(burst, "CountCurve", "Burst"), "Burst.CountCurve")
                count = evaluate_minmax_curve(count_curve, (time % length) / length, _unit(burst_rng))
                if count < 0:
                    raise ParticleSnapshotError(f"Negative burst count: burst={burst_index}, count={count}")
                births.extend(delay + time for _ in range(math.floor(count)))
                if len(births) > limit:
                    raise ParticleSnapshotError(f"Emission event limit exceeded: limit={limit}")
    return tuple(sorted(births))


def _custom_stream(value: object, fraction: float, rng: random.Random, context: str) -> tuple[float, float, float, float] | None:
    stream = _record(value, context)
    mode = _integer(_field(stream, "Mode", context), f"{context}.Mode")
    if mode == 0:
        return None
    if mode == 2:
        return _color(_field(stream, "Color", context), fraction, rng, f"{context}.Color")
    if mode != 1:
        raise UnsupportedParticleSnapshot(f"CustomData stream mode is unsupported: field={context}.Mode, mode={mode}")
    count = _integer(_field(stream, "VectorComponentCount", context), f"{context}.VectorComponentCount")
    curves = _sequence(_field(stream, "Vector", context), f"{context}.Vector")
    if count != 4 or len(curves) != 4:
        raise UnsupportedParticleSnapshot(f"CustomData stream requires four serialized components: field={context}, count={count}, curves={len(curves)}")
    return tuple(evaluate_minmax_curve(parse_minmax_curve(value, f"{context}.Vector[{index}]"), fraction,
                                           _unit(rng)) for index, value in enumerate(curves))  # type: ignore[return-value]


def _uv_rect(value: object, fraction: float, rng: random.Random) -> tuple[float, float, float, float]:
    uv = _record(value, "Uv")
    if not _boolean(_field(uv, "Enabled", "Uv"), "Uv.Enabled"):
        return (0.0, 0.0, 1.0, 1.0)
    mode = _integer(_field(uv, "Mode", "Uv"), "Uv.Mode")
    time_mode = _integer(_field(uv, "TimeMode", "Uv"), "Uv.TimeMode")
    animation = _integer(_field(uv, "AnimationType", "Uv"), "Uv.AnimationType")
    if mode != 0 or time_mode != 0 or animation not in (0, 1):
        raise UnsupportedParticleSnapshot(
            f"UV grid/lifetime animation subset unsupported: mode={mode}, timeMode={time_mode}, animation={animation}")
    columns = _integer(_field(uv, "TilesX", "Uv"), "Uv.TilesX")
    rows = _integer(_field(uv, "TilesY", "Uv"), "Uv.TilesY")
    if columns <= 0 or rows <= 0:
        raise ParticleSnapshotError(f"Invalid UV tile dimensions: columns={columns}, rows={rows}")
    mask = _integer(_field(uv, "UvChannelMask", "Uv"), "Uv.UvChannelMask")
    if mask != -1 and mask & 1 == 0:
        raise UnsupportedParticleSnapshot(f"UV animation excludes primary UV channel: mask={mask}")
    cycles = _number(_field(uv, "Cycles", "Uv"), "Uv.Cycles")
    if cycles <= 0:
        raise ParticleSnapshotError(f"UV animation cycles must be positive: cycles={cycles}")
    start = _curve(uv, "StartFrame", 0.0, rng, "Uv")
    frame = _curve(uv, "FrameOverTime", fraction, rng, "Uv")
    normalized = (start + frame * cycles) % 1.0
    if animation == 0:
        frame_index = min(columns * rows - 1, math.floor(normalized * columns * rows))
        column, row = frame_index % columns, frame_index // columns
    else:
        row_mode = _integer(_field(uv, "RowMode", "Uv"), "Uv.RowMode")
        if row_mode == 0:
            row = _integer(_field(uv, "RowIndex", "Uv"), "Uv.RowIndex")
        elif row_mode == 1:
            row = min(rows - 1, math.floor(_unit(rng) * rows))
        else:
            raise UnsupportedParticleSnapshot(f"UV single-row selector unsupported: rowMode={row_mode}")
        if not 0 <= row < rows:
            raise ParticleSnapshotError(f"UV row index outside sheet: row={row}, rows={rows}")
        column = min(columns - 1, math.floor(normalized * columns))
    flip_u = _number(_field(uv, "FlipU", "Uv"), "Uv.FlipU")
    flip_v = _number(_field(uv, "FlipV", "Uv"), "Uv.FlipV")
    if not 0 <= flip_u <= 1 or not 0 <= flip_v <= 1:
        raise ParticleSnapshotError(f"UV flip probabilities outside [0,1]: flipU={flip_u}, flipV={flip_v}")
    u0, u1 = column / columns, (column + 1) / columns
    v0, v1 = 1.0 - (row + 1) / rows, 1.0 - row / rows
    if _unit(rng) < flip_u:
        u0, u1 = u1, u0
    if _unit(rng) < flip_v:
        v0, v1 = v1, v0
    return u0, v0, u1, v1


def _trajectory(birth: tuple[float, float, float], speed: tuple[float, float, float],
                gravity: tuple[float, float, float], seconds: float) -> tuple[float, float, float]:
    return tuple(point + velocity * seconds + 0.5 * acceleration * seconds * seconds
                 for point, velocity, acceleration in zip(birth, speed, gravity))  # type: ignore[return-value]


def _constant_velocity_curves(value: object, enabled: bool) -> tuple[
        tuple[ParticleMinMaxCurve, ...], tuple[str, ...]]:
    """Select the exact local, constant linear subset; retain explicit partial flags."""
    context = "VelocityOverLifetime"
    row = _record(value, context)
    if _boolean(_field(row, "Enabled", context), f"{context}.Enabled") != enabled:
        raise ParticleSnapshotError("Velocity module state disagrees with typed parameters")
    world_space = _boolean(_field(row, "InWorldSpace", context), f"{context}.InWorldSpace")
    fields = ("X", "Y", "Z", "OrbitalX", "OrbitalY", "OrbitalZ",
              "OrbitalOffsetX", "OrbitalOffsetY", "OrbitalOffsetZ", "Radial", "SpeedModifier")
    curves = {key: parse_minmax_curve(_field(row, key, context), f"{context}.{key}")
              for key in fields}
    if not enabled:
        return (), ()
    issues: list[str] = []
    if world_space:
        issues.append("VelocityModule.WorldSpace_requires_transform")
    for key in ("X", "Y", "Z"):
        if curves[key].mode not in (0, 3):
            issues.append(f"VelocityModule.{key}_requires_curve_integration")
    for key in fields[3:]:
        curve = curves[key]
        expected = 1.0 if key == "SpeedModifier" else 0.0
        constant = (curve.mode == 0 and curve.scalar == expected or
                    curve.mode == 3 and curve.scalar == curve.min_scalar == expected)
        if not constant:
            issues.append(f"VelocityModule.{key}_requires_motion_integration")
    if issues:
        return (), ("VelocityModule", *issues)
    return tuple(curves[key] for key in ("X", "Y", "Z")), ()


def _trail(value: object, birth: tuple[float, float, float], speed: tuple[float, float, float],
           gravity: tuple[float, float, float], elapsed: float, fraction: float,
           color: tuple[float, float, float, float], rng: random.Random) -> tuple[SnapshotTrailPoint, ...]:
    trail = _record(value, "Trail")
    if not _boolean(_field(trail, "Enabled", "Trail"), "Trail.Enabled"):
        return ()
    mode = _integer(_field(trail, "Mode", "Trail"), "Trail.Mode")
    texture_mode = _integer(_field(trail, "TextureMode", "Trail"), "Trail.TextureMode")
    ribbon_count = _integer(_field(trail, "RibbonCount", "Trail"), "Trail.RibbonCount")
    if mode != 0 or texture_mode != 0 or ribbon_count != 1:
        raise UnsupportedParticleSnapshot(f"Trail mode, texture mode, or ribbon count unsupported: mode={mode}, textureMode={texture_mode}, ribbons={ribbon_count}")
    for name in ("SizeAffectsWidth", "SizeAffectsLifetime", "SplitSubEmitterRibbons", "AttachRibbonsToTransform"):
        if _boolean(_field(trail, name, "Trail"), f"Trail.{name}"):
            raise UnsupportedParticleSnapshot(f"Trail setting is unsupported: field={name}")
    if not _boolean(_field(trail, "DieWithParticles", "Trail"), "Trail.DieWithParticles"):
        raise UnsupportedParticleSnapshot("Trail continuing after particle death is unsupported")
    _boolean(_field(trail, "WorldSpace", "Trail"), "Trail.WorldSpace")
    ratio = _number(_field(trail, "Ratio", "Trail"), "Trail.Ratio")
    if not 0 <= ratio <= 1:
        raise ParticleSnapshotError(f"Trail ratio outside [0,1]: ratio={ratio}")
    if _unit(rng) >= ratio:
        return ()
    lifetime = _curve(trail, "Lifetime", fraction, rng, "Trail")
    distance = _number(_field(trail, "MinVertexDistance", "Trail"), "Trail.MinVertexDistance")
    if lifetime <= 0 or distance <= 0:
        raise ParticleSnapshotError(f"Trail lifetime or vertex distance must be positive: lifetime={lifetime}, distance={distance}")
    start = max(0.0, elapsed - lifetime)
    if start >= elapsed:
        return ()
    head = _trajectory(birth, speed, gravity, elapsed)
    tail = _trajectory(birth, speed, gravity, start)
    length = math.dist(head, tail)
    segments = max(1, math.ceil(length / distance))
    if segments > 64:
        raise UnsupportedParticleSnapshot(f"Trail requires more than 64 preview segments: segments={segments}")
    inherit = _boolean(_field(trail, "InheritParticleColor", "Trail"), "Trail.InheritParticleColor")
    points: list[SnapshotTrailPoint] = []
    point_seed = rng.getrandbits(64)
    for index in range(segments + 1):
        point_rng = random.Random(point_seed)
        u = index / segments
        seconds = start * (1.0 - u) + elapsed * u
        width = _curve(trail, "WidthOverTrail", u, point_rng, "Trail")
        if width < 0:
            raise ParticleSnapshotError(f"Negative trail width: width={width}")
        over_life = _color(_field(trail, "ColorOverLifetime", "Trail"), fraction, point_rng,
                           "Trail.ColorOverLifetime")
        over_trail = _color(_field(trail, "ColorOverTrail", "Trail"), u, point_rng,
                            "Trail.ColorOverTrail")
        base = color if inherit else (1.0, 1.0, 1.0, 1.0)
        rgba = tuple(a * b * c for a, b, c in zip(base, over_life, over_trail))
        points.append(SnapshotTrailPoint(_trajectory(birth, speed, gravity, seconds), width, rgba, u))
    return tuple(points)


def generate_particle_snapshot(system_value: object, snapshot_age: float, identity: str,
                               max_particles: int, max_events: int,
                               gravity_local_unity: tuple[float, float, float]) -> ParticleSnapshot:
    """Sample a static preview with explicit limits and unsupported module flags."""
    system = _record(system_value, "System")
    age = _number(snapshot_age, "snapshotAge")
    gravity = _vector3(gravity_local_unity, "gravityLocalUnity")
    if age < 0 or not identity or max_particles <= 0 or max_events <= 0:
        raise ParticleSnapshotError("Snapshot age, identity, or limits are invalid")
    if "Parameters" not in system:
        raise UnsupportedParticleSnapshot("unsupported_parameter_schema: Parameters missing")
    if not isinstance(system["Parameters"], dict):
        raise UnsupportedParticleSnapshot("unsupported_parameter_schema: Parameters is not an object")
    parameters = _record(system["Parameters"], "Parameters")
    if "SchemaVersion" not in parameters:
        raise UnsupportedParticleSnapshot("unsupported_parameter_schema: SchemaVersion missing")
    version = _integer(parameters["SchemaVersion"], "Parameters.SchemaVersion")
    if version != PARTICLE_PARAMETER_SCHEMA_VERSION:
        raise UnsupportedParticleSnapshot(
            f"unsupported_parameter_schema: required={PARTICLE_PARAMETER_SCHEMA_VERSION}, actual={version}")
    for module_name in PARTICLE_PARAMETER_MODULE_FIELDS:
        if module_name not in parameters:
            raise UnsupportedParticleSnapshot(
                f"unsupported_parameter_schema: required field missing: Parameters.{module_name}")
    initial = _record(_field(parameters, "Initial", "Parameters"), "Initial")
    emission = _record(_field(parameters, "Emission", "Parameters"), "Emission")
    shape = _field(parameters, "Shape", "Parameters")
    size = _record(_field(parameters, "SizeOverLifetime", "Parameters"), "SizeOverLifetime")
    color = _record(_field(parameters, "ColorOverLifetime", "Parameters"), "ColorOverLifetime")
    trail = _record(_field(parameters, "Trail", "Parameters"), "Trail")
    custom = _record(_field(parameters, "CustomData", "Parameters"), "CustomData")
    uv = _record(_field(parameters, "Uv", "Parameters"), "Uv")
    velocity_parameters = _field(parameters, "VelocityOverLifetime", "Parameters")
    states = _sequence(_field(system, "ModuleStates", "System"), "System.ModuleStates")
    modeled = {"InitialModule", "EmissionModule", "ShapeModule", "SizeModule", "ColorModule",
               "TrailModule", "CustomDataModule", "UVModule", "VelocityModule"}
    enabled: dict[str, bool] = {}
    for item in states:
        row = _record(item, "ModuleState")
        name = _field(row, "Name", "ModuleState")
        if type(name) is not str or name in enabled:
            raise ParticleSnapshotError(f"Module state name is missing or duplicated: name={name!r}")
        enabled[name] = _boolean(_field(row, "Enabled", "ModuleState"), "ModuleState.Enabled")
    for name in modeled:
        if name not in enabled:
            raise ParticleSnapshotError(f"Required module state missing: name={name}")
    if not enabled["InitialModule"]:
        raise UnsupportedParticleSnapshot("InitialModule disabled; lifetime and speed semantics are unavailable")
    unsupported = [name for name, active in enabled.items() if active and name not in modeled]
    velocity_curves, velocity_issues = _constant_velocity_curves(
        velocity_parameters, enabled["VelocityModule"])
    unsupported.extend(velocity_issues)
    rotation_3d = _boolean(_field(initial, "Rotation3D", "Initial"), "Initial.Rotation3D")
    if rotation_3d:
        unsupported.append("InitialModule.Rotation3D")
    for name in ("StartRotationX", "StartRotationY"):
        if not _trivially_zero_curve(_field(initial, name, "Initial"), f"Initial.{name}"):
            unsupported.append(f"InitialModule.{name}")
    scaling_mode = _integer(_field(system, "ScalingMode", "System"), "System.ScalingMode")
    if scaling_mode not in (0, 1, 2):
        raise UnsupportedParticleSnapshot(f"Unsupported particle scaling mode: mode={scaling_mode}")
    if _boolean(_field(trail, "Enabled", "Trail"), "Trail.Enabled") != enabled["TrailModule"]:
        raise ParticleSnapshotError("Trail module state disagrees with typed parameters")
    if _boolean(_field(custom, "Enabled", "CustomData"), "CustomData.Enabled") != enabled["CustomDataModule"]:
        raise ParticleSnapshotError("CustomData module state disagrees with typed parameters")
    if _boolean(_field(uv, "Enabled", "Uv"), "Uv.Enabled") != enabled["UVModule"]:
        raise ParticleSnapshotError("UV module state disagrees with typed parameters")
    if enabled["TrailModule"] and _boolean(_field(trail, "WorldSpace", "Trail"), "Trail.WorldSpace"):
        unsupported.append("TrailModule.WorldSpace_static_anchor_only")
    if _integer(_field(system, "MoveWithTransform", "System"), "System.MoveWithTransform") != 0:
        raise UnsupportedParticleSnapshot("Nonzero simulation-space mode requires verified transform semantics")
    if _boolean(_field(system, "Prewarm", "System"), "System.Prewarm"):
        raise UnsupportedParticleSnapshot("Prewarm requires simulation before the requested snapshot")
    gravity_curve = parse_minmax_curve(_field(initial, "GravityModifier", "Initial"),
                                       "Initial.GravityModifier")
    if gravity_curve.mode not in (0, 3):
        raise UnsupportedParticleSnapshot(
            f"Gravity modifier varies over time and requires trajectory integration: mode={gravity_curve.mode}")
    system_seed = _integer(_field(system, "RandomSeed", "System"), "System.RandomSeed")
    births = (_birth_times(system, emission, age, max_events, random.Random(_seed(identity, system_seed)))
              if enabled["EmissionModule"] else ())
    duration = _number(_field(system, "LengthInSec", "System"), "System.LengthInSec")
    speed_multiplier = _number(_field(system, "SimulationSpeed", "System"), "System.SimulationSpeed")
    if speed_multiplier != 1:
        raise UnsupportedParticleSnapshot(f"Simulation speed is unsupported: speed={speed_multiplier}")
    alive: list[SnapshotParticle] = []
    for index, birth in enumerate(births):
        rng = random.Random(_seed(f"{identity}:{index}", system_seed))
        emitter_time = (birth % duration) / duration
        life = _curve(initial, "StartLifetime", emitter_time, rng, "Initial")
        if life <= 0:
            raise ParticleSnapshotError(f"Particle lifetime must be positive: index={index}, lifetime={life}")
        elapsed = (age - birth) * speed_multiplier
        if elapsed < 0 or elapsed >= life:
            continue
        fraction = elapsed / life
        position, direction = _shape(shape, rng)
        start_speed = _curve(initial, "StartSpeed", emitter_time, rng, "Initial")
        initial_velocity = tuple(component * start_speed for component in direction)
        if velocity_curves:
            velocity_rng = random.Random(_seed(f"{identity}:{index}:velocity", system_seed))
            initial_velocity = tuple(
                component + evaluate_minmax_curve(curve, 0.0, _unit(velocity_rng))
                for component, curve in zip(initial_velocity, velocity_curves))
        gravity_modifier = _curve(initial, "GravityModifier", emitter_time, rng, "Initial")
        acceleration = tuple(component * gravity_modifier for component in gravity)
        birth_position = position
        position = _trajectory(birth_position, initial_velocity, acceleration, elapsed)
        velocity = tuple(speed + force * elapsed for speed, force in zip(initial_velocity, acceleration))
        rotation = _curve(initial, "StartRotation", emitter_time, rng, "Initial")
        rotation_probability = _number(_field(initial, "RandomizeRotationDirection", "Initial"),
                                       "Initial.RandomizeRotationDirection")
        if not 0 <= rotation_probability <= 1:
            raise ParticleSnapshotError(f"Rotation direction probability outside [0,1]: value={rotation_probability}")
        if _unit(rng) < rotation_probability:
            rotation = -rotation
        start_size = _curve(initial, "StartSize", emitter_time, rng, "Initial")
        size_3d = _boolean(_field(initial, "Size3D", "Initial"), "Initial.Size3D")
        if size_3d:
            initial_y = _curve(initial, "StartSizeY", emitter_time, rng, "Initial")
            initial_z = _curve(initial, "StartSizeZ", emitter_time, rng, "Initial")
        else:
            initial_y = initial_z = start_size
        if _boolean(_field(size, "Enabled", "SizeOverLifetime"), "SizeOverLifetime.Enabled"):
            x_scale = _curve(size, "Curve", fraction, rng, "SizeOverLifetime")
            separate = _boolean(_field(size, "SeparateAxes", "SizeOverLifetime"), "SizeOverLifetime.SeparateAxes")
            y_scale = _curve(size, "Y", fraction, rng, "SizeOverLifetime") if separate else x_scale
            z_scale = _curve(size, "Z", fraction, rng, "SizeOverLifetime") if separate else x_scale
        else:
            x_scale = y_scale = z_scale = 1.0
        initial_color = _color(_field(initial, "StartColor", "Initial"), emitter_time, rng, "Initial.StartColor")
        lifetime_color = _color(_field(color, "Gradient", "ColorOverLifetime"), fraction, rng, "ColorOverLifetime.Gradient") if _boolean(_field(color, "Enabled", "ColorOverLifetime"), "ColorOverLifetime.Enabled") else (1.0, 1.0, 1.0, 1.0)
        rgba = tuple(a * b for a, b in zip(initial_color, lifetime_color))
        trail_points = _trail(trail, birth_position, initial_velocity, acceleration, elapsed,
                              fraction, rgba, random.Random(_seed(f"{identity}:{index}:trail", system_seed))) if enabled["TrailModule"] else ()
        streams = _sequence(_field(custom, "Streams", "CustomData"), "CustomData.Streams")
        if len(streams) != 2:
            raise ParticleSnapshotError(f"CustomData stream count invalid: count={len(streams)}")
        custom_values = tuple(_custom_stream(stream, fraction, rng, f"CustomData.Streams[{stream_index}]")
                              for stream_index, stream in enumerate(streams)) if enabled["CustomDataModule"] else (None, None)
        tile = _uv_rect(uv, fraction, rng) if enabled["UVModule"] else (0.0, 0.0, 1.0, 1.0)
        alive.append(SnapshotParticle(index, birth, position, velocity,
                                      (start_size * x_scale, initial_y * y_scale, initial_z * z_scale),
                                      rgba, fraction, rotation, trail_points,
                                      custom_values[0], custom_values[1], tile))
    count = len(alive)
    system_limit = _integer(_field(initial, "MaxNumParticles", "Initial"), "Initial.MaxNumParticles")
    if system_limit < 0:
        raise ParticleSnapshotError(f"Negative maximum alive particles: maximum={system_limit}")
    kept = min(max_particles, system_limit)
    return ParticleSnapshot(tuple(alive[:kept]), count, max(0, count - kept),
                            tuple(unsupported), "deterministic_static_preview_not_unity_simulation")
