"""Render real packaged Hair to verify LineMap replaces procedural stripes."""
# ruff: noqa: E402
from __future__ import annotations

import argparse
from pathlib import Path
import json
import sys

import bpy
from mathutils import Vector

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT / 'src'), str(ROOT / 'src' / 'character_runtime'), str(ROOT / 'tests')]

from blender_character_shading_test_helpers import active_labels, materials_for_source, refresh_materials
from character_hair_lighting import (
    HairShaderFrame, _line_modulated_diffuse, line_map_binding_from_material, source_parameters,
)
from patch_character_eye_lighting import exact_source_record


def labelled(tree: bpy.types.NodeTree, label: str) -> bpy.types.Node:
    """Resolve the unambiguous real source operation before adding the new branch."""
    nodes = tuple(item for item in tree.nodes if item.label == label)
    if len(nodes) != 1:
        raise AssertionError(f'Expected one source Hair operation: {tree.name}/{label}/{len(nodes)}')
    return nodes[0]


def source(socket: bpy.types.NodeSocket) -> bpy.types.NodeSocket:
    """Require the existing real source connection."""
    if len(socket.links) != 1:
        raise AssertionError(f'Expected one connected source: {socket.node.name}/{socket.name}')
    return socket.links[0].from_socket


def pixels(path: Path) -> tuple[float, ...]:
    """Read the completed native render without a display transform."""
    bpy.ops.render.render()
    image = bpy.data.images.get('Render Result')
    if image is None:
        raise AssertionError('Hair regression produced no native render result')
    image.save_render(str(path))
    result = bpy.data.images.load(str(path), check_existing=False)
    values = tuple(result.pixels)
    bpy.data.images.remove(result)
    if not values:
        raise AssertionError(f'Hair regression produced no saved native render pixels: {path}')
    return values


def main(blend: Path, output: Path) -> None:
    bpy.ops.wm.open_mainfile(filepath=str(blend.resolve(strict=True)), use_scripts=False)
    trees = tuple(tree for tree in bpy.data.node_groups
                  if tree.get('source_shader') == 'HGRP/CharacterNPR_Hair' and tree.get('hair_lighting_patch'))
    if not trees:
        raise AssertionError('Real packaged sample has no reconstructed Hair')
    tree = trees[0]
    _, native = exact_source_record(tree)
    frame_record = json.loads(tree['hair_lighting_shader_frame'])
    frame = HairShaderFrame(**frame_record)
    parameters = source_parameters(tree, frame, line_map_binding_from_material(native))
    if not parameters.use_specular_line or not parameters.use_line_map or parameters.line_amount <= 0.0:
        raise AssertionError('Regression requires real source LineMap Hair with nonzero procedural amount')
    materials = materials_for_source(tree.name)
    before = frozenset(node.as_pointer() for node in tree.nodes)
    diffuse = labelled(tree, 'HGRP source line-modulated diffuse')
    result, _ = _line_modulated_diffuse(
        tree, source(diffuse.inputs[0]),
        source(labelled(tree, 'HGRP source line modulation delta').inputs[0]),
        source(labelled(tree, 'HGRP source line blend by strand maximum').inputs[1]),
        source(labelled(tree, 'HGRP source line normal contribution').inputs[0]),
        source(labelled(tree, 'HGRP source line tangent sum').inputs[1]),
        source(labelled(tree, 'HGRP source line tangent dot half').inputs[1]), parameters,
    )
    result.node.label = 'Regression rebuilt source Hair LineMap output'
    tree.links.new(result, labelled(tree, 'HGRP hair dual-strand specular added to NPR light').inputs[1])
    added = tuple(node for node in tree.nodes if node.as_pointer() not in before)
    period = next(node for node in added if node.label == 'HGRP source procedural line period')
    blend_node = next(node for node in added if node.label == 'HGRP source LineMap/procedural blend')
    if source(blend_node.inputs[1]).node.label != 'HGRP source procedural line mask':
        raise AssertionError('Source LineMap selector must add the procedural term without another gate')
    refresh_materials(materials)
    for material in materials:
        if 'Regression rebuilt source Hair LineMap output' not in active_labels(material):
            raise AssertionError(f'Actual packaged Material Output lost the rebuilt Hair line branch: {material.name}')
    meshes = tuple(obj for obj in bpy.context.scene.objects if obj.type == 'MESH' and not obj.hide_render
                   and any(slot.material in materials for slot in obj.material_slots))
    if not meshes:
        raise AssertionError('Real Hair materials have no visible mesh consumers')
    for obj in bpy.context.scene.objects:
        if obj.type in ('MESH', 'CURVES') and obj not in meshes:
            obj.hide_render = True
    points = tuple(obj.matrix_world @ Vector(corner) for obj in meshes for corner in obj.bound_box)
    low = Vector(tuple(min(point[axis] for point in points) for axis in range(3)))
    high = Vector(tuple(max(point[axis] for point in points) for axis in range(3)))
    center = (low + high) * .5
    extent = max(high - low)
    scene = bpy.context.scene
    if scene.camera is None:
        raise AssertionError('Real packaged sample has no preview camera')
    scene.camera.location = center + Vector((0., -extent * 3., extent * .06))
    scene.camera.rotation_euler = (center - scene.camera.location).to_track_quat('-Z', 'Y').to_euler()
    scene.camera.data.type = 'ORTHO'
    scene.camera.data.ortho_scale = extent * 1.15
    scene.render.engine = 'BLENDER_EEVEE_NEXT'
    scene.render.resolution_x = scene.render.resolution_y = 128
    scene.render.resolution_percentage = 100
    scene.render.film_transparent = True
    scene.render.image_settings.file_format = 'OPEN_EXR'
    scene.render.image_settings.color_mode = 'RGBA'
    scene.render.image_settings.color_depth = '32'
    scene.eevee.taa_render_samples = 8
    scene.use_nodes = False
    output.parent.mkdir(parents=True, exist_ok=True)
    reference = pixels(output.with_name(output.stem + '_source.exr'))
    period.inputs[1].default_value = 0.0
    refresh_materials(materials)
    changed = pixels(output.with_name(output.stem + '_zero_amount.exr'))
    if len(reference) != len(changed):
        raise AssertionError('Hair render dimensions changed during LineAmount isolation')
    maximum = max(abs(left - right) for left, right in zip(reference, changed))
    visible_pixels = sum(reference[index + 3] > .5 and max(reference[index:index + 3]) > .001
                         for index in range(0, len(reference), 4))
    if maximum > 2e-6 or visible_pixels < 100:
        raise AssertionError(f'LineMap mode still depends on procedural LineAmount: max_error={maximum}, pixels={visible_pixels}')
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps({'status': 'passed', 'blend': str(blend), 'source': tree.name,
                                 'materials': [material.name for material in materials],
                                 'source_line_amount': parameters.line_amount,
                                 'source_line_intensity': parameters.line_intensity,
                                 'maximum_render_difference': maximum,
                                 'visible_pixels': visible_pixels,
                                 'active_material_outputs_verified': True}, indent=2), encoding='utf-8')
    print(json.dumps({'status': 'passed', 'audit': str(output), 'maximum_render_difference': maximum}))


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--blend', type=Path, required=True)
    parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args(sys.argv[sys.argv.index('--') + 1:])
    main(args.blend, args.output)
