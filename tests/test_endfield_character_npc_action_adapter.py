"""Validate exact-source NPC Action receipts without inventing Animator records."""
from dataclasses import replace
import hashlib
import json
from pathlib import Path
import tempfile
import unittest

from endfield_character_npc_action_adapter import (
    _npc_inspection_source_paths, npc_cpu_reference_document, parse_npc_action_catalog, parse_npc_cpu_action_references, public_npc_failure,
)
from endfield_character_cache import CharacterDataCache
from endfield_character_selection import CharacterSelection


class NpcActionCatalogTests(unittest.TestCase):
    def setUp(self) -> None:
        self.selection = CharacterSelection("a" * 64, "miner", "template", "body", "npc", "assets/source.asset", "main/source.ab")
        self.receipt = {"format": "EndfieldNpcAnimationCatalog/1", "source_identity": "a" * 64,
            "npc_id": "miner", "template_id": "template", "part_name": "body",
            "status": "exact_avatar_base_clip_references", "avatar_identity_matches": True,
            "selected_avatar": {"cab": "CAB-avatar", "path_id": "17"},
            "cpu_avatar": {"cab": "cab-avatar", "path_id": "17"},
            "actions": [{"action_id": "b" * 64, "state_name": "IdleLoop", "name": "source_idle",
                "resource_path": "assets/source_idle.fbx", "logical_provider": "main/clip.ab", "clip_cab": "CAB-clip",
                "clip_path_id": "31", "clip_raw_data_sha256": "c" * 64,
                "binding_status": "exact_cpu_avatar_base_clip_reference_requires_final_clip_binding"}]}
        cpu_identity = [self.selection.catalog_identity, self.selection.character_id, self.selection.template_id,
            self.selection.part_name, "NPC/AnimationConfig/Humanoid/Girl/miner", "cab-cpu", "14", "d" * 64,
            "cab-avatar", "17", "IdleLoop", "cab-clip", "31", "main/clip.ab"]
        self.reference_receipt = {"format": "EndfieldNpcCpuActionReferences/1", "status": "exact_avatar_base_clip_references",
            "source_kind": "prefab", "source_identity": self.selection.catalog_identity, "npc_id": self.selection.character_id,
            "template_id": self.selection.template_id, "part_name": self.selection.part_name,
            "cpu_animation_template": "NPC/AnimationConfig/Humanoid/Girl/miner", "template_namespace": "Humanoid",
            "cpu_source": {"cab": "CAB-cpu", "path_id": "14"}, "cpu_source_sha256": "d" * 64,
            "selected_avatar": {"cab": "CAB-avatar", "path_id": "17"},
            "cpu_avatar": {"cab": "CAB-avatar", "path_id": "17"},
            "avatar_binding_status": "cpu_avatar_matches_avatar_templet_size_avatar",
            "execution_available": False, "runtime_animator_reconstructed": False,
            "actions": [{"reference_id": hashlib.sha256(json.dumps(cpu_identity, separators=(",", ":")).encode()).hexdigest(),
                "state_name": "IdleLoop", "clip_cab": "CAB-clip", "clip_path_id": "31", "logical_provider": "main/clip.ab",
                "binding_status": "exact_cpu_base_clip_reference", "execution_available": False}]}

    def test_exact_source_and_selection_are_required(self) -> None:
        result = parse_npc_action_catalog(self.receipt, self.selection, ("body",))
        self.assertEqual(result.actions[0].state_name, "IdleLoop")
        with self.assertRaises(ValueError):
            parse_npc_action_catalog(self.receipt, replace(self.selection, part_name="other"), ("body",))
        with self.assertRaises(ValueError):
            parse_npc_action_catalog({**self.receipt, "cpu_avatar": {"cab": "CAB-other", "path_id": "17"}}, self.selection, ("body",))

    def test_source_parser_upgrade_invalidates_real_file_cache(self) -> None:
        repository_src = Path(__file__).resolve().parents[1] / "src"
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            copied_src = root / "src"
            for path in _npc_inspection_source_paths(repository_src)[1:]:
                target = copied_src / path.relative_to(repository_src)
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_bytes(path.read_bytes())
            sources = _npc_inspection_source_paths(copied_src)
            cache = CharacterDataCache(root / "cache")
            for namespace, value in (("npc_cpu_actions", self.receipt),
                                     ("npc_cpu_action_references", self.reference_receipt)):
                def build() -> object:
                    return value

                def check_cancelled() -> None:
                    return None

                first, hit = cache.get_or_build_json(namespace, "dependency-test/1", sources, (), build, check_cancelled)
                self.assertFalse(hit)
                self.assertEqual(first, value)
                self.assertTrue(cache.get_or_build_json(namespace, "dependency-test/1", sources, (), build, check_cancelled)[1])
                # The inspector wrapper remains unchanged while a consumed parser changes.
                for name in ("endfield_character_npc_assembly.py", "endfield_character_npc_animation.py"):
                    parser = copied_src / name
                    parser.write_bytes(parser.read_bytes() + b"\n# Source parser upgrade.\n")
                    rebuilt, hit = cache.get_or_build_json(namespace, "dependency-test/1", sources, (), build, check_cancelled)
                    self.assertFalse(hit)
                    self.assertEqual(rebuilt, value)
                    self.assertTrue(cache.get_or_build_json(namespace, "dependency-test/1", sources, (), build, check_cancelled)[1])

    def test_avatar_mismatch_exposes_no_actions(self) -> None:
        value = {**self.receipt, "status": "avatar_mismatch", "avatar_identity_matches": False, "actions": []}
        self.assertEqual(parse_npc_action_catalog(value, self.selection, ("body",)).actions, ())
        with self.assertRaises(ValueError):
            parse_npc_action_catalog({**value, "actions": self.receipt["actions"]}, self.selection, ("body",))

    def test_duplicate_or_invalid_source_clip_is_rejected(self) -> None:
        with self.assertRaises(ValueError):
            parse_npc_action_catalog({**self.receipt, "actions": self.receipt["actions"] * 2}, self.selection, ("body",))
        action = self.receipt["actions"][0]
        with self.assertRaises(ValueError):
            parse_npc_action_catalog({**self.receipt, "actions": [{**action, "clip_path_id": "0"}]}, self.selection, ("body",))

    def test_cross_avatar_requires_independent_human_source_evidence(self) -> None:
        value = {**self.receipt, "status": "source_humanoid_target_avatar_base_clip_references",
            "avatar_identity_matches": False, "cpu_avatar": {"cab": "CAB-source", "path_id": "19"},
            "avatar_binding_status": "source_humanoid_to_selected_avatar", "template_namespace": "Humanoid",
            "cpu_avatar_source_sha256": "d" * 64, "target_avatar_source_sha256": "e" * 64}
        self.assertEqual(parse_npc_action_catalog(value, self.selection, ("body",)).actions[0].state_name, "IdleLoop")
        for field in ("avatar_binding_status", "cpu_avatar_source_sha256", "target_avatar_source_sha256"):
            with self.subTest(field=field), self.assertRaises(ValueError):
                parse_npc_action_catalog({key: item for key, item in value.items() if key != field}, self.selection, ("body",))
        with self.assertRaisesRegex(ValueError, "Humanoid"):
            parse_npc_action_catalog({**value, "template_namespace": "Generic"}, self.selection, ("body",))

    def test_full_source_slot_tuple_is_authenticated_without_independent_mount_actions(self) -> None:
        names = ("body", "face", "ear")
        value = {**self.receipt, "format": "EndfieldNpcAnimationCatalog/2", "part_names": list(names),
            "host_part_name": "body", "assembly_action_scope": "host_action_static_mounted_slots",
            "slots": [{"part_name": name, "independent_action_exported": False} for name in names]}
        self.assertEqual(parse_npc_action_catalog(value, self.selection, names).actions[0].state_name, "IdleLoop")
        for changed in ({**value, "part_names": ["body", "ear", "face"]},
                        {**value, "slots": value["slots"][:1]},
                        {**value, "slots": [{**slot, "independent_action_exported": True} for slot in value["slots"]]}):
            with self.assertRaises(ValueError):
                parse_npc_action_catalog(changed, self.selection, names)
        with self.assertRaisesRegex(ValueError, "Legacy single-slot"):
            parse_npc_action_catalog(self.receipt, self.selection, names)

    def test_public_failure_keeps_cause_and_removes_host_paths(self) -> None:
        for path in ("F:\\private tools\\reader.log", "/home/private tools/reader.log"):
            message = public_npc_failure(ValueError("Exact Avatar source missing: path=" + path))
            self.assertIn("Exact Avatar source missing", message)
            self.assertNotIn(path, message)

    def test_cpu_references_are_authenticated_and_never_executable(self) -> None:
        result = parse_npc_cpu_action_references(self.reference_receipt, self.selection)
        self.assertEqual(result.template_namespace, "Humanoid")
        self.assertEqual(result.actions[0].state_name, "IdleLoop")
        with self.assertRaises(ValueError):
            parse_npc_cpu_action_references(self.reference_receipt, replace(self.selection, part_name="other"))
        with self.assertRaises(ValueError):
            parse_npc_cpu_action_references({**self.reference_receipt, "execution_available": True}, self.selection)
        action = self.reference_receipt["actions"][0]
        with self.assertRaises(ValueError):
            parse_npc_cpu_action_references({**self.reference_receipt,
                "actions": [{**action, "clip_path_id": "32"}]}, self.selection)

    def test_cpu_states_may_reference_the_same_clip(self) -> None:
        first = self.reference_receipt["actions"][0]
        second_identity = [self.selection.catalog_identity, self.selection.character_id, self.selection.template_id,
            self.selection.part_name, self.reference_receipt["cpu_animation_template"], "cab-cpu", "14", "d" * 64,
            "cab-avatar", "17", "RunLoop", "cab-clip", "31", "main/clip.ab"]
        second = {**first, "state_name": "RunLoop",
            "reference_id": hashlib.sha256(json.dumps(second_identity, separators=(",", ":")).encode()).hexdigest()}
        result = parse_npc_cpu_action_references({**self.reference_receipt, "actions": [first, second]}, self.selection)
        self.assertEqual(tuple(action.state_name for action in result.actions), ("IdleLoop", "RunLoop"))

    def test_selected_reference_document_roundtrip_preserves_exact_identity(self) -> None:
        catalog = parse_npc_cpu_action_references(self.reference_receipt, self.selection)
        reference = catalog.actions[0]
        document = npc_cpu_reference_document(catalog, self.selection, reference.reference_id)
        self.assertEqual(parse_npc_cpu_action_references(document, self.selection), catalog)
        self.assertNotIn("action_id", document["actions"][0])
        with self.assertRaisesRegex(ValueError, "absent or duplicated"):
            npc_cpu_reference_document(catalog, self.selection, "f" * 64)
        with self.assertRaisesRegex(ValueError, "authenticated source pointers"):
            npc_cpu_reference_document(catalog, replace(self.selection, part_name="other"), reference.reference_id)


if __name__ == "__main__":
    unittest.main()
