"""Validate independent-scene requests without starting extraction."""
from __future__ import annotations

import os
import re
from pathlib import Path
from typing import Literal, Mapping, NotRequired, TypedDict, cast
from uuid import UUID

from endfield_water_transport import (WaterTransportLighting, WaterTransportLightingRow,
                                     read_water_transport_lighting,
                                     serialize_water_transport_lighting)
from extractor_core.scene_identity import validate_scene_id
from endfield_scene_vfxrefract_contract import SceneColorSnapshotPlan, read_scene_color_snapshot
from webui_source_contract import EXPORT_GROUPS, FogDepthPlan, read_fog_depth_options, validate_source_paths


class SceneRequestError(ValueError):
    """A scene request violates the current catalog or selected paths."""


class SceneSource(TypedDict):
    game_root: str
    cache_root: str
    blender_exe: str


class SceneOutput(TypedDict):
    root: str
    label: str


class SceneRequest(TypedDict):
    format: Literal["InstancedSceneExportRequest/1"]
    request_id: str
    scene_id: str
    catalog_id: str
    first_run_identity: str
    export_mode: Literal["blend"]
    export_groups: list[str]
    effects_mode: Literal["full_system_by_anchor"]
    source: SceneSource
    output: SceneOutput
    water_transport_lighting: NotRequired[WaterTransportLightingRow]
    fog_depth: NotRequired[FogDepthPlan]
    scene_color_snapshot: NotRequired[SceneColorSnapshotPlan]


def json_object(value: object, context: str) -> dict[str, object]:
    if not isinstance(value, dict) or any(not isinstance(key, str) for key in value):
        raise SceneRequestError(f"{context}: expected an object")
    return cast(dict[str, object], value)


def required_text(value: object, context: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise SceneRequestError(f"{context}: expected a non-empty string")
    return value.strip()


def absolute_path(value: object, context: str) -> Path:
    path = Path(required_text(value, context)).expanduser()
    if not path.is_absolute():
        raise SceneRequestError(f"{context}: 必须选择绝对路径")
    return path.resolve()


def request_uuid(value: object) -> str:
    text = required_text(value, "request_id")
    try:
        parsed = UUID(text)
    except ValueError as error:
        raise SceneRequestError("request_id: 必须为 UUID") from error
    if str(parsed) != text:
        raise SceneRequestError("request_id: 必须使用标准小写 UUID 格式")
    return text


def canonical_scene_request(payload: Mapping[str, object], catalog_id: str,
                            first_run_identity: str, runtime_paths: Mapping[str, object],
                            runtime_directory: Path, scene_ids: frozenset[str]) -> SceneRequest:
    if payload.get("format") != "InstancedSceneExportRequest/1":
        raise SceneRequestError("不支持的独立场景请求格式")
    request_id = request_uuid(payload.get("request_id"))
    scene_id = validate_scene_id(required_text(payload.get("scene_id"), "scene_id"))
    if scene_id not in scene_ids:
        raise SceneRequestError(f"当前游戏目录没有该物理场景: {scene_id}")
    if payload.get("catalog_id") != catalog_id or payload.get("first_run_identity") != first_run_identity:
        raise SceneRequestError("场景目录或首次准备身份已改变，请刷新目录后重新提交")
    if payload.get("export_mode") != "blend":
        raise SceneRequestError("当前独立场景链支持 Blender 场景；数据包模式尚未接入")
    if payload.get("effects_mode") != "full_system_by_anchor":
        raise SceneRequestError("独立场景需要 full_system_by_anchor 特效策略")
    groups = payload.get("export_groups")
    if (not isinstance(groups, list) or not groups or
            any(not isinstance(group, str) or group not in EXPORT_GROUPS for group in groups)):
        raise SceneRequestError("export_groups: 请选择至少一种有效构件")
    if len(set(groups)) != len(groups):
        raise SceneRequestError("export_groups: 构件类型不能重复")
    water_transport_lighting: WaterTransportLighting | None = None
    if "water_transport_lighting" in payload:
        try:
            water_transport_lighting = read_water_transport_lighting(payload["water_transport_lighting"])
        except ValueError as error:
            raise SceneRequestError(str(error)) from error
        if water_transport_lighting.map_id != scene_id:
            raise SceneRequestError("water_transport_lighting.map_id must match scene_id")
        if (not set(groups).intersection({"all", "water"})
                or not set(groups).intersection({"all", "lighting"})):
            raise SceneRequestError("water_transport_lighting requires water and lighting export groups")
    fog_depth = None
    if "fog_depth" in payload:
        fog_depth = read_fog_depth_options(payload["fog_depth"])
        if not set(groups).intersection({"effects", "particle"}):
            raise SceneRequestError("fog_depth: 需要包含特效或粒子构件")
    scene_color_snapshot = None
    if "scene_color_snapshot" in payload:
        try:
            scene_color_snapshot = read_scene_color_snapshot(payload["scene_color_snapshot"])
        except ValueError as error:
            raise SceneRequestError(str(error)) from error
        if not set(groups).intersection({"effects", "all"}):
            raise SceneRequestError("scene_color_snapshot requires the effects export group")
    source = json_object(payload.get("source"), "source")
    game = absolute_path(source.get("game_root"), "source.game_root")
    cache = absolute_path(source.get("cache_root"), "source.cache_root")
    blender = absolute_path(source.get("blender_exe"), "source.blender_exe")
    validate_source_paths({"game_root": str(game), "blender_exe": str(blender)})
    for field, selected in (("game_root", game), ("cache_root", cache)):
        configured = Path(required_text(runtime_paths.get(field), f"runtime.{field}"))
        configured = (configured if configured.is_absolute() else runtime_directory / configured).resolve()
        if selected != configured:
            raise SceneRequestError(f"{field}: 当前路径与首次准备不符，请先准备所选目录")
    output = json_object(payload.get("output"), "output")
    root = absolute_path(output.get("root"), "output.root")
    label = required_text(output.get("label"), "output.label")
    if re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]{0,79}", label) is None:
        raise SceneRequestError("output.label: 仅支持字母、数字、点、下划线和短横线，长度不超过80")
    for name, path in (("cache_root", cache), ("output.root", root)):
        if path.is_relative_to(game) or game.is_relative_to(path):
            raise SceneRequestError(f"{name}: 不能与游戏安装目录重叠")
        parent = path
        while not parent.exists() and parent != parent.parent:
            parent = parent.parent
        if not parent.is_dir() or not os.access(parent, os.W_OK):
            raise SceneRequestError(f"{name}: 输出位置不可写: {parent}")
    result = SceneRequest(format="InstancedSceneExportRequest/1", request_id=request_id,
                        scene_id=scene_id, catalog_id=catalog_id, first_run_identity=first_run_identity,
                        export_mode="blend", export_groups=list(groups), effects_mode="full_system_by_anchor",
                        source=SceneSource(game_root=str(game), cache_root=str(cache), blender_exe=str(blender)),
                        output=SceneOutput(root=str(root), label=label))
    if fog_depth is not None:
        result["fog_depth"] = fog_depth
    if water_transport_lighting is not None:
        result["water_transport_lighting"] = serialize_water_transport_lighting(water_transport_lighting)
    if scene_color_snapshot is not None:
        result["scene_color_snapshot"] = scene_color_snapshot
    return result


def scene_build_request(request: SceneRequest, bounds: Mapping[str, float]) -> dict[str, object]:
    """Adapt a whole physical scene to the shared builder's internal spatial plan."""
    groups = frozenset(request["export_groups"])
    return {
        "format": "EndfieldCanonicalExportJob/2", "job_id": request["request_id"],
        "map_id": request["scene_id"], "bounds": dict(bounds), "selection_bounds": dict(bounds),
        "batch_mode": "merged_selection", "selection_grid": 128, "export_mode": "blend",
        "export_groups": list(request["export_groups"]), "source": dict(request["source"]),
        "output": dict(request["output"]), "water_mode": "flowmap_fresnel",
        "effects": {"selection_mode": "full_system_by_anchor"},
        **({"water_transport_lighting": serialize_water_transport_lighting(
            read_water_transport_lighting(request["water_transport_lighting"]))}
           if "water_transport_lighting" in request else {}),
        **({"fog_depth": read_fog_depth_options(request["fog_depth"])} if "fog_depth" in request else {}),
        **({"scene_color_snapshot": read_scene_color_snapshot(request["scene_color_snapshot"])}
           if "scene_color_snapshot" in request else {}),
        "layers": {"instances": bool(groups & {"building", "prop", "unknown"}),
                   "roads": "road" in groups, "vegetation": "vegetation" in groups,
                   "terrain": "terrain" in groups, "water": "water" in groups,
                   "effects": bool(groups & {"effects", "particle"}), "lights": "lighting" in groups},
    }
