"""Validate inverted LZ4 headers and decode their payload with native LZ4."""

from __future__ import annotations

def decompress_lz4_inv(source: bytes, expected: int) -> bytes:
    if type(expected) is not int:
        raise TypeError(f"expected inverted LZ4 output size must be an integer: type={type(expected).__name__}")
    if expected < 0:
        raise ValueError(f"expected inverted LZ4 output size must be non-negative: expected={expected}")
    from .lz4_numba import normalize_inverted_lz4

    (
        converted_array,
        status,
        source_pos,
        output_pos,
        detail,
        final_literal_token,
        final_literal_payload,
        final_literal_length,
        last_match_start,
    ) = normalize_inverted_lz4(source, expected)
    source_size = len(source)
    if status == 1:
        raise ValueError("truncated inverted LZ4 literal length")
    if status == 2:
        raise ValueError("truncated inverted LZ4 literal payload")
    if status == 3:
        raise ValueError(
            f"inverted LZ4 literal exceeds expected output: source_pos={source_pos}, "
            f"literal_length={detail}, output_pos={output_pos}, expected={expected}"
        )
    if status == 4:
        raise ValueError("truncated inverted LZ4 match offset")
    if status == 5:
        raise ValueError(
            f"invalid inverted LZ4 match offset: offset={detail}, "
            f"output_pos={output_pos}, expected={expected}"
        )
    if status == 6:
        raise ValueError("truncated inverted LZ4 match length")
    if status == 7:
        raise ValueError(
            f"inverted LZ4 match exceeds expected output: source_pos={source_pos}, "
            f"match_length={detail}, output_pos={output_pos}, expected={expected}"
        )
    if status == 8:
        raise ValueError(
            f"inverted LZ4 size mismatch: source={source_pos}/{source_size}, "
            f"output={output_pos}/{expected}"
        )
    converted = bytearray(converted_array.tobytes())
    if expected == 0:
        return b""

    # Native LZ4 requires five final literals and twelve bytes after the last
    # match starts. Custom blocks can end in a match; normalize only that tail.
    padding = 0
    if final_literal_token < 0 or (
        last_match_start >= 0
        and (final_literal_length < 5 or expected - last_match_start < 12)
    ):
        padding = 9
        literals = source[final_literal_payload:] if final_literal_token >= 0 else b""
        literal_length = len(literals) + padding
        tail = bytearray((min(literal_length, 15) << 4,))
        if literal_length >= 15:
            extension = literal_length - 15
            tail.extend(b"\xff" * (extension // 255))
            tail.append(extension % 255)
        tail.extend(literals)
        tail.extend(b"\x00" * padding)
        if final_literal_token >= 0:
            converted[final_literal_token:] = tail
        else:
            converted.extend(tail)

    # Blender imports extractor modules without the host Python dependencies.
    from lz4.block import LZ4BlockError, decompress

    try:
        output = decompress(converted, uncompressed_size=expected + padding)
    except LZ4BlockError as exc:
        raise ValueError(
            f"native inverted LZ4 decode failed: source_size={source_size}, "
            f"expected={expected}, reason={exc}"
        ) from exc
    if len(output) != expected + padding:
        raise ValueError(
            f"native inverted LZ4 size mismatch: output={len(output)}, "
            f"expected={expected}, padding={padding}"
        )
    return output[:-padding] if padding else output
