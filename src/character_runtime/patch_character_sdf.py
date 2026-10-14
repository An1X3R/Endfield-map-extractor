"""Attach source SDF to HGRP ramps with a live explicit face anchor, dry only."""
from __future__ import annotations
import argparse
import json
import hashlib
from pathlib import Path
import sys
from typing import Callable
import bpy
from mathutils import Matrix,Vector,Quaternion
sys.dont_write_bytecode=True
sys.path.insert(0,str(Path(__file__).resolve().parent))
from character_node_graph import node,math_node

from character_weather_nodes import combine_vector,vector_math
from character_sdf_nodes import build_face_sdf
from patch_character_hgrp import source_keywords,by_label
from patch_character_weather import file_hash
from audit_character_materials import geometry_digest
from verify_character_weather import render_image


def make_face_anchor(name: str, origin: tuple[float,float,float], right: tuple[float,float,float], up: tuple[float,float,float], forward: tuple[float,float,float]) -> bpy.types.Object:
    axes=tuple(Vector(value) for value in (right,up,forward))
    if any(abs(axis.length-1.)>1e-6 for axis in axes) or any(abs(axes[i].dot(axes[j]))>1e-6 for i,j in ((0,1),(0,2),(1,2))) or axes[0].cross(axes[1]).dot(axes[2])<.999999:raise ValueError("Explicit face basis requires orthonormal right/up/forward with positive determinant")
    anchor=bpy.data.objects.new(name,None);anchor.empty_display_type="ARROWS";anchor.empty_display_size=.2;bpy.context.scene.collection.objects.link(anchor)
    matrix=Matrix(tuple(tuple(axes[col][row] for col in range(3)) for row in range(3))).to_4x4();matrix.translation=Vector(origin);anchor.matrix_world=matrix
    anchor["basis_scope"]="Explicit adjustable preview face/object basis, not claimed as captured per-instance HGRP matrix"
    return anchor


def attach_sdf(tree: bpy.types.ShaderNodeTree, anchor: bpy.types.Object, shader_sha256: str, layout: Callable[[bpy.types.NodeTree],None]) -> dict[str,object]:
    if tree.get("source_shader")!="HGRP/CharacterNPR_Skin" or "_SDFLIGHTMAP" not in source_keywords(tree):raise ValueError("SDF attachment requires source Skin _SDFLIGHTMAP keyword")
    samples={item["source_slot"]:item for item in tree.nodes if item.type=="TEX_IMAGE" and "source_slot" in item}
    for slot in ("_SDFLightmap","_SDFMask"):
        if slot not in samples or samples[slot].image is None or samples[slot].image.packed_file is None or samples[slot].image.colorspace_settings.name!="Non-Color":raise ValueError(f"Exact packed linear SDF source texture missing: {tree.name}/{slot}")
    if len(samples["_SDFLightmap"].inputs["Vector"].links)!=1:raise ValueError("Source SDF UV0/ST link must be explicit")
    kernel=build_face_sdf(samples["_SDFLightmap"].image,shader_sha256,layout)
    instance=node(tree,"ShaderNodeGroup","Native Face SDF / Explicit Adjustable Basis","Face SDF Binding");instance.node_tree=kernel
    tree.links.new(samples["_SDFLightmap"].inputs["Vector"].links[0].from_socket,instance.inputs["UV"])
    mask=node(tree,"ShaderNodeSeparateColor","Actual SDFMask G surface blend","Face SDF Binding");tree.links.new(samples["_SDFMask"].outputs["Color"],mask.inputs[0]);tree.links.new(mask.outputs["Green"],instance.inputs["SDF Mask G"])
    bsdfs=tuple(item for item in tree.nodes if item.type=="BSDF_PRINCIPLED")
    if len(bsdfs)!=1:raise ValueError("SDF baseline source surface ambiguous")
    geo=node(tree,"ShaderNodeNewGeometry","SDF actual geometry","Face SDF Binding")
    surface=bsdfs[0].inputs["Normal"].links[0].from_socket if bsdfs[0].inputs["Normal"].is_linked else geo.outputs["Normal"]
    tree.links.new(surface,instance.inputs["Source Surface Normal"])
    instance.inputs["World Up"].default_value=(0.,0.,1.)
    tree.links.new(by_label(tree,"Artist global state - not game constants").outputs["Direction"],instance.inputs["Light Direction"])
    camera=node(tree,"ShaderNodeVectorTransform","SDF explicit Blender camera frame","Face SDF Binding");camera.vector_type="VECTOR";camera.convert_from="CAMERA";camera.convert_to="WORLD";camera.inputs[0].default_value=(0,0,1);tree.links.new(camera.outputs[0],instance.inputs["Camera Direction"])
    for col,label in enumerate(("Face Right","Face Up","Face Forward")):
        basis=node(tree,"ShaderNodeCombineXYZ",label+" live anchor basis","Face SDF Anchor")
        for row in range(3):
            socket=basis.inputs[row];socket.default_value=anchor.matrix_world[row][col]
            # RNA matrix driver paths are column-major; Python Matrix indexing is row-major.
            curve=socket.driver_add("default_value");driver=curve.driver;driver.type="SCRIPTED";variable=driver.variables.new();variable.name="basis_component";variable.type="SINGLE_PROP";variable.targets[0].id=anchor;variable.targets[0].data_path=f"matrix_world[{col}][{row}]";driver.expression="basis_component"
        tree.links.new(basis.outputs[0],instance.inputs[label])
    instance.inputs["CP12 X Backlight Mix"].default_value=0.
    instance.inputs["CP11 W NdotL Offset"].default_value=0.
    ramp=by_label(tree,"Source diffuse ramp RGB and alpha")
    if ramp.image is None:raise ValueError("Source face diffuse ramp missing")
    tree.links.new(combine_vector(tree,(instance.outputs["Ramp U"],.5,0.),"Native SDF-driven diffuse ramp UV"),ramp.inputs["Vector"])
    origin=node(tree,"ShaderNodeCombineXYZ","Live explicit per-object origin","Face SDF Hemisphere")
    for row in range(3):
        socket=origin.inputs[row];socket.default_value=anchor.matrix_world[row][3]
        curve=socket.driver_add("default_value");driver=curve.driver;driver.type="SCRIPTED";variable=driver.variables.new();variable.name="origin_component";variable.type="SINGLE_PROP";variable.targets[0].id=anchor;variable.targets[0].data_path=f"matrix_world[3][{row}]";driver.expression="origin_component"
    radial=vector_math(tree,"SUBTRACT",geo.outputs["Position"],origin.outputs[0],"Native position minus per-object origin").outputs[0]
    def flatten(source: bpy.types.NodeSocket,label: str) -> bpy.types.NodeSocket:
        axes=node(tree,"ShaderNodeSeparateXYZ",label+" components","Face SDF Hemisphere");tree.links.new(source,axes.inputs[0])
        flat=combine_vector(tree,(axes.outputs["X"],axes.outputs["Y"],6.103515625e-5),label+" native horizontal world plane")
        return vector_math(tree,"NORMALIZE",flat,(0.,0.,0.),label+" normalized").outputs[0]
    radial=flatten(radial,"Source radial hemisphere");flat_geometry=flatten(surface,"Source geometry hemisphere")
    inverse=math_node(tree,"SUBTRACT",1.,mask.outputs["Green"],"Hemisphere source mask inverse","Face SDF Hemisphere")
    radial=vector_math(tree,"MULTIPLY",radial,combine_vector(tree,(inverse,inverse,inverse),"Hemisphere radial weight"),"Masked radial hemisphere").outputs[0]
    surface_weight=combine_vector(tree,(mask.outputs["Green"],)*3,"Hemisphere geometry weight")
    flat_geometry=vector_math(tree,"MULTIPLY",flat_geometry,surface_weight,"Masked geometry hemisphere").outputs[0]
    hemisphere=vector_math(tree,"NORMALIZE",vector_math(tree,"ADD",radial,flat_geometry,"Source hemisphere mask blend").outputs[0],(0.,0.,0.),"Native Skin SDF hemisphere").outputs[0]
    tree.links.new(hemisphere,by_label(tree,"Verified local HGRP diffuse equations").inputs["Hemisphere Normal"])
    tree["sdf_scope"]="Current local SDF/ramp and mask-blended radial hemisphere equations; source textures/keywords; explicit preview face basis/object origin and lighting; no captured globals/object matrix; decoded SDF normal exposed for future skin specular"
    layout(tree)
    return {"group":tree.name,"source_identity":json.loads(tree["source_identity"]),"sdf_image":samples["_SDFLightmap"].image.name,"mask_image":samples["_SDFMask"].image.name,"source_packed_sha256":{slot:hashlib.sha256(samples[slot].image.packed_file.data).hexdigest() for slot in ("_SDFLightmap","_SDFMask")},"anchor":anchor.name,"kernel":kernel.name,"ramp_connected":True,"hemisphere_connected":"Current _549 radial/geometry MaskG blend; anchor origin is an explicit preview override; decoded _1434 is a separate specular consumer","CP12X_CP11W_scope":"Explicit zero preview overrides, not runtime capture"}


def set_light(direction: Vector) -> None:
    for label,value in zip(("Light Direction X","Light Direction Y","Light Direction Z"),direction):
        matches=tuple(item for tree in bpy.data.node_groups for item in tree.nodes if item.type=="VALUE" and item.label==label)
        if len(matches)!=1:raise ValueError(f"Shared artist light control ambiguous: {label}")
        matches[0].outputs[0].default_value=value


def freeze_weather() -> None:
    for tree in bpy.data.node_groups:
        for item in tree.nodes:
            if item.type!="VALUE":continue
            if item.label in ("Wetness","Rain Wetness","Submersion Strength","User Override Weight","User Snow Intensity 0-1") or item.label.startswith("Source Effect "):item.outputs[0].default_value=0.
            if item.label in ("Source Water Height","Source Preview Water Height"):item.outputs[0].default_value=-1000.


def main(argv: list[str]) -> None:
    parser=argparse.ArgumentParser(description=__doc__)
    for name in ("blend","shader","scene-source-dir","output"):parser.add_argument("--"+name,type=Path,required=True)
    for name in ("origin","right","up","forward"):parser.add_argument("--"+name,type=float,nargs=3,required=True)
    args=parser.parse_args(argv)
    if args.output.exists():raise FileExistsError(str(args.output))
    sys.path.insert(0,str(args.scene_source_dir));from blender_scene_materials import layout_shader_node_tree
    bpy.ops.wm.open_mainfile(filepath=str(args.blend));freeze_weather()
    names=tuple(obj.name for obj in bpy.data.objects if obj.type=="MESH");before=geometry_digest(names)
    groups=tuple(tree for tree in bpy.data.node_groups if tree.get("source_shader")=="HGRP/CharacterNPR_Skin" and "_SDFLIGHTMAP" in source_keywords(tree))
    if not groups:raise ValueError("Source sample has no Skin SDF material")
    anchor=make_face_anchor("EF Editable Face SDF Anchor",tuple(args.origin),tuple(args.right),tuple(args.up),tuple(args.forward))
    rows=[attach_sdf(tree,anchor,file_hash(args.shader),layout_shader_node_tree) for tree in groups]
    identities={tuple(json.loads(tree["source_identity"])) for tree in groups}
    faces=tuple(obj for obj in bpy.context.scene.objects if obj.type=="MESH" and not obj.hide_render and any(material is not None and any(tuple(identity) in identities for identity in json.loads(material.get("source_material_stack","[]"))) for material in tuple(slot.material for slot in obj.material_slots)))
    if not faces:raise ValueError("No visible source-bound SDF face mesh")
    points=tuple(obj.matrix_world@Vector(corner) for obj in faces for corner in obj.bound_box)
    minimum=Vector(tuple(min(p[axis] for p in points) for axis in range(3)));maximum=Vector(tuple(max(p[axis] for p in points) for axis in range(3)));center=(minimum+maximum)/2;extent=max(maximum-minimum)
    camera=bpy.context.scene.camera
    if camera is None:raise ValueError("Source review camera absent")
    camera.location=center+Vector(args.forward)*3.;camera.rotation_euler=(center-camera.location).to_track_quat('-Z','Y').to_euler();camera.data.type="ORTHO";camera.data.ortho_scale=extent*1.8
    args.output.mkdir(parents=True)
    render_rows=[]
    for name,x,z in (("front",0.,1.),("left_light",-1.,-.35),("right_light",1.,-.35)):
        direction=(Vector(args.forward)*z+Vector(args.right)*x+Vector(args.up)*.2).normalized();set_light(direction)
        path=args.output/(name+"_eevee.png");render_image(path,"BLENDER_EEVEE_NEXT",384,384,"PNG");render_rows.append({"name":name,"light":list(direction),"sha256":file_hash(path)})
    set_light((Vector(args.forward)*-.35+Vector(args.right)+Vector(args.up)*.2).normalized())
    original=anchor.matrix_world.copy();rotated=Quaternion(Vector(args.up),.4363323129985824).to_matrix().to_4x4()@original;rotated.translation=original.translation;anchor.matrix_world=rotated;bpy.context.scene.frame_set(bpy.context.scene.frame_current)
    render_image(args.output/"anchor_rotated_25deg_eevee.png","BLENDER_EEVEE_NEXT",384,384,"PNG")
    anchor.matrix_world=original;bpy.context.scene.frame_set(bpy.context.scene.frame_current)
    set_light((Vector(args.forward)+Vector(args.up)*.2).normalized())
    render_image(args.output/"front_cycles.png","CYCLES",192,192,"PNG")
    target=args.output/"character_sdf_dry.blend";bpy.ops.wm.save_as_mainfile(filepath=str(target));bpy.ops.wm.open_mainfile(filepath=str(target))
    if geometry_digest(names)!=before:raise ValueError("SDF changed source geometry/UV/weights/bind/visibility")
    if any(obj.data.pose_position!="POSE" for obj in bpy.data.objects if obj.type=="ARMATURE"):raise ValueError("Editable POSE was changed")
    report={"format":"EndfieldCharacterFaceSDFPatch/1","status":"render_reopen_geometry_passed","source_blend_sha256":file_hash(args.blend),"source_shader_sha256":file_hash(args.shader),"scripts":{str(p):file_hash(p) for p in (Path(__file__),Path(__file__).with_name("character_sdf_nodes.py"))},"output":str(target),"output_sha256":file_hash(target),"materials":rows,"anchor_basis":{"right":args.right,"up":args.up,"forward":args.forward,"origin":args.origin,"scope":"Explicit editable preview anchor, not captured object matrix"},"light_renders":render_rows,"weather_frozen_zero":True,"geometry_preserved":True,"editable_pose":True,"limits":["Blender camera and artist light direction supplied explicitly; no runtime light loop/global capture","CP12.x and CP11.w are explicit zero overrides","SDF normal decoded but native skin specular still awaiting separate consumer","Blender implicit texture derivatives differ from native SampleLevel0","Current local SDF variant has no NORMALMAP keyword; later combination uses current source surface normal and should be validated separately"]}
    (args.output/"verification.json").write_text(json.dumps(report,indent=2),encoding="utf-8")

if __name__=="__main__":main(sys.argv[sys.argv.index("--")+1:])
