"""Read source facial poses and emotion references without inventing scheduling."""
from __future__ import annotations

from dataclasses import dataclass
import json
from pathlib import Path

from character_facial_curves import (
    FacialCurveFormatError, FacialCurveValue, _as_array, _as_integer, _as_number,
    _as_object, _as_path_id, _as_text, _required,
)


@dataclass(frozen=True, slots=True)
class FacialSourcePointer:
    field_name: str
    file_id: int
    path_id: str


@dataclass(frozen=True, slots=True)
class FacialPose:
    values: tuple[FacialCurveValue, ...]
    additive_pose: int


@dataclass(frozen=True, slots=True)
class FacialEmotion:
    asset_path: str
    source_cab: str
    source_path_id: str
    name: str
    pose: FacialPose
    blink_interval: float
    blink_interval_random_range: float
    blink_speed: float
    special_eye_speed: float
    phoneme_lips_type: int
    emotion_remaining_degree: float
    references: tuple[FacialSourcePointer, ...]


def read_facial_pose(value: object, context: str) -> FacialPose:
    """Preserve all six source regions and their exact control weights."""
    raw = _as_object(value, context)
    values: list[FacialCurveValue] = []
    for field in ('_browValueL', '_browValueR', '_eyeValueL', '_eyeValueR', '_mouthValue', '_otherValue'):
        entries = _as_array(_required(raw, field, context), f'{context}.{field}')
        for index, entry in enumerate(entries):
            where = f'{context}.{field}[{index}]'
            values.append(FacialCurveValue(field,
                _as_text(_required(entry, '_ctrlName', where), f'{where}._ctrlName'),
                _as_number(_required(entry, '_value', where), f'{where}._value')))
    additive = _as_integer(_required(raw, '_bIsAdditivePose', context), f'{context}._bIsAdditivePose')
    if additive not in (0, 1):
        raise FacialCurveFormatError(f'Unsupported serialized pose flag at {context}: {additive}')
    return FacialPose(tuple(values), additive)


def _read_pointer(value: object, field_name: str) -> FacialSourcePointer:
    raw = _as_object(value, field_name)
    file_id = _as_integer(_required(raw, 'm_FileID', field_name), f'{field_name}.m_FileID')
    path_id = _as_path_id(_required(raw, 'm_PathID', field_name), f'{field_name}.m_PathID')
    if file_id < 0 or (path_id == '0' and file_id != 0):
        raise FacialCurveFormatError(f'Invalid serialized reference at {field_name}: {file_id}/{path_id}')
    return FacialSourcePointer(field_name, file_id, path_id)


def read_facial_emotion_probe(probe_path: Path, asset_path: str,
                              expected_cab: str, expected_path_id: str) -> FacialEmotion:
    """Read one identified emotion asset, retaining references rather than following guesses."""
    rows = _as_array(json.loads(probe_path.read_text(encoding='utf-8')), str(probe_path))
    if len(rows) != 1:
        raise FacialCurveFormatError(f'Expected one emotion object at {probe_path}, got {len(rows)}')
    row = _as_object(rows[0], str(probe_path))
    cab = _as_text(_required(row, 'source', str(probe_path)), 'source')
    path_id = _as_path_id(_required(row, 'pathId', str(probe_path)), 'pathId')
    if cab != expected_cab or path_id != expected_path_id:
        raise FacialCurveFormatError(f'Emotion source mismatch: actual={cab}/{path_id}, expected={expected_cab}/{expected_path_id}')
    raw = _as_object(_required(row, 'typeTree', str(probe_path)), 'typeTree')
    references = [_read_pointer(_required(raw, field, 'emotion'), field) for field in ('_blinkAnim', '_speyeAnim')]
    for index, value in enumerate(_as_array(_required(raw, '_microExpressionPoses', 'emotion'), '_microExpressionPoses')):
        references.append(_read_pointer(value, f'_microExpressionPoses[{index}]'))
    return FacialEmotion(
        _as_text(asset_path, 'asset_path'), cab, path_id,
        _as_text(_required(raw, 'm_Name', 'emotion'), 'm_Name'),
        read_facial_pose(_required(raw, '_pose', 'emotion'), 'emotion._pose'),
        _as_number(_required(raw, '_blinkInterval', 'emotion'), '_blinkInterval'),
        _as_number(_required(raw, '_blinkIntervalRandomRange', 'emotion'), '_blinkIntervalRandomRange'),
        _as_number(_required(raw, '_blinkSpeed', 'emotion'), '_blinkSpeed'),
        _as_number(_required(raw, '_speyeAnimSpeed', 'emotion'), '_speyeAnimSpeed'),
        _as_integer(_required(raw, '_phonemePoseLipsType', 'emotion'), '_phonemePoseLipsType'),
        _as_number(_required(raw, '_emotionRemainedDegree', 'emotion'), '_emotionRemainedDegree'),
        tuple(references))


def missing_pose_controls(pose: FacialPose, available_names: tuple[str, ...]) -> tuple[str, ...]:
    """Report exact missing names; compatibility does not establish character ownership."""
    available = frozenset(available_names)
    return tuple(sorted({row.control_name for row in pose.values if row.control_name not in available}))


def source_pose_weights(pose: FacialPose) -> dict[str, float]:
    """Expose an unambiguous source pose without assigning additive/override semantics."""
    weights = {row.control_name: row.value for row in pose.values}
    if len(weights) != len(pose.values):
        raise FacialCurveFormatError('Duplicate pose control names require verified source combination semantics')
    return weights
