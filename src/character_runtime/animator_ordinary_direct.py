"""Mix ordinary local T/Q/S child buffers using verified native dispatch order.

Inputs are already sampled and bound child poses. Clip reference conversion,
layer additive application, Humanoid channels, transitions and WriteDefaults
selection remain separate. An explicit default is never inferred from time zero.
"""
from __future__ import annotations

from dataclasses import dataclass
import math

from animator_auxiliary_layers import LocalPose
from animator_weighted_channels import MixedPose, WeightedPose, float32, mix_selected_pose


@dataclass(frozen=True)
class OrdinaryPoseBuffer:
    channels: tuple[MixedPose, ...]
    active_child_indices: tuple[int, ...]
    unit_child_bypass: bool


def mix_ordinary_direct(inputs: tuple[tuple[WeightedPose, ...], ...],
                        defaults: tuple[LocalPose, ...] | None,
                        initial: tuple[LocalPose, ...]) -> OrdinaryPoseBuffer:
    """Match positive port filtering, the unit-child bypass and bulk accumulation."""
    count = len(initial)
    if count == 0 or any(len(child) != count for child in inputs) or (defaults is not None and len(defaults) != count):
        raise ValueError('Ordinary Direct child/default/initial dimensions must agree and be nonempty')
    for child in inputs:
        weight = child[0].weight
        if not math.isfinite(weight) or any(row.weight != weight for row in child):
            raise ValueError('Ordinary Direct requires one finite uniform weight per child port')
        for row in child:
            if len(row.enabled) != 3 or any(type(value) is not bool for value in row.enabled):
                raise ValueError('Ordinary Direct requires three boolean channel flags')
            for values, width in ((row.pose.position, 3), (row.pose.rotation, 4), (row.pose.scale, 3)):
                if len(values) != width or any(not math.isfinite(value) for value in values):
                    raise ValueError('Ordinary Direct source vector has invalid width or nonfinite values')
    active = tuple(index for index, child in enumerate(inputs) if float32(child[0].weight) > 0.)
    bypass = len(active) == 1 and float32(inputs[active[0]][0].weight) == 1.
    if bypass:
        channels = tuple(MixedPose(row.pose, row.enabled, (0., 0., 0.)) for row in inputs[active[0]])
        return OrdinaryPoseBuffer(channels, active, True)
    channels: list[MixedPose] = []
    for index, original in enumerate(initial):
        contributions = tuple(inputs[child][index] for child in active)
        mixed = mix_selected_pose(contributions, defaults[index] if defaults is not None else None)
        vectors = tuple(candidate if enabled else retained for candidate, retained, enabled in zip(
            (mixed.pose.position, mixed.pose.rotation, mixed.pose.scale),
            (original.position, original.rotation, original.scale), mixed.enabled, strict=True))
        channels.append(MixedPose(LocalPose(*vectors), mixed.enabled, mixed.channel_weights))
    return OrdinaryPoseBuffer(tuple(channels), active, False)
