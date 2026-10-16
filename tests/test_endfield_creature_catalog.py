"""Verify exact environmental identities and primary mesh taxonomy."""
from __future__ import annotations

from copy import deepcopy
from dataclasses import replace
from pathlib import Path
import unittest

from extractor_core.scene_tables import TableValue
from endfield_character_catalog import CharacterResourceProvider, CharacterSerializedCab
from endfield_character_enemy_index import EnemyModelBinding, EnemyModelIndexError, enemy_index_from_catalog
from endfield_character_npc_catalog import (
    IndependentNpcCatalogError,
    IndependentNpcConfigReference,
    IndependentNpcRecord,
    IndependentNpcPrefabReference,
    IndependentNpcResourceReference,
    _npc_info_with_environmental_templates,
    _resolve_name,
    _resolve_enemy_name,
    _enemy_template_ids,
    _npc_info_with_monster_prefabs,
    _NpcPrefabSource,
    _cache_name_source,
    _enemy_direct_prefab_records,
    _resolve_enemy_prefab_name,
    npc_entity_category,
)


class CreatureCatalogTests(unittest.TestCase):
    def test_catalog_receipt_accepts_memory_tuples_and_json_arrays(self) -> None:
        source = {"source_name": "EnemyTemplateTable", "virtual_path": "source.bytes", "sha256": "a" * 64, "rows": 95}
        self.assertIsNone(enemy_index_from_catalog({"sources": (source,)}, Path("unused-cache")))
        self.assertIsNone(enemy_index_from_catalog({"sources": [source]}, Path("unused-cache")))
        with self.assertRaisesRegex(EnemyModelIndexError, "Expected enemy source array"):
            enemy_index_from_catalog({"sources": source}, Path("unused-cache"))

    def test_exact_enemy_model_join_reuses_npc_and_adds_unique_direct_prefab(self) -> None:
        bindings = (
            EnemyModelBinding("eny_new_number", "exact_remapped_model", "assets/old.asset", "CAB-data", 1, "a" * 64),
            EnemyModelBinding("eny_direct", "exact_direct_model", "assets/data.asset", "CAB-data", 2, "b" * 64),
        )
        names = {template: _resolve_enemy_name(template, {template: {"templateId": template, "name": {"id": index}}}, {},
                 {str(index): "Source " + template}) for index, template in enumerate(("eny_new_number", "eny_direct"), 1)}
        self.assertEqual(_resolve_enemy_prefab_name(("exact_remapped_model",), bindings, names), names["eny_new_number"])
        self.assertEqual(_resolve_enemy_prefab_name(("eny_new_number",), bindings, names).status, "missing")
        provider = CharacterResourceProvider("main/exact.ab", (CharacterSerializedCab("CAB-model", ()),))
        resource = "assets/beyond/dynamicassets/gameplay/actors/postmodels/enemies/exact_direct_model.prefab"
        reference = IndependentNpcResourceReference("npc_old_number", "source.json", "c" * 64,
            (), IndependentNpcConfigReference("avatarMeshName", "NPC/AvatarMesh/Monster/source", None, ()), None, None)
        reference = replace(reference, prefab_parts=(IndependentNpcPrefabReference("exact_remapped_model", "assets/npc.prefab", ()),))
        entries = (IndependentNpcRecord("existing_source_id", None, None, "missing", "unresolved", reference),)
        paths = {"exact_remapped_model": (), "exact_direct_model": (resource,)}
        added = _enemy_direct_prefab_records(entries, bindings, names, paths, {resource: (provider,)})
        self.assertEqual(len(added), 1)
        self.assertEqual((added[0].npc_id, added[0].source_kind), ("enemy-eny_direct", "prefab"))
        self.assertEqual(added[0].resource_reference.prefab_parts[0].resource_path, resource)
        self.assertEqual(npc_entity_category(added[0]).source_field, "enemyModelPrefab")
        self.assertEqual(npc_entity_category(added[0]).category, "creature")
        with self.assertRaisesRegex(IndependentNpcCatalogError, "multiple exact prefabs"):
            _enemy_direct_prefab_records(entries, bindings, names, {**paths, "exact_direct_model": (resource, resource + "other")}, {resource: (provider,)})
        with self.assertRaisesRegex(IndependentNpcCatalogError, "one exact provider/CAB"):
            _enemy_direct_prefab_records(entries, bindings, names, paths, {})

    def test_enemy_combat_variants_collapse_to_declared_source_templates(self) -> None:
        templates: dict[str, TableValue] = {"eny_source": {"templateId": "eny_source"}}
        enemies: dict[str, TableValue] = {
            "eny_source": {"enemyId": "eny_source", "templateId": "eny_source", "modelId": ""},
            "eny_activity": {"enemyId": "eny_activity", "templateId": "eny_source", "modelId": "exact_alt_model"},
        }
        self.assertEqual(_enemy_template_ids(templates, enemies), ("eny_source",))
        with self.assertRaisesRegex(IndependentNpcCatalogError, "undeclared template"):
            _enemy_template_ids({}, enemies)

    def test_enemy_names_use_authoritative_display_ids_and_preserve_missing_or_ambiguous(self) -> None:
        template_display: dict[str, TableValue] = {"eny_source": {"templateId": "eny_source", "name": {"id": 10}}}
        variant_display: dict[str, TableValue] = {
            "eny_variant": {"templateId": "eny_source", "name": {"id": 11}},
            "eny_other": {"templateId": "eny_source", "name": {"id": 12}},
        }
        localized: dict[str, TableValue] = {"10": "Canonical source name", "11": "Variant name", "12": "Other name"}
        name = _resolve_enemy_name("eny_source", template_display, variant_display, localized)
        self.assertEqual((name.value, name.status), ("Canonical source name", "resolved"))
        self.assertIsNotNone(name.source)
        assert name.source is not None
        self.assertEqual((name.source.table, name.source.row_key, name.source.text_id), ("EnemyTemplateDisplayInfoTable", "eny_source", "10"))
        self.assertEqual(_cache_name_source({"table": name.source.table, "row_key": name.source.row_key, "field": name.source.field, "text_key": None, "text_id": name.source.text_id}, "enemy"), name.source)
        self.assertEqual(_resolve_enemy_name("eny_source", {}, variant_display, localized).status, "ambiguous")
        self.assertEqual(_resolve_enemy_name("eny_missing", template_display, variant_display, localized).status, "missing")

    def test_missing_monster_prefabs_deduplicate_exact_templates_without_guessing_enemy_ids(self) -> None:
        prefabs = {
            "npc_remapped": _NpcPrefabSource("source.json", "a" * 64, ("eny_new_number_postmodel",), "NPC/AvatarMesh/Monster/source", "NPC/AvatarTemplet/Entity/Monster/source", None),
            "npc_animal": _NpcPrefabSource("animal.json", "b" * 64, ("animal_model",), "NPC/AvatarMesh/Animal/source", None, None),
        }
        merged = _npc_info_with_monster_prefabs({}, prefabs, set())
        self.assertEqual(merged, {"enemy-npc-npc_remapped": {"npcId": "enemy-npc-npc_remapped", "templateId": "npc_remapped"}})
        existing: dict[str, TableValue] = {"source_id": {"npcId": "source_id", "templateId": "npc_remapped"}}
        self.assertEqual(_npc_info_with_monster_prefabs(existing, prefabs, set()), existing)
        self.assertEqual(_npc_info_with_monster_prefabs({}, prefabs, {"npc_remapped"}), {})
    def test_environment_templates_preserve_ids_and_exclude_existing_and_playable_sources(self) -> None:
        existing: dict[str, TableValue] = {"commonrabbit": {"npcId": "commonrabbit", "templateId": "rabbit"}}
        environmental: dict[str, TableValue] = {
            "rabbit": {"dataKey": "rabbit", "extra": "ignored"},
            "slug": {"dataKey": "slug"},
            "playable": {"dataKey": "playable"},
        }
        original = deepcopy(existing)
        merged = _npc_info_with_environmental_templates(existing, environmental, {"playable"})
        self.assertEqual(existing, original)
        self.assertEqual(merged, {
            **existing, "environmental-slug": {"npcId": "environmental-slug", "templateId": "slug"},
        })
        self.assertEqual(_resolve_name("environmental-slug", "slug", {}, {}, {}, {}).status, "missing")

    def test_environmental_identity_collision_and_malformed_source_fail(self) -> None:
        collision: dict[str, TableValue] = {
            "environmental-slug": {"npcId": "environmental-slug", "templateId": "other"},
        }
        with self.assertRaisesRegex(IndependentNpcCatalogError, "collides"):
            _npc_info_with_environmental_templates(collision, {"slug": {"dataKey": "slug"}}, set())
        with self.assertRaisesRegex(IndependentNpcCatalogError, "differs from dataKey"):
            _npc_info_with_environmental_templates({}, {"slug": {"dataKey": "rabbit"}}, set())
        with self.assertRaisesRegex(IndependentNpcCatalogError, "Unsafe NPC resource"):
            _npc_info_with_environmental_templates({}, {"../slug": {"dataKey": "../slug"}}, set())

    def test_primary_mesh_family_controls_category_and_retains_exact_evidence(self) -> None:
        for family, expected in (("Animal", "animal"), ("Monster", "creature"), ("Object", "object"), ("Actor", "npc")):
            mesh = IndependentNpcConfigReference("avatarMeshName", f"NPC/AvatarMesh/{family}/source", None, ())
            templet = IndependentNpcConfigReference("avatarTempletName", "NPC/AvatarTemplet/Entity/Animal/source", None, ())
            reference = IndependentNpcResourceReference("template", "prefab.json", None, (), mesh, templet, None)
            entry = IndependentNpcRecord("npc", None, None, "missing", "unresolved", reference)
            category = npc_entity_category(entry)
            self.assertEqual(category.category, expected)
            self.assertEqual((category.source_field, category.source_value), ("avatarMeshName", mesh.source_value))
        missing = IndependentNpcResourceReference("template", "prefab.json", None, (), None, templet, None)
        evidence = npc_entity_category(IndependentNpcRecord("npc", None, None, "missing", "unresolved", missing))
        self.assertEqual((evidence.category, evidence.source_field, evidence.source_value), ("npc", None, None))


if __name__ == "__main__":
    unittest.main()
