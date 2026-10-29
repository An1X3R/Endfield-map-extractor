"""Correctness tests for the inverted LZ4 native adapter."""

from __future__ import annotations

import random
import unittest

from lz4.block import compress, decompress

from extractor_core.lz4 import decompress_lz4_inv


_STANDARD_TOKENS = bytes(
    (((value & 3) | ((value >> 2) & 12)) << 4)
    | ((value >> 2) & 3)
    | ((value >> 4) & 12)
    for value in range(256)
)
_STANDARD_TO_INVERTED: tuple[int, ...] = tuple(
    next(inverted for inverted, standard in enumerate(_STANDARD_TOKENS) if standard == token)
    for token in range(256)
)


def token_for(literal_nibble: int, match_nibble: int) -> bytes:
    token = (
        (literal_nibble & 0x03)
        | ((literal_nibble & 0x0C) << 2)
        | ((match_nibble & 0x03) << 2)
        | ((match_nibble & 0x0C) << 4)
    )
    return bytes((token,))


def invert_standard_lz4_block(source: bytes) -> bytes:
    inverted = bytearray(source)
    source_pos = 0
    while source_pos < len(source):
        token = source[source_pos]
        inverted[source_pos] = _STANDARD_TO_INVERTED[token]
        source_pos += 1
        literal_length = token >> 4
        if literal_length == 15:
            while True:
                if source_pos >= len(source):
                    raise ValueError("Truncated standard LZ4 literal length in test fixture")
                extension = source[source_pos]
                source_pos += 1
                literal_length += extension
                if extension != 255:
                    break
        if source_pos + literal_length > len(source):
            raise ValueError("Truncated standard LZ4 literal payload in test fixture")
        source_pos += literal_length
        if source_pos == len(source):
            break
        if source_pos + 2 > len(source):
            raise ValueError("Truncated standard LZ4 offset in test fixture")
        inverted[source_pos] = source[source_pos + 1]
        inverted[source_pos + 1] = source[source_pos]
        source_pos += 2
        if token & 15 == 15:
            while True:
                if source_pos >= len(source):
                    raise ValueError("Truncated standard LZ4 match length in test fixture")
                extension = source[source_pos]
                source_pos += 1
                if extension != 255:
                    break
    return bytes(inverted)


class InvertedLz4Tests(unittest.TestCase):
    def test_compiled_decoder_matches_lz4_blocks_without_mutating_read_only_input(self) -> None:
        generator = random.Random(0xE4D1)
        payloads = (
            b"x",
            b"ABCD" * 4096,
            generator.randbytes(8192),
        )
        for payload in payloads:
            with self.subTest(payload_size=len(payload)):
                standard_block = compress(payload, store_size=False)
                release_output = decompress(standard_block, uncompressed_size=len(payload))
                self.assertEqual(release_output, payload)
                inverted_block = invert_standard_lz4_block(standard_block)
                source_snapshot = bytes(inverted_block)
                self.assertEqual(decompress_lz4_inv(inverted_block, len(payload)), release_output)
                self.assertEqual(inverted_block, source_snapshot)

        from extractor_core.lz4_numba import _normalize

        self.assertTrue(_normalize.signatures)

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
