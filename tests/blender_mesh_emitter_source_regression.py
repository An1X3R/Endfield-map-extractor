"""Compare real mesh-emitter static geometry with its animation samplers."""
from __future__ import annotations

import argparse
from collections.abc import Mapping
import json
from pathlib import Path
import sys

import bpy

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'src'))

from blender_export_effect_animation import _build_samplers, _source_objects
from blender_scene_effect_materials import make_effect_material
from blender_scene_effects import build_scene_effects
from endfield_scene_material_contract import load_catalog, reference_key
from extractor_core.particle_snapshot import _record, _sequence


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--manifest', type=Path, required=True)
    parser.add_argument('--output', type=Path, required=True)
    parser.add_argument('--expected-systems', type=int, required=True)
    args = parser.parse_args(sys.argv[sys.argv.index('--') + 1:])
    manifest = _record(json.loads(args.manifest.read_text(encoding='utf-8-sig')), 'Manifest')
    particle_records = _record(manifest['Particles'], 'Particles')
    systems = [_record(item, 'System') for item in _sequence(particle_records['Systems'], 'Systems')]
    mesh_sources = {_record(system['Asset'], 'Asset')['Source'] for system in systems
                    if _record(_record(system['Parameters'], 'Parameters')['Shape'], 'Shape')['Type'] == 6}
    roots = [_record(_record(item, 'Container')['Asset'], 'Asset') for item in _sequence(manifest['Containers'], 'Containers')
             if _record(_record(item, 'Container')['Asset'], 'Asset')['Source'] in mesh_sources
             and _record(_record(item, 'Container')['Asset'], 'Asset')['Type'] == 'GameObject']
    if len(roots) != args.expected_systems:
        raise AssertionError(f'Unexpected real mesh-emitter prefab count: {len(roots)}')
    identity = [float(row == column) for column in range(4) for row in range(4)]
    instances = [{'entity_id': index + 1, 'matrix_space': 'dynamic_effect_anchor', 'matrix': identity,
                  'scene_manifest': str(args.manifest.resolve()), 'prefab_root': root}
                 for index, root in enumerate(roots)]
    material_records, textures = load_catalog((args.manifest,))
    camera = bpy.data.objects.new('Mesh Emitter Source Camera', bpy.data.cameras.new('Mesh Emitter Source Camera'))
    bpy.context.scene.collection.objects.link(camera)
    camera.location = (0.0, -3.0, 1.0)
    collection = bpy.data.collections.new('Real Mesh Emitter Regression')
    bpy.context.scene.collection.children.link(collection)
    bpy.context.view_layer.update()

    def material_factory(reference: Mapping[str, object], renderer: Mapping[str, object],
                         system: Mapping[str, object]) -> bpy.types.Material:
        streams = _sequence(renderer['VertexStreams'], 'Renderer.VertexStreams')
        if any(type(stream) is not int for stream in streams):
            raise TypeError('Real renderer vertex streams must contain integers')
        return make_effect_material(material_records[reference_key(reference)], textures, 0.5, tuple(streams))

    static_audit = build_scene_effects(instances, collection, material_factory, 0.5, camera,
                                      args.expected_systems, 10000, (0.0, -9.81, 0.0))
    sources, trails = _source_objects()
    samplers, materials, animated, skipped = _build_samplers({'effect_instances': instances}, camera, sources,
                                                            trails, 60.0, 30, (0.0, -9.81, 0.0))
    if len(samplers) != args.expected_systems or skipped or len(static_audit) != args.expected_systems:
        raise AssertionError(f'Real mesh-emitter integration was skipped: static={static_audit}, animated={animated}, skipped={skipped}')
    comparisons = []
    for sampler in samplers:
        source = sampler.object.data
        sampled = sampler.sample(0.5)
        if len(source.vertices) != len(sampled.vertices) or len(source.polygons) != len(sampled.polygons):
            raise AssertionError(f'Static and animation mesh-emitter topology differ: {sampler.object.name}')
        error = max((left.co - right.co).length for left, right in zip(source.vertices, sampled.vertices, strict=True))
        source_colors = source.color_attributes['EF_FX_COLOR']
        sampled_colors = sampled.color_attributes['EF_FX_COLOR']
        color_error = max(abs(left - right) for one, two in zip(source_colors.data, sampled_colors.data, strict=True)
                          for left, right in zip(one.color, two.color, strict=True))
        if error > 1e-7 or color_error > 1e-7:
            raise AssertionError(f'Static and animation mesh-emitter output differ: object={sampler.object.name}, position={error}, color={color_error}')
        later = sampler.sample(1.0)
        if not later.polygons or len(later.polygons) == len(source.polygons):
            raise AssertionError(f'Real emission schedule did not advance: object={sampler.object.name}')
        comparisons.append({'object': sampler.object.name, 'static_particles': len(source.polygons),
                            'later_particles': len(later.polygons), 'position_error': error,
                            'color_error': color_error, 'scope': sampler.object['endfield_fx_preview_scope']})
        bpy.data.meshes.remove(sampled)
        bpy.data.meshes.remove(later)
    moving = samplers[0].object
    moving.animation_data_create()
    try:
        _build_samplers({'effect_instances': instances}, camera, sources, trails,
                        60.0, 30, (0.0, -9.81, 0.0))
    except ValueError as error:
        if 'fixed, unparented emitter' not in str(error):
            raise
    else:
        raise AssertionError('Animated world-space emitter was accepted by the fixed preview entry')
    finally:
        moving.animation_data_clear()
    args.output.parent.mkdir(parents=True, exist_ok=True)
    with args.output.open('x', encoding='utf-8') as stream:
        json.dump({'status': 'passed', 'static_audit': static_audit, 'animation_audit': animated,
                   'comparisons': comparisons, 'source_materials': sorted(materials),
                   'animated_world_emitter_rejected': True,
                   'scope': 'Real source materials and fixed-emitter static/animation sampling; no render or moving-emitter history claim'}, stream, indent=2)


if __name__ == '__main__':
    main()
