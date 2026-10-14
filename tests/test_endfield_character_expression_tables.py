"""Validate independent source-table bindings without relying on character-name guesses."""
from __future__ import annotations

import base64
import hashlib
from pathlib import Path
import sys
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from endfield_character_catalog import CharacterPrefabInfoSource, CharacterPrefabReferenceField, CharacterPrefabTemplate, CharacterPrefabPart
from endfield_character_expression_tables import (
    CharacterExpressionTableError,
    build_expression_table_catalog,
    make_expression_table_bake_manifest,
    plan_expression_tables,
    read_expression_material_properties,
)


AVATAR_CAB = "CAB-avatar"
AVATAR_PATH_ID = "875047822331719712"
FACE_PATH = "assets/beyond/dynamicassets/gamedata/skeletalmorph/skeletalmorphcfg/girl/data_facemorph_avatar_pelica.asset"
EAR_PATH = "assets/beyond/dynamicassets/gamedata/skeletalmorph/skeletalmorphcfg/girl/data_earmorph_avatar_pelica.asset"
TEMPLATE_SHA = "a" * 64


def _template(reference_fields: tuple[CharacterPrefabReferenceField, ...]) -> CharacterPrefabTemplate:
    return CharacterPrefabTemplate(
        "npc_chr_0004_pelica",
        "chr_0004_pelica",
        CharacterPrefabInfoSource("Data/Json/NPC/PrefabInfo/npc_chr_0004_pelica.json", TEMPLATE_SHA, 120),
        ("chr_0004_pelica_postmodel",),
        reference_fields,
        (),
        "not_verified",
    )


def _candidate(path: str, logical_name: str) -> dict[str, object]:
    return {
        "logical_name": logical_name,
        "kind": "match",
        "container_paths": [path],
        "serialized_files": [{"cab": "CAB-morph", "externals": [AVATAR_CAB]}],
    }


def _resolution(candidates: tuple[dict[str, object], ...]) -> dict[str, object]:
    return {
        "format": "EndfieldExpressionResourceResolution/1",
        "status": "asset_selection_required",
        "index_scan": {"status": "complete", "provider_index_sha256": "b" * 64},
        "selected_animator_avatar": {"cab": AVATAR_CAB, "path_id": AVATAR_PATH_ID, "class_name": "Avatar"},
        "candidates": list(candidates),
    }


def _source_document(path: str, provider_name: str, avatar_path_id: str) -> dict[str, object]:
    raw_object = b"synthetic serialized morph object"
    raw_sha = hashlib.sha256(raw_object).hexdigest()
    names = ("ear_pose_ctrl", "shader_ctrl", "empty_ctrl")
    rids = ("101", "102", "103")
    classes = ("SkeletalMorphMappingData", "UnsupportedMappingClass", "SkeletalMorphMappingData")
    managed = [
        {"rid": rids[0], "class": classes[0], "data": {"id": 0, "bones": [{"boneID": 7, "boneNameHash": 33}]}},
        {"rid": rids[1], "class": classes[1], "data": {"id": 1}},
        {"rid": rids[2], "class": classes[2], "data": {"id": 2, "bones": []}},
    ]
    return {
        "schema": "EndfieldCharacterExpressionSource/2",
        "status": "verified_raw_source_extracted",
        "character_id": "chr_0004_pelica",
        "source": {
            "container_path": path,
            "provider_logical_name": provider_name,
            "provider_kind": "match",
            "serialized_files": [{"cab": "CAB-morph", "externals": [AVATAR_CAB]}],
            "serialized_file": "CAB-morph",
            "object_path_id": "-5109486278731279560",
            "object_class_id": 114,
            "object_bytes": len(raw_object),
            "raw_object_bytes_base64": base64.b64encode(raw_object).decode("ascii"),
            "bundle_sha256": "c" * 64,
            "raw_object_sha256": raw_sha,
        },
        "selected_animator_avatar": {"cab": AVATAR_CAB, "path_id": avatar_path_id, "class_name": "Avatar"},
        "selection": {"candidate_index_scan_complete": True, "selected_asset_avatar_pointer_verified": True},
        "provenance": {"source_audit_sha256": "d" * 64},
        "avatar_binding_provenance": {"avatar_cab": AVATAR_CAB, "avatar_path_id": avatar_path_id},
        "avatar_bone_bindings_status": "exact_avatar_m_id_to_tos_paths_and_source_base_pose_verified",
        "avatar_bone_bindings": [{"bone_id": 7, "bone_name_hash_unsigned": 33}],
        "decoded_source_canonical_sha256": "e" * 64,
        "raw": {
            "asset_prefix": {"m_Name": "data_earmorph_avatar_pelica", "data": {
                "morphMappingNames": list(names),
                "morphMappingCfgs": [{"rid": rid} for rid in rids],
            }},
            "managed_references": managed,
        },
        "control_index": [
            {"index": index, "name": name, "rid": rids[index], "class_name": classes[index]}
            for index, name in enumerate(names)
        ],
    }


class ExpressionTableTests(unittest.TestCase):
    def test_material_properties_resolve_exact_parameter_rid_without_bone_endpoint(self) -> None:
        document = _source_document(FACE_PATH, "main/face.ab", AVATAR_PATH_ID)
        raw = document["raw"]
        raw["asset_prefix"]["data"].update({"shaderParams": [{"rid": "104"}],
                                            "shaderParamNames": ["CharacterNPR_Skin__EmotionBlend"]})
        raw["managed_references"][1] = {"rid": "102", "class": "SkeletalMorphShaderPropMappingData",
            "data": {"id": 1, "shaderParam": {"rid": "104"}, "partType": 32, "vectorIndex": 0, "bones": []}}
        raw["managed_references"].append({"rid": "104", "class": "SkMorphShaderParamFloat",
            "data": {"_paramName": "_EmotionBlend", "defaultValue": 0., "_rendererMask": -1, "blendMode": 0}})
        document["control_index"][1]["class_name"] = "SkeletalMorphShaderPropMappingData"
        properties = read_expression_material_properties(document)
        self.assertEqual([(item.property_name, item.parameter_rid) for item in properties], [("_EmotionBlend", "104")])
        template = _template((CharacterPrefabReferenceField("facialMorphAvatarName", "FacialMorph/Avatar/Girl/pelica"),))
        catalog = build_expression_table_catalog(template, _resolution((_candidate(FACE_PATH, "main/face.ab"),)), (document,))
        self.assertEqual(catalog.tables[0].controls[1].endpoint_status, "material_property_control")
        raw["managed_references"][1]["data"]["shaderParam"]["rid"] = "999"
        with self.assertRaisesRegex(CharacterExpressionTableError, "no exact shader parameter"):
            read_expression_material_properties(document)

    def test_reference_fields_select_exact_tables_face_first_and_report_unbound_candidates(self) -> None:
        template = _template((
            CharacterPrefabReferenceField("earMorphAvatarName", "EarMorph/Avatar/Girl/pelica"),
            CharacterPrefabReferenceField("facialMorphAvatarName", "FacialMorph/Avatar/Girl/pelica"),
        ))
        resolution = _resolution((
            _candidate(EAR_PATH, "main/ear.ab"),
            _candidate(FACE_PATH, "main/face.ab"),
            _candidate(FACE_PATH.replace("data_facemorph", "data_tailmorph"), "main/extra.ab"),
        ))

        plan = plan_expression_tables(template, resolution)

        self.assertEqual([(item.family, item.status) for item in plan.bindings[:2]], [
            ("facial", "selected"), ("ear", "selected")])
        self.assertEqual([item.resource_path for item in plan.bindings[:2]], [FACE_PATH, EAR_PATH])
        self.assertEqual(plan.bindings[2].pending_code, "unbound_morph_candidate")
        self.assertEqual(plan.bindings[0].provider_logical_name, "main/face.ab")

    def test_missing_reference_and_incomplete_scan_remain_explicit(self) -> None:
        template = _template((CharacterPrefabReferenceField("facialMorphAvatarName", None),))
        missing = plan_expression_tables(template, _resolution(()))
        self.assertEqual(missing.bindings[0].pending_code, "missing_prefab_reference")
        self.assertEqual(missing.bindings[1].pending_code, "missing_prefab_reference")

        incomplete_resolution = _resolution((_candidate(FACE_PATH, "main/face.ab"),))
        incomplete_resolution["index_scan"] = {"status": "incomplete", "provider_index_sha256": "b" * 64}
        incomplete = plan_expression_tables(_template((
            CharacterPrefabReferenceField("facialMorphAvatarName", "FacialMorph/Avatar/Girl/pelica"),
        )), incomplete_resolution)
        self.assertEqual(incomplete.bindings[0].pending_code, "provider_index_scan_incomplete")

    def test_empty_reference_is_missing_but_nonempty_malformed_reference_stays_unsupported(self) -> None:
        empty_plan = plan_expression_tables(_template((
            CharacterPrefabReferenceField("earMorphAvatarName", ""),
        )), _resolution(()))
        empty_ear = next(binding for binding in empty_plan.bindings if binding.family == "ear")
        self.assertEqual(empty_ear.status, "pending")
        self.assertEqual(empty_ear.pending_code, "missing_prefab_reference")
        self.assertIsNone(empty_ear.reference_value)

        malformed_plan = plan_expression_tables(_template((
            CharacterPrefabReferenceField("earMorphAvatarName", "EarMorph/Avatar/"),
        )), _resolution(()))
        malformed_ear = next(binding for binding in malformed_plan.bindings if binding.family == "ear")
        self.assertEqual(malformed_ear.status, "pending")
        self.assertEqual(malformed_ear.pending_code, "unsupported_prefab_reference_format")
        self.assertEqual(malformed_ear.reference_value, "EarMorph/Avatar/")

    def test_duplicate_path_provider_is_ambiguous_not_picked_by_order(self) -> None:
        plan = plan_expression_tables(_template((
            CharacterPrefabReferenceField("facialMorphAvatarName", "FacialMorph/Avatar/Girl/pelica"),
        )), _resolution((_candidate(FACE_PATH, "main/one.ab"), _candidate(FACE_PATH, "main/two.ab"))))
        self.assertEqual(plan.bindings[0].status, "pending")
        self.assertEqual(plan.bindings[0].pending_code, "referenced_table_provider_ambiguous")

    def test_catalog_verifies_exact_source_and_surfaces_unsupported_controls(self) -> None:
        template = _template((
            CharacterPrefabReferenceField("facialMorphAvatarName", "FacialMorph/Avatar/Girl/pelica"),
            CharacterPrefabReferenceField("earMorphAvatarName", "EarMorph/Avatar/Girl/pelica"),
        ))
        resolution = _resolution((_candidate(FACE_PATH, "main/face.ab"), _candidate(EAR_PATH, "main/ear.ab")))
        catalog = build_expression_table_catalog(template, resolution, (_source_document(FACE_PATH, "main/face.ab", AVATAR_PATH_ID),))

        facial, ear = catalog.tables
        self.assertEqual(facial.status, "source_verified")
        self.assertEqual(facial.source_identity.resource_path, FACE_PATH)
        self.assertEqual(facial.source_identity.avatar_path_id, AVATAR_PATH_ID)
        self.assertEqual(facial.endpoint_readiness, "source_pose_endpoint_candidates_available")
        self.assertEqual([item.endpoint_status for item in facial.controls], [
            "source_pose_endpoint_candidate", "unsupported_mapping_class", "empty_mapping"])
        self.assertIn("unsupported_mapping_class", {item.code for item in catalog.pending})
        self.assertEqual(ear.status, "pending")
        self.assertEqual(ear.pending[0].code, "missing_table_sidecar")

    def test_source_avatar_identity_mismatch_fails_with_specific_error(self) -> None:
        template = _template((CharacterPrefabReferenceField("facialMorphAvatarName", "FacialMorph/Avatar/Girl/pelica"),))
        with self.assertRaisesRegex(CharacterExpressionTableError, "Avatar CAB/PathID"):
            build_expression_table_catalog(
                template,
                _resolution((_candidate(FACE_PATH, "main/face.ab"),)),
                (_source_document(FACE_PATH, "main/face.ab", "7"),),
            )

    def test_source_bone_hash_mismatch_is_rejected(self) -> None:
        template = _template((CharacterPrefabReferenceField("facialMorphAvatarName", "FacialMorph/Avatar/Girl/pelica"),))
        source = _source_document(FACE_PATH, "main/face.ab", AVATAR_PATH_ID)
        raw = source["raw"]
        raw["managed_references"][0]["data"]["bones"][0]["boneNameHash"] = 44
        with self.assertRaisesRegex(CharacterExpressionTableError, "bone-name hash differs"):
            build_expression_table_catalog(
                template,
                _resolution((_candidate(FACE_PATH, "main/face.ab"),)),
                (source,),
            )

    def test_bake_manifest_keeps_family_identity_and_every_control_disposition(self) -> None:
        template = _template((CharacterPrefabReferenceField("earMorphAvatarName", "EarMorph/Avatar/Girl/pelica"),))
        catalog = build_expression_table_catalog(
            template,
            _resolution((_candidate(EAR_PATH, "main/ear.ab"),)),
            (_source_document(EAR_PATH, "main/ear.ab", AVATAR_PATH_ID),),
        )

        ear_result = next(item for item in catalog.tables if item.binding.family == "ear")
        facial_result = next(item for item in catalog.tables if item.binding.family == "facial")
        manifest = make_expression_table_bake_manifest(ear_result, "ear_source/expression_source.json")

        self.assertEqual(manifest.format, "EndfieldExpressionTableBakeManifest/1")
        self.assertEqual(manifest.family, "ear")
        self.assertEqual(manifest.source_document_path, "ear_source/expression_source.json")
        self.assertEqual(manifest.endpoint_control_names, ("ear_pose_ctrl",))
        self.assertEqual(manifest.empty_control_names, ("empty_ctrl",))
        self.assertEqual(manifest.unsupported_control_names, ("shader_ctrl",))
        with self.assertRaisesRegex(CharacterExpressionTableError, "unverified"):
            make_expression_table_bake_manifest(facial_result, "missing.json")


if __name__ == "__main__":
    unittest.main()
