"""Decode source display records and exact Transform ancestry for potential layers."""
from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
import math
from pathlib import Path
import json


@dataclass(frozen=True)
class DisplayEffect:
    name: str
    mount_point: str
    follow_rotation: int
    follow_scale: int
    offset: tuple[float, float, float]


@dataclass(frozen=True)
class SourceTransform:
    name: str
    source: str
    path_id: int
    position: tuple[float, ...]
    rotation: tuple[float, ...]
    scale: tuple[float, ...]


def record(value: object, context: str) -> Mapping[str, object]:
    if not isinstance(value, dict) or any(type(key) is not str for key in value):
        raise ValueError(f'Potential source requires an object: field={context}')
    return value


def rows(value: object, context: str) -> tuple[Mapping[str, object], ...]:
    if not isinstance(value, list):
        raise ValueError(f'Potential source requires an array: field={context}')
    return tuple(record(item, f'{context}[{index}]') for index, item in enumerate(value))


def text(value: object, context: str) -> str:
    if type(value) is not str or not value.strip():
        raise ValueError(f'Potential source requires nonempty text: field={context}')
    return value


def integer(value: object, context: str) -> int:
    if type(value) is not int:
        raise ValueError(f'Potential source requires an integer: field={context}, value={value!r}')
    return value


def vector(value: object, components: str, context: str) -> tuple[float, ...]:
    source = record(value, context)
    result: list[float] = []
    for component in components:
        number = source.get(component)
        if type(number) not in (int, float) or not math.isfinite(number):
            raise ValueError(f'Potential source vector is invalid: field={context}.{component}, value={number!r}')
        result.append(float(number))
    return tuple(result)


def read_document(path: Path) -> Mapping[str, object]:
    return record(json.loads(path.read_text(encoding='utf-8-sig')), str(path))


def display_effects(document: object, character_id: str) -> tuple[DisplayEffect, ...]:
    """Select a verified managed-reference record by its complete character key."""
    root = record(document, 'display configuration')
    data = record(root.get('data'), 'data')
    keys = data.get('_keys')
    values = rows(data.get('_values'), 'data._values')
    if not isinstance(keys, list) or any(type(key) is not str for key in keys):
        raise ValueError('Potential display dictionary keys must be strings')
    if len(keys) != len(set(keys)) or len(keys) != len(values):
        raise ValueError('Potential display dictionary is ambiguous')
    if character_id not in keys:
        raise ValueError(f'Potential display record is absent: character_id={character_id}')
    reference = integer(values[keys.index(character_id)].get('rid'), 'selected.rid')
    if reference == -2:
        return ()
    registry = rows(record(root.get('references'), 'references').get('RefIds'), 'references.RefIds')
    identities = tuple(integer(row.get('rid'), 'RefIds.rid') for row in registry)
    if len(identities) != len(set(identities)) or reference not in identities:
        raise ValueError(f'Potential managed reference is ambiguous or missing: rid={reference}')
    entry = registry[identities.index(reference)]
    if entry.get('type') != {'class': 'CharacterDisplayData', 'ns': 'Beyond.Gameplay', 'asm': 'Gameplay.Beyond'}:
        raise ValueError(f'Potential display record has unsupported type: character_id={character_id}')
    payload = record(entry.get('data'), 'selected.data')
    if payload.get('charId') != character_id or payload.get('exactTypeTreeDecoded') is not True:
        raise ValueError(f'Potential display record lacks exact type-tree evidence: character_id={character_id}')
    effects = rows(record(payload.get('potentialEffectConfig'), 'potentialEffectConfig').get('potentialEffects'), 'potentialEffects')
    result: list[DisplayEffect] = []
    for effect in effects:
        rotation = integer(effect.get('followRotation'), 'followRotation')
        scale = integer(effect.get('followScale'), 'followScale')
        if rotation not in (0, 1) or scale not in (0, 1):
            raise ValueError(f'Potential display follow flags must be source 0/1: effect={effect}')
        offset = vector(effect.get('offset'), 'xyz', 'offset')
        result.append(DisplayEffect(text(effect.get('name'), 'name'), text(effect.get('mountPoint'), 'mountPoint'),
                                    rotation, scale, (offset[0], offset[1], offset[2])))
    return tuple(result)


def mount_ancestry(json_root: Path, resource_path: str, mount_point: str) -> tuple[SourceTransform, ...]:
    """Use exact source CAB/PathID links, with no fuzzy bone-name matching."""
    objects = tuple(read_document(path) for path in sorted((json_root / 'GameObject').glob('*.json')))
    selected = tuple(row for row in objects if record(row.get('$animestudio'), 'metadata').get('container') == resource_path
                     and row.get('m_Name') == mount_point)
    if len(selected) != 1:
        raise ValueError(f'Potential source mount must resolve once: resource={resource_path}, mount={mount_point}, matches={len(selected)}')
    by_transform: dict[tuple[str, int], Mapping[str, object]] = {}
    for row in objects:
        source = text(record(row.get('$animestudio'), 'metadata').get('sourceFile'), 'sourceFile')
        transform = record(row.get('m_Transform'), 'm_Transform')
        components = rows(row.get('m_Components'), 'm_Components')
        # The reader preserves Unity's leading Transform component without a type label.
        if not components:
            raise ValueError(f'Potential GameObject has no leading Transform: source={source}')
        component = components[0]
        key = (source, integer(component.get('m_PathID'), 'Transform.m_PathID'))
        if key in by_transform:
            raise ValueError(f'Potential hierarchy repeats a Transform: identity={key}')
        by_transform[key] = row
        record(transform.get('m_Father'), 'm_Father')
    current = selected[0]
    seen: set[tuple[str, int]] = set()
    chain: list[SourceTransform] = []
    while True:
        source = text(record(current.get('$animestudio'), 'metadata').get('sourceFile'), 'sourceFile')
        component = rows(current.get('m_Components'), 'm_Components')[0]
        key = (source, integer(component.get('m_PathID'), 'Transform.m_PathID'))
        if key in seen:
            raise ValueError(f'Potential source hierarchy contains a cycle: identity={key}')
        seen.add(key)
        transform = record(current.get('m_Transform'), 'm_Transform')
        chain.append(SourceTransform(text(current.get('m_Name'), 'm_Name'), key[0], key[1],
                                     vector(transform.get('m_LocalPosition'), 'XYZ', 'position'),
                                     vector(transform.get('m_LocalRotation'), 'XYZW', 'rotation'),
                                     vector(transform.get('m_LocalScale'), 'XYZ', 'scale')))
        father = record(transform.get('m_Father'), 'm_Father')
        path_id = integer(father.get('m_PathID'), 'm_Father.m_PathID')
        if path_id == 0:
            break
        if integer(father.get('m_FileID'), 'm_Father.m_FileID') != 0:
            raise ValueError(f'Potential source hierarchy has an external parent: identity={key}')
        parent = by_transform.get((source, path_id))
        if parent is None:
            raise ValueError(f'Potential source hierarchy parent is absent: source={source}, path_id={path_id}')
        current = parent
    if len(chain) < 2:
        raise ValueError(f'Potential mount has no parent Transform: mount={mount_point}')
    return tuple(reversed(chain))
