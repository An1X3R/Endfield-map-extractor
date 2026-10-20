"""Exercise real no-soft particle systems through the existing material factory."""
from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
import sys

import bpy
from mathutils import Matrix

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'src'))
from blender_export_effect_animation import _particle_sample, _snapshot
from blender_scene_effects import _components, _mesh_templates, _particle_mesh_instances, _particle_mesh_rotations
from blender_scene_effect_materials import make_effect_material
from blender_scene_effect_vertex_offset import apply_vertex_offset_research_preview, validate_vertex_offset_source
from endfield_scene_material_contract import load_catalog, reference_key
from extractor_core.particle_mesh_geometry import load_particle_shape_geometry


def coordinates_error(left: bpy.types.Mesh, right: bpy.types.Mesh) -> float:
    if len(left.vertices) != len(right.vertices) or not left.vertices:
        raise AssertionError('Real particle meshes must have matching nonempty vertices')
    return max((a.co - b.co).length for a, b in zip(left.vertices, right.vertices, strict=True))


def mesh_signature(mesh: bpy.types.Mesh) -> str:
    return repr((tuple(tuple(vertex.co) for vertex in mesh.vertices),
                 tuple(tuple(polygon.vertices) for polygon in mesh.polygons),
                 tuple((layer.name, tuple(tuple(item.uv) for item in layer.data)) for layer in mesh.uv_layers)))


def main(manifest_path: Path, effect_name: str, expected_systems: int,
         ages: tuple[float, ...], output: Path) -> None:
    output.mkdir(parents=True, exist_ok=False)
    original = manifest_path.read_bytes()
    manifest = json.loads(original)
    roots = tuple(row for row in manifest['Nodes'] if row['IsRoot'] and row['Name'] == effect_name)
    if len(roots) != 1:
        raise ValueError(f'Effect root must resolve once: name={effect_name}; count={len(roots)}')
    root = roots[0]
    _, pairs = _components(manifest, (root['Source'], root['PathId']))
    if len(pairs) != expected_systems:
        raise AssertionError(f'Actual source system coverage changed: expected={expected_systems}; actual={len(pairs)}')
    materials, textures = load_catalog((manifest_path,))
    source_state = repr(materials)
    camera_data = bpy.data.cameras.new('Explicit offset regression camera')
    camera = bpy.data.objects.new('Explicit offset regression camera', camera_data)
    bpy.context.scene.collection.objects.link(camera)
    camera.location = (2.0, -4.0, 2.0)
    bpy.context.view_layer.update()
    rows: list[dict[str, object]] = []
    empty_samples: list[dict[str, object]] = []
    for index, (system, renderer, _) in enumerate(pairs):
        if renderer['RenderMode'] != 4 or len(renderer['Materials']) != 1:
            raise ValueError('No-soft regression requires real mesh particles with one source material')
        record = materials[reference_key(renderer['Materials'][0]['Target'])]
        if set(record['RenderState']['ValidKeywords']) != {'_SAMPLE_TEX0', '_SAMPLE_TEX1', '_USE_VERTOFFSET'}:
            raise AssertionError('Real source material does not match the verified no-soft keyword profile')
        reference, sampler = validate_vertex_offset_source(record)
        weighted = {**record, 'Floats': {**record['Floats'], '_UseVertexColorAlphaAsVertOffsetWeight': 1.0}}
        try:
            validate_vertex_offset_source(weighted)
        except NotImplementedError as error:
            if 'color-alpha weighting' not in str(error):
                raise
        else:
            raise AssertionError('Unimplemented source vertex-color weighting was accepted')
        templates = _mesh_templates(manifest, manifest_path, renderer, {})
        template_state = tuple(mesh_signature(mesh) for slot in templates for mesh in slot)
        geometry = load_particle_shape_geometry(manifest, manifest_path, system)
        image_path, image_name = textures[reference_key(reference)]
        image = bpy.data.images.load(str(image_path / image_name), check_existing=False)
        image.colorspace_settings.name = 'Non-Color'
        live_samples = 0
        for age in ages:
            identity = f'1:{system["Asset"]["Source"]}:{system["Asset"]["PathId"]}'
            snapshot = _snapshot(system, age, identity, (0.0, -9.81, 0.0), geometry)
            if not snapshot.particles:
                empty_samples.append({'system': system['Asset'], 'age': age,
                                      'status': 'source_snapshot_has_no_live_particles'})
                continue
            live_samples += 1
            base = _particle_mesh_instances(f'Base{index}_{age}', snapshot.particles, templates,
                _particle_mesh_rotations(snapshot.particles, renderer['RenderAlignment']))
            state = mesh_signature(base)
            context = (image, sampler, reference['Source'], reference['PathId'],
                       Matrix.Identity(4), camera.matrix_world.translation, age)
            displaced = apply_vertex_offset_research_preview(record, base, *context, 0.0)
            gated = apply_vertex_offset_research_preview(record, base, *context, 1.0)
            material = make_effect_material(record, textures, age, tuple(renderer['VertexStreams']))
            actual = _particle_sample(system, renderer, Matrix.Identity(4), camera, templates, identity,
                (0.0, -9.81, 0.0), f'Factory{index}_{age}', material, geometry, age)
            factory_error = coordinates_error(displaced, actual)
            displacement = coordinates_error(displaced, base)
            if factory_error > 1e-6 or displacement <= 1e-6 or coordinates_error(gated, base) != 0:
                raise AssertionError(f'Real no-soft displacement/factory/pass gate mismatch: system={identity}; age={age}; factory_error={factory_error}; displacement={displacement}')
            if state != mesh_signature(base):
                raise AssertionError('Source particle mesh was mutated')
            actual.materials.append(material)
            obj = bpy.data.objects.new(f'System{index}Age{age}', actual)
            bpy.context.scene.collection.objects.link(obj)
            obj['endfield_nosoft_regression_sample'] = True
            obj.hide_render = age != ages[-1]
            rows.append({'system': system['Asset'], 'age': age, 'particles': len(snapshot.particles),
                         'vertices': len(actual.vertices), 'source_material': [record['Source'], record['PathId']],
                         'max_displacement_m': displacement, 'factory_error_m': factory_error,
                         'pass_gate1_identity': True, 'unimplemented_color_weight_rejected': True})
            for temporary in (base, displaced, gated):
                bpy.data.meshes.remove(temporary)
        if template_state != tuple(mesh_signature(mesh) for slot in templates for mesh in slot):
            raise AssertionError('Original source mesh templates were mutated')
        if live_samples == 0:
            raise AssertionError(f'Real source has no live particles at any requested age: system={system["Asset"]}; ages={ages}')
    if manifest_path.read_bytes() != original or repr(materials) != source_state:
        raise AssertionError('Regression mutated source records')
    bpy.ops.wm.save_as_mainfile(filepath=str(output / 'real_nosoft_particles.blend'))
    bpy.ops.wm.open_mainfile(filepath=str(output / 'real_nosoft_particles.blend'), load_ui=False, use_scripts=False)
    if len(tuple(obj for obj in bpy.context.scene.objects if obj.type == 'MESH'
                 and obj.get('endfield_nosoft_regression_sample') is True)) != len(rows):
        raise AssertionError('Real particle meshes did not survive save and reopen')
    receipt = {'status': 'passed', 'source_manifest': str(manifest_path),
               'source_sha256': hashlib.sha256(original).hexdigest(), 'effect_name': effect_name,
               'system_count': len(pairs), 'samples': rows, 'empty_source_samples': empty_samples,
               'saved_reopened': True,
               'scope': 'Real source mesh particles and textures at explicit preview ages; native per-renderer time selection and game rendering parity are unverified.'}
    (output / 'verification.json').write_text(json.dumps(receipt, ensure_ascii=False, indent=2), encoding='utf-8')
    print(json.dumps({'status': 'passed', 'systems': len(pairs), 'samples': len(rows), 'output': str(output)}))


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--manifest', type=Path, required=True)
    parser.add_argument('--effect-name', required=True)
    parser.add_argument('--expected-systems', type=int, required=True)
    parser.add_argument('--ages', type=float, nargs='+', required=True)
    parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args(sys.argv[sys.argv.index('--') + 1:])
    main(args.manifest, args.effect_name, args.expected_systems, tuple(args.ages), args.output)
