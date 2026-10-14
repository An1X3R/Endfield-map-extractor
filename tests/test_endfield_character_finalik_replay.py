"""Check pinned direct-limb scenarios without invoking or replacing a solver."""
from __future__ import annotations

from dataclasses import asdict
import json
from pathlib import Path
import tempfile
import unittest
from uuid import uuid4

from endfield_character_finalik_replay import read_configured_finalik_replay, verify_finalik_snapshot
from endfield_character_replay import CharacterReplayError, ReplaySelectionBinding, replay_file_hash
from webui_character_contract import CharacterRequestError, normalize_character_request, public_request


class FinalIKReplayTests(unittest.TestCase):
    def setUp(self) -> None:
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name).resolve()
        self.scenario = self.root / "scenario.json"
        self.scenario.write_text('{"format":"EndfieldFinalIKScenario/1"}', encoding="utf-8")
        self.inventory = self.root / "inventory.json"
        self.inventory.write_text("{}", encoding="utf-8")
        self.path = self.root / "finalik.json"
        self.binding = ReplaySelectionBinding("source", "selection", "variant", "assets/action.fbx", "main/action.ab")
        self.row = {"format": "EndfieldCharacterFinalIKReplay/1", "label": "Explicit limb correction",
            "provenance": "Offline targets/weights/history; no terrain query capture",
            "selection_binding": asdict(self.binding), "animation_source_identity": {
                "character_id": "actor", "avatar_cab": "CAB-" + "a" * 32, "avatar_path_id": 1,
                "clip_source": "CAB-" + "b" * 32, "clip_path_id": 2,
                "animator_root_path": "root", "resource_path": self.binding.action_resource_path},
            "expected_input_source_local_frames_sha256": "c" * 64,
            "scenario": self.scenario.name, "source_component_inventory": self.inventory.name,
            "source_sha256": {"scenario": replay_file_hash(self.scenario), "source_component_inventory": replay_file_hash(self.inventory)}}
        self.path.write_text(json.dumps(self.row), encoding="utf-8")

    def test_relative_paths_and_pose_are_part_of_selection_identity(self) -> None:
        snapshot = read_configured_finalik_replay(self.path)
        verify_finalik_snapshot(snapshot)
        self.assertEqual(snapshot["scenario"], str(self.scenario))
        self.row["expected_input_source_local_frames_sha256"] = "d" * 64
        self.path.write_text(json.dumps(self.row), encoding="utf-8")
        self.assertNotEqual(snapshot["replay_id"], read_configured_finalik_replay(self.path)["replay_id"])
        with self.assertRaisesRegex(CharacterReplayError, "configuration or scenario changed"):
            verify_finalik_snapshot(snapshot)

    def test_changed_scenario_and_wrong_source_are_rejected(self) -> None:
        self.scenario.write_text('{"format":"EndfieldFinalIKScenario/1","changed":true}', encoding="utf-8")
        with self.assertRaisesRegex(CharacterReplayError, "pinned SHA256"):
            read_configured_finalik_replay(self.path)
        self.scenario.write_text('{"format":"EndfieldFinalIKScenario/1"}', encoding="utf-8")
        self.row["animation_source_identity"]["resource_path"] = "assets/other.fbx"
        self.path.write_text(json.dumps(self.row), encoding="utf-8")
        with self.assertRaisesRegex(CharacterReplayError, "selected Action"):
            read_configured_finalik_replay(self.path)

    def test_public_selector_keeps_only_id_and_rejects_reference(self) -> None:
        identifier = read_configured_finalik_replay(self.path)["replay_id"]
        request = {"format": "CharacterExportRequest/1", "request_id": str(uuid4()), "catalog_id": "catalog",
            "source_identity": "source", "selection_id": "selection", "variant_id": "variant",
            "export_profile_id": "hgrp-preview", "output_label": "preview"}
        animation = {"action_id": "action", "maximum_stored_frames": 61,
            "finalik_replay_id": identifier, "scenario": "F:/private/scenario.json"}
        _, normalized = normalize_character_request({**request, "options": {"animation": animation}})
        self.assertEqual(normalized["options"]["animation"]["finalik_replay_id"], identifier)
        self.assertNotIn("private", json.dumps(public_request(normalized)))
        for invalid in ({"reference_id": "a" * 64, "maximum_stored_frames": 61, "finalik_replay_id": identifier},
                        {**animation, "finalik_replay_id": "unknown"}):
            with self.assertRaises(CharacterRequestError):
                normalize_character_request({**request, "options": {"animation": invalid}})

    def test_grounder_version_requires_its_explicit_pinned_scenario(self) -> None:
        direct = read_configured_finalik_replay(self.path)
        self.row["format"] = "EndfieldCharacterFinalIKReplay/2"
        self.path.write_text(json.dumps(self.row), encoding="utf-8")
        with self.assertRaisesRegex(CharacterReplayError, "EndfieldGrounderScenario/1"):
            read_configured_finalik_replay(self.path)
        self.scenario.write_text('{"format":"EndfieldGrounderScenario/1"}', encoding="utf-8")
        self.row["source_sha256"]["scenario"] = replay_file_hash(self.scenario)
        self.path.write_text(json.dumps(self.row), encoding="utf-8")
        grounder = read_configured_finalik_replay(self.path)
        verify_finalik_snapshot(grounder)
        self.assertEqual(grounder["replay_format"], "EndfieldFinalIKReplay/2")
        self.assertNotIn("replay_format", direct)
        self.assertNotEqual(grounder["replay_id"], direct["replay_id"])
        self.row["format"] = "EndfieldCharacterFinalIKReplay/1"
        self.path.write_text(json.dumps(self.row), encoding="utf-8")
        with self.assertRaisesRegex(CharacterReplayError, "EndfieldFinalIKScenario/1"):
            read_configured_finalik_replay(self.path)


if __name__ == "__main__":
    unittest.main()
