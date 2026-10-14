"""Assemble a strict WebUI result from verified portable scene artifacts."""
from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Sequence
from pathlib import Path, PureWindowsPath
from typing import Literal, TypedDict, cast

from endfield_scene_export import SceneArtifact


ExportGroup = Literal[
    "building", "prop", "lighting", "particle", "vegetation", "terrain",
    "road", "water", "effects", "unknown",
]


class InstancedSceneExportFile(TypedDict):
    relative_path: str
    role: Literal["scene", "audit", "plan"]
    bytes: int
    sha256: str


class InstancedSceneComponentCounts(TypedDict):
    exported: int | None
    pending: int
    unsupported: int


class InstancedSceneComponentSummary(TypedDict):
    building: InstancedSceneComponentCounts
    prop: InstancedSceneComponentCounts
    lighting: InstancedSceneComponentCounts
    particle: InstancedSceneComponentCounts
    vegetation: InstancedSceneComponentCounts
    terrain: InstancedSceneComponentCounts
    road: InstancedSceneComponentCounts
    water: InstancedSceneComponentCounts
    effects: InstancedSceneComponentCounts
    unknown: InstancedSceneComponentCounts


class InstancedScenePendingItem(TypedDict):
    group_id: ExportGroup
    code: str
    message: str


class InstancedSceneExportResult(TypedDict):
    format: Literal["InstancedSceneExportResult/1"]
    status: Literal["completed", "partial"]
    output_directory: str
    files: list[InstancedSceneExportFile]
    component_summary: InstancedSceneComponentSummary
    pending: list[InstancedScenePendingItem]


_GROUPS: tuple[ExportGroup, ...] = (
    "building", "prop", "lighting", "particle", "vegetation", "terrain",
    "road", "water", "effects", "unknown",
)
_EFFECT_SUCCESS_STATUSES = frozenset(("static_preview", "static_preview_partial"))
_EFFECT_NONEXPORT_STATUSES = frozenset((
    "particle_component_diagnostic", "particle_pair_missing", "renderer_disabled",
    "game_object_inactive", "no_alive_particles_at_snapshot", "mesh_export_missing",
    "mesh_import_empty", "material_slot_missing", "material_slot_null",
    "material_record_missing", "unsupported_material_slot_count", "unsupported_mesh_uv1",
    "unsupported_mesh_vertex_mapping", "unsupported_renderer_mode", "unsupported_renderer_alignment",
    "unsupported_camera_velocity_scale", "unsupported_particle_parameters",
    "unsupported_multiple_materials", "unsupported_material", "unsupported_custom_vertex_stream",
    "unsupported_vertex_offset", "unsupported_trail_custom_vertex_stream", "unsupported_direct_effect",
    "truncated_particle_limit", "truncated_effect_limit",
))
_DECAL_STATUSES = frozenset(("restored", "no_receiver_surface", "unsupported_decal_feature"))
_WATER_STATUSES = frozenset((
    "restored", "outside_clipped_mesh", "covered_by_terrain", "bottom_geometry_incomplete",
    "bottom_geometry_missing",
))
_LIGHT_STATUSES = frozenset((
    "source_light_preview_partial", "unsupported_baked_area_light", "unsupported_light_type_or_shape",
    "unsupported_character_only_render_mask", "unsupported_cookie_projection", "truncated_light_limit",
))
_PENDING_EXPLANATIONS = {
    "decal_receiver_missing": "缺少可用的贴花接收面",
    "decal_material_incomplete": "贴花材质或纹理依赖不完整",
    "renderer_material_missing": "缺少特效渲染器的材质证据",
    "mesh_export_missing": "缺少该组件所需的网格导出数据",
    "prefab_container_missing": "当前 SceneProbe 目录中缺少该特效预制体容器",
    "prefab_root_ambiguous": "特效预制体对应多个来源根对象，无法唯一确定",
    "particle_component_invalid": "特效预制体中的粒子组件配对不完整",
    "particle_component_unsupported": "特效预制体没有受支持的粒子系统与渲染器配对",
    "particle_parameter_schema_unsupported": "粒子参数结构超出当前支持范围",
    "particle_material_incomplete": "粒子材质或纹理依赖不完整",
    "mesh_component_incomplete": "特效网格组件或材质依赖不完整",
    "global_resource_path_missing": "缺少水材质全局配置资源的已验证路径",
    "terrain_height_source_missing": "缺少经验证的地形高度源，无法补齐水深",
    "source_light_preview_partial": "光源只生成源参数预览，运行时效果尚未完整还原",
    "unsupported_material": "特效材质包含当前导出器不支持的特性",
    "source_formula_preview_runtime_unresolved": "材质源公式对应的运行时行为尚未完整还原",
    "source_formula_preview_partial": "材质源公式仅部分还原",
    "bottom_geometry_incomplete": "水体底部几何未完整还原",
    "bottom_geometry_missing": "缺少水体底部几何",
    "unsupported_decal_feature": "贴花包含当前导出器不支持的特性",
    "unsupported_light_type_or_shape": "光源类型或形状尚未支持",
    "unsupported_baked_area_light": "烘焙区域光尚未支持",
    "unsupported_cookie_projection": "光源 Cookie 投影尚未支持",
}
_FX_LIMITATION_EXPLANATIONS = (
    ("Game exposure and grading not captured", "游戏曝光和调色未还原"),
    ("Game fog not reconstructed", "游戏雾效未还原"),
    ("Runtime renderer vertex-color attenuation is not captured",
     "运行时渲染器的顶点色衰减未还原"),
    ("Runtime instance vertex-color attenuation and disappearance are explicit zero references",
     "运行时实例的顶点色衰减和消失行为仍使用零值参考"),
    ("MeshRenderer missing COLOR uses an explicit white RGBA reference",
     "MeshRenderer 缺少 COLOR 顶点流；当前只使用白色参考值，未合成源数据"),
    ("Opaque VFX retains source RGB alpha premultiplication",
     "不透明 VFX 的混合和深度写入行为仍为近似"),
    ("Compiled VFXDitherAlpha HGBuffer RGB and DepthOnly discard are combined",
     "VFX Dither 的 GBuffer 与深度流程仍为合并近似"),
    ("Source TAA noise phase is an editable zero reference",
     "TAA 噪声相位与栅格采样仍为近似"),
    ("Game exposure is an explicit reference of one",
     "游戏曝光与调色尚未还原"),
)


def _object(value: object, context: str) -> dict[str, object]:
    if not isinstance(value, dict) or any(not isinstance(key, str) for key in value):
        raise ValueError(f"{context}: expected an object with string keys")
    return cast(dict[str, object], value)


def _text(value: object, context: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{context}: expected non-empty text")
    return value.strip()


def _count(value: object, context: str) -> int:
    if type(value) is not int or value < 0:
        raise ValueError(f"{context}: expected a nonnegative integer")
    return value


def _rows(document: dict[str, object], field: str, context: str) -> list[dict[str, object]]:
    value = document.get(field)
    if not isinstance(value, list):
        raise ValueError(f"{context}.{field}: expected an array")
    return [_object(item, f"{context}.{field}[{index}]") for index, item in enumerate(value)]


def _path_rows(document: dict[str, object], field: str, context: str) -> list[dict[str, object]]:
    value = document.get(field)
    if not isinstance(value, list):
        raise ValueError(f"{context}.{field}: expected an array")
    return [{"path": _text(item, f"{context}.{field}[{index}]")}
            for index, item in enumerate(value)]


def _relative_output_file(root: Path, value: object, context: str) -> tuple[Path, str]:
    raw = _text(value, context)
    windows_path = PureWindowsPath(raw)
    normalized = raw.replace("\\", "/")
    segments = normalized.split("/")
    if (windows_path.is_absolute() or windows_path.drive or normalized.startswith("/")
            or any(segment in ("", ".", "..") for segment in segments)):
        raise ValueError(f"{context}: expected a safe relative output path")
    path = root.joinpath(*segments).resolve(strict=True)
    if not path.is_file() or not path.is_relative_to(root):
        raise ValueError(f"{context}: output file is missing or escapes its root: {raw}")
    return path, "/".join(segments)


def _file_entry(path: Path, relative_path: str, role: Literal["scene", "audit", "plan"],
                expected_bytes: int | None, expected_sha256: str | None) -> InstancedSceneExportFile:
    before = path.stat()
    with path.open("rb") as stream:
        digest = hashlib.file_digest(stream, "sha256").hexdigest()
    after = path.stat()
    if before.st_size != after.st_size or before.st_size <= 0:
        raise ValueError(f"Output changed or is empty while hashing: {relative_path}")
    if expected_bytes is not None and expected_bytes != after.st_size:
        raise ValueError(f"SceneArtifact byte count differs from output: {relative_path}")
    if expected_sha256 is not None and expected_sha256 != digest:
        raise ValueError(f"SceneArtifact SHA-256 differs from output: {relative_path}")
    return InstancedSceneExportFile(relative_path=relative_path, role=role,
                                    bytes=after.st_size, sha256=digest)


def _read_document(path: Path, context: str) -> dict[str, object]:
    try:
        parsed = json.loads(path.read_text(encoding="utf-8-sig"))
    except (OSError, json.JSONDecodeError) as error:
        raise ValueError(f"{context}: could not read valid JSON from {path.name}") from error
    return _object(parsed, context)


def _group(value: object) -> ExportGroup:
    if value in ("building", "prop", "lighting", "particle", "vegetation", "terrain", "road", "water", "effects", "unknown"):
        return cast(ExportGroup, value)
    if value == "terrain-structure":
        return "terrain"
    return "unknown"


def _code_part(value: object, context: str) -> str:
    text = _text(value, context)
    normalized = re.sub(r"[^A-Za-z0-9_.-]+", "_", text).strip("._-")
    return normalized[:96] or "unspecified"


def _issue_key(source_path: str, family: str, values: Sequence[object]) -> tuple[str, ...]:
    encoded = json.dumps(list(values), ensure_ascii=False, separators=(",", ":"))
    return source_path, family, encoded


def _identity_value(value: object) -> str | None:
    if type(value) in (str, int) and str(value).strip():
        return str(value)
    if isinstance(value, (list, tuple)) and value and all(type(item) in (str, int) for item in value):
        return "#".join(str(item) for item in value)
    return None


def _issue_identity(row: dict[str, object]) -> str:
    fields = (
        ("object", "对象"), ("material", "材质"), ("entity_id", "实体 ID"),
        ("component", "构件"), ("source_key", "源记录"), ("prefab_path", "FX 资源"),
        ("map_id", "场景"),
    )
    labels: list[str] = []
    for field, label in fields:
        value = _identity_value(row.get(field))
        if value is not None:
            labels.append(f"{label} {value}")
    source = _identity_value(row.get("owner_source")) or _identity_value(row.get("system_source"))
    path_id = _identity_value(row.get("owner_path_id")) or _identity_value(row.get("system_path_id"))
    if source is not None or path_id is not None:
        labels.append(f"源对象 {source or '未知源'}#{path_id or '未知 PathId'}")
    pair_index = _identity_value(row.get("source_pair_index"))
    if pair_index is not None:
        labels.append(f"源配对序号 {pair_index}")
    return "；".join(labels)


def _pending_explanation(code: str, row: dict[str, object]) -> str:
    status = row.get("status")
    reason = row.get("reason")
    if status == "unsupported_material" and isinstance(reason, str):
        match = re.search(r"features=\(([^)]*)\)", reason)
        features = re.findall(r"'([^']+)'", match.group(1)) if match is not None else []
        if features:
            return f"特效材质含当前导出器不支持的特性：{'、'.join(features)}"
    for value in (status, reason, code.rsplit(".", 1)[-1]):
        if isinstance(value, str) and value in _PENDING_EXPLANATIONS:
            return _PENDING_EXPLANATIONS[value]
    if status == "static_preview_partial":
        modules = row.get("unsupported_modules")
        if isinstance(modules, list) and modules and all(isinstance(item, str) for item in modules):
            return f"静态 FX 预览未覆盖模块：{'、'.join(cast(list[str], modules))}"
        limitations = row.get("material_limitations")
        if isinstance(limitations, list):
            explanations = [translation for source, translation in _FX_LIMITATION_EXPLANATIONS
                            if any(isinstance(item, str) and item.startswith(source) for item in limitations)]
            if explanations:
                return "静态 FX 预览未完整还原：" + "；".join(explanations[:2])
        return "静态 FX 预览未完整还原"
    return f"该组件尚未完整还原（{code}）"


def _message(code: str, row: dict[str, object], context: str) -> str:
    explanation = _pending_explanation(code, row)
    identity = _issue_identity(row)
    location = f"定位：{identity}；{context}" if identity else f"定位：{context}"
    return f"{explanation}；{location}"


def _add_issue(issues: dict[tuple[str, ...], tuple[InstancedScenePendingItem, bool]],
               key: tuple[str, ...], group_id: ExportGroup, code: str, unsupported: bool,
               message: str) -> None:
    item = InstancedScenePendingItem(group_id=group_id, code=code,
                                     message=message)
    existing = issues.get(key)
    if existing is None:
        issues[key] = (item, unsupported)
        return
    if existing[0]["group_id"] != group_id:
        raise ValueError(f"Duplicate pending identity was assigned to different groups: {code}")
    if unsupported and not existing[1]:
        issues[key] = (existing[0], True)


def _row_id(row: dict[str, object], field: str, context: str) -> str:
    value = row.get(field)
    if type(value) not in (str, int):
        raise ValueError(f"{context}.{field}: expected text or integer identity")
    return str(value)


def _pending_component_group(component: str) -> ExportGroup:
    if component in ("water_geometry", "water_materials", "water_depth", "water"):
        return "water"
    if component in ("projected_decal", "surface_effect", "direct_effect", "effect"):
        return "effects"
    if component in ("light", "lighting"):
        return "lighting"
    if component in ("terrain", "terrain_surface"):
        return "terrain"
    if component in ("building", "prop", "vegetation", "road", "particle", "effects"):
        return cast(ExportGroup, component)
    return "unknown"


def _add_plan_pending(issues: dict[tuple[str, ...], tuple[InstancedScenePendingItem, bool]],
                      source_path: str, row: dict[str, object], context: str) -> None:
    component = _text(row.get("component"), f"{context}.component")
    reason = _text(row.get("reason"), f"{context}.reason")
    entity = _row_id(row, "entity_id", context)
    group_id = _pending_component_group(component)
    family = "decal" if component == "projected_decal" else "water" if component == "water_geometry" else "plan"
    identity = (entity,) if family in ("decal", "water") else (component, entity, reason)
    code = f"plan.{_code_part(component, context)}.{_code_part(reason, context)}"
    _add_issue(issues, _issue_key(source_path, family, identity), group_id, code,
               reason.startswith("unsupported_"), _message(code, row, f"{source_path} · {context}"))


def _add_keyed_pending(issues: dict[tuple[str, ...], tuple[InstancedScenePendingItem, bool]],
                       source_path: str, family: str, row: dict[str, object],
                       group_id: ExportGroup, code_prefix: str, identity_fields: Sequence[str],
                       context: str) -> None:
    component = _text(row.get("component"), f"{context}.component")
    reason = _text(row.get("reason"), f"{context}.reason")
    identity_values: list[object] = []
    for field in identity_fields:
        value = row.get(field)
        if type(value) not in (str, int):
            raise ValueError(f"{context}.{field}: expected text or integer identity")
        identity_values.append(value)
    code = f"{code_prefix}.{_code_part(component, context)}.{_code_part(reason, context)}"
    _add_issue(issues, _issue_key(source_path, family, identity_values), group_id, code,
               reason.startswith("unsupported_"), _message(code, row, f"{source_path} · {context}"))


def _add_source_pending(issues: dict[tuple[str, ...], tuple[InstancedScenePendingItem, bool]],
                        source_path: str, family: str, row: dict[str, object],
                        group_id: ExportGroup, code_prefix: str, identity_fields: Sequence[str],
                        context: str) -> None:
    status = _text(row.get("status"), f"{context}.status")
    identity_values: list[object] = []
    for field in identity_fields:
        value = row.get(field)
        if type(value) not in (str, int):
            value = ""
        else:
            value = str(value)
        identity_values.append(value)
    if all(value == "" for value in identity_values):
        identity_values.append(json.dumps(row, ensure_ascii=False, sort_keys=True, separators=(",", ":")))
    code = f"{code_prefix}.{_code_part(status, context)}"
    _add_issue(issues, _issue_key(source_path, family, identity_values), group_id, code,
               status.startswith("unsupported_"), _message(code, row, f"{source_path} · {context}"))


def _audit_pending_count(plan_rows: dict[str, list[dict[str, object]]],
                         audit_rows: dict[str, list[dict[str, object]]]) -> int:
    outside_water = {
        _row_id(row, "entity_id", "audit.water")
        for row in audit_rows["water"] if row.get("status") == "outside_clipped_mesh"
    }
    active_plan_pending = sum(
        not (row.get("component") == "water_materials"
             and _row_id(row, "entity_id", "plan.pending") in outside_water)
        for row in plan_rows["pending"]
    )
    return (
        active_plan_pending + len(plan_rows["direct_effect_pending"])
        + len(plan_rows["dynamic_geometry_pending"])
        + sum(row.get("status") != "restored" for row in audit_rows["decals"])
        + sum(row.get("status") not in ("restored", "outside_clipped_mesh", "covered_by_terrain")
              for row in audit_rows["water"])
        + sum(row.get("status") not in (
            "static_preview", "renderer_disabled", "game_object_inactive", "no_alive_particles_at_snapshot",
        ) for row in audit_rows["effects"])
        + len(audit_rows["lights"]) + len(audit_rows["material_pending"])
    )


def _collect_pending(source_path: str, plan_rows: dict[str, list[dict[str, object]]],
                     audit_rows: dict[str, list[dict[str, object]]]
                     ) -> list[tuple[InstancedScenePendingItem, bool]]:
    outside_water = {
        _row_id(row, "entity_id", "audit.water")
        for row in audit_rows["water"] if row.get("status") == "outside_clipped_mesh"
    }
    issues: dict[tuple[str, ...], tuple[InstancedScenePendingItem, bool]] = {}
    effect_groups = _effect_group_by_entity(plan_rows)
    direct_effect_sources = {str(row.get("source_key")) for row in plan_rows["direct_effect_renderers"]}

    for index, row in enumerate(audit_rows["decals"]):
        status = _text(row.get("status"), f"audit.decals[{index}].status")
        if status != "restored":
            _add_source_pending(issues, source_path, "decal", row, "effects", "decal",
                                ("entity_id",), f"audit.decals[{index}]")
    for index, row in enumerate(audit_rows["water"]):
        status = _text(row.get("status"), f"audit.water[{index}].status")
        if status not in ("restored", "outside_clipped_mesh", "covered_by_terrain"):
            _add_source_pending(issues, source_path, "water", row, "water", "water",
                                ("entity_id",), f"audit.water[{index}]")
    for index, row in enumerate(audit_rows["effects"]):
        status = _text(row.get("status"), f"audit.effects[{index}].status")
        if status not in ("static_preview", "renderer_disabled", "game_object_inactive",
                          "no_alive_particles_at_snapshot"):
            entity = row.get("entity_id")
            if entity is not None and type(entity) is not int:
                raise ValueError(f"audit.effects[{index}].entity_id: expected an integer")
            group_id = _effect_group_for_row(row, effect_groups, direct_effect_sources)
            _add_source_pending(issues, source_path, "effect", row,
                                group_id, "effect", ("entity_id", "system_source", "system_path_id",
                                    "owner_source", "owner_path_id", "source_key", "source_pair_index",
                                    "material_slot", "submesh", "code", "status"), f"audit.effects[{index}]")
    for index, row in enumerate(audit_rows["lights"]):
        _add_source_pending(issues, source_path, "light", row, "lighting", "lighting",
                            ("map_id", "entity_id"), f"audit.lights[{index}]")
    for index, row in enumerate(audit_rows["material_pending"]):
        component = _text(row.get("component"), f"audit.material_pending[{index}].component")
        status = _text(row.get("status"), f"audit.material_pending[{index}].status")
        material = _text(row.get("material"), f"audit.material_pending[{index}].material")
        code = f"material.{_code_part(component, 'material component')}.{_code_part(status, 'material status')}"
        _add_issue(issues, _issue_key(source_path, "material", (material, component, status)),
                   "unknown", code, status.startswith("unsupported_"),
                   _message(code, row, f"{source_path} · audit.material_pending[{index}]"))

    for index, row in enumerate(audit_rows["direct_effect_pending"]):
        _add_keyed_pending(issues, source_path, "direct_effect", row, "effects", "direct_effect",
                           ("source_key", "source_pair_index", "component", "reason"),
                           f"audit.direct_effect_pending[{index}]")
    for index, row in enumerate(audit_rows["dynamic_geometry_pending"]):
        _add_keyed_pending(issues, source_path, "dynamic_geometry", row, "unknown", "dynamic_geometry",
                           ("source_key", "component", "reason"),
                           f"audit.dynamic_geometry_pending[{index}]")
    for index, row in enumerate(audit_rows["water_depth_context_pending"]):
        component = _text(row.get("component"), f"audit.water_depth_context_pending[{index}].component")
        reason = _text(row.get("reason"), f"audit.water_depth_context_pending[{index}].reason")
        entity = _row_id(row, "entity_id", f"audit.water_depth_context_pending[{index}]")
        code = f"water_depth.{_code_part(reason, 'water depth reason')}"
        _add_issue(issues, _issue_key(source_path, "water_depth", (entity, component, reason)),
                   "water", code, reason.startswith("unsupported_"),
                   _message(code, row, f"{source_path} · audit.water_depth_context_pending[{index}]"))

    for index, row in enumerate(plan_rows["pending"]):
        if (row.get("component") == "water_materials"
                and _row_id(row, "entity_id", f"plan.pending[{index}]") in outside_water):
            continue
        _add_plan_pending(issues, source_path, row, f"plan.pending[{index}]")
    for index, row in enumerate(plan_rows["direct_effect_pending"]):
        _add_keyed_pending(issues, source_path, "direct_effect", row, "effects", "direct_effect",
                           ("source_key", "source_pair_index", "component", "reason"),
                           f"plan.direct_effect_pending[{index}]")
    for index, row in enumerate(plan_rows["dynamic_geometry_pending"]):
        _add_keyed_pending(issues, source_path, "dynamic_geometry", row, "unknown", "dynamic_geometry",
                           ("source_key", "component", "reason"),
                           f"plan.dynamic_geometry_pending[{index}]")
    for index, row in enumerate(plan_rows["water_depth_pending"]):
        component = _text(row.get("component"), f"plan.water_depth_pending[{index}].component")
        reason = _text(row.get("reason"), f"plan.water_depth_pending[{index}].reason")
        entity = _row_id(row, "entity_id", f"plan.water_depth_pending[{index}]")
        code = f"water_depth.{_code_part(reason, 'water depth reason')}"
        _add_issue(issues, _issue_key(source_path, "water_depth", (entity, component, reason)),
                   "water", code, reason.startswith("unsupported_"),
                   _message(code, row, f"{source_path} · plan.water_depth_pending[{index}]"))

    return list(issues.values())


def _add_exported(counts: dict[ExportGroup, int | None], group_id: ExportGroup,
                  increment: int) -> None:
    if counts[group_id] is not None:
        counts[group_id] = cast(int, counts[group_id]) + increment


def _mark_exported_unknown(counts: dict[ExportGroup, int | None], group_id: ExportGroup) -> None:
    counts[group_id] = None


def _effect_group_by_entity(plan_rows: dict[str, list[dict[str, object]]]) -> dict[int, ExportGroup]:
    groups: dict[int, set[ExportGroup]] = {}
    for row in plan_rows["effect_instances"]:
        entity = row.get("entity_id")
        if type(entity) is not int:
            raise ValueError("plan.effect_instances.entity_id: expected an integer")
        kind = row.get("component_kind", "particle")
        if kind not in ("particle", "mesh_renderer", "mixed"):
            raise ValueError("plan.effect_instances.component_kind: unsupported component kind")
        group_id = "particle" if kind == "particle" else "effects" if kind == "mesh_renderer" else "unknown"
        groups.setdefault(entity, set()).add(cast(ExportGroup, group_id))
    return {entity: next(iter(value)) if len(value) == 1 else "unknown" for entity, value in groups.items()}


def _effect_group_for_row(row: dict[str, object], effect_groups: dict[int, ExportGroup],
                          direct_effect_sources: set[str]) -> ExportGroup:
    entity = row.get("entity_id")
    group_id = effect_groups.get(entity, "effects" if str(row.get("source_key")) in direct_effect_sources else "unknown")
    if group_id != "unknown":
        return group_id
    object_name = row.get("object")
    if isinstance(object_name, str):
        if object_name.startswith("EF_FX_MESH_"):
            return "effects"
        if object_name.startswith("EF_FX_"):
            return "particle"
    if type(row.get("system_path_id")) is int:
        return "particle"
    if type(row.get("material_slot")) is int or type(row.get("submesh")) is int:
        return "effects"
    return group_id


def _collect_exported(plan_rows: dict[str, list[dict[str, object]]],
                      audit_rows: dict[str, list[dict[str, object]]]) -> dict[ExportGroup, int | None]:
    counts: dict[ExportGroup, int | None] = {group_id: 0 for group_id in _GROUPS}
    for field in ("instances", "dynamic_geometry_instances"):
        for row in plan_rows[field]:
            category = _text(row.get("category"), f"plan.{field}.category")
            _add_exported(counts, _group(category), 1)
    _add_exported(counts, "terrain", len(plan_rows["terrain_surfaces"]))
    _add_exported(counts, "effects", len(plan_rows["surface_effect_instances"]))

    for index, row in enumerate(audit_rows["decals"]):
        status = _text(row.get("status"), f"audit.decals[{index}].status")
        if status == "restored":
            _add_exported(counts, "effects", 1)
        elif status not in _DECAL_STATUSES:
            _mark_exported_unknown(counts, "effects")
    for index, row in enumerate(audit_rows["water"]):
        status = _text(row.get("status"), f"audit.water[{index}].status")
        if status == "restored":
            _add_exported(counts, "water", 1)
        elif status not in _WATER_STATUSES:
            _mark_exported_unknown(counts, "water")

    effect_groups = _effect_group_by_entity(plan_rows)
    direct_keys = {str(row.get("source_key")) for row in plan_rows["direct_effect_renderers"]}
    for index, row in enumerate(audit_rows["effects"]):
        status = _text(row.get("status"), f"audit.effects[{index}].status")
        entity = row.get("entity_id")
        if entity is not None and type(entity) is not int:
            raise ValueError(f"audit.effects[{index}].entity_id: expected an integer")
        group_id = _effect_group_for_row(row, effect_groups, direct_keys)
        if status not in _EFFECT_SUCCESS_STATUSES | _EFFECT_NONEXPORT_STATUSES:
            _mark_exported_unknown(counts, group_id)
        elif status in _EFFECT_SUCCESS_STATUSES:
            _add_exported(counts, group_id, 1)

    for index, row in enumerate(audit_rows["lights"]):
        status = _text(row.get("status"), f"audit.lights[{index}].status")
        if status == "source_light_preview_partial":
            _add_exported(counts, "lighting", 1)
        elif status not in _LIGHT_STATUSES:
            _mark_exported_unknown(counts, "lighting")
    return counts


def _scene_rows(plan: dict[str, object], audit: dict[str, object],
                context: str) -> tuple[dict[str, list[dict[str, object]]], dict[str, list[dict[str, object]]]]:
    plan_fields = (
        "instances", "dynamic_geometry_instances", "surface_effect_instances", "effect_instances",
        "direct_effect_renderers", "light_instances", "water_geometry", "water_materials",
        "pending", "water_depth_pending",
        "direct_effect_pending", "dynamic_geometry_pending",
    )
    audit_fields = (
        "decals", "water", "effects", "lights", "material_pending", "direct_effect_pending",
        "dynamic_geometry_pending", "water_depth_context_pending",
    )
    plan_rows = {field: _rows(plan, field, f"{context}.plan") for field in plan_fields}
    plan_rows["terrain_surfaces"] = _path_rows(plan, "terrain_surfaces", f"{context}.plan")
    plan_rows["terrain_details"] = _path_rows(plan, "terrain_details", f"{context}.plan")
    audit_rows = {field: _rows(audit, field, f"{context}.audit") for field in audit_fields}
    for audit_field, plan_field in (
        ("direct_effect_pending", "direct_effect_pending"),
        ("dynamic_geometry_pending", "dynamic_geometry_pending"),
        ("water_depth_context_pending", "water_depth_pending"),
    ):
        if audit[audit_field] != plan[plan_field]:
            raise ValueError(f"{context}: audit {audit_field} differs from its source plan")
    return plan_rows, audit_rows


def build_instanced_scene_export_result(output_root: Path,
                                        artifacts: Sequence[SceneArtifact]) -> InstancedSceneExportResult:
    """Create a result DTO from the files and audits returned by the scene builder.

    Pending rows are deduplicated within each artifact by source identity; equal entity IDs
    in separate batches remain separate. Unclassified plan/material issues use `unknown`.
    Component counts describe source components represented in the saved scene, while
    `pending` and `unsupported` count distinct unresolved issue identities.
    """
    root = output_root.expanduser().resolve(strict=True)
    if not root.is_dir():
        raise ValueError(f"Scene output root is not a directory: {root}")
    if not artifacts:
        raise ValueError("Scene export result requires at least one completed artifact")

    files: list[InstancedSceneExportFile] = []
    seen_paths: set[str] = set()
    all_pending: list[InstancedScenePendingItem] = []
    unsupported_groups: list[ExportGroup] = []
    exported_counts: dict[ExportGroup, int | None] = {group_id: 0 for group_id in _GROUPS}

    for index, artifact in enumerate(artifacts):
        context = f"artifacts[{index}]"
        scene_path, scene_relative = _relative_output_file(root, artifact.get("path"), f"{context}.path")
        audit_path, audit_relative = _relative_output_file(root, artifact.get("audit"), f"{context}.audit")
        plan_path, plan_relative = _relative_output_file(root, artifact.get("plan"), f"{context}.plan")
        relative_paths = (scene_relative, audit_relative, plan_relative)
        if len(set(relative_paths)) != 3 or any(value in seen_paths for value in relative_paths):
            raise ValueError(f"{context}: artifact file paths must be unique")
        seen_paths.update(relative_paths)

        expected_bytes = _count(artifact.get("bytes"), f"{context}.bytes")
        expected_digest = _text(artifact.get("sha256"), f"{context}.sha256")
        if re.fullmatch(r"[0-9a-f]{64}", expected_digest) is None:
            raise ValueError(f"{context}.sha256: expected lowercase SHA-256 hex")
        files.extend((
            _file_entry(scene_path, scene_relative, "scene", expected_bytes, expected_digest),
            _file_entry(audit_path, audit_relative, "audit", None, None),
            _file_entry(plan_path, plan_relative, "plan", None, None),
        ))

        plan = _read_document(plan_path, f"{context}.plan")
        audit = _read_document(audit_path, f"{context}.audit")
        if plan.get("format") != "EndfieldPortableScenePlan/1":
            raise ValueError(f"{context}: unsupported scene plan format")
        if audit.get("format") != "EndfieldPortableSceneAudit/1":
            raise ValueError(f"{context}: unsupported scene audit format")
        audit_instances = _count(audit.get("instances"), f"{context}.audit.instances")
        audit_pending = _count(audit.get("pending"), f"{context}.audit.pending")
        if _count(artifact.get("instances"), f"{context}.instances") != audit_instances:
            raise ValueError(f"{context}: SceneArtifact instance count differs from audit")
        if _count(artifact.get("pending"), f"{context}.pending") != audit_pending:
            raise ValueError(f"{context}: SceneArtifact pending count differs from audit")

        plan_rows, audit_rows = _scene_rows(plan, audit, context)
        expected_instances = len(plan_rows["instances"]) + len(plan_rows["dynamic_geometry_instances"])
        if expected_instances != audit_instances:
            raise ValueError(f"{context}: audit instance count differs from plan rows")
        derived_pending = _audit_pending_count(plan_rows, audit_rows)
        if derived_pending != audit_pending:
            raise ValueError(f"{context}: audit pending count differs from plan and audit records")

        scene_issues = _collect_pending(plan_relative, plan_rows, audit_rows)
        all_pending.extend(item for item, _ in scene_issues)
        unsupported_groups.extend(item["group_id"] for item, unsupported in scene_issues if unsupported)

        scene_exported = _collect_exported(plan_rows, audit_rows)
        for group_id in _GROUPS:
            value = scene_exported[group_id]
            if value is None:
                _mark_exported_unknown(exported_counts, group_id)
            else:
                _add_exported(exported_counts, group_id, value)

    pending_by_group: dict[ExportGroup, int] = {group_id: 0 for group_id in _GROUPS}
    unsupported_by_group: dict[ExportGroup, int] = {group_id: 0 for group_id in _GROUPS}
    for item in all_pending:
        pending_by_group[item["group_id"]] += 1
    for group_id in unsupported_groups:
        unsupported_by_group[group_id] += 1
    summary: InstancedSceneComponentSummary = cast(InstancedSceneComponentSummary, {
        group_id: InstancedSceneComponentCounts(
            exported=exported_counts[group_id], pending=pending_by_group[group_id],
            unsupported=unsupported_by_group[group_id],
        ) for group_id in _GROUPS
    })
    status: Literal["completed", "partial"] = "partial" if all_pending else "completed"
    return InstancedSceneExportResult(
        format="InstancedSceneExportResult/1", status=status,
        output_directory=str(root), files=files, component_summary=summary, pending=all_pending,
    )


