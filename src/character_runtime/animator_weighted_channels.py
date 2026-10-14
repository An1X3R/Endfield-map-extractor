"""Pure kernels for verified native weighted channels, not a live Animator graph.

The native caller selects individual root T/Q/S indices and scalar channels.
These kernels do not infer bindings, WriteDefaults, node weights, or state clocks.
"""
from __future__ import annotations

from dataclasses import dataclass
import math
import struct

from animator_auxiliary_layers import LocalPose


@dataclass(frozen=True)
class WeightedPose:
    pose: LocalPose
    weight: float
    enabled: tuple[bool, bool, bool]


@dataclass(frozen=True)
class MixedPose:
    pose: LocalPose
    enabled: tuple[bool, bool, bool]
    channel_weights: tuple[float, float, float]


def float32(value: float) -> float:
    if not math.isfinite(value):
        raise ValueError(f'Weighted channel value must be finite: value={value}')
    return struct.unpack('<f', struct.pack('<f', value))[0]


def weighted_sum(values: tuple[float, ...], weights: tuple[float, ...]) -> float:
    if len(values) != len(weights):
        raise ValueError('Weighted channel dimensions disagree')
    result = 0.0
    for value, weight in zip(values, weights, strict=True):
        if weight < 0:
            raise ValueError(f'Native active child weights must be nonnegative: weight={weight}')
        result = float32(result + float32(float32(value) * float32(weight)))
    return result


def hemisphere_sign(left: tuple[float, ...], right: tuple[float, ...]) -> float:
    products = tuple(float32(a * b) for a, b in zip(left, right, strict=True))
    dot = float32(float32(products[0] + products[1]) + float32(products[2] + products[3]))
    return -1.0 if dot < 0 else 1.0


def mix_selected_pose(contributions: tuple[WeightedPose, ...], default: LocalPose | None) -> MixedPose:
    """Match the indexed T/Q/S accumulator and residual-default finalizer."""
    if any(len(row.enabled) != 3 for row in contributions):
        raise ValueError('Selected pose requires three channel enable values')
    if default is not None:
        for component, vector, width in ((0, default.position, 3), (1, default.rotation, 4), (2, default.scale, 3)):
            if len(vector) != width or any(not math.isfinite(value) for value in vector):
                raise ValueError(f'Invalid default channel vector: component={component}, width={len(vector)}')
    vectors: list[tuple[float, ...]] = []
    active: list[bool] = []
    totals: list[float] = []
    for component, width in ((0, 3), (1, 4), (2, 3)):
        selected = tuple(row for row in contributions if row.enabled[component] and row.weight > 0)
        for row in contributions:
            if not math.isfinite(row.weight) or row.weight < 0:
                raise ValueError(f'Native active child weight is invalid: weight={row.weight}')
            actual = (row.pose.position, row.pose.rotation, row.pose.scale)[component]
            if len(actual) != width or any(not math.isfinite(value) for value in actual):
                raise ValueError(f'Invalid channel vector: component={component}, width={len(actual)}')
        total = weighted_sum((1.0,) * len(selected), tuple(row.weight for row in selected))
        value = (0.0,) * width
        for row in selected:
            source = (row.pose.position, row.pose.rotation, row.pose.scale)[component]
            sign = hemisphere_sign(value, source) if component == 1 else 1.0
            value = tuple(float32(a + sign * float32(float32(b) * float32(row.weight)))
                          for a, b in zip(value, source, strict=True))
        if selected and default is not None and total < 1.0:
            source = (default.position, default.rotation, default.scale)[component]
            sign = hemisphere_sign(value, source) if component == 1 else 1.0
            residual = float32(1.0 - total)
            value = tuple(float32(a + sign * float32(float32(b) * residual))
                          for a, b in zip(value, source, strict=True))
        if selected and component == 1:
            length = math.hypot(*value)
            if length == 0:
                raise ValueError('Active quaternion accumulation has zero length')
            value = tuple(item / length for item in value)
        vectors.append(value)
        active.append(bool(selected))
        totals.append(total)
    return MixedPose(LocalPose(vectors[0], vectors[1], vectors[2]),
                     (active[0], active[1], active[2]), (totals[0], totals[1], totals[2]))


def mix_scalar(values: tuple[float, ...], weights: tuple[float, ...], default: float | None) -> float:
    """Accumulate active scalar inputs and fill only the remaining weight below one."""
    if len(values) != len(weights):
        raise ValueError('Weighted scalar dimensions disagree')
    weighted_sum(values, weights)
    active = tuple((value, weight) for value, weight in zip(values, weights, strict=True) if weight > 0)
    values = tuple(value for value, _ in active)
    weights = tuple(weight for _, weight in active)
    total = weighted_sum((1.0,) * len(weights), weights)
    result = weighted_sum(values, weights)
    if default is not None and weights and total < 1.0:
        result = float32(result + float32(float32(1.0 - total) * float32(default)))
    return result
