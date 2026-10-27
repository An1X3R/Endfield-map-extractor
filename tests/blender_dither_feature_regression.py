"""Check real Dither disturbance/mask textures and compiled-keyword selection in Blender."""
from __future__ import annotations

import argparse
from array import array
import json
from pathlib import Path
import sys

import bpy

sys.dont_write_bytecode = True
root = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(root / "src"), str(root / "tests")]

from blender_character_vfx_disturbance_regression import _close, _value
from blender_scene_dither_materials import SHADER, make_dither_mesh_material, validate_dither_mesh
from endfield_scene_export_plan import load_scene_export_plan
from endfield_scene_material_contract import MaterialRecord
from endfield_scene_material_handoff import load_plan_material_catalog

Texel = tuple[float, float, float, float]
MATERIAL = "M_fxmap_indie_dg013_inkroad_01_fj_02_d"


def _source_texels(tree: bpy.types.NodeTree, record: MaterialRecord) -> tuple[dict[str, Texel], dict[str, object]]:
    texels: dict[str, Texel] = {}
    evidence: dict[str, object] = {}
    for name in ("_MainTex", "_DisturbTex1", "_MaskTex"):
        node = tree.nodes["Source " + name]
        image = node.image
        if image is None:
            raise AssertionError(f"Source texture is not loaded: {name}")
        width, height = image.size
        if width <= 0 or height <= 0:
            raise AssertionError(f"Source texture dimensions are invalid: {name}, size={image.size[:]}")
        pixels = array("f", [0.]) * (width * height * 4)
        image.pixels.foreach_get(pixels)
        index = min(range(width * height), key=lambda index: abs(pixels[4 * index] - .6))
        x, y = index % width, index // width
        rgba = tuple(float(pixels[4 * index + channel]) for channel in range(4))
        if image.colorspace_settings.name == "sRGB":
            rgba = tuple((value / 12.92 if value <= .04045 else ((value + .055) / 1.055) ** 2.4)
                         if channel < 3 else value for channel, value in enumerate(rgba))
        texels[node.name] = rgba
        reference, = (slot["Texture"] for slot in record["Textures"] if slot["Property"] == name)
        if reference is None:
            raise AssertionError(f"Source texture identity is missing: {name}")
        evidence[name] = {"file": image.filepath, "dimensions": [width, height],
                          "colorspace": image.colorspace_settings.name, "linear_rgba": list(rgba),
                          "pixel": [x, y], "identity": [reference["Source"], reference["PathId"]]}
        coordinate = tree.nodes.new("ShaderNodeCombineXYZ")
        coordinate.name = "Test Pixel Center " + name
        coordinate.inputs[0].default_value = (x + .5) / width
        coordinate.inputs[1].default_value = (y + .5) / height
        tree.links.new(coordinate.outputs[0], node.inputs["Vector"])
    return texels, evidence


def _quad(material: bpy.types.Material) -> None:
    mesh = bpy.data.meshes.new("Dither Regression Quad")
    mesh.from_pydata(((-1., -1., 0.), (1., -1., 0.), (1., 1., 0.), (-1., 1., 0.)), (), ((0, 1, 2, 3),))
    mesh.materials.append(material)
    uv = mesh.uv_layers.new(name="UVMap")
    for index, point in enumerate(((0., 0.), (1., 0.), (1., 1.), (0., 1.))):
        uv.data[index].uv = point
    obj = bpy.data.objects.new(mesh.name, mesh)
    bpy.context.scene.collection.objects.link(obj)


def _render(path: Path) -> tuple[float, float, float, float]:
    scene = bpy.context.scene
    scene.render.filepath = str(path)
    bpy.ops.render.render(write_still=True)
    image = bpy.data.images.load(str(path), check_existing=False)
    width, height = image.size
    offset = ((height // 2) * width + width // 2) * 4
    rgba = tuple(float(value) for value in image.pixels[offset:offset + 4])
    bpy.data.images.remove(image)
    return rgba


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--catalog", type=Path, required=True, help="Scene plan containing the selected material catalog")
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args(sys.argv[sys.argv.index("--") + 1:])
    args.output.mkdir(parents=True, exist_ok=False)
    bpy.ops.wm.read_factory_settings(use_empty=True)
    records, textures = load_plan_material_catalog(load_scene_export_plan(args.catalog))
    record, = (record for record in records.values() if record["Name"] == MATERIAL)
    original = json.dumps(record, sort_keys=True)
    material = make_dither_mesh_material(record, textures, 2., 0)
    tree = material.node_tree
    if tree.nodes.get("Source _DisturbTex2") or tree.nodes.get("Source _BlendTex"):
        raise AssertionError("Inactive serialized controls enabled an absent compiled branch")
    if tree.nodes["Dither Source Tint RGB"].inputs[0].links[0].from_node.name != "_MainTex White RGB":
        raise AssertionError("Main red-as-alpha did not retain native white HGBuffer RGB")
    expected_inputs = {
        "Dither Main Disturbance": record["Floats"]["_MainTexUseDisturb"],
        "Dither Mask Disturbance": record["Floats"]["_MaskTexUseDisturb"],
    }
    for name, strength in expected_inputs.items():
        _close(tree.nodes[name].inputs[3].default_value, strength, name)
        if tree.nodes[name].inputs[0].links[0].from_node.name != "Dither Disturb 1 UV Offset":
            raise AssertionError(f"Compiled single disturbance was not wired: {name}")
    _close(tree.nodes["Dither Disturb Source UV0 Weight"].inputs[3].default_value,
           record["Colors"]["_DisturbUVWeights1"][0], "source UV coefficient before time/rotation/ST")

    baseline: MaterialRecord = {**record, "RenderState": {**record["RenderState"], "ValidKeywords": []}}
    baseline_tree = make_dither_mesh_material(baseline, textures, 2., 0).node_tree
    if any(node.name.startswith("Source _Disturb") or node.name == "Source _MaskTex" for node in baseline_tree.nodes):
        raise AssertionError("No-keyword variant sampled an inactive feature")
    rejected = []
    ink_control: MaterialRecord = {**record, "RenderState": {**record["RenderState"],
        "ValidKeywords": [*record["RenderState"]["ValidKeywords"], "_INK_DISTORT"]}}
    for row in (*records.values(), ink_control):
        if row["Shader"].get("Name") == SHADER and "_INK_DISTORT" in row["RenderState"]["ValidKeywords"]:
            try:
                validate_dither_mesh(row, 0)
            except NotImplementedError as error:
                rejected.append({"material": row["Name"], "reason": str(error), "test_control": row is ink_control})
            else:
                raise AssertionError(f"Runtime ink variant escaped validation: {row['Name']}")
    if not rejected:
        raise AssertionError("Runtime-ink rejection was not exercised")

    scene = bpy.context.scene
    scene.render.engine = "CYCLES"
    scene.cycles.device, scene.cycles.samples, scene.cycles.use_denoising = "CPU", 1, False
    scene.render.resolution_x = scene.render.resolution_y = 128
    scene.render.resolution_percentage = 100
    scene.render.image_settings.file_format = "OPEN_EXR"
    scene.render.image_settings.color_mode, scene.render.image_settings.color_depth = "RGBA", "32"
    scene.render.film_transparent = True
    camera_data = bpy.data.cameras.new("Dither Regression Camera")
    camera = bpy.data.objects.new(camera_data.name, camera_data)
    scene.collection.objects.link(camera)
    camera_data.type, camera_data.ortho_scale, camera.location = "ORTHO", 2.1, (0., 0., 4.)
    scene.camera = camera
    _quad(material)
    for node in tree.nodes:
        if node.type == "TEX_IMAGE" and node.image is not None:
            node.image.pack()
    _render(args.output / "source_preview.exr")
    scene.render.image_settings.file_format = "PNG"
    bpy.data.images["Render Result"].save_render(str(args.output / "source_preview.png"))
    scene.render.image_settings.file_format = "OPEN_EXR"
    probe = material.copy()
    probe_tree = probe.node_tree
    texels, texture_evidence = _source_texels(probe_tree, record)
    (args.output / "texture_samples.json").write_text(json.dumps(texture_evidence, indent=2), encoding="utf-8")
    floats = record["Floats"]
    red = texels["Source _DisturbTex1"][0] * (1. + floats["_Bi_Disturb"]) - floats["_Bi_Disturb"]
    expected = (red * floats["_DisturbUIntensity1"], red * floats["_DisturbVIntensity1"],
                texels["Source _MainTex"][0] * texels["Source _MaskTex"][0] *
                record["Colors"]["_TintColor"][3] * floats["_TintColorAlpha"])
    if not 0. < expected[2] < 1. or abs(expected[0]) < 1e-5:
        raise AssertionError(f"Real-texture probe must exercise nontrivial disturbance and mask values: {expected}")
    _close(_value(probe_tree.nodes["Dither Disturb 1 UV Offset"].outputs[0], texels, 2.), (*expected[:2], 0.), "disturbance")
    _close(_value(probe_tree.nodes["Dither Source Mask Alpha"].outputs[0], texels, 2.), expected[2], "depth mask alpha")
    vector = probe_tree.nodes.new("ShaderNodeCombineXYZ")
    component = probe_tree.nodes.new("ShaderNodeSeparateXYZ")
    probe_tree.links.new(probe_tree.nodes["Dither Disturb 1 UV Offset"].outputs[0], component.inputs[0])
    probe_tree.links.new(component.outputs[0], vector.inputs[0])
    probe_tree.links.new(component.outputs[1], vector.inputs[1])
    probe_tree.links.new(probe_tree.nodes["Dither Source Mask Alpha"].outputs[0], vector.inputs[2])
    emission = probe_tree.nodes.new("ShaderNodeEmission")
    probe_tree.links.new(vector.outputs[0], emission.inputs["Color"])
    output, = (node for node in probe_tree.nodes if node.type == "OUTPUT_MATERIAL")
    probe_tree.links.new(emission.outputs[0], output.inputs["Surface"])
    obj, = (obj for obj in scene.objects if obj.type == "MESH")
    obj.data.materials[0] = probe
    probe.update_tag()
    obj.data.update()
    bpy.context.view_layer.update()
    actual = _render(args.output / "formula_probe.exr")
    for index in range(3):
        if abs(actual[index] - expected[index]) > 2e-4:
            raise AssertionError(f"Cycles formula differs at channel {index}: actual={actual}, expected={expected}")
    if json.dumps(record, sort_keys=True) != original or json.loads(material["endfield_fx_source_material_json"]) != record:
        raise AssertionError("Factory or regression changed the source record")
    receipt = {"status": "passed", "material": record["Name"], "keywords": record["RenderState"]["ValidKeywords"],
               "textures": texture_evidence, "gpu_actual": list(actual), "compiled_formula": list(expected),
               "runtime_ink_rejected": rejected, "source_unchanged": True,
               "scope": "Actual packed-texture source preview plus isolated GPU probes with constant pixel-center texture coordinates; no full-scene parity claim."}
    (args.output / "verification.json").write_text(json.dumps(receipt, indent=2), encoding="utf-8")
    print("DITHER_FEATURE_REGRESSION_PASSED", flush=True)


if __name__ == "__main__":
    main()
