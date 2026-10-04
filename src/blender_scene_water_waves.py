"""Build world-space water wave normals from exact scene water bindings.

The source GPU time uniform and view-dependent normal attenuation are not
recovered here. Blender scene seconds drive the editable wave time control.
"""

from __future__ import annotations

import json
import math
from dataclasses import is_dataclass
from pathlib import Path
from typing import TYPE_CHECKING, cast

import bpy

if TYPE_CHECKING:
    from endfield_scene_water_materials import WaterMaterialRow


Scalar = float | bpy.types.NodeSocket
Vector = tuple[float, float, float] | bpy.types.NodeSocket


def _record(value: object, label: str) -> dict[str, object]:
    if not isinstance(value, dict) or any(not isinstance(key, str) for key in value):
        raise ValueError(f"WATER_WAVE_RECORD_INVALID field={label}")
    return cast(dict[str, object], value)


def _number(value: object, label: str) -> float:
    if type(value) not in (int, float) or not math.isfinite(float(value)):
        raise ValueError(f"WATER_WAVE_NUMBER_INVALID field={label} value={value}")
    return float(value)


def _integer(value: object, label: str) -> int:
    if type(value) is not int:
        raise ValueError(f"WATER_WAVE_INTEGER_INVALID field={label} value={value}")
    return value


def _source(spec: WaterMaterialRow) -> tuple[dict[str, object], dict[str, object]]:
    if (not is_dataclass(spec) or type(spec).__name__ != "WaterMaterialRow" or
            type(spec).__module__ != "endfield_scene_water_materials"):
        raise TypeError(f"WATER_WAVE_SPEC_INVALID type={type(spec).__name__}")
    config = _record(json.loads(spec.config_source_json), "config")
    global_config = _record(json.loads(spec.global_source_json), "global")
    flow = _record(config.get("Flow"), "config.Flow")
    if _integer(flow.get("UseFlowMap"), "Flow.UseFlowMap") != int(spec.flow.use_flow_map):
        raise ValueError(f"WATER_WAVE_FLOW_BINDING_MISMATCH entity={spec.entity_id}")
    for key, expected in (
        ("FlowSpeed", spec.flow.flow_speed),
        ("LargeWaveFlowSpeed", spec.flow.large_wave_flow_speed),
        ("NormalTilling", spec.flow.normal_tilling),
        ("LargeWaveNormalTilling", spec.flow.large_wave_normal_tilling),
        ("NormalScale", spec.flow.normal_scale),
        ("LargeWaveNormalScale", spec.flow.large_wave_normal_scale),
    ):
        if _number(flow.get(key), f"Flow.{key}") != expected:
            raise ValueError(f"WATER_WAVE_CONFIG_BINDING_MISMATCH entity={spec.entity_id} field={key}")
    for key in ("FlowFullCycle", "FlowFullCycleLarge"):
        _number(flow.get(key), f"Flow.{key}")
    if _integer(flow.get("LargeWaveInvDir"), "Flow.LargeWaveInvDir") not in (0, 1):
        raise ValueError(f"WATER_WAVE_INV_DIR_INVALID entity={spec.entity_id}")
    direction = _record(flow.get("FlowDirectionVector2"), "Flow.FlowDirectionVector2")
    if (_number(direction.get("X"), "FlowDirectionVector2.X"),
            _number(direction.get("Y"), "FlowDirectionVector2.Y")) != spec.flow.flow_direction_vector:
        raise ValueError(f"WATER_WAVE_DIRECTION_BINDING_MISMATCH entity={spec.entity_id}")
    map_size = _integer(global_config.get("MapSize"), "Global.MapSize")
    sector_size = _integer(global_config.get("SectorSize"), "Global.SectorSize")
    if map_size <= 0 or sector_size <= 0 or map_size % sector_size:
        raise ValueError(f"WATER_WAVE_GRID_INVALID map={map_size} sector={sector_size}")
    center = _record(global_config.get("SceneCenterOffset"), "Global.SceneCenterOffset")
    _number(center.get("X"), "SceneCenterOffset.X")
    _number(center.get("Y"), "SceneCenterOffset.Y")
    if spec.flow.use_flow_map and (not spec.sector_complete or spec.sector_mapping_status != "native_grid_verified"):
        raise ValueError(
            f"WATER_WAVE_SECTORS_UNRESOLVED entity={spec.entity_id} status={spec.sector_mapping_status}"
        )
    if spec.flow.use_flow_map and len(spec.sectors) > 20:
        raise ValueError(f"WATER_WAVE_SAMPLER_BUDGET entity={spec.entity_id} sectors={len(spec.sectors)} max=20")
    for sector in spec.sectors:
        record = _record(json.loads(sector.source_json), f"sector:{sector.resource_path}")
        pointer = _record(record.get("WaterSectorTexture0"), "WaterSectorTexture0")
        target = _record(pointer.get("Target"), "WaterSectorTexture0.Target")
        if (target.get("Source") != sector.texture.asset.source or
                _integer(target.get("PathId"), "WaterSectorTexture0.PathId") != sector.texture.asset.path_id or
                target.get("Type") != sector.texture.asset.type_name):
            raise ValueError(f"WATER_WAVE_SECTOR_TEXTURE_MISMATCH path={sector.resource_path}")
    if spec.small_wave_normal.slice_index != spec.flow.small_wave_normal_type or \
            spec.large_wave_normal.slice_index != spec.flow.large_wave_normal_type:
        raise ValueError(f"WATER_WAVE_NORMAL_SLICE_MISMATCH entity={spec.entity_id}")
    return flow, global_config


def _image(file: str) -> bpy.types.Image:
    path = Path(file).resolve()
    if not path.is_file():
        raise FileNotFoundError(f"WATER_WAVE_TEXTURE_MISSING path={path}")
    for image in bpy.data.images:
        if image.get("endfield_wave_file") == str(path):
            if image.colorspace_settings.name != "Non-Color" or image.alpha_mode != "CHANNEL_PACKED":
                raise ValueError(f"WATER_WAVE_IMAGE_CHANNEL_MODE_CONFLICT image={image.name} path={path}")
            return image
    image = bpy.data.images.load(str(path), check_existing=True)
    if image.colorspace_settings.name != "Non-Color" or image.alpha_mode != "CHANNEL_PACKED":
        if image.users:
            image = image.copy()
            image.name = f"EF_Wave_{path.stem}"
        image.colorspace_settings.name = "Non-Color"
        image.alpha_mode = "CHANNEL_PACKED"
    image["endfield_wave_file"] = str(path)
    return image


def _node(tree: bpy.types.NodeTree, kind: str, name: str) -> bpy.types.Node:
    node = tree.nodes.new(kind)
    node.name = f"EF_Wave_{name}"
    node.label = name.replace("_", " ")
    index = len(tree.nodes) - 1
    node.location = ((index // 12) * 240, -(index % 12) * 170)
    return node


def _mark_new_nodes(tree: bpy.types.NodeTree, start: int, section: str, note: str) -> None:
    for node in tuple(tree.nodes)[start:]:
        node["endfield_layout_section"] = section
        node["endfield_layout_note"] = note


def _input(tree: bpy.types.NodeTree, socket: bpy.types.NodeSocket, value: Scalar | Vector) -> None:
    if isinstance(value, bpy.types.NodeSocket):
        tree.links.new(value, socket)
    else:
        socket.default_value = value


def _math(tree: bpy.types.NodeTree, name: str, operation: str, a: Scalar, b: Scalar) -> bpy.types.NodeSocket:
    node = _node(tree, "ShaderNodeMath", name)
    node.operation = operation
    _input(tree, node.inputs[0], a)
    _input(tree, node.inputs[1], b)
    return node.outputs[0]


def _vector(tree: bpy.types.NodeTree, name: str, operation: str, a: Vector, b: Vector) -> bpy.types.NodeSocket:
    node = _node(tree, "ShaderNodeVectorMath", name)
    node.operation = operation
    _input(tree, node.inputs[0], a)
    _input(tree, node.inputs[1], b)
    return node.outputs[0]


def _scale(tree: bpy.types.NodeTree, name: str, vector: Vector, scale: Scalar) -> bpy.types.NodeSocket:
    node = _node(tree, "ShaderNodeVectorMath", name)
    node.operation = "SCALE"
    _input(tree, node.inputs[0], vector)
    _input(tree, node.inputs[3], scale)
    return node.outputs[0]


def _components(tree: bpy.types.NodeTree, name: str, vector: bpy.types.NodeSocket) -> bpy.types.Node:
    node = _node(tree, "ShaderNodeSeparateXYZ", name)
    tree.links.new(vector, node.inputs["Vector"])
    return node


def _combine(tree: bpy.types.NodeTree, name: str, x: Scalar, y: Scalar, z: Scalar) -> bpy.types.NodeSocket:
    node = _node(tree, "ShaderNodeCombineXYZ", name)
    _input(tree, node.inputs["X"], x)
    _input(tree, node.inputs["Y"], y)
    _input(tree, node.inputs["Z"], z)
    return node.outputs["Vector"]


def _mix_color(
    tree: bpy.types.NodeTree, name: str, factor: Scalar,
    a: bpy.types.NodeSocket, b: bpy.types.NodeSocket,
) -> bpy.types.NodeSocket:
    node = _node(tree, "ShaderNodeMixRGB", name)
    node.blend_type = "MIX"
    node.use_alpha = False
    _input(tree, node.inputs[0], factor)
    tree.links.new(a, node.inputs[1])
    tree.links.new(b, node.inputs[2])
    return node.outputs[0]


def _mix_float(
    tree: bpy.types.NodeTree, name: str, factor: Scalar,
    a: Scalar, b: Scalar,
) -> bpy.types.NodeSocket:
    left = _math(tree, f"{name}_Inverse", "SUBTRACT", 1.0, factor)
    first = _math(tree, f"{name}_First", "MULTIPLY", a, left)
    second = _math(tree, f"{name}_Second", "MULTIPLY", b, factor)
    return _math(tree, f"{name}_Sum", "ADD", first, second)


def _texture(
    tree: bpy.types.NodeTree, name: str, image: bpy.types.Image,
    uv: bpy.types.NodeSocket,
) -> bpy.types.Node:
    node = _node(tree, "ShaderNodeTexImage", name)
    node.image = image
    node.extension = "REPEAT"
    node.interpolation = "Linear"
    tree.links.new(uv, node.inputs["Vector"])
    return node


def _world_xz(tree: bpy.types.NodeTree) -> bpy.types.NodeSocket:
    geometry = _node(tree, "ShaderNodeNewGeometry", "World_Position")
    position = _components(tree, "World_Position_XYZ", geometry.outputs["Position"])
    negative_y = _math(tree, "Unity_Z_From_Blender_Y", "MULTIPLY", position.outputs["Y"], -1.0)
    return _combine(tree, "World_XZ", position.outputs["X"], negative_y, 0.0)


def _time(tree: bpy.types.NodeTree) -> bpy.types.NodeSocket:
    scene = bpy.context.scene
    if scene is None or scene.render.fps <= 0 or scene.render.fps_base <= 0:
        raise ValueError("WATER_WAVE_SCENE_FRAME_RATE_INVALID")
    seconds = _node(tree, "ShaderNodeValue", "Time_Seconds")
    seconds.label = "Blender seconds (source GPU time scale unverified)"
    driver = seconds.outputs[0].driver_add("default_value").driver
    driver.type = "SCRIPTED"
    for name, data_path in (("fps", "render.fps"), ("fps_base", "render.fps_base")):
        variable = driver.variables.new()
        variable.name = name
        variable.type = "SINGLE_PROP"
        variable.targets[0].id_type = "SCENE"
        variable.targets[0].id = scene
        variable.targets[0].data_path = data_path
    driver.expression = "frame * fps_base / fps"
    speed = _node(tree, "ShaderNodeValue", "Time_Scale")
    speed.label = "Editable source-time multiplier"
    speed.outputs[0].default_value = 1.0
    return _math(tree, "Source_Time", "MULTIPLY", seconds.outputs[0], speed.outputs[0])


def _sector_mask(
    tree: bpy.types.NodeTree, name: str, world: bpy.types.NodeSocket,
    xmin: float, xmax: float, zmin: float, zmax: float,
) -> bpy.types.NodeSocket:
    parts = _components(tree, f"{name}_World_XZ", world)
    x_lower = _math(tree, f"{name}_X_Min", "LESS_THAN", parts.outputs["X"], xmin)
    x_inside = _math(tree, f"{name}_X_Inside", "SUBTRACT", 1.0, x_lower)
    x_upper = _math(tree, f"{name}_X_Max", "LESS_THAN", parts.outputs["X"], xmax)
    z_lower = _math(tree, f"{name}_Z_Min", "LESS_THAN", parts.outputs["Y"], zmin)
    z_inside = _math(tree, f"{name}_Z_Inside", "SUBTRACT", 1.0, z_lower)
    z_upper = _math(tree, f"{name}_Z_Max", "LESS_THAN", parts.outputs["Y"], zmax)
    x_mask = _math(tree, f"{name}_X_Mask", "MULTIPLY", x_inside, x_upper)
    z_mask = _math(tree, f"{name}_Z_Mask", "MULTIPLY", z_inside, z_upper)
    return _math(tree, f"{name}_Mask", "MULTIPLY", x_mask, z_mask)


def _flow(
    tree: bpy.types.NodeTree, spec: WaterMaterialRow, flow: dict[str, object],
    global_config: dict[str, object], world: bpy.types.NodeSocket,
    images: dict[str, bpy.types.Image],
) -> tuple[bpy.types.NodeSocket, Scalar]:
    direction = _record(flow.get("FlowDirectionVector2"), "Flow.FlowDirectionVector2")
    config_dir = _combine(
        tree, "Config_Direction",
        _number(direction.get("X"), "FlowDirectionVector2.X") * 2.0 - 1.0,
        1.0 - _number(direction.get("Y"), "FlowDirectionVector2.Y") * 2.0,
        0.0,
    )
    if not spec.flow.use_flow_map:
        return config_dir, 1.0
    if spec.flow_map is None:
        raise ValueError(f"WATER_WAVE_FLOW_MAP_REQUIRED entity={spec.entity_id}")
    center = _record(global_config.get("SceneCenterOffset"), "Global.SceneCenterOffset")
    origin = (_number(center.get("X"), "SceneCenterOffset.X"),
              _number(center.get("Y"), "SceneCenterOffset.Y"), 0.0)
    map_size = _integer(global_config.get("MapSize"), "Global.MapSize")
    local = _vector(tree, "Global_Local_XZ", "SUBTRACT", world, origin)
    global_uv = _vector(tree, "Global_UV", "ADD", _scale(tree, "Global_UV_Scale", local, 1.0 / map_size),
                        (0.5, 0.5, 0.0))
    global_tex = _texture(tree, "Global_Flow_Map", images[spec.flow_map.file], global_uv)
    color: bpy.types.NodeSocket = global_tex.outputs["Color"]
    alpha: Scalar = global_tex.outputs["Alpha"]
    sector_size = _integer(global_config.get("SectorSize"), "Global.SectorSize")
    for index, sector in enumerate(spec.sectors):
        xmin = origin[0] + sector.sector_x * sector_size
        zmin = origin[1] + sector.sector_z * sector_size
        name = f"Sector_{index}_{sector.sector_x}_{sector.sector_z}"
        mask = _sector_mask(tree, name, world, xmin, xmin + sector_size, zmin, zmin + sector_size)
        local_uv = _vector(tree, f"{name}_Local", "SUBTRACT", world, (xmin, zmin, 0.0))
        sector_uv = _scale(tree, f"{name}_UV", local_uv, 1.0 / sector_size)
        texture = _texture(tree, f"{name}_Flow", images[sector.texture.file], sector_uv)
        color = _mix_color(tree, f"{name}_Color", mask, color, texture.outputs["Color"])
        alpha = _mix_float(tree, f"{name}_Alpha", mask, alpha, texture.outputs["Alpha"])
    channels = _node(tree, "ShaderNodeSeparateColor", "Flow_RG")
    channels.mode = "RGB"
    tree.links.new(color, channels.inputs["Color"])
    x = _math(tree, "Flow_Signed_X", "MULTIPLY_ADD", channels.outputs["Red"], 2.0)
    x.node.inputs[2].default_value = -1.0
    y = _math(tree, "Flow_Signed_Z", "MULTIPLY_ADD", channels.outputs["Green"], -2.0)
    y.node.inputs[2].default_value = 1.0
    return _combine(tree, "Flow_World_XZ", x, y, 0.0), alpha


def _phase_weight(tree: bpy.types.NodeTree, name: str, phase: bpy.types.NodeSocket) -> bpy.types.NodeSocket:
    doubled = _math(tree, f"{name}_Twice", "MULTIPLY", phase, 2.0)
    delta = _math(tree, f"{name}_Delta", "SUBTRACT", 1.0, doubled)
    absolute = _math(tree, f"{name}_Abs", "ABSOLUTE", delta, 0.0)
    return _math(tree, f"{name}_Triangle", "SUBTRACT", 1.0, absolute)


def _wave_sample_single(
    tree: bpy.types.NodeTree, name: str, image: bpy.types.Image,
    world: bpy.types.NodeSocket, direction: bpy.types.NodeSocket,
    source_time: bpy.types.NodeSocket, cycle: float, flow_speed: float,
    tilling: float, phase_shift: float,
) -> bpy.types.NodeSocket:
    progress = _math(tree, f"{name}_Progress", "MULTIPLY", source_time, cycle)
    shifted = _math(tree, f"{name}_Shifted", "ADD", progress, phase_shift)
    offset = _scale(tree, f"{name}_Offset", direction,
                    _math(tree, f"{name}_Travel", "MULTIPLY", shifted, flow_speed))
    uv = _scale(tree, f"{name}_UV", _vector(tree, f"{name}_WorldMinusFlow", "SUBTRACT", world, offset), tilling)
    return _texture(tree, f"{name}_Sample", image, uv).outputs["Color"]


def _wave_sample_dual(
    tree: bpy.types.NodeTree, name: str, image: bpy.types.Image,
    world: bpy.types.NodeSocket, direction: bpy.types.NodeSocket,
    source_time: bpy.types.NodeSocket, cycle: float, flow_speed: float,
    tilling: float, phase_shift: float,
) -> bpy.types.NodeSocket:
    progress = _math(tree, f"{name}_Progress", "MULTIPLY", source_time, cycle)
    shifted = _math(tree, f"{name}_Shifted", "ADD", progress, phase_shift)
    samples: list[bpy.types.NodeSocket] = []
    weights: list[bpy.types.NodeSocket] = []
    for index in range(2):
        shifted_phase = _math(tree, f"{name}_{index}_Shift", "ADD", shifted, index * 0.5)
        phase = _math(tree, f"{name}_{index}_Phase", "FRACT", shifted_phase, 0.0)
        offset = _scale(tree, f"{name}_{index}_Offset", direction,
                        _math(tree, f"{name}_{index}_Travel", "MULTIPLY", phase, flow_speed))
        flowed = _vector(tree, f"{name}_{index}_Flowed", "SUBTRACT", world, offset)
        tiled = _scale(tree, f"{name}_{index}_Tiled", flowed, tilling)
        completed = _math(tree, f"{name}_{index}_Whole", "SUBTRACT", shifted, phase)
        drift = _math(tree, f"{name}_{index}_Drift", "MULTIPLY", completed, cycle)
        drift_with_half = _math(tree, f"{name}_{index}_Drift_Half", "ADD", drift, index * 0.5)
        uv = _vector(tree, f"{name}_{index}_UV", "ADD", tiled,
                     _combine(tree, f"{name}_{index}_UV_Offset", drift_with_half, drift_with_half, 0.0))
        samples.append(_texture(tree, f"{name}_{index}_Sample", image, uv).outputs["Color"])
        weights.append(_phase_weight(tree, f"{name}_{index}", phase))
    return _mix_color(tree, f"{name}_Dual_Phase", weights[1], samples[0], samples[1])


def _decode_rg(
    tree: bpy.types.NodeTree, name: str, color: bpy.types.NodeSocket,
    strength: Scalar,
) -> bpy.types.NodeSocket:
    channels = _node(tree, "ShaderNodeSeparateColor", f"{name}_RG")
    channels.mode = "RGB"
    tree.links.new(color, channels.inputs["Color"])
    x = _math(tree, f"{name}_X", "MULTIPLY_ADD", channels.outputs["Red"], 2.0)
    x.node.inputs[2].default_value = -1.0
    y = _math(tree, f"{name}_Y", "MULTIPLY_ADD", channels.outputs["Green"], 2.0)
    y.node.inputs[2].default_value = -1.0
    x2 = _math(tree, f"{name}_X2", "MULTIPLY", x, x)
    y2 = _math(tree, f"{name}_Y2", "MULTIPLY", y, y)
    length2 = _math(tree, f"{name}_XY2", "MINIMUM", _math(tree, f"{name}_Sum2", "ADD", x2, y2), 1.0)
    z2 = _math(tree, f"{name}_Z2", "MAXIMUM", _math(tree, f"{name}_OneMinusXY2", "SUBTRACT", 1.0, length2), 0.0)
    z = _math(tree, f"{name}_Z", "SQRT", z2, 0.0)
    scaled_x = _math(tree, f"{name}_StrengthX", "MULTIPLY", x, strength)
    scaled_y = _math(tree, f"{name}_StrengthY", "MULTIPLY", y, strength)
    return _combine(tree, f"{name}_Normal", scaled_x, scaled_y, z)


def _rnm(
    tree: bpy.types.NodeTree, small: bpy.types.NodeSocket, large: bpy.types.NodeSocket,
) -> bpy.types.NodeSocket:
    t = _vector(tree, "RNM_T", "ADD", small, (0.0, 0.0, 1.0))
    u = _vector(tree, "RNM_U", "MULTIPLY", large, (-1.0, -1.0, 1.0))
    dot = _node(tree, "ShaderNodeVectorMath", "RNM_Dot")
    dot.operation = "DOT_PRODUCT"
    tree.links.new(t, dot.inputs[0])
    tree.links.new(u, dot.inputs[1])
    components = _components(tree, "RNM_T_XYZ", t)
    safe_z = _math(tree, "RNM_Safe_Z", "MAXIMUM", components.outputs["Z"], 1.0e-5)
    factor = _math(tree, "RNM_Factor", "DIVIDE", dot.outputs["Value"], safe_z)
    corrected = _vector(tree, "RNM_Corrected", "SUBTRACT", _scale(tree, "RNM_Scaled_T", t, factor), u)
    node = _node(tree, "ShaderNodeVectorMath", "RNM_Normalized")
    node.operation = "NORMALIZE"
    tree.links.new(corrected, node.inputs[0])
    return node.outputs[0]


def _world_normal(tree: bpy.types.NodeTree, tangent_normal: bpy.types.NodeSocket) -> bpy.types.NodeSocket:
    parts = _components(tree, "Tangent_Normal_XYZ", tangent_normal)
    geometry = _node(tree, "ShaderNodeNewGeometry", "Surface_Normal")
    normal = geometry.outputs["Normal"]
    bitangent = _vector(tree, "World_Bitangent", "CROSS_PRODUCT", (1.0, 0.0, 0.0), normal)
    x = _scale(tree, "World_Tangent_X", (1.0, 0.0, 0.0), parts.outputs["X"])
    y = _scale(tree, "World_Bitangent_Y", bitangent, parts.outputs["Y"])
    z = _scale(tree, "World_Surface_Z", normal, parts.outputs["Z"])
    combined = _vector(tree, "World_Normal_Sum", "ADD", _vector(tree, "World_XY", "ADD", x, y), z)
    result = _node(tree, "ShaderNodeVectorMath", "World_Normal")
    result.operation = "NORMALIZE"
    tree.links.new(combined, result.inputs[0])
    return result.outputs[0]


def build_water_normal(material: bpy.types.Material, spec: WaterMaterialRow) -> bpy.types.NodeSocket:
    """Append the native-inspired RG dual-wave graph and return a world normal.

    GPU source time and view fading remain unverified. Native sector atlas
    copying uses an unflipped full source rect in a 3x3 grid. This graph
    samples that source directly in its native grid cell and uses the global
    flow map outside the cell; camera-relative atlas edge fading is omitted.
    """
    if not isinstance(material, bpy.types.Material) or not material.use_nodes or material.node_tree is None:
        raise TypeError("WATER_WAVE_MATERIAL_NODES_REQUIRED")
    flow, global_config = _source(spec)
    tree = material.node_tree
    if any(node.name.startswith("EF_Wave_") for node in tree.nodes):
        raise ValueError(f"WATER_WAVE_ALREADY_BUILT material={material.name}")
    files = {spec.small_wave_normal.file, spec.large_wave_normal.file}
    if spec.flow.use_flow_map:
        if spec.flow_map is None:
            raise ValueError(f"WATER_WAVE_FLOW_MAP_REQUIRED entity={spec.entity_id}")
        files.add(spec.flow_map.file)
        files.update(sector.texture.file for sector in spec.sectors)
    for file in files:
        if not Path(file).resolve().is_file():
            raise FileNotFoundError(f"WATER_WAVE_TEXTURE_MISSING path={file}")
    images = {file: _image(file) for file in sorted(files)}
    start = len(tree.nodes)
    world = _world_xz(tree)
    _mark_new_nodes(tree, start, "01 World Coordinates", "Blender world to Unity XZ")
    start = len(tree.nodes)
    source_time = _time(tree)
    _mark_new_nodes(tree, start, "02 Time", "Editable Blender time; GPU scale unknown")
    start = len(tree.nodes)
    direction, flow_alpha = _flow(tree, spec, flow, global_config, world, images)
    flow_note = ("Unity flow and verified sector bindings" if spec.flow.use_flow_map
                 else "Unity constant direction; no sector map")
    _mark_new_nodes(tree, start, "03 Flow and Sectors", flow_note)
    small_sample = _wave_sample_dual if spec.flow.use_flow_map else _wave_sample_single
    start = len(tree.nodes)
    small = small_sample(tree, "Small", images[spec.small_wave_normal.file], world, direction,
                         source_time, _number(flow.get("FlowFullCycle"), "Flow.FlowFullCycle"),
                         spec.flow.flow_speed, spec.flow.normal_tilling, 0.0)
    _mark_new_nodes(tree, start, "04 Small Wave", "Unity texture and flow values")
    large_speed = spec.flow.large_wave_flow_speed
    if _integer(flow.get("LargeWaveInvDir"), "Flow.LargeWaveInvDir") == 1:
        large_speed = -large_speed
    large_sample = _wave_sample_dual if spec.flow.use_flow_map else _wave_sample_single
    start = len(tree.nodes)
    large = large_sample(tree, "Large", images[spec.large_wave_normal.file], world, direction,
                         source_time, _number(flow.get("FlowFullCycleLarge"), "Flow.FlowFullCycleLarge"),
                         large_speed, spec.flow.large_wave_normal_tilling, 0.5)
    _mark_new_nodes(tree, start, "05 Large Wave", "Unity texture and flow values")
    start = len(tree.nodes)
    small_strength = _math(tree, "Small_Flow_Amplitude", "MULTIPLY", spec.flow.normal_scale, flow_alpha)
    large_strength = _math(tree, "Large_Flow_Amplitude", "MULTIPLY", spec.flow.large_wave_normal_scale, flow_alpha)
    small_normal = _decode_rg(tree, "Small", small, small_strength)
    large_normal = _decode_rg(tree, "Large", large, large_strength)
    _mark_new_nodes(tree, start, "06 RG Normal Decode", "Decode RG and apply Unity amplitude")
    start = len(tree.nodes)
    tangent = _rnm(tree, small_normal, large_normal)
    normal = _world_normal(tree, tangent)
    _mark_new_nodes(tree, start, "07 Normal Blend", "RNM blend to world normal")
    material["endfield_wave_contract"] = "WaterProxy_GBuffer_RG_DualWave/1"
    material["endfield_wave_source_time"] = "Blender frame * fps_base / fps; GPU _12._m75.w conversion unverified"
    material["endfield_wave_sector_uv"] = (
        "native grid local UV; full unflipped sector source rect verified; camera-relative 3x3 atlas edge fade omitted"
    )
    material["endfield_wave_view_modulation"] = "GPU view/distance strength modulation omitted"
    material["endfield_wave_config_path"] = spec.config_resource_path
    material["endfield_wave_global_path"] = spec.global_resource_path
    return normal
