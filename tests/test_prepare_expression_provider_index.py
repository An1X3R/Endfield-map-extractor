"""Keep expression discovery compatible with scanner metadata without weakening completeness."""
from __future__ import annotations

import hashlib
import json
from pathlib import Path
import sys
import tempfile
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from character_runtime.prepare_expression_source import JsonValue, scan_provider_index


def _index(path: Path, metadata: tuple[dict[str, JsonValue], ...]) -> bytes:
    provider: dict[str, JsonValue] = {
        "kind": "match", "logical_name": "main/source.ab", "containers": [
            "assets/data/skeletalmorph/skeletalmorphcfg/source.asset"],
        "serialized": [{"name": "CAB-source", "externals": ["CAB-avatar"]}],
    }
    payload = "".join(json.dumps(row) + "\n" for row in (provider, *metadata)).encode("utf-8")
    path.write_bytes(payload)
    return payload


class ExpressionProviderIndexTests(unittest.TestCase):
    def test_scanner_footer_and_progress_do_not_require_timestamp(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "providers.jsonl"
            for timestamp in ({}, {"utc": "2026-10-20T00:00:00Z"}):
                with self.subTest(timestamp=timestamp):
                    payload = _index(path, (
                        {"kind": "progress", "scanned": 2, "failed": 0, "matches": 1, **timestamp},
                        {"kind": "complete", "scanned": 2, "failed": 0, "matches": 1, **timestamp},
                    ))
                    result = scan_provider_index(path, "CAB-avatar")
                    self.assertTrue(result.complete)
                    self.assertEqual((result.provider_rows, result.progress_rows), (1, 1))
                    self.assertEqual(len(result.candidates), 1)
                    self.assertEqual(result.sha256, hashlib.sha256(payload).hexdigest())

    def test_supplied_timestamp_still_requires_nonempty_string(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "providers.jsonl"
            for kind in ("progress", "complete"):
                for timestamp in (None, "", 123, True):
                    with self.subTest(kind=kind, timestamp=timestamp):
                        _index(path, ({"kind": kind, "scanned": 2, "failed": 0,
                                       "matches": 1, "utc": timestamp},))
                        with self.assertRaisesRegex(TypeError, r"line 2\.utc"):
                            scan_provider_index(path, "CAB-avatar")

    def test_missing_or_failed_or_mismatched_footer_is_not_complete(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "providers.jsonl"
            cases: tuple[tuple[dict[str, JsonValue], ...], ...] = (
                (),
                ({"kind": "complete", "scanned": 2, "failed": 1, "matches": 1},),
                ({"kind": "complete", "scanned": 2, "failed": 0, "matches": 2},),
            )
            for metadata in cases:
                with self.subTest(metadata=metadata):
                    _index(path, metadata)
                    self.assertFalse(scan_provider_index(path, "CAB-avatar").complete)

    def test_footer_counters_and_terminal_position_remain_strict(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "providers.jsonl"
            for missing in ("scanned", "failed", "matches"):
                footer: dict[str, JsonValue] = {"kind": "complete", "scanned": 2, "failed": 0, "matches": 1}
                del footer[missing]
                _index(path, (footer,))
                with self.subTest(missing=missing), self.assertRaises(TypeError):
                    scan_provider_index(path, "CAB-avatar")
            footer = {"kind": "complete", "scanned": 2, "failed": 0, "matches": 1}
            _index(path, (footer, footer))
            with self.assertRaisesRegex(ValueError, "after its completion footer"):
                scan_provider_index(path, "CAB-avatar")


if __name__ == "__main__":
    unittest.main()
