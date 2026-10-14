"""Apply verified ordinary additive channel math to an explicit layer output.

Scheduling, default selection and mask construction are caller responsibilities.
The native delta rotation scales xyz while retaining w, then normalizes; it does
not use an angle-power interpolation for an additive layer's fractional weight.
"""
from __future__ import annotations

import math

from animator_auxiliary_layers import LocalPose, multiply, unit
from animator_weighted_channels import MixedPose, float32


def apply_ordinary_additive_layer(initial: tuple[MixedPose, ...],
                                  child: tuple[MixedPose, ...],
                                  defaults: tuple[LocalPose, ...],
                                  weight: float) -> tuple[MixedPose, ...]:
    """Match the additive branch of native ordinary layer channel application."""
    if not initial or len(initial) != len(child) or len(initial) != len(defaults):
        raise ValueError('Ordinary layer initial/child/default dimensions must agree')
    if not math.isfinite(weight) or weight < 0:
        raise ValueError('Ordinary layer weight must be finite and nonnegative')
    if weight == 0:
        return initial
    weight = float32(weight)
    output: list[MixedPose] = []
    for prior, source, default in zip(initial, child, defaults, strict=True):
        prior_vectors = (prior.pose.position, prior.pose.rotation, prior.pose.scale)
        source_vectors = (source.pose.position, source.pose.rotation, source.pose.scale)
        default_vectors = (default.position, default.rotation, default.scale)
        vectors: list[tuple[float, ...]] = []
        for component, width in ((0, 3), (1, 4), (2, 3)):
            base = prior_vectors[component] if prior.enabled[component] else default_vectors[component]
            delta = source_vectors[component]
            if len(base) != width or len(delta) != width or any(not math.isfinite(value) for value in (*base, *delta)):
                raise ValueError('Ordinary layer channel has invalid dimensions or nonfinite values')
            if not source.enabled[component]:
                vectors.append(prior_vectors[component])
            elif component == 1:
                weighted = tuple(float32(float32(value) * weight) for value in delta[:3]) + (float32(delta[3]),)
                length = math.hypot(*weighted)
                if length == 0:
                    raise ValueError('Ordinary additive weighted quaternion has zero length')
                normalized = tuple(value / length for value in weighted)
                vectors.append(multiply(unit(tuple(float32(value) for value in base)), normalized))
            else:
                vectors.append(tuple(float32(float32(value) + float32(float32(change) * weight))
                    for value, change in zip(base, delta, strict=True)))
        enabled = tuple(a or b for a, b in zip(prior.enabled, source.enabled, strict=True))
        output.append(MixedPose(LocalPose(*vectors), enabled, prior.channel_weights))
    return tuple(output)
