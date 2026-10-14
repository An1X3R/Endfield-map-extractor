"""Compiled Skin main-light GGX; native volume rim terms remain separate."""
from __future__ import annotations
from dataclasses import dataclass
import json
import bpy
from character_node_graph import node, math_node, color_mix
from character_weather_nodes import vector_math, combine_vector
from patch_character_hgrp import by_label


def build_skin_ggx() -> bpy.types.ShaderNodeTree:
    tree=bpy.data.node_groups.new('EF Skin Main Light GGX - Current Source','ShaderNodeTree')
    section='Skin GGX Arithmetic'
    for name in ('Normal','View','Camera','Light','World Up'):
        tree.interface.new_socket(name=name,in_out='INPUT',socket_type='NodeSocketVector')
    for name in ('Cast Shadow','Perceptual Roughness'):
        tree.interface.new_socket(name=name,in_out='INPUT',socket_type='NodeSocketFloat')
    tree.interface.new_socket(name='GGX',in_out='OUTPUT',socket_type='NodeSocketFloat')
    entry=node(tree,'NodeGroupInput','Source skin vectors and roughness',section)
    output=node(tree,'NodeGroupOutput','Current Skin GGX output',section)
    def scalar(op: str,a: bpy.types.NodeSocket|float,b: bpy.types.NodeSocket|float,label: str) -> bpy.types.NodeSocket:
        return math_node(tree,op,a,b,label,section)
    def vector(op: str,a: bpy.types.NodeSocket,b: bpy.types.NodeSocket|tuple[float,float,float],label: str) -> bpy.types.NodeSocket:
        return vector_math(tree,op,a,b,label).outputs[0]
    def dot(a: bpy.types.NodeSocket,b: bpy.types.NodeSocket,label: str) -> bpy.types.NodeSocket:
        return vector_math(tree,'DOT_PRODUCT',a,b,label).outputs['Value']
    def scale(a: bpy.types.NodeSocket,b: bpy.types.NodeSocket,label: str) -> bpy.types.NodeSocket:
        return vector('MULTIPLY',a,combine_vector(tree,(b,b,b),label+' scalar'),label)
    normal,view,camera,light,up=(vector('NORMALIZE',entry.outputs[name],(0.,0.,0.),'Normalize skin '+name) for name in ('Normal','View','Camera','Light','World Up'))
    cast=entry.outputs['Cast Shadow']
    camera_height=dot(camera,up,'Skin camera height')
    light_height=dot(light,up,'Skin light height')
    adjusted_height=scalar('ADD',.5,scalar('MULTIPLY',cast,scalar('SUBTRACT',light_height,.5,'Skin light height delta'),'Skin cast weighted height'),'Skin adjusted camera height')
    adjusted_camera=vector('ADD',camera,scale(up,scalar('SUBTRACT',adjusted_height,camera_height,'Skin camera height replacement'),'Skin vertical replacement'),'Skin modified camera direction')
    adjusted_camera=vector('NORMALIZE',adjusted_camera,(0.,0.,0.),'Normalize modified skin camera')
    two=scalar('ADD',2.,0.,'Skin source two')
    half=vector('ADD',vector('ADD',scale(light,cast,'Skin shadow-weighted light'),scale(adjusted_camera,two,'Skin double camera term'),'Skin light and camera term'),scale(view,scalar('ADD',2.,cast,'Skin view weight'),'Skin weighted view'),'Skin half vector before normalization')
    half=vector('NORMALIZE',half,(0.,0.,0.),'Normalize skin half vector')
    nh=dot(normal,half,'Skin NdotH')
    nv=scalar('MINIMUM',1.,scalar('MAXIMUM',0.,dot(normal,view,'Skin signed NdotV'),'Skin NdotV lower'),'Skin NdotV saturate')
    rough=entry.outputs['Perceptual Roughness']
    alpha=scalar('MAXIMUM',scalar('MULTIPLY',rough,rough,'Skin roughness squared'),.0078125,'Skin roughness source minimum')
    alpha2=scalar('MULTIPLY',alpha,alpha,'Skin alpha squared')
    denominator=scalar('ADD',1.,scalar('MULTIPLY',scalar('SUBTRACT',alpha2,1.,'Skin alpha square minus one'),scalar('MULTIPLY',nh,nh,'Skin NdotH squared'),'Skin GGX denominator term'),'Skin GGX denominator')
    denominator2=scalar('MULTIPLY',denominator,denominator,'Skin GGX denominator squared')
    equal=node(tree,'ShaderNodeMath','Skin exact denominator equality',section);equal.operation='COMPARE';equal.inputs[2].default_value=0.
    tree.links.new(alpha2,equal.inputs[0]);tree.links.new(denominator2,equal.inputs[1])
    ratio=scalar('DIVIDE',alpha2,denominator2,'Skin GGX ratio')
    distribution=scalar('ADD',scalar('MULTIPLY',ratio,scalar('SUBTRACT',1.,equal.outputs[0],'Skin unequal denominator'),'Skin selected ratio'),equal.outputs[0],'Skin source distribution')
    visibility=scalar('DIVIDE',.5,scalar('ADD',scalar('ADD',scalar('MULTIPLY',nv,2.,'Skin double NdotV'),alpha,'Skin visibility roughness'),.0001,'Skin visibility epsilon'),'Skin Kelemen visibility')
    term=scalar('MINIMUM',20.,scalar('MAXIMUM',0.,scalar('SUBTRACT',scalar('MULTIPLY',distribution,visibility,'Skin distribution times visibility'),2.**-14,'Skin half-min subtraction'),'Skin specular lower bound'),'Skin specular upper bound')
    tree.links.new(term,output.inputs['GGX'])
    return tree


@dataclass(frozen=True)
class SkinSpecularReceipt:
    group: str
    specular: float
    metallic: float
    smoothness: float
    source_response: float


def attach_skin_specular(tree: bpy.types.ShaderNodeTree,kernel: bpy.types.ShaderNodeTree) -> SkinSpecularReceipt:
    if tree.get('source_shader')!='HGRP/CharacterNPR_Skin':raise ValueError(f'Expected Skin source group: {tree.name}')
    if tree.get('skin_ggx_connected'):raise ValueError(f'Skin GGX already connected: {tree.name}')
    floats=json.loads(tree['source_floats']);section='Skin Native Main Specular'
    surface=next(n for n in tree.nodes if n.type=='BSDF_PRINCIPLED')
    geo=node(tree,'ShaderNodeNewGeometry','Skin main specular geometry',section)
    normal=surface.inputs['Normal'].links[0].from_socket if surface.inputs['Normal'].is_linked else geo.outputs['Normal']
    camera=node(tree,'ShaderNodeVectorTransform','Skin main specular camera frame',section);camera.vector_type='VECTOR';camera.convert_from='CAMERA';camera.convert_to='WORLD';camera.inputs[0].default_value=(0.,0.,1.)
    shading=node(tree,'ShaderNodeGroup','Compiled Skin main-light GGX',section);shading.node_tree=kernel
    artist=by_label(tree,'Artist global state - not game constants')
    diffuse=by_label(tree,'Verified local HGRP diffuse equations')
    shared=by_label(tree,'Explicit HGRP state and preview inputs')
    for label,value in (('Normal',normal),('View',geo.outputs['Incoming']),('Camera',camera.outputs[0]),('Light',artist.outputs['Direction']),('Cast Shadow',shared.outputs['Cast Shadow'])):
        tree.links.new(value,shading.inputs[label])
    shading.inputs['World Up'].default_value=(0.,0.,1.)
    shading.inputs['Perceptual Roughness'].default_value=1.-floats['_Smoothness']
    specular=math_node(tree,'MULTIPLY',floats['_Specular'],.04,'Skin source dielectric F0',section)
    if tree.get('sdf_scope'):
        specular=math_node(tree,'MULTIPLY',specular,by_label(tree,'Actual SDFMask G surface blend').outputs['Green'],'Skin SDFMask scales F0',section)
    albedo=surface.inputs['Base Color'].links[0].from_socket
    spec_color=color_mix(tree,'MIX',floats['_Metallic'],specular,albedo,'Skin source metallic specular color',section)
    cast_weight=math_node(tree,'ADD',math_node(tree,'MULTIPLY',diffuse.outputs['Cast Blend'],.5,'Skin ambient half cast',section),.5,'Skin ambient cast weight',section)
    ambient=math_node(tree,'MULTIPLY',cast_weight,diffuse.outputs['Ambient Diffuse Intensity'],'Skin source ambient specular scale',section)
    weight=color_mix(tree,'MULTIPLY',1.,diffuse.outputs['Full Diffuse'],ambient,'Skin full diffuse specular weight',section)
    cp13=node(tree,'ShaderNodeValue','Skin CP13 W - constructor initial',section);cp13.outputs[0].default_value=1.
    weight=color_mix(tree,'MULTIPLY',1.,weight,math_node(tree,'MULTIPLY',shading.outputs['GGX'],cp13.outputs[0],'Skin GGX with explicit CP13 W',section),'Skin lit GGX weight',section)
    contribution=color_mix(tree,'MULTIPLY',1.,weight,spec_color,'Skin source main specular contribution',section)
    emission=by_label(tree,'Artist-lit NPR diffuse - shared engines')
    tree.links.new(color_mix(tree,'ADD',1.,emission.inputs['Color'].links[0].from_socket,contribution,'Skin diffuse and source GGX',section),emission.inputs['Color'])
    blend=by_label(tree,'Editable native-sampling response / baseline')
    response=node(tree,'ShaderNodeValue','Skin Source Response Weight',section);response.outputs[0].default_value=1.;tree.links.new(response.outputs[0],blend.inputs[0])
    tree['skin_ggx_connected']=True
    tree['skin_ggx_scope']='Current compiled main-light GGX with source material controls. Host Sun and CP13.w constructor initial; additional skin rim, native adjusted light and runtime volume state remain incomplete.'
    return SkinSpecularReceipt(tree.name,floats['_Specular'],floats['_Metallic'],floats['_Smoothness'],1.)
