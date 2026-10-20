"""Validate legacy streamed cubic parsing independently of game assets."""
from __future__ import annotations

from pathlib import Path
import struct
import sys
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'src/character_runtime'))
from animator_legacy_dense import sample_streamed, streamed_channels


def words(payload: bytes) -> list[int]:
    return list(struct.unpack('<' + 'I' * (len(payload) // 4), payload))


class LegacyStreamedTests(unittest.TestCase):
    def test_sparse_cubic_and_terminal_constant(self) -> None:
        payload = (struct.pack('<fii4f', 0., 1, 0, 2., -3., 4., 5.)
                   + struct.pack('<fii4f', 1., 1, 0, 0., 0., 0., 8.)
                   + struct.pack('<fi', float('inf'), 0))
        channels = streamed_channels(words(payload), 1)
        self.assertEqual(sample_streamed(channels, .5), (6.5,))
        self.assertEqual(sample_streamed(channels, 1.), (8.,))

    def test_missing_channel_and_duplicate_index_rejected(self) -> None:
        payload = struct.pack('<fii4f', 0., 1, 0, 0., 0., 0., 5.) + struct.pack('<fi', float('inf'), 0)
        with self.assertRaisesRegex(ValueError, 'declared channel'):
            streamed_channels(words(payload), 2)
        duplicate = struct.pack('<fi', 0., 2) + struct.pack('<i4f', 0, 0., 0., 0., 5.) * 2 + struct.pack('<fi', float('inf'), 0)
        with self.assertRaisesRegex(ValueError, 'unique in-range'):
            streamed_channels(words(duplicate), 2)

    def test_truncated_frame_and_missing_sentinel_rejected(self) -> None:
        with self.assertRaisesRegex(ValueError, 'payload length'):
            streamed_channels(words(struct.pack('<fi', 0., 1)), 1)
        with self.assertRaisesRegex(ValueError, 'terminal sentinel'):
            streamed_channels(words(struct.pack('<fii4f', 0., 1, 0, 0., 0., 0., 5.)), 1)


if __name__ == '__main__':
    unittest.main()
