"""Resolve server-owned Action IDs and run the configured source authoring tools."""
from __future__ import annotations

from collections.abc import Callable
from contextlib import closing
from dataclasses import asdict, dataclass
import hashlib
import json
import os
from pathlib import Path
import sqlite3

from endfield_character_bundle_inputs import prepare_provider_bundles
from endfield_character_cache import CharacterDataCache
from endfield_character_catalog import CharacterPrefabTemplate, CharacterResourceProvider, _provider_details
from endfield_character_expression_tables import build_expression_table_catalog, make_expression_table_bake_manifest, plan_expression_tables
from endfield_character_sources import CharacterSourceContext
from endfield_character_selection import CharacterSelection
from endfield_character_replay import verify_replay_snapshot
from endfield_character_authoring_replay import verify_authoring_replay_snapshot
from endfield_character_finalik_replay import verify_finalik_snapshot
from endfield_scene_export import run_scene_process
from endfield_scene_resources import write_index
from webui_character_contract import CharacterRequestError, json_object, required_text


@dataclass(frozen=True)
class CharacterActionSource:
    action_id: str
    resource_path: str
    provider: CharacterResourceProvider


@dataclass(frozen=True)
class PreparedCharacterAuthoring:
    expression: Path | None
    expression_files: tuple[Path, ...]
    independent_manifests: tuple[Path, ...]
    expression_catalog_path: Path | None
    animations: tuple[dict[str, object], ...]
    pending: tuple[dict[str, str], ...]


def _digest(value: object) -> str:
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


def read_character_actions(context: CharacterSourceContext, source_identity: str, variant_id: str,
                           character_id: str, families: tuple[str, ...],
                           check_cancelled: Callable[[], None]) -> tuple[CharacterActionSource, ...]:
    """List exact provider candidates; the selected Avatar reader establishes binding."""
    cache = CharacterDataCache(context.cache_root)

    def build() -> object:
        with closing(sqlite3.connect(context.resource_database.as_uri() + "?mode=ro", uri=True)) as connection:
            paths = [str(row[0]).replace("\\", "/").casefold() for row in connection.execute(
                "SELECT path FROM paths WHERE path_folded LIKE '%/animations/%' AND path_folded NOT LIKE '%##%' ORDER BY path")]
        selected = frozenset(path for path in paths if path.endswith((".fbx", ".anim"))
                             and any(path.startswith(family.casefold()) for family in families))
        providers, _ = _provider_details(context.provider_index, selected, check_cancelled)
        return [{"resource_path": path, "provider": asdict(provider)}
                for path in sorted(providers) for provider in providers[path]]

    rows, _ = cache.get_or_build_json("character_actions", "exact-provider-actions/2",
        (context.resource_database, context.provider_index), (character_id, *families), build, check_cancelled)
    if not isinstance(rows, list):
        raise CharacterRequestError("Action catalog cache requires an array")
    from endfield_character_catalog import _cache_provider
    actions: list[CharacterActionSource] = []
    for value in rows:
        row = json_object(value, "Action source")
        path = required_text(row.get("resource_path"), "Action resource path")
        provider = _cache_provider(row.get("provider"), "Action provider")
        identity = _digest((source_identity, variant_id, path, asdict(provider)))
        actions.append(CharacterActionSource(identity, path, provider))
    return tuple(actions)


def _read(path: Path) -> dict[str, object]:
    return json_object(json.loads(path.read_text(encoding="utf-8-sig")), "authoring source")


def _rows(value: object, field: str) -> tuple[dict[str, object], ...]:
    if not isinstance(value, list):
        raise ValueError(f"{field}: expected array")
    return tuple(json_object(row, field) for row in value)


def _file_record(path: Path, root: Path, role: str) -> dict[str, object]:
    resolved = path.resolve(strict=True)
    if not resolved.is_relative_to(root.resolve(strict=True)):
        raise ValueError("Authoring artifact escapes the job output")
    with resolved.open("rb") as stream:
        digest = hashlib.file_digest(stream, "sha256").hexdigest()
    return {"relative_path": resolved.relative_to(root.resolve()).as_posix(), "role": role,
            "bytes": resolved.stat().st_size, "sha256": digest}


def prepare_character_authoring(context: CharacterSourceContext, selection: CharacterSelection, template: CharacterPrefabTemplate | None, payload: Path,
                            output_dir: Path, defaults: dict[str, object], tools: dict[str, Path],
                            options: dict[str, object], action: CharacterActionSource | None,
                            ordinary_replay: dict[str, object] | None,
                            native_expression_replay: dict[str, object] | None,
                            finalik_replay: dict[str, object] | None,
                            check_cancelled: Callable[[], None],
                            progress: Callable[[str, float, str], None]) -> PreparedCharacterAuthoring:
    """Prepare source expressions and one Action independently of Blender construction."""
    character_id = selection.character_id
    audit_path = payload / "prepared/static_probe/source_audit.json"
    audit = _read(audit_path)
    temporary = output_dir / "tmp"
    environment = {**os.environ, "TEMP": str(temporary), "TMP": str(temporary), "PYTHONDONTWRITEBYTECODE": "1"}

    def execute(name: str, command: list[str], fraction: float) -> None:
        check_cancelled()
        progress("building", fraction, "Running " + name)
        run_scene_process(command, output_dir / (name + ".log"), environment, check_cancelled)

    expression: Path | None = None
    expression_files: list[Path] = []
    independent_manifests: list[Path] = []
    expression_catalog_path: Path | None = None
    pending: list[dict[str, str]] = []
    if options.get("raw_expressions") or options.get("basic_mmd") or native_expression_replay is not None:
        if template is None:
            raise ValueError("Expression authoring requires the exact selected PrefabInfo template")
        expression_dir = output_dir / "expressions"
        discovery_dir = output_dir / "expression_candidates"
        command = [str(defaults["python"]), str(tools["expression_exporter"]),
            "--character-id", character_id, "--source-audit", str(audit_path),
            "--provider-index", str(context.provider_index), "--resource-database", str(context.resource_database),
            "--game-vfs", str(context.game_root / "Endfield_Data/StreamingAssets/VFS"),
            "--extractor-src", str(defaults["repository_src"]),
            "--temp-root", str(temporary), "--unity-version", str(defaults["unity_version"])]
        if "expression_reader" in tools:
            command.extend(("--expression-reader", str(tools["expression_reader"]), "--dotnet", str(tools["dotnet"])))
        execute("expression_candidates", [*command, "--output-dir", str(discovery_dir)], .77)
        resolution = _read(discovery_dir / "resource_resolution.json")
        if resolution.get("status") != "asset_selection_required":
            raise ValueError(f"Expression reader cannot select one source Avatar: {resolution.get('status')}")
        scan = json_object(resolution.get("index_scan"), "expression candidate scan")
        if scan.get("status") != "complete":
            raise ValueError("Expression candidate index scan is incomplete")
        avatar_providers = _rows(resolution.get("avatar_provider_candidates"), "expression Avatar providers")
        if len(avatar_providers) != 1:
            raise ValueError("Expression authoring requires one exact Avatar provider")
        plan = plan_expression_tables(template, resolution)
        sources: list[dict[str, object]] = []
        source_paths: dict[str, Path] = {}
        for binding in plan.bindings:
            if binding.status != "selected":
                continue
            table_dir = expression_dir if binding.family == "facial" else output_dir / ("expressions_" + binding.family)
            execute("expression_source_" + binding.family, [*command, "--output-dir", str(table_dir),
                "--resource-path", required_text(binding.resource_path, "expression path"),
                "--logical-name", required_text(binding.provider_logical_name, "expression provider"),
                "--avatar-logical-name", required_text(avatar_providers[0].get("logical_name"), "Avatar provider"),
                "--avatar-cab", required_text(binding.avatar_cab, "Avatar CAB"),
                "--avatar-path-id", required_text(binding.avatar_path_id, "Avatar PathID")], .80)
            table_path = table_dir / "expression_source.json"
            source = _read(table_path)
            verification = _read(table_dir / "verification.json")
            if (source.get("schema") != "EndfieldCharacterExpressionSource/2" or source.get("character_id") != character_id
                    or verification.get("status") != "decoded_source_roundtrip_and_binary64_client_passed"
                    or verification.get("absolute_host_paths_in_artifact") is not False
                    or _file_record(table_path, output_dir, "expressions")["sha256"] != verification.get("artifact_sha256")):
                raise ValueError("Expression reader did not verify the selected table, hash and portable provenance")
            sources.append(source)
            source_paths[binding.family] = table_path
            expression_files.append(table_path)
            if binding.family == "facial":
                expression = table_path
        table_catalog = build_expression_table_catalog(template, resolution, tuple(sources))
        expression_catalog_path = output_dir / "expression_tables.json"
        write_index(expression_catalog_path, asdict(table_catalog))
        for item in table_catalog.pending:
            if item.code != "missing_prefab_reference":
                pending.append({"code": "expression_" + item.code, "message": item.message})
        if options.get("basic_mmd") and expression is None:
            raise ValueError("Basic MMD expressions require a verified facial source table")
        for table in table_catalog.tables:
            if table.binding.family == "facial" or table.status != "source_verified":
                continue
            manifest_path = output_dir / ("expression_" + table.binding.family + "_bake.json")
            manifest = make_expression_table_bake_manifest(table, str(source_paths[table.binding.family]))
            write_index(manifest_path, asdict(manifest))
            independent_manifests.append(manifest_path)

    animations: list[dict[str, object]] = []
    if action is not None:
        animators = [row for row in _rows(audit.get("animators"), "animators")
                     if isinstance(json_object(row.get("avatar"), "Avatar").get("resolved"), dict)]
        if len(animators) != 1:
            raise ValueError("Animation requires exactly one resolved source Animator Avatar")
        animator = animators[0]
        avatar = json_object(json_object(animator["avatar"], "Avatar")["resolved"], "resolved Avatar")
        if avatar.get("type") != "Avatar" or type(avatar.get("path_id")) is not int or avatar["path_id"] == 0:
            raise ValueError("Selected Animator has no valid Avatar identity")
        cab = required_text(avatar.get("source"), "Avatar CAB")
        cab_index, _ = CharacterDataCache(context.cache_root).read_character_cab_index(context.cab_inventory, check_cancelled)
        logical_names = dict(cab_index.providers).get(cab.casefold(), ())
        bundle_paths = [(payload / "prepared/bundles" / name) for name in logical_names
                        if (payload / "prepared/bundles" / name).is_file()]
        if len(bundle_paths) != 1:
            raise ValueError("Selected Avatar CAB has no unique extracted source bundle")
        object_name = required_text(json_object(animator.get("object"), "Animator object").get("name"), "Animator name")
        transforms = [row for row in _rows(audit.get("transforms"), "source transforms")
                      if json_object(row.get("game_object"), "transform object").get("name") == object_name]
        if len(transforms) != 1:
            raise ValueError("Selected Animator root transform is ambiguous")
        root_path = required_text(transforms[0].get("path"), "Animator root path")
        controller = json_object(animator.get("controller"), "Animator controller").get("resolved")
        controller_bundle: str | None = None
        controller_selection_receipt: str | None = None
        if controller is not None:
            controller_cab = required_text(json_object(controller, "resolved controller").get("source"), "controller CAB")
            controller_paths = [payload / "prepared/bundles" / name for name in dict(cab_index.providers).get(controller_cab.casefold(), ())
                                if (payload / "prepared/bundles" / name).is_file()]
            if len(controller_paths) != 1:
                raise ValueError("Selected Animator controller has no unique source bundle")
            controller_bundle = str(controller_paths[0])
        bundles = prepare_provider_bundles(context, action.provider, output_dir / "action_bundles", check_cancelled,
            lambda stage, fraction, message: progress(stage, .82 + .03 * fraction, message))
        clips = [file.path for file in bundles if file.logical_name == action.provider.logical_name]
        if len(clips) != 1:
            raise ValueError("Selected Action provider was not extracted uniquely")
        controller_tools = {"controller_resolver", "controller_json_reader", "controller_audit"}
        configured_controller_tools = controller_tools.intersection(tools)
        if configured_controller_tools and configured_controller_tools != controller_tools:
            raise ValueError("Source controller resolution requires resolver, JSON reader and controller audit tools together")
        if controller_bundle is None and template is not None and configured_controller_tools:
            controller_output = output_dir / "controller_source"
            controller_request = output_dir / "controller_request.json"
            write_index(controller_request, {
                "format": "EndfieldCharacterControllerRequest/1", "repository_src": defaults["repository_src"],
                "game_root": str(context.game_root), "provider_index": str(context.provider_index),
                "resource_database": str(context.resource_database), "cache_root": str(context.cache_root),
                "character_id": character_id, "template_id": selection.template_id,
                "part_name": selection.part_name, "resource_path": selection.resource_path,
                "unity_version": defaults["unity_version"], "json_reader": str(tools["controller_json_reader"]),
                "controller_audit": str(tools["controller_audit"]), "output": str(controller_output),
                "action_resource_path": action.resource_path, "action_logical_provider": action.provider.logical_name})
            execute("controller_source", [str(defaults["python"]), str(tools["controller_resolver"]),
                "--request", str(controller_request)], .855)
            controller_receipt = _read(controller_output / "receipt.json")
            expected_selection = {"format": "EndfieldCharacterControllerReceipt/1",
                "character_id": character_id, "template_id": selection.template_id,
                "part_name": selection.part_name, "resource_path": selection.resource_path}
            if any(controller_receipt.get(key) != value for key, value in expected_selection.items()):
                raise ValueError("Source controller receipt does not match the selected character template and model")
            if controller_receipt.get("status") in ("source_normal_controller_selected_clip_verified", "source_normal_controller_dynamic_clip_verified"):
                controller_path = Path(required_text(controller_receipt.get("reader_controller_bundle"), "verified controller bundle")).resolve(strict=True)
                if not controller_path.is_relative_to(controller_output.resolve()):
                    raise ValueError("Resolved source controller escapes its prepared output")
                if _file_record(controller_path, controller_output, "controller")["sha256"] != controller_receipt.get("controller_bundle_sha256"):
                    raise ValueError("Resolved source controller hash differs from its verified receipt")
                controller_bundle = str(controller_path)
                controller_selection_receipt = str(controller_output / "receipt.json")
                pending.append({"code": "source_controller_runtime_scope", "message": "角色配置中的源控制器和所选片段已分别核对，可读取参数及支持的衣物层；游戏实时控制器切换、montage 混合及 IK 状态仍未复现。"})
            else:
                raise ValueError("Source controller resolver did not return a verified selection or an explicit missing static reference")
        animation_options = json_object(options["animation"], "animation options")
        animation_output = output_dir / "animation"
        request_path = output_dir / "animation_request.json"
        animation_request: dict[str, object] = {"format": "EndfieldAnimationExportRequest/1", "character_id": character_id,
            "avatar_bundle": str(bundle_paths[0]), "avatar_cab": cab, "avatar_path_id": avatar["path_id"],
            "clip_bundle": clips[0], "resource_path": action.resource_path, "source_audit": str(audit_path),
            "animator_root_path": root_path, "controller_bundle": controller_bundle,
            "controller_selection_receipt": controller_selection_receipt,
            "unity_version": defaults["unity_version"], "maximum_stored_frames": animation_options["maximum_stored_frames"],
            "output": str(animation_output), "dotnet": str(tools["dotnet"]), "reader": str(tools["animation_reader"])}
        if ordinary_replay is not None:
            verify_replay_snapshot(ordinary_replay)
            if controller_bundle is None:
                raise ValueError("Explicit ordinary replay requires the verified selected source controller")
            hashes = json_object(ordinary_replay["input_sha256"], "replay input hashes")
            template_path = output_dir / "ordinary_replay_template.json"
            write_index(template_path, {"format": "EndfieldOrdinaryLayerReplayTemplate/1",
                "source_identity": ordinary_replay["source_identity"], "provenance": ordinary_replay["provenance"],
                "replay_id": ordinary_replay["replay_id"],
                "parameter_scenario": ordinary_replay["parameter_scenario"], "state_scenario": ordinary_replay["state_scenario"],
                "scenario_sha256": {name: hashes[str(ordinary_replay[name])] for name in ("parameter_scenario", "state_scenario")},
                "snapshot_input_sha256": hashes,
                "sampling": {"repository_src": defaults["repository_src"], "game_root": str(context.game_root),
                    "cache_root": str(context.cache_root), "resource_database": str(context.resource_database),
                    "cab_inventory": str(context.cab_inventory), "sampler": str(tools["auxiliary_sampler"]),
                    "prepared_bundle_root": str(payload / "prepared/bundles")}})
            animation_request["ordinary_layer_replay_template"] = str(template_path)
        elif controller_bundle is not None:
            if "auxiliary_sampler" not in tools:
                raise ValueError("Source clothing layers require authoring_tools.auxiliary_sampler; repair the character tool bundle.")
            sampling_path = output_dir / "default_clothing_sampling.json"
            write_index(sampling_path, {
                "repository_src": defaults["repository_src"], "game_root": str(context.game_root),
                "cache_root": str(context.cache_root), "resource_database": str(context.resource_database),
                "cab_inventory": str(context.cab_inventory), "sampler": str(tools["auxiliary_sampler"]),
                "prepared_bundle_root": str(payload / "prepared/bundles")})
            animation_request["default_clothing_sampling"] = str(sampling_path)
        if finalik_replay is not None:
            verify_finalik_snapshot(finalik_replay)
            finalik_template = output_dir / "server_finalik_template.json"
            grounder_replay = finalik_replay.get("replay_format") == "EndfieldFinalIKReplay/2"
            write_index(finalik_template, {"format": ("EndfieldFinalIKReplayTemplate/2" if grounder_replay
                                                       else "EndfieldFinalIKReplayTemplate/1"),
                "source_identity": finalik_replay["source_identity"], "provenance": finalik_replay["provenance"],
                "scenario": finalik_replay["scenario"], "source_component_inventory": finalik_replay["source_component_inventory"],
                "expected_input_source_local_frames_sha256": finalik_replay["expected_input_source_local_frames_sha256"],
                "snapshot_input_sha256": finalik_replay["input_sha256"]})
            animation_request["finalik_replay_template"] = str(finalik_template)
        write_index(request_path, animation_request)
        if ordinary_replay is not None:
            verify_replay_snapshot(ordinary_replay)
        execute("animation_export", [str(defaults["python"]), str(tools["animation_exporter"]), "--request", str(request_path)], .86)
        receipt = _read(animation_output / "receipt.json")
        if receipt.get("status") != "decoded_bound_and_merged_candidate" or receipt.get("character_id") != character_id:
            raise ValueError("Animation reader did not bind the selected clip to its source Avatar")
        if "default_clothing_sampling" in animation_request:
            clothing = json_object(receipt.get("default_clothing"), "default clothing result")
            if clothing.get("status") == "applied":
                replay_path = Path(required_text(receipt.get("ordinary_layer_replay"), "default clothing receipt")).resolve(strict=True)
                if not replay_path.is_relative_to(animation_output.resolve()):
                    raise ValueError("Default clothing receipt escapes the selected animation output")
                pending.append({"code": "default_clothing_scope", "message": "已随动作自动应用源控制器支持的衣物辅助层；使用所选动作曲线与源默认参数，尚不包含实时布料碰撞和完整状态机。"})
            elif clothing.get("status") == "unsupported":
                reason = required_text(clothing.get("reason"), "unsupported default clothing reason")
                pending.append({"code": "default_clothing_unsupported", "message": "该动作的衣物辅助层尚不支持自动应用：" + reason})
            else:
                raise ValueError("Default clothing export did not report applied or an explicit unsupported reason")
        if finalik_replay is not None:
            verify_finalik_snapshot(finalik_replay)
            finalik_receipt_path = Path(required_text(receipt.get("finalik_replay"), "FinalIK replay receipt")).resolve(strict=True)
            if not finalik_receipt_path.is_relative_to(animation_output.resolve()):
                raise ValueError("FinalIK receipt escapes the selected animation output")
            finalik_receipt = _read(finalik_receipt_path)
            expected_status = ("explicit_grounder_two_leg_pose_replay_applied" if grounder_replay
                               else "explicit_animation0_direct_limb_replay_applied")
            if (finalik_receipt.get("status") != expected_status
                    or finalik_receipt.get("source_identity") != finalik_replay["source_identity"]):
                raise ValueError("FinalIK did not publish the exact requested source scenario")
            pending.append({"code": "finalik_replay_scope", "message": (
                "已在源动作及所选衣物回放之后应用显式 Grounder 骨盆、双腿和足端后处理并保存重开；地面查询结果、权重和历史由场景明确提供，未模拟游戏实时调度。"
                if grounder_replay else
                "已在源动作及所选衣物回放之后应用显式 FinalIK 目标、权重和历史并保存重开；地面查询及游戏实时调度仍未模拟。")})
        if ordinary_replay is not None:
            verify_replay_snapshot(ordinary_replay)
            replay_receipt_path = Path(required_text(receipt.get("ordinary_layer_replay"), "ordinary replay receipt")).resolve(strict=True)
            if not replay_receipt_path.is_relative_to(animation_output.resolve()):
                raise ValueError("Ordinary replay receipt escapes the selected animation output")
            replay_receipt = _read(replay_receipt_path)
            if (replay_receipt.get("status") != "explicit_source_scenario_applied"
                    or replay_receipt.get("request_format") != "EndfieldOrdinaryLayerReplay/4"
                    or replay_receipt.get("source_identity") != ordinary_replay["source_identity"]):
                raise ValueError("Ordinary replay did not publish the exact requested source-bound scenario")
            pending.append({"code": "ordinary_replay_scope", "message": "所选服务端显式场景已应用源衣物层、状态时钟与转换，并随动作保存重开；这是离线回放，不是游戏实时 Animator。"})
        animations.append({"receipt": str(animation_output / "receipt.json"), "armatures": [],
                           "action_name": Path(action.resource_path).stem})
        pending.append({"code": "animation_candidate", "message": "源动作与 Avatar 已绑定并重开验收；原生 IK、布料及游戏运行时一致性仍未完整还原。动作超出所选帧预算会明确失败，不截断。"})

    check_cancelled()
    return PreparedCharacterAuthoring(expression, tuple(expression_files), tuple(independent_manifests),
        expression_catalog_path, tuple(animations), tuple(pending))


def finish_character_authoring(context: CharacterSourceContext, character_id: str, payload: Path,
                              output_dir: Path, defaults: dict[str, object], tools: dict[str, Path],
                              options: dict[str, object], action: CharacterActionSource | None,
                              native_expression_replay: dict[str, object] | None,
                              prepared: PreparedCharacterAuthoring, check_cancelled: Callable[[], None],
                              progress: Callable[[str, float, str], None]) -> tuple[list[dict[str, object]], list[dict[str, str]]]:
    """Attach prepared source data only after the independently audited base build."""
    audit_path = payload / "prepared/static_probe/source_audit.json"
    build = _read(payload / "build/build_receipt.json")
    armature = required_text(build.get("armature"), "source armature")
    expression = prepared.expression
    expression_files = prepared.expression_files
    independent_manifests = prepared.independent_manifests
    expression_catalog_path = prepared.expression_catalog_path
    animations = [{**row, "armatures": [armature]} for row in prepared.animations]
    pending = list(prepared.pending)
    temporary = output_dir / "tmp"
    environment = {**os.environ, "TEMP": str(temporary), "TMP": str(temporary), "PYTHONDONTWRITEBYTECODE": "1"}
    def execute(name: str, command: list[str], fraction: float) -> None:
        check_cancelled()
        progress("building", fraction, "Running " + name)
        run_scene_process(command, output_dir / (name + ".log"), environment, check_cancelled)
    final_blend = payload / "build/character.blend"
    if animations or options.get("basic_mmd"):
        authoring = output_dir / "authoring_request.json"
        write_index(authoring, {"format": "EndfieldCharacterAuthoringRequest/1", "character_id": character_id,
            "source_audit": str(audit_path), "original_table": str(expression) if options.get("raw_expressions") else None,
            "basic_expressions": {"table": str(expression), "armature": armature, "controller": "EF Basic Expressions"}
            if options.get("basic_mmd") else None, "animations": animations,
            "independent_expression_tables": [
                {"manifest": str(path), "armature": armature, "controller": "EF Ear Expressions"}
                for path in independent_manifests] if options.get("basic_mmd") else []})
        output = output_dir / "authoring"
        build_request = output_dir / "authoring_build_request.json"
        write_index(build_request, {"format": "EndfieldCharacterAuthoringBuild/1", "source_blend": str(final_blend),
                                   "authoring": str(authoring), "repository_src": defaults["repository_src"],
                                   "cache_root": str(context.cache_root), "output": str(output)})
        execute("authoring_build", [str(context.blender), "--background", "--factory-startup", "--disable-autoexec",
            "--python-exit-code", "1", "--python", str(tools["authoring_builder"]), "--", "--request", str(build_request)], .93)
        receipt = _read(output / "build_receipt.json")
        reopened = _read(output / "reopen_audit.json")
        final_blend = output / "character_authoring.blend"
        actual = _file_record(final_blend, output_dir, "character")
        if reopened.get("status") != "passed" or reopened.get("scripts_enabled_on_reopen") is not False:
            raise ValueError("Authoring output failed its independent script-disabled reopen audit")
        if actual["sha256"] != receipt.get("blend_sha256") or actual["sha256"] != reopened.get("blend_sha256"):
            raise ValueError("Authoring file hash differs from its save/reopen receipts")
        if options.get("basic_mmd"):
            pending.append({"code": "basic_expression_scope", "message": "基础 MMD 表情控制仅包含可编辑表情端点；不包含完整 MMD 骨骼转换或原生表情混合。"})
    if native_expression_replay is not None:
        if expression is None or action is None or not animations:
            raise ValueError("Native expression replay requires a fresh facial table and authenticated body Action")
        verify_authoring_replay_snapshot(native_expression_replay)
        source_output = final_blend.parent
        expression_template = output_dir / "native_expression_template.json"
        write_index(expression_template, {"format": "EndfieldNativeExpressionReplayTemplate/1",
            "snapshot": native_expression_replay, "fresh_sources": {
                "source_blend": str(final_blend), "source_build_receipt": str(source_output / "build_receipt.json"),
                "source_audit": str(audit_path), "expression_table": str(expression),
                "body_receipt": animations[0]["receipt"]}, "armature": armature,
            "output": str(output_dir / "native_expression_replays")})
        authoring = output_dir / "native_expression_authoring_request.json"
        write_index(authoring, {"format": "EndfieldCharacterAuthoringRequest/1", "character_id": character_id,
            "source_audit": str(audit_path), "original_table": None, "basic_expressions": None,
            "animations": [], "native_expression_replay_template": str(expression_template)})
        output = output_dir / "authoring_native"
        build_request = output_dir / "native_expression_build_request.json"
        write_index(build_request, {"format": "EndfieldCharacterAuthoringBuild/1", "source_blend": str(final_blend),
            "authoring": str(authoring), "repository_src": defaults["repository_src"],
            "cache_root": str(context.cache_root), "output": str(output)})
        execute("native_expression_build", [str(context.blender), "--background", "--factory-startup", "--disable-autoexec",
            "--python-exit-code", "1", "--python", str(tools["authoring_builder"]), "--", "--request", str(build_request)], .97)
        verify_authoring_replay_snapshot(native_expression_replay)
        receipt, reopened = _read(output / "build_receipt.json"), _read(output / "reopen_audit.json")
        final_blend = output / "character_authoring.blend"
        actual = _file_record(final_blend, output_dir, "character")
        replays = _rows(receipt.get("native_expression_replays"), "native expression receipts")
        if (len(replays) != len(native_expression_replay["templates"])
                or reopened.get("status") != "passed" or reopened.get("scripts_enabled_on_reopen") is not False
                or actual["sha256"] != receipt.get("blend_sha256") or actual["sha256"] != reopened.get("blend_sha256")):
            raise ValueError("Native expression output differs from its script-disabled save/reopen receipts")
        pending.append({"code": "native_expression_replay_scope", "message": "已将所选源表情姿态和显式权重烘焙为独立 Action 并保存重开，保留原始动作及表情控制；不包含自动情绪、眨眼或游戏实时调度。"})
    files = [_file_record(final_blend, output_dir, "character")]
    if options.get("raw_expressions"):
        files.extend(_file_record(path, output_dir, "expressions") for path in expression_files)
        if expression_catalog_path is not None:
            files.append(_file_record(expression_catalog_path, output_dir, "expressions"))
        pending.append({"code": "raw_expression_scope", "message": "原始表按所选角色的独立引用提取，包含已核实的面部及耳部表；原生运行时混合和不支持的映射类型仍未还原。"})
    check_cancelled()
    return files, pending


def run_character_authoring(context: CharacterSourceContext, selection: CharacterSelection, template: CharacterPrefabTemplate | None, payload: Path,
                            output_dir: Path, defaults: dict[str, object], tools: dict[str, Path],
                            options: dict[str, object], action: CharacterActionSource | None,
                            ordinary_replay: dict[str, object] | None,
                            native_expression_replay: dict[str, object] | None,
                            finalik_replay: dict[str, object] | None,
                            check_cancelled: Callable[[], None],
                            progress: Callable[[str, float, str], None]) -> tuple[list[dict[str, object]], list[dict[str, str]]]:
    """Keep the sequential authoring API while sharing the independent preparation."""
    prepared = prepare_character_authoring(context, selection, template, payload, output_dir, defaults, tools,
        options, action, ordinary_replay, native_expression_replay, finalik_replay, check_cancelled, progress)
    return finish_character_authoring(context, selection.character_id, payload, output_dir, defaults, tools,
        options, action, native_expression_replay, prepared, check_cancelled, progress)
