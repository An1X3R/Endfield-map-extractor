"""Add a clearly marked, editable reference environment without changing native lights."""

from __future__ import annotations

import math
from typing import TypedDict

import bpy


REFERENCE_CONTRACT = "EndfieldReferenceEnvironment/1"
REFERENCE_GROUP_NAME = "Endfield_Reference_Environment_Nodes"
REFERENCE_COLOR = (0.22, 0.26, 0.32, 1.0)
REFERENCE_STRENGTH = 0.8


class EnvironmentAudit(TypedDict):
    mode: str
    world: str
    source_world: str
    enabled: float
    strength: float
    color_linear: list[float]
    source_background_color_linear: list[float]
    source_background_strength: float
    enable_control: str
    source_exposure: float
    exposure_unchanged: bool
    source_world_preserved: bool
    source_world_fake_user: bool
    render_engine: str


def _number(value: object, context: str) -> float:
    if type(value) not in (int, float) or not math.isfinite(float(value)):
        raise ValueError(f"Reference environment requires a finite number: field={context}, value={value!r}")
    return float(value)


def _source_background(source_world: bpy.types.World) -> tuple[tuple[float, float, float, float], float]:
    if not source_world.use_nodes or source_world.node_tree is None:
        color = (*tuple(float(component) for component in source_world.color), 1.0)
        if any(not math.isfinite(component) for component in color):
            raise ValueError(f"Source World color is invalid: world={source_world.name}, color={color}")
        return color, 1.0
    outputs = [node for node in source_world.node_tree.nodes
               if node.bl_idname == "ShaderNodeOutputWorld" and node.is_active_output]
    if len(outputs) != 1:
        raise ValueError(
            f"Cannot preserve source World background: world={source_world.name}, active_outputs={len(outputs)}")
    surface = outputs[0].inputs["Surface"]
    links = list(surface.links)
    if len(links) != 1 or links[0].from_node.bl_idname != "ShaderNodeBackground":
        raise ValueError(f"Cannot preserve non-constant source World graph: world={source_world.name}")
    background = links[0].from_node
    color_socket = background.inputs["Color"]
    strength_socket = background.inputs["Strength"]
    if color_socket.is_linked or strength_socket.is_linked:
        raise ValueError(f"Cannot preserve linked source World background inputs: world={source_world.name}")
    color = tuple(float(component) for component in color_socket.default_value)
    strength = _number(strength_socket.default_value, f"{source_world.name}.Background.Strength")
    if len(color) != 4 or any(not math.isfinite(component) for component in color):
        raise ValueError(f"Source World background color is invalid: world={source_world.name}, color={color}")
    return color, strength


def _reference_group() -> bpy.types.NodeTree:
    group = bpy.data.node_groups.get(REFERENCE_GROUP_NAME)
    if group is not None:
        if group.get("endfield_environment_contract") != REFERENCE_CONTRACT:
            raise ValueError(f"Reference environment node group name is already in use: group={REFERENCE_GROUP_NAME}")
        return group

    group = bpy.data.node_groups.new(REFERENCE_GROUP_NAME, "ShaderNodeTree")
    color = group.interface.new_socket(name="Color", in_out="INPUT", socket_type="NodeSocketColor")
    color.default_value = REFERENCE_COLOR
    strength = group.interface.new_socket(name="Strength", in_out="INPUT", socket_type="NodeSocketFloat")
    strength.default_value = REFERENCE_STRENGTH
    strength.min_value = 0.0
    enabled = group.interface.new_socket(name="Enabled", in_out="INPUT", socket_type="NodeSocketFloat")
    enabled.default_value = 1.0
    enabled.min_value = 0.0
    enabled.max_value = 1.0
    source_color = group.interface.new_socket(name="Source Color", in_out="INPUT", socket_type="NodeSocketColor")
    source_color.default_value = (0.050876, 0.050876, 0.050876, 1.0)
    source_strength = group.interface.new_socket(name="Source Strength", in_out="INPUT", socket_type="NodeSocketFloat")
    source_strength.default_value = 1.0
    source_strength.min_value = 0.0
    group.interface.new_socket(name="Environment", in_out="OUTPUT", socket_type="NodeSocketShader")

    nodes = group.nodes
    group_input = nodes.new("NodeGroupInput")
    group_input.location = (-420.0, 0.0)
    reference_background = nodes.new("ShaderNodeBackground")
    reference_background.name = "Reference Environment Fill"
    reference_background.label = "Non-native reference fill"
    reference_background.location = (-80.0, 180.0)
    source_background = nodes.new("ShaderNodeBackground")
    source_background.name = "Source Environment Fill"
    source_background.label = "Preserved original world fill"
    source_background.location = (-80.0, -40.0)
    environment_mix = nodes.new("ShaderNodeMixShader")
    environment_mix.name = "Reference Environment Enable"
    environment_mix.label = "Enabled selects reference; disabled restores source fill"
    environment_mix.location = (180.0, 80.0)
    group_output = nodes.new("NodeGroupOutput")
    group_output.location = (420.0, 80.0)
    group.links.new(group_input.outputs["Color"], reference_background.inputs["Color"])
    group.links.new(group_input.outputs["Strength"], reference_background.inputs["Strength"])
    group.links.new(group_input.outputs["Source Color"], source_background.inputs["Color"])
    group.links.new(group_input.outputs["Source Strength"], source_background.inputs["Strength"])
    group.links.new(group_input.outputs["Enabled"], environment_mix.inputs[0])
    group.links.new(source_background.outputs["Background"], environment_mix.inputs[1])
    group.links.new(reference_background.outputs["Background"], environment_mix.inputs[2])
    group.links.new(environment_mix.outputs[0], group_output.inputs["Environment"])
    group["endfield_environment_contract"] = REFERENCE_CONTRACT
    group["endfield_environment_mode"] = "reference_not_native"
    return group


def _build_world(name: str, source_world: bpy.types.World,
                 source_color: tuple[float, float, float, float], source_strength: float) -> bpy.types.World:
    world = bpy.data.worlds.get(name)
    if world is not None:
        if world.get("endfield_environment_contract") != REFERENCE_CONTRACT:
            raise ValueError(f"Reference environment world name is already in use: world={name}")
        if world.get("endfield_environment_source_world") != source_world.name:
            raise ValueError(f"Reference environment belongs to a different source World: world={name}")
        return world

    world = bpy.data.worlds.new(name)
    world.use_nodes = True
    tree = world.node_tree
    if tree is None:
        raise ValueError(f"Reference World node tree was not created: world={name}")
    tree.nodes.clear()
    reference = tree.nodes.new("ShaderNodeGroup")
    reference.name = "Editable Reference Environment"
    reference.label = "Reference environment; set Enabled to 0 to turn off"
    reference.node_tree = _reference_group()
    reference.inputs["Color"].default_value = REFERENCE_COLOR
    reference.inputs["Strength"].default_value = REFERENCE_STRENGTH
    reference.inputs["Enabled"].default_value = 1.0
    reference.inputs["Source Color"].default_value = source_color
    reference.inputs["Source Strength"].default_value = source_strength
    reference.location = (40.0, 160.0)

    source = tree.nodes.new("ShaderNodeBackground")
    source.name = "Preserved Source World Background"
    source.label = "Original world background; not a native-light reconstruction"
    source.inputs["Color"].default_value = source_color
    source.inputs["Strength"].default_value = source_strength
    source.location = (40.0, -120.0)
    light_path = tree.nodes.new("ShaderNodeLightPath")
    light_path.location = (-200.0, -280.0)
    mix = tree.nodes.new("ShaderNodeMixShader")
    mix.name = "Camera Background Preservation"
    mix.label = "Reference fill is used for non-camera rays"
    mix.location = (300.0, 80.0)
    output = tree.nodes.new("ShaderNodeOutputWorld")
    output.location = (520.0, 80.0)
    tree.links.new(reference.outputs["Environment"], mix.inputs[1])
    tree.links.new(source.outputs["Background"], mix.inputs[2])
    tree.links.new(light_path.outputs["Is Camera Ray"], mix.inputs[0])
    tree.links.new(mix.outputs[0], output.inputs["Surface"])

    world["endfield_environment_contract"] = REFERENCE_CONTRACT
    world["endfield_environment_mode"] = "reference_not_native"
    world["endfield_environment_source_world"] = source_world.name
    world["endfield_environment_source_background_color"] = list(source_color)
    world["endfield_environment_source_background_strength"] = source_strength
    world["endfield_environment_enable_control"] = "Editable Reference Environment.Enabled; zero restores source fill"
    world["endfield_environment_strength_control"] = "Editable Reference Environment.Strength"
    return world


def _audit(scene: bpy.types.Scene, world: bpy.types.World, source_world: bpy.types.World) -> EnvironmentAudit:
    if world.node_tree is None:
        raise ValueError(f"Reference environment World has no node tree: world={world.name}")
    reference = world.node_tree.nodes.get("Editable Reference Environment")
    if reference is None or reference.bl_idname != "ShaderNodeGroup" or reference.node_tree is None:
        raise ValueError(f"Reference environment node group is missing: world={world.name}")
    if reference.node_tree.get("endfield_environment_contract") != REFERENCE_CONTRACT:
        raise ValueError(f"Reference environment node group contract differs: world={world.name}")
    required_inputs = ("Color", "Enabled", "Strength", "Source Color", "Source Strength")
    if any(reference.inputs.get(name) is None for name in required_inputs):
        raise ValueError(f"Reference environment controls are incomplete: world={world.name}")
    color = tuple(float(component) for component in reference.inputs["Color"].default_value)
    source_color_raw = world.get("endfield_environment_source_background_color")
    if isinstance(source_color_raw, (str, bytes)) or source_color_raw is None:
        raise ValueError(f"Reference environment source background color is invalid: world={world.name}")
    try:
        source_color_values = tuple(source_color_raw)
    except TypeError as error:
        raise ValueError(f"Reference environment source background color is invalid: world={world.name}") from error
    if len(source_color_values) != 4:
        raise ValueError(f"Reference environment source background color is invalid: world={world.name}")
    source_color = [_number(component, f"{world.name}.source_background_color[{index}]")
                    for index, component in enumerate(source_color_values)]
    source_strength = _number(world.get("endfield_environment_source_background_strength"),
                              f"{world.name}.source_background_strength")
    enabled = _number(reference.inputs["Enabled"].default_value, f"{world.name}.Enabled")
    strength = _number(reference.inputs["Strength"].default_value, f"{world.name}.Strength")
    source_exposure = _number(world.get("endfield_environment_source_exposure"),
                              f"{world.name}.source_exposure")
    current_exposure = _number(scene.view_settings.exposure, f"{scene.name}.view_settings.exposure")
    if len(color) != 4 or any(not math.isfinite(component) for component in color):
        raise ValueError(f"Reference environment color is invalid: world={world.name}, color={color}")
    return {
        "mode": "reference_not_native",
        "world": world.name,
        "source_world": source_world.name,
        "enabled": enabled,
        "strength": strength,
        "color_linear": list(color),
        "source_background_color_linear": source_color,
        "source_background_strength": source_strength,
        "enable_control": "Editable Reference Environment.Enabled; set to 0 to restore source fill",
        "source_exposure": source_exposure,
        "exposure_unchanged": current_exposure == source_exposure,
        "source_world_preserved": bpy.data.worlds.get(source_world.name) is source_world,
        "source_world_fake_user": source_world.use_fake_user,
        "render_engine": scene.render.engine,
    }


def setup_environment(scene: bpy.types.Scene) -> EnvironmentAudit:
    """Assign an editable reference world and leave source lights and exposure untouched."""
    source_world = scene.world
    if source_world is None:
        raise ValueError(f"Cannot add a reference environment to a scene without a source World: scene={scene.name}")
    if source_world.get("endfield_environment_contract") == REFERENCE_CONTRACT:
        original_name = source_world.get("endfield_environment_source_world")
        if type(original_name) is not str or not original_name:
            raise ValueError(f"Reference environment source World identity is invalid: world={source_world.name}")
        original_world = bpy.data.worlds.get(original_name)
        if original_world is None:
            raise ValueError(
                f"Reference environment source World is missing: world={source_world.name}, source={original_name}")
        original_world.use_fake_user = True
        return _audit(scene, source_world, original_world)
    exposure_before = _number(scene.view_settings.exposure, f"{scene.name}.view_settings.exposure")
    engine_before = scene.render.engine
    source_color, source_strength = _source_background(source_world)
    world = _build_world(f"Endfield_Reference_Environment_{scene.name}", source_world, source_color, source_strength)
    if "endfield_environment_source_exposure" not in world:
        world["endfield_environment_source_exposure"] = exposure_before
    source_world.use_fake_user = True
    scene.world = world
    exposure_after = _number(scene.view_settings.exposure, f"{scene.name}.view_settings.exposure")
    if exposure_after != exposure_before or scene.render.engine != engine_before:
        raise RuntimeError(f"Reference environment changed source render settings: scene={scene.name}")
    return _audit(scene, world, source_world)
