"""Compare real saved character shading and exercise live outer artist controls."""
from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys

import bpy

sys.dont_write_bytecode = True
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'src'))
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'src' / 'character_runtime'))

from blender_scene_materials import layout_shader_node_tree
from character_material_authoring import (
    contains_images, flatten_image_groups, package_material_authoring,
    shader_editor, verify_material_authoring, shader_dependencies,
)
from character_expression_material_controls import inherited_expression_material_controls
from character_authoring_validation import material_signature


def render(output: Path, name: str, engine: str) -> tuple[float, ...]:
    """Render the same real character camera and return its saved pixel buffer."""
    scene = bpy.context.scene
    scene.render.engine = engine
    scene.render.resolution_x = 256
    scene.render.resolution_y = 256
    scene.render.resolution_percentage = 100
    scene.eevee.taa_render_samples = 8
    scene.cycles.samples = 16
    scene.cycles.seed = 1
    scene.cycles.use_animated_seed = False
    scene.cycles.use_denoising = False
    scene.render.image_settings.file_format = 'PNG'
    scene.render.filepath = str(output / (name + '.png'))
    bpy.ops.render.render(write_still=True)
    image = bpy.data.images.load(scene.render.filepath, check_existing=False)
    pixels = tuple(image.pixels)
    bpy.data.images.remove(image)
    return pixels


def delta(first: tuple[float, ...], second: tuple[float, ...]) -> dict[str, float]:
    """Require matching dimensions before comparing rendered RGBA samples."""
    if len(first) != len(second):
        raise AssertionError('Material comparison dimensions changed')
    differences = tuple(abs(left - right) for left, right in zip(first, second))
    return {'maximum': max(differences), 'mean': sum(differences) / len(differences)}


def drivers() -> tuple[tuple[str, str, int, str, str, tuple[tuple[str, str, tuple[tuple[str, str], ...]], ...]], ...]:
    """Snapshot original driver definitions without evaluated socket values."""
    owners = (*bpy.data.node_groups, *(item.node_tree for item in bpy.data.materials if item.use_nodes))
    return tuple((tree.name, curve.data_path, curve.array_index, curve.driver.type, curve.driver.expression,
                  tuple((variable.name, variable.type, tuple((target.id.name if target.id else '', target.data_path)
                        for target in variable.targets)) for variable in curve.driver.variables))
                 for tree in owners if tree.animation_data for curve in tree.animation_data.drivers)


def render_native_reference(materials: tuple[bpy.types.Material, ...], output: Path) -> tuple[float, ...]:
    """Render native expanded equations before relocation or computation grouping.

    Old Cycles output is not an equivalence oracle when the source graph contains
    invalid group-feedback links. Expansion restores those exact socket routes.
    """
    originals = tuple((node, node.node_tree) for material in materials for node in material.node_tree.nodes
                      if node.type == 'GROUP' and contains_images(node.node_tree, frozenset()))
    copies = []
    try:
        for node, source in originals:
            tree = source.copy()
            copies.append(tree)
            node.node_tree = tree
            with shader_editor(tree) as space:
                flatten_image_groups(tree, space)
        return render(output, 'native_equations_cycles_reference', 'CYCLES')
    finally:
        for node, source in originals:
            node.node_tree = source
        for tree in copies:
            bpy.data.node_groups.remove(tree)


def main(argv: list[str]) -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument('--output-dir', type=Path, required=True)
    args = parser.parse_args(argv)
    output = args.output_dir.resolve()
    output.mkdir(parents=True, exist_ok=True)
    before_drivers = drivers()
    before_expression = inherited_expression_material_controls()
    before = render(output, 'materials_before', 'BLENDER_EEVEE_NEXT')
    repeated_before = render(output, 'materials_before_control', 'BLENDER_EEVEE_NEXT')
    render_control = delta(before, repeated_before)
    before_cycles = render(output, 'materials_before_cycles', 'CYCLES')
    cycles_control = delta(before_cycles, render(output, 'materials_before_cycles_control', 'CYCLES'))
    materials = tuple(sorted({slot.material for obj in bpy.context.scene.objects if obj.type == 'MESH'
                            for slot in obj.material_slots if slot.material is not None and slot.material.use_nodes},
                            key=lambda item: item.name))
    invalid_source_links = [(tree.name, link.from_node.name, link.from_socket.name,
                             link.to_node.name, link.to_socket.name)
                            for tree in bpy.data.node_groups for link in tree.links if not link.is_valid]
    reference_cycles = render_native_reference(materials, output)
    source_cycles_correction = delta(before_cycles, reference_cycles)
    if source_cycles_correction['maximum'] > 1. / 255. and not invalid_source_links:
        raise AssertionError(f'Native expansion changed a source graph without invalid links: {source_cycles_correction}')
    report = package_material_authoring(materials, layout_shader_node_tree)
    bpy.context.scene.frame_set(bpy.context.scene.frame_current)
    graph_check = verify_material_authoring(report)
    after_expression = inherited_expression_material_controls()
    if before_expression != after_expression:
        raise AssertionError('Source expression material receipts changed')
    after_drivers = frozenset(drivers())
    if any(item not in after_drivers for item in before_drivers):
        raise AssertionError('Original material driver definition changed')
    after_cycles = render(output, 'materials_after_cycles', 'CYCLES')
    cycles_comparison = delta(reference_cycles, after_cycles)
    if cycles_comparison['mean'] > cycles_control['mean'] + 0.0001 or cycles_comparison['maximum'] > cycles_control['maximum'] + 1. / 255.:
        raise AssertionError(f'Material packaging changed Cycles PNG pixels: {cycles_comparison}')
    after = render(output, 'materials_after', 'BLENDER_EEVEE_NEXT')
    comparison = delta(before, after)
    if comparison['mean'] > render_control['mean'] + 0.0001 or comparison['maximum'] > render_control['maximum'] + 1. / 255.:
        raise AssertionError(f'Material packaging changed saved PNG pixels: {comparison}')
    candidates = tuple((material, node) for material in materials
                       for node in material.node_tree.nodes if node.type == 'TEX_IMAGE'
                       and node.get('endfield_authoring_texture') and node.get('source_slot') == '_BaseMap'
                       and 'cloth_01' in material.name and 'lod' not in material.name)
    if not candidates:
        raise AssertionError('Real character has no exposed non-LOD cloth BaseMap for replacement testing')
    material, sample = candidates[0]
    original_image = sample.image
    replacement = bpy.data.images.new('Authoring Replacement Verification', width=4, height=4)
    replacement.generated_color = (1., 0., 1., 1.)
    sample.image = replacement
    swapped = render(output, 'materials_replaced_texture', 'BLENDER_EEVEE_NEXT')
    replacement_comparison = delta(after, swapped)
    if replacement_comparison['mean'] <= 0.00001:
        raise AssertionError(f'Outer texture replacement did not affect final shading: {replacement_comparison}')
    sample.image = original_image
    bpy.data.images.remove(replacement)
    controls = tuple(node for node in material.node_tree.nodes if node.get('endfield_authoring_parameters'))
    tint = controls[0].inputs['Base Tint']
    original_tint = tuple(tint.default_value)
    tint.default_value = (0., 0., 0., 1.)
    changed = render(output, 'materials_changed_parameter', 'BLENDER_EEVEE_NEXT')
    parameter_comparison = delta(after, changed)
    if parameter_comparison['mean'] <= 0.00001:
        raise AssertionError(f'Original Base Tint did not affect the packaged shader: {parameter_comparison}')
    tint.default_value = original_tint
    if after_expression is None:
        raise AssertionError('Real authoring sample has no source expression material controller')
    controller = bpy.data.objects[after_expression['controller']]
    original_blend = float(controller['_EmotionBlend'])
    controller['_EmotionBlend'] = 0. if original_blend != 0. else 1.
    controller.update_tag()
    bpy.context.view_layer.update()
    live_expression_bindings = 0
    for binding in after_expression['bindings']:
        if binding['property'] != '_EmotionBlend':
            continue
        if binding['owner_kind'] != 'MATERIAL':
            raise AssertionError('Real sample expression control has an unexpected owner kind')
        owner = bpy.data.materials[binding['owner']]
        socket = owner.node_tree.nodes[binding['node']].inputs[binding['socket']]
        if socket.default_value != controller['_EmotionBlend'] or socket.as_pointer() not in shader_dependencies(owner).input_sockets:
            raise AssertionError('Source emotion blend does not propagate into the actual packaged shader')
        live_expression_bindings += 1
    if live_expression_bindings == 0:
        raise AssertionError('Real sample has no live emotion-blend driver binding')
    controller['_EmotionBlend'] = original_blend
    controller.update_tag()
    bpy.context.view_layer.update()
    signature = material_signature()
    repeated = package_material_authoring(materials, layout_shader_node_tree)
    if repeated != report or material_signature() != signature:
        raise AssertionError('Repeated material packaging changed an already packaged graph')
    packed = output / 'material_authoring_verified.blend'
    bpy.ops.wm.save_as_mainfile(filepath=str(packed))
    bpy.ops.wm.open_mainfile(filepath=str(packed))
    reopened = verify_material_authoring(report)
    inherited_expression_material_controls()
    if material_signature() != signature:
        raise AssertionError('Saved material graph changed after reopen')
    result = {'status': 'passed', 'packaging': report, 'graph': graph_check,
              'same_render': comparison, 'repeat_render_control': render_control,
              'same_cycles_render': cycles_comparison, 'repeat_cycles_render_control': cycles_control,
              'cycles_reference': 'Native source equations expanded before relocation/grouping',
              'source_invalid_links': invalid_source_links, 'source_cycles_correction': source_cycles_correction,
              'outer_texture_replacement': replacement_comparison,
              'original_parameter_response': parameter_comparison, 'original_drivers': len(before_drivers),
              'expression_receipt_preserved': before_expression is not None, 'reopened': reopened,
              'live_expression_bindings': live_expression_bindings,
              'second_call_changed_nothing': True, 'blend': str(packed)}
    (output / 'material_authoring_verification.json').write_text(json.dumps(result, indent=2), encoding='utf-8')
    print(json.dumps({key: value for key, value in result.items() if key != 'packaging'}, indent=2))


if __name__ == '__main__':
    main(sys.argv[sys.argv.index('--') + 1:])
