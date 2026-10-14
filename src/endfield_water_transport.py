"""Source water coefficients and analytic transport with explicit host lighting inputs."""
from __future__ import annotations

from dataclasses import dataclass
import math
from typing import NotRequired, TypedDict, cast

from endfield_scene_material_contract import require_object

RGB = tuple[float, float, float]
SHVector = tuple[float, float, float, float]
WaterAmbientL1 = tuple[SHVector, SHVector, SHVector]


def validate_water_ambient_l1(coefficients: WaterAmbientL1, environment_scale: float) -> None:
    """Validate an explicitly selected native L1 buffer, including signed terms."""
    if len(coefficients) != 3 or any(len(channel) != 4 for channel in coefficients):
        raise ValueError("Water ambient L1 requires three four-component channel vectors")
    for channel in coefficients:
        for value in channel:
            _finite(value, "water ambient L1 coefficient")
    _finite(environment_scale, "water environment global scale")


def evaluate_water_ambient_l1(coefficients: WaterAmbientL1, source_normal: RGB,
                             environment_scale: float) -> RGB:
    """Evaluate the source default-SH branch with local irradiance volumes disabled.

    The input vectors already include their explicitly selected writer intensity.
    No native renderer route, environment phase or local probe grid is inferred.
    """
    validate_water_ambient_l1(coefficients, environment_scale)
    if len(source_normal) != 3:
        raise ValueError("Water source normal requires three components")
    for value in source_normal:
        _finite(value, "water source normal")
    if not math.isclose(sum(value * value for value in source_normal), 1.0, abs_tol=1e-5):
        raise ValueError("Water source normal must have unit length")
    return cast(RGB, tuple(max(0.0, sum(channel[index] * source_normal[index]
        for index in range(3)) + channel[3]) * environment_scale for channel in coefficients))


class WaterAmbientL1Row(TypedDict):
    coefficients: list[list[float]]
    environment_global_scale: float


@dataclass(frozen=True)
class WaterAmbientL1Lighting:
    """Explicit source default-SH buffer, with local irradiance volumes excluded."""

    coefficients: WaterAmbientL1
    environment_global_scale: float


def read_water_ambient_l1(value: object) -> WaterAmbientL1Lighting:
    """Validate source-selected SH input without inventing missing coefficients."""
    row = require_object(value, "water_transport_lighting.ambient_l1")
    raw = row.get("coefficients")
    if (not isinstance(raw, (list, tuple)) or len(raw) != 3
            or any(not isinstance(channel, (list, tuple)) or len(channel) != 4 for channel in raw)):
        raise ValueError("water_transport_lighting.ambient_l1.coefficients requires three four-component vectors")
    coefficients = cast(WaterAmbientL1, tuple(tuple(_finite(component, "ambient_l1.coefficients")
                                                  for component in channel) for channel in raw))
    scale = _finite(row.get("environment_global_scale"), "ambient_l1.environment_global_scale")
    validate_water_ambient_l1(coefficients, scale)
    return WaterAmbientL1Lighting(coefficients, scale)


class WaterTransportLightingRow(TypedDict):
    map_id: str
    entity_id: int
    ambient_radiance: list[float]
    radiance_scale: float
    phase_atmosphere_rgb: NotRequired[list[float]]
    ambient_l1: NotRequired[WaterAmbientL1Row]


@dataclass(frozen=True)
class WaterTransportLighting:
    """Explicit host inputs, not inferred native runtime activation."""

    map_id: str
    entity_id: int
    ambient_radiance: RGB
    radiance_scale: float
    phase_atmosphere_rgb: RGB | None
    ambient_l1: WaterAmbientL1Lighting | None


def read_water_transport_lighting(value: object) -> WaterTransportLighting:
    """Validate the opt-in prepared-scene lighting contract before Blender starts."""
    from extractor_core.scene_identity import validate_scene_id

    row = require_object(value, "water_transport_lighting")
    map_id = row.get("map_id")
    if not isinstance(map_id, str):
        raise ValueError("water_transport_lighting.map_id must be a scene id")
    validate_scene_id(map_id)
    entity_id = row.get("entity_id")
    if type(entity_id) is not int:
        raise ValueError("water_transport_lighting.entity_id must be an integer")
    raw = row.get("ambient_radiance")
    if not isinstance(raw, (list, tuple)) or len(raw) != 3:
        raise ValueError("water_transport_lighting.ambient_radiance must have three linear RGB channels")
    ambient = tuple(_finite(component, "ambient_radiance") for component in raw)
    scale = _finite(row.get("radiance_scale"), "water_transport_lighting.radiance_scale")
    if any(component < 0 for component in ambient) or scale <= 0:
        raise ValueError("Water host ambient radiance must be nonnegative and radiance_scale positive")
    phase_rgb: RGB | None = None
    if "phase_atmosphere_rgb" in row:
        raw_phase = row["phase_atmosphere_rgb"]
        if not isinstance(raw_phase, (list, tuple)) or len(raw_phase) != 3:
            raise ValueError("water_transport_lighting.phase_atmosphere_rgb must have three linear RGB channels")
        phase_values = tuple(_finite(component, "phase_atmosphere_rgb") for component in raw_phase)
        if any(component < 0 for component in phase_values):
            raise ValueError("Water phase atmosphere RGB must be nonnegative")
        phase_rgb = cast(RGB, phase_values)
    ambient_l1 = read_water_ambient_l1(row["ambient_l1"]) if "ambient_l1" in row else None
    return WaterTransportLighting(map_id, entity_id, cast(RGB, ambient), scale, phase_rgb, ambient_l1)


def serialize_water_transport_lighting(value: WaterTransportLighting) -> WaterTransportLightingRow:
    """Serialize validated water inputs without carrying unrelated request fields."""
    row: WaterTransportLightingRow = {
        "map_id": value.map_id,
        "entity_id": value.entity_id,
        "ambient_radiance": list(value.ambient_radiance),
        "radiance_scale": value.radiance_scale,
    }
    if value.phase_atmosphere_rgb is not None:
        row["phase_atmosphere_rgb"] = list(value.phase_atmosphere_rgb)
    if value.ambient_l1 is not None:
        row["ambient_l1"] = {
            "coefficients": [list(channel) for channel in value.ambient_l1.coefficients],
            "environment_global_scale": value.ambient_l1.environment_global_scale,
        }
    return row


@dataclass(frozen=True)
class WaterTransport:
    absorption_near: RGB
    absorption_far: RGB
    scattering: RGB
    safe_distance: float
    phase_mix: float
    environment_intensity: float
    scene_color_environment_influence: float


@dataclass(frozen=True)
class WaterEdgeBlend:
    distance_edge_blend_factor: float


def read_water_edge_blend(value: object) -> WaterEdgeBlend:
    """Read the serialized controls for the final background/water coverage mix."""
    config = require_object(value, "water configuration")
    optics = require_object(config.get("Optics"), "water Optics")
    factor = _finite(optics.get("DistanceEdgeBlendFactor"), "DistanceEdgeBlendFactor")
    if factor < 0:
        raise ValueError("Water DistanceEdgeBlendFactor must be nonnegative")
    return WaterEdgeBlend(factor)


def evaluate_water_edge_blend(settings: WaterEdgeBlend, depth: float,
                              view_distance: float, view_normal_cosine: float) -> float:
    """Evaluate the final compiled coverage using explicit host geometric inputs."""
    for key, value in (("depth", depth), ("view_distance", view_distance),
                       ("view_normal_cosine", view_normal_cosine)):
        _finite(value, key)
    if depth < 0 or view_distance < 0 or not -1 <= view_normal_cosine <= 1:
        raise ValueError("Water edge depth/distance must be nonnegative and cosine within [-1, 1]")
    distance = view_distance * 0.05000000074505806
    angle = 1.0 + (abs(view_normal_cosine) - 1.0) * min(distance, 1.0)
    # The upload writes zero to the subtraction slot; Appearance.DistanceBlend is unrelated.
    value = min(depth, 10000.0) * angle * settings.distance_edge_blend_factor
    return min(max(value, 0.0), 1.0)


def _finite(value: object, field: str) -> float:
    if type(value) not in (int, float) or not math.isfinite(value):
        raise ValueError(f"Water transport requires a finite number: field={field}, value={value!r}")
    return float(value)


def _spectrum(value: object, field: str) -> RGB:
    row = require_object(value, field)
    result = tuple(_finite(row.get(key), field + "." + key) for key in ("X", "Y", "Z"))
    if any(channel < 0 for channel in result):
        raise ValueError(f"Water transport coefficient must be nonnegative: field={field}")
    return cast(RGB, result)


def read_water_transport(value: object) -> WaterTransport:
    """Read serialized coefficients without deriving them again from artistic tint controls."""
    config = require_object(value, "water configuration")
    optics = require_object(config.get("Optics"), "water Optics")
    result = WaterTransport(
        _spectrum(optics.get("WaterRayleighAbsorptionMeter2"), "WaterRayleighAbsorptionMeter2"),
        _spectrum(optics.get("WaterRayleighAbsorptionMeter"), "WaterRayleighAbsorptionMeter"),
        _spectrum(optics.get("WaterRayleighScatteringMeter"), "WaterRayleighScatteringMeter"),
        _finite(optics.get("SafeFullAbsorpDistance"), "SafeFullAbsorpDistance"),
        _finite(optics.get("RayleighMieLerpFactor"), "RayleighMieLerpFactor"),
        _finite(optics.get("EnvLightIntensity"), "EnvLightIntensity"),
        _finite(optics.get("SceneColorEnvInfluence"), "SceneColorEnvInfluence"),
    )
    if result.scene_color_environment_influence < 0:
        raise ValueError("SceneColorEnvInfluence must be nonnegative")
    if result.safe_distance <= 0:
        raise ValueError("SafeFullAbsorpDistance must be positive")
    for absorption in (result.absorption_near, result.absorption_far):
        if any(a + s <= 0 for a, s in zip(absorption, result.scattering, strict=True)):
            raise ValueError("Water transport source integral has a zero extinction denominator")
    return result


def evaluate_water_transport(settings: WaterTransport, depth: float, view_y: float,
                             light_y: float, phase_cosine: float,
                             ambient_radiance: RGB, direct_radiance: RGB) -> tuple[RGB, RGB]:
    """Evaluate source transport; host radiance includes upstream lighting and visibility factors."""
    for key, value in (("depth", depth), ("view_y", view_y), ("light_y", light_y),
                       ("phase_cosine", phase_cosine)):
        _finite(value, key)
    if depth < 0 or not -1 <= phase_cosine <= 1:
        raise ValueError("Water depth must be nonnegative and phase cosine must be in [-1, 1]")
    for field, rgb in (("ambient_radiance", ambient_radiance), ("direct_radiance", direct_radiance)):
        if len(rgb) != 3:
            raise ValueError(f"Water host lighting requires three channels: field={field}")
        for value in rgb:
            _finite(value, field)
    distance = min(depth, 50.0)
    ratio = min(distance / settings.safe_distance, 1.0)
    phase_base = 1.728933334350586 - 1.707551956176758 * phase_cosine
    mie = 0.2710666060447693 / max(12.566370964050293 * phase_base ** 1.5, 0.0010000000474974513)
    rayleigh = 0.05968310311436653 * (1.0 + phase_cosine * phase_cosine)
    phase = mie + (rayleigh - mie) * settings.phase_mix
    transmission: list[float] = []
    inscattering: list[float] = []
    for near, far, scatter, ambient, direct in zip(
            settings.absorption_near, settings.absorption_far, settings.scattering,
            ambient_radiance, direct_radiance, strict=True):
        sigma = scatter + near + (far - near) * ratio
        transmission.append(math.exp(-sigma * distance))
        ambient_rate = sigma * (1.0 + abs(view_y * 1.3940000534057617))
        direct_rate = sigma * (1.0 + abs(view_y / max(light_y, 0.05000000074505806)))
        ambient_integral = -math.expm1(-ambient_rate * distance) / ambient_rate
        direct_integral = -math.expm1(-direct_rate * distance) / direct_rate
        inscattering.append(scatter * (
            ambient_integral * ambient * 0.7170000076293945 * settings.environment_intensity
            + direct_integral * phase * direct))
    return cast(RGB, tuple(transmission)), cast(RGB, tuple(inscattering))


def evaluate_water_scene_color_weight(settings: WaterTransport, depth: float, view_y: float,
                                     light_y: float, phase_cosine: float,
                                     direct_radiance: RGB) -> RGB:
    """Factor the background RGB out of the two source in-scattering additions.

    The ambient addition is background * influence, not ambient * background.
    Blender supplies the background through its existing refraction closure;
    source screen sampling, caustics and native visibility are not reconstructed.
    """
    influence = _finite(settings.scene_color_environment_influence, "SceneColorEnvInfluence")
    if influence < 0:
        raise ValueError("SceneColorEnvInfluence must be nonnegative")
    _, unit_scatter = evaluate_water_transport(
        settings, depth, view_y, light_y, phase_cosine, (1.0, 1.0, 1.0), direct_radiance)
    return cast(RGB, tuple(channel * influence for channel in unit_scatter))
