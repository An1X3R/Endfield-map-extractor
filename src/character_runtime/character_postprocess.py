"""Own opt-in, scene-local Blender compositor grading and portable presets."""
from __future__ import annotations

import json
import math
import os
from pathlib import Path
import tempfile
from typing import TypedDict

import bpy


STAGE_MARKER = 'endfield_postprocess_stage'
STATE_KEY = 'endfield_postprocess_state'
CURVES_NODE_NAME = 'Endfield Style Curves'
DEFAULT_DITHER = 1.0
FLOAT_LIMIT = 3.4028234663852886e38
INFRASTRUCTURE_MARKER = 'endfield_postprocess_infrastructure'
CONTROL_SPECS: tuple[tuple[str, float, float, float, str], ...] = (
    ('Exposure EV', 0.0, -8.0, 8.0, 'Basic'),
    ('Contrast', 0.0, -100.0, 100.0, 'Basic'),
    ('Saturation', 1.0, 0.0, 2.0, 'Basic'),
    ('Bloom Strength', 0.0, 0.0, 2.0, 'Bloom'),
    ('Bloom Threshold', 1.0, 0.0, 32.0, 'Bloom'),
    ('Bloom Softness', 0.25, 0.001, 4.0, 'Bloom'),
    ('Bloom Radius', 32.0, 0.0, 256.0, 'Bloom'),
    ('Style Mix', 1.0, 0.0, 1.0, 'Style'),
    ('Sharpen', 0.0, 0.0, 1.0, 'Finish'),
)


class CurvePoint(TypedDict):
    x: float
    y: float
    handle: str


class CurvePreset(TypedDict):
    tone: str
    black_level: list[float]
    white_level: list[float]
    clip_min_x: float
    clip_min_y: float
    clip_max_x: float
    clip_max_y: float
    extend: str
    use_clip: bool
    channels: list[list[CurvePoint]]


class StagePreset(TypedDict):
    controls: dict[str, float]
    curves: CurvePreset


class PostprocessPreset(TypedDict):
    version: int
    dither_intensity: float
    stages: list[StagePreset]


class PresetValidationError(ValueError):
    """Reject an incomplete or unsafe preset before touching the compositor."""


def _tree(scene: bpy.types.Scene) -> bpy.types.NodeTree | None:
    if hasattr(scene, 'compositing_node_group'):
        return scene.compositing_node_group
    return scene.node_tree


def stage_nodes(scene: bpy.types.Scene) -> tuple[bpy.types.Node, ...]:
    """Return final-image group instances in this scene without modifying data."""
    tree = _tree(scene)
    return tuple(node for node in tree.nodes if node.get(STAGE_MARKER) == 1) if tree else ()


def find_group(scene: bpy.types.Scene) -> bpy.types.Node | None:
    stages = stage_nodes(scene)
    return stages[0] if stages else None


def is_shared(scene: bpy.types.Scene) -> bool:
    """Report whether scene-local editing first requires explicit isolation."""
    tree = _tree(scene)
    if tree is None:
        return False
    if any(other != scene and _tree(other) == tree for other in bpy.data.scenes):
        return True
    stages = stage_nodes(scene)
    for stage in stages:
        group = stage.node_tree
        if group is None:
            raise ValueError(f'Postprocess group is missing: scene={scene.name}, node={stage.name}')
        if group.users - int(group.use_fake_user) > sum(node.node_tree == group for node in stages):
            return True
    return False


def _isolate(scene: bpy.types.Scene) -> None:
    tree = _tree(scene)
    if tree is None:
        return
    if hasattr(scene, 'compositing_node_group') and any(
        other != scene and _tree(other) == tree for other in bpy.data.scenes
    ):
        scene.compositing_node_group = tree.copy()
    stages = stage_nodes(scene)
    replacements: dict[int, bpy.types.NodeTree] = {}
    for stage in stages:
        group = stage.node_tree
        if group is None:
            raise ValueError(f'Postprocess group is missing: scene={scene.name}, node={stage.name}')
        shared_elsewhere = any(
            other != scene and any(node.node_tree == group for node in stage_nodes(other))
            for other in bpy.data.scenes
        )
        instance_count = sum(node.node_tree == group for node in stages)
        if shared_elsewhere or group.users - int(group.use_fake_user) > instance_count:
            pointer = group.as_pointer()
            if pointer not in replacements:
                replacements[pointer] = group.copy()
            stage.node_tree = replacements[pointer]


def _required_stages(scene: bpy.types.Scene) -> tuple[bpy.types.Node, ...]:
    stages = stage_nodes(scene)
    if not stages:
        raise ValueError(f'Postprocess is not installed: scene={scene.name}')
    return stages


def _math(tree: bpy.types.NodeTree, operation: str, left: bpy.types.NodeSocket,
          right: bpy.types.NodeSocket | float) -> bpy.types.NodeSocket:
    node = tree.nodes.new('CompositorNodeMath')
    node.operation = operation
    tree.links.new(left, node.inputs[0])
    if isinstance(right, bpy.types.NodeSocket):
        tree.links.new(right, node.inputs[1])
    else:
        node.inputs[1].default_value = right
    return node.outputs[0]


def _group(scene: bpy.types.Scene) -> bpy.types.NodeTree:
    tree = bpy.data.node_groups.new(f'Endfield Postprocess - {scene.name}', 'CompositorNodeTree')
    tree['endfield_postprocess_native'] = 1
    tree.interface.new_socket(name='Image', in_out='INPUT', socket_type='NodeSocketColor')
    panels: dict[str, bpy.types.NodeTreeInterfacePanel] = {}
    for name, default, minimum, maximum, panel_name in CONTROL_SPECS:
        if panel_name not in panels:
            panels[panel_name] = tree.interface.new_panel(name=panel_name)
        socket = tree.interface.new_socket(name=name, in_out='INPUT', socket_type='NodeSocketFloat', parent=panels[panel_name])
        socket.default_value = default
        socket.min_value = minimum
        socket.max_value = maximum
    tree.interface.new_socket(name='Image', in_out='OUTPUT', socket_type='NodeSocketColor')
    source = tree.nodes.new('NodeGroupInput')
    output = tree.nodes.new('NodeGroupOutput')
    straight = tree.nodes.new('CompositorNodePremulKey')
    straight.mapping = 'PREMUL_TO_STRAIGHT'
    tree.links.new(source.outputs['Image'], straight.inputs['Image'])
    exposure = tree.nodes.new('CompositorNodeExposure')
    tree.links.new(straight.outputs[0], exposure.inputs['Image'])
    tree.links.new(source.outputs['Exposure EV'], exposure.inputs['Exposure'])
    contrast = tree.nodes.new('CompositorNodeBrightContrast')
    contrast.use_premultiply = False
    tree.links.new(exposure.outputs[0], contrast.inputs['Image'])
    tree.links.new(source.outputs['Contrast'], contrast.inputs['Contrast'])
    saturation = tree.nodes.new('CompositorNodeHueSat')
    tree.links.new(contrast.outputs[0], saturation.inputs['Image'])
    tree.links.new(source.outputs['Saturation'], saturation.inputs['Saturation'])
    curves = tree.nodes.new('CompositorNodeCurveRGB')
    curves.name = CURVES_NODE_NAME
    curves.label = 'Editable RGB style - no external LUT'
    curves.mapping.initialize()
    tree.links.new(saturation.outputs[0], curves.inputs['Image'])
    tree.links.new(source.outputs['Style Mix'], curves.inputs['Fac'])
    luminance = tree.nodes.new('CompositorNodeRGBToBW')
    tree.links.new(curves.outputs[0], luminance.inputs[0])
    mask = _math(tree, 'SUBTRACT', luminance.outputs[0], source.outputs['Bloom Threshold'])
    mask = _math(tree, 'DIVIDE', mask, source.outputs['Bloom Softness'])
    mask = _math(tree, 'MAXIMUM', mask, 0.0)
    mask = _math(tree, 'MINIMUM', mask, 1.0)
    highlight = tree.nodes.new('CompositorNodeMixRGB')
    highlight.blend_type = 'MULTIPLY'
    highlight.inputs[0].default_value = 1.0
    tree.links.new(curves.outputs[0], highlight.inputs[1])
    tree.links.new(mask, highlight.inputs[2])
    blur = tree.nodes.new('CompositorNodeBlur')
    blur.label = 'Native Gaussian bloom - radius in pixels'
    blur.filter_type = 'GAUSS'
    blur.size_x = 256
    blur.size_y = 256
    tree.links.new(highlight.outputs[0], blur.inputs['Image'])
    radius = _math(tree, 'DIVIDE', source.outputs['Bloom Radius'], 256.0)
    tree.links.new(radius, blur.inputs['Size'])
    strength = tree.nodes.new('CompositorNodeMixRGB')
    strength.blend_type = 'MULTIPLY'
    strength.inputs[0].default_value = 1.0
    tree.links.new(blur.outputs[0], strength.inputs[1])
    tree.links.new(source.outputs['Bloom Strength'], strength.inputs[2])
    bloom = tree.nodes.new('CompositorNodeMixRGB')
    bloom.blend_type = 'ADD'
    bloom.inputs[0].default_value = 1.0
    tree.links.new(curves.outputs[0], bloom.inputs[1])
    tree.links.new(strength.outputs[0], bloom.inputs[2])
    sharpen = tree.nodes.new('CompositorNodeFilter')
    sharpen.filter_type = 'SHARPEN'
    tree.links.new(bloom.outputs[0], sharpen.inputs['Image'])
    tree.links.new(source.outputs['Sharpen'], sharpen.inputs['Fac'])
    separate = tree.nodes.new('CompositorNodeSepRGBA')
    tree.links.new(source.outputs['Image'], separate.inputs['Image'])
    alpha = tree.nodes.new('CompositorNodeSetAlpha')
    alpha.mode = 'REPLACE_ALPHA'
    tree.links.new(sharpen.outputs[0], alpha.inputs['Image'])
    tree.links.new(separate.outputs['A'], alpha.inputs['Alpha'])
    premultiplied = tree.nodes.new('CompositorNodePremulKey')
    premultiplied.mapping = 'STRAIGHT_TO_PREMUL'
    tree.links.new(alpha.outputs[0], premultiplied.inputs['Image'])
    tree.links.new(premultiplied.outputs[0], output.inputs['Image'])
    basic_frame = tree.nodes.new('NodeFrame')
    basic_frame.label = '01 - Straight color grading'
    style_frame = tree.nodes.new('NodeFrame')
    style_frame.label = '02 - Editable RGB style'
    bloom_frame = tree.nodes.new('NodeFrame')
    bloom_frame.label = '03 - Native Gaussian highlight bloom'
    finish_frame = tree.nodes.new('NodeFrame')
    finish_frame.label = '04 - Sharpen and restore premultiplied alpha'
    main_nodes = (source, straight, exposure, contrast, saturation, curves, bloom,
                  sharpen, alpha, premultiplied, output)
    for index, node in enumerate(main_nodes):
        node.location = (220.0 * index, 0.0)
        node.width = 175.0
        if node in (straight, exposure, contrast, saturation):
            node.parent = basic_frame
        elif node == curves:
            node.parent = style_frame
        elif node in (sharpen, alpha, premultiplied):
            node.parent = finish_frame
    separate.location = (1540.0, -600.0)
    separate.label = 'Preserve source alpha'
    branch_nodes = tuple(node for node in tree.nodes if node not in main_nodes
                         and node not in (separate, basic_frame, style_frame, bloom_frame, finish_frame))
    for index, node in enumerate(branch_nodes):
        node.location = (440.0 + 190.0 * (index % 5), -420.0 - 260.0 * (index // 5))
        node.parent = bloom_frame
    source.label = 'Scene-local controls'
    straight.label = 'Unpremultiply before grading'
    exposure.label = 'Exposure in stops'
    contrast.label = 'Contrast'
    saturation.label = 'Saturation'
    bloom.label = 'Add highlight bloom'
    sharpen.label = 'Optional sharpen'
    alpha.label = 'Original alpha'
    premultiplied.label = 'Premultiply for composition'
    return tree


def _final_links(tree: bpy.types.NodeTree) -> tuple[bpy.types.NodeLink, ...]:
    targets = tuple(node for node in tree.nodes if node.bl_idname in (
        'CompositorNodeComposite', 'CompositorNodeViewer', 'NodeGroupOutput'
    ) and getattr(node, 'is_active_output', True))
    return tuple(link for link in tree.links if link.to_node in targets
                 and link.to_socket.name == 'Image')


def install(scene: bpy.types.Scene) -> bpy.types.Node:
    """Insert opt-in grading on each existing final image, preserving all sources."""
    _isolate(scene)
    existing = stage_nodes(scene)
    if existing:
        scene.render.use_compositing = True
        return existing[0]
    originally_enabled = getattr(scene, 'use_nodes', True)
    originally_missing = _tree(scene) is None
    if hasattr(scene, 'compositing_node_group'):
        if scene.compositing_node_group is None:
            tree = bpy.data.node_groups.new(f'{scene.name} Compositor', 'CompositorNodeTree')
            scene.compositing_node_group = tree
            tree.interface.new_socket(name='Image', in_out='OUTPUT', socket_type='NodeSocketColor')
            render = tree.nodes.new('CompositorNodeRLayers')
            output = tree.nodes.new('NodeGroupOutput')
            render[INFRASTRUCTURE_MARKER] = 1
            output[INFRASTRUCTURE_MARKER] = 1
            tree.links.new(render.outputs['Image'], output.inputs['Image'])
    else:
        scene.use_nodes = True
    tree = _tree(scene)
    if tree is None:
        raise RuntimeError(f'Cannot initialize compositor: scene={scene.name}')
    finals = _final_links(tree)
    if not finals:
        if not originally_enabled and hasattr(scene, 'use_nodes'):
            scene.use_nodes = False
        raise ValueError(f'No linked final Image input in compositor: scene={scene.name}')
    scene[STATE_KEY] = json.dumps({'use_nodes': originally_enabled, 'use_compositing': scene.render.use_compositing, 'tree_missing': originally_missing, 'dither_intensity': scene.render.dither_intensity})
    scene.render.use_compositing = True
    instances: dict[tuple[int, str], bpy.types.Node] = {}
    for link in finals:
        source = link.from_socket
        destination = link.to_socket
        key = (link.from_node.as_pointer(), source.identifier)
        if key not in instances:
            stage = tree.nodes.new('CompositorNodeGroup')
            stage.node_tree = _group(scene)
            stage[STAGE_MARKER] = 1
            stage.label = 'Endfield - native scene postprocess'
            stage.location = (link.to_node.location.x - 280.0, link.to_node.location.y)
            tree.links.new(source, stage.inputs['Image'])
            instances[key] = stage
        tree.links.remove(link)
        tree.links.new(instances[key].outputs['Image'], destination)
    return next(iter(instances.values()))


def bypass(scene: bpy.types.Scene) -> None:
    """Mute image grading while keeping controls and native output dither intact."""
    _isolate(scene)
    for stage in _required_stages(scene):
        stage.mute = True


def is_bypassed(scene: bpy.types.Scene) -> bool:
    return all(stage.mute for stage in _required_stages(scene))


def resume(scene: bpy.types.Scene) -> None:
    _isolate(scene)
    for stage in _required_stages(scene):
        stage.mute = False


def _reset_curves(stage: bpy.types.Node) -> None:
    mapping = stage.node_tree.nodes[CURVES_NODE_NAME].mapping
    mapping.initialize()
    for curve in mapping.curves:
        while len(curve.points) > 2:
            curve.points.remove(curve.points[1])
        curve.points[0].location = (0.0, 0.0)
        curve.points[1].location = (1.0, 1.0)
        for point in curve.points:
            point.handle_type = 'AUTO'
    mapping.tone = 'STANDARD'
    mapping.extend = 'EXTRAPOLATED'
    mapping.use_clip = True
    mapping.clip_min_x = 0.0
    mapping.clip_min_y = 0.0
    mapping.clip_max_x = 1.0
    mapping.clip_max_y = 1.0
    mapping.black_level = (0.0, 0.0, 0.0)
    mapping.white_level = (1.0, 1.0, 1.0)
    mapping.update()


def reset_defaults(scene: bpy.types.Scene) -> None:
    """Reset owned grading and output dither without altering color management."""
    _isolate(scene)
    stages = _required_stages(scene)
    for stage in stages:
        for name, _, _, _, _ in CONTROL_SPECS:
            if stage.inputs[name].is_linked:
                raise ValueError(f'Cannot reset externally driven postprocess input: node={stage.name}, socket={name}')
    for stage in stages:
        for name, default, _, _, _ in CONTROL_SPECS:
            stage.inputs[name].default_value = default
        _reset_curves(stage)
    scene.render.dither_intensity = DEFAULT_DITHER


def remove(scene: bpy.types.Scene) -> None:
    """Reconnect current image sources, including user edits made after install."""
    _isolate(scene)
    stages = _required_stages(scene)
    tree = _tree(scene)
    routes: list[tuple[bpy.types.NodeSocket, tuple[bpy.types.NodeSocket, ...]]] = []
    for stage in stages:
        incoming = tuple(stage.inputs['Image'].links)
        destinations = tuple(link.to_socket for link in stage.outputs['Image'].links)
        if destinations and len(incoming) != 1:
            raise ValueError(f'Cannot restore ambiguous postprocess input: scene={scene.name}, node={stage.name}, image_links={len(incoming)}')
        if incoming:
            if incoming[0].from_node in stages or any(socket.node in stages for socket in destinations):
                raise ValueError(f'Cannot remove interlinked postprocess stages: scene={scene.name}, node={stage.name}')
            routes.append((incoming[0].from_socket, destinations))
    state = json.loads(scene[STATE_KEY])
    groups = {stage.node_tree for stage in stages}
    for stage in stages:
        tree.nodes.remove(stage)
    for source, destinations in routes:
        for destination in destinations:
            tree.links.new(source, destination)
    for group in groups:
        if group.users == 0:
            bpy.data.node_groups.remove(group)
    scene.render.dither_intensity = state['dither_intensity']
    scene.render.use_compositing = state['use_compositing']
    if hasattr(scene, 'use_nodes'):
        scene.use_nodes = state['use_nodes']
    if state['tree_missing'] and hasattr(scene, 'compositing_node_group'):
        infrastructure = tuple(node for node in tree.nodes if node.get(INFRASTRUCTURE_MARKER) == 1)
        if len(tree.nodes) == 2 and len(infrastructure) == 2 and len(tree.links) == 1:
            link = tree.links[0]
            if (link.from_node.bl_idname == 'CompositorNodeRLayers'
                    and link.to_node.bl_idname == 'NodeGroupOutput'
                    and link.from_socket.name == 'Image' and link.to_socket.name == 'Image'):
                scene.compositing_node_group = None
                if tree.users == 0:
                    bpy.data.node_groups.remove(tree)
    del scene[STATE_KEY]


def _object(value: object, path: str) -> dict[str, object]:
    if not isinstance(value, dict) or not all(isinstance(key, str) for key in value):
        raise PresetValidationError(f'Expected object: path={path}')
    return value


def _number(value: object, minimum: float, maximum: float, path: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise PresetValidationError(f'Expected finite number in [{minimum}, {maximum}]: path={path}, value={value!r}')
    if not math.isfinite(value) or not minimum <= value <= maximum:
        raise PresetValidationError(f'Expected finite number in [{minimum}, {maximum}]: path={path}, value={value!r}')
    return float(value)


def _level(value: object, path: str) -> list[float]:
    if not isinstance(value, list) or len(value) != 3:
        raise PresetValidationError(f'Expected three RGB components: path={path}')
    return [_number(component, -FLOAT_LIMIT, FLOAT_LIMIT, path) for component in value]


def validate_preset(value: object) -> PostprocessPreset:
    """Validate required fields and drop unrelated JSON fields recursively."""
    root = _object(value, '$')
    if type(root.get('version')) is not int or root['version'] != 1:
        raise PresetValidationError(f'Unsupported preset version: value={root.get("version")!r}')
    raw_stages = root.get('stages')
    if not isinstance(raw_stages, list) or not 1 <= len(raw_stages) <= 64:
        raise PresetValidationError('Expected 1 to 64 stage objects: path=$.stages')
    stages: list[StagePreset] = []
    for index, raw_stage in enumerate(raw_stages):
        path = f'$.stages[{index}]'
        stage = _object(raw_stage, path)
        raw_controls = _object(stage.get('controls'), path + '.controls')
        controls = {name: _number(raw_controls.get(name), low, high, path + '.controls.' + name)
                    for name, _, low, high, _ in CONTROL_SPECS}
        raw_curves = _object(stage.get('curves'), path + '.curves')
        tone = raw_curves.get('tone')
        if tone not in ('STANDARD', 'FILMLIKE'):
            raise PresetValidationError(f'Invalid curve tone: path={path}.curves.tone, value={tone!r}')
        black_level = _level(raw_curves.get('black_level'), path + '.curves.black_level')
        white_level = _level(raw_curves.get('white_level'), path + '.curves.white_level')
        clip_min_x = _number(raw_curves.get('clip_min_x'), -100.0, 100.0, path + '.curves.clip_min_x')
        clip_min_y = _number(raw_curves.get('clip_min_y'), -100.0, 100.0, path + '.curves.clip_min_y')
        clip_max_x = _number(raw_curves.get('clip_max_x'), -100.0, 100.0, path + '.curves.clip_max_x')
        clip_max_y = _number(raw_curves.get('clip_max_y'), -100.0, 100.0, path + '.curves.clip_max_y')
        if clip_min_x >= clip_max_x or clip_min_y >= clip_max_y:
            raise PresetValidationError(f'Curve clip bounds must have positive extent: path={path}.curves')
        extend = raw_curves.get('extend')
        use_clip = raw_curves.get('use_clip')
        if extend not in ('HORIZONTAL', 'EXTRAPOLATED') or type(use_clip) is not bool:
            raise PresetValidationError(f'Invalid curve settings: path={path}.curves')
        raw_channels = raw_curves.get('channels')
        if not isinstance(raw_channels, list) or len(raw_channels) != 4:
            raise PresetValidationError(f'Expected four curve channels: path={path}.curves.channels')
        channels: list[list[CurvePoint]] = []
        for channel_index, raw_channel in enumerate(raw_channels):
            if not isinstance(raw_channel, list) or not 2 <= len(raw_channel) <= 256:
                raise PresetValidationError(f'Expected 2 to 256 curve points: path={path}, channel={channel_index}')
            channel: list[CurvePoint] = []
            for raw_point in raw_channel:
                point = _object(raw_point, path + '.curves.point')
                handle = point.get('handle')
                if handle not in ('AUTO', 'AUTO_CLAMPED', 'VECTOR'):
                    raise PresetValidationError(f'Invalid curve handle: path={path}, value={handle!r}')
                channel.append({'x': _number(point.get('x'), -FLOAT_LIMIT, FLOAT_LIMIT, path + '.curves.x'),
                                'y': _number(point.get('y'), -FLOAT_LIMIT, FLOAT_LIMIT, path + '.curves.y'), 'handle': handle})
            if any(left['x'] >= right['x'] for left, right in zip(channel, channel[1:])):
                raise PresetValidationError(f'Curve x coordinates must strictly increase: path={path}, channel={channel_index}')
            channels.append(channel)
        stages.append({'controls': controls, 'curves': {'tone': str(tone), 'black_level': black_level, 'white_level': white_level,
                    'clip_min_x': clip_min_x, 'clip_min_y': clip_min_y, 'clip_max_x': clip_max_x, 'clip_max_y': clip_max_y,
                    'extend': str(extend), 'use_clip': use_clip, 'channels': channels}})
    return {'version': 1, 'dither_intensity': _number(root.get('dither_intensity'), 0.0, 2.0, '$.dither_intensity'), 'stages': stages}


def _preset(scene: bpy.types.Scene) -> PostprocessPreset:
    stages: list[StagePreset] = []
    for stage in _required_stages(scene):
        for name, _, _, _, _ in CONTROL_SPECS:
            if stage.inputs[name].is_linked:
                raise ValueError(f'Cannot export externally driven input: node={stage.name}, socket={name}')
        mapping = stage.node_tree.nodes[CURVES_NODE_NAME].mapping
        mapping.initialize()
        controls = {name: float(stage.inputs[name].default_value) for name, _, _, _, _ in CONTROL_SPECS}
        channels: list[list[CurvePoint]] = [[{'x': float(point.location.x), 'y': float(point.location.y), 'handle': point.handle_type}
                     for point in curve.points] for curve in mapping.curves]
        stages.append({'controls': controls, 'curves': {'tone': mapping.tone, 'black_level': list(mapping.black_level), 'white_level': list(mapping.white_level),
                    'clip_min_x': mapping.clip_min_x, 'clip_min_y': mapping.clip_min_y,
                    'clip_max_x': mapping.clip_max_x, 'clip_max_y': mapping.clip_max_y,
                    'extend': mapping.extend, 'use_clip': mapping.use_clip, 'channels': channels}})
    return validate_preset({'version': 1, 'dither_intensity': float(scene.render.dither_intensity), 'stages': stages})


def export_preset(scene: bpy.types.Scene, path: str | Path) -> None:
    """Write a validated preset with atomic replacement in the destination folder."""
    preset = _preset(scene)
    destination = Path(path)
    descriptor, temporary = tempfile.mkstemp(prefix=destination.name + '.', suffix='.tmp', dir=destination.parent)
    try:
        with os.fdopen(descriptor, 'w', encoding='utf-8') as handle:
            json.dump(preset, handle, ensure_ascii=False, allow_nan=False, indent=2)
            handle.write('\n')
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, destination)
    finally:
        Path(temporary).unlink(missing_ok=True)


def import_preset(scene: bpy.types.Scene, path: str | Path) -> None:
    """Validate the complete preset before changing existing scene parameters."""
    with Path(path).open('r', encoding='utf-8') as handle:
        preset = validate_preset(json.load(handle))
    stages = _required_stages(scene)
    if len(stages) != len(preset['stages']):
        raise PresetValidationError(f'Preset stage count mismatch: scene={scene.name}, expected={len(stages)}, actual={len(preset["stages"])}')
    for stage in stages:
        for name, _, _, _, _ in CONTROL_SPECS:
            if stage.inputs[name].is_linked:
                raise ValueError(f'Cannot import over externally driven input: node={stage.name}, socket={name}')
    _isolate(scene)
    for stage, values in zip(stage_nodes(scene), preset['stages']):
        for name, value in values['controls'].items():
            stage.inputs[name].default_value = value
        _reset_curves(stage)
        mapping = stage.node_tree.nodes[CURVES_NODE_NAME].mapping
        mapping.tone = values['curves']['tone']
        mapping.black_level = values['curves']['black_level']
        mapping.white_level = values['curves']['white_level']
        mapping.clip_min_x = values['curves']['clip_min_x']
        mapping.clip_min_y = values['curves']['clip_min_y']
        mapping.clip_max_x = values['curves']['clip_max_x']
        mapping.clip_max_y = values['curves']['clip_max_y']
        mapping.extend = values['curves']['extend']
        mapping.use_clip = values['curves']['use_clip']
        for curve, points in zip(mapping.curves, values['curves']['channels']):
            curve.points[0].location = (points[0]['x'], points[0]['y'])
            curve.points[1].location = (points[-1]['x'], points[-1]['y'])
            for point in points[1:-1]:
                curve.points.new(point['x'], point['y'])
            mapping.update()
            for point, value in zip(curve.points, points):
                point.handle_type = value['handle']
        mapping.update()
    scene.render.dither_intensity = preset['dither_intensity']
