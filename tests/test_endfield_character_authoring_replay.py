"""Check immutable facial template selection with actual local files."""
from __future__ import annotations

from dataclasses import asdict
import json
from pathlib import Path
import tempfile
import unittest
from uuid import uuid4

from endfield_character_authoring_replay import EXPRESSION_ROLES, read_configured_expression_replay, verify_authoring_replay_snapshot
from endfield_character_replay import CharacterReplayError, ReplaySelectionBinding, replay_file_hash, replay_matches
from endfield_character_service import CharacterService, CharacterServiceError
from webui_character_contract import CharacterRequestError, normalize_character_request, public_request


class NativeExpressionReplayTests(unittest.TestCase):
    def setUp(self) -> None:
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name).resolve()
        self.binding = ReplaySelectionBinding("source", "selection", "variant", "assets/action.fbx", "main/action.ab")
        self.body = {"format": "EndfieldAnimationExportReceipt/1", "status": "decoded_bound_and_merged_candidate",
            "character_id": "actor", "avatar_cab": "CAB-" + "a" * 32, "avatar_path_id": 1,
            "clip_source": "CAB-" + "b" * 32, "clip_path_id": 2, "animator_root_path": "root",
            "resource_path": self.binding.action_resource_path}
        self.sources = {role: self.root / (role + ".json") for role in EXPRESSION_ROLES}
        for role, path in self.sources.items():
            path.write_text(json.dumps(self.body if role == "body_receipt" else {}), encoding="utf-8")
        self.manifest = self.root / "absolute.json"
        self.manifest_record = {"format": "EndfieldNativeExpressionAbsoluteReplay/1", "character_id": "actor",
            "action_name": "Expression", "sources": {role: {"path": path.name, "sha256": replay_file_hash(path)}
                for role, path in self.sources.items()}}
        self.manifest.write_text(json.dumps(self.manifest_record), encoding="utf-8")
        self.configuration = self.root / "expression.json"
        self.configuration_record = {"format": "EndfieldCharacterNativeExpressionReplay/1", "label": "Explicit expression",
            "provenance": "Offline source pose and explicit frame weights", "selection_binding": asdict(self.binding),
            "templates": [{"path": self.manifest.name, "sha256": replay_file_hash(self.manifest)}]}
        self.configuration.write_text(json.dumps(self.configuration_record), encoding="utf-8")
        self.request = {"format": "CharacterExportRequest/1", "request_id": str(uuid4()), "catalog_id": "catalog",
            "source_identity": "source", "selection_id": "selection", "variant_id": "variant",
            "export_profile_id": "hgrp-preview", "output_label": "preview"}

    def test_relative_evidence_and_exact_binding(self) -> None:
        snapshot = read_configured_expression_replay(self.configuration)
        verify_authoring_replay_snapshot(snapshot)
        self.assertEqual(snapshot["templates"], [str(self.manifest)])
        self.assertTrue(replay_matches(snapshot, self.binding, "actor"))
        for field in ReplaySelectionBinding.__dataclass_fields__:
            changed = ReplaySelectionBinding(**{**asdict(self.binding), field: "other"})
            self.assertFalse(replay_matches(snapshot, changed, "actor"))

    def test_changed_evidence_or_config_rejects_snapshot(self) -> None:
        snapshot = read_configured_expression_replay(self.configuration)
        self.sources["emotion_probe"].write_text('{"changed":true}', encoding="utf-8")
        with self.assertRaisesRegex(CharacterReplayError, "pinned SHA256"):
            read_configured_expression_replay(self.configuration)
        with self.assertRaisesRegex(CharacterReplayError, "evidence changed"):
            verify_authoring_replay_snapshot(snapshot)

    def test_body_identity_missing_field_is_specific_public_error(self) -> None:
        body = {key: value for key, value in self.body.items() if key != "avatar_path_id"}
        self.sources["body_receipt"].write_text(json.dumps(body), encoding="utf-8")
        self.manifest_record["sources"]["body_receipt"]["sha256"] = replay_file_hash(self.sources["body_receipt"])
        self.manifest.write_text(json.dumps(self.manifest_record), encoding="utf-8")
        self.configuration_record["templates"][0]["sha256"] = replay_file_hash(self.manifest)
        self.configuration.write_text(json.dumps(self.configuration_record), encoding="utf-8")
        runtime = self.root / "runtime.json"
        runtime.write_text(json.dumps({"format": "EndfieldCharacterRuntime/1",
            "native_expression_replay_config": self.configuration.name}), encoding="utf-8")
        with self.assertRaisesRegex(CharacterServiceError, "avatar_path_id") as captured:
            CharacterService(runtime)._native_expression_replay()
        self.assertNotIn(str(self.root), str(captured.exception))

    def test_public_id_has_no_private_paths_and_requires_body_action(self) -> None:
        identifier = read_configured_expression_replay(self.configuration)["replay_id"]
        options = {"native_expression_replay_id": identifier, "path": "F:/private/pose.json",
            "animation": {"action_id": "action", "maximum_stored_frames": 61}}
        _, request = normalize_character_request({**self.request, "options": options})
        self.assertNotIn("private", json.dumps(public_request(request)))
        self.assertEqual(request["options"]["native_expression_replay_id"], identifier)
        for bad in ({"native_expression_replay_id": identifier}, {**options, "animation": {
                "reference_id": "a" * 64, "maximum_stored_frames": 61}},
                {**options, "native_expression_replay_id": "unknown"}):
            with self.assertRaises(CharacterRequestError):
                normalize_character_request({**self.request, "options": bad})


if __name__ == "__main__":
    unittest.main()
