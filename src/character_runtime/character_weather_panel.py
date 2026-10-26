"""Expose existing weather and clothing effect controls in material properties."""
from __future__ import annotations

from pathlib import Path
from collections.abc import Sequence
import json
from typing import TypedDict

import bpy


TEXT_NAME = 'Endfield Weather Panel.py'
CONTROL_ROWS = (
    ('User Override Weight', '手动控制权重'),
    ('User Effect Z', '湿润度'),
    ('User Effect W', '覆雪量'),
    ('User Effect X', '雨水强度'),
    ('User Effect Y', '浸水强度'),
    ('User Water Height', '水面高度'),
)


class WeatherPanelReport(TypedDict):
    status: str
    text: str
    material_count: int


def weather_groups(material: bpy.types.Material) -> tuple[bpy.types.NodeTree, ...]:
    """Resolve controls through this material, including appended/renamed groups."""
    options = material.get('endfield_material_options')
    simple_only: bool = False
    if options is not None:
        selection = json.loads(options)
        if not selection['weather']['effects']:
            return ()
        simple_only = selection['mode'] == 'simplified' and selection['weather']['quality'] == 'simple'
    pending = [material.node_tree] if material.use_nodes else []
    visited: set[int] = set()
    matches: list[bpy.types.NodeTree] = []
    while pending:
        tree = pending.pop()
        if tree.as_pointer() in visited:
            continue
        visited.add(tree.as_pointer())
        if tree.get('endfield_simple_weather') or (
            not simple_only and tree.get('endfield_weather_selected_effects')
        ):
            matches.append(tree)
        elif not simple_only and any(
            item.item_type == 'SOCKET' and item.in_out == 'OUTPUT'
            and item.name == 'Native Global Wetness' for item in tree.interface.items_tree
        ):
            labels = {node.label for node in tree.nodes if node.bl_idname == 'ShaderNodeValue'}
            if all(label in labels for label, _ in CONTROL_ROWS):
                matches.append(tree)
        pending.extend(node.node_tree for node in tree.nodes
                       if node.bl_idname == 'ShaderNodeGroup' and node.node_tree is not None)
    return tuple(matches)


def weather_socket(tree: bpy.types.NodeTree, label: str) -> bpy.types.NodeSocket:
    """Return the original scalar socket; no duplicate state or update driver."""
    matches = tuple(node for node in tree.nodes if node.bl_idname == 'ShaderNodeValue' and node.label == label)
    if len(matches) != 1:
        raise ValueError(f'Weather control must be unique: tree={tree.name}, label={label}, count={len(matches)}')
    return matches[0].outputs[0]


def clothing_effect_controls(material: bpy.types.Material) -> tuple[bpy.types.NodeSocket, ...]:
    """Find native effect values without retaining duplicate parameter evidence."""
    pending = [material.node_tree] if material.use_nodes else []
    visited: set[int] = set()
    controls: list[bpy.types.NodeSocket] = []
    while pending:
        tree = pending.pop()
        if tree.as_pointer() in visited:
            continue
        visited.add(tree.as_pointer())
        controls.extend(item.outputs[0] for item in tree.nodes
                        if item.bl_idname == 'ShaderNodeValue'
                        and item.get('endfield_special_vfx_control') is True
                        and item.outputs[0].is_linked)
        pending.extend(item.node_tree for item in tree.nodes
                       if item.bl_idname == 'ShaderNodeGroup' and item.node_tree is not None
                       and not item.get('endfield_authoring_parameters')
                       and any(socket.is_linked for socket in item.outputs))
    return tuple(controls)


class ENDFIELD_PT_material_clothing_effects(bpy.types.Panel):
    """Edit source-selected effects without introducing callbacks or drivers."""
    bl_label = '终末地 · 服装特效'
    bl_idname = 'ENDFIELD_PT_material_clothing_effects'
    bl_space_type = 'PROPERTIES'
    bl_region_type = 'WINDOW'
    bl_context = 'material'
    bl_order = -2

    @classmethod
    def poll(cls, context: bpy.types.Context) -> bool:
        return context.material is not None and bool(clothing_effect_controls(context.material))

    def draw(self, context: bpy.types.Context) -> None:
        layout = self.layout
        layout.use_property_split = True
        layout.label(text='0 关闭；1 为源材质强度')
        for index, socket in enumerate(clothing_effect_controls(context.material), start=1):
            layout.prop(socket, 'default_value', text='特效强度 ' + str(index))


class ENDFIELD_PT_material_weather(bpy.types.Panel):
    """Present existing shader values without changing their evaluation."""

    bl_label = '终末地 · 天气'
    bl_idname = 'ENDFIELD_PT_material_weather'
    bl_space_type = 'PROPERTIES'
    bl_region_type = 'WINDOW'
    bl_context = 'material'
    bl_order = -1

    @classmethod
    def poll(cls, context: bpy.types.Context) -> bool:
        return context.material is not None and bool(weather_groups(context.material))

    def draw(self, context: bpy.types.Context) -> None:
        layout = self.layout
        layout.use_property_split = True
        layout.label(text='共用此天气组的材质同步变化', icon='INFO')
        layout.label(text='强度 0–1；水面高度为世界坐标')
        for tree in weather_groups(context.material):
            box = layout.box()
            if tree.get('endfield_simple_weather') or tree.get('endfield_weather_selected_effects'):
                box.label(text='简化天气 · 独立材质控制' if tree.get('endfield_simple_weather') else '精细天气 · 已选效果')
                labels = {node.label for node in tree.nodes if node.bl_idname == 'ShaderNodeValue'}
                for label, title in CONTROL_ROWS[1:4]:
                    if label in labels:
                        box.prop(weather_socket(tree, label), 'default_value', text=title)
                continue
            for label, title in CONTROL_ROWS[:4]:
                box.prop(weather_socket(tree, label), 'default_value', text=title)
            header, body = layout.panel('endfield_weather_water_' + tree.name, default_closed=True)
            header.label(text='浸水设置')
            if body is not None:
                for label, title in CONTROL_ROWS[4:]:
                    body.prop(weather_socket(tree, label), 'default_value', text=title)


def register() -> None:
    """Replace only this panel when another exported character is opened."""
    owner = getattr(bpy.types, 'ENDFIELD_PT_character_tools_addon', None)
    if owner is not None and owner.__module__ != globals().get('__package__'):
        return
    for cls in (ENDFIELD_PT_material_weather, ENDFIELD_PT_material_clothing_effects):
        previous = getattr(bpy.types, cls.bl_idname, None)
        if previous is not None:
            bpy.utils.unregister_class(previous)
        bpy.utils.register_class(cls)


def attach_weather_panel(materials: Sequence[bpy.types.Material]) -> WeatherPanelReport:
    """Embed a portable optional UI script without enabling global script trust."""
    supported = tuple(material for material in materials
                      if weather_groups(material) or clothing_effect_controls(material))
    if not supported:
        return {'status': 'no_weather_materials', 'text': '', 'material_count': 0}
    source = Path(__file__).read_text(encoding='utf-8') + '\nregister()\n'
    text = bpy.data.texts.get(TEXT_NAME)
    if text is None:
        text = bpy.data.texts.new(TEXT_NAME)
    text.clear()
    text.write(source)
    text.use_module = True
    register()
    return {'status': 'attached', 'text': text.name, 'material_count': len(supported)}


def verify_weather_panel(report: WeatherPanelReport) -> str:
    """Check portability while scripts may be disabled during the reopen audit."""
    if report['status'] == 'no_weather_materials':
        return 'not_applicable'
    text = bpy.data.texts.get(report['text'])
    if text is None or not text.use_module or not text.as_string().endswith('\nregister()\n'):
        raise AssertionError('Embedded material weather panel is missing or not reloadable')
    compile(text.as_string(), text.name, 'exec')
    return 'passed'
