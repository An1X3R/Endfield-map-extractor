"""Patch verified snow equations with explicitly experimental rest-pose varyings."""
from __future__ import annotations
import argparse
import json
from pathlib import Path
import sys
from typing import Callable
import bpy
from mathutils import Vector
sys.dont_write_bytecode = True
sys.path.insert(0, str(Path(__file__).resolve().parent))
from character_node_graph import node, math_node
from character_weather_nodes import make_native_skin_snow, make_native_standard_snow, make_native_standard_wet_film, combine_vector
from patch_character_weather import file_hash, labelled, splice, object_value, number
from audit_character_materials import geometry_digest
from verify_character_weather import render_image


def coordinates(tree: bpy.types.NodeTree, source: bpy.types.NodeSocket, axes: tuple[int, int, int], signs: tuple[float, float, float], label: str) -> bpy.types.NodeSocket:
    split = node(tree, "ShaderNodeSeparateXYZ", label, "Experimental Coordinate Binding")
    tree.links.new(source, split.inputs[0])
    values = tuple(math_node(tree, "MULTIPLY", split.outputs[index], sign, label + str(index), "Experimental Coordinate Binding") for index, sign in zip(axes, signs))
    return combine_vector(tree, values, label + " result")


def rest_attributes(objects: tuple[bpy.types.Object, ...]) -> list[dict[str, object]]:
    rows: list[dict[str, object]] = []
    for obj in objects:
        mesh = obj.data
        for attribute_name, semantic in (("EF_EXPERIMENTAL_REST_UNITY_POSITION", "position"), ("EF_EXPERIMENTAL_REST_UNITY_NORMAL", "normal")):
            if attribute_name in mesh.attributes: raise ValueError(f"Experimental attribute already exists: {obj.name}/{attribute_name}")
            attribute = mesh.attributes.new(attribute_name, "FLOAT_VECTOR", "POINT")
            matrix = obj.matrix_world if semantic == "position" else obj.matrix_world.to_3x3().inverted().transposed()
            for index, vertex in enumerate(mesh.vertices):
                value = matrix @ (vertex.co if semantic == "position" else vertex.normal)
                if semantic == "normal": value.normalize()
                attribute.data[index].vector = (value.x, value.z, -value.y)
        rows.append({"object": obj.name, "vertices": len(mesh.vertices), "binding": "Experimental Blender static rest world coordinates converted to Unity axes; NOT source TEXCOORD mapping"})
    return rows


def patch_group(tree: bpy.types.ShaderNodeTree, kernel: bpy.types.ShaderNodeTree, snow: bpy.types.ShaderNodeTree, orientation: float, tiling: float, layout: Callable[[bpy.types.NodeTree], None]) -> dict[str, object]:
    floats = object_value(json.loads(tree["source_floats"]), "source material floats")
    disable = number(floats["_DisableRainEffectOnMaterial"], "_DisableRainEffectOnMaterial")
    bsdfs = tuple(item for item in tree.nodes if item.type == "BSDF_PRINCIPLED")
    if len(bsdfs) != 1: raise ValueError(f"Source surface is ambiguous: {tree.name}")
    if len(bsdfs[0].inputs["Base Color"].links) != 1 or len(bsdfs[0].inputs["Roughness"].links) != 1: raise ValueError(f"Explicit prior surface channels are required: {tree.name}")
    source_color = bsdfs[0].inputs["Base Color"].links[0].from_socket
    roughness = bsdfs[0].inputs["Roughness"].links[0].from_socket
    shadow_label = "Diagnostic LUT enable; source default on" if number(floats["_UseShadowLutTex"], "_UseShadowLutTex") > 0.5 else "Source ComputeShadowColor"
    wet_nodes = tuple(item for item in tree.nodes if item.label == "Native Standard Wet Film / Experimental Projection")
    shadow = wet_nodes[0].outputs["Shadow Color"] if len(wet_nodes) == 1 else labelled(tree, shadow_label).outputs[0]
    # Preserve the pre-snow shadow lookup, then blend both material color paths.
    shadow_to = tuple(link.to_socket for link in shadow.links)
    color_to = tuple(link.to_socket for link in source_color.links if link.to_node.type == "BSDF_PRINCIPLED" or link.to_node.label.startswith("Approx shadow-to-lit"))
    rough_to = tuple(link.to_socket for link in roughness.links)
    bsdfs = tuple(item for item in tree.nodes if item.type == "BSDF_PRINCIPLED")
    bases = tuple(item for item in tree.nodes if item.get("source_slot") == "_BaseMap")
    if len(bsdfs) != 1 or len(bases) != 1: raise ValueError(f"Exact source surface/base texture is ambiguous: {tree.name}")
    bsdf = bsdfs[0]
    old_metal = bsdf.inputs["Metallic"]
    old_metal_source = old_metal.links[0].from_socket if old_metal.is_linked else float(old_metal.default_value)
    geo = node(tree, "ShaderNodeNewGeometry", "Actual Blender world geometry", "Native Snow Binding")
    instance = node(tree, "ShaderNodeGroup", ("Native Skin Snow / Experimental Projection" if tree["source_shader"] == "HGRP/CharacterNPR_Skin" else "Native Standard Snow / Experimental Projection"), "Native Snow Binding")
    instance.node_tree = kernel
    for name, attribute_name in (("Projection Position", "EF_EXPERIMENTAL_REST_UNITY_POSITION"), ("Projection Normal", "EF_EXPERIMENTAL_REST_UNITY_NORMAL")):
        attribute = node(tree, "ShaderNodeAttribute", attribute_name, "Experimental Coordinate Binding")
        attribute.attribute_name = attribute_name
        tree.links.new(attribute.outputs["Vector"], instance.inputs[name])
    if len(bsdf.inputs["Normal"].links) > 1: raise ValueError("Prior normal frame is ambiguous")
    prior_normal = bsdf.inputs["Normal"].links[0].from_socket if bsdf.inputs["Normal"].is_linked else geo.outputs["Normal"]
    normal = coordinates(tree, prior_normal, (0,2,1), (1.0,1.0,-1.0), "Pre-weather normal frame to Unity")
    tree.links.new(normal, instance.inputs["Geometric Normal"])
    front = math_node(tree, "SUBTRACT", 1.0, geo.outputs["Backfacing"], "Native front-facing sign", "Native Snow Binding")
    tree.links.new(front, instance.inputs["Front Facing"])
    state = node(tree, "ShaderNodeGroup", "Shared explicit snow artist state", "Native Snow Binding")
    state.node_tree = snow
    tree.links.new(state.outputs["Snow Intensity"], instance.inputs["Snow Intensity"])
    instance.inputs["Orientation Flag"].default_value = orientation
    instance.inputs["Global Tiling"].default_value = tiling
    instance.inputs["Disable Weather"].default_value = disable
    tree.links.new(bases[0].outputs["Alpha"], instance.inputs["Base Texture Alpha"])
    for name, source in (("Albedo", source_color), ("Shadow Color", shadow), ("Roughness", roughness)):
        tree.links.new(source, instance.inputs[name])
    if isinstance(old_metal_source, float): instance.inputs["Metallic"].default_value = old_metal_source
    else: tree.links.new(old_metal_source, instance.inputs["Metallic"])
    splice(tree, source_color, instance.outputs["Albedo"], color_to)
    splice(tree, shadow, instance.outputs["Shadow Color"], shadow_to)
    splice(tree, roughness, instance.outputs["Roughness"], rough_to)
    tree.links.new(instance.outputs["Metallic"], bsdf.inputs["Metallic"])
    bpy_normal = coordinates(tree, instance.outputs["Normal"], (0,2,1), (1.0,-1.0,1.0), "Unity snow normal to Blender")
    tree.links.new(bpy_normal, bsdf.inputs["Normal"])
    layout(tree)
    return {"material": tree.name, "source_identity": json.loads(tree["source_identity"]), "source_shader": tree["source_shader"], "source_disable_weather": disable, "projection_verified": False, "opaque_alpha_unchanged": True, "normal_frame_source": {"node": prior_normal.node.name, "socket": prior_normal.name, "rule": "Native Standard _NORMALMAP uses mapped frame; coverage zero preserves the prior BSDF normal"}}


def embedded_keywords() -> dict[tuple[str, int], tuple[str, ...]]:
    result: dict[tuple[str, int], tuple[str, ...]] = {}
    for text in bpy.data.texts:
        if not text.name.startswith("SOURCE MATERIAL "): continue
        row = object_value(json.loads(text.as_string()), "embedded source material")
        identity = object_value(row["object"], "source identity")
        source, path_id, name = identity["source"], identity["path_id"], identity["name"]
        if not isinstance(source,str) or isinstance(path_id,bool) or not isinstance(path_id,int) or not isinstance(name,str): raise ValueError("Embedded source identity is invalid")
        tree = object_value(json.loads(bpy.data.texts["SOURCE TYPETREE " + name + ".json"].as_string()), "embedded TypeTree")
        keywords = tree["m_ValidKeywords"]
        if not isinstance(keywords,list) or any(not isinstance(item,str) for item in keywords): raise ValueError("Embedded material keywords require string array")
        key=(source.casefold(),path_id)
        if key in result: raise ValueError("Duplicate embedded material identity")
        result[key]=tuple(keywords)
    return result


def patch_wet_film(tree: bpy.types.ShaderNodeTree, kernel: bpy.types.ShaderNodeTree, weather: bpy.types.ShaderNodeTree, orientation: float, tiling: float, layout: Callable[[bpy.types.NodeTree], None]) -> None:
    bsdfs=tuple(item for item in tree.nodes if item.type=="BSDF_PRINCIPLED")
    if len(bsdfs)!=1:raise ValueError("Wet-film baseline surface is ambiguous")
    bsdf=bsdfs[0]
    if len(bsdf.inputs["Base Color"].links)!=1 or len(bsdf.inputs["Roughness"].links)!=1:raise ValueError("Wet-film requires explicitly connected baseline color/roughness")
    albedo=bsdf.inputs["Base Color"].links[0].from_socket
    roughness=bsdf.inputs["Roughness"].links[0].from_socket
    floats=object_value(json.loads(tree["source_floats"]),"wet source floats")
    label="Diagnostic LUT enable; source default on" if number(floats["_UseShadowLutTex"],"source LUT toggle")>.5 else "Source ComputeShadowColor"
    shadow=labelled(tree,label).outputs[0]
    color_to=tuple(link.to_socket for link in albedo.links if link.to_node.type=="BSDF_PRINCIPLED" or link.to_node.label.startswith("Approx shadow-to-lit"))
    rough_to=tuple(link.to_socket for link in roughness.links)
    shadow_to=tuple(link.to_socket for link in shadow.links)
    state=node(tree,"ShaderNodeGroup","Explicit native wet-film state","Native Wet Film Binding");state.node_tree=weather
    geo=node(tree,"ShaderNodeNewGeometry","Wet-film actual world geometry","Native Wet Film Binding")
    height=node(tree,"ShaderNodeSeparateXYZ","Wet-film Unity Y is Blender Z","Native Wet Film Binding");tree.links.new(geo.outputs["Position"],height.inputs[0]);tree.links.new(height.outputs["Z"],state.inputs["World Height"])
    wet=node(tree,"ShaderNodeGroup","Native Standard Wet Film / Experimental Projection","Native Wet Film Binding");wet.node_tree=kernel
    for name,attribute_name in (("Projection Position","EF_EXPERIMENTAL_REST_UNITY_POSITION"),("Projection Normal","EF_EXPERIMENTAL_REST_UNITY_NORMAL")):
        attribute=node(tree,"ShaderNodeAttribute",attribute_name,"Experimental Coordinate Binding");attribute.attribute_name=attribute_name;tree.links.new(attribute.outputs["Vector"],wet.inputs[name])
    normal=coordinates(tree,geo.outputs["Normal"],(0,2,1),(1.0,1.0,-1.0),"Wet-film Unity world normal")
    tree.links.new(normal,wet.inputs["Geometric Normal"])
    for name,output in (("Rain Intensity","Native Rain Intensity"),("Water Submersion","Native Water Submersion"),("Global Wetness","Native Global Wetness")):tree.links.new(state.outputs[output],wet.inputs[name])
    for name,value in (("Orientation Flag",orientation),("Global Tiling",tiling),("Disable Weather",number(floats["_DisableRainEffectOnMaterial"],"source disable"))):wet.inputs[name].default_value=value
    for name,value in (("Albedo",albedo),("Shadow Color",shadow),("Roughness",roughness)):tree.links.new(value,wet.inputs[name])
    if bsdf.inputs["Metallic"].is_linked:tree.links.new(bsdf.inputs["Metallic"].links[0].from_socket,wet.inputs["Metallic"])
    else:wet.inputs["Metallic"].default_value=bsdf.inputs["Metallic"].default_value
    splice(tree,albedo,wet.outputs["Albedo"],color_to);splice(tree,roughness,wet.outputs["Roughness"],rough_to);splice(tree,shadow,wet.outputs["Shadow Color"],shadow_to)
    layout(tree)


def main(argv: list[str]) -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    for name in ("blend", "shader", "standard-shader", "texture-manifest", "scene-source-dir", "output"):
        parser.add_argument("--" + name, required=True, type=Path)
    parser.add_argument("--orientation", required=True, type=float, choices=(0.0,1.0))
    parser.add_argument("--tiling", required=True, type=float)
    args = parser.parse_args(argv)
    if args.output.exists(): raise FileExistsError(f"Output exists: {args.output}")
    if not 0.0 < args.tiling < 1000.0: raise ValueError("Explicit global tiling must be finite and positive below 1000")
    rows = json.loads(args.texture_manifest.read_text(encoding="utf-8"))
    if not isinstance(rows, list): raise ValueError("Texture manifest requires an array")
    textures = tuple(row for row in rows if row["slot"] == "characterSnowTexture")
    if len(textures) != 1 or textures[0]["color_space"] != 0: raise ValueError("Exactly one linear source snow texture is required")
    texture_path = args.texture_manifest.parent / textures[0]["artifact"]
    if file_hash(texture_path).casefold() != textures[0]["sha256"].casefold(): raise ValueError("Source snow texture hash differs")
    sys.path.insert(0, str(args.scene_source_dir.resolve()))
    from blender_scene_materials import layout_shader_node_tree
    bpy.ops.wm.open_mainfile(filepath=str(args.blend.resolve()))
    objects = tuple(obj for obj in bpy.data.objects if obj.type == "MESH")
    names = tuple(obj.name for obj in objects)
    before = geometry_digest(names)
    attributes = rest_attributes(objects)
    image = bpy.data.images.load(str(texture_path)); image.colorspace_settings.name = "Non-Color"; image.alpha_mode = "CHANNEL_PACKED"; image.pack()
    kernel = make_native_skin_snow(image, file_hash(args.shader), layout_shader_node_tree)
    snow = bpy.data.node_groups.new("EF Native Snow State - EXPLICIT USER OVERRIDE", "ShaderNodeTree")
    snow.interface.new_socket(name="Snow Intensity", in_out="OUTPUT", socket_type="NodeSocketFloat")
    control = node(snow, "ShaderNodeValue", "User Snow Intensity 0-1", "Explicit Research State"); control.outputs[0].default_value = 0.0
    output = node(snow, "NodeGroupOutput", "User snow override", "Explicit Research State"); snow.links.new(control.outputs[0], output.inputs["Snow Intensity"])
    standard_kernel = make_native_standard_snow(image, file_hash(args.standard_shader), layout_shader_node_tree)
    rain_rows=tuple(row for row in rows if row["slot"]=="characterRainDropTexture")
    if len(rain_rows)!=1 or rain_rows[0]["color_space"]!=0:raise ValueError("Exactly one linear source CharacterRainEffectTex is required")
    rain_path=args.texture_manifest.parent/rain_rows[0]["artifact"]
    if file_hash(rain_path).casefold()!=rain_rows[0]["sha256"].casefold():raise ValueError("Source rain texture changed")
    rain_image=bpy.data.images.load(str(rain_path));rain_image.colorspace_settings.name="Non-Color";rain_image.alpha_mode="CHANNEL_PACKED";rain_image.pack()
    wet_kernel=make_native_standard_wet_film(rain_image,file_hash(args.standard_shader),layout_shader_node_tree)
    weather_groups=tuple(tree for tree in bpy.data.node_groups if "source_provenance" in tree)
    if len(weather_groups)!=1:raise ValueError("Explicit wet-film weather source is ambiguous")
    weather_group=weather_groups[0]
    output=tuple(item for item in weather_group.nodes if item.type=="GROUP_OUTPUT")
    if len(output)!=1:raise ValueError("Weather output is ambiguous")
    for name,label in (("Native Rain Intensity","Explicit effect X"),("Native Water Submersion","Source water-mask intensity"),("Native Global Wetness","Explicit effect Z")):
        source = labelled(weather_group,label).outputs[0]
        existing = tuple(socket for socket in output[0].inputs if socket.name == name)
        if existing:
            if len(existing) != 1 or len(existing[0].links) != 1 or existing[0].links[0].from_socket != source:
                raise ValueError(f'Existing weather output has an incompatible producer: {weather_group.name}/{name}')
        else:
            weather_group.interface.new_socket(name=name,in_out="OUTPUT",socket_type="NodeSocketFloat")
            weather_group.links.new(source,output[0].inputs[name])
    keyword_map=embedded_keywords()
    skipped=[]
    groups = tuple(tree for tree in bpy.data.node_groups if tree.get("source_shader") in ("HGRP/CharacterNPR_Skin", "HGRP/CharacterNPR"))
    if not groups: raise ValueError("Input has no materials of the evidence-supported HGRP/CharacterNPR_Skin family")
    eligible=[]
    for tree in groups:
        identity=json.loads(tree["source_identity"]);key=(identity[0].casefold(),identity[1])
        if key not in keyword_map:raise ValueError("Weather material missing embedded source TypeTree")
        if tree["source_shader"]=="HGRP/CharacterNPR" and "_CHARACTER_FUR" in keyword_map[key]:
            skipped.append({"identity":identity,"reason":"Source _CHARACTER_FUR requires separate native consumer; no-FUR weather excluded"});continue
        eligible.append(tree)
        if tree["source_shader"]=="HGRP/CharacterNPR":patch_wet_film(tree,wet_kernel,weather_group,args.orientation,args.tiling,layout_shader_node_tree)
    groups=tuple(eligible)
    patched = [patch_group(tree, kernel if tree["source_shader"] == "HGRP/CharacterNPR_Skin" else standard_kernel, snow, args.orientation, args.tiling, layout_shader_node_tree) for tree in groups]
    args.output.mkdir(parents=True)
    camera = bpy.context.scene.camera
    if camera is None or camera.data.type != "ORTHO": raise ValueError("Audited input requires its explicit orthographic review camera")
    # The close-up is an explicit review framing override, not an asset change.
    camera.location.z += 0.50
    camera.data.ortho_scale = 0.85
    render_image(args.output / "closeup_snow_0_eevee.png", "BLENDER_EEVEE_NEXT", 384,384,"PNG")
    labelled(weather_group,"User Override Weight").outputs[0].default_value=1.0
    labelled(weather_group,"User Effect X").outputs[0].default_value=1.0
    render_image(args.output/"closeup_rain_1_snow_0_eevee.png","BLENDER_EEVEE_NEXT",384,384,"PNG")
    labelled(weather_group,"User Effect X").outputs[0].default_value=0.0
    labelled(weather_group,"User Effect Y").outputs[0].default_value=1.0
    labelled(weather_group,"User Water Height").outputs[0].default_value=1.25
    render_image(args.output/"closeup_waterline_1_snow_0_eevee.png","BLENDER_EEVEE_NEXT",384,384,"PNG")
    labelled(weather_group,"User Effect Y").outputs[0].default_value=0.0
    control.outputs[0].default_value = 1.0
    render_image(args.output / "closeup_snow_1_eevee.png", "BLENDER_EEVEE_NEXT", 384,384,"PNG")
    render_image(args.output / "closeup_snow_1_cycles.png", "CYCLES", 192,192,"PNG")
    weather = tuple(tree for tree in bpy.data.node_groups if "source_provenance" in tree)
    if len(weather) != 1: raise ValueError("Expected one explicit source/user wetness control")
    labelled(weather[0], "User Override Weight").outputs[0].default_value = 1.0
    for axis, value in zip("XYZW", (1.0,0.0,0.0,0.0)):
        labelled(weather[0], "User Effect " + axis).outputs[0].default_value = value
    render_image(args.output / "closeup_wet_1_snow_1_eevee.png", "BLENDER_EEVEE_NEXT",384,384,"PNG")
    target = args.output / "character_weather_experimental.blend"
    bpy.ops.wm.save_as_mainfile(filepath=str(target))
    bpy.ops.wm.open_mainfile(filepath=str(target))
    if geometry_digest(names) != before: raise ValueError("Weather patch altered geometry/weights/UV/bind/visibility")
    arms = tuple(obj for obj in bpy.data.objects if obj.type == "ARMATURE")
    if not arms or any(obj.data.pose_position != "POSE" for obj in arms): raise ValueError("Editable POSE armature is missing after reopen")
    report = {"format":"EndfieldCharacterNativeSnowExperiment/1", "status":"render_and_reopen_passed", "native_projection_binding_verified":False, "source_weather_runtime_captured":False, "formula_scope":"Current compiled Skin/Standard snow plus Standard static wet-film scalar equations; procedural droplet contribution explicitly zero; other shader families unchanged", "inputs":{str(path):file_hash(path) for path in (args.blend,args.shader,args.standard_shader,args.texture_manifest,texture_path,rain_path)}, "scripts":{str(path):file_hash(path) for path in (Path(__file__).resolve(),Path(__file__).with_name("character_weather_nodes.py"))}, "global_tiling_explicit":args.tiling, "orientation_flag_explicit_user_override":args.orientation,"attributes":attributes,"materials":patched,"skipped_keyword_variants":skipped,"geometry_digest":before,"editable_pose":True,"output":str(target),"output_sha256":file_hash(target),"images":{path.name:file_hash(path) for path in args.output.glob("*.png")},"limits":["Projection inputs are experimental rest coordinates, not proven source TEXCOORD8/TEXCOORD6", "Instance orientation flag is explicit user state, not recovered per-draw data", "Source normals/animation not changed; additional attributes remain fixed in bind pose", "Pre-weather normal-map frame preserved by snow; procedural rain/droplet normal interaction remains incomplete", "Source Ruri silk wetness has invariant dry/wet colors and specular for this sample; no invented darkening", "Skin/Standard no-FUR snow and Standard wet film bound; procedural drops/streak normals, Skin rain, Hair/Eye/special cloth remain incomplete", "Native mip bias and exact keyword-variant choice not reproduced", "Lighting remains existing 06 Blender PBR/NPR approximation"]}
    (args.output / "receipt.json").write_text(json.dumps(report,indent=2),encoding="utf-8")
    print(json.dumps({"status":report["status"],"materials":len(patched),"output":str(target)}))

if __name__ == "__main__":
    main(sys.argv[sys.argv.index("--")+1:])
