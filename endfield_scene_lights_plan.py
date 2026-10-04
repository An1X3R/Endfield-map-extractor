"""Select source-qualified native Light records for a portable scene region."""

from __future__ import annotations

from dataclasses import asdict, dataclass
import math
from typing import Mapping, TypedDict, cast

from extractor_core.lights import NativeLight, NativeLightInventory


class LightPropertyPlan(TypedDict):
    encoded_id: int
    data_type: int
    raw_hex: str


class LightPlanRow(TypedDict):
    map_id: str
    entity_id: int
    name: str
    position: list[float]
    euler_zxy: list[float]
    scale: list[float]
    light_type: int
    shape: int
    color: list[float]
    intensity: float
    range: float
    spot_angle: float
    inner_spot_angle: float
    linear_length: float
    color_temperature: float
    use_color_temperature: bool
    enable_obb: bool
    character_only: bool
    falloff_exponent: float
    cookie_path: str | None
    component_mask: int
    light_properties: list[LightPropertyPlan]
    additional_properties: list[LightPropertyPlan]
    source_init_paths: list[str]
    source_streaming_paths: list[str]
    baseline_sha256: str
    source_scope: str
    runtime_activation: str
    intensity_unit: str
    color_space: str
    selection_scope: str


@dataclass(frozen=True)
class LightPlan:
    rows: tuple[LightPlanRow, ...]
    pending: tuple[dict[str, object], ...]
    required_resource_paths: tuple[str, ...]
    source_sha256: tuple[tuple[str, str], ...]
    selected_count: int


def _bound(bounds: Mapping[str, object], name: str) -> float:
    value = bounds.get(name)
    if type(value) not in (float, int) or not math.isfinite(float(value)):
        raise ValueError(f"Native Light region bound must be finite: field={name}, value={value!r}")
    return float(value)


def _selection_scope(light: NativeLight, xmin: float, xmax: float,
                     zmin: float, zmax: float, selection_xmin: float,
                     selection_zmin: float) -> str | None:
    if light.light_type == 1:
        return ("directional_global" if xmin <= selection_xmin < xmax and
                zmin <= selection_zmin < zmax else None)
    x, z = light.position[0], light.position[2]
    if xmin <= x < xmax and zmin <= z < zmax:
        return "center_half_open"
    return None


def build_light_plan(inventory: NativeLightInventory, bounds: Mapping[str, object],
                     selection: Mapping[str, object]) -> LightPlan:
    """Use Init world-space centers and preserve unsupported source behavior in the plan."""
    xmin, xmax = _bound(bounds, "xmin"), _bound(bounds, "xmax")
    zmin, zmax = _bound(bounds, "zmin"), _bound(bounds, "zmax")
    sxmin, sxmax = _bound(selection, "xmin"), _bound(selection, "xmax")
    szmin, szmax = _bound(selection, "zmin"), _bound(selection, "zmax")
    if xmin >= xmax or zmin >= zmax:
        raise ValueError(f"Native Light region is empty: bounds={(xmin, xmax, zmin, zmax)}")
    if sxmin >= sxmax or szmin >= szmax or xmin < sxmin or xmax > sxmax or zmin < szmin or zmax > szmax:
        raise ValueError(f"Native Light batch must be inside selection: bounds={(xmin, xmax, zmin, zmax)}, selection={(sxmin, sxmax, szmin, szmax)}")
    rows: list[LightPlanRow] = []
    selected_sources: set[str] = set()
    for light in inventory.rows:
        selection_scope = _selection_scope(light, xmin, xmax, zmin, zmax, sxmin, szmin)
        if selection_scope is None:
            continue
        if light.light_type not in (0, 1, 2, 3):
            raise ValueError(f"Native Light type unsupported by source schema: map={light.map_id}, entity={light.entity_id}, type={light.light_type}")
        row = asdict(light)
        row["position"] = list(light.position)
        row["euler_zxy"] = list(light.euler_zxy)
        row["scale"] = list(light.scale)
        row["color"] = list(light.color)
        row["source_init_paths"] = list(light.source_init_paths)
        row["source_streaming_paths"] = list(light.source_streaming_paths)
        row["light_properties"] = [asdict(prop) for prop in light.light_properties]
        row["additional_properties"] = [asdict(prop) for prop in light.additional_properties]
        row["source_scope"] = "native_init_baseline_with_streaming_cookie"
        row["runtime_activation"] = "unverified"
        row["intensity_unit"] = "unverified_source_scalar"
        row["color_space"] = "unverified_source_rgba"
        row["selection_scope"] = selection_scope
        rows.append(cast(LightPlanRow, row))
        selected_sources.update(light.source_init_paths)
        selected_sources.update(light.source_streaming_paths)
    source_map = dict(inventory.source_sha256)
    if any(source not in source_map for source in selected_sources):
        raise ValueError("Selected native Light source fingerprint missing")
    return LightPlan(tuple(rows), (), (),
                     tuple((path, source_map[path]) for path in sorted(selected_sources)), len(rows))


def validate_light_plan_rows(value: object, map_id: str,
                             source_hashes: Mapping[str, str]) -> tuple[LightPlanRow, ...]:
    """Validate portable Light identities and the source files that anchor every row."""
    if not isinstance(value, list):
        raise ValueError("Light plan rows must be an array")
    if map_id not in ("map01", "map02"):
        raise ValueError(f"Light plan map invalid: {map_id}")
    seen: set[int] = set()
    rows: list[LightPlanRow] = []
    for index, item in enumerate(value):
        if not isinstance(item, dict) or any(type(key) is not str for key in item):
            raise ValueError(f"Light plan row must be an object: index={index}")
        context = f"light_instances[{index}]"
        def required(name: str) -> object:
            if name not in item:
                raise ValueError(f"Light plan required field missing: {context}.{name}")
            return item[name]
        def number(name: str) -> float:
            field = required(name)
            if type(field) not in (int, float) or not math.isfinite(float(field)):
                raise ValueError(f"Light plan finite number required: {context}.{name}={field!r}")
            return float(field)
        def vector(name: str, length: int) -> list[float]:
            field = required(name)
            if not isinstance(field, list) or len(field) != length or any(type(part) not in (int, float) or not math.isfinite(float(part)) for part in field):
                raise ValueError(f"Light plan vector invalid: {context}.{name}")
            return [float(part) for part in field]
        entity_id = required("entity_id")
        if type(entity_id) is not int or entity_id in seen or required("map_id") != map_id:
            raise ValueError(f"Light plan identity invalid or duplicate: {context}, entity={entity_id}")
        seen.add(entity_id)
        if type(required("name")) is not str or not required("name"):
            raise ValueError(f"Light plan name invalid: {context}")
        for name in ("position", "euler_zxy", "scale"):
            vector(name, 3)
        vector("color", 4)
        light_type, shape = required("light_type"), required("shape")
        if type(light_type) is not int or light_type not in (0, 1, 2, 3) or type(shape) is not int or shape != 0:
            raise ValueError(f"Light plan type or shape invalid: {context}, type={light_type}, shape={shape}")
        if number("intensity") < 0 or number("range") <= 0 or not 0 < number("spot_angle") < 180:
            raise ValueError(f"Light plan intensity, range or cone invalid: {context}")
        if not 0 <= number("inner_spot_angle") <= number("spot_angle"):
            raise ValueError(f"Light plan inner cone invalid: {context}")
        for name in ("linear_length", "color_temperature", "falloff_exponent"):
            number(name)
        for name in ("use_color_temperature", "enable_obb", "character_only"):
            if type(required(name)) is not bool:
                raise ValueError(f"Light plan boolean invalid: {context}.{name}")
        if type(required("component_mask")) is not int or not required("component_mask") & 131072:
            raise ValueError(f"Light plan component mask invalid: {context}")
        cookie = required("cookie_path")
        if cookie is not None and (type(cookie) is not str or not cookie.startswith("Assets/")):
            raise ValueError(f"Light plan Cookie path invalid: {context}")
        baseline = required("baseline_sha256")
        if type(baseline) is not str or len(baseline) != 64 or any(char not in "0123456789abcdef" for char in baseline):
            raise ValueError(f"Light plan baseline fingerprint invalid: {context}")
        if (required("source_scope") != "native_init_baseline_with_streaming_cookie" or
                required("runtime_activation") != "unverified" or
                required("intensity_unit") != "unverified_source_scalar" or
                required("color_space") != "unverified_source_rgba"):
            raise ValueError(f"Light plan source scope invalid: {context}")
        selection = required("selection_scope")
        expected_scopes = (("directional_global",) if light_type == 1 else
                           ("center_half_open",))
        if selection not in expected_scopes:
            raise ValueError(f"Light plan selection scope invalid: {context}")
        init_paths, stream_paths = required("source_init_paths"), required("source_streaming_paths")
        if (not isinstance(init_paths, list) or not isinstance(stream_paths, list) or
                not init_paths or len(init_paths) != len(stream_paths)):
            raise ValueError(f"Light plan source pairs invalid: {context}")
        for init_path, stream_path in zip(init_paths, stream_paths, strict=True):
            if (type(init_path) is not str or type(stream_path) is not str or
                    not init_path.startswith(f"Data/Streaming/PC/{map_id}/Streaming/InitChunkData_") or
                    stream_path != init_path.replace("/InitChunkData_", "/StreamingChunkData_")):
                raise ValueError(f"Light plan source pair path invalid: {context}")
            for path in (init_path, stream_path):
                digest = source_hashes.get(path)
                if type(digest) is not str or len(digest) != 64 or any(char not in "0123456789abcdef" for char in digest):
                    raise ValueError(f"Light plan source SHA-256 missing: {context}, source={path}")
        for name, count in (("light_properties", 60), ("additional_properties", 21)):
            properties = required(name)
            if not isinstance(properties, list) or len(properties) != count:
                raise ValueError(f"Light plan raw property array invalid: {context}.{name}")
        rows.append(cast(LightPlanRow, dict(item)))
    return tuple(rows)
