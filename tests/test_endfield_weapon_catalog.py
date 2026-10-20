"""Check exact weapon joins and unresolved directory entries without external I/O."""
from __future__ import annotations

from pathlib import Path
import sys
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'src'))

from endfield_character_catalog import CharacterResourceProvider
from endfield_weapon_catalog import WeaponCatalogError, assemble_weapon_records, resolve_weapon_model
from extractor_core.scene_tables import TableValue


def basic_row(weapon_id: str, model: str) -> dict[str, TableValue]:
    return {'weaponId': weapon_id, 'modelPath': model, 'rarity': 6, 'weaponType': 1}


def item_row(weapon_id: str, text_id: int) -> dict[str, TableValue]:
    return {'id': weapon_id, 'name': {'id': text_id, 'text': ''}}


class WeaponCatalogTests(unittest.TestCase):
    def test_names_preserve_ascii_and_duplicate_ids(self) -> None:
        basic: dict[str, TableValue] = {key: basic_row(key, '') for key in ('first', 'second', 'third')}
        items: dict[str, TableValue] = {key: item_row(key, identifier) for key, identifier in (('first', 1), ('second', 1), ('third', 2))}
        entries = assemble_weapon_records(basic, items, {'1': '\u5b8f\u613f', '2': 'J.E.T.'}, (), {})
        self.assertEqual([row.weapon_id for row in entries], ['first', 'second', 'third'])
        self.assertEqual([row.name for row in entries], ['\u5b8f\u613f', '\u5b8f\u613f', 'J.E.T.'])
        self.assertTrue(all(row.model_status == 'missing_resource' for row in entries))

    def test_unique_exact_binding_and_unverified_refined_candidate(self) -> None:
        path = 'Assets/Generated/Gameplay/Weapons/sample.prefab'
        refined = 'Assets/Generated/Gameplay/Weapons/sample_refined.prefab'
        provider = CharacterResourceProvider('main/sample.ab', ())
        entries = assemble_weapon_records({'sample': basic_row('sample', 'Gameplay/Weapons/sample.prefab')},
            {'sample': item_row('sample', 1)}, {'1': 'Sample'}, (path, refined),
            {path.casefold(): (provider,), refined.casefold(): (provider,)})
        self.assertEqual(resolve_weapon_model(entries, 'sample').resource_path, path)
        self.assertEqual(entries[0].refined_candidates[0].semantic_status, 'unverified_candidate')
        self.assertEqual(entries[0].geometry_status, 'not_verified')

    def test_missing_and_ambiguous_providers_remain_catalog_rows(self) -> None:
        path = 'Assets/Gameplay/Weapons/sample.prefab'
        first = CharacterResourceProvider('main/first.ab', ())
        second = CharacterResourceProvider('main/second.ab', ())
        for providers, expected in (({}, 'missing_provider'), ({path.casefold(): (first, second)}, 'ambiguous_provider')):
            with self.subTest(expected=expected):
                entries = assemble_weapon_records({'sample': basic_row('sample', 'Gameplay/Weapons/sample.prefab')},
                    {}, {}, (path,), providers)
                self.assertEqual(len(entries), 1)
                self.assertEqual(entries[0].model_status, expected)
                self.assertEqual(entries[0].name_status, 'missing_item')
                with self.assertRaises(WeaponCatalogError):
                    resolve_weapon_model(entries, 'sample')

    def test_ambiguous_resource_suffix_remains_unresolved(self) -> None:
        entries = assemble_weapon_records({'sample': basic_row('sample', 'Gameplay/Weapons/sample.prefab')},
            {}, {}, ('Assets/A/Gameplay/Weapons/sample.prefab', 'Assets/B/Gameplay/Weapons/sample.prefab'), {})
        self.assertEqual(entries[0].model_status, 'ambiguous_resource')
        self.assertEqual(len(entries[0].resources), 2)

    def test_conflicting_ids_and_invalid_types_fail(self) -> None:
        with self.assertRaises(WeaponCatalogError):
            assemble_weapon_records({'sample': basic_row('different', '')}, {}, {}, (), {})
        with self.assertRaises(WeaponCatalogError):
            assemble_weapon_records({'sample': basic_row('sample', '')}, {'sample': item_row('different', 1)}, {}, (), {})
        row = basic_row('sample', '')
        row['rarity'] = True
        with self.assertRaises(WeaponCatalogError):
            assemble_weapon_records({'sample': row}, {}, {}, (), {})
        with self.assertRaises(WeaponCatalogError):
            assemble_weapon_records({'sample': basic_row('sample', '../sample.prefab')}, {}, {}, (), {})


if __name__ == '__main__':
    unittest.main()
