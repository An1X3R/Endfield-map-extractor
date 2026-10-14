"""Correctness tests for the inverted LZ4 native adapter."""

from __future__ import annotations

import unittest

from extractor_core.lz4 import decompress_lz4_inv


def token_for(literal_nibble: int, match_nibble: int) -> bytes:
    token = (
        (literal_nibble & 0x03)
        | ((literal_nibble & 0x0C) << 2)
        | ((match_nibble & 0x03) << 2)
        | ((match_nibble & 0x0C) << 4)
    )
    return bytes((token,))


class InvertedLz4Tests(unittest.TestCase):
    def test_empty_block(self) -> None:
        self.assertEqual(decompress_lz4_inv(b"", 0), b"")

    def test_extended_literals(self) -> None:
        source = token_for(15, 0) + b"\xff\x02" + b"A" * 272
        self.assertEqual(decompress_lz4_inv(source, 272), b"A" * 272)

    def test_short_tail_after_match(self) -> None:
        source = token_for(1, 0) + b"A\x00\x01" + token_for(2, 0) + b"BC"
        self.assertEqual(decompress_lz4_inv(source, 7), b"AAAAABC")

    def test_tail_requires_extended_length_when_padded(self) -> None:
        source = token_for(1, 0) + b"A\x00\x01" + token_for(7, 0) + b"BCDEFGH"
        self.assertEqual(decompress_lz4_inv(source, 12), b"AAAAABCDEFGH")

    def test_truncated_streams(self) -> None:
        cases = (
            (token_for(15, 0), 16, "literal length"),
            (token_for(3, 0) + b"ab", 3, "literal payload"),
            (token_for(1, 0) + b"A\x00", 5, "match offset"),
            (token_for(1, 15) + b"A\x00\x01", 20, "match length"),
        )
        for source, expected, message in cases:
            with self.subTest(message=message):
                with self.assertRaisesRegex(ValueError, message):
                    decompress_lz4_inv(source, expected)

    def test_rejects_size_mismatch_and_unconsumed_tail(self) -> None:
        for source, expected in ((b"", 1), (b"\x00", 0), (token_for(1, 0) + b"A", 2),
                                 (token_for(1, 0) + b"A\x00\x01\x00", 5)):
            with self.subTest(source=source, expected=expected):
                with self.assertRaisesRegex(ValueError, "size mismatch"):
                    decompress_lz4_inv(source, expected)

    def test_literal_only_block(self) -> None:
        self.assertEqual(decompress_lz4_inv(token_for(3, 0) + b"abc", 3), b"abc")

    def test_nonoverlapping_match_copies_a_slice(self) -> None:
        source = token_for(8, 0) + b"abcdefgh\x00\x08"
        self.assertEqual(decompress_lz4_inv(source, 12), b"abcdefghabcd")

    def test_overlapping_match_repeats_prefix_and_remainder(self) -> None:
        source = token_for(3, 4) + b"abc\x00\x03"
        self.assertEqual(decompress_lz4_inv(source, 11), b"abcabcabcab")

    def test_long_offset_one_match_repeats_a_single_byte(self) -> None:
        match_extension = b"\xff" * 15 + b"\xfc"
        source = token_for(1, 15) + b"A\x00\x01" + match_extension
        self.assertEqual(decompress_lz4_inv(source, 4097), b"A" * 4097)

    def test_rejects_match_offset_beyond_written_output(self) -> None:
        source = token_for(0, 0) + b"\x00\x01"
        with self.assertRaisesRegex(ValueError, r"offset=1, output_pos=0, expected=4"):
            decompress_lz4_inv(source, 4)

    def test_rejects_literal_output_overflow_before_resizing_buffer(self) -> None:
        source = token_for(3, 0) + b"abc"
        with self.assertRaisesRegex(ValueError, r"literal_length=3, output_pos=0, expected=2"):
            decompress_lz4_inv(source, 2)

    def test_rejects_match_output_overflow_before_copying(self) -> None:
        source = token_for(1, 0) + b"A\x00\x01"
        with self.assertRaisesRegex(ValueError, r"match_length=4, output_pos=1, expected=4"):
            decompress_lz4_inv(source, 4)

    def test_rejects_invalid_expected_size(self) -> None:
        with self.assertRaisesRegex(ValueError, r"expected=-1"):
            decompress_lz4_inv(b"", -1)
        with self.assertRaisesRegex(TypeError, r"type=float"):
            decompress_lz4_inv(b"", 1.0)


if __name__ == "__main__":
    unittest.main()
