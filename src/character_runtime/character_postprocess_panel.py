"""Scene-owned compositor controls for the persistent Blender extension."""
from pathlib import Path

import bpy
from bpy_extras.io_utils import ExportHelper, ImportHelper

if __package__:
    from . import character_postprocess as post
else:
    import character_postprocess as post


CONTROL_LABELS = {
    'Exposure EV': '曝光 EV', 'Contrast': '对比度', 'Saturation': '饱和度',
    'Bloom Strength': '辉光强度', 'Bloom Threshold': '高光阈值',
    'Bloom Softness': '阈值柔和度', 'Bloom Radius': '扩散半径',
    'Style Mix': '曲线混合', 'Sharpen': '锐化强度',
}
SECTION_LABELS = (('Basic', '基础调整'), ('Bloom', '辉光'),
                  ('Style', '风格与曲线'), ('Finish', '输出'))


class ENDFIELD_OT_post_install(bpy.types.Operator):
    """Insert native processing without replacing the user's compositor."""
    bl_idname = 'endfield.post_install'
    bl_label = '启用场景后处理'
    bl_description = '为当前场景添加原生合成节点，保留已有合成链和色彩管理'
    bl_options = {'REGISTER', 'UNDO'}

    def execute(self, context: bpy.types.Context) -> set[str]:
        post.install(context.scene)
        return {'FINISHED'}


class ENDFIELD_OT_post_bypass(bpy.types.Operator):
    """Compare the original image without discarding authored values."""
    bl_idname = 'endfield.post_bypass'
    bl_label = '查看原图'
    bl_description = '暂时旁路效果，保留全部参数'
    bl_options = {'REGISTER', 'UNDO'}

    def execute(self, context: bpy.types.Context) -> set[str]:
        post.bypass(context.scene)
        return {'FINISHED'}


class ENDFIELD_OT_post_resume(bpy.types.Operator):
    """Resume the authored postprocessing stage."""
    bl_idname = 'endfield.post_resume'
    bl_label = '恢复效果'
    bl_options = {'REGISTER', 'UNDO'}

    def execute(self, context: bpy.types.Context) -> set[str]:
        post.resume(context.scene)
        return {'FINISHED'}


class ENDFIELD_OT_post_reset(bpy.types.Operator):
    """Reset only values owned by this feature."""
    bl_idname = 'endfield.post_reset'
    bl_label = '重置后处理参数'
    bl_description = '恢复中性参数和直线曲线；保留后处理节点'
    bl_options = {'REGISTER', 'UNDO'}

    def execute(self, context: bpy.types.Context) -> set[str]:
        post.reset_defaults(context.scene)
        return {'FINISHED'}


class ENDFIELD_OT_post_remove(bpy.types.Operator):
    """Reconnect the prior compositor and restore owned settings."""
    bl_idname = 'endfield.post_remove'
    bl_label = '移除并恢复原合成链'
    bl_description = '移除本工具的处理节点，恢复接入前的合成连接和输出抖动值'
    bl_options = {'REGISTER', 'UNDO'}

    def execute(self, context: bpy.types.Context) -> set[str]:
        post.remove(context.scene)
        return {'FINISHED'}


class ENDFIELD_OT_post_export(bpy.types.Operator, ExportHelper):
    """Write a portable preset without embedding images or game assets."""
    bl_idname = 'endfield.post_export'
    bl_label = '保存后处理预设'
    filename_ext = '.json'
    filter_glob: bpy.props.StringProperty(default='*.json', options={'HIDDEN'})

    def execute(self, context: bpy.types.Context) -> set[str]:
        try:
            post.export_preset(context.scene, Path(self.filepath))
        except (OSError, ValueError) as error:
            self.report({'ERROR'}, str(error))
            return {'CANCELLED'}
        self.report({'INFO'}, '后处理预设已保存')
        return {'FINISHED'}


class ENDFIELD_OT_post_import(bpy.types.Operator, ImportHelper):
    """Validate a selected preset before touching scene values."""
    bl_idname = 'endfield.post_import'
    bl_label = '载入后处理预设'
    bl_options = {'REGISTER', 'UNDO'}
    filename_ext = '.json'
    filter_glob: bpy.props.StringProperty(default='*.json', options={'HIDDEN'})

    def execute(self, context: bpy.types.Context) -> set[str]:
        try:
            post.import_preset(context.scene, Path(self.filepath))
        except (OSError, ValueError) as error:
            self.report({'ERROR'}, str(error))
            return {'CANCELLED'}
        self.report({'INFO'}, '后处理预设已载入')
        return {'FINISHED'}


class ENDFIELD_OT_post_nodes(bpy.types.Operator):
    """Open a compositor editor with the owned stage selected."""
    bl_idname = 'endfield.post_nodes'
    bl_label = '打开后处理节点'
    bl_description = '切换当前区域为合成编辑器，选中后处理组；按 Tab 编辑内部曲线'

    @classmethod
    def poll(cls, context: bpy.types.Context) -> bool:
        return context.area is not None and post.find_group(context.scene) is not None

    def execute(self, context: bpy.types.Context) -> set[str]:
        node = post.find_group(context.scene)
        if node is None:
            raise ValueError('Current scene has no Endfield postprocessing stage')
        context.area.type = 'NODE_EDITOR'
        space = context.area.spaces.active
        space.tree_type = 'CompositorNodeTree'
        space.pin = False
        tree = node.id_data
        for item in tree.nodes:
            item.select = item == node
        tree.nodes.active = node
        self.report({'INFO'}, '已选中后处理组，按 Tab 可编辑内部节点；Shift+F5 返回三维视图')
        return {'FINISHED'}


class ENDFIELD_PT_scene_postprocess(bpy.types.Panel):
    """Expose actual native sockets instead of maintaining duplicate values."""
    bl_idname = 'ENDFIELD_PT_scene_postprocess'
    bl_label = '终末地 · 后处理'
    bl_space_type = 'VIEW_3D'
    bl_region_type = 'UI'
    bl_category = '终末地'
    bl_order = 20

    def draw(self, context: bpy.types.Context) -> None:
        layout = self.layout
        stages = post.stage_nodes(context.scene)
        if not stages:
            layout.label(text='角色、动物和地图共用；仅作用于当前场景')
            layout.operator('endfield.post_install', icon='NODE_COMPOSITING')
            return
        if post.is_shared(context.scene):
            layout.label(text='此场景与副本共用后处理节点', icon='INFO')
            layout.operator('endfield.post_install', text='分离为当前场景的独立后处理', icon='DUPLICATE')
            return
        layout.label(text='渲染合成效果；普通材质预览不显示', icon='INFO')
        layout.operator('endfield.post_resume' if post.is_bypassed(context.scene)
                        else 'endfield.post_bypass', icon='HIDE_OFF')
        row = layout.row(align=True)
        row.operator('endfield.post_import', text='载入预设', icon='IMPORT')
        row.operator('endfield.post_export', text='保存预设', icon='EXPORT')
        for index, stage in enumerate(stages):
            if len(stages) > 1:
                layout.label(text=f'输出分支 {index + 1} · {stage.name}')
            for section, label in SECTION_LABELS:
                header, body = layout.panel(f'endfield_post_{index}_{section}',
                                            default_closed=section in {'Style', 'Finish'})
                header.label(text=label)
                if body is None:
                    continue
                body.use_property_split = True
                for name, _value, _minimum, _maximum, panel in post.CONTROL_SPECS:
                    if panel == section:
                        body.prop(stage.inputs[name], 'default_value', text=CONTROL_LABELS[name])
                if section == 'Style':
                    body.template_curve_mapping(stage.node_tree.nodes[post.CURVES_NODE_NAME], 'mapping')
                elif section == 'Finish' and index == 0:
                    body.prop(context.scene.render, 'dither_intensity', text='输出抖动')
        layout.operator('endfield.post_nodes', icon='NODETREE')
        layout.operator('endfield.post_reset', icon='LOOP_BACK')
        layout.operator('endfield.post_remove', icon='X')


CLASSES = (ENDFIELD_OT_post_install, ENDFIELD_OT_post_bypass, ENDFIELD_OT_post_resume,
           ENDFIELD_OT_post_reset, ENDFIELD_OT_post_remove, ENDFIELD_OT_post_export,
           ENDFIELD_OT_post_import, ENDFIELD_OT_post_nodes, ENDFIELD_PT_scene_postprocess)


def register() -> None:
    """Register controls without creating compositor data or changing settings."""
    for cls in CLASSES:
        if not cls.is_registered:
            bpy.utils.register_class(cls)


def unregister() -> None:
    """Remove the UI while leaving native scene data usable."""
    for cls in reversed(CLASSES):
        if cls.is_registered:
            bpy.utils.unregister_class(cls)
