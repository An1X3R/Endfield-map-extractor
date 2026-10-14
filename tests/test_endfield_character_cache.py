from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from endfield_character_cache import (
    CharacterDataCache,
    CharacterSourceChangedError,
    character_bundle_closure,
)
from endfield_character_catalog import (
    CharacterCatalog,
    CharacterIdentity,
    CharacterModelSummary,
    CharacterProviderIndexSource,
    read_character_catalog_cached,
)
from extractor_core.resources import read_bundle_closure_names


def _never_cancelled() -> None:
    return None


class CharacterDataCacheTests(unittest.TestCase):
    def test_source_fingerprint_reuses_hash_and_invalidates_full_stat_identity(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source = root / "resource.db"
            source.write_bytes(b"first content")
            cache = CharacterDataCache(root / "cache")
            original_open = Path.open
            source_reads = 0

            def counted_open(path: Path, *args: object, **kwargs: object):
                nonlocal source_reads
                if path.resolve() == source.resolve() and args and args[0] == "rb":
                    source_reads += 1
                return original_open(path, *args, **kwargs)

            with patch.object(Path, "open", counted_open):
                first = cache.fingerprint_file(source, "database/1", _never_cancelled)
                second = cache.fingerprint_file(source, "database/1", _never_cancelled)
                self.assertEqual(first, second)
                self.assertEqual(source_reads, 1)
                source.write_bytes(b"updated content")
                third = cache.fingerprint_file(source, "database/1", _never_cancelled)
            self.assertNotEqual(first.sha256, third.sha256)
            self.assertEqual(third.sha256, hashlib.sha256(b"updated content").hexdigest())
            self.assertNotEqual(first.stamp, third.stamp)
            self.assertEqual(source_reads, 2)

    def test_derived_json_reuses_content_and_rejects_mutation_during_build(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source = root / "index.jsonl"
            source.write_text("first", encoding="utf-8")
            cache = CharacterDataCache(root / "cache")
            built = 0

            def build() -> object:
                nonlocal built
                built += 1
                return {"value": built}

            first, first_hit = cache.get_or_build_json(
                "test_index", "index/1", (source,), (), build, _never_cancelled,
            )
            second, second_hit = cache.get_or_build_json(
                "test_index", "index/1", (source,), (), build, _never_cancelled,
            )
            self.assertEqual((first, first_hit), ({"value": 1}, False))
            self.assertEqual((second, second_hit), ({"value": 1}, True))
            source.write_text("second", encoding="utf-8")
            third, third_hit = cache.get_or_build_json(
                "test_index", "index/1", (source,), (), build, _never_cancelled,
            )
            self.assertEqual((third, third_hit), ({"value": 2}, False))

            def mutate_source() -> object:
                source.write_text("changed during read", encoding="utf-8")
                return {"value": 3}

            with self.assertRaises(CharacterSourceChangedError):
                cache.get_or_build_json(
                    "changing_index", "index/1", (source,), (), mutate_source, _never_cancelled,
                )

    def test_cached_cab_closure_matches_shared_resource_resolver_and_reuses_jsonl_parse(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            inventory = root / "cab_inventory.jsonl"
            records = (
                {"kind": "cab", "logical_name": "characters/root.ab", "serialized": [
                    {"name": "root.cab", "externals": ["shared/dep.cab", "missing.cab"]},
                ]},
                {"kind": "cab", "logical_name": "shared/dependency.ab", "serialized": [
                    {"name": "dep.cab", "externals": []},
                ]},
                {"kind": "complete", "failed": 0},
            )
            inventory.write_text("".join(json.dumps(row) + "\n" for row in records), encoding="utf-8")
            cache = CharacterDataCache(root / "cache")
            original_open = Path.open
            source_reads = 0

            def counted_open(path: Path, *args: object, **kwargs: object):
                nonlocal source_reads
                if path.resolve() == inventory.resolve() and args and args[0] in ("rb", "r"):
                    source_reads += 1
                return original_open(path, *args, **kwargs)

            with patch.object(Path, "open", counted_open):
                index, fingerprint = cache.read_character_cab_index(inventory, _never_cancelled)
                cached_closure = character_bundle_closure(index, {"character": ["characters/root.ab"]})
                same_index, same_fingerprint = cache.read_character_cab_index(inventory, _never_cancelled)
            expected = read_bundle_closure_names({"character": ["characters/root.ab"]}, inventory)
            self.assertEqual(cached_closure, expected)
            self.assertEqual(same_index, index)
            self.assertEqual(same_fingerprint, fingerprint)
            self.assertEqual(source_reads, 2)

    def test_catalog_cache_includes_game_header_chunks_and_provider_source_stamp(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            game = root / "game"
            vfs_root = game / "Endfield_Data" / "StreamingAssets" / "VFS"
            (game / "Endfield.exe").parent.mkdir(parents=True)
            (game / "Endfield.exe").write_bytes(b"exe")
            groups = ("42A8FCA6", "775A31D1")
            for group_hash in groups:
                group = vfs_root / group_hash
                group.mkdir(parents=True)
                (group / f"{group_hash}.blc").write_bytes(f"header-{group_hash}".encode())
                (group / "data.chk").write_bytes(f"chunk-{group_hash}".encode())
            provider = root / "providers.jsonl"
            provider.write_text('{"kind":"complete","failed":0}\n', encoding="utf-8")
            provider_digest = hashlib.sha256(provider.read_bytes()).hexdigest()
            cache = CharacterDataCache(root / "outputs" / ".endfield_character_cache")
            calls = 0

            def build_catalog(_vfs_root: Path, provider_path: Path, _cancelled: object) -> CharacterCatalog:
                nonlocal calls
                calls += 1
                return CharacterCatalog(
                    "EndfieldCharacterCatalog/2",
                    "a" * 64,
                    (CharacterIdentity("char_1", "Name", "Name", "1"),),
                    (),
                    CharacterProviderIndexSource(str(provider_path), provider_digest, provider.stat().st_size),
                    (),
                    CharacterModelSummary(0, 1, 0, 0, 0, 0, "not_performed"),
                )

            with patch("endfield_character_catalog._read_character_catalog_sources", build_catalog):
                first = read_character_catalog_cached(game, provider, cache, _never_cancelled)
                second = read_character_catalog_cached(game, provider, cache, _never_cancelled)
                self.assertEqual(first, second)
                self.assertEqual(calls, 1)
                chunk = vfs_root / groups[1] / "data.chk"
                chunk.write_bytes(b"changed chunk")
                third = read_character_catalog_cached(game, provider, cache, _never_cancelled)
            self.assertEqual(calls, 2)
            self.assertEqual(first.source_identity, third.source_identity)


if __name__ == "__main__":
    unittest.main()
