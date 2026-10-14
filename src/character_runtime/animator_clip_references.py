"""Read clip reference components using the verified UnityPlayer vector branch.

The exact engine reference consumers select the serialized reference vector when
its offset pointer is nonzero, otherwise ValueArrayDelta.start. This describes
reference setup only; it does not establish Animator state or pose mixing.
"""
from __future__ import annotations

from dataclasses import dataclass

from animator_auxiliary_layers import number, object_row, rows


@dataclass(frozen=True)
class ClipReference:
    components: tuple[float, ...]
    provenance: str


def binding_width(binding: object) -> int:
    row = object_row(binding, 'binding')
    if row['type'] == 'Transform':
        attribute = row['attribute']
        if attribute in (1, 3):
            return 3
        if attribute == 2:
            return 4
        raise ValueError(f'Unsupported reference Transform attribute: attribute={attribute}')
    if row['type'] != 'Animator':
        raise ValueError(f'Unsupported reference binding type: type={row["type"]}')
    return 1


def read_clip_reference(source: dict[str, object], delta: object) -> ClipReference:
    """Require the exact serialized binding component domain for either branch."""
    width = sum(binding_width(binding) for binding in rows(source['bindings'], 'bindings'))
    references = rows(source['m_ValueArrayReferencePose'], 'm_ValueArrayReferencePose')
    if references:
        if len(references) != width:
            raise ValueError(f'Reference vector length differs from bindings: expected={width}, actual={len(references)}')
        return ClipReference(tuple(number(value, 'reference component') for value in references),
                             'serialized_value_array_reference_pose')
    values = rows(delta, 'm_ValueArrayDelta')
    if len(values) != width:
        raise ValueError(f'Empty-reference start vector length differs from bindings: expected={width}, actual={len(values)}')
    return ClipReference(tuple(number(object_row(value, 'ValueDelta')['m_Start'], 'ValueDelta.m_Start')
                               for value in values), 'native_empty_reference_value_delta_start')
