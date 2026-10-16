"""Admit only source-proven constant alpha for an unbound Fur direction texture."""
from __future__ import annotations

import math


def null_fur_direction_alpha(default_name: str, texture_dimension: int, map_enable: float) -> float:
    """Use the opaque alpha of Unity's bump default when directional flow is disabled."""
    if default_name != 'bump' or texture_dimension != 2:
        raise ValueError(f'Null FurDirMap requires its exact source bump/2D default: default={default_name}; dimension={texture_dimension}')
    if not math.isfinite(map_enable) or map_enable != 0.0:
        raise ValueError(f'Null FurDirMap requires explicitly disabled directional flow: _FurDirMapEnable={map_enable}')
    return 1.0
