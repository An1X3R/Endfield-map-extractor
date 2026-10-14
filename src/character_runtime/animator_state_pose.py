"""Consume explicit source state pose ports using verified ordinary layer kernels."""
from __future__ import annotations

from animator_auxiliary_layers import (AuxiliaryClip, LocalPose, number, object_row,
    pose_from_row, pose_to_row, sample_pose)
from animator_clip_delta import subtract_clip_reference
from animator_ordinary_source import ordinary_layer_mask
from animator_ordinary_direct import mix_ordinary_direct
from animator_ordinary_layers import apply_ordinary_additive_layer
from animator_state_schedule import StatePort
from animator_weighted_channels import MixedPose, WeightedPose


def evaluate_state_pose(base: list[object], hierarchy: list[object], layer: dict[str, object],
                        ports: tuple[StatePort, ...], clips: dict[int, AuxiliaryClip]) -> list[dict[str, object]]:
    """Mix source state deltas once, apply the actual mask, then the additive layer."""
    if not base or len(base) != len(hierarchy) or not ports:
        raise ValueError('Source state pose requires matching nonempty hierarchy/base and explicit ports')
    paths = tuple(str(object_row(row, 'hierarchy')['path']) for row in hierarchy)
    if len(set(paths)) != len(paths):
        raise ValueError('Source state hierarchy paths must be unique')
    allowed = ordinary_layer_mask(layer, paths)
    neutral = LocalPose((0.,)*3, (0.,0.,0.,1.), (0.,)*3)
    children: list[tuple[WeightedPose, ...]] = []
    for port in ports:
        if port.clip.index not in clips:
            raise ValueError(f'State port has no verified selected Avatar clip: index={port.clip.index}')
        clip = clips[port.clip.index]
        if len(set(clip.paths)) != len(clip.paths) or any(path not in paths for path in clip.paths):
            raise ValueError('State clip bindings are ambiguous or outside the exact selected hierarchy')
        if not clip.times[0] <= port.sample_seconds <= clip.times[-1] or clip.times[0] == clip.times[-1]:
            raise ValueError(f'State clock is outside the verified stored sample interval: clip={clip.index}, time={port.sample_seconds}, interval={(clip.times[0],clip.times[-1])}')
        sampled = sample_pose(clip, (port.sample_seconds-clip.times[0])/(clip.times[-1]-clip.times[0]))
        mapped = {path: (subtract_clip_reference(sample, reference, (True,True,has_scale)),has_scale)
            for path,sample,reference,has_scale in zip(clip.paths,sampled,clip.references,clip.has_scale,strict=True)}
        children.append(tuple(WeightedPose(mapped[path][0],port.weight,(True,True,mapped[path][1])) if path in mapped
            else WeightedPose(neutral,port.weight,(False,False,False)) for path in paths))
    mixed = mix_ordinary_direct(tuple(children),None,(neutral,)*len(paths))
    masked = tuple(MixedPose(row.pose,tuple(value and enable for value in row.enabled),row.channel_weights)
        for row,enable in zip(mixed.channels,allowed,strict=True))
    original = tuple(pose_from_row(row) for row in base)
    initial = tuple(MixedPose(pose,(True,True,True),(0.,)*3) for pose in original)
    output = apply_ordinary_additive_layer(initial,masked,original,number(layer['m_DefaultWeight'],'layer.weight'))
    return [pose_to_row(row.pose) for row in output]
