"""Connect portable character preparation and the configured private reader runtime."""
from __future__ import annotations

from collections.abc import Callable
from concurrent.futures import Future, ThreadPoolExecutor
from contextlib import contextmanager
from dataclasses import asdict, dataclass
import hashlib
import json
import os
from pathlib import Path
import re
import subprocess
import threading
import time
import tempfile
from typing import Iterator, cast

from endfield_character_cache import CharacterDataCache, source_stamp
from endfield_character_catalog import CharacterCatalog, read_character_catalog_cached
from endfield_character_authoring import (CharacterActionSource, PreparedCharacterAuthoring, read_character_actions,
    prepare_character_authoring, finish_character_authoring)
from endfield_character_animation_families import CharacterAnimationFamilyError, resolve_animation_family_prefixes
from endfield_character_selection import CharacterSelection, parse_character_selection
from endfield_character_npc_catalog import IndependentNpcCatalog, read_independent_npc_catalog, npc_entity_category
from endfield_character_enemy_index import enemy_index_from_catalog, prepare_enemy_model_index, read_enemy_model_index
from endfield_character_npc_animation import resolve_npc_animation_prefixes
from endfield_character_npc_action_adapter import (
    NpcActionCatalog, npc_cpu_reference_document, public_npc_failure, read_npc_action_catalog, read_npc_cpu_action_references,
    read_enemy_animation_catalog, parse_enemy_action_catalog, enemy_animation_selection_document,
)
from endfield_character_npc_prefab_action import run_prefab_npc_action
from endfield_character_replay import (ReplaySelectionBinding,
    read_configured_replay, replay_matches, verify_replay_snapshot)
from endfield_character_authoring_replay import read_configured_expression_replay, verify_authoring_replay_snapshot
from endfield_character_finalik_replay import read_configured_finalik_replay, verify_finalik_snapshot
from endfield_character_sources import (
    CharacterSourceContext, game_source_stamps, prepare_character_sources,
    validate_character_storage, verify_game_source,
)
from endfield_scene_capacity import system_memory_snapshot
from endfield_scene_export import run_scene_process
from endfield_scene_resources import write_index
from webui_character_contract import CharacterRequestError, json_object, required_text, normalize_character_request
from character_runtime.character_material_options import (
    CharacterMaterialOptions, CharacterMaterialOptionsError, parse_character_material_options,
)
from character_runtime.character_appearance_options import (
    CharacterAppearanceOptionsError, parse_character_appearance_options, verify_character_appearance_report,
)


Progress = Callable[[str, float, str], None]
CheckCancelled = Callable[[], None]
_PATH_DEFAULTS = ("repository_src", "prepare_script", "python", "static_reader", "shader_reader",
                  "visibility_reader", "profile", "skin_shader", "ruri_glsl", "alpha_audit", "caster_module_dir")
_VALUE_DEFAULTS = ("unity_version", "head_bone", "face_right", "face_up", "face_forward", "hair_x", "hair_y", "hair_z")
_MEMORY_LOCK = threading.Lock()
_RESERVATIONS: set[str] = set()


@dataclass(frozen=True)
class PreparedAuthoringState:
    prepared: PreparedCharacterAuthoring
    action: CharacterActionSource | None
    expression_snapshot: dict[str, object] | None


class CharacterServiceError(CharacterRequestError):
    """An actionable public setup or source-state error with no private paths."""

    @property
    def safe_message(self) -> str:
        return str(self)


def _no_cancel() -> None:
    return None


def _no_progress(stage: str, fraction: float, message: str) -> None:
    return None


def _digest(value: object) -> str:
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


def _read(path: Path) -> dict[str, object]:
    return json_object(json.loads(path.read_text(encoding="utf-8-sig")), "character data")


def _path(value: object, parent: Path, field: str) -> Path:
    return (parent / Path(required_text(value, field)).expanduser()).resolve()


def _context_row(context: CharacterSourceContext) -> dict[str, object]:
    return {name: str(value) for name, value in asdict(context).items()}


def _context_from_row(row: dict[str, object]) -> CharacterSourceContext:
    return CharacterSourceContext(*(_absolute_source_path(row.get(name), name) for name in CharacterSourceContext.__dataclass_fields__))


def _absolute_source_path(value: object, field: str) -> Path:
    path = Path(required_text(value, field)).expanduser()
    if not path.is_absolute():
        raise CharacterServiceError(f"{field}: 请选择绝对路径。")
    return path.resolve()


def _safe_storage(game: Path, target: Path) -> None:
    try:
        validate_character_storage(game, target)
    except ValueError as error:
        raise CharacterServiceError("角色缓存、输出及运行状态必须使用与游戏目录不重叠的独立文件夹。") from error


def require_character_export_blender(blender: Path) -> None:
    """Probe the selected executable only at preparation and export validation boundaries."""
    if not blender.is_file():
        raise CharacterServiceError(f"Blender 程序不存在：path={blender}")
    try:
        result = subprocess.run([str(blender), "--version"], stdin=subprocess.DEVNULL,
            capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=10,
            creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0, check=False)
    except subprocess.TimeoutExpired as error:
        raise CharacterServiceError(
            f"Blender 版本读取超时：path={blender}; timeout=10s; returncode=timeout; "
            f"stdout={error.stdout!r}; stderr={error.stderr!r}") from error
    except OSError as error:
        raise CharacterServiceError(
            f"Blender 版本读取无法启动：path={blender}; returncode=not_started; error={error}") from error
    evidence = f"path={blender}; returncode={result.returncode}; stdout={result.stdout!r}; stderr={result.stderr!r}"
    match = re.search(r"^Blender (\d+)\.(\d+)(?:\.\d+)?(?:\s|$)", result.stdout, re.MULTILINE)
    if result.returncode != 0 or match is None:
        raise CharacterServiceError(f"无法读取有效 Blender 版本：{evidence}")
    version = (int(match.group(1)), int(match.group(2)))
    if version < (4, 4):
        raise CharacterServiceError(
            f"角色导出需要 Blender 4.4 或更新版本；4.3 可读取成品不代表支持导出。{evidence}")


def _confirm_material_options(expected: CharacterMaterialOptions, value: object,
                              evidence: str) -> CharacterMaterialOptions:
    """Require canonical material settings from a named execution receipt."""
    try:
        actual = parse_character_material_options(value)
    except CharacterMaterialOptionsError as error:
        raise CharacterServiceError(f"Material confirmation is invalid: evidence={evidence}; {error}") from error
    if actual != expected:
        raise CharacterServiceError(
            f"Material confirmation differs: evidence={evidence}; "
            f"requested={expected.canonical_json()}; actual={actual.canonical_json()}")
    return actual


def _verified_character_material_options(options: dict[str, object], receipt: dict[str, object],
                                         build: dict[str, object], audit: dict[str, object]) -> dict[str, object] | None:
    """Confirm explicit settings against the exported file's build and reopen evidence."""
    if "materials" not in options:
        return None
    expected = parse_character_material_options(options["materials"])
    for evidence, row, format_name, status in (
            ("export receipt", receipt, "EndfieldCharacterExport/1", "exported_reopened"),
            ("build receipt", build, "EndfieldCharacterBuild/1", "built_not_reopened"),
            ("reopen audit", audit, "EndfieldCharacterBuildReopenAudit/1", "passed")):
        if row.get("format") != format_name or row.get("status") != status:
            raise CharacterServiceError(f"Material confirmation requires completed evidence: evidence={evidence}")
    confirmed = _confirm_material_options(expected, receipt.get("material_options"), "export receipt")
    options_hash = hashlib.sha256(confirmed.canonical_json().encode("utf-8")).hexdigest()
    if receipt.get("material_options_sha256") != options_hash:
        raise CharacterServiceError("Material confirmation hash differs: evidence=export receipt")
    if (build.get("sha256") != receipt.get("blend_sha256")
            or audit.get("blend_sha256") != receipt.get("blend_sha256")):
        raise CharacterServiceError("Material evidence refers to different character files")
    if confirmed.mode == "original":
        if "material_variant" in build or "material_variant" in audit:
            raise CharacterServiceError("Original materials have unexpected variant evidence")
    else:
        variant = json_object(build.get("material_variant"), "build material variant")
        reopened = json_object(audit.get("material_variant"), "reopened material variant")
        if variant.get("format") != "EndfieldMaterialVariant/1" or reopened.get("status") != "passed":
            raise CharacterServiceError("Simplified material variant did not pass saved-file verification")
        _confirm_material_options(confirmed, variant.get("options"), "build material variant")
        _confirm_material_options(confirmed, reopened.get("options"), "reopened material variant")
    return confirmed.as_json()


def _verified_npc_material_options(options: dict[str, object], receipt: dict[str, object]) -> dict[str, object] | None:
    """Confirm the assembly adapter's fixed original-material scope independently."""
    if "materials" not in options:
        return None
    if (receipt.get("format") != "EndfieldNpcExportReceipt/1"
            or receipt.get("status") != "source_assembled_reopened"):
        raise CharacterServiceError("Original NPC materials require the source assembly reopen receipt")
    original = parse_character_material_options({"mode": "original", "lighting": "full",
        "weather": {"quality": "full", "effects": ["wet", "snow", "rain"]}})
    _confirm_material_options(original, options["materials"], "NPC assembly material scope")
    if "material_options" in receipt:
        _confirm_material_options(original, receipt["material_options"], "NPC assembly receipt")
    return original.as_json()


def _verified_character_appearance(options: dict[str, object], receipt: dict[str, object],
                                   build: dict[str, object], audit: dict[str, object]
                                   ) -> tuple[dict[str, str] | None, dict[str, object] | None]:
    """Expose requested appearance only after the completed export and reopen evidence agree."""
    if "appearance" not in options:
        return None, None
    try:
        expected = parse_character_appearance_options(options["appearance"])
    except CharacterAppearanceOptionsError as error:
        raise CharacterServiceError(f"Character appearance request is invalid: {error}") from error
    if (receipt.get("format") != "EndfieldCharacterExport/1"
            or receipt.get("status") != "exported_reopened"
            or build.get("format") != "EndfieldCharacterBuild/1"
            or build.get("status") != "built_not_reopened"
            or audit.get("format") != "EndfieldCharacterBuildReopenAudit/1"
            or audit.get("status") != "passed"):
        raise CharacterServiceError("Appearance confirmation requires completed export, build, and reopen evidence")
    digest = receipt.get("blend_sha256")
    if not isinstance(digest, str) or re.fullmatch(r"[0-9a-f]{64}", digest) is None:
        raise CharacterServiceError("Appearance confirmation requires the completed character SHA-256")
    if build.get("sha256") != digest or audit.get("blend_sha256") != digest:
        raise CharacterServiceError("Appearance evidence refers to different character files")
    try:
        confirmed = parse_character_appearance_options(receipt.get("appearance_options"))
        build_options = parse_character_appearance_options(build.get("appearance_options"))
        audit_options = parse_character_appearance_options(audit.get("appearance_options"))
        report = verify_character_appearance_report(expected, receipt.get("appearance_report"))
        build_report = verify_character_appearance_report(expected, build.get("appearance_report"))
        audit_report = verify_character_appearance_report(expected, audit.get("appearance_report"))
    except CharacterAppearanceOptionsError as error:
        raise CharacterServiceError(f"Character appearance confirmation is invalid: {error}") from error
    expected_json = expected.as_json()
    if (confirmed.as_json() != expected_json
            or build_options.as_json() != expected_json
            or audit_options.as_json() != expected_json
            or receipt.get("appearance_options") != expected_json
            or build.get("appearance_options") != expected_json
            or audit.get("appearance_options") != expected_json):
        raise CharacterServiceError(
            "Appearance options differ across the request, export receipt, build receipt, and reopen audit")
    if build_report != report or audit_report != report:
        raise CharacterServiceError(
            "Appearance reports differ across the export receipt, build receipt, and reopen audit")
    return confirmed.as_json(), report


def character_source_pending(probe: dict[str, object], audit: dict[str, object]) -> list[dict[str, str]]:
    """Expose reader coverage separately from successful source material rebuilding."""
    if probe.get("format") != "EndfieldCharacterStaticReceipt/1" or audit.get("format") != "EndfieldCharacterSourceAudit/1":
        raise ValueError("Character reader coverage requires current source receipt formats")
    counts: dict[str, int] = {}
    for field in ("source_materials", "converted_materials", "source_textures", "converted_textures"):
        value = probe.get(field)
        if type(value) is not int or value < 0:
            raise ValueError(f"Character reader coverage requires nonnegative integer {field}")
        counts[field] = value
    pending: list[dict[str, str]] = []
    if probe.get("complete") is not True:
        pending.append({"code": "source_converter_coverage", "message":
            f"通用 FBX 读取器材料覆盖 {counts['converted_materials']}/{counts['source_materials']}，"
            f"贴图覆盖 {counts['converted_textures']}/{counts['source_textures']}；"
            "最终文件使用保留的源材料另行重建并验收，此覆盖差异仍明确记录。"})
    renderers = audit.get("renderers")
    if not isinstance(renderers, list):
        raise ValueError("Character source audit requires renderer records")
    unresolved = 0
    for value in renderers:
        row = json_object(value, "source renderer")
        fields = tuple(row.get(field) for field in ("source_bone_reference_count", "resolved_source_bone_count",
                                                   "positive_unresolved_bone_influences"))
        if any(type(count) is not int or count < 0 for count in fields):
            raise ValueError("Character renderer requires nonnegative bone resolution counts")
        source, resolved, weighted = cast(tuple[int, int, int], fields)
        if resolved > source:
            raise ValueError("Character renderer resolved bone count exceeds its source references")
        if weighted:
            raise CharacterServiceError("角色源包含未解析的正权重骨引用；当前文件不能作为完整蒙皮结果交付。")
        unresolved += source - resolved
    if unresolved:
        pending.append({"code": "unweighted_bone_references", "message":
                        f"源渲染器共有 {unresolved} 条未解析骨引用；已核实没有正权重指向这些引用。"})
    return pending


@contextmanager
def _character_memory(request_id: str, check_cancelled: CheckCancelled) -> Iterator[None]:
    """Admit at most two readers while reserving physical and commit headroom."""
    while True:
        check_cancelled()
        with _MEMORY_LOCK:
            memory = system_memory_snapshot()
            required = (len(_RESERVATIONS) + 1) * 3 * 1024**3 + 1024**3
            if len(_RESERVATIONS) < 2 and min(memory.available_physical_bytes, memory.available_commit_bytes) >= required:
                _RESERVATIONS.add(request_id)
                break
        time.sleep(.2)
    try:
        yield
    finally:
        with _MEMORY_LOCK:
            _RESERVATIONS.remove(request_id)


class CharacterService:
    """Own external tool configuration, source readiness and reader execution."""

    def __init__(self, config_path: Path | None) -> None:
        self.config_path = config_path.resolve() if config_path is not None else None
        self._prepare_lock = threading.Lock()
        self._enemy_index_lock = threading.Lock()
        self._npc_action_bindings: dict[tuple[str, str], str] = {}
        self.config = _read(self.config_path) if self.config_path is not None else None
        if self.config is not None and self.config.get("format") not in {"EndfieldCharacterRuntime/1", "EndfieldCharacterRuntime/2"}:
            raise CharacterServiceError("角色运行配置版本不受支持，请更新角色工具配置。")
        if self.config is not None and self.config.get("format") == "EndfieldCharacterRuntime/2":
            from endfield_character_runtime import read_character_tools
            if self.config_path is None:
                raise CharacterServiceError("角色运行配置文件缺失。")
            read_character_tools(_path(self.config.get("tools_receipt"), self.config_path.parent, "tools_receipt"))

    def _require_config(self) -> tuple[dict[str, object], Path]:
        if self.config is None or self.config_path is None:
            raise CharacterServiceError("尚未配置角色读取工具；请使用 --character-config 指定角色运行配置。")
        return self.config, self.config_path.parent

    def _state_path(self) -> Path:
        config, parent = self._require_config()
        return _path(config.get("state_root"), parent, "state_root") / "character_source.json"

    def _current(self) -> tuple[CharacterSourceContext, dict[str, object]]:
        path = self._state_path()
        if not path.is_file():
            raise CharacterServiceError("请先完成角色独立准备；不需要先提取武陵或谷地。")
        record = _read(path)
        if record.get("format") != "EndfieldCharacterSourceContext/1":
            raise CharacterServiceError("角色准备记录版本无效，请重新进行角色准备。")
        context = _context_from_row(json_object(record.get("context"), "context"))
        for target in (context.cache_root, context.output_root, path.parent):
            _safe_storage(context.game_root, target)
        if game_source_stamps(context.game_root) != record.get("game_stamps"):
            raise CharacterServiceError("游戏文件已更新，请重新准备角色数据。")
        for name in ("provider_index", "resource_database", "cab_inventory"):
            if asdict(source_stamp(getattr(context, name))) != json_object(record["index_stamps"], "index_stamps").get(name):
                raise CharacterServiceError("角色资源索引已变化，请重新准备角色数据。")
        if self.config is not None and self.config.get("format") == "EndfieldCharacterRuntime/2":
            weather = record.get("weather_manifest")
            if not isinstance(weather, dict) or not isinstance(weather.get("path"), str):
                raise CharacterServiceError("请重新准备角色数据以生成当前游戏的天气贴图。")
            manifest = Path(weather["path"])
            if not manifest.is_file() or asdict(source_stamp(manifest)) != weather.get("stamp"):
                raise CharacterServiceError("角色天气纹理清单已变化，请重新准备角色数据。")
        return context, record

    def _runtime_defaults(self, options: dict[str, object],
                          context: CharacterSourceContext | None) -> tuple[Path, dict[str, object]]:
        config, parent = self._require_config()
        exporter = _path(config.get("exporter"), parent, "exporter")
        raw = json_object(config.get("export_defaults"), "export_defaults")
        path_fields = _PATH_DEFAULTS[:6] if config.get("format") == "EndfieldCharacterRuntime/2" else _PATH_DEFAULTS
        defaults: dict[str, object] = {name: str(_path(raw.get(name), parent, name)) for name in path_fields}
        for name in _VALUE_DEFAULTS:
            if name not in raw:
                raise CharacterServiceError(f"角色运行配置缺少参数：{name}")
            defaults[name] = raw[name]
        if raw.get("fur_curves_settings") is not None:
            settings = _path(raw["fur_curves_settings"], parent, "fur_curves_settings")
            if not settings.is_file():
                raise CharacterServiceError("可选毛发转换配置不存在，请检查 fur_curves_settings。")
            defaults["fur_curves_settings"] = str(settings)
        missing = [name for name in path_fields if not Path(str(defaults[name])).exists()]
        if not exporter.is_file() or missing:
            raise CharacterServiceError("角色工具或材质组件未安装完整，请检查角色运行配置。")
        if "appearance" in options:
            try:
                parse_character_appearance_options(options["appearance"])
            except CharacterAppearanceOptionsError as error:
                raise CharacterServiceError(f"Character appearance request is invalid: {error}") from error
            if config.get("format") != "EndfieldCharacterRuntime/2":
                raise CharacterServiceError("外观选项需要支持 ExportRequest/2 的角色运行配置，请更新角色工具配置。")
            if context is None or self.config_path is None:
                raise CharacterServiceError("外观选项需要已准备的角色源及运行配置。")

            if "potential_json_reader" in raw:
                json_reader = _path(raw.get("potential_json_reader"), parent, "potential_json_reader")
            else:
                if config.get("npc_assembly_tools") is None:
                    raise CharacterServiceError(
                        "角色运行配置缺少满潜 JSON reader，请更新运行配置或配置 npc_assembly_tools.json_reader。")
                assembly = json_object(config.get("npc_assembly_tools"), "npc_assembly_tools")
                json_reader = _path(assembly.get("json_reader"), parent, "npc_assembly_tools.json_reader")
            if not json_reader.is_file():
                raise CharacterServiceError("满潜源配置读取器不存在，请检查 potential_json_reader 或 npc_assembly_tools.json_reader。")

            if "potential_scene_probe" in raw:
                scene_probe = _path(raw.get("potential_scene_probe"), parent, "potential_scene_probe")
            else:
                try:
                    from endfield_scene_helpers import require_prepared_scene_helpers
                    scene_probe = require_prepared_scene_helpers(self.config_path, context.cache_root).probe
                except (OSError, ValueError, KeyError, TypeError) as error:
                    raise CharacterServiceError(
                        f"无法从已核验的场景工具回执解析 potential_scene_probe：{error}") from error
            if not scene_probe.is_file():
                raise CharacterServiceError("满潜场景探针不存在，请检查 potential_scene_probe 或已准备的场景工具回执。")
            defaults["potential_json_reader"] = str(json_reader)
            defaults["potential_scene_probe"] = str(scene_probe)
        return exporter, defaults

    def prepared_source_context(self) -> CharacterSourceContext:
        """Expose verified shared indexes to image-only consumers without model export."""
        context, _ = self._current()
        return context

    def _catalog_source(self) -> tuple[CharacterSourceContext, CharacterCatalog, IndependentNpcCatalog, str]:
        context, record = self._current()
        cache = CharacterDataCache(context.cache_root)
        catalog = read_character_catalog_cached(context.game_root, context.provider_index, cache, _no_cancel)
        enemy_index = self._enemy_model_index(context, _no_cancel, _no_progress)
        npc = read_independent_npc_catalog(context.game_root, context.provider_index, cache, enemy_index, _no_cancel)
        verify_game_source(context.game_root, cast(list[dict[str, object]], record["game_stamps"]), _no_cancel)
        for name in ("provider_index", "resource_database", "cab_inventory"):
            if asdict(source_stamp(getattr(context, name))) != json_object(record["index_stamps"], "index_stamps").get(name):
                raise CharacterServiceError("角色资源索引已变化，请重新准备角色数据。")
        return context, catalog, npc, _digest((catalog.source_identity, npc.source_identity, record["index_stamps"]))

    def _variants(self, catalog: CharacterCatalog) -> dict[str, tuple[str, CharacterSelection]]:
        variants: dict[str, tuple[str, CharacterSelection]] = {}
        for binding in catalog.model_bindings:
            for template in binding.templates:
                for part in template.parts:
                    for variant in part.variants:
                        for provider in variant.providers:
                            selection = CharacterSelection(catalog.source_identity, binding.character_id, template.template_id,
                                                           part.part_name, variant.variant, variant.resource_path, provider.logical_name)
                            identity = _digest(asdict(selection))[:32]
                            variants[identity] = ("playable-" + binding.character_id, selection)
        return variants

    def _npc_variants(self, catalog: IndependentNpcCatalog) -> dict[str, tuple[str, CharacterSelection]]:
        variants: dict[str, tuple[str, CharacterSelection]] = {}
        for entry in catalog.entries:
            if entry.source_kind == "config_assembled":
                reference = entry.resource_reference
                mesh, templet = reference.avatar_mesh, reference.avatar_templet
                if (mesh is not None and templet is not None and mesh.resource_path is not None
                        and len(mesh.providers) == len(templet.providers) == 1
                        and len(mesh.providers[0].serialized_cabs) == len(templet.providers[0].serialized_cabs) == 1
                        and reference.prefab_parts):
                    first_part = reference.prefab_parts[0]
                    selection = CharacterSelection(catalog.source_identity, entry.npc_id,
                        reference.template_id, first_part.part_name, "npc", mesh.resource_path, mesh.providers[0].logical_name)
                    group_identity = _digest({"contract": "npc-prefabinfo-slot-group/v1",
                                              "selection": asdict(selection),
                                              "part_names": [part.part_name for part in reference.prefab_parts]})[:32]
                    variants[group_identity] = ("independent-npc-" + entry.npc_id, selection)
                continue
            if entry.source_kind != "prefab":
                continue
            for part in entry.resource_reference.prefab_parts:
                for provider in part.providers:
                    if len(provider.serialized_cabs) != 1:
                        continue
                    selection = CharacterSelection(catalog.source_identity, entry.npc_id,
                        entry.resource_reference.template_id, part.part_name, "npc", part.resource_path, provider.logical_name)
                    variants[_digest(asdict(selection))[:32]] = ("independent-npc-" + entry.npc_id, selection)
        return variants

    def _authoring_tools(self) -> dict[str, Path]:
        config, parent = self._require_config()
        raw = json_object(config.get("authoring_tools", {}), "authoring_tools")
        tools: dict[str, Path] = {}
        for name in ("expression_exporter", "animation_exporter", "animation_reader", "dotnet", "authoring_builder",
                     "controller_resolver", "controller_json_reader", "controller_audit", "auxiliary_sampler", "expression_reader"):
            if name in raw:
                path = _path(raw[name], parent, "authoring_tools." + name)
                if not path.is_file():
                    raise CharacterServiceError(f"角色可选工具未安装完整，请检查 authoring_tools.{name}。")
                tools[name] = path
        return tools

    def _ordinary_replay(self) -> dict[str, object] | None:
        config, parent = self._require_config()
        configured = config.get("ordinary_replay_config")
        if configured is None:
            return None
        path = _path(configured, parent, "ordinary_replay_config")
        if not path.is_file():
            raise CharacterServiceError("普通层回放配置不存在，请检查 ordinary_replay_config。")
        try:
            snapshot = read_configured_replay(path)
            verify_replay_snapshot(snapshot)
        except CharacterRequestError as error:
            raise CharacterServiceError("普通层回放配置字段无效：" + str(error)) from error
        except (OSError, json.JSONDecodeError) as error:
            raise CharacterServiceError("普通层回放配置或场景验证失败，请核对配置身份及固定 SHA256。") from error
        if "auxiliary_sampler" not in self._authoring_tools():
            raise CharacterServiceError("普通层回放需要配置 authoring_tools.auxiliary_sampler。")
        return snapshot

    def _selected_ordinary_replay(self, request: dict[str, object], selection: CharacterSelection,
                                 action_resource_path: str, action_logical_provider: str) -> dict[str, object]:
        animation = json_object(json_object(request["options"], "options")["animation"], "animation options")
        snapshot = self._ordinary_replay()
        binding = ReplaySelectionBinding(str(request["source_identity"]), str(request["selection_id"]),
            str(request["variant_id"]), action_resource_path, action_logical_provider)
        if (snapshot is None or snapshot["replay_id"] != animation["replay_id"]
                or not replay_matches(snapshot, binding, selection.character_id)):
            raise CharacterServiceError("所选回放 ID 不属于当前来源、角色、变体和动作，或配置已变化；请刷新动作目录。")
        return snapshot

    def _native_expression_replay(self) -> dict[str, object] | None:
        config, parent = self._require_config()
        configured = config.get("native_expression_replay_config")
        if configured is None:
            return None
        try:
            snapshot = read_configured_expression_replay(_path(configured, parent, "native_expression_replay_config"))
            verify_authoring_replay_snapshot(snapshot)
        except CharacterRequestError as error:
            raise CharacterServiceError("表情回放配置无效：" + str(error)) from error
        except (OSError, json.JSONDecodeError) as error:
            raise CharacterServiceError("表情回放固定来源验证失败，请检查配置文件和 SHA256。") from error
        return snapshot

    def _selected_expression_replay(self, request: dict[str, object], selection: CharacterSelection,
                                    action_resource_path: str, action_logical_provider: str) -> dict[str, object]:
        snapshot = self._native_expression_replay()
        binding = ReplaySelectionBinding(str(request["source_identity"]), str(request["selection_id"]),
            str(request["variant_id"]), action_resource_path, action_logical_provider)
        options = json_object(request["options"], "options")
        if (snapshot is None or snapshot["replay_id"] != options["native_expression_replay_id"]
                or not replay_matches(snapshot, binding, selection.character_id)):
            raise CharacterServiceError("所选表情回放 ID 不属于当前来源、角色、变体和动作，或配置已变化；请刷新动作目录。")
        return snapshot

    def _finalik_replay(self) -> dict[str, object] | None:
        config, parent = self._require_config()
        if config.get("finalik_replay_config") is None:
            return None
        try:
            snapshot = read_configured_finalik_replay(_path(config["finalik_replay_config"], parent, "finalik_replay_config"))
            verify_finalik_snapshot(snapshot)
        except CharacterRequestError as error:
            raise CharacterServiceError("FinalIK 回放配置无效：" + str(error)) from error
        except (OSError, json.JSONDecodeError) as error:
            raise CharacterServiceError("FinalIK 固定来源验证失败，请检查配置文件和 SHA256。") from error
        return snapshot

    def _selected_finalik_replay(self, request: dict[str, object], selection: CharacterSelection,
                                action_resource_path: str, action_logical_provider: str) -> dict[str, object]:
        snapshot = self._finalik_replay()
        binding = ReplaySelectionBinding(str(request["source_identity"]), str(request["selection_id"]),
            str(request["variant_id"]), action_resource_path, action_logical_provider)
        animation = json_object(json_object(request["options"], "options")["animation"], "animation options")
        if (snapshot is None or snapshot["replay_id"] != animation["finalik_replay_id"]
                or not replay_matches(snapshot, binding, selection.character_id)):
            raise CharacterServiceError("所选 FinalIK 回放 ID 不属于当前来源、角色、变体和动作，或配置已变化；请刷新动作目录。")
        return snapshot

    def _family_override(self, character_id: str) -> tuple[str, ...] | None:
        config, _ = self._require_config()
        raw = json_object(config.get("animation_families", {}), "animation_families").get(character_id)
        if raw is None:
            return None
        if not isinstance(raw, list) or any(not isinstance(value, str) or not value.startswith("assets/")
                                           or not value.endswith("/animations/") for value in raw):
            raise CharacterServiceError("animation_families 必须为明确的角色动作资源目录列表。")
        return tuple(cast(list[str], raw))

    def _families(self, context: CharacterSourceContext, selection: CharacterSelection) -> tuple[str, ...]:
        override = self._family_override(selection.character_id)
        if override is not None:
            return override
        npc = read_independent_npc_catalog(context.game_root, context.provider_index,
                                           CharacterDataCache(context.cache_root), self._enemy_model_index(context, _no_cancel, _no_progress), _no_cancel)
        matches = tuple(row for row in npc.entries if row.npc_id == selection.character_id
                        and row.resource_reference.template_id == selection.template_id)
        if len(matches) == 1:
            return resolve_npc_animation_prefixes(context, matches[0], (), _no_cancel)
        try:
            return resolve_animation_family_prefixes(context, selection.character_id, selection.resource_path, _no_cancel)
        except CharacterAnimationFamilyError as error:
            raise CharacterServiceError("所选预制体尚无明确的动作资源族，请使用支持的变体或配置经过核实的 animation_families。") from error

    def _npc_prepare_script(self) -> Path | None:
        config, parent = self._require_config()
        if config.get("npc_prepare_script") is None:
            return None
        path = _path(config["npc_prepare_script"], parent, "npc_prepare_script")
        if not path.is_file():
            raise CharacterServiceError("独立 NPC 准备工具未安装，请检查 npc_prepare_script。")
        return path

    def _enemy_model_index(self, context: CharacterSourceContext, check_cancelled: Callable[[], None],
                           progress: Callable[[str, float, str], None]) -> Path | None:
        """Use the configured source reader even when optional export tools are absent."""
        config, parent = self._require_config()
        section = config.get("npc_assembly_tools")
        if section is None:
            return None
        raw = json_object(section, "npc_assembly_tools")
        reader = _path(raw.get("json_reader"), parent, "npc_assembly_tools.json_reader")
        defaults = json_object(config.get("export_defaults"), "export_defaults")
        version = defaults.get("unity_version")
        if not isinstance(version, str):
            raise CharacterServiceError("敌人资源索引需要配置 unity_version。")
        while not self._enemy_index_lock.acquire(timeout=.2):
            check_cancelled()
        try:
            return prepare_enemy_model_index(context, reader, version, check_cancelled, progress)
        finally:
            self._enemy_index_lock.release()

    def _npc_assembly_tools(self) -> dict[str, Path]:
        config, parent = self._require_config()
        if config.get("npc_assembly_tools") is None:
            return {}
        raw = json_object(config["npc_assembly_tools"], "npc_assembly_tools")
        tools: dict[str, Path] = {}
        for name in ("exporter", "json_reader", "assembly_probe", "shader_probe", "fbx_auditor",
                     "material_builder", "material_module_dir", "repository_src"):
            value = raw.get(name)
            if not isinstance(value, str) or not value.strip():
                raise CharacterServiceError(f"NPC 配置装配工具尚未配置，请检查 npc_assembly_tools.{name}。")
            path = _path(raw.get(name), parent, "npc_assembly_tools." + name)
            directory = name in ("material_module_dir", "repository_src")
            if not (path.is_dir() if directory else path.is_file()):
                raise CharacterServiceError(f"NPC 配置装配工具未安装完整，请检查 npc_assembly_tools.{name}。")
            tools[name] = path
        return tools

    def _npc_animation_tools(self) -> dict[str, Path]:
        config, parent = self._require_config()
        if config.get("npc_animation_tools") is None:
            return {}
        raw = json_object(config["npc_animation_tools"], "npc_animation_tools")
        tools: dict[str, Path] = {}
        for name in ("catalog_inspector", "exporter", "reader", "action_builder", "dotnet"):
            value = raw.get(name)
            if not isinstance(value, str) or not value.strip():
                raise CharacterServiceError(f"NPC 配置动作工具尚未配置，请检查 npc_animation_tools.{name}。")
            path = _path(raw.get(name), parent, "npc_animation_tools." + name)
            if not path.is_file():
                raise CharacterServiceError(f"NPC 动作工具未安装完整，请检查 npc_animation_tools.{name}。")
            tools[name] = path
        assembly = self._npc_assembly_tools()
        if not assembly:
            raise CharacterServiceError("NPC 动作需要同时配置 npc_assembly_tools。")
        animation_exporter = self._authoring_tools().get("animation_exporter")
        if animation_exporter is None:
            raise CharacterServiceError("NPC 动作需要配置 authoring_tools.animation_exporter。")
        return {**tools, "json_reader": assembly["json_reader"], "animation_exporter": animation_exporter}

    def _npc_actions(self, context: CharacterSourceContext, selection: CharacterSelection,
                     identity: str, variant_id: str, check_cancelled: CheckCancelled) -> NpcActionCatalog:
        tools = self._npc_animation_tools()
        if not tools:
            raise CharacterServiceError("NPC 配置动作需要配置 npc_animation_tools；不会按动作族猜测 Avatar。")
        _, defaults = self._runtime_defaults({}, None)
        try:
            catalog = read_npc_action_catalog(context, selection, tools, defaults, check_cancelled)
        except (ValueError, OSError, RuntimeError) as error:
            check_cancelled()
            raise CharacterServiceError("NPC 精确 CPU/Avatar 动作读取失败：" + public_npc_failure(error)) from error
        self._npc_action_bindings[(identity, variant_id)] = catalog.status
        return catalog

    def _npc_prefab_animation_tools(self) -> dict[str, Path]:
        config, parent = self._require_config()
        raw = config.get("npc_animation_tools")
        if raw is None:
            raise CharacterServiceError("NPC 精确预制体动作需要配置 npc_animation_tools。")
        values = json_object(raw, "npc_animation_tools")
        for name in ("prefab_action_builder", "catalog_inspector", "exporter", "reader", "dotnet"):
            value = values.get(name)
            if not isinstance(value, str) or not value.strip():
                raise CharacterServiceError(f"NPC 精确预制体动作工具尚未配置：npc_animation_tools.{name}。")
        builder = _path(values.get("prefab_action_builder"), parent, "npc_animation_tools.prefab_action_builder")
        if not builder.is_file():
            raise CharacterServiceError("NPC 精确预制体动作 builder 不存在，请检查 npc_animation_tools.prefab_action_builder。")
        tools = {name: _path(values.get(name), parent, "npc_animation_tools." + name)
                 for name in ("catalog_inspector", "exporter", "reader", "dotnet")}
        for name, path in tools.items():
            if not path.is_file():
                raise CharacterServiceError(f"NPC 精确预制体动作工具不存在：npc_animation_tools.{name}。")
        readers: dict[str, Path] = {}
        for section, field in (("npc_assembly_tools", "json_reader"), ("authoring_tools", "animation_exporter")):
            values = json_object(config.get(section, {}), section)
            value = values.get(field)
            if not isinstance(value, str) or not value.strip():
                raise CharacterServiceError(f"NPC 精确预制体动作需要配置 {section}.{field}。")
            reader = _path(value, parent, section + "." + field)
            if not reader.is_file():
                raise CharacterServiceError(f"NPC 精确预制体动作工具不存在：{section}.{field}。")
            readers[field] = reader
        return {**tools, **readers,
                "prefab_action_builder": builder}

    def _enemy_actions(self, context: CharacterSourceContext, selection: CharacterSelection,
                       identity: str, variant_id: str, check_cancelled: CheckCancelled) -> dict[str, object]:
        """Authenticate controller-referenced clips for one exact enemy prefab."""
        tools = self._npc_prefab_animation_tools()
        _, defaults = self._runtime_defaults({}, None)
        try:
            document = read_enemy_animation_catalog(context, selection, tools, defaults, check_cancelled)
            source = parse_enemy_action_catalog(document, selection)
        except (ValueError, OSError, RuntimeError) as error:
            check_cancelled()
            raise CharacterServiceError("敌人精确控制器动作读取失败：" + public_npc_failure(error)) from error
        self._npc_action_bindings[(identity, variant_id)] = source.status if source.actions else "source_enemy_controller_only_unsupported_references"
        return document

    def _npc_cpu_reference_runtime(self) -> tuple[dict[str, Path], dict[str, object]] | None:
        config, parent = self._require_config()
        if config.get("npc_animation_tools") is None or config.get("npc_assembly_tools") is None:
            return None
        animation = json_object(config["npc_animation_tools"], "npc_animation_tools")
        assembly = json_object(config["npc_assembly_tools"], "npc_assembly_tools")
        tools = {
            "catalog_inspector": _path(animation.get("catalog_inspector"), parent, "npc_animation_tools.catalog_inspector"),
            "json_reader": _path(assembly.get("json_reader"), parent, "npc_assembly_tools.json_reader"),
        }
        for name, path in tools.items():
            if not path.is_file():
                raise CharacterServiceError(f"NPC CPU reference reader is missing: tool={name}.")
        defaults = json_object(config.get("export_defaults"), "export_defaults")
        python = _path(defaults.get("python"), parent, "export_defaults.python")
        repository_src = _path(defaults.get("repository_src"), parent, "export_defaults.repository_src")
        unity_version = required_text(defaults.get("unity_version"), "export_defaults.unity_version")
        if not python.is_file() or not repository_src.is_dir():
            raise CharacterServiceError("NPC CPU reference reader runtime is incomplete: check export_defaults.python and repository_src.")
        return tools, {"python": str(python), "repository_src": str(repository_src), "unity_version": unity_version}

    def _npc_cpu_action_references(self, context: CharacterSourceContext, selection: CharacterSelection,
                                   check_cancelled: CheckCancelled) -> dict[str, object]:
        runtime = self._npc_cpu_reference_runtime()
        if runtime is None:
            return {"format": "EndfieldNpcCpuActionReferences/1", "status": "not_configured",
                "execution_available": False, "actions": [],
                "unavailable_reason": "CPU 来源引用读取工具尚未配置；当前动作候选列表保持原路径。"}
        tools, defaults = runtime
        try:
            catalog = read_npc_cpu_action_references(context, selection, tools, defaults, check_cancelled)
        except (ValueError, OSError, RuntimeError) as error:
            check_cancelled()
            raise CharacterServiceError("NPC 精确 CPU 来源引用读取失败：" + public_npc_failure(error)) from error
        try:
            self._npc_prefab_animation_tools()
            request_available = True
            request_reason = None
        except CharacterServiceError as error:
            request_available = False
            request_reason = str(error)
        return {"format": "EndfieldNpcCpuActionReferences/1", "status": catalog.status,
            "source_identity": selection.catalog_identity, "selection_id": "independent-npc-" + selection.character_id,
            "template_id": selection.template_id, "part_name": selection.part_name,
            "cpu_animation_template": catalog.cpu_animation_template,
            "template_namespace": catalog.template_namespace,
            "cpu_source": {"cab": catalog.cpu_source_cab, "path_id": catalog.cpu_source_path_id,
                "sha256": catalog.cpu_source_sha256},
            "avatar": {"cab": catalog.avatar_cab, "path_id": catalog.avatar_path_id,
                "binding_status": catalog.avatar_binding_status},
            "execution_available": False,
            "request_available": request_available, "request_unavailable_reason": request_reason,
            "actions": [{**asdict(action), "execution_available": False, "request_available": request_available}
                        for action in catalog.actions],
            "limitations": ["精确 CPU 指针和唯一 CAB 提供者已核对；使用独立 reference_id 提交，不能替代 generic action_id。",
                "目录不证明每个动作已通过完整轨道和材质验收；选定导出会解码、校准绑骨并逐帧保存重开，运行时 Animator 仍未还原。"]}

    def catalog(self) -> dict[str, object]:
        try:
            context, catalog, npc, identity = self._catalog_source()
        except CharacterServiceError as error:
            return {"format": "CharacterCatalog/1", "catalog_id": "unavailable", "source_identity": None,
                    "availability": {"ready": False, "reason": str(error)}, "characters": [], "profiles": []}
        runtime_reason: str | None = None
        try:
            self._runtime_defaults({}, None)
        except CharacterServiceError as error:
            runtime_reason = str(error)
        variants = self._variants(catalog)
        npc_variants = self._npc_variants(npc)
        tools: dict[str, Path] = {}
        optional_reason: str | None = None
        try:
            tools = self._authoring_tools()
            npc_script = self._npc_prepare_script()
        except CharacterServiceError as error:
            optional_reason = str(error)
            npc_script = None
        animation_ready = all(name in tools for name in ("animation_exporter", "animation_reader", "dotnet", "authoring_builder"))
        expression_ready = "expression_exporter" in tools
        basic_ready = expression_ready and "authoring_builder" in tools
        assembly_reason: str | None = None
        try:
            assembly_tools = self._npc_assembly_tools()
        except CharacterServiceError as error:
            assembly_reason = str(error)
            assembly_tools = {}
        try:
            npc_animation_tools = self._npc_animation_tools()
        except CharacterServiceError as error:
            optional_reason = str(error)
            npc_animation_tools = {}
        rows: list[dict[str, object]] = []
        for character in catalog.characters:
            selection_id = "playable-" + character.character_id
            choices = [{"variant_id": key, "label": selection.part_name, "variant": selection.variant,
                        "geometry_status": "not_verified"} for key, (entry, selection) in variants.items() if entry == selection_id]
            ready = bool(choices) and runtime_reason is None
            rows.append({"selection_id": selection_id, "character_id": character.character_id, "name": character.name,
                         "english_name": character.english_name, "entry_kind": "playable", "variants": choices,
                         "asset_category": "playable",
                         "capabilities": {"bundle_prepare": bool(choices), "blend_export": ready,
                                          "animation": ready and animation_ready, "raw_expressions": ready and expression_ready,
                                          "basic_mmd": ready and basic_ready},
                         "optional_tools_reason": optional_reason,
                         "unavailable_reason": runtime_reason if choices else "尚未找到可用的模型资源绑定"})
        reasons = {
            "prefab": "已定位独立 NPC 预制体；当前角色导出器尚未接入此类独立装配入口。",
            "config_assembled": "此独立 NPC 由配置装配；请配置 npc_assembly_tools 后导出静态 LOD0。",
            "unresolved": "此独立 NPC 的资源来源尚未解析，暂不能导出。",
        }
        for entry in npc.entries:
            reference = entry.resource_reference
            category = npc_entity_category(entry)
            provider_found = (any(part.provider_found for part in reference.prefab_parts)
                              or any(value is not None and value.provider_found for value in
                                     (reference.avatar_mesh, reference.avatar_templet)))
            name_source = entry.name_source
            selection_id = "independent-npc-" + entry.npc_id
            configured = entry.source_kind == "config_assembled"
            choices = [{"variant_id": key, "label": (f"PrefabInfo 全组（{len(reference.prefab_parts)} 部件）" if configured else selection.part_name),
                        "variant": "npc", "geometry_status": "not_verified",
                        **({"part_names": [part.part_name for part in reference.prefab_parts]} if configured else {}),
                        "animation_binding_status": self._npc_action_bindings.get((identity, key), "not_inspected")}
                       for key, (owner, selection) in npc_variants.items() if owner == selection_id]
            source_ready = bool(assembly_tools) if configured else npc_script is not None
            ready = bool(choices) and runtime_reason is None and source_ready
            rows.append({"selection_id": "independent-npc-" + entry.npc_id, "npc_id": entry.npc_id,
                         "name": entry.name, "english_name": None, "entry_kind": "independent_npc",
                         "asset_category": category.category, "category_source": asdict(category),
                         "template_id": reference.template_id,
                         "name_status": entry.name_status, "source_kind": entry.source_kind,
                         "name_source": ({key: getattr(name_source, key) for key in ("table", "row_key", "field", "text_id")}
                                         if name_source is not None else None),
                         "variants": choices, "capabilities": {"bundle_prepare": bool(choices) and source_ready,
                             "blend_export": ready, "animation": (ready and bool(npc_animation_tools)
                                 and any(self._npc_action_bindings.get((identity, str(choice["variant_id"])))
                                         in ("exact_avatar_base_clip_references", "source_humanoid_target_avatar_base_clip_references", "source_generic_equivalent_avatar_base_clip_references", "source_enemy_controller_clip_references") for choice in choices)) if configured or category.source_field == "enemyModelPrefab" else
                                 ready and animation_ready and bool(self._family_override(entry.npc_id)
                                     or reference.cpu_animation_template and reference.cpu_animation_template.casefold().startswith("npc/animationconfig/humanoid/")),
                             "raw_expressions": False, "basic_mmd": False},
                         "expression_unavailable_reason": "独立 NPC 尚未接入确切的面部或耳部表情模板引用；当前不支持原始表情表或基础 MMD。",
                         "cpu_animation_template": reference.cpu_animation_template,
                         "animation_scope": "source_enemy_controller_clips_require_final_avatar_binding_and_saved_action_audit" if category.source_field == "enemyModelPrefab" else "source_cpu_template_and_selected_target_avatar_require_final_clip_and_material_audit" if configured else "source_family_candidates_require_avatar_binding",
                         "animation_catalog_available": ready and bool(npc_animation_tools) if configured or category.source_field == "enemyModelPrefab" else ready and animation_ready,
                         "provider_found": provider_found, "optional_tools_reason": optional_reason,
                         "unavailable_reason": None if ready else runtime_reason or (assembly_reason if configured else optional_reason) or reasons[entry.source_kind],
                         "assembly_scope": ("exact_prefab_info_slot_group_lod0" if configured else "one_exact_prefab_part") if choices else None})
        enemy_index_path = (enemy_index_from_catalog(asdict(npc), context.cache_root)
                            if any(source.source_name == "EnemyModelIndex" for source in npc.sources) else None)
        enemy_coverage: dict[str, object] = {"status": "reader_not_configured", "bound_template_count": 0,
                                           "scope": "Exact source bindings; per-export geometry, rig and materials still require validation."}
        if enemy_index_path is not None:
            enemy_index = read_enemy_model_index(enemy_index_path)
            represented = {part.part_name for entry in npc.entries for part in entry.resource_reference.prefab_parts
                           if entry.source_kind in ("prefab", "config_assembled")}
            bound = tuple(binding for binding in enemy_index.bindings if binding.model_id in represented)
            unsupported = [{"template_id": binding.template_id, "reason": "exact_model_prefab_not_bound",
                            "model_id": binding.model_id, "resource_path": binding.resource_path}
                           for binding in enemy_index.bindings if binding.model_id not in represented]
            unsupported.extend(asdict(row) for row in enemy_index.unavailable)
            enemy_coverage = {**enemy_coverage, "status": "indexed", "source_identity": enemy_index.source_identity,
                              "template_count": len(enemy_index.bindings) + len(enemy_index.unavailable),
                              "model_component_count": len(enemy_index.bindings), "bound_template_count": len(bound),
                              "unsupported": unsupported}
        return {"format": "CharacterCatalog/1", "catalog_id": _digest((identity, "catalog/1")), "source_identity": identity,
                "availability": {"ready": True, "reason": None}, "characters": rows,
                "enemy_model_coverage": enemy_coverage,
                "profiles": [{"profile_id": "hgrp-preview", "name": "HGRP 可编辑材质", "engines": ["EEVEE", "CYCLES"]}],
                "limitations": ["资源绑定不等于逐角色完整度验收；动作候选在导出时由所选 Avatar 验证。",
                                 "独立 NPC 每个变体为确切预制体部件或 PrefabInfo 同时配置的完整源槽组；配置动作需逐变体核对 CPU Avatar，最终轨道和材质在导出时验收。"]}

    def creature_catalog(self) -> dict[str, object]:
        """Offer source-classified creatures through the existing export contract."""
        catalog = self.catalog()
        rows = cast(list[dict[str, object]], catalog['characters'])
        creatures = [row for row in rows if row.get('asset_category') in ('animal', 'creature')]
        return {**catalog, 'catalog_scope': 'creatures', 'characters': creatures,
                'category_counts': {category: sum(row['asset_category'] == category for row in creatures)
                                    for category in ('animal', 'creature')},
                'scope_note': 'Exact NPC/environmental Animal/Monster configurations and EnemyData model prefab providers. Shared NPC templates retain distinct source IDs; combat variants collapse to canonical enemy templates.'}

    def detail(self, selection_id: str) -> dict[str, object]:
        catalog = self.catalog()
        rows = cast(list[dict[str, object]], catalog["characters"])
        matches = [row for row in rows if row["selection_id"] == selection_id]
        if len(matches) != 1:
            raise CharacterServiceError("当前目录没有该角色，请刷新后重新选择。")
        return {"format": "CharacterDetail/1", "catalog_id": catalog["catalog_id"], **matches[0]}

    def animations(self, selection_id: str, variant_id: str, offset: int, limit: int) -> dict[str, object]:
        if type(offset) is not int or offset < 0 or type(limit) is not int or not 1 <= limit <= 500:
            raise CharacterServiceError("动作分页要求 offset >= 0 且 limit 为 1 到 500。")
        context, catalog, npc, identity = self._catalog_source()
        match = {**self._variants(catalog), **self._npc_variants(npc)}.get(variant_id)
        if match is None or match[0] != selection_id:
            raise CharacterServiceError("动作目录的角色变体不属于当前选择，请刷新目录。")
        entries = tuple(row for row in npc.entries if row.npc_id == match[1].character_id
                        and row.resource_reference.template_id == match[1].template_id)
        if len(entries) == 1 and npc_entity_category(entries[0]).source_field == "enemyModelPrefab":
            document = self._enemy_actions(context, match[1], identity, variant_id, _no_cancel)
            source = parse_enemy_action_catalog(document, match[1])
            return {"format": "CharacterAnimationCatalog/1", "source_identity": identity, "selection_id": selection_id,
                "variant_id": variant_id, "offset": offset, "limit": limit, "total": len(source.actions),
                "binding_status": source.status, "unavailable_reason": source.reason,
                "coverage_counts": document["coverage_counts"],
                "unsupported_actions": [asdict(action) for action in source.unsupported_actions],
                "animations": [{"action_id": action.action_id, "name": action.name, "state_name": action.state_name,
                    "binding_status": "exact_enemy_controller_clip_reference_requires_final_clip_binding"}
                    for action in source.actions[offset:offset + limit]],
                "limitations": ["动作来自 EnemyData 动作配置及确切控制器片段指针；目录不证明完整 Animator 状态机还原。",
                                "所选动作仍需 Avatar/轨道校准、材质保留及逐帧保存重开验收。"]}
        if selection_id.startswith("independent-npc-") and len(entries) == 1 and entries[0].source_kind == "config_assembled":
            source = self._npc_actions(context, match[1], identity, variant_id, _no_cancel)
            return {"format": "CharacterAnimationCatalog/1", "source_identity": identity, "selection_id": selection_id,
                "variant_id": variant_id, "offset": offset, "limit": limit, "total": len(source.actions),
                "binding_status": source.status, "unavailable_reason": source.reason,
                "unsupported_actions": [asdict(action) for action in source.unsupported_actions],
                "coverage_counts": {"scope": "exact_cpu_base_state_references",
                    "source_state_count": len(source.actions) + len(source.unsupported_actions),
                    "available_action_count": len(source.actions), "unsupported_action_count": len(source.unsupported_actions)},
                "animations": [{"action_id": action.action_id, "name": action.name, "state_name": action.state_name,
                    "binding_status": ("source_generic_equivalent_avatar_requires_final_clip_binding" if source.status == "source_generic_equivalent_avatar_base_clip_references" else
                        "source_humanoid_target_avatar_requires_final_clip_binding" if source.status == "source_humanoid_target_avatar_base_clip_references" else "exact_cpu_avatar_base_clip_reference_requires_final_clip_binding")}
                    for action in source.actions[offset:offset + limit]],
                "limitations": ["Generic 跨 Avatar 动作仅在实际驱动路径及祖先骨架通过源数据比较时开放；不包含任意骨架重定向。" if source.status == "source_generic_equivalent_avatar_base_clip_references" else
                                "确切 CPU 模板和配置 Avatar 已按来源证据核对。",
                                "所选动作的完整轨道、可见材质及逐帧保存重开仍需导出验收。"]}
        actions = read_character_actions(context, identity, variant_id, match[1].character_id,
                                         self._families(context, match[1]), _no_cancel)
        result = {"format": "CharacterAnimationCatalog/1", "source_identity": identity, "selection_id": selection_id,
                "variant_id": variant_id, "offset": offset, "limit": limit, "total": len(actions),
                "animations": [{"action_id": action.action_id, "name": Path(action.resource_path).stem,
                                "binding_status": "provider_candidate_requires_selected_avatar"} for action in actions[offset:offset + limit]],
                "limitations": ["目录仅证明源资源与提供者存在；最终所选 Avatar 绑定及重开校验在导出时执行。"]}
        if not selection_id.startswith("independent-npc-"):
            replay = self._ordinary_replay()
            expression_replay = self._native_expression_replay()
            finalik_replay = self._finalik_replay()
            if finalik_replay is not None:
                for row, action in zip(result["animations"], actions[offset:offset + limit], strict=True):
                    binding = ReplaySelectionBinding(identity, selection_id, variant_id,
                        action.resource_path, action.provider.logical_name)
                    if replay_matches(finalik_replay, binding, match[1].character_id):
                        row["finalik_replays"] = [{"replay_id": finalik_replay["replay_id"], "label": finalik_replay["label"],
                            "boundary": "固定来源姿态上的显式离线目标、权重与历史；不是原生地面查询或实时 IK。"}]
            if expression_replay is not None:
                for row, action in zip(result["animations"], actions[offset:offset + limit], strict=True):
                    binding = ReplaySelectionBinding(identity, selection_id, variant_id,
                        action.resource_path, action.provider.logical_name)
                    if replay_matches(expression_replay, binding, match[1].character_id):
                        row["native_expression_replays"] = [{"replay_id": expression_replay["replay_id"],
                            "label": expression_replay["label"], "boundary": "显式源表情姿态与权重快照；不是自动表情或游戏实时记录。"}]
            if replay is not None:
                for row, action in zip(result["animations"], actions[offset:offset + limit], strict=True):
                    binding = ReplaySelectionBinding(identity, selection_id, variant_id,
                        action.resource_path, action.provider.logical_name)
                    if replay_matches(replay, binding, match[1].character_id):
                        row["ordinary_replays"] = [{"replay_id": replay["replay_id"], "label": replay["label"],
                            "boundary": "显式源场景普通层与状态时钟回放；不是游戏实时 Animator。"}]
        if selection_id.startswith("independent-npc-") and len(entries) == 1 and entries[0].source_kind == "prefab":
            cpu_template = entries[0].resource_reference.cpu_animation_template
            if cpu_template is None:
                references: dict[str, object] = {"format": "EndfieldNpcCpuActionReferences/1",
                    "status": "not_present", "execution_available": False, "actions": [],
                    "unavailable_reason": "该预制体目录没有 CPU 动作模板引用。"}
            else:
                references = self._npc_cpu_action_references(context, match[1], _no_cancel)
            result["source_cpu_action_references"] = references
        return result

    def canonical_preparation(self, payload: dict[str, object]) -> dict[str, object]:
        self._require_config()
        self._runtime_defaults({}, None)
        kind, request = normalize_character_request(payload)
        if kind != "preparation":
            raise CharacterServiceError("需要角色准备请求。")
        raw = json_object(request["source"], "source")
        paths = {name: _absolute_source_path(value, name) for name, value in raw.items()}
        game, blender = paths["game_root"], paths["blender_exe"]
        if not (game / "Endfield.exe").is_file() or not (game / "Endfield_Data/StreamingAssets/VFS").is_dir():
            raise CharacterServiceError("游戏目录不完整，请选择包含 Endfield.exe 的目录。")
        for name in ("cache_root", "output_root"):
            target = paths[name]
            _safe_storage(game, target)
        _safe_storage(game, self._state_path().parent)
        require_character_export_blender(blender)
        return {**request, "source": {name: str(path) for name, path in paths.items()}}

    def canonical_export(self, payload: dict[str, object]) -> dict[str, object]:
        kind, request = normalize_character_request(payload)
        if kind != "export":
            raise CharacterServiceError("需要角色导出请求。")
        options = json_object(request.get("options", {}), "options")
        if "appearance" not in options:
            _, runtime_defaults = self._runtime_defaults(options, None)
        context, catalog, npc, identity = self._catalog_source()
        require_character_export_blender(context.blender)
        if request["catalog_id"] != _digest((identity, "catalog/1")) or request["source_identity"] != identity:
            raise CharacterServiceError("角色目录已更新，请刷新后重新选择。")
        match = {**self._variants(catalog), **self._npc_variants(npc)}.get(str(request["variant_id"]))
        if match is None or match[0] != request["selection_id"]:
            raise CharacterServiceError("角色变体不属于当前选择，或尚无可运行的导出入口。")
        if "appearance" in options:
            parse_character_appearance_options(options["appearance"])
            if self.config is None or self.config.get("format") != "EndfieldCharacterRuntime/2":
                raise CharacterServiceError("外观选项需要支持 ExportRequest/2 的角色运行配置，请更新角色工具配置。")
            materials = json_object(options["materials"], "material options") if "materials" in options else None
            if materials is not None and materials.get("mode") == "simplified":
                raise CharacterServiceError("外观选项需要原始材质；请移除简化材质选项后重试。")
            if str(request["selection_id"]).startswith("independent-npc-"):
                raise CharacterServiceError("当前不支持独立 NPC 外观选项；请移除 options.appearance 后重试。")
            _, runtime_defaults = self._runtime_defaults(options, context)
        if options.get("animation") and "finalik_replay_id" in json_object(options["animation"], "animation options"):
            if str(request["selection_id"]).startswith("independent-npc-"):
                raise CharacterServiceError("FinalIK 回放暂仅支持角色 action_id；独立 NPC 回放尚未接入。")
        if options.get("native_expression_replay_id") and str(request["selection_id"]).startswith("independent-npc-"):
            raise CharacterServiceError("源表情回放暂仅支持具有确切表情模板的角色 action_id；独立 NPC 尚未接入。")
        if options.get("animation") and "replay_id" in json_object(options["animation"], "animation options"):
            if str(request["selection_id"]).startswith("independent-npc-"):
                raise CharacterServiceError("普通层回放暂仅支持角色 action_id 入口；独立 NPC 回放尚未接入。")
        if str(request["selection_id"]).startswith("independent-npc-"):
            entries = tuple(row for row in npc.entries if row.npc_id == match[1].character_id
                            and row.resource_reference.template_id == match[1].template_id)
            if len(entries) != 1:
                raise CharacterServiceError("独立 NPC 来源不唯一，请刷新目录。")
            if entries[0].source_kind == "config_assembled":
                materials = json_object(options["materials"], "material options") if "materials" in options else None
                if materials is not None and materials["mode"] == "simplified":
                    raise CharacterServiceError(
                        "简化材质暂不支持配置装配型 NPC；请改用原始材质模式，或选择普通角色/NPC 导出入口。"
                    )
                if not self._npc_assembly_tools():
                    raise CharacterServiceError("NPC 配置装配导出需要配置 npc_assembly_tools。")
                if any(options.get(name) for name in ("raw_expressions", "basic_mmd")):
                    raise CharacterServiceError("配置装配 NPC 尚无确切的表情模板接线，请关闭原始表情和基础 MMD。")
                if options.get("animation"):
                    animation = json_object(options["animation"], "animation options")
                    if "reference_id" in animation:
                        raise CharacterServiceError("reference_id 仅用于精确预制体 CPU 引用；配置装配 NPC 请使用 action_id。")
                    action_id = animation["action_id"]
                    actions = self._npc_actions(context, match[1], identity, str(request["variant_id"]), _no_cancel)
                    if not any(action.action_id == action_id for action in actions.actions):
                        raise CharacterServiceError("所选 NPC 动作没有当前配置的精确 CPU/Avatar 绑定，请刷新动作目录。")
                return {**request, "selection": asdict(match[1]), "context": _context_row(context)}
            elif options.get("raw_expressions") or options.get("basic_mmd"):
                raise CharacterServiceError("独立 NPC 尚未接入确切的表情模板引用，请关闭原始表情和基础 MMD 选项。")
            elif self._npc_prepare_script() is None:
                raise CharacterServiceError("独立 NPC 预制体导出需要配置 npc_prepare_script。")
            if options.get("animation"):
                animation = json_object(options["animation"], "animation options")
                if npc_entity_category(entries[0]).source_field == "enemyModelPrefab":
                    if "reference_id" in animation:
                        raise CharacterServiceError("敌人控制器片段请使用 action_id；reference_id 仅用于 NPC CPU 来源引用。")
                    document = self._enemy_actions(context, match[1], identity, str(request["variant_id"]), _no_cancel)
                    selected = enemy_animation_selection_document(document, match[1], str(animation["action_id"]))
                    return {**request, "selection": asdict(match[1]), "context": _context_row(context),
                            "enemy_animation_selection": selected}
                if "reference_id" in animation:
                    if entries[0].source_kind != "prefab":
                        raise CharacterServiceError("reference_id 需要精确预制体 NPC 来源。")
                    tools = self._npc_prefab_animation_tools()
                    _, defaults = self._runtime_defaults(options, context)
                    try:
                        references = read_npc_cpu_action_references(context, match[1], tools, defaults, _no_cancel)
                        document = npc_cpu_reference_document(references, match[1], str(animation["reference_id"]))
                    except (ValueError, OSError, RuntimeError) as error:
                        raise CharacterServiceError("NPC 精确 CPU 引用选择失败：" + public_npc_failure(error)) from error
                    return {**request, "selection": asdict(match[1]), "context": _context_row(context),
                            "npc_cpu_reference": document}
        if options.get("animation") and "reference_id" in json_object(options["animation"], "animation options"):
            raise CharacterServiceError("reference_id 仅用于独立预制体 NPC；当前角色请使用 action_id。")
        tools = self._authoring_tools() if options else {}
        replay_snapshot: dict[str, object] | None = None
        expression_snapshot: dict[str, object] | None = None
        finalik_snapshot: dict[str, object] | None = None
        needed: set[str] = set()
        if options.get("raw_expressions") or options.get("basic_mmd") or options.get("native_expression_replay_id"):
            needed.add("expression_exporter")
            if self.config is not None and self.config.get("format") == "EndfieldCharacterRuntime/2":
                needed.update(("expression_reader", "dotnet"))
        if options.get("basic_mmd") or options.get("animation"):
            needed.add("authoring_builder")
        if options.get("animation"):
            needed.update(("animation_exporter", "animation_reader", "dotnet"))
            action_id = json_object(options["animation"], "animation options")["action_id"]
            matches = [action for action in read_character_actions(context, identity, str(request["variant_id"]),
                match[1].character_id, self._families(context, match[1]), _no_cancel) if action.action_id == action_id]
            if len(matches) != 1:
                raise CharacterServiceError("所选动作不属于当前角色、变体或数据版本，请刷新动作目录。")
            if "replay_id" in json_object(options["animation"], "animation options"):
                replay_snapshot = self._selected_ordinary_replay(request, match[1],
                    matches[0].resource_path, matches[0].provider.logical_name)
            if options.get("native_expression_replay_id"):
                expression_snapshot = self._selected_expression_replay(request, match[1],
                    matches[0].resource_path, matches[0].provider.logical_name)
            if "finalik_replay_id" in json_object(options["animation"], "animation options"):
                finalik_snapshot = self._selected_finalik_replay(request, match[1],
                    matches[0].resource_path, matches[0].provider.logical_name)
        if not needed.issubset(tools):
            raise CharacterServiceError("所选动作或表情工具尚未配置，请检查 authoring_tools。")
        result = {**request, "selection": asdict(match[1]), "context": _context_row(context)}
        if "fur_curves_settings" in runtime_defaults:
            settings = Path(str(runtime_defaults["fur_curves_settings"]))
            result["fur_curves_settings_identity"] = {
                "path": str(settings), "sha256": hashlib.sha256(settings.read_bytes()).hexdigest()}
        if replay_snapshot is not None:
            result["ordinary_replay"] = replay_snapshot
        if expression_snapshot is not None:
            result["native_expression_replay"] = expression_snapshot
        if finalik_snapshot is not None:
            result["finalik_replay"] = finalik_snapshot
        return result

    def prepare(self, canonical: dict[str, object], output_dir: Path,
                check_cancelled: CheckCancelled, progress: Progress) -> dict[str, object]:
        check_cancelled()
        canonical = self.canonical_preparation(canonical)
        config, parent = self._require_config()
        raw = json_object(canonical["source"], "source")
        source = CharacterSourceContext(Path(str(raw["game_root"])), Path(str(raw["blender_exe"])),
                                        Path(str(raw["cache_root"])), Path(str(raw["output_root"])), Path(), Path(), Path())
        _safe_storage(source.game_root, output_dir)
        if not output_dir.resolve().is_relative_to(source.output_root):
            raise CharacterServiceError("角色产物目录不属于所选输出文件夹，请重新选择输出目录。")
        reusable: CharacterSourceContext | None = None
        configured = config.get("source_indexes")
        if configured is not None:
            indexes = json_object(configured, "source_indexes")
            if _path(indexes.get("game_root"), parent, "source_indexes.game_root") == source.game_root:
                reusable = CharacterSourceContext(source.game_root, source.blender, source.cache_root, source.output_root,
                    *(_path(indexes.get(name), parent, name) for name in ("provider_index", "resource_database", "cab_inventory")))
        scanner = _path(config["scanner"], parent, "scanner") if config.get("scanner") is not None else None
        while not self._prepare_lock.acquire(timeout=.2):
            check_cancelled()
        try:
            check_cancelled()
            before = game_source_stamps(source.game_root)
            check_cancelled()
            source.cache_root.mkdir(parents=True, exist_ok=True)
            source.output_root.mkdir(parents=True, exist_ok=True)
            context = prepare_character_sources(source, source.cache_root / "sources" / str(canonical["request_id"]),
                                                scanner, reusable, check_cancelled, progress)
            index_stamps = {name: asdict(source_stamp(getattr(context, name)))
                            for name in ("provider_index", "resource_database", "cab_inventory")}
            cache = CharacterDataCache(context.cache_root)
            catalog = read_character_catalog_cached(context.game_root, context.provider_index, cache, check_cancelled)
            npc = read_independent_npc_catalog(context.game_root, context.provider_index, cache,
                self._enemy_model_index(context, check_cancelled, progress), check_cancelled)
            weather_state: dict[str, object] = {}
            if config.get("format") == "EndfieldCharacterRuntime/2":
                from endfield_character_weather_prepare import prepare_character_weather_textures
                from endfield_character_weather_prepare import CharacterWeatherPreparationProgress

                def weather_progress(event: CharacterWeatherPreparationProgress) -> None:
                    progress("preparing", .7 + .18 * event.fraction, event.message)

                defaults = json_object(config.get("export_defaults"), "export_defaults")
                weather = prepare_character_weather_textures(context, required_text(defaults.get("unity_version"), "unity_version"),
                    _path(config.get("weather_reader"), parent, "weather_reader"),
                    source.cache_root / "weather" / str(canonical["request_id"]), check_cancelled, weather_progress)
                manifest = Path(weather.texture_manifest.path)
                weather_state = {"weather_manifest": {"path": str(manifest), "stamp": asdict(source_stamp(manifest))}}
            # Materialize only the small catalog; mesh and texture reads remain per selection.
            progress("preparing", .9, "Character names and exact model providers prepared")
            check_cancelled()
            self._commit_source_state(context, {"format": "EndfieldCharacterSourceContext/1", "context": _context_row(context),
                                      "catalog_identity": catalog.source_identity, "npc_catalog_identity": npc.source_identity,
                                      "game_stamps": before, "index_stamps": index_stamps, **weather_state}, check_cancelled)
        finally:
            self._prepare_lock.release()
        return {"status": "completed", "files": [], "pending": []}

    def _commit_source_state(self, context: CharacterSourceContext, record: dict[str, object],
                             check_cancelled: CheckCancelled) -> None:
        """Verify the original generation immediately before publishing readiness."""
        check_cancelled()
        state = self._state_path()
        _safe_storage(context.game_root, state.parent)
        state.parent.mkdir(parents=True, exist_ok=True)
        handle, temporary = tempfile.mkstemp(prefix="character_source_", suffix=".tmp", dir=state.parent)
        try:
            with os.fdopen(handle, "w", encoding="utf-8") as stream:
                json.dump(record, stream, ensure_ascii=False, indent=2)
            for name in ("provider_index", "resource_database", "cab_inventory"):
                check_cancelled()
                if asdict(source_stamp(getattr(context, name))) != json_object(record["index_stamps"], "index_stamps").get(name):
                    raise CharacterServiceError("角色资源索引在准备期间变化，请重新准备角色数据。")
            verify_game_source(context.game_root, cast(list[dict[str, object]], record["game_stamps"]), check_cancelled)
            os.replace(temporary, state)
        finally:
            Path(temporary).unlink(missing_ok=True)

    def export(self, canonical: dict[str, object], output_dir: Path,
               check_cancelled: CheckCancelled, progress: Progress) -> dict[str, object]:
        check_cancelled()
        current = self.canonical_export(canonical)
        if current != canonical:
            raise CharacterServiceError("角色准备数据已变化，请重新提交。")
        context = _context_from_row(json_object(canonical["context"], "context"))
        selected_options = json_object(canonical.get("options", {}), "options")
        exporter, defaults = self._runtime_defaults(selected_options, context)
        _safe_storage(context.game_root, output_dir)
        if not output_dir.resolve().is_relative_to(context.output_root):
            raise CharacterServiceError("角色产物目录不属于所选输出文件夹，请重新选择输出目录。")
        selection = json_object(canonical["selection"], "selection")
        catalog_context, catalog, npc, identity = self._catalog_source()
        if catalog_context != context or identity != canonical["source_identity"]:
            raise CharacterServiceError("角色准备数据在导出启动期间变化，请重新提交。")
        if str(canonical["selection_id"]).startswith("independent-npc-"):
            entries = tuple(row for row in npc.entries if row.npc_id == selection["character_id"]
                            and row.resource_reference.template_id == selection["template_id"])
            if len(entries) == 1 and entries[0].source_kind == "config_assembled":
                part_names = tuple(part.part_name for part in entries[0].resource_reference.prefab_parts)
                return self._export_npc_assembly(canonical, output_dir, context, npc.source_identity,
                                                 identity, part_names, defaults, check_cancelled, progress)
        catalog_path = output_dir / "source_catalog.json"
        write_index(catalog_path, asdict(npc) if str(canonical["selection_id"]).startswith("independent-npc-") else asdict(catalog))
        payload = output_dir / "payload"
        request = {"format": "EndfieldCharacterExportRequest/1", **defaults,
                   "game_root": str(context.game_root), "blender": str(context.blender), "catalog": str(catalog_path),
                   "database": str(context.resource_database), "cab_inventory": str(context.cab_inventory),
                   "cache_dir": str(context.cache_root), "output": str(payload),
                   **{name: selection[name] for name in ("character_id", "template_id", "part_name", "resource_path", "variant")},
                   "provider": selection["logical_provider"]}
        if "materials" in selected_options:
            request["material_options"] = selected_options["materials"]
        if "appearance" in selected_options:
            request["appearance_options"] = selected_options["appearance"]
        if self.config is not None and self.config.get("format") == "EndfieldCharacterRuntime/2":
            _, record = self._current()
            request["format"] = "EndfieldCharacterExportRequest/2"
            request["weather_manifest"] = json_object(record.get("weather_manifest"), "weather_manifest")["path"]
        if "fur_curves_settings_identity" in canonical:
            settings_identity = json_object(canonical["fur_curves_settings_identity"], "fur_curves_settings_identity")
            request["fur_curves_settings_sha256"] = settings_identity["sha256"]
        if str(canonical["selection_id"]).startswith("independent-npc-"):
            request["prepare_script"] = str(self._npc_prepare_script())
        request_path = output_dir / "export_request.json"
        write_index(request_path, request)
        log = output_dir / "export.log"
        temporary = output_dir / "tmp"
        temporary.mkdir()
        position = 0
        fragment = ""
        static_fraction = .74 if any(selected_options.get(name) for name in ("raw_expressions", "basic_mmd", "animation")) else 1.0
        fractions = {"prepared_cache": .69, "prepare_bundles": .15, "source_export": .45, "shader_identity": .6,
                     "renderer_visibility": .65, "fur_curves_source_channels": .69,
                     "blender_build": .92, "final_reopen": .98}

        preparation: Future[PreparedAuthoringState] | None = None
        prepared_ready = False
        stop_preparation = threading.Event()
        pool = ThreadPoolExecutor(max_workers=1, thread_name_prefix="character-source-authoring")
        overlap_started: float | None = None
        overlap_eligible = any(selected_options.get(name) for name in ("raw_expressions", "basic_mmd", "animation")) and "npc_cpu_reference" not in canonical and "enemy_animation_selection" not in canonical

        def preparation_cancel() -> None:
            check_cancelled()
            if stop_preparation.is_set():
                raise InterruptedError("Character base build stopped; cancelling its source authoring preparation")

        def preparation_progress(stage: str, fraction: float, message: str) -> None:
            progress(stage, min(fraction, .70), message)

        def monitor() -> None:
            nonlocal position, fragment, preparation, prepared_ready, overlap_started
            check_cancelled()
            if not log.is_file():
                return
            with log.open(encoding="utf-8") as stream:
                stream.seek(position)
                lines = (fragment + stream.read()).split("\n")
                position = stream.tell()
            fragment = lines.pop()
            for line in lines:
                if line.startswith('{"stage":'):
                    row = json_object(json.loads(line), "export progress")
                    stage = str(row["stage"])
                    if stage == "prepared_ready":
                        prepared_ready = True
                    if stage in fractions:
                        progress("auditing" if stage in {"blender_build", "final_reopen"}
                                 else "building" if stage == "renderer_visibility" else "extracting",
                                 fractions[stage] * static_fraction, "Completed " + stage)
            if preparation is not None and preparation.done():
                preparation.result()
            if overlap_eligible and prepared_ready and preparation is None:
                memory = system_memory_snapshot()
                if min(memory.available_physical_bytes, memory.available_commit_bytes) >= 7 * 1024**3:
                    overlap_started = time.perf_counter()
                    preparation = pool.submit(self._prepare_authoring, canonical, context, catalog, selection,
                        identity, payload, output_dir, defaults, selected_options, preparation_cancel, preparation_progress)

        try:
            with _character_memory(str(canonical["request_id"]), check_cancelled):
                progress("extracting", .02, "Starting the selected character reader")
                run_scene_process([str(defaults["python"]), str(exporter), "--request", str(request_path)], log,
                                  {**os.environ, "TEMP": str(temporary), "TMP": str(temporary), "PYTHONDONTWRITEBYTECODE": "1"}, monitor)
            monitor()
            receipt = _read(payload / "export_receipt.json")
            audit = _read(payload / "build/reopen_audit.json")
            if receipt.get("status") != "exported_reopened" or audit.get("status") != "passed":
                raise RuntimeError("Character reader did not finish the independent saved-file audit")
            blend = payload / "build/character.blend"
            with blend.open("rb") as stream:
                digest = hashlib.file_digest(stream, "sha256").hexdigest()
            if digest != receipt["blend_sha256"]:
                raise ValueError("Character delivery differs from its reader receipt")
            build = _read(payload / "build/build_receipt.json")
            options = json_object(canonical.get("options", {}), "options")
            verified_materials = _verified_character_material_options(options, receipt, build, audit)
            verified_appearance_options, appearance_report = _verified_character_appearance(
                options, receipt, build, audit)
            files: list[dict[str, object]] = [{"relative_path": blend.relative_to(output_dir).as_posix(),
                                            "role": "character", "bytes": blend.stat().st_size, "sha256": digest}]
            authoring_pending: list[dict[str, str]] = []
            if options.get("raw_expressions") or options.get("basic_mmd") or options.get("animation"):
                if "enemy_animation_selection" in canonical:
                    from endfield_character_enemy_action import run_enemy_prefab_action
                    with _character_memory(str(canonical["request_id"]), check_cancelled):
                        files, authoring_pending = run_enemy_prefab_action(context, parse_character_selection(selection),
                            json_object(canonical["enemy_animation_selection"], "enemy animation selection"), payload, output_dir,
                            defaults, self._npc_prefab_animation_tools(), json_object(options["animation"], "enemy animation options"),
                            check_cancelled, progress)
                elif "npc_cpu_reference" in canonical:
                    with _character_memory(str(canonical["request_id"]), check_cancelled):
                        files, authoring_pending = run_prefab_npc_action(context, parse_character_selection(selection),
                            json_object(canonical["npc_cpu_reference"], "NPC CPU reference"), payload, output_dir,
                            defaults, self._npc_prefab_animation_tools(),
                            json_object(options["animation"], "NPC reference options"), check_cancelled, progress)
                else:
                    if preparation is None:
                        files, authoring_pending = self._run_authoring(canonical, context, catalog, selection,
                            identity, payload, output_dir, defaults, options, check_cancelled, progress)
                    else:
                        state = preparation.result()
                        files, authoring_pending = self._finish_authoring(context, selection, payload, output_dir,
                            defaults, options, state, check_cancelled, progress)
            write_index(output_dir / "authoring_overlap.json", {"format": "EndfieldCharacterAuthoringOverlap/1",
                "started": overlap_started is not None, "memory_gate_bytes": 7 * 1024**3,
                "scope": "Independent source expressions/action preparation overlaps base Blender build; final authoring starts after base reopen."})
        finally:
            stop_preparation.set()
            pool.shutdown(wait=True, cancel_futures=True)
        late = build.get("late_materials", [])
        pending = [{"code": "native_runtime_approximation", "message": "使用可编辑 HGRP 主机近似；原生运行时布料、动画层混合及部分特效尚未完整还原。"}]
        pending.extend(character_source_pending(_read(payload / "prepared/static_probe/receipt.json"),
                                                _read(payload / "prepared/static_probe/source_audit.json")))
        if isinstance(late, list) and any(isinstance(row, dict) and row.get("status") == "source_variant_not_implemented" for row in late):
            pending.append({"code": "material_variant_unimplemented", "message": "部分源材质分支仍保留基础表现，详情待后续还原。"})
        check_cancelled()
        final_context, _, _, final_identity = self._catalog_source()
        if final_context != context or final_identity != identity:
            raise CharacterServiceError("角色或动作源数据在导出期间变化，请重新准备后导出。")
        manifest = {"status": "partial", "files": files, "pending": [*pending, *authoring_pending]}
        if verified_materials is not None:
            manifest["material_options"] = verified_materials
        if verified_appearance_options is not None and appearance_report is not None:
            manifest["appearance_options"] = verified_appearance_options
            manifest["appearance_report"] = appearance_report
        return manifest

    def _prepare_authoring(self, canonical: dict[str, object], context: CharacterSourceContext, catalog: CharacterCatalog,
                       selection: dict[str, object], identity: str, payload: Path, output_dir: Path,
                       defaults: dict[str, object], options: dict[str, object], check_cancelled: CheckCancelled,
                       progress: Progress) -> PreparedAuthoringState:
        action = None
        replay_snapshot: dict[str, object] | None = None
        expression_snapshot: dict[str, object] | None = None
        finalik_snapshot: dict[str, object] | None = None
        if options.get("animation"):
            action_id = json_object(options["animation"], "animation options")["action_id"]
            matches = [value for value in read_character_actions(context, identity, str(canonical["variant_id"]),
                str(selection["character_id"]), self._families(context, parse_character_selection(selection)), check_cancelled)
                if value.action_id == action_id]
            if len(matches) != 1:
                raise CharacterServiceError("角色动作来源已变化，请刷新后重新提交。")
            action = matches[0]
            if "replay_id" in json_object(options["animation"], "animation options"):
                replay_snapshot = self._selected_ordinary_replay(canonical, parse_character_selection(selection),
                    action.resource_path, action.provider.logical_name)
                if replay_snapshot != canonical.get("ordinary_replay"):
                    raise CharacterServiceError("普通层回放配置在排队后变化，请刷新动作目录并重新提交。")
            if options.get("native_expression_replay_id"):
                expression_snapshot = self._selected_expression_replay(canonical, parse_character_selection(selection),
                    action.resource_path, action.provider.logical_name)
                if expression_snapshot != canonical.get("native_expression_replay"):
                    raise CharacterServiceError("表情回放配置在排队后变化，请刷新动作目录并重新提交。")
            if "finalik_replay_id" in json_object(options["animation"], "animation options"):
                finalik_snapshot = self._selected_finalik_replay(canonical, parse_character_selection(selection),
                    action.resource_path, action.provider.logical_name)
                if finalik_snapshot != canonical.get("finalik_replay"):
                    raise CharacterServiceError("FinalIK 回放配置在排队后变化，请刷新动作目录并重新提交。")
        with _character_memory("prepare-authoring-" + str(canonical["request_id"]), check_cancelled):
            templates = tuple(template for binding in catalog.model_bindings for template in binding.templates
                              if template.template_id == selection["template_id"]
                              and template.corresponding_character_id == selection["character_id"])
            if len(templates) > 1:
                raise CharacterServiceError("角色 PrefabInfo 模板重复，请重新准备角色数据。")
            prepared = prepare_character_authoring(context, parse_character_selection(selection),
                templates[0] if templates else None, payload,
                output_dir, defaults, self._authoring_tools(), options, action, replay_snapshot, expression_snapshot, finalik_snapshot,
                check_cancelled, progress)
            return PreparedAuthoringState(prepared, action, expression_snapshot)

    def _finish_authoring(self, context: CharacterSourceContext, selection: dict[str, object], payload: Path,
                          output_dir: Path, defaults: dict[str, object], options: dict[str, object],
                          state: PreparedAuthoringState, check_cancelled: CheckCancelled,
                          progress: Progress) -> tuple[list[dict[str, object]], list[dict[str, str]]]:
        with _character_memory("authoring-" + output_dir.name, check_cancelled):
            return finish_character_authoring(context, str(selection["character_id"]), payload, output_dir,
                defaults, self._authoring_tools(), options, state.action, state.expression_snapshot,
                state.prepared, check_cancelled, progress)

    def _run_authoring(self, canonical: dict[str, object], context: CharacterSourceContext, catalog: CharacterCatalog,
                       selection: dict[str, object], identity: str, payload: Path, output_dir: Path,
                       defaults: dict[str, object], options: dict[str, object], check_cancelled: CheckCancelled,
                       progress: Progress) -> tuple[list[dict[str, object]], list[dict[str, str]]]:
        state = self._prepare_authoring(canonical, context, catalog, selection, identity, payload, output_dir,
                                       defaults, options, check_cancelled, progress)
        return self._finish_authoring(context, selection, payload, output_dir, defaults, options, state,
                                      check_cancelled, progress)

    def _export_npc_assembly(self, canonical: dict[str, object], output_dir: Path,
                             context: CharacterSourceContext, npc_identity: str, identity: str,
                             part_names: tuple[str, ...], defaults: dict[str, object], check_cancelled: CheckCancelled,
                             progress: Progress) -> dict[str, object]:
        """Run the source configuration adapter and verify its actual saved artifact."""
        options = json_object(canonical.get("options", {}), "NPC options")
        if "appearance" in options:
            raise CharacterServiceError("当前不支持独立 NPC 外观选项；请移除 options.appearance 后重试。")
        tools = self._npc_assembly_tools()
        selection = json_object(canonical["selection"], "selection")
        payload = output_dir / "payload"
        request = {"format": "EndfieldNpcExportRequest/1", "game_root": str(context.game_root),
            "blender": str(context.blender), "cache_root": str(context.cache_root),
            "provider_index": str(context.provider_index), "resource_database": str(context.resource_database),
            "cab_inventory": str(context.cab_inventory), "output": str(payload),
            "npc_id": selection["character_id"], "template_id": selection["template_id"],
            "part_name": selection["part_name"], "part_names": list(part_names), "source_identity": npc_identity,
            "unity_version": defaults["unity_version"], "tools": {name: str(path) for name, path in tools.items() if name != "exporter"}}
        animated = bool(options.get("animation"))
        if animated:
            action_options = json_object(options["animation"], "NPC animation options")
            selected = parse_character_selection(selection)
            actions = self._npc_actions(context, selected, identity, str(canonical["variant_id"]), check_cancelled)
            matches = tuple(action for action in actions.actions if action.action_id == action_options["action_id"])
            if len(matches) != 1:
                raise CharacterServiceError("NPC 动作源已变化，请刷新动作目录。")
            request["animation"] = {"action": asdict(matches[0]), "maximum_stored_frames": action_options["maximum_stored_frames"]}
            request["animation_tools"] = {name: str(path) for name, path in self._npc_animation_tools().items()}
            request["python"] = defaults["python"]
        request_path = output_dir / "export_request.json"
        write_index(request_path, request)
        temporary = output_dir / "tmp"
        temporary.mkdir()
        log = output_dir / "export.log"
        position = 0
        fragment = ""
        stages = {"assembly": (.4, .62), "shader": (.64, .75), "fbx_audit": (.78, .84), "materials": (.86, .94),
                  "animation": (.77, .86), "action_build": (.87, .94)}

        def monitor() -> None:
            nonlocal position, fragment
            check_cancelled()
            if not log.is_file():
                return
            with log.open(encoding="utf-8") as stream:
                stream.seek(position)
                lines = (fragment + stream.read()).split("\n")
                position = stream.tell()
            fragment = lines.pop()
            for line in lines:
                if not line.startswith('{"stage":'):
                    continue
                row = json_object(json.loads(line), "NPC export progress")
                stage = str(row.get("stage"))
                if stage in stages:
                    fraction = stages[stage][1 if row.get("state") == "completed" else 0]
                    progress("auditing" if stage in {"fbx_audit", "materials"} else "building", fraction, stage)
                elif stage == "extracting":
                    fraction = row.get("fraction")
                    if isinstance(fraction, (float, int)) and 0 <= fraction <= 1:
                        progress("extracting", .05 + .3 * fraction, "Reading exact NPC bundle dependencies")
        with _character_memory(str(canonical["request_id"]), check_cancelled):
            progress("extracting", .05, "Reading selected NPC configuration and exact source dependencies")
            run_scene_process([str(defaults["python"]), str(tools["exporter"]), "--request", str(request_path)],
                log, {**os.environ, "TEMP": str(temporary), "TMP": str(temporary),
                    "PYTHONDONTWRITEBYTECODE": "1"}, monitor)
        monitor()
        check_cancelled()
        progress("auditing", .95, "Verifying NPC source geometry, materials and saved-file reopen")
        receipt = _read(payload / "npc_export_receipt.json")
        if (receipt.get("format") != "EndfieldNpcExportReceipt/1"
                or receipt.get("status") != "source_assembled_reopened"
                or receipt.get("source_identity") != npc_identity
                or receipt.get("npc_id") != selection["character_id"]
                or receipt.get("template_id") != selection["template_id"]
                or receipt.get("part_name") != selection["part_name"]
                or receipt.get("part_names") != list(part_names)
                or receipt.get("runtime_renderer_fields_known") is not False
                or receipt.get("animation_exported") is not animated):
            raise RuntimeError("NPC configuration adapter did not verify the selected source and static scope")
        rows = receipt.get("files")
        if not isinstance(rows, list) or len(rows) != 1:
            raise ValueError("NPC configuration delivery requires one verified character file")
        row = json_object(rows[0], "NPC delivery")
        relative = Path(required_text(row.get("relative_path"), "NPC artifact"))
        if relative.is_absolute() or ".." in relative.parts:
            raise ValueError("NPC configuration delivery has an unsafe artifact path")
        blend = (payload / relative).resolve(strict=True)
        if not blend.is_relative_to(payload.resolve(strict=True)) or blend.suffix != ".blend" or row.get("role") != "character":
            raise ValueError("NPC configuration delivery escapes its source output or is not a character blend")
        with blend.open("rb") as stream:
            digest = hashlib.file_digest(stream, "sha256").hexdigest()
        if digest != row.get("sha256") or blend.stat().st_size != row.get("bytes"):
            raise ValueError("NPC character artifact differs from its source reopen receipt")
        verified_materials = _verified_npc_material_options(options, receipt)
        files = [{"relative_path": blend.relative_to(output_dir).as_posix(), "role": "character",
                  "bytes": blend.stat().st_size, "sha256": digest}]
        pending = [{"code": "npc_configuration_static_scope", "message":
            "已按源 AvatarMesh/AvatarTemplet 装配静态 LOD0 并重开验收；游戏运行时显隐、平台选择、布料及完整 HGRP 表现仍未还原。"},
            {"code": "npc_configuration_animation_scope", "message":
             "动作来自确切 CPU 基础状态并逐帧保存重开验收；跨槽动作重定向、运行时 Animator 和原生 IK 仍未还原。" if animated else
             "配置装配型 NPC 的表情尚未接入；可选动作需精确 CPU/Avatar 绑定和完整轨道验收。"}]
        unresolved = receipt.get("unresolved_archives")
        adapters = receipt.get("material_adapters", [])
        if not isinstance(adapters, list):
            raise ValueError("NPC configuration receipt requires material adapter records")
        if any(json_object(adapter, "material adapter").get("adapter") == "EndfieldStaticLitEffect/1"
               for adapter in adapters):
            pending.append({"code": "creature_static_liteffect_scope", "message":
                "LitEffect 已绑定源贴图和可编辑参数；Blender 光照、粗糙度和透明裁剪为近似，动态溶解等分支尚未还原。"})
        if any(json_object(adapter, "material adapter").get("adapter") == "scene.make_mesh_effect_material / bpy.ops.node.group_make"
               for adapter in adapters):
            pending.append({"code": "creature_mesh_effect_snapshot_scope", "message":
                "已复用场景 VFX 的源贴图和 UV 公式，当前为时间快照；源采样设置、软交界和运行时顶点色衰减仍为近似，完整动画尚未还原。"})
        if type(unresolved) is not int or unresolved < 0:
            raise ValueError("NPC configuration receipt requires an exact unresolved-material archive count")
        if unresolved:
            pending.append({"code": "npc_disabled_material_archives", "message":
                f"有 {unresolved} 个显式禁用的源部件保留未解析材质记录；未编造材质或丢弃网格。"})
        check_cancelled()
        final_context, _, _, final_identity = self._catalog_source()
        if final_context != context or final_identity != identity:
            raise CharacterServiceError("NPC 源数据在导出期间变化，请重新准备后导出。")
        manifest: dict[str, object] = {"status": "partial", "files": files, "pending": pending}
        if verified_materials is not None:
            manifest["material_options"] = verified_materials
        return manifest
