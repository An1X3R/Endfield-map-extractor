from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
import hashlib
from pathlib import Path
import sqlite3
import struct
import tempfile
import threading
import unittest

from extractor_core import vfs
from extractor_core.resources import BootstrapExtractionError, extract_candidate_bundles


def write_vfs_source(root: Path) -> tuple[Path, Path, dict[str, bytes]]:
    """Write actual encrypted BLC/CHK inputs, including Unicode-folded bundle identities."""
    game_vfs = root / "VFS"
    group_hash = "7064D8E2"
    group_dir = game_vfs / group_hash
    group_dir.mkdir(parents=True)
    chunk_name = "00112233445566778899AABBCCDDEEFF"
    names = ("MAIN/aB.ab", "main/straße.ab", "main/İ.ab", "main/unselected.ab")
    payloads = {name: hashlib.sha256(name.encode()).digest() * 512 for name in names}
    key = bytes.fromhex(vfs.DEFAULT_VFS_KEY_HEX)
    version = 1
    chunk = bytearray()
    records = bytearray()
    rows = []
    for index, (name, payload) in enumerate(payloads.items()):
        virtual_path = f"Data/Bundles/Windows/source{index}.ab"
        encoded_path = virtual_path.encode("utf-8")
        offset = len(chunk)
        nonce_seed = index + 10
        cipher = vfs.make_cipher(key, struct.pack("<Iq", version, nonce_seed))
        chunk.extend(cipher.encrypt(payload))
        records.extend(struct.pack("<h", len(encoded_path)) + encoded_path)
        records.extend(bytes(8) + bytes.fromhex(chunk_name) + hashlib.md5(payload).digest())
        records.extend(struct.pack("<qqBBqI", offset, len(payload), 11, 1, nonce_seed, 0))
        rows.append((name, group_hash, virtual_path))
    header = struct.pack("<Iq", version, 42)
    body = struct.pack("<IIh", 4, 0, 6) + b"Bundle"
    body += bytes.fromhex(group_hash) + bytes(4)
    body += struct.pack("<iqBi", len(rows), len(chunk), 11, 1)
    body += bytes.fromhex(chunk_name) + hashlib.md5(chunk).digest()
    body += struct.pack("<qBii", len(chunk), 11, 0, len(rows)) + records
    (group_dir / (group_hash + ".blc")).write_bytes(header + vfs.make_cipher(key, header).encrypt(body))
    (group_dir / (chunk_name + ".chk")).write_bytes(chunk)
    database = root / "resources.sqlite"
    with closing(sqlite3.connect(database)) as connection, connection:
        connection.execute("CREATE TABLE bundles(logical_name TEXT PRIMARY KEY,group_hash TEXT,virtual_path TEXT)")
        connection.executemany("INSERT INTO bundles VALUES(?,?,?)", rows)
    return database, game_vfs, payloads


class CandidateBundleSelectionTests(unittest.TestCase):
    def test_real_encrypted_sources_preserve_unicode_casefold_and_order(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            database, game_vfs, payloads = write_vfs_source(root)
            names = ["MAIN/STRASSE.ab", "main/AB.ab", "MAIN/i\u0307.ab", "main/ab.ab"]
            original_names = list(names)
            progress_names: list[str] = []
            progress_values: list[float] = []
            main_thread = threading.get_ident()

            def progress(phase: str, value: float, message: str, details: dict[str, object] | None) -> None:
                self.assertEqual(main_thread, threading.get_ident())
                self.assertEqual("scene_candidate_bundles", phase)
                self.assertIsNotNone(details)
                assert details is not None
                progress_names.append(str(details["logicalName"]))
                progress_values.append(value)

            result = extract_candidate_bundles(database, game_vfs, names, root / "output", progress=progress, cancelled=None)
            self.assertEqual(original_names, names)
            self.assertEqual({"format": "EndfieldCandidateBundleSet/1", "requested_count": "3",
                              "extracted_count": "3", "bytes": str(3 * 16384)}, result)
            self.assertEqual(["MAIN/aB.ab", "main/straße.ab", "main/İ.ab"], progress_names)
            self.assertEqual([1 / 3, 2 / 3, 1], progress_values)
            for name in progress_names:
                self.assertEqual(payloads[name], (root / "output" / name).read_bytes())
            self.assertFalse((root / "output" / "main/unselected.ab").exists())

    def test_cancel_stops_after_acknowledged_bundle_and_fresh_output_can_resume(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            database, game_vfs, payloads = write_vfs_source(root)
            stop = threading.Event()

            def progress(phase: str, value: float, message: str, details: dict[str, object] | None) -> None:
                stop.set()

            with self.assertRaisesRegex(BootstrapExtractionError, "first-run extraction was cancelled"):
                extract_candidate_bundles(database, game_vfs, list(payloads), root / "cancelled",
                                          progress=progress, cancelled=stop.is_set)
            outputs = [path for path in (root / "cancelled").rglob("*") if path.is_file()]
            self.assertEqual(1, len(outputs))
            self.assertEqual(payloads["MAIN/aB.ab"], outputs[0].read_bytes())
            with self.assertRaises(FileExistsError):
                extract_candidate_bundles(database, game_vfs, list(payloads), root / "cancelled", progress=None, cancelled=None)
            result = extract_candidate_bundles(database, game_vfs, list(payloads), root / "resumed", progress=None, cancelled=None)
            self.assertEqual("4", result["extracted_count"])

    def test_missing_and_corrupt_source_errors_remain_explicit(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            database, game_vfs, payloads = write_vfs_source(root)
            with self.assertRaisesRegex(BootstrapExtractionError, "absent.*resource index: main/missing.ab"):
                extract_candidate_bundles(database, game_vfs, ["MAIN/MISSING.ab"], root / "missing", progress=None, cancelled=None)
            chunk_path = next(game_vfs.rglob("*.chk"))
            original = chunk_path.read_bytes()
            chunk_path.write_bytes(bytes([original[0] ^ 1]) + original[1:])
            with self.assertRaisesRegex(BootstrapExtractionError, "Candidate bundle checksum mismatch for MAIN/aB.ab"):
                extract_candidate_bundles(database, game_vfs, ["main/ab.ab"], root / "corrupt", progress=None, cancelled=None)
            self.assertEqual(4, len(payloads))

    def test_concurrent_invocations_do_not_share_selection_state(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            database, game_vfs, payloads = write_vfs_source(root)

            def extract(name: str) -> dict[str, str]:
                return extract_candidate_bundles(database, game_vfs, [name], root / (name + "_output"),
                                                 progress=None, cancelled=None)

            names = ["MAIN/aB.ab", "main/straße.ab"]
            with ThreadPoolExecutor(max_workers=2) as executor:
                results = list(executor.map(extract, names))
            self.assertEqual(["1", "1"], [result["extracted_count"] for result in results])
            for name in names:
                self.assertEqual(payloads[name], (root / (name + "_output") / name).read_bytes())

    def test_distinct_database_identities_with_same_fold_remain_selected(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            database, game_vfs, payloads = write_vfs_source(root)
            with closing(sqlite3.connect(database)) as connection, connection:
                connection.execute("UPDATE bundles SET logical_name=? WHERE logical_name=?",
                                   ("main/STRASSE.ab", "main/unselected.ab"))
            result = extract_candidate_bundles(database, game_vfs, ["MAIN/STRASSE.ab"], root / "output",
                                              progress=None, cancelled=None)
            self.assertEqual("1", result["requested_count"])
            self.assertEqual("2", result["extracted_count"])
            self.assertEqual(payloads["main/straße.ab"], (root / "output/main/straße.ab").read_bytes())
            self.assertEqual(payloads["main/unselected.ab"], (root / "output/main/STRASSE.ab").read_bytes())
