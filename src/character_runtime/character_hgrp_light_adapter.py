"""Bind editable HGRP preview state to a Blender Sun without custom handlers.

This is a host-light adapter, not recovered Unity shadow-atlas or light-loop data.
Direction is toward the emitter; intensity explicitly uses Blender Sun strength.
"""
from __future__ import annotations
import bpy
from mathutils import Vector


def labelled_control(label: str, node_type: str) -> bpy.types.Node:
    values = [item for tree in bpy.data.node_groups for item in tree.nodes if item.type == node_type and item.label == label]
    if len(values) != 1:
        raise ValueError(f'Expected exactly one shared light control: label={label}, count={len(values)}')
    return values[0]


def transform_variable(driver: bpy.types.Driver, light: bpy.types.Object, axis: str) -> None:
    variable = driver.variables.new(); variable.name = axis.lower(); variable.type = 'TRANSFORMS'
    target = variable.targets[0]; target.id = light; target.transform_type = 'ROT_' + axis
    target.transform_space = 'WORLD_SPACE'; target.rotation_mode = 'XYZ'


def scalar_property_driver(socket: bpy.types.NodeSocket, light: bpy.types.Light, data_path: str) -> None:
    driver = socket.driver_add('default_value').driver
    driver.type = 'SCRIPTED'; driver.expression = 'source'
    variable = driver.variables.new(); variable.name = 'source'; variable.type = 'SINGLE_PROP'
    target = variable.targets[0]; target.id_type = 'LIGHT'; target.id = light; target.data_path = data_path


def bind_main_light(light: bpy.types.Object) -> None:
    if light.type != 'LIGHT' or light.data.type != 'SUN':
        raise ValueError('The HGRP directional preview adapter requires a Blender Sun')
    expressions = ('cos(z)*sin(y)*cos(x)+sin(z)*sin(x)', 'sin(z)*sin(y)*cos(x)-cos(z)*sin(x)', 'cos(y)*cos(x)')
    for axis, expression in zip('XYZ', expressions):
        socket = labelled_control('Light Direction ' + axis, 'VALUE').outputs[0]
        driver = socket.driver_add('default_value').driver
        driver.type = 'SCRIPTED'; driver.expression = expression
        for rotation_axis in 'XYZ': transform_variable(driver, light, rotation_axis)
    scalar_property_driver(labelled_control('Light Intensity', 'VALUE').outputs[0], light.data, 'energy')
    scalar_property_driver(labelled_control('Direct Intensity', 'VALUE').outputs[0], light.data, 'energy')
    color = labelled_control('Light Color', 'RGB').outputs[0]
    for index in range(3):
        driver = color.driver_add('default_value', index).driver
        driver.type = 'SCRIPTED'; driver.expression = 'source'
        variable = driver.variables.new(); variable.name = 'source'; variable.type = 'SINGLE_PROP'
        target = variable.targets[0]; target.id_type = 'LIGHT'; target.id = light.data; target.data_path = f'color[{index}]'
    light['hgrp_adapter_scope'] = 'Blender Sun orientation/color/strength drive shared preview uniforms; no native light-loop or shadow-atlas claim'


def create_main_light(name: str, direction: Vector, strength: float, color: tuple[float, float, float]) -> bpy.types.Object:
    if name in bpy.data.objects: raise ValueError(f'Light object already exists: {name}')
    if direction.length == 0.0 or strength < 0.0: raise ValueError('Nonzero direction and nonnegative strength required')
    data = bpy.data.lights.new(name, type='SUN'); data.energy = strength; data.color = color
    light = bpy.data.objects.new(name, data); bpy.context.scene.collection.objects.link(light)
    light.rotation_mode = 'XYZ'; light.rotation_euler = direction.normalized().to_track_quat('Z', 'Y').to_euler('XYZ')
    bind_main_light(light)
    return light
