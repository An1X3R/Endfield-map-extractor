"""Convert already bound ordinary clip channels to native additive deltas.

The verified leaf subtracts explicit clip references before Direct mixing.
This is not outer-layer additive application or a complete Animator evaluator.
"""
from __future__ import annotations

import math

from animator_auxiliary_layers import LocalPose, multiply, unit
from animator_weighted_channels import float32


def subtract_clip_reference(sample: LocalPose, reference: LocalPose,
                            enabled: tuple[bool, bool, bool]) -> LocalPose:
    """Match native ordinary reference subtraction, preserving disabled channels."""
    if len(enabled) != 3 or any(type(value) is not bool for value in enabled):
        raise ValueError('Clip reference subtraction requires three boolean channel flags')
    for pose in (sample, reference):
        for values, width in ((pose.position, 3), (pose.rotation, 4), (pose.scale, 3)):
            if len(values) != width or any(not math.isfinite(value) for value in values):
                raise ValueError('Clip reference subtraction requires finite T/Q/S vectors')
    position = sample.position
    rotation = sample.rotation
    scale = sample.scale
    if enabled[0]:
        position = tuple(float32(float32(value) - float32(origin))
                         for value, origin in zip(sample.position, reference.position, strict=True))
    if enabled[1]:
        inverse = tuple(-float32(value) for value in reference.rotation[:3]) + (float32(reference.rotation[3]),)
        rotation = multiply(unit(inverse), unit(tuple(float32(value) for value in sample.rotation)))
    if enabled[2]:
        scale = tuple(float32(float32(float32(value) - 1.) - float32(float32(origin) - 1.))
                      for value, origin in zip(sample.scale, reference.scale, strict=True))
    return LocalPose(position, rotation, scale)
