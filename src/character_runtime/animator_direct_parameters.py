"""Evaluate the verified source type-4 Direct weight/pose-time parameter step.

This covers the selected root's child parameters, not pose accumulation,
WriteDefaults, transitions, or captured runtime parameter values.
"""
from __future__ import annotations

from dataclasses import dataclass
import math

from animator_auxiliary_layers import object_row, rows
from animator_selected_avatar_bindings import integer
from animator_weighted_channels import float32


@dataclass(frozen=True)
class DirectSource:
    weight_ids: tuple[int, ...]
    pose_time_ids: tuple[int, ...]
    normalized: bool
    use_pose_time: bool


@dataclass(frozen=True)
class DirectParameters:
    child_weights: tuple[float, ...]
    pose_times: tuple[float, ...] | None


def read_direct_source(node: dict[str, object]) -> DirectSource:
    if node['m_BlendType'] != 4 or node['m_ClipID'] != 0xffffffff:
        raise ValueError('Source root must be an actual type-4 Direct branch')
    direct = object_row(object_row(node['m_BlendDirectData'], 'direct')['data'], 'direct.data')
    ids = tuple(integer(value, 'weight.id') for value in rows(direct['m_ChildBlendEventIDArray'], 'weight.ids'))
    clocks = tuple(integer(value, 'clock.id') for value in rows(direct['m_ChildPoseTimeEventIDArray'], 'clock.ids'))
    if not ids or len(ids) != len(clocks) or len(ids) != len(rows(node['m_ChildIndices'], 'children')):
        raise ValueError('Source Direct child, weight, and clock dimensions disagree')
    normalized, pose_time = direct['m_NormalizedBlendValues'], direct['m_UsePoseTimeValues']
    if not isinstance(normalized, bool) or not isinstance(pose_time, bool):
        raise ValueError('Source Direct normalized/pose-time fields must be boolean')
    return DirectSource(ids, clocks, normalized, pose_time)


def evaluate_direct_parameters(source: DirectSource, parameters: dict[int, float]) -> DirectParameters:
    if any(isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value) for value in parameters.values()):
        raise ValueError('Direct parameters require finite numeric scalars')
    raw = tuple(float32(parameters.get(key, 0.)) if key != 0xffffffff else 0. for key in source.weight_ids)
    total = 0.
    for value in raw:
        total = float32(total + value)
    weights = tuple(max(0., value) for value in raw)
    if source.normalized and total > 1.:
        weights = tuple(float32(value / total) for value in weights)
    times = None
    if source.use_pose_time:
        missing = tuple(key for key in source.pose_time_ids if key != 0xffffffff and key not in parameters)
        if missing:
            raise ValueError(f'Source Direct pose-time parameter is unresolved: ids={missing}')
        times = tuple(float32(parameters[key]) if key != 0xffffffff else 0. for key in source.pose_time_ids)
    return DirectParameters(weights, times)
