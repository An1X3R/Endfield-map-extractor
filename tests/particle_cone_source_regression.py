"""Verify cone-volume support against actual SceneProbe shape records."""
from __future__ import annotations

import argparse
import json
import math
from pathlib import Path
import random
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'src'))
from extractor_core.particle_snapshot import _shape


def inverse_rotation(vector: tuple[float, float, float], angles: tuple[float, float, float]) -> tuple[float, float, float]:
    x, y, z = vector
    rx, ry, rz = (-math.radians(value) for value in angles)
    x, z = math.cos(ry) * x + math.sin(ry) * z, -math.sin(ry) * x + math.cos(ry) * z
    y, z = math.cos(rx) * y - math.sin(rx) * z, math.sin(rx) * y + math.cos(rx) * z
    x, y = math.cos(rz) * x - math.sin(rz) * y, math.sin(rz) * x + math.cos(rz) * y
    return x, y, z


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--manifest', type=Path, required=True)
    parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args()
    manifest = json.loads(args.manifest.read_text(encoding='utf-8-sig'))
    records: list[dict[str, object]] = []
    for system in manifest['Particles']['Systems']:
        shape = system['Parameters']['Shape']
        if not shape['Enabled'] or shape['Type'] != 8:
            continue
        before = json.dumps(shape, sort_keys=True)
        radius, length = shape['Radius']['Value'], shape['Length']
        if shape['Angle'] != 0 or radius <= 0 or length <= 0:
            raise AssertionError('Real source fixture changed outside the cylindrical cone subset')
        angles = tuple(shape['Rotation'][axis] for axis in ('X', 'Y', 'Z'))
        rng = random.Random(143)
        samples = tuple(_shape(shape, rng, None) for _ in range(2048))
        rng = random.Random(143)
        if samples != tuple(_shape(shape, rng, None) for _ in range(2048)):
            raise AssertionError('Cone-volume sampling is not deterministic')
        heights: list[float] = []
        radius_squared: list[float] = []
        for position, direction, color in samples:
            relative = tuple(position[index] - shape['Position'][axis] for index, axis in enumerate(('X', 'Y', 'Z')))
            rotated = inverse_rotation(relative, angles)
            local = tuple(rotated[index] / shape['Scale'][axis] for index, axis in enumerate(('X', 'Y', 'Z')))
            normal = inverse_rotation(direction, angles)
            radial = math.hypot(local[0], local[1])
            if not radius * (1 - shape['RadiusThickness']) - 1e-12 <= radial <= radius + 1e-12:
                raise AssertionError('Source radial shell thickness was not preserved')
            if not -1e-12 <= local[2] <= length + 1e-12 or any(abs(a-b) > 1e-12 for a,b in zip(normal,(0,0,1))):
                raise AssertionError('Source length or rotated emission direction was not preserved')
            heights.append(local[2] / length)
            radius_squared.append((radial / radius) ** 2)
        expected_radial_mean = (1 + (1-shape['RadiusThickness'])**2) / 2
        if abs(sum(heights)/len(heights) - .5) > .03 or abs(sum(radius_squared)/len(radius_squared)-expected_radial_mean) > .03:
            raise AssertionError('Cone volume sampling has an unexpected distribution bias')
        if json.dumps(shape, sort_keys=True) != before:
            raise AssertionError('Sampling mutated the source shape')
        records.append({'asset':system['Asset'],'samples':len(samples),'radius':radius,'length':length})
    if not records:
        raise AssertionError('No actual cone-volume sources were exercised')
    args.output.write_text(json.dumps({'status':'passed','systems':records,
        'scope':'Deterministic geometric volume preview; Unity random sequence equivalence is not claimed.'},indent=2),encoding='utf-8')
    print('CONE_SOURCE_REGRESSION_PASSED', len(records), flush=True)


if __name__ == '__main__':
    main()
