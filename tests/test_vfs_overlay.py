"""Exercise real filesystem overlay precedence and source invalidation."""
from __future__ import annotations

from contextlib import closing
from pathlib import Path
import sqlite3
import tempfile
import unittest

from endfield_character_sources import game_source_stamps, verify_game_source
from endfield_character_cache import CharacterSourceChangedError
from extractor_core.resources import write_bundle_scan_index
from extractor_core.vfs_overlay import resolve_vfs_file, vfs_group_sources


def no_cancel() -> None:
    return None


class VfsOverlayTests(unittest.TestCase):
    def test_scan_index_uses_update_and_base_chunks(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            game = Path(temporary)
            base = game / 'Endfield_Data/StreamingAssets/VFS/12345678'
            hot = game / 'Endfield_Data/Persistent/VFS/12345678'
            base.mkdir(parents=True)
            hot.mkdir(parents=True)
            for path in (base / '12345678.blc', hot / '12345678.blc',
                         base / 'base.chk', base / 'hot.chk', hot / 'hot.chk'):
                path.write_bytes(b'input')
            vfs = base.parent
            self.assertEqual(hot / '12345678.blc', resolve_vfs_file(vfs, '12345678', '12345678.blc'))
            self.assertEqual((hot / '12345678.blc', base / 'base.chk', hot / 'hot.chk'),
                             vfs_group_sources(vfs, '12345678'))
            database = game / 'index.sqlite'
            with closing(sqlite3.connect(database)) as connection:
                connection.execute('CREATE TABLE bundles (logical_name,group_hash,chunk_name,chunk_offset,byte_length)')
                connection.executemany('INSERT INTO bundles VALUES (?,?,?,?,?)',
                                       [('main/base.ab', '12345678', 'base', 0, 5),
                                        ('main/hot.ab', '12345678', 'hot', 0, 5)])
                connection.commit()
            output = game / 'scan.tsv'
            write_bundle_scan_index(database, vfs, output)
            rows = output.read_text(encoding='utf-8').splitlines()
            self.assertIn(str(base / 'base.chk'), rows[1])
            self.assertIn(str(hot / 'hot.chk'), rows[2])
            (hot / 'missing.chk').write_bytes(b'')
            with self.assertRaises(FileNotFoundError):
                resolve_vfs_file(vfs, '12345678', 'absent.chk')

    def test_hot_update_addition_and_change_invalidate_generation(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            game = Path(temporary)
            (game / 'Endfield.exe').write_bytes(b'input')
            base = game / 'Endfield_Data/StreamingAssets/VFS/12345678'
            base.mkdir(parents=True)
            (base / '12345678.blc').write_bytes(b'base')
            before = game_source_stamps(game)
            hot = game / 'Endfield_Data/Persistent/VFS/12345678'
            hot.mkdir(parents=True)
            catalog = hot / '12345678.blc'
            catalog.write_bytes(b'hot')
            with self.assertRaises(CharacterSourceChangedError):
                verify_game_source(game, before, no_cancel)
            before = game_source_stamps(game)
            catalog.write_bytes(b'new generation')
            with self.assertRaises(CharacterSourceChangedError):
                verify_game_source(game, before, no_cancel)


if __name__ == '__main__':
    unittest.main()
