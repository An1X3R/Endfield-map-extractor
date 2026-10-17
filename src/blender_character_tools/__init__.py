"""Persistent Blender UI for the extractor's existing character control data."""
import bpy
from bpy.app.handlers import persistent

from . import character_rig_ik, character_rig_panel, character_weather_panel, character_postprocess_panel


SHARED_MODULES = (character_rig_ik, character_rig_panel, character_weather_panel, character_postprocess_panel)
SHARED_CLASSES = (
    character_rig_ik.ENDFIELD_OT_setup_character_ik,
    character_rig_ik.ENDFIELD_OT_snap_character_ik,
    character_rig_ik.ENDFIELD_OT_select_ik_control,
    character_rig_panel.ENDFIELD_OT_select_rig_bone_collection,
    character_rig_panel.ENDFIELD_OT_select_rig_source_bone,
    character_rig_panel.ENDFIELD_OT_reset_expression_controller,
    character_rig_panel.ENDFIELD_OT_keyframe_expression_controller,
    character_rig_panel.ENDFIELD_PT_character_rig_panel,
    character_weather_panel.ENDFIELD_PT_material_weather,
) + character_postprocess_panel.CLASSES


def refresh_panels() -> None:
    """Claim UI classes without executing any file text or modifying scene data."""
    for module in SHARED_MODULES:
        module.register()


@persistent
def restore_panels_after_load(_file_path: str) -> None:
    """Reclaim classes after legacy file scripts finish loading."""
    refresh_panels()


class ENDFIELD_AP_character_tools(bpy.types.AddonPreferences):
    """Identify the installed owner and show the supported editing workflow."""
    bl_idname = __package__

    def draw(self, context: bpy.types.Context) -> None:
        layout = self.layout
        layout.label(text='终末地 · 角色工具 0.1.4')
        layout.label(text='选择提取器导出的角色或骨架：N 侧栏 → 终末地。')
        layout.label(text='天气参数位于材质属性 → 终末地 · 天气。')
        layout.label(text='场景后处理位于 N 侧栏 → 终末地 → 后处理，手动启用。')
        layout.label(text='无需运行文件内嵌脚本；插件不修改全局脚本信任设置。')
        layout.label(text='旧文件缺少的控制器需重新导出；不自动改绑、不重建材质。')


class ENDFIELD_OT_refresh_character_tools(bpy.types.Operator):
    """Restore the installed UI after manually running an older embedded script."""
    bl_idname = 'endfield.refresh_character_tools'
    bl_label = '刷新角色工作台'
    bl_description = '使用已安装插件的工作台，不运行文件内脚本、不更改模型'

    def execute(self, context: bpy.types.Context) -> set[str]:
        refresh_panels()
        for window in context.window_manager.windows:
            for area in window.screen.areas:
                area.tag_redraw()
        self.report({'INFO'}, '角色工作台已刷新')
        return {'FINISHED'}


class ENDFIELD_PT_character_tools_addon(bpy.types.Panel):
    """Keep an entry point visible in empty and unsupported files."""
    bl_idname = 'ENDFIELD_PT_character_tools_addon'
    bl_label = '终末地 · 插件'
    bl_space_type = 'VIEW_3D'
    bl_region_type = 'UI'
    bl_category = '终末地'
    bl_order = 10

    def draw(self, context: bpy.types.Context) -> None:
        self.layout.label(text='角色工具 0.1.4 · 已启用', icon='CHECKMARK')
        self.layout.label(text='选择导出的角色网格或骨架')
        self.layout.operator('endfield.refresh_character_tools', icon='FILE_REFRESH')


ADDON_CLASSES = (ENDFIELD_AP_character_tools, ENDFIELD_OT_refresh_character_tools,
                 ENDFIELD_PT_character_tools_addon)


def register() -> None:
    """Install one persistent owner with rollback on registration errors."""
    try:
        for cls in ADDON_CLASSES:
            if not cls.is_registered:
                bpy.utils.register_class(cls)
        refresh_panels()
        if restore_panels_after_load not in bpy.app.handlers.load_post:
            bpy.app.handlers.load_post.append(restore_panels_after_load)
    except Exception:
        unregister()
        raise


def unregister() -> None:
    """Remove only installed classes and callbacks; retain all authored data."""
    if restore_panels_after_load in bpy.app.handlers.load_post:
        bpy.app.handlers.load_post.remove(restore_panels_after_load)
    for cls in reversed(SHARED_CLASSES + ADDON_CLASSES):
        if cls.is_registered:
            bpy.utils.unregister_class(cls)
