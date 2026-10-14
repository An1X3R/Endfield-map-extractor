"""Authenticate server-owned replay selections using actual local scenario files."""
from __future__ import annotations

from dataclasses import asdict
import json
from pathlib import Path
import tempfile
import unittest
from uuid import uuid4

from endfield_character_replay import (CharacterReplayError, ReplaySelectionBinding,
    read_configured_replay, replay_file_hash, replay_matches, verify_replay_snapshot)
from endfield_character_service import CharacterService, CharacterServiceError
from webui_character_contract import CharacterRequestError, normalize_character_request, public_request


class CharacterReplayTests(unittest.TestCase):
    def setUp(self) -> None:
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name).resolve()
        self.parameter = self.root / "parameter.json"
        self.state = self.root / "state.json"
        for path in (self.parameter, self.state):
            path.write_text("{}", encoding="utf-8")
        self.binding = ReplaySelectionBinding("source", "playable", "variant", "assets/action.fbx", "main/action.ab")
        self.configuration = self.root / "replay.json"
        self.record = {"format": "EndfieldCharacterOrdinaryReplay/1", "label": "Explicit source replay",
            "selection_binding": asdict(self.binding), "provenance": "Explicit offline test input",
            "animation_source_identity": {"character_id": "actor", "avatar_cab": "CAB-" + "a" * 32,
                "avatar_path_id": 1, "clip_source": "CAB-" + "b" * 32, "clip_path_id": 2,
                "controller_cab": "CAB-" + "c" * 32, "controller_path_id": 3},
            "parameter_scenario": self.parameter.name, "state_scenario": self.state.name,
            "scenario_sha256": {"parameter_scenario": replay_file_hash(self.parameter),
                "state_scenario": replay_file_hash(self.state)}}
        self.configuration.write_text(json.dumps(self.record), encoding="utf-8")
        self.request = {"format": "CharacterExportRequest/1", "request_id": str(uuid4()),
            "catalog_id": "catalog", "source_identity": "source", "selection_id": "playable",
            "variant_id": "variant", "export_profile_id": "hgrp-preview", "output_label": "preview"}

    def test_relative_scenarios_and_exact_selection_are_pinned(self) -> None:
        snapshot = read_configured_replay(self.configuration)
        verify_replay_snapshot(snapshot)
        self.assertEqual(snapshot["parameter_scenario"], str(self.parameter))
        self.assertTrue(replay_matches(snapshot, self.binding, "actor"))
        for field in ReplaySelectionBinding.__dataclass_fields__:
            changed = {**asdict(self.binding), field: "other"}
            self.assertFalse(replay_matches(snapshot, ReplaySelectionBinding(**changed), "actor"))
        self.assertFalse(replay_matches(snapshot, self.binding, "other_actor"))
        self.assertEqual(read_configured_replay(self.configuration), snapshot)

    def test_scenario_and_configuration_edits_reject_selected_snapshot(self) -> None:
        snapshot = read_configured_replay(self.configuration)
        self.state.write_text('{"changed":true}', encoding="utf-8")
        with self.assertRaisesRegex(CharacterReplayError, "pinned SHA256"):
            read_configured_replay(self.configuration)
        with self.assertRaisesRegex(CharacterReplayError, "changed after selection"):
            verify_replay_snapshot(snapshot)
        self.state.write_text("{}", encoding="utf-8")
        self.configuration.write_text(json.dumps({**self.record, "label": "Changed selection"}), encoding="utf-8")
        with self.assertRaisesRegex(CharacterReplayError, "changed after selection"):
            verify_replay_snapshot(snapshot)
        self.assertNotEqual(read_configured_replay(self.configuration)["replay_id"], snapshot["replay_id"])

    def test_public_request_keeps_only_id_and_rejects_reference_selector(self) -> None:
        identifier = read_configured_replay(self.configuration)["replay_id"]
        options = {"animation": {"action_id": "action", "maximum_stored_frames": 61,
            "replay_id": identifier, "state_scenario": "F:/private/state.json"},
            "ordinary_replay_config": "F:/private/replay.json"}
        _, canonical = normalize_character_request({**self.request, "options": options})
        self.assertNotIn("private", json.dumps(public_request(canonical)))
        self.assertEqual(canonical["options"]["animation"]["replay_id"], identifier)
        for invalid in ("unknown", True, "A" * 64):
            with self.assertRaises(CharacterRequestError):
                normalize_character_request({**self.request, "options": {"animation": {
                    "action_id": "action", "maximum_stored_frames": 61, "replay_id": invalid}}})
        with self.assertRaisesRegex(CharacterRequestError, "requires action_id"):
            normalize_character_request({**self.request, "options": {"animation": {
                "reference_id": "a" * 64, "maximum_stored_frames": 61, "replay_id": identifier}}})

    def test_malformed_server_fields_raise_specific_public_setup_error(self) -> None:
        self.record.pop("animation_source_identity")
        self.configuration.write_text(json.dumps(self.record), encoding="utf-8")
        runtime = self.root / "runtime.json"
        runtime.write_text(json.dumps({"format": "EndfieldCharacterRuntime/1",
            "ordinary_replay_config": self.configuration.name}), encoding="utf-8")
        with self.assertRaisesRegex(CharacterServiceError, "animation_source_identity") as captured:
            CharacterService(runtime)._ordinary_replay()
        self.assertNotIn(str(self.root), str(captured.exception))


if __name__ == "__main__":
    unittest.main()
