"""Exercise source boundaries and readiness commits on real local files."""
from __future__ import annotations

from dataclasses import asdict
import hashlib
import json
import os
from pathlib import Path
import sys
import tempfile
import unittest
from uuid import uuid4

from endfield_character_cache import CharacterSourceChangedError, source_stamp
from endfield_character_runtime import configure_character_runtime
from endfield_character_service import (
    CharacterService, CharacterServiceError, _PATH_DEFAULTS, _VALUE_DEFAULTS, require_character_export_blender,
)
from endfield_character_sources import CharacterSourceContext, game_source_stamps, prepare_character_sources
from endfield_character_tools import character_tool_source_identity
from endfield_dependency_bootstrap import helper_source_identity
from webui_character_worker import CharacterJobCancelled


def no_cancel() -> None:
    return None


def no_progress(stage: str, fraction: float, message: str) -> None:
    return None


class CharacterServiceTests(unittest.TestCase):
    def setUp(self) -> None:
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.root = Path(self.directory.name)
        game = self.root / "game"
        group = game / "Endfield_Data/StreamingAssets/VFS/group"
        group.mkdir(parents=True)
        (game / "Endfield.exe").write_bytes(b"game executable fixture")
        (group / "group.blc").write_bytes(b"vfs header fixture")
        self.chunk = group / "data.chk"
        self.chunk.write_bytes(b"vfs chunk fixture")
        tool = self.root / "blender.exe"
        tool.write_bytes(b"validation only; never executed")
        provider = self.root / "providers.jsonl"
        cab = self.root / "cab.jsonl"
        database = self.root / "database.sqlite"
        for file in (provider, cab):
            file.write_text('{"kind":"complete","failed":0}\n', encoding="utf-8")
        database.write_bytes(b"source stamp fixture; never queried")
        self.context = CharacterSourceContext(game, tool, self.root / "cache", self.root / "outputs", provider, database, cab)
        self.config_path = self.root / "config.json"
        self.config = {"format": "EndfieldCharacterRuntime/1", "exporter": str(tool), "state_root": str(self.root / "state"),
                       "export_defaults": {**{key: str(tool) for key in _PATH_DEFAULTS},
                                           **{key: "test" for key in _VALUE_DEFAULTS}}}
        self.config_path.write_text(json.dumps(self.config), encoding="utf-8")
        self.service = CharacterService(self.config_path)
        self.request = {"format": "CharacterPreparationRequest/1", "request_id": str(uuid4()),
                        "source": {"game_root": str(game), "blender_exe": str(tool),
                                   "cache_root": str(self.context.cache_root), "output_root": str(self.context.output_root)}}
        self.record = {"format": "EndfieldCharacterSourceContext/1", "context": {
            key: str(value) for key, value in asdict(self.context).items()}, "catalog_identity": "a" * 64,
            "npc_catalog_identity": "b" * 64, "game_stamps": game_source_stamps(game), "index_stamps": {
                key: asdict(source_stamp(getattr(self.context, key))) for key in
                ("provider_index", "resource_database", "cab_inventory")}}

    def _runtime2_service(self, export_defaults: dict[str, object],
                          npc_assembly_tools: dict[str, object] | None) -> CharacterService:
        reader = self.root / "json-reader.exe"
        reader.write_bytes(b"verified reader fixture")
        receipt = self.root / "character_tools.json"
        receipt.write_text(json.dumps({
            "format": "EndfieldCharacterTools/1", "status": "built",
            "source_identity": character_tool_source_identity(),
            "files": {reader.name: {"bytes": reader.stat().st_size,
                                    "sha256": hashlib.sha256(reader.read_bytes()).hexdigest()}},
            "tools": {"json_reader": reader.name},
        }), encoding="utf-8")
        config = {**self.config, "format": "EndfieldCharacterRuntime/2", "tools_receipt": str(receipt),
                  "export_defaults": {**self.config["export_defaults"], **export_defaults},
                  "npc_assembly_tools": npc_assembly_tools}
        self.config_path.write_text(json.dumps(config), encoding="utf-8")
        return CharacterService(self.config_path)

    def _write_scene_helper_receipt(self) -> Path:
        identity = helper_source_identity(Path(__file__).resolve().parents[1])
        helper_root = self.context.cache_root / "helpers" / identity[:16]
        outputs: dict[str, object] = {}

        def descriptor(path: Path, parent: Path) -> dict[str, object]:
            payload = path.read_bytes()
            return {"path": path.relative_to(parent).as_posix(), "bytes": len(payload),
                    "sha256": hashlib.sha256(payload).hexdigest().upper()}

        for name in ("bundleScanner", "sceneProbe"):
            executable_root = helper_root / "bin" / name
            executable_root.mkdir(parents=True, exist_ok=True)
            files = {f"{name}.exe": f"{name} executable fixture".encode(),
                     f"{name}.dll": f"{name} assembly fixture".encode(),
                     f"{name}.runtimeconfig.json": b'{"fixture":true}'}
            for filename, payload in files.items():
                (executable_root / filename).write_bytes(payload)
            executable = executable_root / f"{name}.exe"
            assembly = executable.with_suffix(".dll")
            outputs[name] = {
                **descriptor(executable, helper_root),
                "assembly": descriptor(assembly, helper_root),
                "runtimeFiles": [descriptor(executable_root / filename, executable_root) for filename in files],
            }
        receipt = helper_root / "current.json"
        receipt.write_text(json.dumps({"sourceIdentity": identity, "outputs": outputs}), encoding="utf-8")
        return receipt

    def test_appearance_runtime_defaults_normalize_configured_tool_paths(self) -> None:
        reader = self.root / "json-reader.exe"
        reader.write_bytes(b"reader")
        scene_probe = self.root / "sceneProbe.exe"
        scene_probe.write_bytes(b"scene probe")
        service = self._runtime2_service({"potential_json_reader": reader.name,
                                          "potential_scene_probe": scene_probe.name}, None)

        _, defaults = service._runtime_defaults(
            {"appearance": {"potential": "base", "fur": "existing"}}, self.context)

        self.assertEqual(str(reader.resolve()), defaults["potential_json_reader"])
        self.assertEqual(str(scene_probe.resolve()), defaults["potential_scene_probe"])

    def test_appearance_runtime_defaults_resolve_legacy_tools_from_verified_receipts(self) -> None:
        self._write_scene_helper_receipt()
        reader = self.root / "json-reader.exe"
        service = self._runtime2_service({}, {"json_reader": str(reader)})

        _, defaults = service._runtime_defaults(
            {"appearance": {"potential": "base", "fur": "existing"}}, self.context)

        self.assertEqual(str(reader.resolve()), defaults["potential_json_reader"])
        self.assertEqual(str((self.context.cache_root / "helpers" /
                              helper_source_identity(Path(__file__).resolve().parents[1])[:16] /
                              "bin" / "sceneProbe" / "sceneProbe.exe").resolve()),
                         defaults["potential_scene_probe"])

    def test_runtime_generation_includes_verified_appearance_tool_paths(self) -> None:
        self._write_scene_helper_receipt()
        names = ("static_reader", "shader_reader", "visibility_reader", "json_reader", "animation_reader",
                 "auxiliary_sampler", "controller_audit", "expression_reader", "snow_channel_reader",
                 "npc_assembly_probe", "npc_shader_probe", "npc_animation_reader", "weather_reader")
        files: dict[str, object] = {}
        tool_paths: dict[str, str] = {}
        for name in names:
            path = self.root / f"{name}.exe"
            path.write_bytes(f"{name} fixture".encode())
            files[path.name] = {"bytes": path.stat().st_size,
                                "sha256": hashlib.sha256(path.read_bytes()).hexdigest()}
            tool_paths[name] = path.name
        receipt = self.root / "runtime_tools.json"
        receipt.write_text(json.dumps({
            "format": "EndfieldCharacterTools/1", "status": "built",
            "source_identity": character_tool_source_identity(), "files": files, "tools": tool_paths,
        }), encoding="utf-8")
        destination = self.root / "generated_runtime.json"

        configure_character_runtime(receipt, self.context.blender, self.context.blender,
                                    self.context.cache_root, destination)

        document = json.loads(destination.read_text(encoding="utf-8"))
        defaults = document["export_defaults"]
        json_reader = (destination.parent / defaults["potential_json_reader"]).resolve()
        scene_probe = (destination.parent / defaults["potential_scene_probe"]).resolve()
        self.assertEqual((self.root / tool_paths["json_reader"]).resolve(), json_reader)
        self.assertEqual((self.context.cache_root / "helpers" /
                          helper_source_identity(Path(__file__).resolve().parents[1])[:16] /
                          "bin" / "sceneProbe" / "sceneProbe.exe").resolve(), scene_probe)
        self.assertEqual("EndfieldCharacterRuntime/2", document["format"])

    def test_missing_appearance_tool_fields_do_not_affect_legacy_requests(self) -> None:
        service = CharacterService(self.config_path)

        _, defaults = service._runtime_defaults({}, None)

        self.assertNotIn("potential_json_reader", defaults)
        self.assertNotIn("potential_scene_probe", defaults)
        upgraded_config = self._runtime2_service(
            {"potential_json_reader": "missing-reader.exe", "potential_scene_probe": "missing-probe.exe"}, None)
        _, upgraded_defaults = upgraded_config._runtime_defaults({}, None)
        self.assertNotIn("potential_json_reader", upgraded_defaults)
        self.assertNotIn("potential_scene_probe", upgraded_defaults)

    def test_appearance_runtime_defaults_fail_for_missing_configured_tools(self) -> None:
        missing_reader = self._runtime2_service(
            {"potential_json_reader": "missing-reader.exe", "potential_scene_probe": "missing-probe.exe"},
            {"json_reader": str(self.root / "json-reader.exe")})
        with self.assertRaisesRegex(CharacterServiceError, "满潜源配置读取器"):
            missing_reader._runtime_defaults(
                {"appearance": {"potential": "base", "fur": "existing"}}, self.context)

        missing_probe = self._runtime2_service(
            {"potential_json_reader": "json-reader.exe", "potential_scene_probe": "missing-probe.exe"}, None)
        with self.assertRaisesRegex(CharacterServiceError, "满潜场景探针"):
            missing_probe._runtime_defaults(
                {"appearance": {"potential": "base", "fur": "existing"}}, self.context)

    def test_validation_rejects_both_overlap_directions_and_relative_paths(self) -> None:
        source = self.request["source"]
        for field in ("cache_root", "output_root"):
            for target in (self.context.game_root, self.context.game_root / "nested", self.root, Path("relative")):
                with self.assertRaises(CharacterServiceError):
                    self.service.canonical_preparation({**self.request, "source": {**source, field: str(target)}})
        config = {**self.config, "state_root": str(self.context.game_root / "state")}
        self.config_path.write_text(json.dumps(config), encoding="utf-8")
        with self.assertRaises(CharacterServiceError):
            CharacterService(self.config_path).canonical_preparation(self.request)
        self.assertFalse(self.context.cache_root.exists())
        self.assertFalse(self.context.output_root.exists())

    def test_missing_prefab_builder_is_an_explicit_optional_setup_error(self) -> None:
        self.config_path.write_text(json.dumps({**self.config, "npc_animation_tools": {}}), encoding="utf-8")
        with self.assertRaisesRegex(CharacterServiceError, "prefab_action_builder"):
            CharacterService(self.config_path)._npc_prefab_animation_tools()

    def test_optional_fur_settings_resolve_from_runtime_directory(self) -> None:
        settings = self.root / 'fur.json'
        settings.write_text('{}', encoding='utf-8')
        values = {**self.config, 'export_defaults': {
            **self.config['export_defaults'], 'fur_curves_settings': 'fur.json'}}
        self.config_path.write_text(json.dumps(values), encoding='utf-8')
        _, defaults = CharacterService(self.config_path)._runtime_defaults({}, None)
        self.assertEqual(defaults['fur_curves_settings'], str(settings))
        settings.unlink()
        with self.assertRaisesRegex(CharacterServiceError, 'fur_curves_settings'):
            CharacterService(self.config_path)._runtime_defaults({}, None)

    def test_prefab_actions_require_only_their_actual_reader_tools(self) -> None:
        tool = self.context.blender
        values = {**self.config,
            "npc_animation_tools": {name: str(tool) for name in
                ("prefab_action_builder", "catalog_inspector", "exporter", "reader", "dotnet")},
            "npc_assembly_tools": {"json_reader": str(tool)},
            "authoring_tools": {"animation_exporter": str(tool), "expression_exporter": "missing_unused_expression.py"}}
        self.config_path.write_text(json.dumps(values), encoding="utf-8")
        tools = CharacterService(self.config_path)._npc_prefab_animation_tools()
        self.assertEqual(tools["json_reader"], tool)
        self.assertEqual(tools["animation_exporter"], tool)
        self.assertNotIn("expression_exporter", tools)

    def test_cancellation_after_lock_acquisition_creates_no_output_and_releases_lock(self) -> None:
        selected = os.environ.get("ENDFIELD_TEST_BLENDER")
        if not selected:
            self.skipTest("Set ENDFIELD_TEST_BLENDER to an actual Blender >=4.4 executable")
        request = {**self.request, "source": {**self.request["source"], "blender_exe": str(Path(selected).resolve())}}
        def cancel_when_acquired() -> None:
            if self.service._prepare_lock.locked():
                raise CharacterJobCancelled("cancel after admission")
        with self.assertRaises(CharacterJobCancelled):
            self.service.prepare(request, self.context.output_root / "character_jobs/artifacts" / str(uuid4()),
                                 cancel_when_acquired, no_progress)
        self.assertFalse(self.service._prepare_lock.locked())
        self.assertFalse(self.context.cache_root.exists())
        self.assertFalse(self.context.output_root.exists())

    def test_preparation_requires_an_actual_supported_blender_before_writing_outputs(self) -> None:
        supported = os.environ.get("ENDFIELD_TEST_BLENDER")
        unsupported = os.environ.get("ENDFIELD_TEST_UNSUPPORTED_BLENDER")
        if not supported or not unsupported:
            self.skipTest("Set actual supported and unsupported Blender executable paths for version integration checks")
        for executable in (supported, unsupported):
            request = {**self.request, "source": {
                **self.request["source"], "blender_exe": str(Path(executable).resolve())}}
            if executable == supported:
                self.assertEqual(self.service.canonical_preparation(request)["source"], request["source"])
            else:
                with self.assertRaisesRegex(CharacterServiceError, "Blender 4.4") as captured:
                    self.service.canonical_preparation(request)
                self.assertIn(str(Path(executable).resolve()), str(captured.exception))
                self.assertIn("returncode=0", str(captured.exception))
                self.assertIn("stdout=", str(captured.exception))
        self.assertFalse(self.context.cache_root.exists())
        self.assertFalse(self.context.output_root.exists())
        self.assertFalse((self.root / "state").exists())

    def test_blender_probe_rejects_non_blender_output_and_unlaunchable_file(self) -> None:
        with self.assertRaisesRegex(CharacterServiceError, "returncode=0") as captured:
            require_character_export_blender(Path(sys.executable))
        self.assertIn(str(Path(sys.executable)), str(captured.exception))
        self.assertIn("Python ", str(captured.exception))
        with self.assertRaisesRegex(CharacterServiceError, "returncode=not_started") as captured:
            require_character_export_blender(self.context.blender)
        self.assertIn(str(self.context.blender), str(captured.exception))

    def test_sources_cancel_before_snapshot_or_workspace_creation(self) -> None:
        workspace = self.context.cache_root / "sources" / str(uuid4())
        def cancelled() -> None:
            raise CharacterJobCancelled("cancel before source access")
        with self.assertRaises(CharacterJobCancelled):
            prepare_character_sources(self.context, workspace, None, None, cancelled, no_progress)
        self.assertFalse(workspace.exists())

    def test_source_workspace_cannot_escape_owned_cache(self) -> None:
        with self.assertRaisesRegex(ValueError, "inside its cache"):
            prepare_character_sources(self.context, self.root / "unowned", None, None, no_cancel, no_progress)
        self.assertFalse((self.root / "unowned").exists())

    def test_original_game_and_index_generation_is_required_for_atomic_ready_commit(self) -> None:
        self.service._commit_source_state(self.context, self.record, no_cancel)
        state = self.root / "state/character_source.json"
        previous = state.read_bytes()
        self.assertEqual(self.service._current()[0], self.context)
        self.chunk.write_bytes(b"game updated after catalog read")
        with self.assertRaises(CharacterSourceChangedError):
            self.service._commit_source_state(self.context, self.record, no_cancel)
        self.assertEqual(state.read_bytes(), previous)
        with self.assertRaises(CharacterServiceError):
            self.service._current()
        self.assertEqual(list(state.parent.glob("*.tmp")), [])

    def test_changed_index_or_cancel_during_temp_write_preserves_previous_ready_record(self) -> None:
        self.service._commit_source_state(self.context, self.record, no_cancel)
        state = self.root / "state/character_source.json"
        previous = state.read_bytes()
        def cancel_after_temp_write() -> None:
            if list(state.parent.glob("character_source_*.tmp")):
                raise CharacterJobCancelled("cancel before publication")
        with self.assertRaises(CharacterJobCancelled):
            self.service._commit_source_state(self.context, self.record, cancel_after_temp_write)
        self.assertEqual(state.read_bytes(), previous)
        self.context.cab_inventory.write_text('{"kind":"error"}\n', encoding="utf-8")
        with self.assertRaises(CharacterServiceError):
            self.service._commit_source_state(self.context, self.record, no_cancel)
        self.assertEqual(state.read_bytes(), previous)
        self.assertEqual(list(state.parent.glob("*.tmp")), [])


if __name__ == "__main__":
    unittest.main()
