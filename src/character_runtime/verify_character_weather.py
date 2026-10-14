"""Bounded real-engine checks for explicit weather masks and editable silk colors."""
from __future__ import annotations
import argparse
import json
from pathlib import Path
import sys

import bpy
from mathutils import Vector

sys.path.insert(0, str(Path(__file__).resolve().parent))
from character_weather_nodes import WeatherUniforms, WeatherOverride, make_weather_controls, evaluate_ruri_wetness, pack_native_preview_word, unpack_native_weather_word
from patch_character_weather import file_hash, labelled


def no_layout(tree: bpy.types.NodeTree) -> None:
    """Numeric fixtures need no UI layout; material sample retains real layout."""


def render_image(path: Path, engine: str, width: int, height: int, file_format: str) -> None:
    scene = bpy.context.scene
    scene.render.engine = engine
    scene.render.resolution_x, scene.render.resolution_y = width, height
    scene.render.resolution_percentage = 100
    scene.render.image_settings.file_format = file_format
    scene.render.image_settings.color_depth = "32" if file_format == "OPEN_EXR" else "8"
    scene.render.image_settings.color_mode = "RGBA"
    scene.render.filepath = str(path)
    if engine == "CYCLES":
        scene.cycles.samples = 8
        scene.cycles.use_denoising = False
    else:
        scene.eevee.taa_render_samples = 8
    bpy.ops.render.render(write_still=True)
    if not path.is_file():
        raise RuntimeError(f"Render output is missing: {path}")


def numerical_mask_checks(output: Path) -> list[dict[str, object]]:
    bpy.ops.wm.read_factory_settings(use_empty=True)
    scene = bpy.context.scene
    scene.view_settings.view_transform = "Raw"
    scene.world = bpy.data.worlds.new("Numeric black world")
    scene.world.use_nodes = True
    scene.world.node_tree.nodes["Background"].inputs["Strength"].default_value = 0.0
    uniforms = WeatherUniforms((0.0,0.0,0.0,0.0), -1000.0, 0.0, -1000.0, "Explicit neutral numeric fixture; not a live game capture")
    override = WeatherOverride(1.0, (0.0,1.0,0.0,0.0), 0.5)
    group = make_weather_controls(uniforms, override, no_layout)
    heights = (-1.0, 0.0, 0.5, 1.0, 2.0)
    for index, value in enumerate(heights):
        bpy.ops.mesh.primitive_plane_add(size=1.0, location=(index-2.0,0.0,0.0))
        material = bpy.data.materials.new("Height fixture " + str(value))
        material.use_nodes = True
        tree = material.node_tree
        tree.nodes.clear()
        instance = tree.nodes.new("ShaderNodeGroup"); instance.node_tree = group
        instance.inputs["World Height"].default_value = value
        rgb = tree.nodes.new("ShaderNodeCombineColor")
        tree.links.new(instance.outputs["Height Mask"],rgb.inputs[0]); tree.links.new(instance.outputs["Wetness"],rgb.inputs[1])
        emission = tree.nodes.new("ShaderNodeEmission"); tree.links.new(rgb.outputs[0],emission.inputs["Color"])
        result = tree.nodes.new("ShaderNodeOutputMaterial"); tree.links.new(emission.outputs[0],result.inputs["Surface"])
        bpy.context.object.data.materials.append(material)
    data = bpy.data.cameras.new("Numeric camera"); camera = bpy.data.objects.new("Numeric camera",data)
    scene.collection.objects.link(camera); scene.camera=camera
    camera.location=(0.0,0.0,5.0); camera.rotation_euler=(0.0,0.0,0.0)
    data.type="ORTHO"; data.ortho_scale=5.0
    rows=[]
    for engine in ("CYCLES","BLENDER_EEVEE_NEXT"):
        path=output/("mask_"+engine.lower()+".exr")
        render_image(path,engine,100,20,"OPEN_EXR")
        image=bpy.data.images.load(str(path)); pixels=tuple(image.pixels)
        errors=[]
        for index,height in enumerate(heights):
            start=(10*100+10+index*20)*4
            actual=pixels[start:start+2]
            expected=evaluate_ruri_wetness(uniforms,override,height)
            error=max(abs(a-e) for a,e in zip(actual,expected))
            if error>0.002:
                raise RuntimeError(f"Weather mask differs from source equation: engine={engine}, height={height}, actual={actual}, expected={expected}, error={error}")
            errors.append({"height":height,"actual":actual,"expected":expected,"max_error":error})
        rows.append({"engine":engine,"path":str(path),"sha256":file_hash(path),"samples":errors})
        bpy.data.images.remove(image)
    words=[]
    for rain,snow in ((0.0,0.0),(1.0,0.0),(0.0,1.0),(0.5,0.5),(1.0,1.0)):
        word=pack_native_preview_word(rain,snow)
        decoded=unpack_native_weather_word(word)
        if decoded[1]!=1.0 or decoded[2]!=0.0 or abs(decoded[0]-rain)>1/255 or abs(decoded[3]-snow)>1/255:
            raise RuntimeError("Native byte pack failed")
        words.append({"rain":rain,"snow":snow,"word_hex":f"{word:08x}","decoded":decoded})
    (output/"native_pack_checks.json").write_text(json.dumps(words,indent=2),encoding="utf-8")
    return rows


def material_checks(blend: Path, output: Path) -> dict[str, object]:
    bpy.ops.wm.open_mainfile(filepath=str(blend.resolve()))
    camera=bpy.context.scene.camera
    if camera is None: raise ValueError("Audited material sample has no camera")
    armatures=tuple(obj for obj in bpy.data.objects if obj.type=="ARMATURE")
    if not armatures or any(obj.data.pose_position!="POSE" for obj in armatures):
        raise ValueError("Weather patch must retain editable POSE armatures")
    render_image(output/"source_neutral_eevee.png","BLENDER_EEVEE_NEXT",192,256,"PNG")
    controls=tuple(tree for tree in bpy.data.node_groups if "source_provenance" in tree)
    if len(controls)!=1: raise ValueError("Shared weather control is ambiguous")
    weather=controls[0]
    labelled(weather,"User Override Weight").outputs[0].default_value=1.0
    for axis,value in zip("XYZW",(1.0,0.0,0.0,0.0)):
        labelled(weather,"User Effect "+axis).outputs[0].default_value=value
    render_image(output/"source_wet_eevee.png","BLENDER_EEVEE_NEXT",192,256,"PNG")
    silk_nodes=tuple(item for tree in bpy.data.node_groups for item in tree.nodes if item.type=="GROUP" and item.node_tree and "source_formula" in item.node_tree and item.node_tree.name.startswith("EF Silk Surface"))
    overrides=[]
    for item in silk_nodes:
        original=tuple(item.inputs["_SilkStockingsWetColor"].default_value)
        chosen=(0.35,0.55,0.75,1.0)
        item.inputs["_SilkStockingsWetColor"].default_value=chosen
        overrides.append({"group":item.id_data.name,"original":original,"user_override":chosen,"reason":"Explicit artist override used only for sensitivity validation; no game-native wet tint claim"})
    if overrides:
        render_image(output/"user_wet_tint_eevee.png","BLENDER_EEVEE_NEXT",192,256,"PNG")
        render_image(output/"user_wet_tint_cycles.png","CYCLES",128,192,"PNG")
    return {"editable_pose":True,"silk_overrides":overrides,"renders":{path.name:file_hash(path) for path in output.glob("*.png")},"source_parameters_modified_in_saved_blend":False}


def main(argv: list[str]) -> None:
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--blend",required=True,type=Path)
    parser.add_argument("--output",required=True,type=Path)
    args=parser.parse_args(argv)
    if args.output.exists(): raise FileExistsError(f"Output exists: {args.output}")
    args.output.mkdir(parents=True)
    numerical=numerical_mask_checks(args.output)
    material=material_checks(args.blend,args.output)
    report={"format":"EndfieldWeatherNodeVerification/1","status":"passed","input_blend":str(args.blend.resolve()),"input_sha256":file_hash(args.blend),"module_sha256":file_hash(Path(__file__).with_name("character_weather_nodes.py")),"verification_script_sha256":file_hash(Path(__file__)),"numeric_masks":numerical,"material":material,"snow_coverage_implemented":False}
    (args.output/"verification.json").write_text(json.dumps(report,indent=2),encoding="utf-8")


if __name__=="__main__":
    main(sys.argv[sys.argv.index("--")+1:])
