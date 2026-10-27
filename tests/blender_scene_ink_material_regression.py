"""Check real ink-road VFX disturbance formulas and source face-culling modes."""
from __future__ import annotations

import argparse
from array import array
import json
import math
from pathlib import Path
import sys

import bpy
from bpy_extras.object_utils import world_to_camera_view

root = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(root / "src"), str(root / "tests")]

from blender_character_vfx_disturbance_regression import _close, _value
from blender_scene_effect_materials import make_mesh_effect_material
from endfield_scene_export_plan import load_scene_export_plan
from endfield_scene_material_contract import AssetReference, MaterialRecord, reference_key
from endfield_scene_material_handoff import load_plan_material_catalog

Texel = tuple[float, float, float, float]
Vector3 = tuple[float, float, float]
MATERIAL_NAME = "M_fxmap_indie_dg013_inkbuilding_01_1103"
SNAPSHOT_SECONDS = 2.0


def _linear_srgb(value: float) -> float:
    return value / 12.92 if value <= 0.04045 else ((value + 0.055) / 1.055) ** 2.4


def _image_texel(image: bpy.types.Image) -> Texel:
    width, height = image.size
    if width <= 0 or height <= 0:
        raise AssertionError(f"Disturbance image has invalid dimensions: {image.name} {image.size[:]}")
    pixels = array("f", [0.0]) * (width * height * 4)
    image.pixels.foreach_get(pixels)
    sample_index = max(
        range(width * height),
        key=lambda index: abs(pixels[index * 4] - 0.5),
    )
    values = tuple(float(pixels[sample_index * 4 + channel]) for channel in range(4))
    if image.colorspace_settings.name == "sRGB":
        values = tuple(_linear_srgb(value) if channel < 3 else value
                       for channel, value in enumerate(values))
    if abs(values[0] - 0.5) < 0.05:
        raise AssertionError(f"Disturbance image red channel is too close to neutral: image={image.name}, red={values[0]}")
    return values


def _texture_references(record: MaterialRecord) -> dict[str, AssetReference | None]:
    return {
        row["Property"]: row["Texture"]
        for row in record["Textures"]
        if isinstance(row.get("Property"), str)
    }


def _source_texels(material: bpy.types.Material, record: MaterialRecord) -> tuple[dict[str, Texel], dict[str, object]]:
    tree = material.node_tree
    references = _texture_references(record)
    texels: dict[str, Texel] = {}
    evidence: dict[str, object] = {}
    for sample_index, disturb_index in ((0, 1), (1, 2)):
        sample_name = f"_SampleTex{sample_index}"
        disturb_name = f"_DisturbTex{disturb_index}"
        sample_reference = references[sample_name]
        disturb_reference = references[disturb_name]
        if sample_reference is None or disturb_reference is None:
            raise AssertionError(f"Disturbance source reference is null: {sample_name}/{disturb_name}")
        if reference_key(sample_reference) != reference_key(disturb_reference):
            raise AssertionError(f"Disturbance source differs from its sample layer: {sample_name}/{disturb_name}")
        node_name = f"Source {sample_name}"
        node = tree.nodes.get(node_name)
        if node is None or node.type != "TEX_IMAGE" or node.image is None:
            raise AssertionError(f"Factory did not create the real source image node: {node_name}")
        texel = _image_texel(node.image)
        texels[node_name] = texel
        evidence[sample_name] = {
            "source_reference": list(reference_key(sample_reference)),
            "source_name": sample_reference.get("Name"),
            "image": node.image.name,
            "colorspace": node.image.colorspace_settings.name,
            "dimensions": list(node.image.size),
            "linear_texel": list(texel),
        }
    return texels, evidence


def _check_red_remap(tree: bpy.types.NodeTree, record: MaterialRecord, texels: dict[str, Texel],
                     sample_name: str, red_prefix: str) -> float:
    floats = record["Floats"]
    bias = floats["_Bi_Disturb"]
    red_scale = tree.nodes.get(f"{red_prefix} Red Scale")
    red_shift = tree.nodes.get(f"{red_prefix} Signed Red")
    if red_scale is None or red_shift is None:
        raise AssertionError(
            f"Factory omitted the red remap: prefix={red_prefix}, missing_scale={red_scale is None}, "
            f"missing_shift={red_shift is None}"
        )
    if (red_scale.operation != "MULTIPLY" or red_shift.operation != "SUBTRACT"
            or not math.isclose(red_scale.inputs[1].default_value, 1.0 + bias)
            or not math.isclose(red_shift.inputs[1].default_value, bias)):
        raise AssertionError(f"BiDisturb red-remap constants differ: prefix={red_prefix}")
    actual_value = _value(red_shift.outputs[0], texels, SNAPSHOT_SECONDS)
    if not isinstance(actual_value, (int, float)):
        raise AssertionError(f"Expected scalar red remap at {red_prefix}")
    actual = float(actual_value)
    expected = texels[sample_name][0] * (1.0 + bias) - bias
    _close(actual, expected, f"{red_prefix} source-red remap")
    return actual


def _scalar_layer(tree: bpy.types.NodeTree, record: MaterialRecord, texels: dict[str, Texel],
                  layer_index: int) -> tuple[float, Vector3]:
    prefix = f"Disturb {layer_index}"
    sample_name = f"Source _SampleTex{layer_index - 1}"
    red = _check_red_remap(tree, record, texels, sample_name, prefix)
    node = tree.nodes.get(f"{prefix} UV Offset")
    if node is None:
        raise AssertionError(f"Factory omitted the scalar offset node: {prefix}")
    value = _value(node.outputs["Vector"], texels, SNAPSHOT_SECONDS)
    if not isinstance(value, tuple):
        raise AssertionError(f"Expected scalar vector offset: {prefix}")
    floats = record["Floats"]
    expected = (red * floats[f"_DisturbUIntensity{layer_index}"],
                red * floats[f"_DisturbVIntensity{layer_index}"], 0.0)
    _close(value, expected, f"{prefix} scalar offset")
    return red, tuple(value)


def _normal_layer(tree: bpy.types.NodeTree, record: MaterialRecord, texels: dict[str, Texel],
                  layer_index: int) -> tuple[float, Vector3]:
    prefix = f"Disturb {layer_index}"
    sample_name = f"Source _SampleTex{layer_index - 1}"
    red = _check_red_remap(tree, record, texels, sample_name, f"{prefix} Normal")
    node = tree.nodes.get(f"{prefix} Normal UV Offset")
    if node is None:
        raise AssertionError(f"Factory omitted the normal offset node: {prefix}")
    value = _value(node.outputs["Vector"], texels, SNAPSHOT_SECONDS)
    if not isinstance(value, tuple):
        raise AssertionError(f"Expected normal vector offset: {prefix}")
    texel = texels[sample_name]
    strength = record["Floats"][f"_DisturbUIntensity{layer_index}"]
    expected = ((2.0 * texel[3] * red - 1.0) * strength,
                (2.0 * texel[1] - 1.0) * strength, 0.0)
    _close(value, expected, f"{prefix} normal offset")
    return red, tuple(value)


def _check_accumulation(tree: bpy.types.NodeTree, texels: dict[str, Texel],
                        first_offset: Vector3, second_offset: Vector3) -> Vector3:
    accumulated = tree.nodes.get("Disturb 2 Accumulation")
    if accumulated is None:
        raise AssertionError("Factory omitted the second-layer accumulation node")
    value = _value(accumulated.outputs["Vector"], texels, SNAPSHOT_SECONDS)
    if not isinstance(value, tuple):
        raise AssertionError("Expected accumulated disturbance vector")
    actual_sum = tuple(value)
    expected_sum = tuple(a + b for a, b in zip(first_offset, second_offset, strict=True))
    _close(actual_sum, expected_sum, "two-layer disturbance accumulation")
    return actual_sum


def _check_scalar_disturbance(material: bpy.types.Material, record: MaterialRecord,
                              texels: dict[str, Texel]) -> dict[str, object]:
    first_red, first_offset = _scalar_layer(material.node_tree, record, texels, 1)
    second_red, second_offset = _scalar_layer(material.node_tree, record, texels, 2)
    accumulated = _check_accumulation(material.node_tree, texels, first_offset, second_offset)
    return {"remapped_red": [first_red, second_red],
            "layer_offsets": [list(first_offset), list(second_offset)],
            "accumulated_offset": list(accumulated)}


def _check_normal_disturbance(material: bpy.types.Material, record: MaterialRecord,
                              texels: dict[str, Texel]) -> dict[str, object]:
    first_red, first_offset = _normal_layer(material.node_tree, record, texels, 1)
    second_red, second_offset = _normal_layer(material.node_tree, record, texels, 2)
    accumulated = _check_accumulation(material.node_tree, texels, first_offset, second_offset)
    return {"remapped_red": [first_red, second_red],
            "layer_offsets": [list(first_offset), list(second_offset)],
            "accumulated_offset": list(accumulated)}


def _replace_surface_with_test_emission(material: bpy.types.Material, cull_mode: int) -> None:
    tree = material.node_tree
    output_nodes = [node for node in tree.nodes if node.type == "OUTPUT_MATERIAL"]
    if len(output_nodes) != 1:
        raise AssertionError(f"Expected one material output: material={material.name}, outputs={len(output_nodes)}")
    emission = tree.nodes.new("ShaderNodeEmission")
    emission.name = "Test Control White Emission"
    emission.inputs["Color"].default_value = (1.0, 1.0, 1.0, 1.0)
    emission.inputs["Strength"].default_value = 1.0
    if cull_mode == 0:
        target = output_nodes[0].inputs["Surface"]
    else:
        cull_name = "Source Frontface Culling" if cull_mode == 1 else "Source Backface Culling"
        cull = tree.nodes.get(cull_name)
        if cull is None:
            raise AssertionError(f"Factory omitted culling node for mode {cull_mode}")
        target = cull.inputs[2 if cull_mode == 1 else 1]
    for link in tuple(target.links):
        tree.links.remove(link)
    tree.links.new(emission.outputs[0], target)
    material["endfield_test_control"] = "white_emission_surface_isolates_face_culling"


def _quad(name: str, center_x: float, face_name: str, face_indices: tuple[int, int, int, int],
          material: bpy.types.Material) -> bpy.types.Object:
    half = 0.35
    vertices = [(-half, -half, 0.0), (half, -half, 0.0),
                (half, half, 0.0), (-half, half, 0.0)]
    mesh = bpy.data.meshes.new(f"{name} Mesh")
    mesh.from_pydata(vertices, (), (face_indices,))
    mesh.update()
    obj = bpy.data.objects.new(name, mesh)
    bpy.context.scene.collection.objects.link(obj)
    obj.location.x = center_x
    obj.data.materials.append(material)
    obj["endfield_test_face"] = face_name
    return obj


def _render_culling(output: Path, materials: dict[int, bpy.types.Material]) -> dict[str, object]:
    scene = bpy.context.scene
    scene.render.engine = "BLENDER_EEVEE_NEXT"
    scene.render.resolution_x = 768
    scene.render.resolution_y = 256
    scene.render.resolution_percentage = 100
    scene.render.image_settings.file_format = "PNG"
    scene.render.image_settings.color_mode = "RGBA"
    scene.render.image_settings.color_depth = "8"
    scene.render.film_transparent = False
    scene.view_settings.view_transform = "Standard"
    world = bpy.data.worlds.new("Ink Material Cull Regression World")
    world.use_nodes = True
    world.node_tree.nodes["Background"].inputs[0].default_value = (0.0, 0.0, 0.0, 1.0)
    world.node_tree.nodes["Background"].inputs[1].default_value = 1.0
    scene.world = world
    camera_data = bpy.data.cameras.new("Ink Material Cull Regression Camera")
    camera = bpy.data.objects.new(camera_data.name, camera_data)
    scene.collection.objects.link(camera)
    camera.location = (0.0, 0.0, 10.0)
    camera.data.type = "ORTHO"
    camera.data.ortho_scale = 7.2
    scene.camera = camera
    layout = (
        ("Cull0_front", 0, "front", (0, 1, 2, 3), -2.5, True),
        ("Cull0_back", 0, "back", (0, 3, 2, 1), -1.5, True),
        ("Cull1_front", 1, "front", (0, 1, 2, 3), -0.5, False),
        ("Cull1_back", 1, "back", (0, 3, 2, 1), 0.5, True),
        ("Cull2_front", 2, "front", (0, 1, 2, 3), 1.5, True),
        ("Cull2_back", 2, "back", (0, 3, 2, 1), 2.5, False),
    )
    objects = {
        name: _quad(name, center_x, face_name, face_indices, materials[cull_mode])
        for name, cull_mode, face_name, face_indices, center_x, _expected_visible in layout
    }
    scene.render.filepath = str(output)
    bpy.ops.render.render(write_still=True)
    if not output.is_file() or output.stat().st_size <= 0:
        raise FileNotFoundError(f"Culling regression render was not written: {output}")
    rendered = bpy.data.images.load(str(output), check_existing=False)
    width, height = rendered.size
    pixels = array("f", [0.0]) * (width * height * 4)
    rendered.pixels.foreach_get(pixels)
    results: dict[str, object] = {}
    for name, cull_mode, face_name, _face_indices, _center_x, expected_visible in layout:
        projected = world_to_camera_view(scene, camera, objects[name].matrix_world.translation)
        x = min(width - 1, max(0, round(projected.x * (width - 1))))
        y = min(height - 1, max(0, round(projected.y * (height - 1))))
        offset = (y * width + x) * 4
        color = tuple(float(pixels[offset + channel]) for channel in range(4))
        visible = max(color[:3]) > 0.8
        if visible != expected_visible:
            raise AssertionError(
                f"CullMode pixel mismatch: quad={name}, cull_mode={cull_mode}, "
                f"face={face_name}, pixel={color}, expected_visible={expected_visible}"
            )
        results[name] = {"cull_mode": cull_mode, "face": face_name,
                         "pixel_xy": [x, y], "pixel_rgba": list(color), "visible": visible,
                         "expected_visible": expected_visible}
    bpy.data.images.remove(rendered)
    return {"image": str(output), "image_bytes": output.stat().st_size,
            "resolution": [width, height], "samples": results,
            "test_control": "Each real-factory material retained its CullMode node graph; the non-cull surface was replaced with fixed white Emission to isolate face culling."}


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--plan", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args(sys.argv[sys.argv.index("--") + 1:])
    output = args.output.resolve()
    output.mkdir(parents=True, exist_ok=False)
    bpy.ops.wm.read_factory_settings(use_empty=True)
    plan = load_scene_export_plan(args.plan)
    records, textures = load_plan_material_catalog(plan)
    record = next((value for value in records.values() if value["Name"] == MATERIAL_NAME), None)
    if record is None:
        raise AssertionError(f"Real plan does not contain the target material: {MATERIAL_NAME}")
    original = json.dumps(record, sort_keys=True)
    floats = record["Floats"]
    if (floats.get("_UseDisturb") != 1 or floats.get("_UseDisturb2") != 1
            or floats.get("_Bi_Disturb") != 1 or floats.get("_DisturbTex1Normal") != 0
            or floats.get("_DisturbTex2Normal") != 0 or floats.get("_InParticle") != 0):
        raise AssertionError(f"The real 1103 material no longer matches the expected two-layer scalar contract: {MATERIAL_NAME}")
    references = _texture_references(record)
    if (reference_key(references["_SampleTex0"]) != reference_key(references["_DisturbTex1"])
            or reference_key(references["_SampleTex1"]) != reference_key(references["_DisturbTex2"])):
        raise AssertionError("The source SampleTex and DisturbTex bindings no longer identify the same two textures")

    material = make_mesh_effect_material(record, textures, SNAPSHOT_SECONDS, 0)
    texels, texture_evidence = _source_texels(material, record)
    scalar_formula = _check_scalar_disturbance(material, record, texels)
    if len(texels) != 2:
        raise AssertionError(f"Expected two real source image texels, found {len(texels)}")

    normal_record = {**record, "Floats": {**floats, "_DisturbTex1Normal": 1, "_DisturbTex2Normal": 1}}
    normal_material = make_mesh_effect_material(normal_record, textures, SNAPSHOT_SECONDS, 0)
    normal_material["endfield_test_control"] = "normal_decoder_flags_enabled_only_for_branch_test"
    normal_formula = _check_normal_disturbance(normal_material, normal_record, texels)
    if json.dumps(record, sort_keys=True) != original:
        raise AssertionError("The test control changed the original plan material record")

    cull_materials: dict[int, bpy.types.Material] = {}
    for cull_mode in (0, 1, 2):
        cull_record = {**record, "Floats": {**floats, "_CullMode": cull_mode}}
        cull_material = make_mesh_effect_material(cull_record, textures, SNAPSHOT_SECONDS, 0)
        _replace_surface_with_test_emission(cull_material, cull_mode)
        cull_materials[cull_mode] = cull_material
    if json.dumps(record, sort_keys=True) != original:
        raise AssertionError("CullMode test variants changed the original plan material record")
    render = _render_culling(output / "cull_modes_single_image.png", cull_materials)

    receipt = {
        "format": "EndfieldSceneInkMaterialRegression/1",
        "status": "passed",
        "scene_id": plan["map_id"],
        "source_material": {"name": record["Name"], "source": record["Source"], "path_id": record["PathId"],
                            "shader": record["Shader"].get("Name")},
        "disturbance_textures": texture_evidence,
        "scalar_two_layer_formula": scalar_formula,
        "normal_branch_test_control": {
            "enabled_flags": {"_DisturbTex1Normal": 1, "_DisturbTex2Normal": 1},
            "source_flags": {"_DisturbTex1Normal": floats["_DisturbTex1Normal"],
                              "_DisturbTex2Normal": floats["_DisturbTex2Normal"]},
            "formula": normal_formula,
            "test_control_only": True,
        },
        "culling": render,
        "source_plan_record_unchanged": True,
        "full_scene_export": False,
        "blender_version": bpy.app.version_string,
    }
    (output / "verification.json").write_text(json.dumps(receipt, indent=2, allow_nan=False), encoding="utf-8")
    print("SCENE_INK_MATERIAL_REGRESSION_PASSED", flush=True)


if __name__ == "__main__":
    main()
