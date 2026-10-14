"""Validate integrity and persistence for expression bake cache entries."""
from __future__ import annotations

import hashlib
from pathlib import Path
import struct
import sys
import tempfile
import unittest
import zlib

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'src' / 'character_runtime'))
from character_expression_bake_cache import (CachedController, CachedExpressionBake, CachedShapeKey,
    CACHE_MAX_HEADER_BYTES, expression_cache_key, read_expression_cache, write_expression_cache)


class CharacterExpressionBakeCacheTests(unittest.TestCase):
    def test_round_trip_accepts_empty_vertex_group_and_missing_entry_is_a_miss(self) -> None:
        raw_coordinates = struct.pack('<3f', 1.0, 2.0, 3.0)
        entry = CachedExpressionBake(
            {'format': 'EndfieldExpressionTest/1'},
            (CachedShapeKey('mesh', 'Smile', 'Basis', 1, zlib.compress(raw_coordinates),
                            hashlib.sha256(raw_coordinates).hexdigest(), '["Smile"]',
                            0.0, 0.0, 1.0, '', 'KEY_LINEAR', False),),
            (CachedController('controller', 'EndfieldBasicExpressionControls/1',
                              (('Smile', 0.0),), 'PLAIN_AXES', 0.15),),
        )
        key = expression_cache_key({'geometry': 'source-a', 'options': {'tolerance': 2e-6}})
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            self.assertIsNone(read_expression_cache(root, key))
            write_expression_cache(root, key, entry)
            self.assertEqual(entry, read_expression_cache(root, key))
            self.assertTrue((root / 'character_expression_bakes' / f'{key}.efc').exists())
            self.assertEqual(tuple((root / 'character_expression_bakes').glob('*.lock')), ())

    def test_equivalent_same_key_publication_ignores_rebased_receipt_metadata(self) -> None:
        raw_coordinates = struct.pack('<3f', 1.0, 2.0, 3.0)
        shape = CachedShapeKey('mesh', 'Smile', 'Basis', 1, zlib.compress(raw_coordinates),
                               hashlib.sha256(raw_coordinates).hexdigest(), None,
                               0.0, 0.0, 1.0, '', 'KEY_LINEAR', False)
        first = CachedExpressionBake(
            {'format': 'EndfieldExpressionTest/1',
             'source_table': {'file': 'first.json', 'sha256': 'a' * 64, 'bytes': 10}},
            (shape,), (),
        )
        second = CachedExpressionBake(
            {'format': 'EndfieldExpressionTest/1',
             'source_table': {'file': 'second.json', 'sha256': 'b' * 64, 'bytes': 20}},
            (shape,), (),
        )
        key = expression_cache_key({'geometry': 'source-a'})
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            write_expression_cache(root, key, first)
            write_expression_cache(root, key, second)
            cached = read_expression_cache(root, key)
            self.assertIsNotNone(cached)
            self.assertEqual(cached.receipt['source_table']['file'], 'first.json')

    def test_input_changes_invalidate_key_and_corrupt_entry_is_left_untouched(self) -> None:
        self.assertNotEqual(expression_cache_key({'geometry': 'source-a'}),
                            expression_cache_key({'geometry': 'source-b'}))
        raw_coordinates = struct.pack('<3f', 1.0, 2.0, 3.0)
        entry = CachedExpressionBake(
            {'format': 'EndfieldExpressionTest/1'},
            (CachedShapeKey('mesh', 'Smile', 'Basis', 1, zlib.compress(raw_coordinates),
                            hashlib.sha256(raw_coordinates).hexdigest(), None,
                            0.0, 0.0, 1.0, '', 'KEY_LINEAR', False),),
            (),
        )
        key = expression_cache_key({'geometry': 'source-a'})
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            write_expression_cache(root, key, entry)
            path = root / 'character_expression_bakes' / f'{key}.efc'
            original = path.read_bytes()
            damaged = original[:-1]
            path.write_bytes(damaged)
            with self.assertRaisesRegex(ValueError, 'corrupt and was left untouched'):
                read_expression_cache(root, key)
            self.assertEqual(path.read_bytes(), damaged)

    def test_oversized_header_fails_before_creating_cache_file(self) -> None:
        entry = CachedExpressionBake({'payload': 'x' * CACHE_MAX_HEADER_BYTES}, (), ())
        key = expression_cache_key({'geometry': 'source-a'})
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            with self.assertRaisesRegex(ValueError, 'metadata exceeds the supported size'):
                write_expression_cache(root, key, entry)
            self.assertFalse((root / 'character_expression_bakes' / f'{key}.efc').exists())


if __name__ == '__main__':
    unittest.main()
