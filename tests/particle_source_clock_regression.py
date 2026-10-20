"""Validate playback speed using actual SceneProbe particle system records."""
from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'src'))

from extractor_core.particle_snapshot import (
    ParticleSnapshotError, UnsupportedParticleSnapshot, _record, _sequence,
    generate_particle_snapshot,
)
from extractor_core.particle_mesh_geometry import load_particle_shape_geometry


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--manifest', type=Path, required=True)
    parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args()
    source = args.manifest.read_bytes()
    manifest = _record(json.loads(source), 'Manifest')
    particles = _record(manifest['Particles'], 'Manifest.Particles')
    systems = _sequence(particles['Systems'], 'Particles.Systems')
    passed: list[dict[str, object]] = []
    unsupported: list[dict[str, object]] = []
    for raw in systems:
        system = _record(raw, 'System')
        asset = _record(system['Asset'], 'System.Asset')
        identity = f"{asset['Source']}:{asset['PathId']}"
        source_before = json.dumps(system, sort_keys=True)
        base = {**system, 'SimulationSpeed': 1.0}
        shape_geometry = load_particle_shape_geometry(manifest, args.manifest, system)
        try:
            counts: list[int] = []
            for speed in (0.0, 0.5, 1.0, 2.0):
                changed = {**system, 'SimulationSpeed': speed}
                for seconds in (0.0, 0.1, 0.25, 0.49, 0.5, 1.0, 2.0, 4.0):
                    actual = generate_particle_snapshot(changed, seconds, identity, 100000, 100000, (0.0, -9.81, 0.0), shape_geometry)
                    expected = generate_particle_snapshot(base, seconds * speed, identity, 100000, 100000, (0.0, -9.81, 0.0), shape_geometry)
                    if actual != expected:
                        raise AssertionError(f'Playback speed changed more than simulation time: {identity}, speed={speed}, seconds={seconds}')
                    counts.append(len(actual.particles))
        except UnsupportedParticleSnapshot as error:
            unsupported.append({'source': identity, 'reason': str(error)})
            continue
        for speed in (-1.0, float('nan'), float('inf')):
            try:
                generate_particle_snapshot({**system, 'SimulationSpeed': speed}, 1.0, identity, 100000, 100000, (0.0, -9.81, 0.0), shape_geometry)
            except ParticleSnapshotError:
                pass
            else:
                raise AssertionError(f'Invalid speed accepted: {speed}')
        if json.dumps(system, sort_keys=True) != source_before:
            raise AssertionError(f'Source record was modified: {identity}')
        passed.append({'source': identity, 'authored_speed': system['SimulationSpeed'],
                       'maximum_alive': max(counts), 'comparisons': len(counts)})
    if not passed or not any(row['maximum_alive'] > 0 for row in passed):
        raise AssertionError('No nonempty real particle source exercised playback speed')
    args.output.parent.mkdir(parents=True, exist_ok=True)
    with args.output.open('x', encoding='utf-8') as stream:
        json.dump({'status': 'passed', 'source': str(args.manifest),
                   'source_sha256': hashlib.sha256(source).hexdigest(),
                   'passed': passed, 'unsupported': unsupported,
                   'scope': 'Constant authored playback speed scales emission and aging; no Unity solver parity claim'}, stream, indent=2)


if __name__ == '__main__':
    main()
