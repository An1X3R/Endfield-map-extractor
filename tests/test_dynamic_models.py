"""Check strict prefab-placement decoding against an optional local source fixture."""
from __future__ import annotations

import math
import os
from pathlib import Path
import struct
import unittest

from extractor_core.dynamic_models import (
    DynamicModelDecodeError, _decode_model_file, read_dynamic_scene_models_for_bounds,
)


class DynamicModelTests(unittest.TestCase):
    def test_cancelled_request_does_not_read_sources(self) -> None:
        def cancelled() -> None:
            raise InterruptedError("model request cancelled")

        with self.assertRaisesRegex(InterruptedError, "model request cancelled"):
            read_dynamic_scene_models_for_bounds(Path("missing"), Path("missing"), "map02",
                                                (0., 128., 0., 128.), cancelled)

    def test_invalid_bounds_fail_before_reading_sources(self) -> None:
        for bounds in ((0., 0., 0., 128.), (128., 0., 0., 128.), (0., math.nan, 0., 128.)):
            with self.subTest(bounds=bounds), self.assertRaisesRegex(ValueError, "Model selection bounds"):
                read_dynamic_scene_models_for_bounds(Path("missing"), Path("missing"), "map02", bounds, lambda: None)

    def test_truncated_source_reports_path(self) -> None:
        for data in (b"", b"\0" * 7, b"\xff" * 12):
            with self.subTest(length=len(data)), self.assertRaisesRegex(DynamicModelDecodeError, "fixture.bytes"):
                _decode_model_file(data, "fixture.bytes")

    @unittest.skipUnless(os.environ.get("ENDFIELD_DYNAMIC_MODEL_SOURCE"), "Local game fixture was not supplied")
    def test_current_source_preserves_all_records_and_rejects_corrupt_transforms(self) -> None:
        path = Path(os.environ["ENDFIELD_DYNAMIC_MODEL_SOURCE"])
        data = path.read_bytes()
        rows = _decode_model_file(data, path.name)
        self.assertTrue(rows)
        for row in rows:
            expected = struct.unpack_from("<4I9f3Iq5fi", data, row.record_offset)
            self.assertEqual(expected[:4], row.guid_words)
            self.assertEqual(expected[4:7], row.position)
            self.assertEqual(expected[7:10], row.rotation_euler_degrees)
            self.assertEqual(expected[10:13], row.scale)
            self.assertEqual(expected[13:16], row.grid_chain)
            self.assertEqual(struct.pack("<q", expected[16]).hex(), row.path_hash_hex)
            self.assertEqual(expected[17:20], row.visibility_center)
            self.assertEqual(expected[20:], (row.base_radius, row.min_distance, row.importance))
            self.assertEqual(data[row.record_offset:row.record_offset + 96].hex(), row.raw_record_hex)
        for relative_offset, value in ((16, math.nan), (84, -1.), (88, -1.)):
            corrupt = bytearray(data)
            struct.pack_into("<f", corrupt, rows[0].record_offset + relative_offset, value)
            with self.subTest(offset=relative_offset), self.assertRaises(DynamicModelDecodeError):
                _decode_model_file(bytes(corrupt), path.name)


if __name__ == "__main__":
    unittest.main()
