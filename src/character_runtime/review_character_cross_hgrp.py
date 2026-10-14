"""Apply the verified character branches by source family to audited character samples."""
from __future__ import annotations
import argparse
from dataclasses import asdict, dataclass
from typing import Callable, Mapping
import hashlib
import json
from pathlib import Path
import shutil
import sys
import bpy
from mathutils import Vector

sys.path.insert(0,str(Path(__file__).resolve().parent))
from audit_character_materials import geometry_digest
from rebuild_character_materials import render_view,object_value,float_value
from patch_character_hgrp import attach_diffuse,build_profile_state,source_keywords,by_label
from character_hgrp_diffuse import build_hgrp_diffuse
from character_hgrp_light_adapter import create_main_light
from patch_character_sdf import make_face_anchor,attach_sdf
from review_character_hgrp import visible_source_group_names,bind_face_anchor,bind_hair_frame,set_light,check_eye_response
from character_hair_lighting import HairShaderFrame,apply_hair_specular_branch,source_parameters as hair_parameters,line_map_binding_from_material,bind_source_line_map,hair_variant
from character_eye_lighting import source_parameters as eye_parameters,build_eye_matcap_branch
from patch_character_eye_lighting import eye_renderer_objects,add_tangent_sign_attribute,eye_material_instances,set_eye_controls,set_eye_response_mix,exact_source_record
from character_face_highlight import attach_face_highlight
from character_skin_specular import build_skin_ggx,attach_skin_specular
from attach_skin_surface import attach_skin_surface
from character_additional_light_state import AdditionalLightSettings,build_additional_light_state
from character_hair_additional_lights import HairAdditionalSettings,additional_hair_support,attach_hair_default_lights
from character_punctual_kernel import build_skin_punctual
from attach_skin_additional_lights import attach_skin_additional_lights
from character_node_graph import color_mix,node
from review_character_skin_lights import add_light,refresh


def select_eye_branches(groups: tuple[bpy.types.ShaderNodeTree,...]) -> tuple[tuple[bpy.types.ShaderNodeTree,...],list[dict[str,object]]]:
    """Dispatch independent Eye features without forcing source-disabled branches on."""
    selected=[];decisions=[]
    required=('_UseMatcap','_EyeHighLight','_UseDiffRampMap')
    feature_keywords=('_MATCAP_ON','_EYE_HIGHLIGHT','_DIFF_RAMP_ON')
    for tree in groups:
        if tree['source_shader']!='HGRP/CharacterNPR_Eye':continue
        floats=object_value(json.loads(tree['source_floats']),'source Eye floats')
        controls={name:float_value(floats[name],'source Eye '+name) for name in required}
        if any(value not in (0.,1.) for value in controls.values()):raise ValueError('Source Eye feature switches are not binary: '+tree.name)
        keywords=source_keywords(tree)
        for name,keyword in zip(required,feature_keywords):
            if (controls[name]==1.)!=(keyword in keywords):raise ValueError('Source Eye switch/keyword disagree: '+tree.name+'/'+name+'/'+keyword)
        eye_parameters(tree)
        selected.append(tree)
        decisions.append({'group':tree.name,'source_identity':json.loads(tree['source_identity']),'source_controls':controls,'source_keywords':keywords,'status':'implemented_branch_selected','action':'Apply independent source Matcap, EyeHighlight and diffuse-ramp branches without changing source switches.'})
    return tuple(selected),decisions


def select_hair_branches(groups: tuple[bpy.types.ShaderNodeTree,...]) -> tuple[tuple[bpy.types.ShaderNodeTree,...],list[dict[str,object]]]:
    """Select compiled Hair variants from actual source switches and keywords."""
    selected=[];decisions=[]
    required=('_UseBumpMap','_UseSpecBumpMap','_UseSpecRampMap','_UseMetallicGlossMap')
    required_keywords={'_NORMALMAP','_SPECULAR_NORMALMAP','_SPEC_RAMP_ON','_METALLICSPECGLOSSMAP'}
    for tree in groups:
        if tree['source_shader']!='HGRP/CharacterNPR_Hair':continue
        floats=object_value(json.loads(tree['source_floats']),'source Hair floats')
        missing=tuple(name for name in required if name not in floats)
        if missing:raise ValueError('Required source Hair switches absent: '+tree.name+'/'+repr(missing))
        controls={name:float_value(floats[name],'source Hair '+name) for name in required}
        if any(value not in (0.,1.) for value in controls.values()):raise ValueError('Source Hair feature switches are not binary: '+tree.name)
        keywords=source_keywords(tree)
        disabled=tuple(name for name,value in controls.items() if value!=1.)
        absent=tuple(sorted(required_keywords-set(keywords)))
        variant=hair_variant(tree)
        enabled=variant!='branch_not_implemented'
        if enabled:selected.append(tree)
        decisions.append({'group':tree.name,'source_identity':json.loads(tree['source_identity']),'source_controls':controls,'source_keywords':keywords,'status':'implemented_branch_selected' if enabled else 'branch_not_implemented','source_variant':variant,'disabled_source_controls':disabled,'missing_branch_keywords':absent,'action':'Apply the matching compiled Hair variant without changing source switches.' if enabled else 'Preserve baseline Hair and generic diffuse; source switches remain unchanged.'})
    return tuple(selected),decisions


@dataclass(frozen=True)
class CharacterFrameSettings:
    head_bone: str
    face_right: tuple[float, float, float]
    face_up: tuple[float, float, float]
    face_forward: tuple[float, float, float]
    hair_x: tuple[float, float, float]
    hair_y: tuple[float, float, float]
    hair_z: tuple[float, float, float]


@dataclass(frozen=True)
class LoadedHgrpBuild:
    names: tuple[str, ...]
    geometry_before: dict[str, object]
    center: Vector
    close: Vector
    extent: float
    main_light: bpy.types.Object
    point_light: bpy.types.Object
    spot_light: bpy.types.Object
    details: Mapping[str, object]


def build_loaded_hgrp(profile_path: Path, shader_identity_path: Path, frames: CharacterFrameSettings, layout_shader_node_tree: Callable[[bpy.types.NodeTree], None]) -> LoadedHgrpBuild:
    """Attach source-selected character equations and initial lights without rendering or saving."""
    from character_material_profile import validate_profile_shader_identities
    shader_identities = validate_profile_shader_identities(profile_path, shader_identity_path)
    from attach_skin_volume import attach_skin_volume
    names=tuple(o.name for o in bpy.data.objects if o.type=='MESH' and 'source_renderer_identity' in o)
    before=geometry_digest(names);visible=visible_source_group_names()
    groups=tuple(t for t in bpy.data.node_groups if 'source_shader' in t and t.name in visible)
    if not groups:raise ValueError('No visible source material groups')
    if any(t.get('hgrp_scope') for t in groups):raise ValueError('Input already contains HGRP diffuse attachment')
    state=build_profile_state(json.loads(profile_path.read_text(encoding='utf-8')))
    diffuse=build_hgrp_diffuse('EF HGRP Local Diffuse - Verified Equations')
    skins=tuple(t for t in groups if t['source_shader']=='HGRP/CharacterNPR_Skin')
    hairs=tuple(t for t in groups if t['source_shader']=='HGRP/CharacterNPR_Hair')
    faces=tuple(t for t in skins if '_SDFLIGHTMAP' in source_keywords(t))
    preserved=[];connected=[]
    for tree in groups:
        if tree['source_shader'] in ('HGRP/CharacterNPR','HGRP/CharacterNPR_Skin','HGRP/CharacterNPR_Hair') and '_CHARACTER_FUR' not in source_keywords(tree):
            attach_diffuse(tree,state,diffuse);connected.append(tree.name)
        else:preserved.append({'group':tree.name,'shader':tree['source_shader'],'keywords':source_keywords(tree),'reason':'Keep the existing source-family branch; no Skin substitution.'})
    rigs=tuple(o for o in bpy.data.objects if o.type=='ARMATURE')
    if (hairs or faces) and len(rigs)!=1:raise ValueError('Face/hair host-frame binding requires an unambiguous armature')
    face_receipt=[]
    if faces:
        rig=rigs[0]
        if frames.head_bone not in rig.pose.bones:raise ValueError('Explicit head bone absent: '+frames.head_bone)
        origin=tuple(rig.matrix_world@rig.pose.bones[frames.head_bone].head)
        anchor=make_face_anchor('EF Editable Face SDF Anchor',origin,tuple(frames.face_right),tuple(frames.face_up),tuple(frames.face_forward))
        shader_hash=shader_identities['HGRP/CharacterNPR_Skin']['object_sha256']
        for tree in faces:face_receipt.append(attach_sdf(tree,anchor,shader_hash,layout_shader_node_tree))
        anchor_receipt=bind_face_anchor(anchor,rig,frames.head_bone)
    else:anchor_receipt={'status':'not_applicable','reason':'No visible source Skin SDF material'}
    hair_receipt=[]
    supported_hairs,hair_branch_status=select_hair_branches(hairs)
    for tree in supported_hairs:
        frame=HairShaderFrame(tuple(frames.hair_x),tuple(frames.hair_y),tuple(frames.hair_z),(0.,0.,1.),'fbx_root_frame_proxy_unverified')
        _,native=exact_source_record(tree);binding=line_map_binding_from_material(native);bind_source_line_map(tree,binding)
        receipt=asdict(apply_hair_specular_branch(tree,bpy.data.node_groups['EF Character Artist Lighting - APPROXIMATION'],hair_parameters(tree,frame,binding)))
        receipt['live_frame']=bind_hair_frame(tree,rigs[0],frame);hair_receipt.append(receipt)
        response=node(tree,'ShaderNodeValue','Local HGRP response weight','HGRP Source Response');response.outputs[0].default_value=1.
        tree.links.new(response.outputs[0],by_label(tree,'Editable native-sampling response / baseline').inputs[0])
    hair_additional_support_rows: tuple[tuple[bpy.types.ShaderNodeTree,bool,str],...]=tuple((tree,*additional_hair_support(tree)) for tree in hairs)
    hair_source_state: dict[str,tuple[str,str,str,tuple[str,...]]]={tree.name:(tree['source_identity'],tree['source_floats'],tree['source_colors'],source_keywords(tree)) for tree in hairs}
    eye_receipt=[]
    eyes,eye_branch_status=select_eye_branches(groups)
    if eyes:
        add_tangent_sign_attribute(eye_renderer_objects(eyes))
        eye_diffuse=build_hgrp_diffuse('EF HGRP Eye ComputeNPRDiffuse - Verified Equations');layout_shader_node_tree(eye_diffuse)
        for tree in eyes:
            eye_parameters(tree)
            eye_receipt.append(build_eye_matcap_branch(tree,layout_shader_node_tree,state,eye_diffuse))
        for tree in eyes:
            parameters=eye_parameters(tree)
            instances=eye_material_instances((tree,));set_eye_controls(instances,parameters.use_matcap,parameters.use_eye_highlight,(0.,0.,0.),parameters.parallax_scale);set_eye_response_mix(instances,1.)
        preserved=[item for item in preserved if item['group'] not in {tree.name for tree in eyes}]
        check_eye_response()
    for light in bpy.context.scene.objects:
        if light.type=='LIGHT':light.hide_render=True
    main_light=create_main_light('EF HGRP Main Sun',Vector((-.6,-1.,.7)),.9,(1.,1.,1.))
    points=tuple(bpy.data.objects[n].matrix_world@Vector(c) for n in names if not bpy.data.objects[n].hide_render for c in bpy.data.objects[n].bound_box)
    low=Vector(tuple(min(p[a] for p in points) for a in range(3)));high=Vector(tuple(max(p[a] for p in points) for a in range(3)));center=(low+high)*.5;extent=max(high-low);close=center.copy();close.z=low.z+(high.z-low.z)*.78
    point=add_light('EF Fill Point','POINT',close+Vector((-.65,-.7,.08))*extent,close,(.45,.7,1.),70.)
    spot=add_light('EF Fill Spot','SPOT',close+Vector((.65,-.65,.1))*extent,close,(1.,.58,.35),60.)
    for light in (point,spot):light.hide_render=True
    skin_receipt=[]
    if skins or any(supported for _,supported,_ in hair_additional_support_rows):
        states: tuple[bpy.types.ShaderNodeTree,...]=tuple(build_additional_light_state(light,AdditionalLightSettings(.004,extent*2.5,1.)) for light in (point,spot))
    else:
        states: tuple[bpy.types.ShaderNodeTree,...]=()
    if skins:
        punctual=build_skin_punctual('EF Compiled Skin Plain Additional Light');ggx=build_skin_ggx()
        for tree in skins:
            if tree in faces and '_HIGHLIGHT_MAP' in source_keywords(tree):attach_face_highlight(tree)
            skin_receipt.append(asdict(attach_skin_specular(tree,ggx)))
            main_color=attach_skin_surface(tree)
            volume=attach_skin_volume(tree,(0.,0.,0.,1.),(8.742278e-8,-1.,0.,.4000000059604645),(0.,0.,0.,1.),(0.,.0010000000474974513,-1.,0.))
            extra=attach_skin_additional_lights(tree,punctual,states)
            result=color_mix(tree,'ADD',1.,main_color,volume,'Skin main plus source volume','Skin Final Composition')
            tree.links.new(color_mix(tree,'ADD',1.,result,extra,'Skin with live additional lights','Skin Final Composition'),by_label(tree,'Artist-lit NPR diffuse - shared engines').inputs['Color'])
    hair_additional_receipt: list[dict[str,object]]=[]
    hair_settings=HairAdditionalSettings(1.,0.,0.,1.,1.,1.,2.,1.,'Explicit dry mode0 host preset: CP12.z=1, unmapped record gate=0, wrap=0, normalization=1, specweight=1, no-cookie=1, falloff=2, wet gain=1. Not native GPU capture.')
    for tree,supported,reason in hair_additional_support_rows:
        if not supported:
            hair_additional_receipt.append({'group':tree.name,'source_identity':json.loads(tree['source_identity']),'status':'source_variant_not_implemented','source_variant':hair_variant(tree),'reason':reason})
            continue
        contribution=attach_hair_default_lights(tree,states,hair_settings,layout_shader_node_tree)
        hair_additional_receipt.append({'group':tree.name,'source_identity':json.loads(tree['source_identity']),'status':'connected','source_variant':hair_variant(tree),'source_family':reason,'lights':[str(state['bound_light']) for state in states],'contribution_node':contribution.node.name,'settings':asdict(hair_settings),'scope':'Verified Hair mode0 only; explicit host Point/Spot state and dry inputs, not native runtime light records.'})
    if any(hair_source_state[tree.name]!=(tree['source_identity'],tree['source_floats'],tree['source_colors'],source_keywords(tree)) for tree in hairs):
        raise AssertionError('Hair additional-light attachment changed source switches, material values, identity, or keywords')
    for tree in bpy.data.node_groups:layout_shader_node_tree(tree)
    if geometry_digest(names)!=before:raise AssertionError('Cross-character attachment changed source geometry')
    details={'diffuse_groups':connected,'skin':skin_receipt,'hair':hair_receipt,'hair_additional_lights':hair_additional_receipt,'eyes':eye_receipt,'face':face_receipt,'face_anchor':anchor_receipt,'preserved_families':preserved,'eye_branch_status':eye_branch_status,'hair_branch_status':hair_branch_status}
    return LoadedHgrpBuild(names,before,center,close,extent,main_light,point,spot,details)


def main(args: argparse.Namespace) -> None:
    if args.output.exists():raise FileExistsError(args.output)
    sys.path.insert(0,str(args.repository_src))
    from blender_scene_materials import layout_shader_node_tree
    bpy.ops.wm.open_mainfile(filepath=str(args.blend),use_scripts=False)
    frames=CharacterFrameSettings(args.head_bone,tuple(args.face_right),tuple(args.face_up),tuple(args.face_forward),tuple(args.hair_x),tuple(args.hair_y),tuple(args.hair_z))
    built=build_loaded_hgrp(args.profile,args.shader_identities,frames,layout_shader_node_tree)
    names,before,center,close,extent=built.names,built.geometry_before,built.center,built.close,built.extent
    main_light,point,spot=built.main_light,built.point_light,built.spot_light
    connected,skin_receipt,hair_receipt,eye_receipt=(built.details[k] for k in ('diffuse_groups','skin','hair','eyes'))
    face_receipt,anchor_receipt,preserved=(built.details[k] for k in ('face','face_anchor','preserved_families'))
    eye_branch_status,hair_branch_status=(built.details[k] for k in ('eye_branch_status','hair_branch_status'))
    args.output.mkdir(parents=True);renders=[];refresh()
    for engine,name in (('BLENDER_EEVEE_NEXT','neutral_eevee.png'),('CYCLES','neutral_cycles.png')):
        renders.append(render_view(args.output,name,engine,Vector((0.,-1.,.02)),close,extent*.46,640,640,32))
    for tag,direction,color in (('main_left',(-1.,-.35,.35),(1.,1.,1.)),('main_right',(1.,-.35,.35),(1.,1.,1.))):
        set_light(main_light,direction,color,.9);renders.append(render_view(args.output,tag+'.png','BLENDER_EEVEE_NEXT',Vector((0.,-1.,.02)),close,extent*.46,512,512,32))
    set_light(main_light,(-.6,-1.,.7),(1.,1.,1.),.9)
    point.hide_render=False;refresh();renders.append(render_view(args.output,'point_blue.png','BLENDER_EEVEE_NEXT',Vector((0.,-1.,.02)),close,extent*.46,640,640,32))
    point.hide_render=True;spot.hide_render=False;refresh();renders.append(render_view(args.output,'spot_warm.png','BLENDER_EEVEE_NEXT',Vector((0.,-1.,.02)),close,extent*.46,640,640,32))
    point.hide_render=False;refresh()
    for engine,name in (('BLENDER_EEVEE_NEXT','mixed_eevee.png'),('CYCLES','mixed_cycles.png')):renders.append(render_view(args.output,name,engine,Vector((0.,-1.,.02)),close,extent*.46,640,640,32))
    renders.append(render_view(args.output,'full_eevee.png','BLENDER_EEVEE_NEXT',Vector((0.,-1.,.02)),center,extent*.85,640,800,32))
    target=args.output/'character_hgrp.blend';bpy.ops.wm.save_as_mainfile(filepath=str(target));bpy.ops.wm.open_mainfile(filepath=str(target),use_scripts=False);refresh()
    if geometry_digest(names)!=before:raise AssertionError('Cross-character saved geometry changed')
    result={'status':'rendered_reopened','input':str(args.blend),'input_sha256':hashlib.sha256(args.blend.read_bytes()).hexdigest(),'output':str(target),'output_sha256':hashlib.sha256(target.read_bytes()).hexdigest(),'source_renderers':len(names),'visible_renderers':sum(not bpy.data.objects[n].hide_render for n in names),'geometry_preserved':True,'diffuse_groups':connected,'skin':skin_receipt,'hair':hair_receipt,'hair_additional_lights':hair_additional_receipt,'eyes':eye_receipt,'face':face_receipt,'face_anchor':anchor_receipt,'preserved_families':preserved,'renders':renders,'weather_frozen':True,'full_hgrp':False,'limits':['Frames and constructor/preview state are explicit host bridges, not runtime captures.','Fur retained as original source mesh/material; no Skin shader substitution.','Additional Skin/Hair bindings use the verified mode0 response with explicit host lights, without native shadow production.']}
    snapshot=args.output/'source';snapshot.mkdir();hashes={}
    for name in ('review_character_cross_hgrp.py','patch_character_hgrp.py','character_hgrp_diffuse.py','character_hgrp_light_adapter.py','patch_character_sdf.py','character_sdf_nodes.py','character_hair_lighting.py','character_hair_additional_lights.py','character_eye_lighting.py','patch_character_eye_lighting.py','character_face_highlight.py','character_skin_specular.py','attach_skin_surface.py','character_skin_surface.py','attach_skin_additional_lights.py','character_additional_light_state.py','character_punctual_kernel.py','attach_skin_volume.py','character_skin_volume.py'):
        source=Path(__file__).parent/name;shutil.copy2(source,snapshot/name);hashes[name]=hashlib.sha256(source.read_bytes()).hexdigest()
    result['eye_branch_status']=eye_branch_status
    result['hair_branch_status']=hair_branch_status
    result['source_hashes']=hashes;(args.output/'verification.json').write_text(json.dumps(result,ensure_ascii=False,indent=2),encoding='utf-8')


if __name__=='__main__':
    parser=argparse.ArgumentParser(description=__doc__)
    for name in ('blend','output','repository-src','profile','shader-identities'):parser.add_argument('--'+name,type=Path,required=True)
    parser.add_argument('--head-bone',required=True)
    for name in ('face-right','face-up','face-forward','hair-x','hair-y','hair-z'):parser.add_argument('--'+name,nargs=3,type=float,required=True)
    main(parser.parse_args(sys.argv[sys.argv.index('--')+1:]))
