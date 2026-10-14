"""Encode/decode source per-draw weather bytes without claiming vertex-stream routing."""
from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
import math
import struct


class WeatherPerDrawField(Enum):
    """Weather fields accepted by the native custom-per-draw setter."""

    RAIN = 8
    WET = 16
    GLOBAL_WET = 24
    SNOW = 32


@dataclass(frozen=True)
class WeatherPerDrawBytes:
    """Raw UNorm8 channel bytes from one packed per-draw word."""

    rain: int
    wet: int
    global_wet: int
    snow: int

    def __post_init__(self) -> None:
        values = (self.rain, self.wet, self.global_wet, self.snow)
        if any(type(value) is not int or not 0 <= value <= 255 for value in values):
            raise ValueError("Weather per-draw channels must be uint8 values")

    def normalized(self) -> tuple[float, float, float, float]:
        """Return the channel bytes normalized to [0, 1] in field order."""
        return (self.rain / 255.0, self.wet / 255.0,
                self.global_wet / 255.0, self.snow / 255.0)


@dataclass(frozen=True)
class WeatherPerDrawSource:
    """A raw packed weather word and the separate native world-height scalar."""

    packed_word_bits: int
    channels: WeatherPerDrawBytes
    wet_effect_world_space_height: float
    wet_effect_world_space_height_bits: int
    provenance: str


def make_weather_per_draw_source(
    packed_word_bits: int,
    wet_effect_world_space_height: float,
    provenance: str,
) -> WeatherPerDrawSource:
    """Validate a raw native word and round its height through float32 once."""
    if type(packed_word_bits) is not int or not 0 <= packed_word_bits <= 0xFFFFFFFF:
        raise ValueError("Packed weather source must be raw uint32 bits")
    if type(wet_effect_world_space_height) not in (int, float):
        raise TypeError("Wet-effect world-space height must be an int or float scalar")
    if not isinstance(provenance, str) or not provenance.strip():
        raise ValueError("Weather per-draw provenance is required")
    try:
        height_value = float(wet_effect_world_space_height)
        height_bytes = struct.pack("<f", height_value)
    except (OverflowError, struct.error) as error:
        raise ValueError("Wet-effect world-space height cannot be represented as float32") from error
    if not math.isfinite(height_value):
        raise ValueError("Wet-effect world-space height must be finite")
    height_float32 = struct.unpack("<f", height_bytes)[0]
    if not math.isfinite(height_float32):
        raise ValueError("Wet-effect world-space height is not finite after float32 rounding")
    return WeatherPerDrawSource(
        packed_word_bits=packed_word_bits,
        channels=decode_weather_per_draw_bits(packed_word_bits),
        wet_effect_world_space_height=height_float32,
        wet_effect_world_space_height_bits=struct.unpack("<I", height_bytes)[0],
        provenance=provenance,
    )


def pack_weather_per_draw_channel(
    packed_bits: int,
    field: WeatherPerDrawField,
    value: float,
) -> int:
    """Replace one source weather byte using float32 UNorm8 quantization."""
    if type(packed_bits) is not int or not 0 <= packed_bits <= 0xFFFFFFFF:
        raise ValueError("Packed weather input must be raw uint32 bits")
    if type(field) is not WeatherPerDrawField:
        raise TypeError("Weather field must be a WeatherPerDrawField member")
    if type(value) not in (int, float):
        raise TypeError("Weather intensity must be an int or float scalar")
    try:
        source = float(value)
    except OverflowError as error:
        raise ValueError("Weather intensity cannot be represented as float32") from error
    if not math.isfinite(source):
        raise ValueError("Weather intensity must be finite")
    try:
        source_f32 = struct.unpack("<f", struct.pack("<f", source))[0]
    except OverflowError as error:
        raise ValueError("Weather intensity cannot be represented as float32") from error

    clamped = min(1.0, max(0.0, source_f32))
    scaled_f32 = struct.unpack("<f", struct.pack("<f", clamped * 255.0))[0]
    quantized = round(scaled_f32)
    bit_shift = (field.value - 8) & 31
    channel_mask = 0xFF << bit_shift
    return ((packed_bits & ~channel_mask) | (quantized << bit_shift)) & 0xFFFFFFFF


def decode_weather_per_draw_bits(packed_bits: int) -> WeatherPerDrawBytes:
    """Decode the raw bitcast word without converting it through Python float."""
    if type(packed_bits) is not int or not 0 <= packed_bits <= 0xFFFFFFFF:
        raise ValueError("Packed weather input must be raw uint32 bits")
    return WeatherPerDrawBytes(
        rain=packed_bits & 0xFF,
        wet=(packed_bits >> 8) & 0xFF,
        global_wet=(packed_bits >> 16) & 0xFF,
        snow=(packed_bits >> 24) & 0xFF,
    )

