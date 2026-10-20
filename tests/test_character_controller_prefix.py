"""Verify explicit inline and pooled mask-name encodings without offset heuristics."""
from __future__ import annotations

from pathlib import Path
import struct
import sys
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from character_runtime.resolve_character_controller import ControllerSourceError, decode_controller_hash


def _prefix(mask: bytes, count: int, controller: bytes) -> bytes:
    return b"\x0f\xff" + bytes(16) + struct.pack("<i", count) + mask + controller


class CharacterControllerPrefixTests(unittest.TestCase):
    def test_inline_names_keep_original_controller_offset(self) -> None:
        mask = b"\x02" + struct.pack("<i", 5) + b"Upper" + b"maskhash"
        self.assertEqual(decode_controller_hash(_prefix(mask, 1, b"ctrlhash"), None), b"ctrlhash")

    def test_pooled_names_consume_one_id_and_one_path_hash(self) -> None:
        mask = b"\x02" + struct.pack("<i", 7338) + b"maskhash"
        self.assertEqual(decode_controller_hash(_prefix(mask, 1, b"ctrlhash"), {7338: "Upper"}), b"ctrlhash")

    def test_multiple_pooled_masks_and_zero_id_are_exact(self) -> None:
        mask = (b"\x02" + struct.pack("<i", 0) + b"firstone"
                + b"\x02" + struct.pack("<i", 9) + b"second00")
        self.assertEqual(decode_controller_hash(_prefix(mask, 2, b"ctrlhash"), {0: "Upper", 9: "Lower"}), b"ctrlhash")

    def test_missing_pool_name_and_truncated_hash_fail(self) -> None:
        mask = b"\x02" + struct.pack("<i", 7338) + b"maskhash"
        for strings in ({}, {7338: ""}):
            with self.subTest(strings=strings), self.assertRaisesRegex(ControllerSourceError, "string pool"):
                decode_controller_hash(_prefix(mask, 1, b"ctrlhash"), strings)
        with self.assertRaisesRegex(ControllerSourceError, "mask path hash"):
            decode_controller_hash(_prefix(mask[:-2], 1, b""), {7338: "Upper"})
        with self.assertRaisesRegex(ControllerSourceError, "missing or truncated"):
            decode_controller_hash(_prefix(mask, 1, b"short"), {7338: "Upper"})

    def test_null_or_empty_masks_do_not_require_string_consumption(self) -> None:
        for count in (-1, 0):
            for strings in (None, {7338: "Upper"}):
                with self.subTest(count=count, strings=strings):
                    self.assertEqual(decode_controller_hash(_prefix(b"", count, b"ctrlhash"), strings), b"ctrlhash")


if __name__ == "__main__":
    unittest.main()
