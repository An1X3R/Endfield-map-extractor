"""Verify ordered range merging and real native scanner cancellation/failure."""
from __future__ import annotations

import json
from collections.abc import Callable
from itertools import islice
import os
from pathlib import Path
import subprocess
import sys
import tempfile
from threading import Lock
import time
import unittest

from endfield_scene_discovery import observe_helper
from endfield_scene_export import _run_observed_scene_process
from endfield_scene_scanner import Emit, ScannerCapacity, ScanShard, _merge, run_scanner_shards, scan_shards
from tests.test_endfield_scene_export import ProcessCancelled, process_is_running


def no_cancel() -> None:
    pass


class ScannerMergeTests(unittest.TestCase):
    def test_empty_scan_completes_without_starting_a_helper(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            index = root / "empty.tsv"
            index.write_text("logical_name\tchunk_path\toffset\tlength\n", encoding="utf-8")
            run_scanner_shards(Path(sys.executable), index, root / "work", root / "console.log", 0, 0,
                               ScannerCapacity(1, 1024**3, 1024**3, 8 * 1024**3),
                               ("assets/",), no_cancel, lambda row: None, observe_helper)
            complete = json.loads((root / "work" / "bundle_candidates.jsonl").read_text(encoding="utf-8"))
            self.assertEqual({"kind": "complete", "scanned": 0, "failed": 0, "matches": 0}, complete)
            self.assertEqual([], json.loads((root / "work" / "performance.json").read_text())["shards"])

    def test_container_prefixes_are_passed_as_or_filters(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            index = root / "index.tsv"
            index.write_text("logical_name\tchunk_path\toffset\tlength\nrow\tchunk\t0\t1\n", encoding="utf-8")
            commands: list[list[str]] = []

            def observe(command: list[str], log: Path, _temporary: Path,
                        check: Callable[[], None], _emit: Emit) -> None:
                commands.append(command)
                log.write_text("stub scanner\n", encoding="utf-8")
                scanned = int(command[command.index("--limit") + 1])
                complete = json.dumps({"kind": "complete", "scanned": scanned, "failed": 0, "matches": 0}) + "\n"
                Path(command[command.index("--output") + 1]).write_text(complete, encoding="utf-8")
                Path(command[command.index("--cab-output") + 1]).write_text(complete, encoding="utf-8")
                report = {"format": "EndfieldHelperPerformanceReport/1", "status": "complete",
                          "phaseTimingsSeconds": {}}
                Path(command[command.index("--performance-report") + 1]).write_text(json.dumps(report), encoding="utf-8")
                check()

            run_scanner_shards(Path(sys.executable), index, root / "work", root / "console.log", 0, 1,
                               ScannerCapacity(1, 1024**3, 1024**3, 8 * 1024**3),
                               ("assets/", "packages/"), no_cancel, lambda row: None, observe)

            command = commands[0]
            prefixes = [command[index + 1] for index, value in enumerate(command) if value == "--contains"]
            self.assertEqual(["assets/", "packages/"], prefixes)

    def test_absolute_ranges_cover_remainder_without_gaps(self) -> None:
        self.assertEqual((ScanShard(13, 15), ScanShard(15, 18), ScanShard(18, 21), ScanShard(21, 24)),
                         scan_shards(13, 24, 4))
        self.assertEqual((ScanShard(8, 9), ScanShard(9, 10)), scan_shards(8, 10, 4))
        self.assertEqual((), scan_shards(0, 0, 2))

    def test_incomplete_error_and_absolute_count_mismatch_never_merge(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source = root / "part.jsonl"
            for rows in (
                [{"kind": "match"}],
                [{"kind": "complete", "scanned": 2, "failed": 0, "matches": 0}],
                [{"kind": "error", "message": "broken bundle"}, {"kind": "complete", "scanned": 12, "failed": 1, "matches": 0}],
                [{"kind": "complete", "scanned": 12, "failed": 0, "matches": 0}, {"kind": "match"}],
            ):
                source.write_text("".join(json.dumps(row) + "\n" for row in rows), encoding="utf-8")
                with self.assertRaises(ValueError):
                    _merge((source,), (ScanShard(10, 12),), root / "merged.jsonl", no_cancel)
                self.assertFalse((root / "merged.jsonl").exists())


@unittest.skipUnless(os.environ.get("ENDFIELD_TEST_SCANNER") and os.environ.get("ENDFIELD_TEST_SCAN_INDEX"),
                     "Set ENDFIELD_TEST_SCANNER and ENDFIELD_TEST_SCAN_INDEX for real game-bundle integration")
class NativeScannerTests(unittest.TestCase):
    def test_two_process_output_matches_serial_payload_order(self) -> None:
        scanner = Path(os.environ["ENDFIELD_TEST_SCANNER"])
        index = Path(os.environ["ENDFIELD_TEST_SCAN_INDEX"])
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            outputs = []
            for workers in (1, 2):
                work = root / str(workers)
                run_scanner_shards(scanner, index, work, root / f"{workers}.log", 0, 128,
                                   ScannerCapacity(workers, 2 * 1024**3, 1024**3, 8 * 1024**3),
                                   ("assets/",), no_cancel, lambda row: None, observe_helper)
                outputs.append(tuple([json.loads(line) for line in (work / name).read_text(encoding="utf-8").splitlines()]
                                     for name in ("bundle_candidates.jsonl", "bundle_cab_inventory.jsonl")))
            self.assertEqual(outputs[0], outputs[1])
            with self.assertRaises(FileExistsError):
                run_scanner_shards(scanner, index, root / "2", root / "2.log", 0, 128,
                                   ScannerCapacity(2, 2 * 1024**3, 1024**3, 8 * 1024**3),
                                   ("assets/",), no_cancel, lambda row: None, observe_helper)

    def test_cancellation_stops_both_real_children_and_publishes_nothing(self) -> None:
        scanner = Path(os.environ["ENDFIELD_TEST_SCANNER"])
        index = Path(os.environ["ENDFIELD_TEST_SCAN_INDEX"])
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            children: set[int] = set()
            lock = Lock()
            deadline = time.monotonic() + 10

            def observe(command: list[str], log: Path, directory: Path,
                        check: Callable[[], None], emit: Emit) -> None:
                directory.mkdir(parents=True)

                def owned(process: subprocess.Popen[bytes]) -> None:
                    with lock:
                        children.add(process.pid)

                _run_observed_scene_process(command, log, dict(os.environ), check, owned)

            def cancelled() -> None:
                with lock:
                    ready = len(children) == 2
                if ready:
                    raise ProcessCancelled("cancel two owned scanners")
                if time.monotonic() > deadline:
                    raise TimeoutError("Two scanner children did not start within 10 seconds")

            with self.assertRaises(ProcessCancelled):
                run_scanner_shards(scanner, index, root / "work", root / "console.log", 0, 2048,
                                   ScannerCapacity(2, 2 * 1024**3, 1024**3, 8 * 1024**3), ("assets/",), cancelled,
                                   lambda row: None, observe)
            self.assertEqual(2, len(children))
            self.assertTrue(all(not process_is_running(pid) for pid in children))
            self.assertFalse((root / "work" / "bundle_candidates.jsonl").exists())
            self.assertFalse((root / "work" / "performance.json").exists())

    def test_native_exit_failure_stops_sibling_and_cannot_publish(self) -> None:
        scanner = Path(os.environ["ENDFIELD_TEST_SCANNER"])
        index = Path(os.environ["ENDFIELD_TEST_SCAN_INDEX"])
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            with index.open(encoding="utf-8-sig") as stream:
                rows = [line.rstrip("\r\n") for line in islice(stream, 2049)]
            rows[1] = "invalid first index row"
            broken = root / "broken.tsv"
            broken.write_text("\n".join(rows) + "\n", encoding="utf-8")
            with self.assertRaisesRegex(RuntimeError, "exited with code 1"):
                run_scanner_shards(scanner, broken, root / "work", root / "console.log", 0, 2048,
                                   ScannerCapacity(2, 2 * 1024**3, 1024**3, 8 * 1024**3), ("assets/",), no_cancel,
                                   lambda row: None, observe_helper)
            self.assertFalse((root / "work" / "bundle_candidates.jsonl").exists())
            self.assertFalse((root / "work" / "performance.json").exists())

    def test_invalid_bundle_preserves_native_error_record_and_publishes_nothing(self) -> None:
        scanner = Path(os.environ["ENDFIELD_TEST_SCANNER"])
        index = Path(os.environ["ENDFIELD_TEST_SCAN_INDEX"])
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            with index.open(encoding="utf-8-sig") as stream:
                rows = [line.rstrip("\r\n") for line in islice(stream, 2049)]
            first = rows[1].split("\t")
            first[2], first[3] = str(Path(first[1]).stat().st_size + 1), "4"
            rows[1] = "\t".join(first)
            broken = root / "invalid_bundle.tsv"
            broken.write_text("\n".join(rows) + "\n", encoding="utf-8")
            with self.assertRaisesRegex(ValueError, "source_record=.*error.*logical_name"):
                run_scanner_shards(scanner, broken, root / "work", root / "console.log", 0, 2048,
                                   ScannerCapacity(2, 2 * 1024**3, 1024**3, 8 * 1024**3), ("assets/",), no_cancel,
                                   lambda row: None, observe_helper)
            self.assertFalse((root / "work" / "bundle_candidates.jsonl").exists())
            self.assertFalse((root / "work" / "performance.json").exists())


if __name__ == "__main__":
    unittest.main()
