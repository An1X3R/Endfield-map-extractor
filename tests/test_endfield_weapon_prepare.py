"""Check exact catalog selection and portable weapon artifact path boundaries."""
from __future__ import annotations

from dataclasses import replace
import argparse
from pathlib import Path
import sys
import tempfile
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'src'))

from endfield_character_catalog import CharacterProviderIndexSource, CharacterResourceProvider, CharacterSerializedCab
from endfield_weapon_catalog import (
    WeaponCatalog, WeaponCatalogError, WeaponCatalogSummary, WeaponResourceIndexSource,
    assemble_weapon_records,
)
from endfield_weapon_prepare import (
    add_preparation_arguments, preparation_request_from_arguments,
    prepared_artifact_path, resolve_weapon_source,
)


def selection_catalog() -> WeaponCatalog:
    base = 'Assets/Gameplay/Weapons/sample.prefab'
    refined = 'Assets/Gameplay/Weapons/sample_refined.prefab'
    provider = CharacterResourceProvider('main/sample.ab', (CharacterSerializedCab('CAB-sample', ()),))
    records = assemble_weapon_records(
        {'sample': {'weaponId': 'sample', 'modelPath': 'Gameplay/Weapons/sample.prefab', 'rarity': 6, 'weaponType': 1}},
        {}, {}, (base, refined), {base.casefold(): (provider,), refined.casefold(): (provider,)})
    return WeaponCatalog('EndfieldWeaponCatalog/1', 'source_identity', (),
        CharacterProviderIndexSource('provider.jsonl', 'provider_identity', 0),
        WeaponResourceIndexSource('resources.sqlite', 'database_identity', 0), records,
        WeaponCatalogSummary(1, 0, 1, 0, 0, 1, (), ((6, 1),), ((1, 1),)))


class WeaponPreparationSelectionTests(unittest.TestCase):
    def test_shared_parser_constructs_explicit_typed_request(self) -> None:
        parser = argparse.ArgumentParser()
        add_preparation_arguments(parser)
        paths = ('game-root', 'provider-index', 'database', 'cab-inventory', 'cache-root', 'catalog',
                 'output', 'static-reader', 'shader-reader', 'visibility-reader')
        command = [argument for name in paths for argument in ('--' + name, 'source/' + name)]
        command.extend(('--weapon-id', 'sample', '--variant', 'base', '--resource-path',
                        'Assets/Gameplay/Weapons/sample.prefab', '--unity-version', '2021.3.34f5',
                        '--max-bundles', '8', '--max-compressed-bytes', '1000000'))
        request = preparation_request_from_arguments(parser.parse_args(command))
        self.assertEqual(request.game_root, Path('source/game-root'))
        self.assertEqual(request.tools.static_reader, Path('source/static-reader'))
        self.assertEqual(request.max_bundles, 8)
        self.assertEqual(request.variant, 'base')

    def test_exact_base_and_candidate_have_distinct_semantics(self) -> None:
        catalog = selection_catalog()
        base = resolve_weapon_source(catalog, 'sample', 'base', catalog.entries[0].resources[0].resource_path)
        candidate = resolve_weapon_source(catalog, 'sample', 'refined_candidate',
                                          catalog.entries[0].refined_candidates[0].resource.resource_path)
        self.assertIsNone(base.name)
        self.assertEqual(base.variant_semantics, 'source_base')
        self.assertEqual(candidate.variant_semantics, 'unverified_candidate')

    def test_variant_cannot_be_inferred_or_substituted(self) -> None:
        catalog = selection_catalog()
        for resource in ('Assets/Gameplay/Weapons/sample_refined.prefab', 'Assets/Gameplay/Weapons/other.prefab'):
            with self.subTest(resource=resource), self.assertRaises(WeaponCatalogError):
                resolve_weapon_source(catalog, 'sample', 'base', resource)
        with self.assertRaises(WeaponCatalogError):
            resolve_weapon_source(catalog, 'missing', 'base', catalog.entries[0].resources[0].resource_path)

    def test_duplicate_id_and_ambiguous_provider_are_rejected(self) -> None:
        catalog = selection_catalog()
        resource = catalog.entries[0].resources[0]
        with self.assertRaises(WeaponCatalogError):
            resolve_weapon_source(replace(catalog, entries=catalog.entries * 2), 'sample', 'base', resource.resource_path)
        ambiguous = replace(resource, provider_status='ambiguous', providers=resource.providers * 2)
        entry = replace(catalog.entries[0], model_status='ambiguous_provider', resources=(ambiguous,))
        with self.assertRaises(WeaponCatalogError):
            resolve_weapon_source(replace(catalog, entries=(entry,)), 'sample', 'base', resource.resource_path)

    def test_portable_artifact_rejects_absolute_and_parent_paths(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            payload = root / 'geometry.bin'
            payload.write_bytes(b'geometry')
            self.assertEqual(prepared_artifact_path(root, 'geometry.bin'), payload.resolve())
            for path in ('../geometry.bin', '/geometry.bin', 'C:/geometry.bin', './geometry.bin', 'a//geometry.bin'):
                with self.subTest(path=path), self.assertRaises(WeaponCatalogError):
                    prepared_artifact_path(root, path)


if __name__ == '__main__':
    unittest.main()
