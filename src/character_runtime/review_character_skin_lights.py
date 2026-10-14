"""Integrate source Skin color, volume controls and editable host additional lights."""
from __future__ import annotations
import argparse
import hashlib
import json
from pathlib import Path
import shutil
import sys
import bpy
from mathutils import Vector

sys.path.insert(0,str(Path(__file__).resolve().parent))
from audit_character_materials import geometry_digest
from rebuild_character_materials import render_view
from review_character_hgrp import visible_source_group_names,check_eye_response,set_light
from character_additional_light_state import AdditionalLightSettings,build_additional_light_state
from character_punctual_kernel import build_skin_punctual
from attach_skin_additional_lights import attach_skin_additional_lights
from character_node_graph import color_mix
from patch_character_hgrp import by_label


def refresh() -> None:
    """Evaluate current-frame drivers without implicitly advancing body Actions."""
    scene = bpy.context.scene
    scene.frame_set(scene.frame_current, subframe=scene.frame_subframe)
    bpy.context.view_layer.update()


def add_light(name: str,kind: str,position: Vector,target: Vector,color: tuple[float,float,float],energy: float) -> bpy.types.Object:
    if name in bpy.data.objects:raise ValueError('Preview light name already exists: '+name)
    data=bpy.data.lights.new(name,kind);data.color=color;data.energy=energy;data.shadow_soft_size=.05
    light=bpy.data.objects.new(name,data);bpy.context.scene.collection.objects.link(light);light.location=position
    light.rotation_euler=(target-position).to_track_quat('-Z','Y').to_euler()
    if kind=='SPOT':data.spot_size=1.5;data.spot_blend=.5
    light['preview_only']='Editable host demonstration light, not recovered game scene lighting.'
    return light


def main(args: argparse.Namespace) -> None:
    if args.output.exists():raise FileExistsError(args.output)
    input_sha256=hashlib.sha256(args.blend.read_bytes()).hexdigest()
    sys.path.insert(0,str(args.repository_src))
    from blender_scene_materials import layout_shader_node_tree
    from attach_skin_volume import attach_skin_volume,VOLUME_LABEL
    from attach_skin_surface import attach_skin_surface
    bpy.ops.wm.open_mainfile(filepath=str(args.blend),use_scripts=False)
    names=tuple(o.name for o in bpy.data.objects if o.type=='MESH' and 'source_renderer_identity' in o)
    before=geometry_digest(names);visible=visible_source_group_names()
    groups=tuple(t for t in bpy.data.node_groups if t.get('source_shader')=='HGRP/CharacterNPR_Skin' and t.name in visible)
    if not groups:raise ValueError('No visible source Skin groups')
    points=tuple(bpy.data.objects[n].matrix_world@Vector(c) for n in names if not bpy.data.objects[n].hide_render for c in bpy.data.objects[n].bound_box)
    low=Vector(tuple(min(p[a] for p in points) for a in range(3)));high=Vector(tuple(max(p[a] for p in points) for a in range(3)))
    center=(low+high)*.5;extent=max(high-low);close=center.copy();close.z=low.z+(high.z-low.z)*.78
    args.output.mkdir(parents=True)
    renders=[render_view(args.output,'before_skin_lights.png','BLENDER_EEVEE_NEXT',Vector((0.,-1.,.02)),close,extent*.43,640,640,32)]
    point=add_light('EF Fill Point','POINT',close+Vector((-.65,-.7,.08))*extent,close,(.45,.7,1.),120.)
    spot=add_light('EF Fill Spot','SPOT',close+Vector((.65,-.65,.1))*extent,close,(1.,.58,.35),90.)
    lights=(point,spot);states=tuple(build_additional_light_state(light,AdditionalLightSettings(.004,extent*2.5,1.)) for light in lights)
    for light in lights:light.hide_render=True
    kernel=build_skin_punctual('EF Compiled Skin Plain Additional Light')
    for tree in groups:
        base=attach_skin_surface(tree)
        volume=attach_skin_volume(tree,(0.,0.,0.,1.),(8.742278e-8,-1.,0.,.4000000059604645),(0.,0.,0.,1.),(0.,.0010000000474974513,-1.,0.))
        additional=attach_skin_additional_lights(tree,kernel,states)
        composed=color_mix(tree,'ADD',1.,base,volume,'Skin main plus source volume','Skin Final Composition')
        composed=color_mix(tree,'ADD',1.,composed,additional,'Skin with live additional lights','Skin Final Composition')
        tree.links.new(composed,by_label(tree,'Artist-lit NPR diffuse - shared engines').inputs['Color'])
        tree['skin_stage17_scope']='Source local Skin main color/GGX, CP8/14 volume and plain punctual equations. Explicit host camera frame, energy/visibility and preview controls; no native atlas or clustered light-list reconstruction.'
        layout_shader_node_tree(tree)
    for tree in (*states,kernel):layout_shader_node_tree(tree)
    refresh()
    for engine,name in (('BLENDER_EEVEE_NEXT','skin_source_defaults.png'),('CYCLES','skin_source_defaults_cycles.png')):
        renders.append(render_view(args.output,name,engine,Vector((0.,-1.,.02)),close,extent*.43,640,640,32))
    for tree in groups:
        volume=by_label(tree,VOLUME_LABEL);volume.inputs['CP8 Rim RGB'].default_value=(.18,.26,.38,1.)
        if volume.inputs.get('CP14 Face Rim RGB'):volume.inputs['CP14 Face Rim RGB'].default_value=(.025,.035,.045,1.)
        volume['active_preset']='Explicit artist preview override. Native constructor CP8/CP14 RGB are zero.'
    refresh()
    renders.append(render_view(args.output,'skin_volume_preview.png','BLENDER_EEVEE_NEXT',Vector((0.,-1.,.02)),close,extent*.43,640,640,32))
    point.hide_render=False;refresh()
    renders.append(render_view(args.output,'skin_point_blue.png','BLENDER_EEVEE_NEXT',Vector((0.,-1.,.02)),close,extent*.43,640,640,32))
    point.location.x=center.x+extent*.65;point.data.color=(1.,.35,.2);point.data.energy=80.;refresh()
    renders.append(render_view(args.output,'skin_point_moved_red.png','BLENDER_EEVEE_NEXT',Vector((0.,-1.,.02)),close,extent*.43,640,640,32))
    point.hide_render=True;spot.hide_render=False;refresh()
    renders.append(render_view(args.output,'skin_spot_on.png','BLENDER_EEVEE_NEXT',Vector((0.,-1.,.02)),close,extent*.43,640,640,32))
    rotation=spot.rotation_euler.copy();spot.rotation_euler.z+=3.141592653589793;refresh()
    renders.append(render_view(args.output,'skin_spot_away.png','BLENDER_EEVEE_NEXT',Vector((0.,-1.,.02)),close,extent*.43,640,640,32))
    spot.rotation_euler=rotation;spot.data.energy=60.;point.hide_render=False;point.location=close+Vector((-.65,-.7,.08))*extent;point.data.color=(.45,.7,1.);point.data.energy=70.;refresh()
    for engine,name in (('BLENDER_EEVEE_NEXT','skin_lights_eevee.png'),('CYCLES','skin_lights_cycles.png')):
        renders.append(render_view(args.output,name,engine,Vector((0.,-1.,.02)),close,extent*.43,640,640,48))
    renders.append(render_view(args.output,'character_full_eevee.png','BLENDER_EEVEE_NEXT',Vector((0.,-1.,.02)),center,extent,600,900,32))
    if geometry_digest(names)!=before:raise AssertionError('Skin integration changed source geometry')
    target=args.output/'character_skin_additional_lights.blend';bpy.ops.wm.save_as_mainfile(filepath=str(target));bpy.ops.wm.open_mainfile(filepath=str(target),use_scripts=False);refresh()
    if geometry_digest(names)!=before:raise AssertionError('Skin reopen changed source geometry')
    check_eye_response()
    bound=tuple(t for t in bpy.data.node_groups if 'bound_light' in t)
    if len(bound)!=2:raise AssertionError('Additional-light state count changed')
    for tree in bound:
        if tree.animation_data is None or any(not curve.driver.is_valid for curve in tree.animation_data.drivers):raise AssertionError('Invalid reopened additional-light driver: '+tree.name)
    sources=('review_character_skin_lights.py','attach_skin_surface.py','character_skin_surface.py','attach_skin_volume.py','character_skin_volume.py','attach_skin_additional_lights.py','character_additional_light_state.py','character_punctual_kernel.py')
    snapshot=args.output/'source';snapshot.mkdir();hashes={}
    for name in sources:
        source=Path(__file__).parent/name;shutil.copy2(source,snapshot/name);hashes[name]=hashlib.sha256(source.read_bytes()).hexdigest()
    report={'status':'rendered_reopened','input':str(args.blend),'output':str(target),'output_sha256':hashlib.sha256(target.read_bytes()).hexdigest(),'geometry_preserved':True,'renderers':len(names),'skin_groups':[t.name for t in bpy.data.node_groups if t.get('skin_stage17_scope')],'renders':renders,'source_hashes':hashes,'driver_reopen_scripts':False,'weather_frozen':True,'preview_lighting':'Explicit editable Blender Point and Spot conversion, CP8/14 artist overrides; constructor-default render also included.','limitations':['Native screen shadows, clustered culling, cookies and emitter-shape integration are not reproduced','Final blended game volume values and captured camera frame unavailable','Only plain additional-light material mode integrated','Research-stage export; production character-export and UI integration pending']}
    report['input_sha256']=input_sha256
    (args.output/'verification.json').write_text(json.dumps(report,indent=2),encoding='utf-8')


if __name__=='__main__':
    parser=argparse.ArgumentParser(description=__doc__)
    for name in ('blend','output','repository-src'):parser.add_argument('--'+name,type=Path,required=True)
    main(parser.parse_args(sys.argv[sys.argv.index('--')+1:]))
