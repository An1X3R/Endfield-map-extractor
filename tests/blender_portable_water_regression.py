"""Verify a normally prepared portable-water build and render its saved optics."""
from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
import sys

import bmesh
import bpy


def digest(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def require_link(socket: bpy.types.NodeSocket, source: bpy.types.NodeSocket) -> None:
    if len(socket.links) != 1 or socket.links[0].from_socket != source:
        raise AssertionError('Portable water socket lost its live source: '+socket.name)


def verify_water(scene: bpy.types.Scene, entity: int, roughness: float) -> dict[str, object]:
    waters = [obj for obj in scene.objects if obj.type == 'MESH'
              and obj.get('endfield_category') == 'water' and int(obj['entity_id']) == entity]
    if len(waters) != 1:
        raise AssertionError('Expected one real prepared water surface')
    water = waters[0]
    material = water.data.materials[0]
    tree = material.node_tree
    if material.get('endfield_surface_contract') != 'EndfieldPortableWater/2':
        raise AssertionError('Portable surface contract changed')
    core = tree.nodes['EF_Water_Shared_Core']
    settings = tree.nodes['EF_Water_Shared_Settings']
    distortion = tree.nodes['EF_Water_Source_Screen_Distortion']
    if core.node_tree.get('endfield_contract') != 'PortableWaterOptics/6':
        raise AssertionError('Independent reflection/transmission core is missing')
    if distortion.node_tree.get('endfield_contract') != 'HostWaterScreenDistortion/2':
        raise AssertionError('Current source-direction group is missing')
    wave = core.inputs['Normal'].links[0].from_socket
    require_link(distortion.inputs['Wave Normal'], wave)
    require_link(core.inputs['Transmission Normal'], distortion.outputs['Transmission Normal'])
    require_link(distortion.inputs['Host IOR'], settings.outputs['IOR'])
    require_link(distortion.inputs['Depth'], core.inputs['Depth'].links[0].from_socket)
    entry = next(node for node in core.node_tree.nodes if node.type == 'GROUP_INPUT')
    output = next(node for node in core.node_tree.nodes if node.type == 'GROUP_OUTPUT' and node.is_active_output)
    refractions = [node for node in core.node_tree.nodes if node.type == 'BSDF_REFRACTION']
    if len(refractions) != 2:
        raise AssertionError('Expected separate Cycles and Eevee transmission closures')
    fresnels = [node for node in core.node_tree.nodes if node.type == 'FRESNEL']
    if len(fresnels) != 1:
        raise AssertionError('Expected shared original-wave Fresnel')
    require_link(fresnels[0].inputs['Normal'], entry.outputs['Normal'])
    for surface in ('Surface', 'Eevee Surface'):
        boundary = output.inputs[surface].links[0].from_node
        require_link(boundary.inputs[0], fresnels[0].outputs[0])
        reflection = boundary.inputs[2].links[0].from_node
        require_link(reflection.inputs['Normal'], entry.outputs['Normal'])
        refraction = boundary.inputs[1].links[0].from_node
        if refraction not in refractions:
            raise AssertionError('Output does not actually consume independent refraction')
        require_link(refraction.inputs['Normal'], entry.outputs['Transmission Normal'])
        require_link(refraction.inputs['IOR'], entry.outputs['IOR'])
        require_link(refraction.inputs['Roughness'], entry.outputs['Roughness'])
    eevee_refraction = next(node for node in refractions if node.label == 'Eevee Surface independent transmission')
    attenuation = next(node for node in core.node_tree.nodes if node.label == 'Eevee spectral extinction only; no in-scattering')
    require_link(eevee_refraction.inputs['Color'], attenuation.outputs[0])
    settings_output = next(node for node in settings.node_tree.nodes if node.type == 'GROUP_OUTPUT')
    values = {name: settings_output.inputs[name].links[0].from_socket for name in
              ('IOR','Roughness','Attenuation Per Meter','Scattering Per Meter','Deep Color')}
    if abs(values['Roughness'].default_value-roughness)>1e-7:
        raise AssertionError('Source roughness changed')
    if abs(values['IOR'].default_value-1.333)>1e-6:
        raise AssertionError('Physical water IOR was changed to conceal distortion')
    original_ior=values['IOR'].default_value
    values['IOR'].default_value=1.4
    require_link(core.inputs['IOR'],settings.outputs['IOR'])
    require_link(distortion.inputs['Host IOR'],settings.outputs['IOR'])
    values['IOR'].default_value=original_ior
    volumes = [obj for obj in scene.objects if obj.get('endfield_water_volume_source') == water.name]
    if len(volumes)!=1 or volumes[0].hide_render:
        raise AssertionError('Cycles calibrated medium is missing or disabled')
    volume=volumes[0]
    mesh=bmesh.new()
    try:
        mesh.from_mesh(volume.data)
        if any(not edge.is_manifold for edge in mesh.edges) or mesh.calc_volume(signed=False)<=0:
            raise AssertionError('Portable water medium is not a closed positive volume')
    finally:
        mesh.free()
    volume_tree=volume.data.materials[0].node_tree
    volume_core=next(node for node in volume_tree.nodes if node.type=='GROUP'
                     and node.node_tree==core.node_tree)
    volume_settings=next(node for node in volume_tree.nodes if node.type=='GROUP'
                         and node.node_tree==settings.node_tree)
    for name in values:
        require_link(volume_core.inputs[name],volume_settings.outputs[name])
    volume_outputs={node.target:node for node in volume_tree.nodes if node.type=='OUTPUT_MATERIAL'}
    require_link(volume_outputs['CYCLES'].inputs['Volume'],volume_core.outputs['Volume'])
    if volume_outputs['EEVEE'].inputs['Volume'].is_linked:
        raise AssertionError('Eevee applied both boundary extinction and medium')
    if material.get('endfield_water_foam_status')=='source_mask_host_surface':
        for node in tree.nodes:
            if node.type=='OUTPUT_MATERIAL' and node.inputs['Surface'].links[0].from_node.name!='EF_Water_Foam_Mix_'+node.target:
                raise AssertionError('Source foam stopped consuming the portable surface')
    return {'entity_id':entity,'core_contract':core.node_tree['endfield_contract'],
            'distortion_contract':distortion.node_tree['endfield_contract'],
            'reflection_and_fresnel_keep_original_wave':True,
            'two_refractions_consume_corrected_transmission':True,'live_ior_shared_with_volume':True,
            'ior':original_ior,'source_roughness':roughness,
            'attenuation_per_meter':values['Attenuation Per Meter'].default_value,
            'scattering_per_meter':values['Scattering Per Meter'].default_value,
            'deep_color':list(values['Deep Color'].default_value),
            'cycles_closed_volume_enabled':True,'eevee_boundary_extinction_not_double_applied':True,
            'foam_status':material['endfield_water_foam_status'],
            'source_distortion':distortion.inputs['Source Distortion'].default_value,
            'host_perspective':distortion.inputs['Host Perspective'].default_value}


parser=argparse.ArgumentParser()
for name in ('source','plan','camera-reference','output'):
    parser.add_argument('--'+name,type=Path,required=True)
parser.add_argument('--entity',type=int,required=True)
args=parser.parse_args(sys.argv[sys.argv.index('--')+1:])
args.output.mkdir(parents=True,exist_ok=False)
source_hash=digest(args.source)
reference_hash=digest(args.camera_reference)
plan_hash=digest(args.plan)
plan=json.loads(args.plan.read_text(encoding='utf-8-sig'))
if 'water_transport_lighting' in plan:
    raise AssertionError('Portable regression requires the default no-source-transport plan')
config=json.loads(next(row for row in plan['water_materials'] if row['entity_id']==args.entity)['config_source_json'])
bpy.ops.wm.open_mainfile(filepath=str(args.camera_reference),use_scripts=False,load_ui=False)
reference_camera=bpy.context.scene.camera
camera_matrix=reference_camera.matrix_world.copy()
camera_values={name:getattr(reference_camera.data,name) for name in
               ('type','lens','clip_start','clip_end','sensor_width','sensor_height','sensor_fit')}
bpy.ops.wm.open_mainfile(filepath=str(args.source),use_scripts=False,load_ui=False)
bpy.context.preferences.filepaths.save_version=0
scene=bpy.context.scene
first=verify_water(scene,args.entity,config['Appearance']['Roughness'])
camera=bpy.data.objects.new('Portable_Water_Review',bpy.data.cameras.new('Portable_Water_Review'))
scene.collection.objects.link(camera)
camera.matrix_world=camera_matrix
for name,value in camera_values.items():
    setattr(camera.data,name,value)
if camera.data.type!='PERSP' or first['host_perspective']!=1:
    raise AssertionError('Selected camera and explicit projection disagree')
scene.camera=camera
scene.frame_set(1)
scene.render.resolution_x,scene.render.resolution_y,scene.render.resolution_percentage=640,480,100
scene.render.image_settings.file_format='PNG'
scene.cycles.samples=64
scene.cycles.use_denoising=True
scene.cycles.use_animated_seed=False
scene.cycles.seed=0
scene.eevee.taa_render_samples=64
review=args.output/'portable_water_review.blend'
bpy.ops.wm.save_as_mainfile(filepath=str(review))
bpy.ops.wm.open_mainfile(filepath=str(review),use_scripts=False,load_ui=False)
scene=bpy.context.scene
saved=verify_water(scene,args.entity,config['Appearance']['Roughness'])
if saved!=first:
    raise AssertionError('Portable graph/settings changed on scripts-disabled reopen')
rows=[]
for engine in ('BLENDER_EEVEE_NEXT','CYCLES'):
    scene.render.engine=engine
    scene.render.filepath=str(args.output/(engine.lower()+'.png'))
    bpy.ops.render.render(write_still=True)
    path=Path(scene.render.filepath)
    rows.append({'engine':engine,'image':str(path),'sha256':digest(path)})
if digest(args.source)!=source_hash or digest(args.camera_reference)!=reference_hash or digest(args.plan)!=plan_hash:
    raise AssertionError('Readonly normal build/plan/reference changed')
report={'status':'default_portable_saved_reopened_live_graph_verified_two_engines_rendered',
        'source':str(args.source),'source_sha256':source_hash,'plan':str(args.plan),'plan_sha256':plan_hash,
        'camera_reference':str(args.camera_reference),'camera_reference_sha256':reference_hash,
        'review_blend':str(review),'review_sha256':digest(review),'graph':saved,'renders':rows,
        'sources_unchanged':True,'scope':'Default calibrated medium optics with source roughness, source-style above-water local-bottom distortion and retained source foam. No source-transport environment input, native screen/depth rejection or underwater parity claim. Explicit scalar Fresnel plus Glossy/Refraction replaces the inseparable Principled closure; rough microfacet energy is a host approximation.'}
(args.output/'receipt.json').write_text(json.dumps(report,indent=2),encoding='utf-8')
print(json.dumps({'status':report['status'],'artifact':str(review)}))
