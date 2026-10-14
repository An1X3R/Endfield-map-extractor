"""Current Skin SDF local equations with explicit face/camera/light frames.

The adjustable preview face basis is not asserted to be the native per-instance
object matrix. Ramp arithmetic follows the current compiled fragment. SDF normal
is exposed for future source skin-specular consumers, not substituted for PBR.
"""
from __future__ import annotations
from typing import Callable
import bpy
from character_node_graph import node,math_node
from character_weather_nodes import scalar_mix,combine_vector,vector_math,smoothstep


def build_face_sdf(image: bpy.types.Image, shader_sha256: str, layout: Callable[[bpy.types.NodeTree],None]) -> bpy.types.ShaderNodeTree:
    if image.packed_file is None or image.colorspace_settings.name!="Non-Color":raise ValueError("SDF lightmap requires its verified packed linear image")
    if len(shader_sha256)!=64 or any(c not in "0123456789abcdef" for c in shader_sha256.lower()):raise ValueError("Exact current SDF shader SHA256 is required")
    tree=bpy.data.node_groups.new("EF Native Skin Face SDF - Explicit Basis","ShaderNodeTree")
    tree["shader_sha256"]=shader_sha256;tree["source_formula"]="Current SDF variant fragment: object-space light/mirrored UV/SDF decode/backlight/ramp at excerpt1190-1234"
    for name,kind in (("UV","NodeSocketVector"),("Light Direction","NodeSocketVector"),("Camera Direction","NodeSocketVector"),("World Up","NodeSocketVector"),("Face Right","NodeSocketVector"),("Face Up","NodeSocketVector"),("Face Forward","NodeSocketVector"),("Source Surface Normal","NodeSocketVector"),("SDF Mask G","NodeSocketFloat"),("CP12 X Backlight Mix","NodeSocketFloat"),("CP11 W NdotL Offset","NodeSocketFloat")):
        tree.interface.new_socket(name=name,in_out="INPUT",socket_type=kind)
    entry=node(tree,"NodeGroupInput","Explicit SDF inputs","Face SDF Inputs");output=node(tree,"NodeGroupOutput","Verified local SDF outputs","Face SDF Outputs")
    def scalar(op: str,a: bpy.types.NodeSocket|float,b: bpy.types.NodeSocket|float,label: str) -> bpy.types.NodeSocket:
        return math_node(tree,op,a,b,label,"Face SDF Arithmetic")
    def dot(a: bpy.types.NodeSocket,b: bpy.types.NodeSocket,label: str) -> bpy.types.NodeSocket:
        return vector_math(tree,"DOT_PRODUCT",a,b,label).outputs["Value"]
    light=vector_math(tree,"NORMALIZE",entry.outputs["Light Direction"],(0.,0.,0.),"Normalize preview light").outputs[0]
    right=dot(light,entry.outputs["Face Right"],"Face-local light X")
    forward=dot(light,entry.outputs["Face Forward"],"Face-local light Z")
    horizontal=combine_vector(tree,(right,6.103515625e-5,forward),"Native flattened object light")
    horizontal=vector_math(tree,"NORMALIZE",horizontal,(0.,0.,0.),"Native normalized object light").outputs[0]
    axes=node(tree,"ShaderNodeSeparateXYZ","Face-local normalized light","Face SDF Arithmetic");tree.links.new(horizontal,axes.inputs[0])
    side=scalar("GREATER_THAN",axes.outputs["X"],0.,"Native light side positive")
    uv=node(tree,"ShaderNodeSeparateXYZ","Original source UV0","Face SDF Inputs");tree.links.new(entry.outputs["UV"],uv.inputs[0])
    mirrored=scalar_mix(tree,side,scalar("SUBTRACT",1.,uv.outputs["X"],"Native mirrored U"),uv.outputs["X"],"Native side selects source U")
    texture=node(tree,"ShaderNodeTexImage","Actual source SDF Lightmap","Face SDF Sampling");texture.image=image;texture.extension="EXTEND";texture.interpolation="Linear"
    tree.links.new(combine_vector(tree,(mirrored,uv.outputs["Y"],0.),"Native SDF UV"),texture.inputs["Vector"])
    sample=node(tree,"ShaderNodeSeparateColor","Source SDF RG threshold B normal","Face SDF Sampling");tree.links.new(texture.outputs["Color"],sample.inputs[0])
    threshold=scalar("MULTIPLY",scalar("ADD",sample.outputs["Red"],sample.outputs["Green"],"Source SDF R plus G"),.5,"Source SDF threshold half")
    nx=scalar_mix(tree,side,scalar("SUBTRACT",1.,scalar("MULTIPLY",sample.outputs["Blue"],2.,"Source SDF normal B double"),"Source left-side SDF normal X"),scalar("SUBTRACT",scalar("MULTIPLY",sample.outputs["Blue"],2.,"Source right-side B double"),1.,"Source right-side SDF normal X"),"Native side selects SDF normal X")
    flat=combine_vector(tree,(nx,6.103515625e-5,scalar("SUBTRACT",1.,scalar("ABSOLUTE",nx,0.,"SDF normal X absolute"),"SDF normal Z")),"Native local SDF normal")
    flat=vector_math(tree,"NORMALIZE",flat,(0.,0.,0.),"Normalize source local SDF normal").outputs[0]
    normal_axes=node(tree,"ShaderNodeSeparateXYZ","Source flat SDF normal components","Face SDF Arithmetic");tree.links.new(flat,normal_axes.inputs[0])
    terms=[]
    for axis,basis in (("X","Face Right"),("Y","Face Up"),("Z","Face Forward")):
        value=normal_axes.outputs[axis];terms.append(vector_math(tree,"MULTIPLY",entry.outputs[basis],combine_vector(tree,(value,value,value),"SDF frame coefficient "+axis),"SDF frame term "+axis).outputs[0])
    normal=vector_math(tree,"ADD",vector_math(tree,"ADD",terms[0],terms[1],"SDF frame XY").outputs[0],terms[2],"SDF world normal").outputs[0]
    inverse=scalar("SUBTRACT",1.,entry.outputs["SDF Mask G"],"SDF mask inverse")
    normal=vector_math(tree,"MULTIPLY",normal,combine_vector(tree,(inverse,inverse,inverse),"SDF normal blend coefficient"),"Masked SDF normal").outputs[0]
    surface=vector_math(tree,"MULTIPLY",entry.outputs["Source Surface Normal"],combine_vector(tree,(entry.outputs["SDF Mask G"],)*3,"Surface normal blend coefficient"),"Masked surface normal").outputs[0]
    blended=vector_math(tree,"NORMALIZE",vector_math(tree,"ADD",normal,surface,"Source SDF blended normal").outputs[0],(0.,0.,0.),"Normalize SDF blended normal").outputs[0]
    def flat_world(source: bpy.types.NodeSocket,label: str) -> bpy.types.NodeSocket:
        # Native camera/backlight dot flattens in world space, independently of face rotation.
        up=vector_math(tree,"NORMALIZE",entry.outputs["World Up"],(0.,0.,0.),label+" world up").outputs[0]
        amount=dot(source,up,label+" vertical component")
        vertical=vector_math(tree,"MULTIPLY",up,combine_vector(tree,(amount,amount,amount),label+" vertical scale"),label+" vertical vector").outputs[0]
        flat=vector_math(tree,"SUBTRACT",source,vertical,label+" horizontal world vector").outputs[0]
        return vector_math(tree,"NORMALIZE",flat,(0.,0.,0.),label+" normalized").outputs[0]
    camera=flat_world(entry.outputs["Camera Direction"],"Preview camera face frame")
    flat_light=flat_world(light,"Preview light face frame")
    cam_dot=scalar("MINIMUM",scalar("MAXIMUM",scalar("MULTIPLY",dot(camera,flat_light,"Camera light horizontal dot"),-1.,"Opposing camera light dot"),0.,"Camera light dot low"),1.,"Camera light dot high")
    z=axes.outputs["Z"];negative_z=scalar("MINIMUM",scalar("MAXIMUM",scalar("MULTIPLY",z,-1.,"Negative light Z"),0.,"Negative Z low"),1.,"Negative Z high")
    back=scalar("MULTIPLY",scalar("MULTIPLY",cam_dot,negative_z,"Native backlight factor"),scalar("SUBTRACT",1.,entry.outputs["CP12 X Backlight Mix"],"Native backlight CP12"),"Native backlight weight")
    wrap=scalar("ADD",z,scalar("MULTIPLY",back,scalar("MULTIPLY",.5,scalar("SUBTRACT",1.,scalar("MULTIPLY",z,z,"SDF light Z squared"),"SDF Z complement"),"SDF backlight delta"),"Native backlight adjustment"),"Native wrapped light Z")
    half=scalar("MULTIPLY",wrap,.5,"Native half wrap")
    t=scalar("MINIMUM",scalar("MAXIMUM",scalar("SUBTRACT",.5,half,"SDF transition center"),.0010000000474974513,"SDF transition low"),.9990000128746033,"SDF transition high")
    low=scalar("MAXIMUM",scalar("SUBTRACT",scalar("MULTIPLY",t,2.,"SDF transition double"),1.,"SDF transition lower candidate"),0.,"SDF transition lower")
    high=scalar("MINIMUM",scalar("MULTIPLY",t,2.,"SDF transition upper candidate"),1.,"SDF transition upper")
    polynomial=smoothstep(tree,low,high,threshold,"Native SDF smooth threshold")
    signed=scalar("SUBTRACT",scalar("MULTIPLY",scalar("ABSOLUTE",scalar("ADD",polynomial,scalar("MULTIPLY",scalar("CEIL",half,0.,"SDF half-wrap ceil"),half,"Native positive half-wrap term"),"Native SDF signed response"),0.,"Native SDF absolute response"),2.,"Native SDF response double"),1.,"Native SDF NdotL")
    geometric=scalar("ADD",dot(entry.outputs["Source Surface Normal"],light,"Native source surface NdotL"),scalar("MULTIPLY",entry.outputs["CP11 W NdotL Offset"],entry.outputs["CP12 X Backlight Mix"],"Native NdotL CP offset"),"Native adjusted source NdotL")
    geometric=scalar("MINIMUM",scalar("MAXIMUM",geometric,-1.,"Native NdotL low"),1.,"Native NdotL high")
    selected=scalar_mix(tree,entry.outputs["SDF Mask G"],signed,geometric,"Native mask selects SDF or surface NdotL")
    ramp=scalar("ADD",scalar("MULTIPLY",selected,.5,"Native ramp half"),.5,"Native face diffuse ramp U")
    for name,kind,value in (("Ramp U","NodeSocketFloat",ramp),("SDF NdotL","NodeSocketFloat",signed),("Blended Surface Normal","NodeSocketVector",blended),("Mirrored U","NodeSocketFloat",mirrored)):
        tree.interface.new_socket(name=name,in_out="OUTPUT",socket_type=kind);tree.links.new(value,output.inputs[name])
    layout(tree)
    return tree
