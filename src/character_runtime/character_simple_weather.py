"""Small stock-node weather effects for simplified character materials."""
from __future__ import annotations

from dataclasses import dataclass
import json

import bpy


_EFFECT_INPUTS: dict[str, tuple[str, str, str]] = {
    "wet": ("User Effect Z", "潮湿强度", "潮湿"),
    "snow": ("User Effect W", "积雪强度", "积雪"),
    "rain": ("User Effect X", "降雨强度", "降雨"),
}
_EFFECT_ORDER: tuple[str, ...] = ("wet", "snow", "rain")
_CHANNELS: tuple[tuple[str, str, str], ...] = (
    ("Base Color", "基础颜色", "基础颜色输出"),
    ("Roughness", "表面粗糙度", "表面粗糙度输出"),
    ("Normal", "表面法线", "表面法线输出"),
    ("Alpha", "不透明度", "不透明度输出"),
)
_CHANNEL_NAMES: tuple[str, ...] = tuple(surface_name for surface_name, _, _ in _CHANNELS)


@dataclass(frozen=True)
class SimpleWeatherReport:
    """Describe the attached weather group and its editable material controls."""

    owner_name: str
    surface_node_name: str
    group_node_name: str
    group_name: str
    effects: tuple[str, ...]
    control_labels: tuple[str, ...]
    reused: bool


def _validate_effects(effects: tuple[str, ...]) -> tuple[str, ...]:
    if not isinstance(effects, tuple):
        raise TypeError("Simple weather effects must be a tuple containing wet, snow, and/or rain")
    if any(not isinstance(effect, str) for effect in effects):
        raise TypeError("Every simple weather effect must be a string")
    if len(set(effects)) != len(effects):
        raise ValueError(f"Simple weather effects must be unique: {effects!r}")
    unsupported = tuple(effect for effect in effects if effect not in _EFFECT_INPUTS)
    if unsupported:
        raise ValueError(f"Unsupported simple weather effects {unsupported!r}; allowed values are {_EFFECT_ORDER!r}")
    return tuple(effect for effect in _EFFECT_ORDER if effect in effects)


def _new_node(
    tree: bpy.types.NodeTree,
    kind: str,
    name: str,
    label: str,
    location: tuple[float, float],
) -> bpy.types.Node:
    result = tree.nodes.new(kind)
    result.name = name
    result.label = label
    result.location = location
    return result


def _add_socket(
    tree: bpy.types.ShaderNodeTree,
    name: str,
    direction: str,
    socket_type: str,
    default_value: float | tuple[float, float, float, float] | tuple[float, float, float],
    description: str,
    minimum: float | None,
    maximum: float | None,
) -> bpy.types.NodeTreeInterfaceSocket:
    result = tree.interface.new_socket(name=name, in_out=direction, socket_type=socket_type)
    result.default_value = default_value
    result.description = description
    if minimum is not None:
        result.min_value = minimum
    if maximum is not None:
        result.max_value = maximum
    return result


def _math(
    tree: bpy.types.NodeTree,
    operation: str,
    first: bpy.types.NodeSocket | float,
    second: bpy.types.NodeSocket | float,
    label: str,
    location: tuple[float, float],
) -> bpy.types.NodeSocket:
    result = _new_node(tree, "ShaderNodeMath", label, label, location)
    result.operation = operation
    for index, value in enumerate((first, second)):
        if isinstance(value, bpy.types.NodeSocket):
            tree.links.new(value, result.inputs[index])
        else:
            result.inputs[index].default_value = value
    return result.outputs[0]


def _unit_interval(
    tree: bpy.types.NodeTree,
    value: bpy.types.NodeSocket,
    label: str,
    location: tuple[float, float],
) -> bpy.types.NodeSocket:
    lower = _math(tree, "MAXIMUM", value, 0.0, label + " floor", location)
    return _math(tree, "MINIMUM", lower, 1.0, label + " clamp", (location[0] + 180.0, location[1]))


def _float_mix(
    tree: bpy.types.NodeTree,
    factor: bpy.types.NodeSocket,
    first: bpy.types.NodeSocket,
    second: bpy.types.NodeSocket,
    label: str,
    location: tuple[float, float],
) -> bpy.types.NodeSocket:
    inverse = _math(tree, "SUBTRACT", 1.0, factor, label + " inverse", location)
    left = _math(tree, "MULTIPLY", first, inverse, label + " first", (location[0] + 180.0, location[1]))
    right = _math(tree, "MULTIPLY", second, factor, label + " second", (location[0] + 180.0, location[1] - 150.0))
    return _math(tree, "ADD", left, right, label, (location[0] + 360.0, location[1]))


def _color_mix(
    tree: bpy.types.NodeTree,
    factor: bpy.types.NodeSocket,
    first: bpy.types.NodeSocket,
    second: bpy.types.NodeSocket,
    label: str,
    location: tuple[float, float],
) -> bpy.types.NodeSocket:
    result = _new_node(tree, "ShaderNodeMixRGB", label, label, location)
    result.blend_type = "MIX"
    tree.links.new(factor, result.inputs[0])
    tree.links.new(first, result.inputs[1])
    tree.links.new(second, result.inputs[2])
    return result.outputs[0]


def _smooth_range(
    tree: bpy.types.NodeTree,
    value: bpy.types.NodeSocket,
    low: bpy.types.NodeSocket | float,
    high: bpy.types.NodeSocket | float,
    label: str,
    location: tuple[float, float],
) -> bpy.types.NodeSocket:
    result = _new_node(tree, "ShaderNodeMapRange", label, label, location)
    result.interpolation_type = "SMOOTHSTEP"
    result.clamp = True
    for socket_name, source in (("Value", value), ("From Min", low), ("From Max", high)):
        if isinstance(source, bpy.types.NodeSocket):
            tree.links.new(source, result.inputs[socket_name])
        else:
            result.inputs[socket_name].default_value = source
    result.inputs["To Min"].default_value = 0.0
    result.inputs["To Max"].default_value = 1.0
    return result.outputs["Result"]


def _configure_group_interface(tree: bpy.types.ShaderNodeTree, effects: tuple[str, ...]) -> None:
    _add_socket(tree, "基础颜色", "INPUT", "NodeSocketColor", (1.0, 1.0, 1.0, 1.0), "上游简化材质的基础颜色", None, None)
    _add_socket(tree, "表面粗糙度", "INPUT", "NodeSocketFloat", 0.5, "上游简化材质的粗糙度", 0.0, 1.0)
    _add_socket(tree, "表面法线", "INPUT", "NodeSocketVector", (0.0, 0.0, 1.0), "上游简化材质的法线", None, None)
    _add_socket(tree, "不透明度", "INPUT", "NodeSocketFloat", 1.0, "上游简化材质的 Alpha", 0.0, 1.0)

    if "wet" in effects:
        _add_socket(tree, "潮湿暗化比例", "INPUT", "NodeSocketFloat", 0.22, "湿润时基础颜色的最大暗化比例", 0.0, 0.65)
        _add_socket(tree, "湿面粗糙度", "INPUT", "NodeSocketFloat", 0.08, "完全潮湿时的目标粗糙度", 0.0, 1.0)
    if "snow" in effects:
        _add_socket(tree, "积雪颜色", "INPUT", "NodeSocketColor", (0.82, 0.89, 0.96, 1.0), "程序化积雪覆盖颜色", None, None)
        _add_socket(tree, "雪面粗糙度", "INPUT", "NodeSocketFloat", 0.88, "积雪区域的粗糙度", 0.0, 1.0)
        _add_socket(tree, "雪面朝上起点", "INPUT", "NodeSocketFloat", 0.28, "世界法线 Z 达到此值后开始积雪", -1.0, 1.0)
        _add_socket(tree, "雪面朝上终点", "INPUT", "NodeSocketFloat", 0.82, "世界法线 Z 达到此值后积雪全覆盖", -1.0, 1.0)
        _add_socket(tree, "雪噪声尺度", "INPUT", "NodeSocketFloat", 7.0, "雪面破碎噪声的尺度", 0.1, 100.0)
        _add_socket(tree, "雪噪声阈值低", "INPUT", "NodeSocketFloat", 0.34, "积雪噪声过渡下界", 0.0, 1.0)
        _add_socket(tree, "雪噪声阈值高", "INPUT", "NodeSocketFloat", 0.72, "积雪噪声过渡上界", 0.0, 1.0)
    if "rain" in effects:
        _add_socket(tree, "雨纹凹凸强度", "INPUT", "NodeSocketFloat", 0.12, "程序化雨纹的凹凸强度", 0.0, 0.18)
        _add_socket(tree, "雨纹距离", "INPUT", "NodeSocketFloat", 0.012, "程序化雨纹的凹凸距离", 0.0, 0.05)
        _add_socket(tree, "雨纹尺度", "INPUT", "NodeSocketFloat", 22.0, "程序化雨纹的噪声尺度", 0.1, 100.0)

    _add_socket(tree, "基础颜色输出", "OUTPUT", "NodeSocketColor", (1.0, 1.0, 1.0, 1.0), "叠加所选天气后的颜色", None, None)
    _add_socket(tree, "表面粗糙度输出", "OUTPUT", "NodeSocketFloat", 0.5, "叠加所选天气后的粗糙度", 0.0, 1.0)
    _add_socket(tree, "表面法线输出", "OUTPUT", "NodeSocketVector", (0.0, 0.0, 1.0), "叠加所选天气后的法线", None, None)
    _add_socket(tree, "不透明度输出", "OUTPUT", "NodeSocketFloat", 1.0, "保持上游材质 Alpha", 0.0, 1.0)


def _build_group(
    effects: tuple[str, ...],
    owner_name: str,
    surface_name: str,
    scene: bpy.types.Scene | None,
) -> bpy.types.ShaderNodeTree:
    tree = bpy.data.node_groups.new("EF Simple Weather - " + owner_name, "ShaderNodeTree")
    tree.use_fake_user = True
    tree["endfield_simple_weather"] = True
    tree["endfield_simple_weather_effects"] = json.dumps(effects, ensure_ascii=False, separators=(",", ":"))
    tree["endfield_simple_weather_surface"] = surface_name
    tree["endfield_simple_weather_scope"] = "Stock Blender nodes; artist-controlled approximation; no native game weather claim"
    _configure_group_interface(tree, effects)

    entry = _new_node(tree, "NodeGroupInput", "Simple Weather Inputs", "简化材质与所选天气控制", (-1050.0, 100.0))
    output = _new_node(tree, "NodeGroupOutput", "Simple Weather Outputs", "天气叠加输出", (1120.0, 100.0))
    effect_values: dict[str, bpy.types.NodeSocket] = {}
    for index, effect in enumerate(effects):
        value_node = _effect_value_node(tree, effect, (-1280.0, 520.0 - index * 210.0))
        effect_values[effect] = value_node.outputs[0]
    base_color = entry.outputs["基础颜色"]
    roughness = entry.outputs["表面粗糙度"]
    normal = entry.outputs["表面法线"]

    if "wet" in effects:
        wet = _unit_interval(tree, effect_values["wet"], "Clamp wet amount", (-830.0, 760.0))
        darken = _math(tree, "MULTIPLY", wet, entry.outputs["潮湿暗化比例"], "Wet darkening amount", (-470.0, 760.0))
        dark_color = _new_node(tree, "ShaderNodeMixRGB", "Wet darkening", "潮湿颜色暗化", (-220.0, 760.0))
        dark_color.blend_type = "MULTIPLY"
        tree.links.new(darken, dark_color.inputs[0])
        tree.links.new(base_color, dark_color.inputs[1])
        dark_color.inputs[2].default_value = (0.72, 0.72, 0.72, 1.0)
        base_color = dark_color.outputs[0]
        roughness = _float_mix(tree, wet, roughness, entry.outputs["湿面粗糙度"], "潮湿粗糙度", (-470.0, 390.0))

    if "snow" in effects:
        geometry = _new_node(tree, "ShaderNodeNewGeometry", "Snow Surface Normal", "积雪朝上法线遮罩", (-830.0, -80.0))
        separate = _new_node(tree, "ShaderNodeSeparateXYZ", "Snow Normal Z", "积雪世界法线 Z", (-600.0, -80.0))
        tree.links.new(geometry.outputs["Normal"], separate.inputs["Vector"])
        facing = _smooth_range(
            tree,
            separate.outputs["Z"],
            entry.outputs["雪面朝上起点"],
            entry.outputs["雪面朝上终点"],
            "Snow upward-facing coverage",
            (-360.0, -80.0),
        )
        coordinates = _new_node(tree, "ShaderNodeTexCoord", "Snow Coordinates", "积雪程序噪声坐标", (-830.0, -370.0))
        noise = _new_node(tree, "ShaderNodeTexNoise", "Snow Noise", "积雪破碎噪声", (-600.0, -370.0))
        tree.links.new(coordinates.outputs["Generated"], noise.inputs["Vector"])
        tree.links.new(entry.outputs["雪噪声尺度"], noise.inputs["Scale"])
        breakup = _smooth_range(
            tree,
            noise.outputs["Fac"],
            entry.outputs["雪噪声阈值低"],
            entry.outputs["雪噪声阈值高"],
            "Snow noise breakup",
            (-360.0, -370.0),
        )
        amount = _unit_interval(tree, effect_values["snow"], "Clamp snow amount", (-830.0, -650.0))
        snow_cover = _math(tree, "MULTIPLY", facing, breakup, "Snow facing and noise", (0.0, -100.0))
        snow_cover = _math(tree, "MULTIPLY", snow_cover, amount, "Snow coverage amount", (200.0, -100.0))
        base_color = _color_mix(tree, snow_cover, base_color, entry.outputs["积雪颜色"], "积雪颜色叠加", (470.0, -100.0))
        roughness = _float_mix(tree, snow_cover, roughness, entry.outputs["雪面粗糙度"], "积雪粗糙度", (470.0, -350.0))

    if "rain" in effects:
        if scene is None:
            raise ValueError("Rain weather needs an active Blender scene to normalize animation time by its frame rate")
        speed_node, time_node = _add_rain_time_controls(tree, scene)
        coordinates = _new_node(tree, "ShaderNodeTexCoord", "Rain Coordinates", "雨纹程序坐标", (-830.0, -850.0))
        noise = _new_node(tree, "ShaderNodeTexNoise", "Rain Noise 4D", "缓慢流动的程序化雨纹", (-600.0, -850.0))
        noise.noise_dimensions = "4D"
        noise.inputs["Detail"].default_value = 2.0
        noise.inputs["Roughness"].default_value = 0.45
        tree.links.new(coordinates.outputs["Generated"], noise.inputs["Vector"])
        rain_time = _math(tree, "MULTIPLY", time_node.outputs[0], speed_node.outputs[0], "Frame-rate normalized rain time", (-360.0, -700.0))
        tree.links.new(rain_time, noise.inputs["W"])
        tree.links.new(entry.outputs["雨纹尺度"], noise.inputs["Scale"])
        amount = _unit_interval(tree, effect_values["rain"], "Clamp rain amount", (-830.0, -1120.0))
        strength = _math(tree, "MULTIPLY", amount, entry.outputs["雨纹凹凸强度"], "Restrained rain bump strength", (-360.0, -1100.0))
        bump = _new_node(tree, "ShaderNodeBump", "Rain Bump", "克制的雨纹法线凹凸", (0.0, -850.0))
        tree.links.new(strength, bump.inputs["Strength"])
        tree.links.new(entry.outputs["雨纹距离"], bump.inputs["Distance"])
        tree.links.new(noise.outputs["Fac"], bump.inputs["Height"])
        tree.links.new(normal, bump.inputs["Normal"])
        normal = bump.outputs["Normal"]

    for name, socket in (
        ("基础颜色输出", base_color),
        ("表面粗糙度输出", roughness),
        ("表面法线输出", normal),
        ("不透明度输出", entry.outputs["不透明度"]),
    ):
        tree.links.new(socket, output.inputs[name])
    return tree


def _effect_value_node(
    tree: bpy.types.NodeTree,
    effect: str,
    location: tuple[float, float],
) -> bpy.types.Node:
    label, input_name, control_name = _EFFECT_INPUTS[effect]
    result = _new_node(tree, "ShaderNodeValue", "EF " + label, label, location)
    result.outputs[0].default_value = 0.0
    result["endfield_simple_weather_control_zh"] = control_name
    result["endfield_simple_weather_effect"] = effect
    result["endfield_simple_weather_input"] = input_name
    return result


def _add_rain_time_controls(
    tree: bpy.types.NodeTree,
    scene: bpy.types.Scene,
) -> tuple[bpy.types.Node, bpy.types.Node]:
    speed = _new_node(tree, "ShaderNodeValue", "EF Simple Rain Speed", "雨纹速度", (-240.0, -600.0))
    speed.outputs[0].default_value = 0.35
    speed["endfield_simple_weather_control_zh"] = "雨纹速度；按场景帧率换算为秒"
    time = _new_node(tree, "ShaderNodeValue", "EF Simple Rain Time", "雨纹时间（自动）", (-40.0, -600.0))
    time["endfield_simple_weather_control_zh"] = "按场景帧率归一化；无需帧处理器"
    curve = time.outputs[0].driver_add("default_value")
    driver = curve.driver
    driver.type = "SCRIPTED"
    driver.expression = "(frame - frame_start) / fps * fps_base"
    for variable_name, target_id, data_path in (
        ("fps", scene, "render.fps"),
        ("fps_base", scene, "render.fps_base"),
        ("frame_start", scene, "frame_start"),
    ):
        variable = driver.variables.new()
        variable.name = variable_name
        variable.type = "SINGLE_PROP"
        target = variable.targets[0]
        target.id_type = "SCENE"
        target.id = target_id
        target.data_path = data_path
    time["endfield_simple_weather_driver"] = driver.expression
    return speed, time


def _existing_group_node(tree: bpy.types.NodeTree, surface: bpy.types.Node) -> bpy.types.Node | None:
    existing = tuple(
        node
        for node in tree.nodes
        if node.type == "GROUP"
        and node.node_tree is not None
        and node.node_tree.get("endfield_simple_weather") is True
        and node.node_tree.get("endfield_simple_weather_surface") == surface.name
    )
    if len(existing) > 1:
        raise ValueError(f"Node tree {tree.name!r} has multiple EF simple weather groups for surface {surface.name!r}")
    return existing[0] if existing else None


def _report(
    owner_name: str,
    surface: bpy.types.Node,
    group_node: bpy.types.Node,
    effects: tuple[str, ...],
    control_labels: tuple[str, ...],
    reused: bool,
) -> SimpleWeatherReport:
    if group_node.node_tree is None:
        raise ValueError(f"Node tree {owner_name!r} has an EF simple weather node without a node group")
    return SimpleWeatherReport(
        owner_name=owner_name,
        surface_node_name=surface.name,
        group_node_name=group_node.name,
        group_name=group_node.node_tree.name,
        effects=effects,
        control_labels=control_labels,
        reused=reused,
    )


def _attach_simple_weather_surface(
    owner_name: str,
    tree: bpy.types.NodeTree,
    surface: bpy.types.Node,
    effects: tuple[str, ...],
) -> SimpleWeatherReport:
    selected = _validate_effects(effects)
    if not isinstance(tree, bpy.types.NodeTree):
        raise TypeError("Simple weather requires a Blender node tree")
    if not isinstance(surface, bpy.types.Node) or surface.id_data != tree:
        raise ValueError(f"Weather surface must be a node owned by tree {tree.name!r}")
    if surface.type != "BSDF_PRINCIPLED":
        raise ValueError(f"Simple weather needs a Principled BSDF surface; node={surface.name!r}, type={surface.type!r}")
    surface_sockets = tuple(surface.inputs.get(name) for name in _CHANNEL_NAMES)
    missing = tuple(name for name, socket in zip(_CHANNEL_NAMES, surface_sockets, strict=True) if socket is None)
    if missing:
        raise ValueError(f"Principled surface {surface.name!r} is missing required inputs: {missing!r}")

    existing = _existing_group_node(tree, surface)
    if existing is not None:
        if existing.node_tree is None:
            raise ValueError(f"Existing simple weather node {existing.name!r} has no node group")
        recorded = json.loads(existing.node_tree.get("endfield_simple_weather_effects", "[]"))
        if not isinstance(recorded, list) or any(not isinstance(effect, str) for effect in recorded):
            raise ValueError(f"Existing simple weather group {existing.node_tree.name!r} has invalid effect metadata")
        existing_effects = tuple(recorded)
        if existing_effects != selected:
            raise ValueError(
                f"Surface {surface.name!r} already has simple weather effects {existing_effects!r}; requested effects are {selected!r}"
            )
        labels = tuple(_EFFECT_INPUTS[effect][0] for effect in selected)
        if "rain" in selected:
            labels += ("雨纹速度", "雨纹时间（自动）")
        return _report(owner_name, surface, existing, selected, labels, True)

    source_links: dict[str, bpy.types.NodeSocket | None] = {}
    for name, target in zip(_CHANNEL_NAMES, surface_sockets, strict=True):
        matches = tuple(link for link in tree.links if link.to_node == surface and link.to_socket == target)
        if len(matches) > 1:
            raise ValueError(f"Principled input {name!r} has multiple links on surface {surface.name!r}")
        source_links[name] = matches[0].from_socket if matches else None

    scene = bpy.context.scene
    if "rain" in selected and scene is None:
        raise ValueError("Rain weather needs an active Blender scene to normalize animation time by its frame rate")

    weather_group = _build_group(selected, owner_name, surface.name, scene)
    group_node = _new_node(
        tree,
        "ShaderNodeGroup",
        "EF Simple Weather " + surface.name,
        "EF 简化天气",
        (surface.location.x - 360.0, surface.location.y - 220.0),
    )
    group_node.node_tree = weather_group

    for (name, input_name, _), target in zip(_CHANNELS, surface_sockets, strict=True):
        original = source_links[name]
        group_input = group_node.inputs[input_name]
        if original is not None:
            tree.links.new(original, group_input)
        else:
            group_input.default_value = target.default_value
    for link in tuple(tree.links):
        if link.to_node == surface and link.to_socket in surface_sockets:
            tree.links.remove(link)
    for (_, _, output_name), target in zip(_CHANNELS, surface_sockets, strict=True):
        tree.links.new(group_node.outputs[output_name], target)

    control_labels = tuple(_EFFECT_INPUTS[effect][0] for effect in selected)
    if "rain" in selected:
        control_labels += ("雨纹速度", "雨纹时间（自动）")
    return _report(owner_name, surface, group_node, selected, control_labels, False)


def attach_simple_weather_surface(
    tree: bpy.types.NodeTree,
    surface: bpy.types.Node,
    effects: tuple[str, ...],
) -> SimpleWeatherReport:
    """Attach weather to a Principled node inside an existing shader graph."""
    if not isinstance(tree, bpy.types.NodeTree):
        raise TypeError("Simple weather requires a Blender node tree")
    return _attach_simple_weather_surface(tree.name, tree, surface, effects)


def attach_simple_weather(material: bpy.types.Material, effects: tuple[str, ...]) -> SimpleWeatherReport:
    """Wrap the simplified material surface named EF Simple Surface."""
    selected = _validate_effects(effects)
    if not isinstance(material, bpy.types.Material):
        raise TypeError("Simple weather requires a bpy.types.Material")
    if not material.use_nodes or material.node_tree is None:
        raise ValueError(f"Material {material.name!r} must use nodes and contain a Principled surface named 'EF Simple Surface'")
    surfaces = tuple(
        node
        for node in material.node_tree.nodes
        if node.type == "BSDF_PRINCIPLED" and node.name == "EF Simple Surface"
    )
    if len(surfaces) != 1:
        raise ValueError(f"Material {material.name!r} needs exactly one Principled node named 'EF Simple Surface'; found {len(surfaces)}")
    return _attach_simple_weather_surface(material.name, material.node_tree, surfaces[0], selected)
