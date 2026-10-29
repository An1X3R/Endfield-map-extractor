"""JIT-compiled parser for inverted LZ4 token streams."""

from __future__ import annotations

import numpy as np
from numba import njit
from numpy.typing import NDArray


NormalizeResult = tuple[NDArray[np.uint8], int, int, int, int, int, int, int, int]

_STANDARD_TOKENS: bytes = bytes(
    (((value & 3) | ((value >> 2) & 12)) << 4)
    | ((value >> 2) & 3)
    | ((value >> 4) & 12)
    for value in range(256)
)
_TOKEN_ARRAY: NDArray[np.uint8] = np.frombuffer(_STANDARD_TOKENS, dtype=np.uint8)


@njit(cache=True, nogil=True)
def _normalize(
    source: NDArray[np.uint8],
    token_table: NDArray[np.uint8],
    expected: int,
) -> NormalizeResult:
    converted = source.copy()
    source_size = source.shape[0]
    source_pos = 0
    output_pos = 0
    final_literal_token = -1
    final_literal_payload = 0
    final_literal_length = 0
    last_match_start = -1

    while source_pos < source_size and output_pos < expected:
        token_pos = source_pos
        token = np.int64(token_table[int(source[source_pos])])
        converted[source_pos] = token
        source_pos += 1
        literal_length = token >> 4
        match_length = token & 15
        if literal_length == 15:
            while True:
                if source_pos >= source_size:
                    return converted, 1, source_pos, output_pos, 0, -1, 0, 0, -1
                value = np.int64(source[source_pos])
                source_pos += 1
                literal_length += value
                if value != 255:
                    break
        if source_pos + literal_length > source_size:
            return converted, 2, source_pos, output_pos, 0, -1, 0, 0, -1
        if literal_length > expected - output_pos:
            return converted, 3, source_pos, output_pos, literal_length, -1, 0, 0, -1
        literal_payload = source_pos
        source_pos += literal_length
        output_pos += literal_length
        if source_pos >= source_size:
            final_literal_token = token_pos
            final_literal_payload = literal_payload
            final_literal_length = literal_length
            break
        if source_pos + 2 > source_size:
            return converted, 4, source_pos, output_pos, 0, -1, 0, 0, -1
        offset = (np.int64(source[source_pos]) << 8) | np.int64(source[source_pos + 1])
        converted[source_pos] = source[source_pos + 1]
        converted[source_pos + 1] = source[source_pos]
        source_pos += 2
        if offset <= 0 or offset > output_pos:
            return converted, 5, source_pos, output_pos, offset, -1, 0, 0, -1
        if match_length == 15:
            while True:
                if source_pos >= source_size:
                    return converted, 6, source_pos, output_pos, 0, -1, 0, 0, -1
                value = np.int64(source[source_pos])
                source_pos += 1
                match_length += value
                if value != 255:
                    break
        match_length += 4
        if match_length > expected - output_pos:
            return converted, 7, source_pos, output_pos, match_length, -1, 0, 0, -1
        last_match_start = output_pos
        output_pos += match_length

    if output_pos != expected or source_pos != source_size:
        return converted, 8, source_pos, output_pos, 0, -1, 0, 0, -1
    return (
        converted,
        0,
        source_pos,
        output_pos,
        0,
        final_literal_token,
        final_literal_payload,
        final_literal_length,
        last_match_start,
    )


def normalize_inverted_lz4(source: bytes, expected: int) -> NormalizeResult:
    source_array: NDArray[np.uint8] = np.frombuffer(source, dtype=np.uint8)
    return _normalize(source_array, _TOKEN_ARRAY, expected)
