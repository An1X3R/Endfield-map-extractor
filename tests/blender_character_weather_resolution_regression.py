"""Compare weather resolution identities and timings on a real loaded character."""
from __future__ import annotations

import argparse
from collections.abc import Callable
import importlib.util
import json
from pathlib import Path
import statistics
import sys
import time
from typing import Protocol, TypedDict, cast

import bpy

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'src' / 'character_runtime'))
import character_weather_panel as current


class WeatherModule(Protocol):
    weather_groups: Callable[[bpy.types.Material], tuple[bpy.types.NodeTree, ...]]
    weather_socket: Callable[[bpy.types.NodeTree, str], bpy.types.NodeSocket]


class SocketIdentity(TypedDict):
    label: str
    pointer: int
    value: float


class GroupIdentity(TypedDict):
    material: str
    group: str
    pointer: int
    sockets: tuple[SocketIdentity, ...]


class ResolutionTiming(TypedDict):
    cold_ms: float
    repeated_median_ms: float
    repeated_min_ms: float
    repeated_max_ms: float


def load_reference(path: Path) -> WeatherModule:
    spec = importlib.util.spec_from_file_location('weather_resolution_reference', path)
    if spec is None or spec.loader is None:
        raise ImportError(f'Could not load reference weather panel: {path}')
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return cast(WeatherModule, module)


def resolve(module: WeatherModule, materials: tuple[bpy.types.Material, ...]) -> tuple[GroupIdentity, ...]:
    groups: list[GroupIdentity] = []
    for material in materials:
        for tree in module.weather_groups(material):
            labels = {node.label for node in tree.nodes if node.bl_idname == 'ShaderNodeValue'}
            sockets: list[SocketIdentity] = []
            for label, _title in current.CONTROL_ROWS:
                if label in labels:
                    socket = module.weather_socket(tree, label)
                    sockets.append({'label': label, 'pointer': socket.as_pointer(),
                                    'value': float(socket.default_value)})
            groups.append({'material': material.name, 'group': tree.name,
                           'pointer': tree.as_pointer(), 'sockets': tuple(sockets)})
    return tuple(groups)


def measure(module: WeatherModule, materials: tuple[bpy.types.Material, ...]) -> ResolutionTiming:
    samples: list[float] = []
    for _ in range(11):
        started = time.perf_counter()
        for material in materials:
            module.weather_groups(material)
        samples.append((time.perf_counter() - started) * 1000.0)
    return {'cold_ms': samples[0], 'repeated_median_ms': statistics.median(samples[1:]),
            'repeated_min_ms': min(samples[1:]), 'repeated_max_ms': max(samples[1:])}


def main(arguments: list[str]) -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument('--baseline-panel', type=Path, required=True)
    parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args(arguments)
    if not bpy.data.filepath:
        raise ValueError('Load a real exported character blend before running this regression')
    reference = load_reference(args.baseline_panel.resolve())
    materials = tuple(material for material in bpy.data.materials if material.use_nodes)
    reference_timing = measure(reference, materials)
    current_module = cast(WeatherModule, current)
    current_timing = measure(current_module, materials)
    before = resolve(reference, materials)
    after = resolve(current_module, materials)
    assert before, 'The real fixture must expose weather controls'
    assert before == after, 'Weather group, socket identity or scalar value changed'
    document = {'status': 'passed', 'blender': bpy.app.version_string, 'blend': bpy.data.filepath,
                'material_count': len(materials), 'resolved_material_groups': len(after),
                'socket_count': sum(len(group['sockets']) for group in after),
                'group_and_socket_identities_unchanged': True, 'scalar_values_unchanged': True,
                'baseline_timing': reference_timing, 'current_timing': current_timing,
                'groups': after, 'scene_saved': False}
    args.output.resolve().write_text(json.dumps(document, ensure_ascii=False, indent=2), encoding='utf-8')
    print(json.dumps(document, ensure_ascii=False), flush=True)


if __name__ == '__main__':
    main(sys.argv[sys.argv.index('--') + 1:])
