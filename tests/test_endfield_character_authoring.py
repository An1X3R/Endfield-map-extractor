"""Exercise Action provider identity using real SQLite and JSONL inputs."""
from __future__ import annotations

from contextlib import closing
import json
from pathlib import Path
import sqlite3
import tempfile
import unittest

from endfield_character_authoring import read_character_actions
from endfield_character_sources import CharacterSourceContext
from webui_character_contract import CharacterRequestError, normalize_character_request, public_request, request_digest
from uuid import uuid4


class CharacterAuthoringTests(unittest.TestCase):
    def test_action_catalog_resolves_exact_family_provider_and_invalidates_changed_index(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            database = root / "resources.sqlite"
            paths = ("assets/beyond/arts/entity/actor/girl/pelica/animations/battle/stun.fbx",
                     "assets/beyond/arts/entity/actor/girl/pelicard/animations/battle/stun.fbx",
                     "assets/beyond/arts/entity/actor/panda/common/animations/battle/stun.fbx")
            with closing(sqlite3.connect(database)) as connection, connection:
                connection.execute("CREATE TABLE paths(path TEXT,path_folded TEXT)")
                connection.executemany("INSERT INTO paths VALUES(?,?)", [(path, path.casefold()) for path in paths])
            provider = root / "providers.jsonl"
            rows = [{"kind": "match", "logical_name": f"main/{index}.ab", "containers": [path],
                     "serialized": [{"name": f"CAB-{index}", "externals": []}]} for index, path in enumerate(paths)]

            def write_rows() -> None:
                provider.write_text("\n".join(json.dumps(row) for row in [*rows, {"kind": "complete", "failed": 0}]) + "\n", encoding="utf-8")

            write_rows()
            context = CharacterSourceContext(root / "game", root / "blender", root / "cache", root / "output",
                                             provider, database, root / "cab")
            family = ("assets/beyond/arts/entity/actor/girl/pelica/animations/",)
            actions = read_character_actions(context, "identity", "variant", "chr_0004_pelica", family, lambda: None)
            self.assertEqual(len(actions), 1)
            self.assertEqual(actions[0].resource_path, paths[0])
            cached = read_character_actions(context, "identity", "variant", "chr_0004_pelica", family, lambda: None)
            self.assertEqual(actions, cached)
            changed_identity = read_character_actions(context, "new", "variant", "chr_0004_pelica", family, lambda: None)
            self.assertNotEqual(changed_identity[0].action_id, actions[0].action_id)
            override = read_character_actions(context, "identity", "other", "chr_0018_dapan",
                ("assets/beyond/arts/entity/actor/panda/common/animations/",), lambda: None)
            self.assertEqual([action.resource_path for action in override], [paths[2]])
            rows[0]["logical_name"] = "main/changed.ab"
            write_rows()
            changed = read_character_actions(context, "identity", "variant", "chr_0004_pelica", family, lambda: None)
            self.assertNotEqual(changed[0].action_id, actions[0].action_id)
            self.assertEqual(changed[0].provider.logical_name, "main/changed.ab")

    def test_options_validate_frame_budget_and_strip_client_tool_paths(self) -> None:
        request = {"format": "CharacterExportRequest/1", "request_id": str(uuid4()), "catalog_id": "catalog",
                   "source_identity": "source", "selection_id": "playable", "variant_id": "variant",
                   "export_profile_id": "hgrp-preview", "output_label": "preview"}
        _, old = normalize_character_request(request)
        self.assertEqual(old, request)
        options = {"raw_expressions": True, "basic_mmd": True,
                   "animation": {"action_id": "action", "maximum_stored_frames": 61, "clip_bundle": "F:/private"},
                   "authoring_builder": "F:/private"}
        _, normalized = normalize_character_request({**request, "options": options})
        self.assertNotIn("private", json.dumps(public_request(normalized)))
        self.assertNotEqual(request_digest(normalized), request_digest(old))
        for invalid in (True, 1, 18001, 61.0):
            with self.assertRaises(CharacterRequestError):
                normalize_character_request({**request, "options": {"animation": {"action_id": "action", "maximum_stored_frames": invalid}}})
        with self.assertRaises(CharacterRequestError):
            normalize_character_request({**request, "options": {"basic_mmd": 1}})


if __name__ == "__main__":
    unittest.main()
