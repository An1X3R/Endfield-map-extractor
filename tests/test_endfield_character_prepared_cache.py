"""Exercise real filesystem publication, private copies and invalidation."""
from pathlib import Path
from contextlib import closing
import json
import sqlite3
import tempfile
import unittest

from endfield_character_cache import CharacterDataCache, CharacterSourceChangedError, source_stamp
from endfield_character_prepared_cache import PreparedCacheIdentity, new_prepared_root, prepared_identity, publish_prepared, restore_prepared


def no_cancel() -> None:
    return None


class PreparedCacheTests(unittest.TestCase):
    def test_exact_vfs_tool_and_options_invalidate_reuse(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            game = root / 'game'
            group = game / 'Endfield_Data/StreamingAssets/VFS/group'
            group.mkdir(parents=True)
            chunk = group / 'chunk.chk'
            chunk.write_bytes(b'source')
            (group / 'group.blc').write_bytes(b'catalog')
            inventory = root / 'cab.jsonl'
            inventory.write_text(json.dumps({'kind': 'cab', 'logical_name': 'main/selected.ab',
                'serialized': [{'name': 'CAB-selected', 'externals': []}]}) + '\n', encoding='utf-8')
            database = root / 'resources.sqlite'
            with closing(sqlite3.connect(database)) as connection, connection:
                connection.execute('CREATE TABLE bundles(logical_name TEXT,group_hash TEXT,chunk_name TEXT)')
                connection.execute('INSERT INTO bundles VALUES(?,?,?)', ('main/selected.ab', 'group', 'chunk'))
            tool = root / 'reader'
            tool.write_bytes(b'reader')
            cache = CharacterDataCache(root / 'cache')
            def identity(options: tuple[str, ...]) -> PreparedCacheIdentity:
                return prepared_identity(cache, game, database, inventory, 'main/selected.ab', (tool,), options, no_cancel)
            first = identity(('unity-version',))
            self.assertEqual(first.key, identity(('unity-version',)).key)
            self.assertNotEqual(first.key, identity(('changed-option',)).key)
            tool.write_bytes(b'new tool')
            second = identity(('unity-version',))
            self.assertNotEqual(first.key, second.key)
            chunk.write_bytes(b'update')
            self.assertNotEqual(second.key, identity(('unity-version',)).key)

    def test_private_copies_corruption_and_source_change(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / 'source'
            source.write_bytes(b'original')
            identity = PreparedCacheIdentity('selected', (source,), (source_stamp(source),))
            cache = CharacterDataCache(root / 'cache')
            prepared = new_prepared_root(cache)
            prepared.mkdir()
            (prepared / 'mesh').write_bytes(b'mesh')
            publish_prepared(cache, identity, prepared, root / 'first', no_cancel)
            restored = restore_prepared(cache, identity, root / 'second', no_cancel)
            self.assertEqual(restored, prepared)
            (root / 'second/mesh').write_bytes(b'private edit')
            self.assertEqual((prepared / 'mesh').read_bytes(), b'mesh')
            source.write_bytes(b'changed')
            with self.assertRaises(CharacterSourceChangedError):
                restore_prepared(cache, identity, root / 'third', no_cancel)
            source.write_bytes(b'original')
            identity = PreparedCacheIdentity('selected', (source,), (source_stamp(source),))
            (prepared / 'mesh').write_bytes(b'corrupt')
            with self.assertRaisesRegex(ValueError, 'artifact changed'):
                restore_prepared(cache, identity, root / 'fourth', no_cancel)
            receipt = cache.cache_root / 'prepared/selected.json'
            record = json.loads(receipt.read_text(encoding='utf-8'))
            record['files'] = [{'path': 'mesh', 'bytes': 4}]
            receipt.write_text(json.dumps(record), encoding='utf-8')
            with self.assertRaisesRegex(ValueError, 'size/hash'):
                restore_prepared(cache, identity, root / 'fifth', no_cancel)

    def test_interrupted_preparation_never_publishes(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / 'source'
            source.write_bytes(b'original')
            identity = PreparedCacheIdentity('selected', (source,), (source_stamp(source),))
            cache = CharacterDataCache(root / 'cache')
            prepared = new_prepared_root(cache)
            prepared.mkdir()
            (prepared / 'mesh').write_bytes(b'mesh')
            def cancelled() -> None:
                raise InterruptedError('Selected job cancelled')
            with self.assertRaises(InterruptedError):
                publish_prepared(cache, identity, prepared, root / 'first', cancelled)
            self.assertIsNone(restore_prepared(cache, identity, root / 'second', no_cancel))

    def test_independent_publishers_keep_provenance_roots_alive(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / 'source'
            source.write_bytes(b'original')
            identity = PreparedCacheIdentity('selected', (source,), (source_stamp(source),))
            cache = CharacterDataCache(root / 'cache')
            for name in ('first', 'second'):
                prepared = new_prepared_root(cache)
                prepared.mkdir()
                (prepared / 'mesh').write_bytes(b'mesh')
                publish_prepared(cache, identity, prepared, root / name, no_cancel)
                self.assertTrue(prepared.is_dir())
            self.assertIsNotNone(restore_prepared(cache, identity, root / 'third', no_cancel))
