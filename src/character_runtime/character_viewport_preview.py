"""Expose a native, script-free viewport switch without changing render evaluation."""
from __future__ import annotations

from typing import TypedDict

import bpy


FORMAT = 'EndfieldViewportPreview/1'
PROPERTY = '完整预览'
PROPERTY_PATH = '["完整预览"]'
MARKER = 'endfield_viewport_preview'


class ModifierState(TypedDict):
    object: str
    modifier: str
    show_render: bool


class PreviewReport(TypedDict):
    format: str
    armature: str
    property: str
    modifiers: list[ModifierState]


def _preview_modifiers() -> tuple[tuple[bpy.types.Object, bpy.types.Modifier], ...]:
    targets: list[tuple[bpy.types.Object, bpy.types.Modifier]] = []
    for obj in bpy.context.scene.objects:
        for modifier in obj.modifiers:
            if modifier.type != 'NODES' or modifier.node_group is None:
                continue
            tree = modifier.node_group
            receiver = (modifier.name == 'EF Live HGRP Shadow Fields'
                        and tree.get('shadow_attributes') == 'ef_hgrp_shadow_r,ef_hgrp_shadow_g')
            caster = (modifier.name == 'EF Source Caster Alpha'
                      and 'hgrp_caster_source_identity' in obj
                      and tree.get('alpha_mode') in ('clip', 'dither'))
            if receiver or caster:
                targets.append((obj, modifier))
    return tuple(sorted(targets, key=lambda pair: (pair[0].name, pair[1].name)))


def _verify_driver(obj: bpy.types.Object, modifier: bpy.types.Modifier,
                   rig: bpy.types.Object) -> None:
    path = modifier.path_from_id('show_viewport')
    curves = tuple(curve for curve in obj.animation_data.drivers if curve.data_path == path) if obj.animation_data else ()
    if len(curves) != 1:
        raise AssertionError(f'Viewport switch requires one driver: object={obj.name}, path={path}')
    curve = curves[0]
    driver = curve.driver
    if curve.mute or driver.type != 'AVERAGE' or len(driver.variables) != 1:
        raise AssertionError(f'Viewport switch driver has changed: object={obj.name}, path={path}')
    variable = driver.variables[0]
    target = variable.targets[0]
    if variable.type != 'SINGLE_PROP' or target.id != rig or target.data_path != PROPERTY_PATH:
        raise AssertionError(f'Viewport switch driver targets another property: object={obj.name}, path={path}')


def attach_viewport_preview(rig: bpy.types.Object) -> PreviewReport:
    """Preserve full preview by default; subsequent exports retain the user's choice."""
    if rig.type != 'ARMATURE':
        raise ValueError(f'Viewport preview controller must be an armature: {rig.name}')
    existing = rig.get(MARKER)
    if existing is not None and existing != FORMAT:
        raise ValueError(f'Unsupported viewport preview controller version: {existing}')
    if existing is None:
        if PROPERTY in rig:
            raise ValueError(f'Viewport preview property already belongs to another system: {rig.name}')
        rig[PROPERTY] = True
        rig[MARKER] = FORMAT
    rig.id_properties_ui(PROPERTY).update(
        description='开启：完整 HGRP 遮挡预览；关闭：快速编辑动作。仅影响视口，正式渲染始终使用原有完整效果。')
    states: list[ModifierState] = []
    for obj, modifier in _preview_modifiers():
        path = modifier.path_from_id('show_viewport')
        curves = tuple(curve for curve in obj.animation_data.drivers if curve.data_path == path) if obj.animation_data else ()
        if curves:
            _verify_driver(obj, modifier, rig)
        else:
            if not modifier.show_viewport:
                raise ValueError(f'Cannot take over a disabled viewport modifier: object={obj.name}, modifier={modifier.name}')
            driver = modifier.driver_add('show_viewport').driver
            driver.type = 'AVERAGE'
            variable = driver.variables.new()
            variable.name = 'full_preview'
            variable.type = 'SINGLE_PROP'
            variable.targets[0].id = rig
            variable.targets[0].data_path = PROPERTY_PATH
        states.append({'object': obj.name, 'modifier': modifier.name,
                       'show_render': modifier.show_render})
    help_text = bpy.data.texts.get('Viewport preview controls')
    if help_text is None:
        help_text = bpy.data.texts.new('Viewport preview controls')
    help_text.clear()
    help_text.write(f'选择骨架 {rig.name} → 物体属性 → 自定义属性 → {PROPERTY}。\n'
                    '关闭：快速编辑动作；开启：检查完整 HGRP 遮挡效果。\n'
                    '该开关仅暂停视口中的遮挡计算；正式渲染保留原有完整效果。\n'
                    '无需安装插件或启用自动运行脚本。\n')
    rig.update_tag()
    bpy.context.view_layer.update()
    return {'format': FORMAT, 'armature': rig.name, 'property': PROPERTY, 'modifiers': states}


def verify_viewport_preview(report: PreviewReport) -> dict[str, str | int]:
    """Verify saved drivers and independent render flags in either viewport mode."""
    if report['format'] != FORMAT or report['property'] != PROPERTY:
        raise ValueError('Unsupported viewport preview receipt')
    rig = bpy.data.objects[report['armature']]
    if rig.get(MARKER) != FORMAT or PROPERTY not in rig:
        raise AssertionError(f'Viewport preview controller is missing: {rig.name}')
    expected = {(row['object'], row['modifier']) for row in report['modifiers']}
    actual = {(obj.name, modifier.name) for obj, modifier in _preview_modifiers()}
    if expected != actual:
        raise AssertionError(f'Viewport preview modifier coverage changed: expected={expected}, actual={actual}')
    bpy.context.view_layer.update()
    for row in report['modifiers']:
        obj = bpy.data.objects[row['object']]
        modifier = obj.modifiers[row['modifier']]
        _verify_driver(obj, modifier, rig)
        if modifier.show_render != row['show_render'] or modifier.show_viewport != bool(rig[PROPERTY]):
            raise AssertionError(f'Viewport preview state differs from its controller: object={obj.name}, modifier={modifier.name}')
    return {'status': 'verified', 'modifiers': len(actual), 'armature': rig.name}
