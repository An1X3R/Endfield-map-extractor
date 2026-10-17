"""Exercise source boundaries and readiness commits on real local files."""
from __future__ import annotations

from dataclasses import asdict
import json
import os
from pathlib import Path
import sys
import tempfile
import unittest
from uuid import uuid4

from endfield_character_cache import CharacterSourceChangedError, source_stamp
from endfield_character_service import (
    CharacterService, CharacterServiceError, _PATH_DEFAULTS, _VALUE_DEFAULTS, require_character_export_blender,
)
from endfield_character_sources import CharacterSourceContext, game_source_stamps, prepare_character_sources
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
        _, defaults = CharacterService(self.config_path)._runtime_defaults()
        self.assertEqual(defaults['fur_curves_settings'], str(settings))
        settings.unlink()
        with self.assertRaisesRegex(CharacterServiceError, 'fur_curves_settings'):
            CharacterService(self.config_path)._runtime_defaults()

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
