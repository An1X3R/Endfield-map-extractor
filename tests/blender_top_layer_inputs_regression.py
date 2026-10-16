"""Compare real source TOP materials and check the recovered scalar/color formulas."""
from __future__ import annotations

import json
from pathlib import Path
import sys

import bpy
from mathutils import Vector

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from blender_scene_materials import create_material
from endfield_scene_export_plan import load_scene_export_plan
from endfield_scene_material_contract import reference_key, supported_slots
from endfield_scene_material_handoff import load_plan_material_catalog
from endfield_shader_contract import has_verified_lit_top_layer


def node(material: bpy.types.Material, label: str) -> bpy.types.Node:
    rows = [value for value in material.node_tree.nodes if value.label == label]
    if len(rows) != 1:
        raise AssertionError(f"Expected one source node: material={material.name}, label={label}, count={len(rows)}")
    return rows[0]


def emission(material: bpy.types.Material, socket: bpy.types.NodeSocket) -> None:
    tree = material.node_tree
    output = tree.get_output_node("ALL")
    shader = tree.nodes.new("ShaderNodeEmission")
    tree.links.new(socket, shader.inputs["Color"])
    tree.links.new(shader.outputs[0], output.inputs["Surface"])


def render(scene: bpy.types.Scene, path: Path) -> list[float]:
    scene.render.filepath = str(path)
    bpy.ops.render.render(write_still=True)
    if not path.is_file():
        raise AssertionError(f"Render output is missing: {path}")
    image = bpy.data.images.load(str(path), check_existing=False)
    pixels = list(image.pixels[:])
    bpy.data.images.remove(image)
    return pixels


def main() -> None:
    plan_path, before_path, output = (Path(value) for value in sys.argv[sys.argv.index("--") + 1:])
    output.mkdir(parents=True, exist_ok=False)
    records, textures = load_plan_material_catalog(load_scene_export_plan(plan_path))
    selected = tuple(record for record in records.values() if has_verified_lit_top_layer(record))
    if not selected:
        raise AssertionError("Real source plan has no TOP materials")
    bpy.ops.wm.open_mainfile(filepath=str(before_path), load_ui=False, use_scripts=False)
    for obj in tuple(bpy.data.objects):
        bpy.data.objects.remove(obj, do_unlink=True)
    scene = bpy.context.scene
    scene.world = bpy.data.worlds.new("Regression World")
    scene.world.use_nodes = True
    scene.world.node_tree.nodes["Background"].inputs[0].default_value = (.08, .08, .08, 1)
    scene.view_settings.view_transform = "Standard"
    camera = bpy.data.objects.new("Source Material Comparison", bpy.data.cameras.new("Source Material Comparison"))
    scene.collection.objects.link(camera)
    camera.location = (0, -9, 6)
    camera.rotation_euler = (Vector((0, 0, 0)) - camera.location).to_track_quat("-Z", "Y").to_euler()
    camera.data.type = "ORTHO"
    camera.data.ortho_scale = 6
    scene.camera = camera
    light = bpy.data.objects.new("Comparison Key", bpy.data.lights.new("Comparison Key", "AREA"))
    scene.collection.objects.link(light)
    light.location = (0, -3, 6)
    light.data.energy = 1000
    light.data.shape = "DISK"
    light.data.size = 5
    scene.render.resolution_x = 640
    scene.render.resolution_y = 480
    scene.render.resolution_percentage = 100
    scene.render.image_settings.file_format = "PNG"
    scene.render.image_settings.color_mode = "RGBA"
    scene.cycles.samples = 8
    pairs = []
    spheres = []
    for index, record in enumerate(selected):
        before = bpy.data.materials[record["Name"]]
        before.name = "Before / " + record["Name"]
        after = create_material(record, textures, True)
        default = node(after, "_BaseHeightMap declared Shader default")
        if abs(default["endfield_source_texture_default_linear_red"] - .21404114048223255) > 1e-12:
            raise AssertionError("Source grey height was not decoded from sRGB")
        if any(value.label == "_LayerBlendMaskMap" for value in after.node_tree.nodes):
            raise AssertionError("Inactive layer mask was sampled by TOP")
        if node(after, "Base normal source Z floor").inputs[1].default_value != 2 ** -11:
            raise AssertionError("Source normal Z floor differs from DXBC")
        for column, material in enumerate((before, after)):
            bpy.ops.mesh.primitive_uv_sphere_add(segments=48, ring_count=24, radius=1,
                                               location=(-1.25 + column * 2.5, (index - .5) * 2.4, 0))
            sphere = bpy.context.object
            sphere.name = material.name
            sphere.data.materials.append(material)
            for polygon in sphere.data.polygons:
                polygon.use_smooth = True
            spheres.append(sphere)
        pairs.append({"source": reference_key(record), "before": before.name, "after": after.name,
                      "height_default_linear_red": default["endfield_source_texture_default_linear_red"]})
    comparisons = []
    for engine in ("BLENDER_EEVEE_NEXT", "CYCLES"):
        scene.render.engine = engine
        path = output / (engine.lower() + "_before_left_after_right.png")
        render(scene, path)
        comparisons.append(str(path))
    for sphere in spheres:
        sphere.hide_render = True
    bpy.ops.mesh.primitive_plane_add(size=2)
    plane = bpy.context.object
    # Hold texture inputs constant so pixel filtering does not average nonlinear formulas.
    for loop in plane.data.uv_layers.active.data:
        loop.uv = (.314, .716)
    camera.location = (0, 0, 5)
    camera.rotation_euler = (0, 0, 0)
    camera.data.ortho_scale = 2
    scene.render.engine = "CYCLES"
    scene.cycles.samples = 1
    scene.cycles.use_denoising = False
    scene.render.use_compositing = False
    scene.render.resolution_x = scene.render.resolution_y = 32
    scene.render.image_settings.file_format = "OPEN_EXR"
    scene.render.image_settings.color_depth = "32"
    bound_checks = []
    for record in selected:
        source_slot = next(slot for slot in record["Textures"] if slot["Property"] == "_Layer1BumpMap")
        height_slot = {**source_slot, "Property": "_BaseHeightMap", "Scale": [7., 3.], "Offset": [2., 4.]}
        bound = {**record, "Textures": [slot for slot in record["Textures"] if slot["Property"] != "_BaseHeightMap"] + [height_slot]}
        if height_slot not in supported_slots(bound):
            raise AssertionError("Bound height map was omitted from the source resource closure")
        material = create_material(bound, textures, True)
        height = node(material, "_BaseHeightMap")
        bump = node(material, "_Layer1BumpMap")
        if height.image != bump.image or height.image.colorspace_settings.name != "Non-Color":
            raise AssertionError("Bound height did not reuse the same source image and color-space identity")
        if height.inputs["Vector"].links[0].from_socket != bump.inputs["Vector"].links[0].from_socket:
            raise AssertionError("Height map did not use source layer coordinates")
        unverified = {**bound, "Textures": [slot if slot["Property"] != "_BaseHeightMap" else
                      {**slot, "Texture": {key: value for key, value in slot["Texture"].items() if key != "ColorSpace"}}
                      for slot in bound["Textures"]]}
        try:
            create_material(unverified, textures, True)
        except ValueError as error:
            if "requires source ColorSpace" not in str(error):
                raise
        else:
            raise AssertionError("Bound height without source color-space evidence was accepted")
        disabled = {**record, "Textures": [slot for slot in record["Textures"] if slot["Property"] != "_BaseHeightMap"],
                    "Shader": {**record["Shader"], "TextureDefaults": []},
                    "Floats": {**record["Floats"], "_LayerBlendHeight": 0}}
        inactive = create_material(disabled, textures, True)
        if any(value.label.startswith("_BaseHeightMap") for value in inactive.node_tree.nodes):
            raise AssertionError("Disabled height blend still consumed a height texture")
        bound_checks.append({"source": reference_key(record), "shared_image": height.image.name,
                             "source_layer_uv_reused": True, "missing_color_space_rejected": True,
                             "disabled_branch_requires_no_height": True})
    checks = []
    for index, record in enumerate(selected):
        omitted = {**record, "Textures": [slot for slot in record["Textures"] if slot["Property"] != "_BaseHeightMap"],
                   "Floats": {**record["Floats"], "_TopBlendWithBumpMap": 0,
                              "_TopBlendThreshold": .5, "_TopBlendSmoothness": 1}}
        if any(slot["Property"] == "_BaseHeightMap" for slot in supported_slots(omitted)):
            raise AssertionError("Declared default incorrectly requires an exported image")
        for mode in (0., 1.):
            control = {**omitted, "Floats": {**omitted["Floats"], "_Layer1BaseColorBlendMode": mode,
                                             "_Layer1BaseColorAdditiveIntensity": 2}}
            material = create_material(control, textures, True)
            material.name = f"Formula {index} Mode {mode}"
            plane.data.materials.clear()
            plane.data.materials.append(material)
            tree = material.node_tree
            combine = tree.nodes.new("ShaderNodeCombineXYZ")
            tree.links.new(node(material, "Normalized layer height blend").outputs[0], combine.inputs[0])
            tree.links.new(node(material, "_Layer1BaseMap").outputs["Alpha"], combine.inputs[1])
            grey = node(material, "_BaseHeightMap declared Shader default").outputs[0].default_value[0]
            combine.inputs[2].default_value = grey
            emission(material, combine.outputs[0])
            factors = render(scene, output / f"formula_{index}_{int(mode)}_factor.exr")
            base_socket = node(material, "Base source color contribution").inputs[0].links[0].from_socket
            layer_socket = node(material, "Layer source color contribution").inputs[0].links[0].from_socket
            emission(material, base_socket)
            base = render(scene, output / f"formula_{index}_{int(mode)}_base.exr")
            emission(material, layer_socket)
            layer = render(scene, output / f"formula_{index}_{int(mode)}_layer.exr")
            emission(material, node(material, "Top layer albedo").outputs[0])
            result = render(scene, output / f"formula_{index}_{int(mode)}_result.exr")
            factor_error = color_error = 0.
            for pixel in (4 * (y * 32 + x) for y in range(2, 30) for x in range(2, 30)):
                coverage, alpha = factors[pixel:pixel + 2]
                heights = (.5 * grey, .5 * alpha)
                peak = max(heights)
                weights = tuple(.5 * (max(value + record["Floats"]["_LayerBlendHeightTransition"] - peak, 0) + 1e-6)
                                for value in heights)
                expected_factor = weights[1] / sum(weights)
                factor_error = max(factor_error, abs(coverage - expected_factor))
                for channel in range(3):
                    expected_color = base[pixel + channel] * (1 - coverage * (1 - mode)) + layer[pixel + channel] * coverage * (1 + mode)
                    color_error = max(color_error, abs(result[pixel + channel] - expected_color))
            if factor_error > 2e-5 or color_error > 2e-5:
                center = 4 * (16 * 32 + 16)
                raise AssertionError(f"Native scalar/color formula differs: source={record['Name']}, mode={mode}, errors={factor_error}/{color_error}, center_factor={factors[center:center+4]}, base={base[center:center+4]}, layer={layer[center:center+4]}, result={result[center:center+4]}")
            checks.append({"source": reference_key(record), "mode": mode,
                           "max_height_factor_error": factor_error, "max_color_error": color_error})
    plane.hide_render = True
    for sphere in spheres:
        sphere.hide_render = False
    camera.location = (0, -9, 6)
    camera.rotation_euler = (Vector((0, 0, 0)) - camera.location).to_track_quat("-Z", "Y").to_euler()
    camera.data.ortho_scale = 6
    scene.render.resolution_x, scene.render.resolution_y = 640, 480
    scene.render.image_settings.file_format = "PNG"
    target = output / "source_material_comparison.blend"
    bpy.ops.wm.save_as_mainfile(filepath=str(target))
    bpy.ops.wm.open_mainfile(filepath=str(target), load_ui=False, use_scripts=False)
    for pair in pairs:
        material = bpy.data.materials[pair["after"]]
        if not node(material, "_BaseHeightMap declared Shader default").get("endfield_source_texture_default_sampled_channel") == "red":
            raise AssertionError("Reopened material lost height provenance")
        if any(value.type == "TEX_IMAGE" and (value.image is None or not value.image.packed_file)
               for value in material.node_tree.nodes):
            raise AssertionError("Reopened material lost a packed source image")
    (output / "verification.json").write_text(json.dumps({
        "format": "CurrentTopLayerInputsRegression/1", "source_plan": str(plan_path),
        "before_blend": str(before_path), "pairs": pairs, "renders": comparisons,
        "formula_checks": checks, "bound_height_checks": bound_checks, "save_reopen_passed": True,
        "geometry": "Diagnostic UV0 spheres and plane; source material fields and textures retained",
        "native_runtime_equivalence": False, "terrain_depth_vt_reconstruction": False,
    }, indent=2), encoding="utf-8")


if __name__ == "__main__":
    main()
