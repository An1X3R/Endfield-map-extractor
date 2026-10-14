"""Validate inverted LZ4 headers and decode their payload with native LZ4."""

from __future__ import annotations


_STANDARD_TOKENS = bytes(
    (((value & 3) | ((value >> 2) & 12)) << 4)
    | ((value >> 2) & 3)
    | ((value >> 4) & 12)
    for value in range(256)
)


def decompress_lz4_inv(source: bytes, expected: int) -> bytes:
    if type(expected) is not int:
        raise TypeError(f"expected inverted LZ4 output size must be an integer: type={type(expected).__name__}")
    if expected < 0:
        raise ValueError(f"expected inverted LZ4 output size must be non-negative: expected={expected}")
    converted = bytearray(source)
    source_size = len(source)
    source_pos = 0
    output_pos = 0
    final_literal_token = -1
    final_literal_payload = 0
    final_literal_length = 0
    last_match_start = -1
    while source_pos < source_size and output_pos < expected:
        token_pos = source_pos
        token = _STANDARD_TOKENS[source[source_pos]]
        converted[source_pos] = token
        source_pos += 1
        literal_length = token >> 4
        match_length = token & 15
        if literal_length == 15:
            while True:
                if source_pos >= source_size:
                    raise ValueError("truncated inverted LZ4 literal length")
                value = source[source_pos]
                source_pos += 1
                literal_length += value
                if value != 255:
                    break
        if source_pos + literal_length > source_size:
            raise ValueError("truncated inverted LZ4 literal payload")
        if literal_length > expected - output_pos:
            raise ValueError(
                f"inverted LZ4 literal exceeds expected output: source_pos={source_pos}, "
                f"literal_length={literal_length}, output_pos={output_pos}, expected={expected}"
            )
        literal_payload = source_pos
        source_pos += literal_length
        output_pos += literal_length
        if source_pos >= source_size:
            final_literal_token = token_pos
            final_literal_payload = literal_payload
            final_literal_length = literal_length
            break
        if source_pos + 2 > source_size:
            raise ValueError("truncated inverted LZ4 match offset")
        offset = (source[source_pos] << 8) | source[source_pos + 1]
        converted[source_pos] = source[source_pos + 1]
        converted[source_pos + 1] = source[source_pos]
        source_pos += 2
        if offset <= 0 or offset > output_pos:
            raise ValueError(
                f"invalid inverted LZ4 match offset: offset={offset}, "
                f"output_pos={output_pos}, expected={expected}"
            )
        if match_length == 15:
            while True:
                if source_pos >= source_size:
                    raise ValueError("truncated inverted LZ4 match length")
                value = source[source_pos]
                source_pos += 1
                match_length += value
                if value != 255:
                    break
        match_length += 4
        if match_length > expected - output_pos:
            raise ValueError(
                f"inverted LZ4 match exceeds expected output: source_pos={source_pos}, "
                f"match_length={match_length}, output_pos={output_pos}, expected={expected}"
            )
        last_match_start = output_pos
        output_pos += match_length
    if output_pos != expected or source_pos != source_size:
        raise ValueError(
            f"inverted LZ4 size mismatch: source={source_pos}/{source_size}, "
            f"output={output_pos}/{expected}"
        )
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
