"""Run with Blender --background --disable-autoexec --python this_file -- <arguments>."""
from __future__ import annotations

import argparse
from dataclasses import replace
import json
from pathlib import Path
import sys

import bpy
import numpy as np

sys.dont_write_bytecode = True
sys.path[:0] = [str(Path(__file__).resolve().parents[1] / 'src'),
                str(Path(__file__).resolve().parents[1] / 'src/character_runtime')]
from blender_scene_materials import layout_shader_node_tree
from character_npr_nodes import ArtistLighting, make_artist_lighting
from character_fur_transmission import attach_fur_transmission, restore_fur_ndotl, connect_fur_transmission, verify_fur_transmission
from character_special_vfx import KEYWORD, source_from_material, verify_special_vfx
from rebuild_character_materials import fur_dye_color, fur_shell_coverage, load_images, make_environment, material_group, read_packet


def arguments() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    for name in ('blend', 'source-dir', 'shader-identities', 'selection-report',
                 'visibility', 'material-source', 'material-path-id', 'output-dir'):
        parser.add_argument('--' + name, required=True)
    return parser.parse_args(sys.argv[sys.argv.index('--') + 1:])


def check_time(scene: bpy.types.Scene, tree: bpy.types.ShaderNodeTree,
               record: dict[str, object], frame: int) -> float:
    scene.frame_set(frame)
    bpy.context.view_layer.update()
    actual = float(tree.nodes[str(record['time_node'])].outputs[0].default_value)
    expected = frame * scene.render.fps_base / scene.render.fps
    if abs(actual - expected) > 1e-6:
        raise AssertionError(f'Special VFX time differs: frame={frame}; actual={actual}; expected={expected}')
    return actual


def render_pixels(scene: bpy.types.Scene, path: Path) -> np.ndarray:
    scene.render.filepath = str(path)
    bpy.ops.render.render(write_still=True, scene=scene.name)
    image = bpy.data.images.load(str(path), check_existing=False)
    values = np.empty(len(image.pixels), dtype=np.float32)
    image.pixels.foreach_get(values)
    bpy.data.images.remove(image)
    return values.reshape(-1, 4)[:, :3]


def probe_scene(group: bpy.types.ShaderNodeTree, fps: int, fps_base: float) -> bpy.types.Scene:
    scene = bpy.data.scenes.new('Special VFX regression UV plane')
    bpy.context.window.scene = scene
    scene.render.engine = 'BLENDER_EEVEE_NEXT'
    scene.render.resolution_x = 128
    scene.render.resolution_y = 128
    scene.render.resolution_percentage = 100
    scene.render.fps = fps
    scene.render.fps_base = fps_base
    scene.render.image_settings.file_format = 'PNG'
    scene.render.use_compositing = False
    scene.view_settings.view_transform = 'Standard'
    world = bpy.data.worlds.new('Special VFX regression black world')
    world.use_nodes = True
    world.node_tree.nodes['Background'].inputs['Color'].default_value = (0.0, 0.0, 0.0, 1.0)
    scene.world = world
    mesh = bpy.data.meshes.new('Special VFX regression source UV0')
    mesh.from_pydata([(-1.0, -1.0, 0.0), (1.0, -1.0, 0.0),
                      (1.0, 1.0, 0.0), (-1.0, 1.0, 0.0)], [], [(0, 1, 2, 3)])
    layer = mesh.uv_layers.new(name='UV0')
    for item, uv in zip(layer.data, ((0.0, 0.0), (1.0, 0.0), (1.0, 1.0), (0.0, 1.0)), strict=True):
        item.uv = uv
    obj = bpy.data.objects.new('Special VFX regression plane', mesh)
    scene.collection.objects.link(obj)
    material = bpy.data.materials.new('Special VFX regression source shader')
    material.use_nodes = True
    output = material.node_tree.nodes['Material Output']
    instance = material.node_tree.nodes.new('ShaderNodeGroup')
    instance.node_tree = group
    material.node_tree.links.new(instance.outputs['Shader'], output.inputs['Surface'])
    mesh.materials.append(material)
    camera_data = bpy.data.cameras.new('Special VFX regression camera')
    camera_data.type = 'ORTHO'
    camera_data.ortho_scale = 2.0
    camera = bpy.data.objects.new('Special VFX regression camera', camera_data)
    camera.location = (0.0, 0.0, 2.0)
    scene.collection.objects.link(camera)
    scene.camera = camera
    return scene


def main() -> None:
    args = arguments()
    paths = {name: Path(getattr(args, name)).resolve(strict=True)
             for name in ('blend', 'source_dir', 'shader_identities', 'selection_report', 'visibility')}
    output_dir = Path(args.output_dir).resolve()
    key = args.material_source.lower(), int(args.material_path_id)
    packet = read_packet(paths['source_dir'], paths['shader_identities'], paths['selection_report'], paths['visibility'])
    source, = (item for item in packet.materials if item.identity == key)
    if KEYWORD not in source.keywords:
        raise ValueError(f'Regression source lacks exact keyword {KEYWORD}: {key}')
    ordinary = next(item for item in packet.materials if item.shader == source.shader and KEYWORD not in item.keywords)
    output_dir.mkdir(parents=True, exist_ok=False)
    bpy.ops.wm.open_mainfile(filepath=str(paths['blend']), use_scripts=False, load_ui=False)
    original_scene = bpy.context.scene
    images = load_images(packet)
    lighting = make_artist_lighting(ArtistLighting((0.7, -1.0, 0.6), 0.9, 0.15, 0.0, 0.25, 0.75), layout_shader_node_tree)
    group = material_group(source, packet, make_environment(layout_shader_node_tree), images,
                           lighting, layout_shader_node_tree, None)
    transmission: dict[str, object] = {'status': 'not_selected'}
    if source.shader == 'HGRP/CharacterNPR' and '_CHARACTER_FUR' in source.keywords:
        transmission = json.loads(group['source_fur_transmission'])
        verify_fur_transmission(group, transmission)
        count = len(group.nodes)
        excluded = replace(source, keywords=tuple(key for key in source.keywords if key != '_CHARACTER_FUR'))
        if attach_fur_transmission(group, excluded) != {'status': 'not_selected'}:
            raise AssertionError('Fur transmission ignored exact source keyword selection')
        attach_fur_transmission(group, source)
        if len(group.nodes) != count:
            raise AssertionError('Fur transmission duplicated nodes or modified an ordinary material')
        restore_fur_ndotl(group, transmission)
        connect_fur_transmission(group, transmission)
        verify_fur_transmission(group, transmission)
        rebuilt = group.copy()
        rebuilt_samples = {str(item['source_slot']): item for item in rebuilt.nodes if item.type == 'TEX_IMAGE' and item.get('source_slot')}
        rebuilt_output, = (item for item in rebuilt.nodes if item.type == 'GROUP_OUTPUT' and item.is_active_output)
        rebuilt.links.new(fur_shell_coverage(rebuilt, source, rebuilt_samples), rebuilt_output.inputs['Coverage'])
        rebound = attach_fur_transmission(rebuilt, source)
        verify_fur_transmission(rebuilt, rebound)
        if rebound['fur_sample_node'] == transmission['fur_sample_node']:
            raise AssertionError('Rebuilt source shell retained the inactive old coverage sampler for transmission')
        bpy.data.node_groups.remove(rebuilt)
    samples = {str(item['source_slot']): item for item in group.nodes if item.type == 'TEX_IMAGE' and item.get('source_slot')}
    if '_CHARACTER_FUR_DYE' in source.keywords:
        base, = (item for item in group.nodes if item.label == 'Source base sample times BaseColor')
        node_count = len(group.nodes)
        dyed = fur_dye_color(group, source, samples, base.outputs[0])
        if (len(group.nodes) != node_count or dyed.node.blend_type != 'SCREEN'
                or len(samples['_FurDyeMap'].outputs['Color'].links) != 1):
            raise AssertionError('Production source Dye is disconnected or duplicated by verification')
    selected = source_from_material(source, samples)
    if selected is None or source_from_material(ordinary, samples) is not None:
        raise AssertionError('Exact source keyword selection changed')
    for switch in ('_VFXFresnelUseNormalMap', '_VFXAlphaAffectTotalMat', '_VFXMainUVSet', '_EnableVFXColorAdjustment'):
        invalid = replace(source, floats={**source.floats, switch: 1.0})
        try:
            source_from_material(invalid, samples)
        except ValueError as error:
            if 'Active source special VFX switches are unsupported' not in str(error):
                raise
        else:
            raise AssertionError(f'Unsupported active source switch was accepted: {switch}')
    record = json.loads(group['source_special_vfx'])
    verify_special_vfx(group, record)
    if '_CHARACTER_FUR' in source.keywords:
        base, = (binding for binding in source.textures if binding.name == '_BaseMap')
        uv_record = record['surface_uv']
        if (uv_record['space'] != 'base_map_st' or tuple(uv_record['scale']) != base.scale
                or tuple(uv_record['offset']) != base.offset):
            raise AssertionError('Fur special VFX dropped the source BaseMap varying transform')
        if source.floats['_UseBumpMap'] == 1.0:
            base_uv, = samples['_BaseMap'].inputs['Vector'].links
            normal_uv, = samples['_BumpMap'].inputs['Vector'].links
            if normal_uv.from_socket != base_uv.from_socket:
                raise AssertionError('Fur normal sampling no longer follows its surface UV policy')
    group_name = group.name
    scene = probe_scene(group, original_scene.render.fps, original_scene.render.fps_base)
    time_driver, = group.animation_data.drivers
    for variable in time_driver.driver.variables:
        variable.targets[0].id = scene
    group.update_tag()
    bpy.context.view_layer.update()
    first_time = check_time(scene, group, record, 6)
    first = render_pixels(scene, output_dir / 'frame_006.png')
    coordinate_delta = 0.0
    if '_CHARACTER_FUR' in source.keywords and (base.scale != (1.0, 1.0) or base.offset != (0.0, 0.0)):
        mapping = group.nodes[record['surface_uv']['node']]
        mapping.inputs['Scale'].default_value = (1.0, 1.0, 1.0)
        mapping.inputs['Location'].default_value = (0.0, 0.0, 0.0)
        raw_uv = render_pixels(scene, output_dir / 'frame_006_wrong_raw_uv.png')
        mapping.inputs['Scale'].default_value = (*base.scale, 1.0)
        mapping.inputs['Location'].default_value = (*base.offset, 0.0)
        coordinate_delta = float(np.max(np.abs(first - raw_uv)))
        if coordinate_delta <= 1.0 / 255.0:
            raise AssertionError('Nonidentity Fur BaseMap ST has no rendered effect on special VFX')
        verify_special_vfx(group, record)
    second_time = check_time(scene, group, record, 61)
    second = render_pixels(scene, output_dir / 'frame_061.png')
    added = group.nodes[str(record['sum_node'])]
    output, = (item for item in group.nodes if item.type == 'GROUP_OUTPUT' and item.is_active_output)
    coverage = output.inputs['Coverage'].links[0].from_socket
    group.links.new(added.inputs[0].links[0].from_socket, output.inputs['Shader'])
    disabled = render_pixels(scene, output_dir / 'frame_061_without_special.png')
    group.links.new(added.outputs[0], output.inputs['Shader'])
    if output.inputs['Coverage'].links[0].from_socket != coverage:
        raise AssertionError('Special VFX altered source coverage')
    animation_delta = float(np.max(np.abs(second - first)))
    contribution_delta = float(np.max(np.abs(second - disabled)))
    if animation_delta <= 1.0 / 255.0 or contribution_delta <= 1.0 / 255.0:
        raise AssertionError(f'Source VFX render has no measurable animation/contribution: animation={animation_delta}; contribution={contribution_delta}')
    bpy.context.preferences.filepaths.save_version = 0
    blend = output_dir / 'special_vfx_regression.blend'
    bpy.ops.wm.save_as_mainfile(filepath=str(blend), compress=False)
    bpy.ops.wm.open_mainfile(filepath=str(blend), use_scripts=False, load_ui=False)
    group = bpy.data.node_groups[group_name]
    scene = bpy.context.scene
    reopened_time = check_time(scene, group, record, 61)
    verification = verify_special_vfx(group, record)
    if transmission.get('format') == 'EndfieldFurTransmission/1':
        verify_fur_transmission(group, transmission)
    (output_dir / 'receipt.json').write_text(json.dumps({
        'status': 'passed', 'source_identity': source.identity, 'exact_keyword_selection': True,
        'ordinary_source_excluded': True, 'unsupported_active_switches_rejected': True,
        'source_coverage_unchanged': True, 'scripts_disabled_reopen': True,
        'frame6_seconds': first_time, 'frame61_seconds': second_time, 'reopened_seconds': reopened_time,
        'render_max_rgb_animation_delta': animation_delta,
        'render_max_rgb_special_contribution_delta': contribution_delta,
        'render_max_rgb_source_uv_delta': coordinate_delta,
        'source_metadata': record, 'verification': verification,
        'fur_transmission': transmission,
    }, indent=2), encoding='utf-8')
    print('CHARACTER_SPECIAL_VFX_REAL_BLENDER_SMOKE_PASSED', flush=True)


if __name__ == '__main__':
    main()
