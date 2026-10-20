"""Bind source-selected appearance overlays to a portable Blender control."""
from __future__ import annotations

import json

import bpy


PROPERTY = 'potential_appearance'
STATE = 'endfield_potential_overlay_binding'


def resolve_renderers(scene: bpy.types.Scene, identities: tuple[str, ...]) -> tuple[bpy.types.Object, ...]:
    """Require an unambiguous source identity for every selected renderer."""
    keys = tuple(identity.casefold() for identity in identities)
    if not keys or len(set(keys)) != len(keys):
        raise ValueError('Potential overlay selection must contain unique source identities')
    resolved: list[bpy.types.Object] = []
    for key in keys:
        matches = tuple(obj for obj in scene.objects if obj.type == 'MESH'
                        and str(obj.get('source_renderer_identity', '')).casefold() == key)
        if len(matches) != 1:
            raise ValueError(f'Potential renderer must resolve exactly once: identity={key}, matches={len(matches)}')
        resolved.append(matches[0])
    return tuple(resolved)


def install_overlay_control(scene: bpy.types.Scene, owner: bpy.types.Object,
                            identities: tuple[str, ...]) -> tuple[bpy.types.Object, ...]:
    """Install visibility drivers after checking all affected Blender objects."""
    objects = resolve_renderers(scene, identities)
    if scene.objects.get(owner.name) != owner:
        raise ValueError(f'Potential control owner is not in the target scene: {owner.name}')
    if PROPERTY in owner or STATE in owner:
        raise ValueError(f'Potential control is already installed: {owner.name}')
    for obj in objects:
        curves = tuple(obj.animation_data.drivers) if obj.animation_data is not None else ()
        action = obj.animation_data.action if obj.animation_data is not None else None
        if action is not None or any(curve.data_path in ('hide_viewport', 'hide_render') for curve in curves):
            raise ValueError(f'Potential renderer already has visibility animation or an object action: {obj.name}')
        if not obj.hide_render or not obj.hide_viewport:
            raise ValueError(f'Source-disabled potential renderer is unexpectedly visible: {obj.name}')
    owner[PROPERTY] = False
    owner.id_properties_ui(PROPERTY).update(description='满潜外观：启用源配置指定的最高精度材质覆盖层')
    owner[STATE] = json.dumps({'format': 'EndfieldPotentialOverlay/1', 'renderers': identities})
    for obj in objects:
        obj.hide_set(False)
        for path in ('hide_viewport', 'hide_render'):
            curve = obj.driver_add(path)
            driver = curve.driver
            driver.type = 'SCRIPTED'
            variable = driver.variables.new()
            variable.name = 'enabled'
            variable.type = 'SINGLE_PROP'
            variable.targets[0].id = owner
            variable.targets[0].data_path = f'["{PROPERTY}"]'
            driver.expression = '1 - enabled'
    return objects
