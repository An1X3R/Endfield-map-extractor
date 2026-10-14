"""Exercise NPC source selection against real JSONL, JSON and SQLite files."""
from __future__ import annotations

from contextlib import closing
from dataclasses import replace
import json
from pathlib import Path
import sqlite3
import struct
import tempfile
import unittest
from unittest.mock import patch

from endfield_character_npc_animation import resolve_npc_animation_prefixes
from endfield_character_npc_assembly import (
    NpcAssemblyPreparationError, NpcSourceObject, read_npc_source_objects,
    resolve_npc_assets, selected_npc_renderers, selected_npc_slots,
)
from endfield_character_npc_catalog import IndependentNpcRecord, IndependentNpcResourceReference
from endfield_character_npc_catalog import IndependentNpcCatalog, IndependentNpcCatalogSummary, IndependentNpcConfigReference, IndependentNpcPrefabReference
from endfield_character_catalog import (
    CharacterCatalog, CharacterModelSummary, CharacterProviderIndexSource,
    CharacterResourceProvider, CharacterSerializedCab,
)
from endfield_character_service import CharacterService, CharacterServiceError, _digest
from endfield_character_sources import CharacterSourceContext


class NpcAssemblyTests(unittest.TestCase):
    def test_configuration_variants_bind_source_part_and_mesh_provider_without_prefab(self) -> None:
        provider = CharacterResourceProvider("main/source.ab", (CharacterSerializedCab("CAB-source", ()),))
        mesh = IndependentNpcConfigReference("avatarMeshName", "NPC/AvatarMesh/Actor/model", "assets/config_mesh.asset", (provider,))
        templet = IndependentNpcConfigReference("avatarTempletName", "NPC/AvatarTemplet/Entity/Actor/model", "assets/config_templet.asset", (provider,))
        reference = IndependentNpcResourceReference("template", "prefab.json", "a" * 64,
            tuple(IndependentNpcPrefabReference(part, "assets/missing.prefab", ())
                  for part in ("body", "face", "hair", "ears", "tail")), mesh, templet, None)
        entry = IndependentNpcRecord("npc", None, None, "missing", "config_assembled", reference)
        catalog = IndependentNpcCatalog("EndfieldIndependentNpcCatalog/1", "a" * 64, (entry,), (),
            CharacterProviderIndexSource("provider.jsonl", "b" * 64, 1),
            IndependentNpcCatalogSummary(1, 0, 1, 0, 0, 1, 0, 0, 0))
        variants = CharacterService(None)._npc_variants(catalog)
        self.assertEqual(len(variants), 1)
        variant_id, (owner, selection) = next(iter(variants.items()))
        self.assertEqual((owner, selection.part_name, selection.resource_path, selection.logical_provider),
                         ("independent-npc-npc", "body", mesh.resource_path, provider.logical_name))
        self.assertEqual(selection.catalog_identity, catalog.source_identity)
        self.assertNotEqual(variant_id, _digest({"catalog_identity": selection.catalog_identity,
                                                  "character_id": selection.character_id,
                                                  "template_id": selection.template_id,
                                                  "part_name": selection.part_name,
                                                  "variant": selection.variant,
                                                  "resource_path": selection.resource_path,
                                                  "logical_provider": selection.logical_provider})[:32])
        changed_parts = replace(reference, prefab_parts=(*reference.prefab_parts[:-1],
            replace(reference.prefab_parts[-1], part_name="tail_alt")))
        changed_catalog = replace(catalog, entries=(replace(entry, resource_reference=changed_parts),))
        self.assertNotEqual(variant_id, next(iter(CharacterService(None)._npc_variants(changed_catalog))))
        missing = replace(entry, resource_reference=replace(reference, avatar_templet=None))
        self.assertEqual(CharacterService(None)._npc_variants(replace(catalog, entries=(missing,))), {})

    def test_configuration_catalog_exposes_one_prefab_info_group_choice(self) -> None:
        provider = CharacterResourceProvider("main/source.ab", (CharacterSerializedCab("CAB-source", ()),))
        mesh = IndependentNpcConfigReference("avatarMeshName", "NPC/AvatarMesh/Actor/model", "assets/config_mesh.asset", (provider,))
        templet = IndependentNpcConfigReference("avatarTempletName", "NPC/AvatarTemplet/Entity/Actor/model", "assets/config_templet.asset", (provider,))
        parts = tuple(IndependentNpcPrefabReference(name, "assets/missing.prefab", ())
                      for name in ("body", "face", "hair", "ears", "tail"))
        reference = IndependentNpcResourceReference("template", "prefab.json", "a" * 64, parts, mesh, templet, None)
        entry = IndependentNpcRecord("npc", "NPC", None, "resolved", "config_assembled", reference)
        provider_source = CharacterProviderIndexSource("provider.jsonl", "b" * 64, 1)
        npc_catalog = IndependentNpcCatalog("EndfieldIndependentNpcCatalog/1", "c" * 64, (entry,), (),
            provider_source, IndependentNpcCatalogSummary(1, 1, 0, 0, 0, 1, 0, 0, 0))
        character_catalog = CharacterCatalog("EndfieldCharacterCatalog/1", "d" * 64, (), (), provider_source, (),
            CharacterModelSummary(0, 0, 0, 0, 0, 0, "not_performed"))
        service = CharacterService(None)
        with patch.object(service, "_catalog_source", return_value=(None, character_catalog, npc_catalog, "identity")), \
                patch.object(service, "_runtime_defaults", side_effect=CharacterServiceError("tools unavailable")), \
                patch.object(service, "_authoring_tools", return_value={}), \
                patch.object(service, "_npc_prepare_script", return_value=None), \
                patch.object(service, "_npc_assembly_tools", return_value={}), \
                patch.object(service, "_npc_animation_tools", return_value={}):
            catalog = service.catalog()

        rows = catalog["characters"]
        self.assertIsInstance(rows, list)
        npc_row = next(row for row in rows if isinstance(row, dict) and row.get("npc_id") == "npc")
        choices = npc_row["variants"]
        self.assertEqual(len(choices), 1)
        self.assertEqual(choices[0]["label"], "PrefabInfo 全组（5 部件）")
        self.assertEqual(choices[0]["part_names"], ["body", "face", "hair", "ears", "tail"])
        self.assertEqual(npc_row["assembly_scope"], "exact_prefab_info_slot_group_lod0")

    def test_source_hash_preserves_material_name_case_and_requires_unique_provider(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            provider = Path(directory) / "providers.jsonl"
            paths = ("Assets/Beyond/Arts/Entity/NPC/Models/Body.asset", "Assets/Beyond/Arts/Entity/NPC/Materials/M_Body.mat")
            rows = [{"kind": "match", "logical_name": f"main/{index}.ab", "containers": [path],
                     "serialized": [{"name": f"CAB-{index}", "externals": []}]} for index, path in enumerate(paths)]
            provider.write_text("\n".join(json.dumps(row) for row in [*rows, {"kind": "complete", "failed": 0}]), encoding="utf-8")
            index = {struct.pack("<Q", value): (path,) for value, path in zip((1, 2), paths, strict=True)}
            renderers = ({"meshPathHash": 1, "materialPathHashes": [2], "isRendererDisabled": 0},)
            resolved = resolve_npc_assets(index, renderers, provider, lambda: None)
            self.assertEqual(resolved[2].resource_path, paths[1])
            self.assertEqual(resolved[2].provider.logical_name, "main/1.ab")
            ambiguous = {**index, struct.pack("<Q", 1): (paths[0], paths[1])}
            with self.assertRaisesRegex(NpcAssemblyPreparationError, "one source path"):
                resolve_npc_assets(ambiguous, renderers, provider, lambda: None)
            duplicate = {**rows[0], "logical_name": "main/duplicate.ab"}
            provider.write_text("\n".join(json.dumps(row) for row in [*rows, duplicate, {"kind": "complete", "failed": 0}]), encoding="utf-8")
            with self.assertRaisesRegex(NpcAssemblyPreparationError, "provider is not unique"):
                resolve_npc_assets(index, renderers, provider, lambda: None)

    def test_missing_material_is_permitted_only_for_explicitly_disabled_renderer(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = "assets/beyond/body.asset"
            provider = Path(directory) / "providers.jsonl"
            provider.write_text(json.dumps({"kind": "match", "logical_name": "main/mesh.ab", "containers": [path],
                "serialized": [{"name": "CAB-Mesh", "externals": []}]}) + "\n" + json.dumps({"kind": "complete", "failed": 0}), encoding="utf-8")
            index = {struct.pack("<Q", 1): (path,)}
            row = {"meshPathHash": 1, "materialPathHashes": [2], "isRendererDisabled": 1}
            self.assertEqual(set(resolve_npc_assets(index, (row,), provider, lambda: None)), {1})
            with self.assertRaisesRegex(NpcAssemblyPreparationError, "one source path"):
                resolve_npc_assets(index, ({**row, "isRendererDisabled": 0},), provider, lambda: None)

    def test_exact_slot_selection_rejects_self_avatar_and_attachments(self) -> None:
        slot = {"name": "part", "bUseSelfAvatar": 0, "parentBoneTransformName": "", "parentBoneName": "",
                "selfAvatarGuid": {"val0": 0, "val1": 0, "val2": 0, "val3": 0},
                "partSubMeshsLOD0": [{"meshName": "source", "meshGuid": {"val0": 0, "val1": 0, "val2": 0, "val3": 0}}]}
        source = NpcSourceObject(Path("unused"), {"avatarSlotMeshDatas": [slot]}, {})
        self.assertEqual(selected_npc_renderers(source, "part"), (slot["partSubMeshsLOD0"][0],))
        for changed in ({**slot, "bUseSelfAvatar": 1}, {**slot, "parentBoneName": "hand"}):
            with self.assertRaises(NpcAssemblyPreparationError):
                selected_npc_renderers(replace(source, value={"avatarSlotMeshDatas": [changed]}), "part")
        with self.assertRaisesRegex(NpcAssemblyPreparationError, "one exact"):
            selected_npc_renderers(source, "different")

    def test_slot_group_retains_source_attachment_and_requires_exact_self_avatar_guid(self) -> None:
        zeros = {"val0": 0, "val1": 0, "val2": 0, "val3": 0}
        guid = {"val0": 1, "val1": 2, "val2": 3, "val3": 4}
        head_path = "Root/Bip001/Bip001_Pelvis/Bip001_Spine/Bip001_Neck/Bip001_Head"
        body = {"name": "body", "bUseSelfAvatar": 0, "parentBoneTransformName": "", "parentBoneName": "",
            "selfAvatarGuid": zeros, "partSubMeshsLOD0": [{"meshName": "body", "meshGuid": zeros}]}
        ear = {"name": "ear", "bUseSelfAvatar": 1, "parentBoneTransformName": head_path,
            "parentBoneName": "Bip001_Head", "selfAvatarGuid": guid,
            "partSubMeshsLOD0": [{"meshName": "ear", "meshGuid": guid}]}
        alternative = {**ear, "name": "ear_alt"}
        source = NpcSourceObject(Path("unused"), {"avatarSlotMeshDatas": [body, ear, alternative]}, {})

        slots = selected_npc_slots(source, ("body", "ear"))
        self.assertEqual(tuple(slot.name for slot in slots), ("body", "ear"))
        self.assertEqual(tuple(slot.index for slot in slots), (0, 1))
        self.assertFalse(slots[0].use_self_avatar)
        self.assertTrue(slots[1].use_self_avatar)
        self.assertEqual(slots[1].parent_bone_transform_name, head_path)
        self.assertEqual(slots[1].self_avatar_guid, (1, 2, 3, 4))
        with self.assertRaisesRegex(NpcAssemblyPreparationError, "GUID differs"):
            selected_npc_slots(replace(source, value={"avatarSlotMeshDatas": [body, {
                **ear, "partSubMeshsLOD0": [{"meshName": "ear", "meshGuid": zeros}]
            }, alternative]}), ("body", "ear"))

    def test_partial_source_json_is_rejected_before_assembly(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            metadata = {"byteSize": 4, "rawDataLength": 4, "rawDataSha256": "a" * 64}
            path = root / "object.json"
            path.write_text(json.dumps({"$animestudio": metadata, "field": {"$partial": True}}), encoding="utf-8")
            with self.assertRaisesRegex(NpcAssemblyPreparationError, "Partial"):
                read_npc_source_objects(root)
            path.write_text(json.dumps({"$animestudio": metadata, "field": 1}), encoding="utf-8")
            self.assertEqual(len(read_npc_source_objects(root)), 1)

    def test_npc_families_include_existing_common_and_selected_model_without_generic_guess(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            database = root / "resources.sqlite"
            common = "assets/beyond/arts/entity/actor/strong/common/animations/"
            own = "assets/beyond/arts/entity/npc/major/strong/ruanyi/animations/"
            with closing(sqlite3.connect(database)) as connection, connection:
                connection.execute("CREATE TABLE paths(path TEXT,path_folded TEXT)")
                connection.executemany("INSERT INTO paths VALUES(?,?)", [(prefix + "clip.fbx", prefix + "clip.fbx") for prefix in (common, own)])
            context = CharacterSourceContext(root / "game", root / "blender", root / "cache", root / "output", root / "provider", database, root / "cab")
            reference = IndependentNpcResourceReference("template", "prefab.json", "a" * 64, (), None, None,
                "NPC/AnimationConfig/Humanoid/Strong/strong")
            npc = IndependentNpcRecord("ruanyi", None, None, "missing", "config_assembled", reference)
            model = "Assets/Beyond/Arts/Entity/NPC/Major/Strong/Ruanyi/Models/SK_ruanyi.fbx"
            self.assertEqual(resolve_npc_animation_prefixes(context, npc, (model,), lambda: None), tuple(sorted((common, own))))
            generic = replace(npc, resource_reference=replace(reference, cpu_animation_template="NPC/AnimationConfig/Generic/unknown"))
            self.assertEqual(resolve_npc_animation_prefixes(context, generic, (), lambda: None), ())
            self.assertEqual(resolve_npc_animation_prefixes(context, generic, (model,), lambda: None), (own,))


if __name__ == "__main__":
    unittest.main()
