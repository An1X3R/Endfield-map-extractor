"""Validate exact-source NPC Action receipts without inventing Animator records."""
from dataclasses import replace
import hashlib
import json
from pathlib import Path
import tempfile
import unittest

from endfield_character_npc_action_adapter import (
    _npc_inspection_source_paths, npc_cpu_reference_document, parse_npc_action_catalog, parse_npc_cpu_action_references, public_npc_failure,
    parse_enemy_action_catalog,
)
from endfield_character_cache import CharacterDataCache
from endfield_character_selection import CharacterSelection
from endfield_character_enemy_animation import EMPTY_ENEMY_CLIP_EVIDENCE_FIELDS, empty_enemy_clip_evidence


class NpcActionCatalogTests(unittest.TestCase):
    def test_enemy_controller_action_authenticates_clip_kind_and_source_generation(self) -> None:
        action = {"action_id": "4cd473848e34a6c5ef716be661d019c4a078a3a52d80d8a267db8d2976752a5e",
                  "state_name": "controller_override_clip_0", "name": "source_idle", "resource_path": "assets/source.overridecontroller",
                  "logical_provider": "main/clip.ab", "clip_cab": "CAB-clip", "clip_path_id": "31",
                  "clip_raw_data_sha256": "c" * 64, "controller_clip_index": 0, "controller_clip_kind": "override",
                  "binding_status": "exact_enemy_controller_clip_reference_requires_final_clip_binding"}
        document = {"format": "EndfieldEnemyAnimationSource/1", "status": "source_enemy_controller_clip_references",
                    "source_identity": self.selection.catalog_identity, "npc_id": self.selection.character_id,
                    "template_id": self.selection.template_id, "part_name": self.selection.part_name,
                    "model_resource_path": self.selection.resource_path, "model_provider": self.selection.logical_provider,
                    "enemy_data": {"template_id": "template", "animation_config_path": "Data/Json/AnimationConfig/source.json", "raw_data_sha256": "d" * 64},
                    "animation_config": {"virtual_path": "Data/Json/AnimationConfig/source.json", "sha256": "e" * 64},
                    "controller": {"type": "AnimatorOverrideController", "raw_data_sha256": "f" * 64},
                    "base_controller": {}, "selected_avatar": {"cab": "CAB-avatar", "path_id": "17"},
                    "actions": [action], "unsupported_actions": [], "runtime_animator_reconstructed": False,
                    "coverage_counts": {"source_reference_count": 1, "effective_action_references": 1,
                        "available_action_count": 1, "empty_placeholder_count": 0, "unsupported_action_count": 0}}
        parsed = parse_enemy_action_catalog(document, self.selection)
        self.assertEqual(parsed.status, "source_enemy_controller_clip_references")
        self.assertEqual(parsed.actions[0].action_id, action["action_id"])
        with self.assertRaisesRegex(ValueError, "ID, pointer or source binding"):
            parse_enemy_action_catalog({**document, "actions": [{**action, "controller_clip_kind": "base"}]}, self.selection)
        with self.assertRaisesRegex(ValueError, "exact base controller"):
            parse_enemy_action_catalog({**document, "base_controller": None}, self.selection)
        with self.assertRaisesRegex(ValueError, "exact selected prefab"):
            parse_enemy_action_catalog(document, replace(self.selection, part_name="other"))
        rejected = {"state_name": "controller_override_clip_1", "clip_name": "source_placeholder",
            "resource_path": "assets/source.overridecontroller", "logical_provider": "main/clip.ab",
            "clip_cab": "CAB-clip", "clip_path_id": "32", "clip_raw_data_sha256": "d" * 64,
            "binding_status": "source_enemy_controller_empty_clip_placeholder", "reason": "Source ACL buffers and all source curves are empty",
            "controller_clip_kind": "override", "controller_clip_index": 1, "original_clip": {"cab": "CAB-original", "path_id": "33"},
            "empty_clip_evidence": dict.fromkeys(EMPTY_ENEMY_CLIP_EVIDENCE_FIELDS, 0)}
        partial = {**document, "unsupported_actions": [rejected], "coverage_counts": {
            "source_reference_count": 2, "effective_action_references": 2, "available_action_count": 1,
            "empty_placeholder_count": 1, "unsupported_action_count": 1}}
        parsed = parse_enemy_action_catalog(partial, self.selection)
        self.assertEqual(parsed.unsupported_actions[0].clip_name, "source_placeholder")
        self.assertEqual(parsed.unsupported_actions[0].controller_clip_index, 1)
        for changed in ({"action_id": "a" * 64}, {"controller_clip_index": 0},
                        {"empty_clip_evidence": {**rejected["empty_clip_evidence"], "acl_root_tracks": 1}}):
            with self.subTest(changed=changed), self.assertRaises(ValueError):
                parse_enemy_action_catalog({**partial, "unsupported_actions": [{**rejected, **changed}]}, self.selection)
        with self.assertRaisesRegex(ValueError, "coverage"):
            parse_enemy_action_catalog({**partial, "coverage_counts": document["coverage_counts"]}, self.selection)

    def test_empty_enemy_placeholder_requires_zero_acl_and_legacy_source_data(self) -> None:
        clip = {"m_AclCompressedBuffer": {"OutputTrackCount": 0, "RootTrackCount": 0, "FloatCurveCount": 0,
                    "TransformBufferData": "", "RootMotionBufferData": "", "FloatBufferData": "", "m_ConstantValues": []},
            "m_ClipBindingConstant": {"genericBindings": []}, "m_MuscleClip": {"m_Clip": {
                "m_StreamedClip": {"curveCount": 0, "data": []},
                "m_DenseClip": {"m_FrameCount": 0, "m_CurveCount": 0, "m_SampleArray": [], "m_ACLArray": ""},
                "m_ConstantClip": {"data": []}, "m_ACLClip": {"IsSet": False}}},
            **{field: [] for field in ("m_RotationCurves", "m_CompressedRotationCurves", "m_EulerCurves", "m_PositionCurves",
                                      "m_ScaleCurves", "m_FloatCurves", "m_PPtrCurves")}}
        self.assertEqual(empty_enemy_clip_evidence(clip), dict.fromkeys(EMPTY_ENEMY_CLIP_EVIDENCE_FIELDS, 0))
        for changed in ({"RootTrackCount": 7}, {"TransformBufferData": "AA=="}, {"m_ConstantValues": [0.0]}):
            with self.subTest(changed=changed):
                self.assertIsNone(empty_enemy_clip_evidence({**clip, "m_AclCompressedBuffer": {**clip["m_AclCompressedBuffer"], **changed}}))
        self.assertIsNone(empty_enemy_clip_evidence({**clip, "m_FloatCurves": [{"source": "curve"}]}))

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

    def test_generic_cross_avatar_requires_driven_path_and_pose_evidence(self) -> None:
        action = {**self.receipt["actions"][0],
            "binding_status": "source_generic_equivalent_avatar_base_clip_reference_requires_final_clip_binding"}
        evidence = {"format": "NpcGenericAvatarEquivalence/1", "tolerance": 1e-6,
            "source_avatar_sha256": "d" * 64, "target_avatar_sha256": "e" * 64,
            "clip_count": 1, "transform_binding_count": 6, "driven_path_count": 2, "validated_path_count": 4,
            "max_default_pose_delta": 1e-7, "max_avatar_skeleton_pose_delta": 0.0}
        value = {**self.receipt, "status": "source_generic_equivalent_avatar_base_clip_references",
            "avatar_identity_matches": False, "cpu_avatar": {"cab": "CAB-source", "path_id": "19"},
            "avatar_binding_status": "source_generic_equivalent_avatar", "template_namespace": "Generic",
            "cpu_avatar_source_sha256": "d" * 64, "target_avatar_source_sha256": "e" * 64,
            "generic_avatar_equivalence": evidence, "actions": [action], "unsupported_actions": []}
        parsed = parse_npc_action_catalog(value, self.selection, ("body",))
        self.assertEqual(parsed.status, value["status"])
        for field in ("generic_avatar_equivalence", "cpu_avatar_source_sha256", "target_avatar_source_sha256"):
            with self.subTest(field=field), self.assertRaises(ValueError):
                parse_npc_action_catalog({key: item for key, item in value.items() if key != field}, self.selection, ("body",))
        for changed in ({"format": "other"}, {"tolerance": 1e-3}, {"clip_count": True},
                        {"source_avatar_sha256": "f" * 64}, {"target_avatar_sha256": "f" * 64},
                        {"clip_count": 2}, {"transform_binding_count": 0}, {"driven_path_count": 0},
                        {"validated_path_count": 1}, {"max_default_pose_delta": 1e-5},
                        {"max_avatar_skeleton_pose_delta": float("nan")}):
            with self.subTest(changed=changed), self.assertRaises(ValueError):
                parse_npc_action_catalog({**value, "generic_avatar_equivalence": {**evidence, **changed}}, self.selection, ("body",))
        with self.assertRaisesRegex(ValueError, "Generic"):
            parse_npc_action_catalog({**value, "template_namespace": "Humanoid"}, self.selection, ("body",))
        with self.assertRaisesRegex(ValueError, "source binding"):
            parse_npc_action_catalog({**value, "actions": self.receipt["actions"]}, self.selection, ("body",))
        rejected = [{"state_name": "Bump" + direction, "clip_name": "source_rejected_" + direction,
            "resource_path": "assets/source_rejected.fbx", "logical_provider": "main/rejected.ab",
            "clip_cab": "CAB-rejected", "clip_path_id": "32", "clip_raw_data_sha256": "f" * 64,
            "binding_status": "source_generic_equivalent_avatar_rejected", "reason": "Exact driven Transform path is absent"}
            for direction in ("F", "B", "L", "R")]
        partial = {**value, "unsupported_actions": rejected}
        parsed = parse_npc_action_catalog(partial, self.selection, ("body",))
        self.assertEqual(len(parsed.actions), 1)
        self.assertEqual(tuple(item.state_name for item in parsed.unsupported_actions), ("BumpF", "BumpB", "BumpL", "BumpR"))
        self.assertEqual(parsed.unsupported_actions[0].reason, rejected[0]["reason"])
        self.assertEqual(parsed.unsupported_actions[0].clip_raw_data_sha256, "f" * 64)
        for changed in ({"state_name": "IdleLoop"}, {"clip_raw_data_sha256": "bad"},
                        {"binding_status": "supported"}, {"action_id": "a" * 64}, {"reason": ""}):
            with self.subTest(changed=changed), self.assertRaises(ValueError):
                parse_npc_action_catalog({**value, "unsupported_actions": [{**rejected[0], **changed}]}, self.selection, ("body",))
        with self.assertRaisesRegex(ValueError, "duplicate rejected"):
            parse_npc_action_catalog({**value, "unsupported_actions": rejected * 2}, self.selection, ("body",))
        with self.assertRaisesRegex(ValueError, "explicit unsupported"):
            parse_npc_action_catalog({key: item for key, item in value.items() if key != "unsupported_actions"}, self.selection, ("body",))

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
