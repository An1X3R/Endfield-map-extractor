"""Select the source UI appearance marker without character or group allowlists."""
from __future__ import annotations

from dataclasses import dataclass
from collections.abc import Mapping
import re


APPEARANCE_MARKER = '_vfxpart_'
SELECTION_RULE = 'source_vfxpart_marker_highest_lod/1'


@dataclass(frozen=True)
class PotentialRenderer:
    identity: str
    source_name: str
    lod_index: int | None
    selected: bool
    reason: str


def _object(value: object, context: str) -> Mapping[str, object]:
    if not isinstance(value, dict) or any(type(key) is not str for key in value):
        raise ValueError(f'Potential source must be an object: field={context}')
    return value


def _text(value: object, context: str) -> str:
    if type(value) is not str or not value:
        raise ValueError(f'Potential source must be a nonempty string: field={context}')
    return value


def _boolean(value: object, context: str) -> bool:
    if type(value) is not bool:
        raise ValueError(f'Potential source must be a boolean: field={context}, value={value!r}')
    return value


def select_potential_renderers(selection: object) -> tuple[PotentialRenderer, ...]:
    """Use authored renderer names and the existing source-qualified LOD report."""
    document = _object(selection, 'selection')
    values = document.get('renderer_decisions')
    if not isinstance(values, list):
        raise ValueError('Potential source renderer_decisions must be an array')
    found: set[str] = set()
    result: list[PotentialRenderer] = []
    for index, value in enumerate(values):
        context = f'renderer_decisions[{index}]'
        row = _object(value, context)
        identity = _text(row.get('renderer_identity'), context + '.renderer_identity')
        key = identity.casefold()
        if key in found:
            raise ValueError(f'Duplicate potential source renderer identity: identity={identity}')
        found.add(key)
        name = _text(row.get('native_game_object_name'), context + '.native_game_object_name')
        if APPEARANCE_MARKER not in name:
            continue
        if row.get('renderer_type') not in ('MeshRenderer', 'SkinnedMeshRenderer'):
            raise ValueError(f'Unsupported potential renderer type: identity={identity}, type={row.get("renderer_type")}')
        if 'lod_index' not in row:
            raise ValueError(f'Potential source LOD evidence missing: identity={identity}')
        lod = row['lod_index']
        if lod is not None and (type(lod) is not int or lod < 0):
            raise ValueError(f'Potential source LOD index invalid: identity={identity}, value={lod!r}')
        shadow = _boolean(row.get('native_shadows_only'), context + '.native_shadows_only')
        retained = _boolean(row.get('retained_in_scene'), context + '.retained_in_scene')
        if shadow:
            reason = 'source_shadows_only'
        elif lod is not None and lod > 0:
            reason = 'lower_source_lod'
        elif lod is None and re.search(r'_lod\d+$', name, re.IGNORECASE):
            reason = 'missing_source_lod_membership'
        elif not retained:
            reason = 'source_renderer_not_retained'
        else:
            reason = 'source_ui_appearance_marker'
        result.append(PotentialRenderer(identity, name, lod, reason == 'source_ui_appearance_marker', reason))
    return tuple(result)


def potential_renderer_identities(selection: object) -> tuple[str, ...]:
    """Reject incomplete selections instead of presenting them as full coverage."""
    rows = select_potential_renderers(selection)
    unresolved = tuple(row for row in rows if row.reason in (
        'missing_source_lod_membership', 'source_renderer_not_retained'))
    if unresolved:
        raise ValueError(f'Potential source selection is incomplete: renderers={unresolved}')
    return tuple(row.identity for row in rows if row.selected)
