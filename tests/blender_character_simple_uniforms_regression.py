"""Verify native uniform cloning and final weather migration on a real character."""
from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys

import bpy

sys.dont_write_bytecode = True
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'src' / 'character_runtime'))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from blender_character_simple_materials_regression import scene_digest
from character_material_options import parse_character_material_options
from character_material_variant import (
    apply_material_variant, attach_full_response_simple_weather, material_trees,
    source_materials, verify_material_variant,
)
from character_shader_uniforms import (
    INPUT_MARKER, MARKER, migrate_shader_uniforms, verify_shader_uniforms,
)
from character_simple_materials import clone_source_socket


def _verify_scalar_slice() -> None:
    originals = tuple((tree, node) for tree in bpy.data.node_groups for node in tree.nodes
                      if MARKER in node)
    if not originals:
        raise AssertionError('The integration source must contain migrated scalar evidence')
    source, value = originals[0]
    destination = bpy.data.node_groups.new('Native scalar slice regression', 'ShaderNodeTree')
    result = clone_source_socket(destination, value.outputs[0], ())
    if result.node.bl_idname == 'ShaderNodeValue' or any(MARKER in node for node in destination.nodes):
        raise AssertionError(f'Scalar slicing copied frozen evidence instead of its native graph: {source.name}/{value.name}')
    if not any(node.bl_idname == 'ShaderNodeAttribute' for node in destination.nodes):
        raise AssertionError('Scalar slicing lost its native attribute input')
    bpy.data.node_groups.remove(destination)


def _verify_matrix_values(scene: bpy.types.Scene) -> int:
    graph = bpy.context.evaluated_depsgraph_get()
    checked = 0
    for obj in scene.objects:
        if obj.animation_data is None:
            continue
        for curve in obj.animation_data.drivers:
            if not curve.data_path.startswith('["ef_matrix_'):
                continue
            variable = curve.driver.variables[0]
            target = variable.targets[0]
            key = json.loads(curve.data_path[1:-1])
            expected = float(target.id.evaluated_get(graph).path_resolve(target.data_path))
            actual = float(obj.evaluated_get(graph)[key])
            if abs(actual - expected) > 1e-7:
                raise AssertionError(f'Cloned matrix receiver is stale: {obj.name}/{key}; actual={actual}, expected={expected}')
            checked += 1
    if checked == 0:
        raise AssertionError('The real integration fixture requires a live face matrix binding')
    return checked


def _verify_ambiguous_copy(scene: bpy.types.Scene) -> None:
    source = next(node for material in source_materials(scene) for tree in material_trees(material)
                  for node in tree.nodes if INPUT_MARKER in node)
    document = json.loads(source[INPUT_MARKER])
    key = document['variables'][0]['key']
    owner = next(obj for obj in scene.objects if key in obj)
    conflicting = owner.copy()
    scene.collection.objects.link(conflicting)
    anchor = bpy.data.objects.new('Conflicting matrix anchor', None)
    scene.collection.objects.link(anchor)
    curve = next(curve for curve in conflicting.animation_data.drivers
                 if curve.data_path == '[' + json.dumps(key) + ']')
    curve.driver.variables[0].targets[0].id = anchor
    material = bpy.data.materials.new('Receiver-free copied receipt')
    material.use_nodes = True
    tree = material.node_tree
    tree.nodes.clear()
    attribute = tree.nodes.new('ShaderNodeAttribute')
    attribute.attribute_type = 'OBJECT'
    attribute.attribute_name = key
    component = tree.nodes.new('ShaderNodeSeparateXYZ')
    matrix = tree.nodes.new('ShaderNodeCombineXYZ')
    tree.links.new(attribute.outputs['Vector'], component.inputs[0])
    tree.links.new(component.outputs['X'], matrix.inputs[0])
    attribute[INPUT_MARKER] = json.dumps({**document, 'tree': tree.name,
        'node': matrix.name, 'path': matrix.inputs[0].path_from_id('default_value'),
        'result_node': component.name})
    mesh = bpy.data.meshes.new('Unbound copied receipt mesh')
    receiver = bpy.data.objects.new('Unbound copied receipt receiver', mesh)
    scene.collection.objects.link(receiver)
    mesh.materials.append(material)
    try:
        migrate_shader_uniforms((material,), (receiver,), scene)
    except ValueError as error:
        if 'missing or ambiguous' not in str(error):
            raise
    else:
        raise AssertionError('A receiver-free copied receipt accepted two anchor targets for one UUID')
    finally:
        for obj in (receiver, conflicting, anchor):
            bpy.data.objects.remove(obj, do_unlink=True)
        bpy.data.meshes.remove(mesh)
        bpy.data.materials.remove(material)


def main(output: Path, lighting: str, weather: str) -> None:
    output.mkdir(parents=True, exist_ok=True)
    scene = bpy.context.scene
    original = scene_digest(scene)
    migrate_shader_uniforms(source_materials(scene), tuple(scene.objects), scene)
    _verify_scalar_slice()
    options = parse_character_material_options({'mode': 'simplified', 'lighting': lighting,
        'weather': {'quality': weather, 'effects': ['wet', 'snow', 'rain']}})
    prepared = attach_full_response_simple_weather(scene, options)
    report = apply_material_variant(scene, options, prepared)
    verify_material_variant(report)
    if scene_digest(scene) != original:
        raise AssertionError('Uniform integration changed geometry, rig, actions or visibility')
    materials = source_materials(scene)
    verify_shader_uniforms(materials, tuple(scene.objects), scene)
    trees = {tree.as_pointer(): tree for material in materials for tree in material_trees(material)}
    scene_drivers = tuple((tree.name, curve.data_path) for tree in trees.values() if tree.animation_data
        for curve in tree.animation_data.drivers if any(target.id_type == 'SCENE'
            for variable in curve.driver.variables for target in variable.targets))
    if scene_drivers:
        raise AssertionError(f'Final material variant retained broad Scene drivers: {scene_drivers}')
    if lighting == 'simple' and not any(INPUT_MARKER in node for material in materials
                                        for node in material.node_tree.nodes):
        raise AssertionError('Simplified face slicing did not retain native matrix receipts')
    repeat = migrate_shader_uniforms(materials, tuple(scene.objects), scene)
    if repeat['migrated'] or repeat['bindings']:
        raise AssertionError('Final variant uniform migration is not idempotent')
    _verify_ambiguous_copy(scene)
    frame = scene.frame_current
    counts: list[int] = []
    for current in (frame, frame + 10, frame):
        scene.frame_set(current)
        bpy.context.view_layer.update()
        counts.append(_verify_matrix_values(scene))
    target = output / 'simple_uniforms.blend'
    bpy.context.preferences.filepaths.save_version = 0
    bpy.ops.wm.save_as_mainfile(filepath=str(target), check_existing=False)
    bpy.ops.wm.open_mainfile(filepath=str(target), use_scripts=False, load_ui=False)
    verify_material_variant(report)
    reopened = bpy.context.scene
    verify_shader_uniforms(source_materials(reopened), tuple(reopened.objects), reopened)
    repeat = migrate_shader_uniforms(source_materials(reopened), tuple(reopened.objects), reopened)
    if repeat['migrated'] or repeat['bindings']:
        raise AssertionError('Reopened variant uniform migration is not idempotent')
    result = {'status': 'passed', 'options': options.as_json(), 'matrix_values_checked': counts,
              'final_uniforms': report['uniform_check'], 'new_uniforms': report['uniforms']['migrated'],
              'scalar_evidence_slice_resolved': True, 'copied_matrix_receipts_remapped': True,
              'ambiguous_anchor_rejected': True,
              'remaining_scene_drivers': 0, 'save_reopen': True, 'idempotence': True}
    (output / 'simple_uniforms_regression.json').write_text(
        json.dumps(result, ensure_ascii=False, allow_nan=False, indent=2), encoding='utf-8')
    print('SIMPLE_UNIFORMS_REGRESSION_PASSED', json.dumps(result, ensure_ascii=False), flush=True)


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output', type=Path, required=True)
    parser.add_argument('--lighting', choices=('simple', 'full'), required=True)
    parser.add_argument('--weather', choices=('simple', 'full'), required=True)
    arguments = parser.parse_args(sys.argv[sys.argv.index('--') + 1:])
    main(arguments.output, arguments.lighting, arguments.weather)
