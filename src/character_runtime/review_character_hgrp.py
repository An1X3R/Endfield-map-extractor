"""Merge source-driven character lighting branches and verify their live host state."""
from __future__ import annotations
import argparse
from dataclasses import asdict
import hashlib
import json
from pathlib import Path
import shutil
import sys
import bpy
from mathutils import Vector, Matrix

sys.path.insert(0,str(Path(__file__).resolve().parent))
from audit_character_materials import geometry_digest
from character_hgrp_light_adapter import create_main_light, labelled_control
from character_hair_lighting import HairShaderFrame, apply_hair_specular_branch, source_parameters, line_map_binding_from_material, bind_source_line_map
from character_eye_lighting import source_parameters as eye_parameters
from patch_character_eye_lighting import source_eye_groups, patch_eye_groups, eye_renderer_objects, add_tangent_sign_attribute, eye_material_instances, set_eye_controls, set_eye_response_mix, exact_source_record
from review_combined_weather import apply_state
from rebuild_character_materials import render_view
from patch_character_hgrp import attach_diffuse

def digest(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()

def check_eye_response() -> int:
    instances=eye_material_instances(source_eye_groups())
    if not instances:raise ValueError('No Eye material instances to verify')
    for instance in instances:
        for name,expected in (('Eye Response Mix',1.),('CP13 Eye Base Light Multiplier',0.),('CP13 Eye Highlight Multiplier',0.),('CP13 Eye Scattering Multiplier',0.)):
            actual=instance.inputs[name].default_value
            if abs(actual-expected)>1e-6:raise AssertionError(f'Eye source response mismatch: instance={instance.name}, input={name}, expected={expected}, actual={actual}')
    return len(instances)

def visible_source_group_names() -> set[str]:
    return {item.node_tree.name for obj in bpy.context.scene.objects if obj.type=='MESH' and not obj.hide_render for slot in obj.material_slots if slot.material and slot.material.use_nodes for item in slot.material.node_tree.nodes if item.type=='GROUP' and item.node_tree}

def check_face_drivers(anchor: bpy.types.Object) -> float:
    errors=[];contexts=[]
    active_groups=visible_source_group_names()
    labels=('Face Right live anchor basis','Face Up live anchor basis','Face Forward live anchor basis','Live explicit per-object origin')
    for tree in bpy.data.node_groups:
        if not tree.get('sdf_scope') or tree.name not in active_groups:continue
        tree=tree.evaluated_get(bpy.context.evaluated_depsgraph_get())
        contexts.append({'tree':tree.name,'users':tree.users,'drivers':[(curve.data_path,curve.driver.is_valid,[(v.targets[0].id.name if v.targets[0].id else None,v.targets[0].data_path) for v in curve.driver.variables]) for curve in tree.animation_data.drivers] if tree.animation_data else []})
        for column,label in enumerate(labels):
            nodes=tuple(item for item in tree.nodes if item.label==label)
            if len(nodes)!=1:raise ValueError(f'Expected live face driver node: tree={tree.name}, label={label}')
            errors.extend(abs(nodes[0].inputs[row].default_value-anchor.matrix_world[row][column]) for row in range(3))
    if not errors or max(errors)>1e-5:raise AssertionError(f'Face drivers differ from evaluated anchor: errors={errors}, contexts={contexts}')
    return max(errors)

def bind_face_anchor(anchor: bpy.types.Object, rig: bpy.types.Object, bone_name: str) -> dict[str,object]:
    if rig.type!='ARMATURE' or bone_name not in rig.pose.bones:raise ValueError(f'Explicit head-bone preset cannot resolve: rig={rig.name}, bone={bone_name}')
    bone=rig.pose.bones[bone_name]
    original=anchor.matrix_world.copy();original.translation=rig.matrix_world@bone.head
    anchor.parent=rig;anchor.parent_type='BONE';anchor.parent_bone=bone_name
    bpy.context.view_layer.update();anchor.matrix_world=original;bpy.context.view_layer.update()
    if max(abs(anchor.matrix_world[r][c]-original[r][c]) for r in range(4) for c in range(4))>1e-5:raise AssertionError('Face anchor keep-world parenting changed its requested basis')
    source_basis=bone.matrix_basis.copy();before=anchor.matrix_world.copy()
    bone.matrix_basis=source_basis@Matrix.Rotation(.25,4,'Y');bpy.context.scene.frame_set(bpy.context.scene.frame_current);bpy.context.view_layer.update()
    changed=max(abs(anchor.matrix_world[r][c]-before[r][c]) for r in range(4) for c in range(4))
    if changed<.01:raise AssertionError('Face SDF anchor did not follow the head pose')
    pose_driver_error=check_face_drivers(anchor)
    bone.matrix_basis=source_basis;bpy.context.scene.frame_set(bpy.context.scene.frame_current);bpy.context.view_layer.update()
    error=max(abs(anchor.matrix_world[r][c]-before[r][c]) for r in range(4) for c in range(4))
    if error>1e-5:raise AssertionError('Face anchor failed exact head-pose restore')
    check_face_drivers(anchor)
    return {'rig':rig.name,'bone':bone_name,'pose_response':changed,'restore_error':error,'pose_driver_error':pose_driver_error,'scope':'Explicit sample bone binding and origin, not recovered native per-instance matrix'}

def set_light(light: bpy.types.Object, direction: tuple[float,float,float], color: tuple[float,float,float], energy: float) -> dict[str,object]:
    expected=Vector(direction).normalized();light.rotation_euler=expected.to_track_quat('Z','Y').to_euler('XYZ');light.data.color=color;light.data.energy=energy
    bpy.context.scene.frame_set(bpy.context.scene.frame_current+1);bpy.context.view_layer.update()
    actual=Vector(tuple(labelled_control('Light Direction '+axis,'VALUE').outputs[0].default_value for axis in 'XYZ'))
    if (expected-actual).length>2e-6:raise AssertionError('Shared light direction did not follow the Blender Sun')
    return {'direction':list(actual),'color':list(color),'energy':energy}

def bind_hair_frame(tree: bpy.types.ShaderNodeTree, rig: bpy.types.Object, shader_frame: HairShaderFrame) -> dict[str,object]:
    axes=(shader_frame.source_x_in_blender_world,shader_frame.source_y_in_blender_world,shader_frame.source_z_in_blender_world)
    matrix=Matrix(tuple(tuple(axes[column][row] for column in range(3)) for row in range(3))).to_4x4()
    anchor=bpy.data.objects.new('EF Editable Hair Shader Frame',None);anchor.empty_display_type='ARROWS';anchor.empty_display_size=.25
    bpy.context.scene.collection.objects.link(anchor);anchor.parent=rig;bpy.context.view_layer.update();anchor.matrix_world=matrix
    anchor['scope']='Editable proxy for native skin-buffer header frame; follows armature object, not asserted as captured native matrix'
    for column,axis in enumerate('XYZ'):
        nodes=tuple(item for item in tree.nodes if item.label=='HGRP explicit shader-frame axis '+axis)
        if len(nodes)!=1:raise ValueError(f'Hair shader-frame axis missing or ambiguous: {axis}')
        for row in range(3):
            driver=nodes[0].inputs[row].driver_add('default_value').driver;driver.type='SCRIPTED';driver.expression='component'
            variable=driver.variables.new();variable.name='component';variable.type='SINGLE_PROP';variable.targets[0].id=anchor;variable.targets[0].data_path=f'matrix_world[{column}][{row}]'
        normalize=tree.nodes.new('ShaderNodeVectorMath');normalize.operation='NORMALIZE';normalize.label='Normalize live hair shader-frame '+axis
        normalize['endfield_layout_section']='HGRP Hair Shader Frame'
        for link in tuple(nodes[0].outputs[0].links):tree.links.new(normalize.outputs[0],link.to_socket)
        tree.links.new(nodes[0].outputs[0],normalize.inputs[0])
    bpy.context.scene.frame_set(bpy.context.scene.frame_current);bpy.context.view_layer.update()
    errors=[abs(next(item for item in tree.nodes if item.label=='HGRP explicit shader-frame axis '+axis).inputs[row].default_value-anchor.matrix_world[row][column]) for column,axis in enumerate('XYZ') for row in range(3)]
    if max(errors)>1e-5:raise AssertionError('Live hair shader-frame drivers failed')
    return {'anchor':anchor.name,'parent':rig.name,'driver_error':max(errors),'frame_status':shader_frame.frame_status}

def main(args: argparse.Namespace) -> None:
    if args.output.exists():raise FileExistsError(args.output)
    sys.path.insert(0,str(args.repository_src))
    from blender_scene_materials import layout_shader_node_tree
    bpy.ops.wm.open_mainfile(filepath=str(args.blend));apply_state(0.,0.)
    names=tuple(obj.name for obj in bpy.data.objects if obj.type=='MESH' and 'source_renderer_identity' in obj)
    before=geometry_digest(names)
    anchor=bpy.data.objects[args.anchor];rig=bpy.data.objects[args.rig]
    anchor_receipt=bind_face_anchor(anchor,rig,args.head_bone)
    lighting=bpy.data.node_groups['EF Character Artist Lighting - APPROXIMATION']
    shared=bpy.data.node_groups['EF HGRP Constructor State + Preview Environment']
    hairs=tuple(tree for tree in bpy.data.node_groups if tree.get('source_shader')=='HGRP/CharacterNPR_Hair' and tree.name in visible_source_group_names())
    if not hairs:raise ValueError('No visible source Hair material groups')
    axis_bridge=HairShaderFrame(tuple(args.source_x),tuple(args.source_y),tuple(args.source_z),tuple(args.source_world_y),args.frame_status)
    hair_receipt=[]
    for hair in hairs:
        attach_diffuse(hair,shared,bpy.data.node_groups['EF HGRP Local Diffuse - Verified Equations'])
        _,native_hair=exact_source_record(hair)
        line_binding=line_map_binding_from_material(native_hair);bind_source_line_map(hair,line_binding)
        receipt=asdict(apply_hair_specular_branch(hair,lighting,source_parameters(hair,axis_bridge,line_binding)))
        receipt['live_frame']=bind_hair_frame(hair,rig,axis_bridge);hair_receipt.append(receipt)
        layout_shader_node_tree(hair)
    eye_groups=source_eye_groups();eye_objects=eye_renderer_objects(eye_groups)
    signs=add_tangent_sign_attribute(eye_objects)
    eye_receipt=patch_eye_groups(layout_shader_node_tree,shared)
    for group in eye_groups:
        set_eye_controls(eye_material_instances((group,)),1.,1.,(0.,0.,0.),eye_parameters(group).parallax_scale)
        set_eye_response_mix(eye_material_instances((group,)),1.)
    check_eye_response()
    for tree in bpy.data.node_groups:
        if tree.get('sdf_scope') or tree in hairs:
            if tree.get('sdf_scope'):tree['hgrp_scope']='Local diffuse plus source face SDF ramp and masked radial/geometry hemisphere; explicit head basis, constructor/preview state; skin specular and native shadow atlas remain incomplete'
            branches=tuple(item for item in tree.nodes if item.label=='Editable native-sampling response / baseline')
            if len(branches)!=1:raise ValueError(f'Face response branch ambiguous: {tree.name}')
            control=tree.nodes.new('ShaderNodeValue');control.label='Local HGRP response weight'
            control['endfield_layout_section']='HGRP Source Response';control.outputs[0].default_value=1.
            tree.links.new(control.outputs[0],branches[0].inputs[0]);layout_shader_node_tree(tree)
    for obj in bpy.context.scene.objects:
        if obj.type=='LIGHT':obj.hide_render=True
    light=create_main_light('EF HGRP Main Sun',Vector((-.6,-1.,.7)),.9,(1.,1.,1.))
    if geometry_digest(names)!=before:raise AssertionError('Integration changed source geometry before rendering')
    args.output.mkdir(parents=True)
    points=tuple(bpy.data.objects[name].matrix_world@Vector(corner) for name in names if not bpy.data.objects[name].hide_render for corner in bpy.data.objects[name].bound_box)
    minimum=Vector(tuple(min(point[axis] for point in points) for axis in range(3)));maximum=Vector(tuple(max(point[axis] for point in points) for axis in range(3)))
    center=(minimum+maximum)/2;extent=max(maximum-minimum)
    close=center.copy();close.z=minimum.z+(maximum.z-minimum.z)*.8
    cases=[]
    for name,direction,color in (('left',(-1.,-.35,.35),(1.,1.,1.)),('right',(1.,-.35,.35),(1.,1.,1.)),('warm',(-.6,-1.,.7),(1.,.65,.38)),('cool',(-.6,-1.,.7),(.38,.65,1.))):
        state=set_light(light,direction,color,.9)
        result=render_view(args.output,name+'_eevee.png','BLENDER_EEVEE_NEXT',Vector((0,-1,.02)),close,extent*.39,512,512,32)
        cases.append({'name':name,'state':state,'render':result})
    set_light(light,(-.6,-1.,.7),(1.,1.,1.),.9)
    cases.append({'name':'neutral','render':render_view(args.output,'neutral_eevee.png','BLENDER_EEVEE_NEXT',Vector((0,-1,.02)),close,extent*.39,512,512,32)})
    cases.append({'name':'neutral_cycles','render':render_view(args.output,'neutral_cycles.png','CYCLES',Vector((0,-1,.02)),close,extent*.39,384,384,32)})
    cases.append({'name':'full_body','render':render_view(args.output,'full_body_eevee.png','BLENDER_EEVEE_NEXT',Vector((0,-1,.05)),center,extent,640,640,32)})
    text=bpy.data.texts.new('HGRP REVIEW CONTROLS.txt');text.write('Rotate EF HGRP Main Sun; color and strength drive shared preview lighting. Face SDF anchor follows the explicitly chosen head bone. Eye CP13 is the constructor initial 0,0,0; Matcap and source eye shading remain enabled. Final runtime values are unavailable. Weather frozen dry. Source assets, mesh, UV, weights and bind pose retained. This is partial local HGRP reconstruction: full native light loop, source shadow atlas, skin specular and exact renderer composition remain unfinished.\n')
    scene=bpy.context.scene;scene.render.engine='BLENDER_EEVEE_NEXT'
    target=args.output/'character_hgrp.blend';bpy.ops.wm.save_as_mainfile(filepath=str(target));bpy.ops.wm.open_mainfile(filepath=str(target))
    if geometry_digest(names)!=before:raise AssertionError('Merged lighting changed source geometry, UV, weights, bind or visibility')
    set_light(bpy.data.objects['EF HGRP Main Sun'],(-.6,-1.,.7),(1.,1.,1.),.9)
    if bpy.data.objects[args.anchor].parent_bone!=args.head_bone:raise AssertionError('Face head-bone binding lost on reopen')
    check_face_drivers(bpy.data.objects[args.anchor])
    eye_instance_count=check_eye_response()
    snapshot=args.output/'source';snapshot.mkdir()
    scripts=('review_character_hgrp.py','character_hgrp_light_adapter.py','character_hgrp_diffuse.py','patch_character_hgrp.py','character_sdf_nodes.py','patch_character_sdf.py','character_hair_lighting.py','patch_character_hair_lighting.py','character_eye_lighting.py','patch_character_eye_lighting.py')
    hashes={}
    for name in scripts:
        source=Path(__file__).parent/name;shutil.copy2(source,snapshot/name);hashes[name]=digest(source)
    report={'status':'merged_rendered_reopened','input':str(args.blend),'input_sha256':digest(args.blend),'output':str(target),'output_sha256':digest(target),'geometry_preserved':True,'renderer_count':len(names),'anchor':anchor_receipt,'hair':hair_receipt,'eyes':eye_receipt,'eye_tangent_signs':signs,'eye_source_response_verified_instances':eye_instance_count,'renders':cases,'source_hashes':hashes,'weather_frozen':True,'full_hgrp_reconstructed':False,'runtime_volume_captured':False,'production_export_changed':False}
    (args.output/'verification.json').write_text(json.dumps(report,ensure_ascii=False,indent=2),encoding='utf-8')

if __name__=='__main__':
    parser=argparse.ArgumentParser(description=__doc__)
    for name in ('blend','output','repository-src'):parser.add_argument('--'+name,type=Path,required=True)
    for name in ('anchor','rig','head-bone'):parser.add_argument('--'+name,required=True)
    for name in ('source-x','source-y','source-z','source-world-y'):parser.add_argument('--'+name,type=float,nargs=3,required=True)
    parser.add_argument('--frame-status',choices=('fbx_root_frame_proxy_unverified','runtime_verified'),required=True)
    main(parser.parse_args(sys.argv[sys.argv.index('--')+1:]))
