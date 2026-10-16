"""Verify optional duplicate IDs without weakening authoritative source bindings."""
import unittest

from endfield_character_catalog import CharacterCatalogError, _validate_prefab_character_binding


class PrefabCharacterBindingTests(unittest.TestCase):
    def test_missing_duplicate_and_matching_duplicate_are_valid(self) -> None:
        for source in ({"id": "template"}, {"correspondingCharId": "character"}):
            _validate_prefab_character_binding(source, "character", "PrefabInfo/template.json")

    def test_present_invalid_or_conflicting_duplicate_is_rejected(self) -> None:
        for value in (None, "", 42, "another-character"):
            with self.subTest(value=value), self.assertRaises(CharacterCatalogError):
                _validate_prefab_character_binding({"correspondingCharId": value}, "character", "PrefabInfo/template.json")
