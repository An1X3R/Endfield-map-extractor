"""Check persisted inventories against real encrypted BLC/CHK and path-index fixtures."""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
import hashlib
import json
import os
from pathlib import Path
import struct
import tempfile
import unittest

from endfield_scene_io import SceneInputChangedError, _estimate_cached_value, scene_io_session
from extractor_core import vfs
from extractor_core.dynamic_renderer_cache import (
    DynamicRendererCacheCorruptError, _BOUNDS, _HEADER, _LENGTH, _RECORD, _STRING_COUNT, _identity,
    read_dynamic_scene_renderers_cached,
)
from extractor_core.dynamic_renderers import DynamicRendererDecodeError, DynamicSceneRendererInventory, read_dynamic_scene_renderers_for_scene
from extractor_core.effects import GROUP_HASH, EffectDecodeError, _dynamic_inventory_sources
from tests.test_dynamic_scene_inventory import _write_path_hash
from tests.test_extractor_core_instances import chunk_with_components


_MAIN = "Data/DynamicStreaming/PC/Scene/map01/fb_main_0_0_0.bytes"
_INIT = "Data/DynamicStreaming/PC/Scene/map01/fb_init_0_7.bytes"
_MATERIAL = bytes.fromhex("0011223344556677")
_MESH = bytes.fromhex("8899aabbccddeeff")


def _check_cancelled() -> None:
    return None


def _main() -> bytes:
    data = bytearray(256)
    struct.pack_into("<I", data, 0, 32)
    struct.pack_into("<HH4H", data, 8, 12, 12, 0, 0, 0, 8)
    struct.pack_into("<i", data, 32, 24)
    struct.pack_into("<I", data, 40, 24)
    struct.pack_into("<II", data, 64, 1, 44)
    struct.pack_into("<HH10H", data, 80, 24, 12, *(0 for _ in range(9)), 8)
    struct.pack_into("<i", data, 112, 32)
    struct.pack_into("<I", data, 120, 40)
    struct.pack_into("<I", data, 160, 2)
    for index, x in enumerate((0.0, 100000.0)):
        struct.pack_into("<ii5fii", data, 164 + index * 36, 7, 0, x, 0, 0, 1, 2, -1, 0)
    return bytes(data)


def _init() -> bytes:
    common = bytearray(256)
    struct.pack_into("<16f", common, 0, 1, 0, 0, 0, 0, 1, 0, 0, 0, 0, 1, 0, 100000, 0, 0, 1)
    struct.pack_into("<20f", common, 176, *(float(index) for index in range(20)))
    references = struct.pack("<Q", 1) + _MATERIAL + _MESH + bytes(24)
    runtime = struct.pack("<IHBbIIIIIff", 1, 0, 3, -1, 0, 0, 0, 17, 0, 200.0, 10.0)
    bounds = tuple((index, struct.pack("<f", number)) for index, number in enumerate((100000, 0, 0, 1, 2, 3)))
    decoded = chunk_with_components((*bounds, (21, b"fixture renderer" + bytes(48)),
                                     (18, bytes(common)), (68, references), (8, runtime)))
    token = next(value for value in range(256) if
                 ((((value & 3) | ((value >> 2) & 12)) << 4) | ((value >> 2) & 3) | ((value >> 4) & 12)) == 0xF0)
    remaining = len(decoded) - 15
    extension = bytes((255,)) * (remaining // 255) + bytes((remaining % 255,))
    return struct.pack("<I", len(decoded)) + bytes((token,)) + extension + decoded


def _write_vfs(root: Path, rows: tuple[tuple[str, bytes], ...]) -> tuple[Path, Path, Path]:
    game = root / "game"
    group = game / GROUP_HASH
    group.mkdir(parents=True, exist_ok=True)
    name = "00112233445566778899AABBCCDDEEFF"
    key = bytes.fromhex(vfs.DEFAULT_VFS_KEY_HEX)
    chunk = bytearray()
    records = bytearray()
    for index, (path, payload) in enumerate(rows):
        encoded = path.encode("utf-8")
        offset, seed = len(chunk), index + 10
        chunk.extend(vfs.make_cipher(key, struct.pack("<Iq", 1, seed)).encrypt(payload))
        records.extend(struct.pack("<h", len(encoded)) + encoded + bytes(8) + bytes.fromhex(name) + hashlib.md5(payload).digest())
        records.extend(struct.pack("<qqBBqI", offset, len(payload), 11, 1, seed, 0))
    header = struct.pack("<Iq", 1, 42)
    body = struct.pack("<IIh", 4, 0, 7) + b"Dynamic" + bytes.fromhex(GROUP_HASH) + bytes(4)
    body += struct.pack("<iqBi", len(rows), len(chunk), 11, 1)
    body += bytes.fromhex(name) + hashlib.md5(chunk).digest() + struct.pack("<qBii", len(chunk), 11, 0, len(rows)) + records
    (group / f"{GROUP_HASH}.blc").write_bytes(header + vfs.make_cipher(key, header).encrypt(body))
    (group / f"{name}.chk").write_bytes(chunk)
    metadata = root / "metadata"
    metadata.mkdir(exist_ok=True)
    path_hash = metadata / "StringPathHash.bin"
    _write_path_hash(path_hash, ((_MATERIAL, "assets/example.mat"), (_MESH, "packages/example.asset")))
    return game, path_hash, root / "cache"


def _read(game: Path, path_hash: Path, cache: Path) -> DynamicSceneRendererInventory:
    return read_dynamic_scene_renderers_cached(game, path_hash, "map01", cache, _check_cancelled)


def _mutate_frame(path: Path, section: int, field: str, value: object) -> None:
    data = path.read_bytes()
    _magic, metadata_length, *counts = _HEADER.unpack_from(data)
    cursor = _HEADER.size + metadata_length
    frames: list[dict[str, object]] = []
    for _ in range(sum(counts[:3])):
        length = _LENGTH.unpack_from(data, cursor)[0]
        frames.append(json.loads(data[cursor + 4:cursor + 4 + length]))
        cursor += 4 + length
    frames[sum(counts[:section])][field] = value
    body = bytearray(data[:_HEADER.size + metadata_length])
    for row in frames:
        raw = json.dumps(row, separators=(",", ":"), allow_nan=True).encode()
        body.extend(_LENGTH.pack(len(raw)) + raw)
    body.extend(data[cursor:-32])
    path.write_bytes(body + hashlib.sha256(body).digest())


def _record_start(data: bytes) -> int:
    _magic, metadata_length, *counts = _HEADER.unpack_from(data)
    cursor = _HEADER.size + metadata_length
    for _ in range(sum(counts[:3])):
        cursor += _LENGTH.size + _LENGTH.unpack_from(data, cursor)[0]
    count = _STRING_COUNT.unpack_from(data, cursor)[0]
    cursor += _STRING_COUNT.size
    for _ in range(count):
        cursor += _LENGTH.size + _LENGTH.unpack_from(data, cursor)[0]
    return cursor


def _mutate_binary(path: Path, offset: int, raw: bytes) -> None:
    data = path.read_bytes()
    start = _record_start(data) + offset
    body = data[:start] + raw + data[start + len(raw):-32]
    path.write_bytes(body + hashlib.sha256(body).digest())


class PersistentDynamicRendererCacheTests(unittest.TestCase):
    def test_real_roundtrip_preserves_all_records_and_aliasing(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            game, path_hash, cache = _write_vfs(Path(directory), ((_MAIN, _main()), (_INIT, _init())))
            original = read_dynamic_scene_renderers_for_scene(game, path_hash, "map01", _check_cancelled)
            cold, warm = _read(game, path_hash, cache), _read(game, path_hash, cache)
            self.assertEqual(original, cold)
            self.assertEqual(original, warm)
            shared_root_cache = path_hash.parent / "dynamic_renderers"
            self.assertEqual(original, _read(game, path_hash, shared_root_cache))
            self.assertEqual(original, _read(game, path_hash, shared_root_cache))
            self.assertEqual(2, len(warm.references))
            self.assertEqual(100000.0, warm.records[0].matrix[12])
            self.assertEqual(100000.0, warm.references[1].center[0])
            self.assertIs(warm.records[0].reference_keys, warm.groups[0].reference_keys)
            self.assertIs(warm.records[0].source_virtual_path, warm.sources[1].virtual_path)
            self.assertIs(warm.groups[0].reference_keys[0], warm.references[0].key)
            self.assertLessEqual(_estimate_cached_value(warm, set()), _estimate_cached_value(original, set()) * 1.03)
            with scene_io_session() as session:
                first, second = _read(game, path_hash, cache), _read(game, path_hash, cache)
                self.assertIs(first, second)
                self.assertIs(first, read_dynamic_scene_renderers_for_scene(game, path_hash, "map01", _check_cancelled))
                self.assertEqual(1, session.source_cache_misses)
                self.assertEqual(2, session.source_cache_hits)

    def test_real_ambiguity_errors_and_source_mutations_are_not_hidden(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            game, path_hash, cache = _write_vfs(root, ((_MAIN, _main()), (_INIT, _init())))
            _read(game, path_hash, cache)
            _write_path_hash(path_hash, ((_MATERIAL, "assets/example.mat"), (_MESH, "packages/example.asset"), (_MESH, "assets/other.mesh")))
            with self.assertRaisesRegex(EffectDecodeError, "ambiguousCount=1"):
                _read(game, path_hash, cache)
            game, path_hash, cache = _write_vfs(root, ((_MAIN, _main()), (_INIT, _init()),
                                                       (_INIT.replace("init_0", "init_1"), _init())))
            with self.assertRaisesRegex(DynamicRendererDecodeError, "Ambiguous referenced"):
                _read(game, path_hash, cache)

    def test_version_and_membership_change_before_publish_fail(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            game, path_hash, cache = _write_vfs(Path(directory), ((_MAIN, _main()), (_INIT, _init())))
            calls = 0

            def changing_source() -> None:
                nonlocal calls
                calls += 1
                if calls == 6:
                    (game / GROUP_HASH / "new.chk").write_bytes(b"changed group membership")

            with self.assertRaisesRegex(SceneInputChangedError, "membership changed"):
                read_dynamic_scene_renderers_cached(game, path_hash, "map01", cache, changing_source)
            self.assertEqual([], list(cache.rglob("*.bin")))
            self.assertEqual([], list(cache.rglob("*.tmp")))

    def test_corrupt_checksum_required_fields_vectors_and_semantics_fail(self) -> None:
        mutations = ((2, "status", "invented"), (2, "blob_offset", 0), (0, "data_md5", "invalid"),
                     (2, "descriptors", [[18, 0, 0]]))
        for section, field, value in mutations:
            with self.subTest(field=field), tempfile.TemporaryDirectory() as directory:
                game, path_hash, cache = _write_vfs(Path(directory), ((_MAIN, _main()), (_INIT, _init())))
                _read(game, path_hash, cache)
                path, = cache.rglob("*.bin")
                _mutate_frame(path, section, field, value)
                with self.assertRaises(DynamicRendererCacheCorruptError):
                    _read(game, path_hash, cache)
        mutations_binary = ((0, struct.pack("<I", 1000)), (4, b"\x02"), (5, b"\xff"),
                            (6, struct.pack("<f", float("nan"))), (70, struct.pack("<f", float("inf"))),
                            (_RECORD.size, struct.pack("<d", float("nan"))),
                            (_RECORD.size + _BOUNDS.size, struct.pack("<I", 1000)),
                            (_RECORD.size + _BOUNDS.size + 72, struct.pack("<ff", 0, 10)))
        for offset, raw in mutations_binary:
            with self.subTest(offset=offset), tempfile.TemporaryDirectory() as directory:
                game, path_hash, cache = _write_vfs(Path(directory), ((_MAIN, _main()), (_INIT, _init())))
                _read(game, path_hash, cache)
                path, = cache.rglob("*.bin")
                _mutate_binary(path, offset, raw)
                with self.assertRaises(DynamicRendererCacheCorruptError):
                    _read(game, path_hash, cache)
        with tempfile.TemporaryDirectory() as directory:
            game, path_hash, cache = _write_vfs(Path(directory), ((_MAIN, _main()), (_INIT, _init())))
            _read(game, path_hash, cache)
            path, = cache.rglob("*.bin")
            data = path.read_bytes()
            path.write_bytes(data[:-1] + bytes((data[-1] ^ 1,)))
            with self.assertRaisesRegex(DynamicRendererCacheCorruptError, "SHA256 differs"):
                _read(game, path_hash, cache)

    def test_cache_directory_and_cancelled_request_fail_before_writes(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            game, path_hash, cache = _write_vfs(Path(directory), ((_MAIN, _main()), (_INIT, _init())))
            for forbidden in (game / "cache", path_hash / "cache"):
                with self.assertRaisesRegex(ValueError, "read-only"):
                    _read(game, path_hash, forbidden)
                self.assertFalse(forbidden.exists())

            def cancelled() -> None:
                raise InterruptedError("cancelled")

            with self.assertRaisesRegex(InterruptedError, "cancelled"):
                read_dynamic_scene_renderers_cached(game, path_hash, "map01", cache, cancelled)
            self.assertFalse(cache.exists())

    def test_exact_identity_and_concurrent_atomic_publication(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            game, path_hash, cache = _write_vfs(Path(directory), ((_MAIN, _main()), (_INIT, _init())))
            sources = _dynamic_inventory_sources(game, path_hash)
            first = _identity(cache, "map01", sources)
            with ThreadPoolExecutor(max_workers=2) as executor:
                inventories = tuple(executor.map(lambda _: _read(game, path_hash, cache), range(2)))
            self.assertEqual(inventories[0], inventories[1])
            self.assertEqual(1, len(list(cache.rglob("*.bin"))))
            self.assertEqual([], list(cache.rglob("*.tmp")))
            group = game / GROUP_HASH
            old, = group.glob("*.chk")
            original_time = old.stat().st_mtime_ns
            raw = old.read_bytes()
            old.write_bytes(raw[:-1] + bytes((raw[-1] ^ 1,)))
            os.utime(old, ns=(old.stat().st_atime_ns, original_time))
            self.assertNotEqual(first.metadata, _identity(cache, "map01", sources).metadata)


if __name__ == "__main__":
    unittest.main()
