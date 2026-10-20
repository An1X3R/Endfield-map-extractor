"""Pure validation for optional character appearance export settings."""
from __future__ import annotations

from dataclasses import dataclass
from typing import Literal, cast


PotentialAppearance = Literal["base", "maximum"]
FurAppearance = Literal["existing", "source_shell", "charcoal_embers"]
AppearanceReportStatus = Literal["completed", "partial"]
PotentialReportStatus = Literal["applied", "not_applicable", "partial"]
FurReportStatus = Literal["applied", "not_applicable", "partial", "unchanged"]


class CharacterAppearanceOptionsError(ValueError):
    """An appearance export option violates the public contract."""


@dataclass(frozen=True)
class CharacterAppearanceOptions:
    potential: PotentialAppearance
    fur: FurAppearance

    def as_json(self) -> dict[str, str]:
        """Return the canonical JSON-compatible appearance selection."""
        return {"potential": self.potential, "fur": self.fur}


def _object(value: object, field: str) -> dict[str, object]:
    if not isinstance(value, dict) or any(not isinstance(key, str) for key in value):
        raise CharacterAppearanceOptionsError(f"{field}: expected an object")
    return cast(dict[str, object], value)


def _choice(value: object, field: str, choices: tuple[str, ...]) -> str:
    if not isinstance(value, str) or value not in choices:
        raise CharacterAppearanceOptionsError(f"{field}: expected one of {', '.join(choices)}")
    return value


def parse_character_appearance_options(value: object) -> CharacterAppearanceOptions:
    """Require both appearance settings, ignore unrelated keys, and canonicalize values."""
    row = _object(value, "options.appearance")
    potential = cast(PotentialAppearance, _choice(
        row.get("potential"), "options.appearance.potential", ("base", "maximum")))
    fur = cast(FurAppearance, _choice(
        row.get("fur"), "options.appearance.fur", ("existing", "source_shell", "charcoal_embers")))
    return CharacterAppearanceOptions(potential, fur)


def verify_character_appearance_report(expected: CharacterAppearanceOptions, value: object) -> dict[str, object]:
    """Require completed source-specific appearance acknowledgements from the export receipt."""
    row = _object(value, "appearance_report")
    if row.get("format") != "EndfieldCharacterAppearance/1":
        raise CharacterAppearanceOptionsError("appearance_report.format: unsupported report format")
    status = cast(AppearanceReportStatus, _choice(
        row.get("status"), "appearance_report.status", ("completed", "partial")))
    potential_row = _object(row.get("potential"), "appearance_report.potential")
    potential = cast(PotentialAppearance, _choice(
        potential_row.get("requested"), "appearance_report.potential.requested", ("base", "maximum")))
    potential_status = cast(PotentialReportStatus, _choice(
        potential_row.get("status"), "appearance_report.potential.status", ("applied", "not_applicable", "partial")))
    fur_row = _object(row.get("fur"), "appearance_report.fur")
    fur = cast(FurAppearance, _choice(
        fur_row.get("requested"), "appearance_report.fur.requested", ("existing", "source_shell", "charcoal_embers")))
    fur_status = cast(FurReportStatus, _choice(
        fur_row.get("status"), "appearance_report.fur.status", ("applied", "not_applicable", "partial", "unchanged")))
    if potential != expected.potential or fur != expected.fur:
        raise CharacterAppearanceOptionsError("appearance_report requested settings differ from the export request")
    if status == "completed" and (potential_status == "partial" or fur_status == "partial"):
        raise CharacterAppearanceOptionsError("appearance_report.status cannot be completed with a partial component")
    return dict(row)
