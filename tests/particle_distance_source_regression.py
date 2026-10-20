"""Check fixed-emitter distance semantics against actual SceneProbe records."""
from __future__ import annotations

import argparse
import hashlib
import json
import math
from pathlib import Path
import random
import sys
from collections.abc import Mapping

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'src'))

from extractor_core.particle_curves import parse_minmax_curve
from extractor_core.particle_mesh_geometry import load_particle_shape_geometry
from extractor_core.particle_snapshot import (
    UnsupportedParticleSnapshot, _birth_times, _fixed_emitter_birth_times, _integer,
    _number, _record, _sequence, generate_fixed_emitter_particle_snapshot,
    generate_particle_snapshot,
)


def expected_constant_births(system: Mapping[str, object], age: float) -> tuple[float, ...]:
    """Calculate the closed-form schedule from the active source constants."""
    parameters = _record(system['Parameters'], 'Parameters')
    emission = _record(parameters['Emission'], 'Emission')
    rate = parse_minmax_curve(emission['RateOverTime'], 'Emission.RateOverTime')
    delay = parse_minmax_curve(system['StartDelay'], 'System.StartDelay')
    if rate.mode != 0 or delay.mode != 0 or delay.scalar != 0 or system['Looping'] is not True:
        raise AssertionError('Source is outside the constant looping schedule regression subset')
    length = _number(system['LengthInSec'], 'System.LengthInSec')
    births = [time for index in range(1, math.floor(age * rate.scalar) + 2)
              if (time := index / rate.scalar) <= age] if rate.scalar else []
    count = _integer(emission['BurstCount'], 'Emission.BurstCount')
    for cycle in range(math.floor(age / length) + 1):
        for value in _sequence(emission['Bursts'], 'Emission.Bursts')[:count]:
            burst = _record(value, 'Burst')
            curve = parse_minmax_curve(burst['CountCurve'], 'Burst.CountCurve')
            if curve.mode != 0 or burst['Probability'] != 1 or burst['CycleCount'] != 1:
                raise AssertionError('Source burst is outside the constant certain single-cycle regression subset')
            time = cycle * length + _number(burst['Time'], 'Burst.Time')
            if time <= age:
                births.extend([time] * math.floor(curve.scalar))
    return tuple(sorted(births))


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--manifest', type=Path, action='append', required=True)
    parser.add_argument('--expected-distance-systems', type=int, required=True)
    parser.add_argument('--boundary-system-path-id', type=int, required=True)
    parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args()
    sources: list[dict[str, object]] = []
    distance_rows: list[dict[str, object]] = []
    snapshots: list[dict[str, object]] = []
    comparisons = 0
    boundary_checks = 0
    for path in args.manifest:
        source = path.read_bytes()
        manifest = _record(json.loads(source), 'Manifest')
        before = json.dumps(manifest, sort_keys=True)
        sources.append({'manifest': str(path), 'sha256': hashlib.sha256(source).hexdigest()})
        particles = _record(manifest['Particles'], 'Particles')
        for value in _sequence(particles['Systems'], 'Particles.Systems'):
            system = _record(value, 'System')
            asset = _record(system['Asset'], 'System.Asset')
            identity = f"{asset['Source']}:{asset['PathId']}"
            parameters = _record(system['Parameters'], 'Parameters')
            emission = _record(parameters['Emission'], 'Emission')
            distance = parse_minmax_curve(emission['RateOverDistance'], 'Emission.RateOverDistance')
            if distance.mode != 0:
                raise AssertionError(f'Unexpected real distance curve mode: {identity}')
            if distance.scalar:
                try:
                    _birth_times(system, emission, 0.731, 100000, random.Random(13))
                except UnsupportedParticleSnapshot as error:
                    if str(error) != 'RateOverDistance needs an emitter path':
                        raise
                else:
                    raise AssertionError(f'Strict schedule accepted missing emitter history: {identity}')
                samples: list[dict[str, object]] = []
                for seconds in (0.0, 0.123, 0.357, 0.731, 1.0 - 1e-9,
                                math.nextafter(1.0, -math.inf), 1.0, math.nextafter(1.0, math.inf),
                                1.0 + 1e-9, 1.137, math.nextafter(1.5, -math.inf), 1.5,
                                math.nextafter(1.5, math.inf), 1.777):
                    age = seconds * _number(system['SimulationSpeed'], 'System.SimulationSpeed')
                    births = _fixed_emitter_birth_times(system, emission, age, 100000, random.Random(13))
                    repeated = _fixed_emitter_birth_times(system, emission, age, 100000, random.Random(13))
                    expected = expected_constant_births(system, age)
                    if births != repeated or len(births) != len(expected) or any(
                            not math.isclose(left, right, rel_tol=0.0, abs_tol=1e-12)
                            for left, right in zip(births, expected)):
                        raise AssertionError(f'Source time/burst schedule changed: {identity}, seconds={seconds}')
                    comparisons += 1
                    samples.append({'seconds': seconds, 'births': len(births), 'distance_births': 0})
                if asset['PathId'] == args.boundary_system_path_id:
                    rate = parse_minmax_curve(emission['RateOverTime'], 'Emission.RateOverTime')
                    if rate.mode != 0 or rate.scalar != 15 or system['SimulationSpeed'] != 1:
                        raise AssertionError('The actual 15/s boundary regression source changed')
                    boundary_cases = ((0.0, 0), (math.nextafter(1.0 / 15.0, -math.inf), 0),
                                      (1.0 / 15.0, 1), (math.nextafter(1.0, -math.inf), 14),
                                      (1.0, 15), (math.nextafter(1.0, math.inf), 15), (1.5, 22))
                    complete = _fixed_emitter_birth_times(system, emission, 2.0, 100000, random.Random(13))
                    for seconds, expected_count in boundary_cases:
                        actual = _fixed_emitter_birth_times(system, emission, seconds, 100000, random.Random(13))
                        if len(actual) != expected_count or actual != tuple(time for time in complete if time <= seconds):
                            raise AssertionError(f'Actual 15/s boundary or temporal prefix failed: seconds={seconds}, births={actual}')
                        boundary_checks += 1
                distance_rows.append({'identity': identity, 'emission': dict(emission), 'samples': samples})
            else:
                for seconds in (0.0, math.nextafter(1.0, -math.inf), 1.0, math.nextafter(1.0, math.inf), 1.5):
                    strict = _birth_times(system, emission, seconds, 100000, random.Random(13))
                    fixed = _fixed_emitter_birth_times(system, emission, seconds, 100000, random.Random(13))
                    if strict != fixed:
                        raise AssertionError(f'Zero-distance source schedule changed: {identity}')
                    comparisons += 1
            geometry = load_particle_shape_geometry(manifest, path, system)
            try:
                snapshot = generate_fixed_emitter_particle_snapshot(
                    system, 0.731, identity, 100000, 100000, (0.0, -9.81, 0.0), geometry)
            except UnsupportedParticleSnapshot as error:
                if 'RateOverDistance' in str(error) or 'distance rate' in str(error):
                    raise AssertionError(f'Fixed emitter still rejected source distance configuration: {identity}') from error
                snapshots.append({'identity': identity, 'status': 'unsupported', 'reason': str(error)})
            else:
                marker = 'EmissionModule.RateOverDistance_zero_fixed_emitter_displacement'
                if (marker in snapshot.unsupported_modules) != (distance.scalar != 0):
                    raise AssertionError(f'Fixed distance limitation is inaccurate: {identity}')
                if distance.scalar:
                    try:
                        generate_particle_snapshot(system, 0.731, identity, 100000, 100000,
                                                   (0.0, -9.81, 0.0), geometry)
                    except UnsupportedParticleSnapshot:
                        pass
                    else:
                        raise AssertionError(f'Strict snapshot accepted missing emitter history: {identity}')
                snapshots.append({'identity': identity, 'status': 'passed', 'alive': len(snapshot.particles),
                                  'scope': snapshot.preview_scope, 'limitations': snapshot.unsupported_modules})
        if json.dumps(manifest, sort_keys=True) != before or path.read_bytes() != source:
            raise AssertionError(f'Regression mutated source data: {path}')
    if len(distance_rows) != args.expected_distance_systems:
        raise AssertionError(f'Distance source coverage changed: actual={len(distance_rows)}, expected={args.expected_distance_systems}')
    if boundary_checks != 7:
        raise AssertionError(f'The actual 15/s boundary source was not tested once: checks={boundary_checks}')
    result = {'status': 'passed', 'sources': sources, 'schedule_comparisons': comparisons,
              'exact_boundary_checks': boundary_checks,
              'distance_systems': distance_rows, 'snapshots': snapshots,
              'scope': 'deterministic fixed emitter source preview; displacement and distance births are zero; no Unity motion history'}
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding='utf-8')
    print(json.dumps({'status': 'passed', 'distance_systems': len(distance_rows),
                      'schedule_comparisons': comparisons, 'snapshots': len(snapshots)}))


if __name__ == '__main__':
    main()
