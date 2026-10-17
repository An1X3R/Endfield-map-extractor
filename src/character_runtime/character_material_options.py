"""Pure, typed validation for optional character material export settings."""
from __future__ import annotations

from dataclasses import dataclass
import json
from typing import Literal, cast


MaterialMode = Literal["original", "simplified"]
LightingQuality = Literal["full", "simple"]
WeatherQuality = Literal["simple", "full"]
WeatherEffect = Literal["wet", "snow", "rain"]
WEATHER_EFFECTS: tuple[WeatherEffect, ...] = ("wet", "snow", "rain")


class CharacterMaterialOptionsError(ValueError):
    """A material export option violates the public contract."""


@dataclass(frozen=True)
class CharacterWeatherOptions:
    quality: WeatherQuality
    effects: tuple[WeatherEffect, ...]

    def as_json(self) -> dict[str, object]:
        """Return a stable JSON-compatible weather selection."""
        return {"quality": self.quality, "effects": list(self.effects)}


@dataclass(frozen=True)
class CharacterMaterialOptions:
    mode: MaterialMode
    lighting: LightingQuality
    weather: CharacterWeatherOptions

    def as_json(self) -> dict[str, object]:
        """Return the canonical JSON-compatible material selection."""
        return {"mode": self.mode, "lighting": self.lighting, "weather": self.weather.as_json()}

    def canonical_json(self) -> str:
        """Serialize settings deterministically for command input and cache keys."""
        return json.dumps(self.as_json(), sort_keys=True, separators=(",", ":"))


def _object(value: object, field: str) -> dict[str, object]:
    if not isinstance(value, dict) or any(not isinstance(key, str) for key in value):
        raise CharacterMaterialOptionsError(f"{field}: expected an object")
    return cast(dict[str, object], value)


def _choice(value: object, field: str, choices: tuple[str, ...]) -> str:
    if not isinstance(value, str) or value not in choices:
        raise CharacterMaterialOptionsError(f"{field}: expected one of {', '.join(choices)}")
    return value


def parse_character_material_options(value: object) -> CharacterMaterialOptions:
    """Require every material setting, ignore unrelated keys, and canonicalize effects."""
    row = _object(value, "options.materials")
    mode = cast(MaterialMode, _choice(row.get("mode"), "options.materials.mode", ("original", "simplified")))
    lighting = cast(LightingQuality, _choice(row.get("lighting"), "options.materials.lighting", ("full", "simple")))
    weather_row = _object(row.get("weather"), "options.materials.weather")
    quality = cast(WeatherQuality, _choice(weather_row.get("quality"), "options.materials.weather.quality", ("simple", "full")))
    effects_value = weather_row.get("effects")
    if not isinstance(effects_value, list):
        raise CharacterMaterialOptionsError("options.materials.weather.effects: expected an array")
    effects: list[WeatherEffect] = []
    for index, raw_effect in enumerate(effects_value):
        effect = cast(WeatherEffect, _choice(raw_effect, f"options.materials.weather.effects[{index}]", WEATHER_EFFECTS))
        if effect in effects:
            raise CharacterMaterialOptionsError(f"options.materials.weather.effects: duplicate effect {effect}")
        effects.append(effect)
    selected_effects = tuple(effect for effect in WEATHER_EFFECTS if effect in effects)
    if mode == "original" and (lighting != "full" or quality != "full" or selected_effects != WEATHER_EFFECTS):
        raise CharacterMaterialOptionsError(
            "options.materials: original mode requires full lighting, full weather, and wet, snow, and rain"
        )
    return CharacterMaterialOptions(mode, lighting, CharacterWeatherOptions(quality, selected_effects))
