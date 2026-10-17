"""Render and persist the native character postprocess graph in Blender 4.3 and 4.4."""
from __future__ import annotations

import argparse
from array import array
import json
from pathlib import Path
import subprocess
import sys
from typing import TypedDict

import bpy


class RenderSnapshot(TypedDict):
    width: int
    height: int
    pixels: tuple[float, ...]


CurvePointSnapshot = tuple[float, float, str]
CurveSnapshot = tuple[
    str,
    tuple[float, float, float],
    tuple[float, float, float],
    float,
    float,
    float,
    float,
    bool,
    str,
    tuple[tuple[CurvePointSnapshot, ...], ...],
]
NodeEdge = tuple[str, str, str, str]
GraphSnapshot = tuple[tuple[tuple[str, str], ...], tuple[NodeEdge, ...]]


def add_emission_tile(scene: bpy.types.Scene, name: str, x_center: float,
                      color: tuple[float, float, float]) -> None:
    mesh = bpy.data.meshes.new(name + ' Mesh')
    mesh.from_pydata(((x_center - 0.55, -1.25, 0.0), (x_center + 0.55, -1.25, 0.0),
                      (x_center + 0.55, 1.25, 0.0), (x_center - 0.55, 1.25, 0.0)), (), ((0, 1, 2, 3),))
    material = bpy.data.materials.new(name + ' Material')
    material.use_nodes = True
    material.node_tree.nodes.clear()
    emission = material.node_tree.nodes.new('ShaderNodeEmission')
    emission.inputs['Color'].default_value = (*color, 1.0)
    emission.inputs['Strength'].default_value = 1.4
    output = material.node_tree.nodes.new('ShaderNodeOutputMaterial')
    material.node_tree.links.new(emission.outputs['Emission'], output.inputs['Surface'])
    obj = bpy.data.objects.new(name, mesh)
    obj.data.materials.append(material)
    scene.collection.objects.link(obj)


def make_shared_user_group() -> bpy.types.NodeTree:
    tree = bpy.data.node_groups.new('Regression Shared User Group', 'CompositorNodeTree')
    tree.interface.new_socket(name='Image', in_out='INPUT', socket_type='NodeSocketColor')
    tree.interface.new_socket(name='Image', in_out='OUTPUT', socket_type='NodeSocketColor')
    source = tree.nodes.new('NodeGroupInput')
    output = tree.nodes.new('NodeGroupOutput')
    tree.links.new(source.outputs['Image'], output.inputs['Image'])
    return tree


def setup_compositor(scene: bpy.types.Scene, shared_user_group: bpy.types.NodeTree) -> None:
    scene.use_nodes = True
    tree = scene.node_tree
    if tree is None:
        raise AssertionError(f'Compositor tree was not created for scene={scene.name}')
    tree.nodes.clear()
    render = tree.nodes.new('CompositorNodeRLayers')
    render.name = 'User Render Layers'
    rgb = tree.nodes.new('CompositorNodeRGB')
    rgb.name = 'User RGB'
    rgb.outputs[0].default_value = (0.1, 0.2, 0.3, 0.5)
    mix = tree.nodes.new('CompositorNodeMixRGB')
    mix.name = 'User Mix'
    mix.blend_type = 'MIX'
    mix.inputs[0].default_value = 0.3
    tree.links.new(render.outputs['Image'], mix.inputs[1])
    tree.links.new(rgb.outputs[0], mix.inputs[2])
    alpha_source = tree.nodes.new('CompositorNodeSepRGBA')
    alpha_source.name = 'User Alpha Source'
    tree.links.new(rgb.outputs[0], alpha_source.inputs['Image'])
    set_alpha = tree.nodes.new('CompositorNodeSetAlpha')
    set_alpha.name = 'User Set Alpha'
    tree.links.new(mix.outputs[0], set_alpha.inputs['Image'])
    tree.links.new(alpha_source.outputs['A'], set_alpha.inputs['Alpha'])
    shared = tree.nodes.new('CompositorNodeGroup')
    shared.name = 'User Shared Group'
    shared.node_tree = shared_user_group
    exposure = tree.nodes.new('CompositorNodeExposure')
    exposure.name = 'User Exposure'
    exposure.inputs['Exposure'].default_value = 0.2
    tree.links.new(set_alpha.outputs[0], shared.inputs['Image'])
    tree.links.new(shared.outputs['Image'], exposure.inputs['Image'])
    composite = tree.nodes.new('CompositorNodeComposite')
    composite.name = 'User Composite'
    tree.links.new(exposure.outputs['Image'], composite.inputs['Image'])


def setup_scene(scene: bpy.types.Scene, shared_user_group: bpy.types.NodeTree) -> None:
    scene.render.engine = 'BLENDER_EEVEE_NEXT'
    scene.render.resolution_x = 96
    scene.render.resolution_y = 64
    scene.render.resolution_percentage = 100
    scene.render.image_settings.file_format = 'PNG'
    scene.render.image_settings.color_mode = 'RGBA'
    scene.render.image_settings.color_depth = '8'
    scene.render.film_transparent = True
    scene.render.use_compositing = True
    scene.render.dither_intensity = 1.0
    scene.view_settings.exposure = 0.2
    scene.view_settings.gamma = 1.07
    setup_compositor(scene, shared_user_group)
    if scene == bpy.context.scene:
        for name, x_center, color in (
            ('Red Tile', -1.15, (1.0, 0.04, 0.02)),
            ('Green Tile', 0.0, (0.02, 1.0, 0.08)),
            ('Blue Tile', 1.15, (0.03, 0.1, 1.0)),
        ):
            add_emission_tile(scene, name, x_center, color)
        camera_data = bpy.data.cameras.new('Postprocess Regression Camera')
        camera = bpy.data.objects.new(camera_data.name, camera_data)
        scene.collection.objects.link(camera)
        camera.location = (0.0, 0.0, 5.0)
        camera_data.type = 'ORTHO'
        camera_data.ortho_scale = 4.0
        scene.camera = camera
        world = bpy.data.worlds.new('Postprocess Regression World')
        world.use_nodes = True
        world.node_tree.nodes['Background'].inputs['Color'].default_value = (0.01, 0.01, 0.01, 1.0)
        world.node_tree.nodes['Background'].inputs['Strength'].default_value = 0.1
        scene.world = world


def color_management_signature(scene: bpy.types.Scene) -> tuple[str, str, float, float, str, str]:
    return (
        scene.view_settings.view_transform,
        scene.view_settings.look,
        float(scene.view_settings.exposure),
        float(scene.view_settings.gamma),
        scene.display_settings.display_device,
        scene.sequencer_colorspace_settings.name,
    )


def graph_signature(scene: bpy.types.Scene, stage_marker: str) -> GraphSnapshot:
    tree = scene.node_tree
    if tree is None:
        raise AssertionError(f'Compositor tree is missing for scene={scene.name}')
    stage_nodes = tuple(node for node in tree.nodes if node.get(stage_marker) == 1)
    stage_set = set(stage_nodes)
    nodes = tuple(sorted((node.name, node.bl_idname) for node in tree.nodes if node not in stage_set))
    links = tuple(sorted(
        (link.from_node.name, link.from_socket.name, link.to_node.name, link.to_socket.name)
        for link in tree.links if link.from_node not in stage_set and link.to_node not in stage_set
    ))
    return nodes, links


def user_value_signature(scene: bpy.types.Scene) -> tuple[float, ...]:
    tree = scene.node_tree
    if tree is None:
        raise AssertionError(f'Compositor tree is missing for scene={scene.name}')
    rgb = tree.nodes['User RGB']
    mix = tree.nodes['User Mix']
    alpha = tree.nodes['User Set Alpha']
    exposure = tree.nodes['User Exposure']
    return (
        *(float(value) for value in rgb.outputs[0].default_value),
        float(mix.inputs[0].default_value),
        float(alpha.inputs['Alpha'].default_value),
        float(exposure.inputs['Exposure'].default_value),
    )


def curve_signature(stage: bpy.types.Node, curves_node_name: str) -> CurveSnapshot:
    mapping = stage.node_tree.nodes[curves_node_name].mapping
    channels = tuple(
        tuple((float(point.location.x), float(point.location.y), point.handle_type) for point in curve.points)
        for curve in mapping.curves
    )
    return (
        mapping.tone,
        tuple(float(value) for value in mapping.black_level),
        tuple(float(value) for value in mapping.white_level),
        float(mapping.clip_min_x),
        float(mapping.clip_min_y),
        float(mapping.clip_max_x),
        float(mapping.clip_max_y),
        bool(mapping.use_clip),
        mapping.extend,
        channels,
    )


def control_signature(stage: bpy.types.Node, control_specs: tuple[tuple[str, float, float, float, str], ...]) -> tuple[float, ...]:
    return tuple(float(stage.inputs[name].default_value) for name, _, _, _, _ in control_specs)


def read_render_image(destination: Path) -> RenderSnapshot:
    image = bpy.data.images.load(str(destination), check_existing=False)
    width, height = image.size
    values = array('f', [0.0]) * (width * height * 4)
    image.pixels.foreach_get(values)
    bpy.data.images.remove(image)
    return {'width': width, 'height': height, 'pixels': tuple(float(value) for value in values)}


def render_image(scene: bpy.types.Scene, destination: Path) -> RenderSnapshot:
    scene.render.filepath = str(destination)
    bpy.ops.render.render(write_still=True)
    return read_render_image(destination)


def maximum_pixel_delta(left: RenderSnapshot, right: RenderSnapshot) -> float:
    if (left['width'], left['height']) != (right['width'], right['height']):
        raise AssertionError('Rendered images have different dimensions')
    maximum = 0.0
    for offset in range(0, len(left['pixels']), 4):
        left_alpha = left['pixels'][offset + 3]
        right_alpha = right['pixels'][offset + 3]
        maximum = max(maximum, abs(left_alpha - right_alpha))
        for channel in range(3):
            left_premultiplied = left['pixels'][offset + channel] * left_alpha
            right_premultiplied = right['pixels'][offset + channel] * right_alpha
            maximum = max(maximum, abs(left_premultiplied - right_premultiplied))
    return maximum


def assert_close_images(left: RenderSnapshot, right: RenderSnapshot, label: str) -> float:
    delta = maximum_pixel_delta(left, right)
    if delta > 0.012:
        raise AssertionError(f'Render mismatch after {label}: max_pixel_delta={delta}')
    return delta


def control_socket(stage_nodes: tuple[bpy.types.Node, ...], name: str) -> bpy.types.NodeSocket:
    matches = tuple(stage.inputs[name] for stage in stage_nodes if name in stage.inputs)
    if len(matches) != 1:
        raise AssertionError(f'Expected exactly one postprocess socket: name={name}, count={len(matches)}')
    return matches[0]


def assert_alpha(snapshot: RenderSnapshot, expected: float) -> None:
    alphas = snapshot['pixels'][3::4]
    covered = tuple(value for value in alphas if value > expected * 0.95)
    if not covered:
        raise AssertionError(f'Render has no fully covered alpha samples: expected={expected}')
    if max(alphas) > expected + 0.025 or min(alphas) < -0.005:
        raise AssertionError(f'Compositor alpha left its source range: expected={expected}, range=({min(alphas)}, {max(alphas)})')


def assert_alpha_unchanged(left: RenderSnapshot, right: RenderSnapshot, label: str) -> float:
    delta = max(abs(a - b) for a, b in zip(left['pixels'][3::4], right['pixels'][3::4]))
    if delta > 0.012:
        raise AssertionError(f'Alpha changed after {label}: max_pixel_delta={delta}')
    return delta


def mutate_curve(stage: bpy.types.Node, curves_node_name: str) -> None:
    mapping = stage.node_tree.nodes[curves_node_name].mapping
    mapping.tone = 'FILMLIKE'
    mapping.black_level = (0.01, 0.02, 0.03)
    mapping.white_level = (0.92, 0.94, 0.96)
    mapping.clip_min_x = -0.1
    mapping.clip_min_y = -0.2
    mapping.clip_max_x = 1.1
    mapping.clip_max_y = 1.2
    mapping.extend = 'HORIZONTAL'
    mapping.use_clip = False
    curve = mapping.curves[1]
    point = curve.points.new(0.4, 0.68)
    point.handle_type = 'VECTOR'
    mapping.update()


def verify_native_reopen(blend_path: Path, render_path: Path) -> None:
    bpy.ops.wm.open_mainfile(filepath=str(blend_path.resolve()), use_scripts=False, load_ui=False)
    scene = bpy.context.scene
    tree = scene.node_tree
    if tree is None or not any(node.get('endfield_postprocess_stage') == 1 for node in tree.nodes):
        raise AssertionError('Saved blend did not retain a native postprocess group node')
    scene.render.use_compositing = True
    scene.render.filepath = str(render_path.resolve())
    bpy.ops.render.render(write_still=True)
    if not render_path.is_file() or render_path.stat().st_size == 0:
        raise AssertionError('Reopened native compositor did not produce an image')
    print('NATIVE_REOPEN_RENDER_PASSED', flush=True)


def run_reopen_process(blend_path: Path, render_path: Path) -> None:
    command = [
        bpy.app.binary_path,
        '--background',
        '--factory-startup',
        '--disable-autoexec',
        '--python-exit-code',
        '1',
        '--python',
        str(Path(__file__).resolve()),
        '--',
        '--reopen-only',
        '--blend',
        str(blend_path.resolve()),
        '--render-output',
        str(render_path.resolve()),
    ]
    result = subprocess.run(command, check=False, capture_output=True, text=True)
    if result.returncode != 0 or 'NATIVE_REOPEN_RENDER_PASSED' not in result.stdout:
        raise AssertionError(
            f'Fresh Blender could not render the saved native graph: exit={result.returncode}, '
            f'stdout={result.stdout[-3000:]}, stderr={result.stderr[-3000:]}'
        )


def run_regression(output_root: Path) -> None:
    sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'src' / 'character_runtime'))
    import character_postprocess as postprocess

    output = output_root / ('blender_' + bpy.app.version_string.replace('.', '_'))
    output.mkdir(parents=True, exist_ok=True)
    scene = bpy.context.scene
    scene.name = 'Postprocess Regression'
    shared_user_group = make_shared_user_group()
    setup_scene(scene, shared_user_group)
    initial_graph = graph_signature(scene, postprocess.STAGE_MARKER)
    initial_values = user_value_signature(scene)
    initial_color_management = color_management_signature(scene)
    baseline = render_image(scene, output / 'before.png')
    if len(set(round(value, 3) for value in baseline['pixels'][0::4])) < 3:
        raise AssertionError('Procedural scene render did not preserve visible color variation')
    assert_alpha(baseline, 0.5)

    second_scene = bpy.data.scenes.new('Independent Postprocess Scene')
    setup_compositor(second_scene, shared_user_group)
    second_scene.render.use_compositing = False
    second_scene.render.dither_intensity = 0.37
    assert postprocess.stage_nodes(second_scene) == ()
    assert not postprocess.stage_nodes(scene)

    primary = postprocess.install(scene)
    first_nodes = postprocess.stage_nodes(scene)
    assert len(first_nodes) == 1 and postprocess.find_group(scene) == primary
    assert scene.render.use_compositing
    assert graph_signature(scene, postprocess.STAGE_MARKER)[0] == initial_graph[0]
    original_terminal_edge = ('User Exposure', 'Image', 'User Composite', 'Image')
    assert set(graph_signature(scene, postprocess.STAGE_MARKER)[1]) == set(initial_graph[1]) - {original_terminal_edge}
    assert user_value_signature(scene) == initial_values
    assert color_management_signature(scene) == initial_color_management
    repeated = postprocess.install(scene)
    assert repeated == primary and postprocess.stage_nodes(scene) == first_nodes
    neutral = render_image(scene, output / 'neutral_enabled.png')
    neutral_delta = assert_close_images(baseline, neutral, 'neutral install')
    assert_alpha(neutral, 0.5)

    stage = postprocess.find_group(scene)
    if stage is None:
        raise AssertionError('Postprocess group disappeared after installation')
    stage.inputs['Exposure EV'].default_value = 1.0
    stage.inputs['Contrast'].default_value = 18.0
    active = render_image(scene, output / 'enabled.png')
    active_delta = maximum_pixel_delta(baseline, active)
    if active_delta < 0.03:
        raise AssertionError(f'Enabled exposure/contrast did not alter rendered pixels: delta={active_delta}')
    assert_alpha(active, 0.5)
    active_alpha_delta = assert_alpha_unchanged(baseline, active, 'enabled grading')
    postprocess.bypass(scene)
    bypassed = render_image(scene, output / 'bypassed.png')
    bypass_delta = assert_close_images(baseline, bypassed, 'bypass')
    assert postprocess.is_bypassed(scene)
    postprocess.resume(scene)
    resumed = render_image(scene, output / 'resumed.png')
    resume_delta = assert_close_images(active, resumed, 'resume')
    postprocess.reset_defaults(scene)
    reset = render_image(scene, output / 'reset.png')
    reset_delta = assert_close_images(baseline, reset, 'reset')
    for name, default, _, _, _ in postprocess.CONTROL_SPECS:
        if abs(float(control_socket(postprocess.stage_nodes(scene), name).default_value) - default) > 1e-6:
            raise AssertionError(f'Reset did not restore control default: name={name}')
    assert color_management_signature(scene) == initial_color_management

    postprocess.install(second_scene)
    second_group = postprocess.find_group(second_scene)
    if second_group is None:
        raise AssertionError('Independent scene did not receive its own postprocess group')
    assert second_scene.render.use_compositing
    assert not postprocess.is_shared(second_scene)
    assert second_group.node_tree != postprocess.find_group(scene).node_tree
    postprocess.remove(second_scene)
    assert not second_scene.render.use_compositing
    assert abs(float(second_scene.render.dither_intensity) - 0.37) < 1e-6
    assert postprocess.stage_nodes(second_scene) == ()

    copied_scene = scene.copy()
    copied_scene.name = 'Copied Postprocess Regression'
    assert len(postprocess.stage_nodes(copied_scene)) == len(postprocess.stage_nodes(scene))
    assert postprocess.is_shared(copied_scene)
    copied_nodes = postprocess.stage_nodes(copied_scene)
    postprocess.install(copied_scene)
    assert len(postprocess.stage_nodes(copied_scene)) == len(copied_nodes)
    assert not postprocess.is_shared(copied_scene)
    copied_stage = postprocess.find_group(copied_scene)
    source_stage = postprocess.find_group(scene)
    if copied_stage is None or source_stage is None:
        raise AssertionError('Copied scene lost its postprocess stage')
    assert copied_stage.node_tree != source_stage.node_tree
    source_curve_before = curve_signature(source_stage, postprocess.CURVES_NODE_NAME)
    copied_stage.inputs['Exposure EV'].default_value = 1.75
    mutate_curve(copied_stage, postprocess.CURVES_NODE_NAME)
    assert abs(float(source_stage.inputs['Exposure EV'].default_value)) < 1e-6
    assert curve_signature(source_stage, postprocess.CURVES_NODE_NAME) == source_curve_before
    postprocess.remove(copied_scene)

    stage = postprocess.find_group(scene)
    if stage is None:
        raise AssertionError('Postprocess group disappeared before preset checks')
    for name, default, minimum, maximum, _ in postprocess.CONTROL_SPECS:
        if name == 'Exposure EV':
            value = 1.25
        elif name == 'Contrast':
            value = 18.0
        elif name == 'Saturation':
            value = 1.2
        elif name == 'Bloom Strength':
            value = 0.4
        elif name == 'Bloom Threshold':
            value = 0.6
        elif name == 'Bloom Softness':
            value = 0.7
        elif name == 'Bloom Radius':
            value = 24.0
        elif name == 'Style Mix':
            value = 0.8
        else:
            value = 0.25
        if not minimum <= value <= maximum or value == default:
            raise AssertionError(f'Invalid non-default regression value: control={name}, value={value}')
        stage.inputs[name].default_value = value
    mutate_curve(stage, postprocess.CURVES_NODE_NAME)
    scene.render.dither_intensity = 0.73
    curve_before_export = curve_signature(stage, postprocess.CURVES_NODE_NAME)
    controls_before_export = control_signature(stage, postprocess.CONTROL_SPECS)
    preset_path = output / 'roundtrip_preset.json'
    postprocess.export_preset(scene, preset_path)
    assert curve_signature(stage, postprocess.CURVES_NODE_NAME) == curve_before_export
    assert control_signature(stage, postprocess.CONTROL_SPECS) == controls_before_export
    postprocess.reset_defaults(scene)
    postprocess.import_preset(scene, preset_path)
    stage = postprocess.find_group(scene)
    if stage is None:
        raise AssertionError('Preset import removed the postprocess group')
    assert control_signature(stage, postprocess.CONTROL_SPECS) == controls_before_export
    assert curve_signature(stage, postprocess.CURVES_NODE_NAME) == curve_before_export
    assert abs(float(scene.render.dither_intensity) - 0.73) < 1e-6

    preset_value = json.loads(preset_path.read_text(encoding='utf-8'))
    preset_value['stages'][0]['controls']['Exposure EV'] = -2.0
    preset_value['stages'][0]['controls']['Sharpen'] = 9.0
    invalid_preset_path = output / 'invalid_preset.json'
    invalid_preset_path.write_text(json.dumps(preset_value, ensure_ascii=False, indent=2), encoding='utf-8')
    controls_before_rejection = control_signature(stage, postprocess.CONTROL_SPECS)
    curves_before_rejection = curve_signature(stage, postprocess.CURVES_NODE_NAME)
    dither_before_rejection = float(scene.render.dither_intensity)
    try:
        postprocess.import_preset(scene, invalid_preset_path)
    except postprocess.PresetValidationError:
        pass
    else:
        raise AssertionError('Out-of-range preset control was accepted')
    assert control_signature(stage, postprocess.CONTROL_SPECS) == controls_before_rejection
    assert curve_signature(stage, postprocess.CURVES_NODE_NAME) == curves_before_rejection
    assert abs(float(scene.render.dither_intensity) - dither_before_rejection) < 1e-6

    saved_blend = output / 'native_postprocess.blend'
    expected_reopen = output / 'expected_reopen.png'
    reopened_render = output / 'reopened_native.png'
    expected_active = render_image(scene, expected_reopen)
    bpy.context.preferences.filepaths.save_version = 0
    bpy.ops.wm.save_as_mainfile(filepath=str(saved_blend))
    run_reopen_process(saved_blend, reopened_render)
    reopened = read_render_image(reopened_render)
    reopen_delta = assert_close_images(expected_active, reopened, 'fresh-process blend reopen')

    postprocess.remove(scene)
    assert postprocess.stage_nodes(scene) == ()
    assert graph_signature(scene, postprocess.STAGE_MARKER) == initial_graph
    assert user_value_signature(scene) == initial_values
    assert color_management_signature(scene) == initial_color_management
    assert scene.render.use_compositing
    removed = render_image(scene, output / 'removed.png')
    removed_delta = assert_close_images(baseline, removed, 'removal')
    report = {
        'status': 'passed',
        'blender': bpy.app.version_string,
        'procedural_scene': True,
        'compositor_user_chain_preserved': True,
        'shared_user_group_preserved': True,
        'repeat_install_no_duplicates': True,
        'neutral_pixel_delta': neutral_delta,
        'enabled_pixel_delta': active_delta,
        'bypass_pixel_delta': bypass_delta,
        'resume_pixel_delta': resume_delta,
        'reset_pixel_delta': reset_delta,
        'remove_pixel_delta': removed_delta,
        'transparent_alpha_preserved': True,
        'active_alpha_pixel_delta': active_alpha_delta,
        'color_management_unchanged': True,
        'independent_scene_install_and_restore': True,
        'copied_scene_group_isolated': True,
        'preset_curve_roundtrip': True,
        'invalid_preset_atomic_rejection': True,
        'fresh_process_native_reopen_pixel_delta': reopen_delta,
        'blend': str(saved_blend),
    }
    (output / 'verification.json').write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding='utf-8')
    print(json.dumps(report, ensure_ascii=False), flush=True)


def main(arguments: list[str]) -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output-dir', type=Path)
    parser.add_argument('--reopen-only', action='store_true')
    parser.add_argument('--blend', type=Path)
    parser.add_argument('--render-output', type=Path)
    args = parser.parse_args(arguments)
    if args.reopen_only:
        if args.blend is None or args.render_output is None:
            raise ValueError('Reopen mode requires --blend and --render-output')
        verify_native_reopen(args.blend, args.render_output)
        return
    if args.output_dir is None:
        raise ValueError('Regression mode requires --output-dir')
    args.output_dir.resolve().mkdir(parents=True, exist_ok=True)
    run_regression(args.output_dir.resolve())


if __name__ == '__main__':
    main(sys.argv[sys.argv.index('--') + 1:])
