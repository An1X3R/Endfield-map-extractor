"""Validate camera-bound source inputs for the supported VFXRefract snapshot branch."""

from __future__ import annotations

import math
from typing import Literal, TypedDict, cast

from endfield_scene_material_contract import MaterialRecord


class SceneColorSnapshotPlan(TypedDict):
    format: Literal["EndfieldSceneColorSnapshot/1"]
    camera_matrix_world: list[float]
    lens_mm: float
    resolution: list[int]
    engine: Literal["BLENDER_EEVEE_NEXT", "CYCLES"]
    samples: int
    time_seconds: float
    clip_start: float
    clip_end: float


_ROTATION_TOLERANCE = 1e-4
_VFXREFRACT_KEYWORDS = frozenset(("_USE_MASK", "_USE_RBOFFSET"))
_VFXREFRACT_FLOATS = {
    "_SurfaceType": 1,
    "_BlendMode": 0,
    "_SrcBlend": 5,
    "_DstBlend": 10,
    "_AlphaSrcBlend": 1,
    "_AlphaDstBlend": 10,
    "_ZWrite": 0,
    "_ZTest": 4,
    "_DisableZTest": 0,
    "_UseMask": 1,
    "_UseRBOffset": 1,
    "_UseRGBOffset": 0,
    "_UseBlend": 0,
    "_UseSaturation": 0,
    "_UseColorTex": 0,
    "_UseFresnel": 0,
    "_UseDissolve": 0,
    "_UseSpread": 0,
    "_UseTrail": 0,
    "_UseSoftBlend": 0,
    "_UsePolarUV": 0,
    "_RefractSwitchUV": 0,
    "_MaskSwitchUV": 0,
    "_DisableParticleInstanceColor": 0,
    "_DisableOccludedRefract": 0,
}
_VFXREFRACT_BINARY_FLOATS = (
    "_RefractIsNormal",
    "_RefractUseRBOffset",
    "_UseMaskTexAsAlpha",
    "_UseNearCameraFade",
    "_MaskTexUseRefract",
)


def _finite_number(value: object, field: str) -> float:
    if type(value) not in (int, float):
        raise ValueError(f"scene_color_snapshot.{field} must be a finite number")
    try:
        number = float(value)
    except OverflowError as error:
        raise ValueError(f"scene_color_snapshot.{field} must be a finite number") from error
    if not math.isfinite(number):
        raise ValueError(f"scene_color_snapshot.{field} must be a finite number")
    return number


def _rigid_camera_matrix(value: object) -> list[float]:
    if not isinstance(value, list) or len(value) != 16:
        raise ValueError("scene_color_snapshot.camera_matrix_world must contain 16 row-major values")
    matrix = [_finite_number(item, "camera_matrix_world") for item in value]
    if any(abs(matrix[index] - expected) > _ROTATION_TOLERANCE
           for index, expected in ((12, 0.0), (13, 0.0), (14, 0.0), (15, 1.0))):
        raise ValueError("scene_color_snapshot.camera_matrix_world must have an affine bottom row")
    rows = (matrix[0:3], matrix[4:7], matrix[8:11])
    for left in range(3):
        for right in range(3):
            dot = sum(rows[left][axis] * rows[right][axis] for axis in range(3))
            expected = 1.0 if left == right else 0.0
            if abs(dot - expected) > _ROTATION_TOLERANCE:
                raise ValueError("scene_color_snapshot.camera_matrix_world rotation must be orthonormal")
    a, b, c = rows
    determinant = (a[0] * (b[1] * c[2] - b[2] * c[1])
                   - a[1] * (b[0] * c[2] - b[2] * c[0])
                   + a[2] * (b[0] * c[1] - b[1] * c[0]))
    if abs(determinant - 1.0) > _ROTATION_TOLERANCE:
        raise ValueError("scene_color_snapshot.camera_matrix_world rotation determinant must be +1")
    return matrix


def read_scene_color_snapshot(value: object) -> SceneColorSnapshotPlan:
    """Validate a camera-bound snapshot recipe and discard unrelated fields."""
    if not isinstance(value, dict) or any(not isinstance(key, str) for key in value):
        raise ValueError("scene_color_snapshot must be an object")
    if value.get("format") != "EndfieldSceneColorSnapshot/1":
        raise ValueError("scene_color_snapshot.format must be EndfieldSceneColorSnapshot/1")
    matrix = _rigid_camera_matrix(value.get("camera_matrix_world"))
    lens_mm = _finite_number(value.get("lens_mm"), "lens_mm")
    if lens_mm <= 0.0:
        raise ValueError("scene_color_snapshot.lens_mm must be positive")
    resolution = value.get("resolution")
    if (not isinstance(resolution, list) or len(resolution) != 2
            or any(type(component) is not int or not 1 <= component <= 4096 for component in resolution)):
        raise ValueError("scene_color_snapshot.resolution must contain two integers from 1 through 4096")
    engine = value.get("engine")
    if engine not in ("BLENDER_EEVEE_NEXT", "CYCLES"):
        raise ValueError("scene_color_snapshot.engine must be BLENDER_EEVEE_NEXT or CYCLES")
    samples = value.get("samples")
    if type(samples) is not int or not 1 <= samples <= 512:
        raise ValueError("scene_color_snapshot.samples must be an integer from 1 through 512")
    time_seconds = _finite_number(value.get("time_seconds"), "time_seconds")
    if time_seconds < 0.0:
        raise ValueError("scene_color_snapshot.time_seconds must be nonnegative")
    clip_start = _finite_number(value.get("clip_start"), "clip_start")
    clip_end = _finite_number(value.get("clip_end"), "clip_end")
    if clip_start <= 0.0 or clip_end <= 0.0 or clip_end <= clip_start:
        raise ValueError("scene_color_snapshot clip distances must be positive and clip_end greater than clip_start")
    return {
        "format": "EndfieldSceneColorSnapshot/1",
        "camera_matrix_world": matrix,
        "lens_mm": lens_mm,
        "resolution": list(cast(list[int], resolution)),
        "engine": cast(Literal["BLENDER_EEVEE_NEXT", "CYCLES"], engine),
        "samples": samples,
        "time_seconds": time_seconds,
        "clip_start": clip_start,
        "clip_end": clip_end,
    }


def vfxrefract_snapshot_issues(record: MaterialRecord, vertex_color_components: int | None) -> tuple[str, ...]:
    """Return source-contract failures for the verified mask/RB VFXRefract branch."""
    issues: list[str] = []
    if record["Shader"].get("Name") != "HGRP/Effect/VFXRefract":
        issues.append("shader_family")
    render_state = record.get("RenderState")
    keywords = None if render_state is None else render_state.get("ValidKeywords")
    if (not isinstance(keywords, list) or any(not isinstance(keyword, str) for keyword in keywords)
            or set(keywords) != _VFXREFRACT_KEYWORDS):
        issues.append("compiled_mask_rb_keyword_contract")
    floats = record["Floats"]
    issues.extend(name for name, expected in _VFXREFRACT_FLOATS.items()
                  if type(floats.get(name)) not in (int, float) or floats.get(name) != expected)
    for name in _VFXREFRACT_BINARY_FLOATS:
        value = floats.get(name)
        if type(value) not in (int, float) or value not in (0, 1):
            issues.append(name)
    cull_mode = floats.get("_CullMode")
    if type(cull_mode) not in (int, float) or cull_mode != 0:
        issues.append("source_double_sided_required")
    if vertex_color_components is not None and (
            type(vertex_color_components) is not int or vertex_color_components not in (0, 4)):
        issues.append("source_rgba_or_explicit_missing_color_reference_required")
    colors = record["Colors"]
    for name in ("_RefractUVSpeed", "_MaskTexUVSpeed"):
        value = colors.get(name)
        if not isinstance(value, list) or len(value) != 4 or value[2:] != [0, 0]:
            issues.append(name + "_custom1_not_available")
    if floats.get("_UseNearCameraFade") == 1:
        for start, end in (("_NearCameraFadeDistanceStart", "_NearCameraFadeDistanceEnd"),
                           ("_NearCameraFadeDistanceStart2", "_NearCameraFadeDistanceEnd2")):
            if floats.get(start) == floats.get(end):
                issues.append("near_fade_zero_denominator")
    return tuple(issues)
