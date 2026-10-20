"""Validate strict managed-reference selection without a game installation."""
from __future__ import annotations

from copy import deepcopy
from pathlib import Path
import sys
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'src'))
from character_runtime.character_potential_source import display_effects


def configuration() -> dict[str, object]:
    return {'data': {'_keys': ['character'], '_values': [{'rid': 7}]}, 'references': {'RefIds': [
        {'rid': 7, 'type': {'class': 'CharacterDisplayData', 'ns': 'Beyond.Gameplay', 'asm': 'Gameplay.Beyond'},
         'data': {'charId': 'character', 'exactTypeTreeDecoded': True, 'potentialEffectConfig': {'potentialEffects': [
             {'name': 'AuthoredEffect', 'mountPoint': 'ExactSlot', 'followScale': 0, 'followRotation': 1,
              'offset': {'x': 0., 'y': 0., 'z': 0.}, 'unused': 'removed'}]}}}]}}


class PotentialSourceTests(unittest.TestCase):
    def test_exact_record_preserves_authored_flags(self) -> None:
        document = configuration()
        before = deepcopy(document)
        effect, = display_effects(document, 'character')
        self.assertEqual((effect.name, effect.mount_point, effect.follow_rotation, effect.follow_scale),
                         ('AuthoredEffect', 'ExactSlot', 1, 0))
        self.assertEqual(document, before)

    def test_missing_character_does_not_borrow_another_record(self) -> None:
        with self.assertRaisesRegex(ValueError, 'record is absent'):
            display_effects(configuration(), 'other')

    def test_null_record_has_no_configured_effects(self) -> None:
        document = configuration()
        document['data'] = {'_keys': ['character'], '_values': [{'rid': -2}]}
        self.assertEqual(display_effects(document, 'character'), ())

    def test_duplicate_keys_are_rejected(self) -> None:
        document = configuration()
        document['data'] = {'_keys': ['character', 'character'], '_values': [{'rid': 7}, {'rid': 7}]}
        with self.assertRaisesRegex(ValueError, 'ambiguous'):
            display_effects(document, 'character')

    def test_unverified_type_tree_is_rejected(self) -> None:
        document = configuration()
        document['references'] = {'RefIds': [{'rid': 7,
            'type': {'class': 'CharacterDisplayData', 'ns': 'Beyond.Gameplay', 'asm': 'Gameplay.Beyond'},
            'data': {'charId': 'character', 'exactTypeTreeDecoded': False}}]}
        with self.assertRaisesRegex(ValueError, 'exact type-tree evidence'):
            display_effects(document, 'character')


if __name__ == '__main__':
    unittest.main()
