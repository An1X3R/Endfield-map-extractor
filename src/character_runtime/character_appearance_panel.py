"""Expose per-character native appearance controls without redraw callbacks."""
import json
import math
from typing import cast

import bpy

from .character_shader_uniforms import migrate_shader_uniforms

from . import character_outline as outline
from . import character_rig_panel as rigs


CONTROL_FORMAT = 'EndfieldOutlineControls/1'
CONTROL_MARKER = 'endfield_outline_controls'
ENABLED = 'endfield_outline_enabled'
WIDTH = 'endfield_outline_width'
BASE_WIDTH = 'endfield_outline_base_scale'
OWNER = 'endfield_outline_owner'
POTENTIAL = 'potential_appearance'
POTENTIAL_STATE = 'endfield_potential_overlay_binding'


def active_rig(context: bpy.types.Context) -> bpy.types.Object:
    """Follow explicit ownership without choosing an arbitrary scene character."""
    active = context.active_object
    if active is not None and active.get('outline_candidate_source'):
        source = context.scene.objects.get(active['outline_candidate_source'])
        if source is None:
            raise ValueError(f'Outline source no longer exists: {active.name}')
        active = source
    if active is not None and active.type == 'EMPTY' and active.parent is not None:
        if active.parent.type == 'ARMATURE':
            active = active.parent
    return rigs.resolve_rig_for_active_object(active)


def source_meshes(scene: bpy.types.Scene, rig: bpy.types.Object) -> tuple[bpy.types.Object, ...]:
    """Use the rig's explicit geometry membership, not scene-wide material names."""
    report = rigs._authoring_report(rig)
    names = rigs._string_rows(report.get('source_geometry'), 'source_geometry')
    return tuple(obj for name in names if (obj := scene.objects.get(name)) is not None
                 and obj.type == 'MESH' and obj.get('source_renderer_pipeline_json') is not None
                 and obj.get('outline_candidate_contract') is None)


def potential_owners(scene: bpy.types.Scene, rig: bpy.types.Object) -> tuple[bpy.types.Object, ...]:
    """Find the existing native switch without parsing large build receipts per draw."""
    return tuple(obj for obj in scene.objects
                 if POTENTIAL in obj and POTENTIAL_STATE in obj
                 and (obj == rig or obj.parent == rig or obj.get(rigs.OWNER_PROPERTY) == rig.name))


def owned_outlines(scene: bpy.types.Scene, sources: tuple[bpy.types.Object, ...]) -> tuple[bpy.types.Object, ...]:
    names = frozenset(obj.name for obj in sources)
    return tuple(obj for obj in scene.objects if obj.get('outline_candidate_contract') == outline.CONTRACT
                 and obj.get('outline_candidate_source') in names)


def _width_input(shell: bpy.types.Object) -> tuple[bpy.types.NodesModifier, str]:
    modifiers = tuple(mod for mod in shell.modifiers if mod.type == 'NODES' and mod.node_group is not None
                      and mod.node_group.get('outline_candidate_contract') == outline.CONTRACT)
    if len(modifiers) != 1:
        raise ValueError(f'Outline requires one owned hull modifier: {shell.name}, count={len(modifiers)}')
    modifier = cast(bpy.types.NodesModifier, modifiers[0])
    sockets = tuple(item for item in modifier.node_group.interface.items_tree
                    if item.item_type == 'SOCKET' and item.in_out == 'INPUT'
                    and item.name == 'Artistic Meters per Source Width')
    if len(sockets) != 1:
        raise ValueError(f'Outline width socket is ambiguous: {shell.name}')
    return modifier, sockets[0].identifier


def _property_path(name: str) -> str:
    return '[' + json.dumps(name) + ']'


def _driver_variable(driver: bpy.types.Driver, name: str, owner: bpy.types.Object, path: str) -> None:
    variable = driver.variables.new()
    variable.name = name
    variable.type = 'SINGLE_PROP'
    variable.targets[0].id = owner
    variable.targets[0].data_path = path


def _replace_driver(owner: bpy.types.Object, path: str) -> bpy.types.Driver:
    owner.driver_remove(path)
    driver = owner.driver_add(path).driver
    driver.type = 'SCRIPTED'
    while driver.variables:
        driver.variables.remove(driver.variables[0])
    return driver


def bind_outline_controls(scene: bpy.types.Scene, rig: bpy.types.Object,
                          shells: tuple[bpy.types.Object, ...]) -> None:
    """Bind existing geometry once; edits only change native scalar/visibility drivers."""
    if not shells:
        raise ValueError('No supported outline geometry is available for this character')
    if rig.get(CONTROL_MARKER) not in (None, CONTROL_FORMAT):
        raise ValueError(f'Unsupported outline controls: {rig.name}')
    if rig.get(CONTROL_MARKER) is None and any(key in rig for key in (ENABLED, WIDTH)):
        raise ValueError(f'Outline property name is already in use: {rig.name}')
    bindings: list[tuple[bpy.types.Object, bpy.types.Object, str, float]] = []
    for shell in shells:
        source = scene.objects.get(shell.get('outline_candidate_source', ''))
        if source is None:
            raise ValueError(f'Outline source no longer exists: {shell.name}')
        if shell.get(OWNER) not in (None, rig.name):
            raise ValueError(f'Outline is already controlled by another rig: {shell.name}')
        if shell.animation_data and shell.animation_data.action is not None:
            raise ValueError(f'Outline has an object action; keep its authored animation: {shell.name}')
        modifier, identifier = _width_input(shell)
        base = shell.get(BASE_WIDTH, modifier[identifier])
        if type(base) not in (int, float) or not math.isfinite(base) or base <= 0:
            raise ValueError(f'Invalid outline base width: object={shell.name}, value={base!r}')
        path = modifier.path_from_id() + _property_path(identifier)
        bindings.append((shell, source, path, float(base)))
    if rig.get(CONTROL_MARKER) is None:
        rig[ENABLED] = True
        rig[WIDTH] = 1.0
        rig[CONTROL_MARKER] = CONTROL_FORMAT
    rig.id_properties_ui(ENABLED).update(description='控制当前角色的描边；不改变原模型材质')
    rig.id_properties_ui(WIDTH).update(min=0.01, max=10.0, soft_min=0.1, soft_max=3.0,
                                     description='相对现有描边宽度的倍率；1 保留当前宽度')
    for shell, source, path, base in bindings:
        shell[BASE_WIDTH] = base
        shell[OWNER] = rig.name
        width = _replace_driver(shell, path)
        _driver_variable(width, 'width', rig, _property_path(WIDTH))
        width.expression = f'{base!r} * width'
        for field in ('hide_viewport', 'hide_render'):
            visibility = _replace_driver(shell, field)
            _driver_variable(visibility, 'enabled', rig, _property_path(ENABLED))
            _driver_variable(visibility, 'source_hidden', source, field)
            visibility.expression = 'not enabled or source_hidden'
        shell.update_tag()
    rig.update_tag()


class ENDFIELD_OT_prepare_outline_controls(bpy.types.Operator):
    """Adopt existing hulls or add source-supported hulls for the selected character."""
    bl_idname = 'endfield.prepare_outline_controls'
    bl_label = '启用描边调整'
    bl_description = '接入已有描边；缺少时根据源材质创建当前角色的描边，不处理其它角色'
    bl_options = {'REGISTER', 'UNDO'}

    def execute(self, context: bpy.types.Context) -> set[str]:
        try:
            rig = active_rig(context)
            sources = source_meshes(context.scene, rig)
            shells = owned_outlines(context.scene, sources)
            if not shells:
                outline.attach_selected_outline_hulls(context.scene, sources, 0.001)
                shells = owned_outlines(context.scene, sources)
            bind_outline_controls(context.scene, rig, shells)
        except (ValueError, TypeError, KeyError) as error:
            self.report({'ERROR'}, str(error))
            return {'CANCELLED'}
        self.report({'INFO'}, f'已接入 {len(shells)} 个描边对象')
        return {'FINISHED'}


class ENDFIELD_OT_optimize_material_refresh(bpy.types.Operator):
    """Replace broad shader dependencies while keeping native dynamic uniforms."""
    bl_idname = 'endfield.optimize_material_refresh'
    bl_label = '优化材质刷新'
    bl_description = '优化选物体和动画时重复更新材质的连接；保留灯光、天气、面部和头发响应，可撤销'
    bl_options = {'REGISTER', 'UNDO'}

    def execute(self, context: bpy.types.Context) -> set[str]:
        try:
            rig = active_rig(context)
            sources = source_meshes(context.scene, rig)
            materials = {slot.material.name: slot.material for obj in sources
                         for slot in obj.material_slots if slot.material is not None}
            report = migrate_shader_uniforms(tuple(materials.values()),
                                             tuple(context.scene.objects), context.scene)
        except (ValueError, TypeError, KeyError, RuntimeError) as error:
            self.report({'ERROR'}, str(error))
            return {'CANCELLED'}
        self.report({'INFO'}, f'已优化 {report["migrated"]} 处材质刷新连接；未降低材质精度')
        return {'FINISHED'}


class ENDFIELD_OT_texture_edit_view(bpy.types.Operator):
    """Use Blender's lightweight textured viewport without modifying render materials."""
    bl_idname = 'endfield.texture_edit_view'
    bl_label = '贴图编辑（快速）'
    bl_description = '切换实体贴图视图，暂停昂贵的材质预览；完整材质和正式渲染保持不变'

    @classmethod
    def poll(cls, context: bpy.types.Context) -> bool:
        return context.area is not None and context.area.type == 'VIEW_3D'

    def execute(self, context: bpy.types.Context) -> set[str]:
        shading = context.space_data.shading
        shading.type = 'SOLID'
        shading.color_type = 'TEXTURE'
        return {'FINISHED'}


class ENDFIELD_OT_material_check_view(bpy.types.Operator):
    """Return to the original material-preview renderer explicitly."""
    bl_idname = 'endfield.material_check_view'
    bl_label = '检查材质效果'
    bl_description = '切换回完整材质预览；复杂材质首次加载可能需要等待'

    @classmethod
    def poll(cls, context: bpy.types.Context) -> bool:
        return context.area is not None and context.area.type == 'VIEW_3D'

    def execute(self, context: bpy.types.Context) -> set[str]:
        context.space_data.shading.type = 'MATERIAL'
        return {'FINISHED'}


class ENDFIELD_PT_character_appearance(bpy.types.Panel):
    """Keep frequent per-character appearance edits together in the sidebar."""
    bl_idname = 'ENDFIELD_PT_character_appearance'
    bl_label = '外观与描边'
    bl_space_type = 'VIEW_3D'
    bl_region_type = 'UI'
    bl_category = '终末地'
    bl_order = 1

    def draw(self, context: bpy.types.Context) -> None:
        layout = self.layout
        row = layout.row(align=True)
        row.operator('endfield.texture_edit_view', icon='SHADING_TEXTURE')
        row.operator('endfield.material_check_view', icon='SHADING_RENDERED')
        try:
            rig = active_rig(context)
        except (ValueError, TypeError) as error:
            layout.label(text='选择角色网格或骨架', icon='INFO')
            layout.label(text=str(error))
            return
        box = layout.box()
        box.label(text='描边 · 当前角色')
        if rig.get(CONTROL_MARKER) == CONTROL_FORMAT:
            box.prop(rig, _property_path(ENABLED), text='显示描边')
            row = box.row()
            row.enabled = bool(rig[ENABLED])
            row.prop(rig, _property_path(WIDTH), text='宽度倍率', slider=True)
        else:
            box.operator('endfield.prepare_outline_controls', icon='MOD_SOLIDIFY')
        box = layout.box()
        box.label(text='满潜外观')
        owners = potential_owners(context.scene, rig)
        for owner in owners:
            box.prop(owner, _property_path(POTENTIAL), text='启用满潜外观', toggle=True)
        if not owners:
            box.label(text='此文件未包含满潜外观控制数据', icon='INFO')
        layout.operator('endfield.optimize_material_refresh', icon='FILE_REFRESH')


CLASSES = (ENDFIELD_OT_prepare_outline_controls, ENDFIELD_OT_texture_edit_view,
           ENDFIELD_OT_material_check_view, ENDFIELD_OT_optimize_material_refresh,
           ENDFIELD_PT_character_appearance)


def register() -> None:
    """Register UI only; opening a file never generates or scans material graphs."""
    for cls in CLASSES:
        if not cls.is_registered:
            bpy.utils.register_class(cls)
