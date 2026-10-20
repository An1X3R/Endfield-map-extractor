"""Validate source-derived canopy fibres against a real full-material character."""
from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys

import bpy
import numpy as np
from mathutils import Vector, Matrix

REPO=Path(__file__).resolve().parents[1]
sys.path[:0]=[str(REPO/'src'),str(REPO/'src/character_runtime')]
from audit_character_materials import source_geometry_digest
from character_fur_appearance import verify_fur_appearance
from character_fur_source_shell import verify_source_fur_shell
from character_fur_microfibres import FurMicrofibreProfile, attach_fur_microfibres, triangle_fibre_samples


def world_positions(obj: bpy.types.Object) -> np.ndarray:
    evaluated=obj.evaluated_get(bpy.context.evaluated_depsgraph_get())
    attribute=evaluated.data.attributes['position']
    values=np.empty(len(attribute.data)*3,dtype=np.float32)
    attribute.data.foreach_get('vector',values)
    matrix=np.asarray(evaluated.matrix_world,dtype=np.float64)
    return values.reshape(-1,3)@matrix[:3,:3].T+matrix[:3,3]


parser=argparse.ArgumentParser()
parser.add_argument('--source',required=True,type=Path)
parser.add_argument('--output',required=True,type=Path)
parser.add_argument('--samples',required=True,type=int)
args=parser.parse_args(sys.argv[sys.argv.index('--')+1:])
if args.samples<=0:
    raise ValueError('Fibre test requires a positive explicit sample count')
args.output.mkdir(parents=True,exist_ok=False)
bpy.ops.wm.open_mainfile(filepath=str(args.source),use_scripts=False,load_ui=False)
shell, = (item for item in bpy.data.objects if item.get('fur_artist_appearance') and not item.hide_render)
source=json.loads(shell['source_fur_shell'])
digest=source_geometry_digest((shell.name,source['source_mesh']))
profile=FurMicrofibreProfile(0,triangle_fibre_samples(args.samples),.000045,.08,.6,1.03,1.4,.15,.8)
record=attach_fur_microfibres(shell,profile)
fibres=bpy.data.objects[record['object_name']]
bpy.context.view_layer.update()
initial=world_positions(fibres)
rig=next(mod.object for mod in shell.modifiers if mod.type=='ARMATURE')
location=rig.location.copy()
translation=rig.matrix_world.translation.copy()
rig.location+=Vector((.13,-.08,.09))
bpy.context.view_layer.update()
expected=np.asarray(rig.matrix_world.translation-translation)
root_error=float(np.max(np.abs(world_positions(fibres)-initial-expected)))
assert root_error<1e-5,root_error
rig.location=location
bpy.context.view_layer.update()
weights_by_group:dict[int,float]={}
for vertex in shell.data.vertices:
    for group in vertex.groups:
        weights_by_group[group.group]=weights_by_group.get(group.group,0.)+group.weight
bone=next(rig.pose.bones[shell.vertex_groups[index].name]
          for index in sorted(weights_by_group,key=weights_by_group.get,reverse=True)
          if shell.vertex_groups[index].name in rig.pose.bones)
basis=bone.matrix_basis.copy()
pose_bone_name=bone.name
bone.matrix_basis=bone.matrix_basis@Matrix.Rotation(.11,4,'X')
bpy.context.view_layer.update()
pose_motion=float(np.max(np.abs(world_positions(fibres)-initial)))
assert pose_motion>1e-6,pose_motion
bone.matrix_basis=basis
bpy.context.view_layer.update()
restored_error=float(np.max(np.abs(world_positions(fibres)-initial)))
assert restored_error<1e-5,restored_error
assert digest==source_geometry_digest((shell.name,source['source_mesh']))
verify_source_fur_shell(source)
verify_fur_appearance(shell)
scene=bpy.context.scene
scene.render.resolution_x=768
scene.render.resolution_y=768
scene.render.engine='BLENDER_EEVEE_NEXT'
center=sum((shell.matrix_world@Vector(c) for c in shell.bound_box),Vector())/8
scene.camera.data.ortho_scale=.5
scene.camera.location=center+Vector((0.,-3.,.12))
scene.camera.rotation_euler=(center-scene.camera.location).to_track_quat('-Z','Y').to_euler()
pixels=[]
for frame in (6,66):
    scene.frame_set(frame)
    path=args.output/f'fibres_close_{frame:03}.png'
    scene.render.filepath=str(path)
    bpy.ops.render.render(write_still=True)
    image=bpy.data.images.load(str(path),check_existing=False)
    values=np.empty(len(image.pixels),dtype=np.float32)
    image.pixels.foreach_get(values)
    pixels.append(values.reshape(-1,4)[:,:3])
    bpy.data.images.remove(image)
time_delta=float(np.max(np.abs(pixels[1]-pixels[0])))
assert time_delta>1./255.,time_delta
scene.frame_set(66)
scene.camera.data.ortho_scale=.78
scene.render.filepath=str(args.output/'fibres_full.png')
bpy.ops.render.render(write_still=True)
bpy.context.preferences.filepaths.save_version=0
output=args.output/'艾尔黛拉_完整材质_细绒暗燃.blend'
bpy.ops.wm.save_as_mainfile(filepath=str(output),compress=False)
fibre_name=fibres.name
shell_name=shell.name
bpy.ops.wm.open_mainfile(filepath=str(output),use_scripts=False,load_ui=False)
fibres=bpy.data.objects[fibre_name]
assert not fibres.hide_render
verify_source_fur_shell(source)
verify_fur_appearance(bpy.data.objects[shell_name])
scene=bpy.context.scene
scene.camera.data.ortho_scale=.5
scene.render.engine='CYCLES'
scene.cycles.samples=24
scene.cycles.use_denoising=True
scene.cycles.transparent_max_bounces=48
scene.render.filepath=str(args.output/'fibres_cycles.png')
bpy.ops.render.render(write_still=True)
report={'output':str(output),'fibres':record,'material_mode':'full','source_shell_unchanged':True,
        'root_error_m':root_error,'pose_motion_m':pose_motion,'restored_error_m':restored_error,
        'pose_test_bone':pose_bone_name, 'time_rgb_delta':time_delta,
        'reopened':True,'rendered_engines':['EEVEE','Cycles']}
(args.output/'receipt.json').write_text(json.dumps(report,ensure_ascii=False,indent=2),encoding='utf-8')
print('FUR_MICROFIBRES_SMOKE_PASSED',str(output),flush=True)
