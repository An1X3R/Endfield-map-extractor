"""Verify ink texture composition against real source texels using Cycles."""
from __future__ import annotations

import argparse
from array import array
import json
from pathlib import Path
import sys

import bpy

root = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(root / "src"), str(root / "tests")]

from blender_dither_feature_regression import _quad, _render
from blender_scene_effect_materials import make_mesh_effect_material
from endfield_scene_export_plan import load_scene_export_plan
from endfield_scene_material_contract import MaterialRecord, load_catalog
from endfield_scene_material_handoff import load_plan_material_catalog
from endfield_scene_vfxadvance_ink_contract import vfxadvance_ink_texture_issues

Texel = tuple[float, float, float, float]


def _probe_images(tree: bpy.types.NodeTree) -> dict[str, Texel]:
    """Pin existing texture samplers to actual pixel centers for independent arithmetic."""
    texels: dict[str, Texel] = {}
    for node in tuple(tree.nodes):
        if node.type != "TEX_IMAGE" or node.image is None:
            continue
        width, height = node.image.size
        pixels = array("f", [0.]) * (width * height * 4)
        node.image.pixels.foreach_get(pixels)
        index = min(range(width * height), key=lambda index: abs(pixels[index * 4] - .63))
        rgba = tuple(float(pixels[index * 4 + channel]) for channel in range(4))
        if node.image.colorspace_settings.name == "sRGB":
            rgba = tuple((value / 12.92 if value <= .04045 else ((value + .055) / 1.055) ** 2.4)
                         if channel < 3 else value for channel, value in enumerate(rgba))
        texels[node.name.removeprefix("Source ")] = rgba
        coordinates = tree.nodes.new("ShaderNodeCombineXYZ")
        coordinates.inputs[0].default_value = (index % width + .5) / width
        coordinates.inputs[1].default_value = (index // width + .5) / height
        tree.links.new(coordinates.outputs[0], node.inputs["Vector"])
    return texels


def _expected(record: MaterialRecord, texels: dict[str, Texel], schedule: float) -> tuple[tuple[float, float, float], float]:
    main, mask, blend = (texels[name] for name in ("_MainTex", "_MaskTex", "_BlendTex"))
    floats, colors = record["Floats"], record["Colors"]
    if floats["_UseMainTexAsAlpha"] != 1 or floats["_UseMaskTexAsAlpha"] != 1:
        raise AssertionError("Selected real regression materials changed their alpha channels")
    tint, blend_tint = colors["_TintColor"], colors["_BlendTint"]
    alpha = main[0] * mask[0] * tint[3] * floats["_TintColorAlpha"]
    presence = min(1., max(0., (alpha + blend[3]) * blend_tint[3]))
    rgb = tuple(tint[channel] * floats["_TintColorIntensity"] +
                blend[channel] * blend_tint[channel] * presence for channel in range(3))
    if "_DissolveTex" in texels:
        delta = texels["_DissolveTex"][0] - ((floats["_DissolveScheduleOffset"] + schedule) * 2.02 - 1.01)
        coverage = min(1., max(0., delta * floats["_DissolveEdgeSharp"]))
        edge = min(1., max(0., (floats["_EdgeDistance"] - delta) * floats["_DissolveEdgeSharp"]))
        rgb = tuple(rgb[channel] * (1. - edge) + colors["_EdgeColor"][channel] *
                    floats["_TintColorIntensity"] * edge * edge for channel in range(3))
        alpha *= coverage
    return tuple(min(1000., max(0., channel)) for channel in rgb), min(1., max(0., alpha))


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--plan", type=Path, required=True)
    parser.add_argument("--material-manifest", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args(sys.argv[sys.argv.index("--") + 1:])
    args.output.mkdir(parents=True, exist_ok=False)
    bpy.ops.wm.read_factory_settings(use_empty=True)
    records, textures = load_plan_material_catalog(load_scene_export_plan(args.plan))
    extra_records, extra_textures = load_catalog((args.material_manifest,))
    records, textures = {**extra_records, **records}, {**extra_textures, **textures}
    scene = bpy.context.scene
    scene.render.engine = "CYCLES"
    scene.cycles.device, scene.cycles.samples, scene.cycles.use_denoising = "CPU", 1, False
    scene.render.resolution_x = scene.render.resolution_y = 96
    scene.render.resolution_percentage = 100
    scene.render.image_settings.file_format = "OPEN_EXR"
    scene.render.image_settings.color_mode, scene.render.image_settings.color_depth = "RGBA", "32"
    scene.render.film_transparent = True
    camera_data = bpy.data.cameras.new("Ink Regression Camera")
    camera = bpy.data.objects.new(camera_data.name, camera_data)
    scene.collection.objects.link(camera)
    camera_data.type, camera_data.ortho_scale, camera.location = "ORTHO", 2.1, (0., 0., 4.)
    scene.camera = camera
    comparisons: list[dict[str, str | int | float | list[float]]] = []
    for material_name in ("M_fxmap_indie_dg013_inkroad_01_fj_01", "M_fxmap_indie_dg013_inkroad_03_fj_01"):
        record, = (row for row in records.values() if row["Name"] == material_name)
        source_json = json.dumps(record, sort_keys=True)
        material = make_mesh_effect_material(record, textures, 2., 0)
        if material.get("endfield_fx_branch") != "vfxadvance_ink_texture_partial":
            raise AssertionError(f"Production dispatch did not select the ink branch: {material_name}")
        tree = material.node_tree
        if tree.nodes.get("Source _DisturbTex2"):
            raise AssertionError("Inactive serialized disturbance enabled an absent compiled stage")
        if not vfxadvance_ink_texture_issues(record, 4):
            raise AssertionError("Unresolved vertex-color attenuation escaped the partial contract")
        for node in tree.nodes:
            if node.type == "TEX_IMAGE" and node.image is not None:
                node.image.pack()
        for obj in tuple(scene.objects):
            if obj.type == "MESH":
                bpy.data.objects.remove(obj, do_unlink=True)
        _quad(material)
        _render(args.output / f"{material_name}_source.exr")
        scene.render.image_settings.file_format = "PNG"
        bpy.data.images["Render Result"].save_render(str(args.output / f"{material_name}_source.png"))
        scene.render.image_settings.file_format = "OPEN_EXR"
        probe = material.copy()
        tree = probe.node_tree
        texels = _probe_images(tree)
        obj, = (obj for obj in scene.objects if obj.type == "MESH")
        obj.data.materials[0] = probe
        schedules = [0.]
        if "_DissolveTex" in texels:
            for delta in (.1, -.05):
                schedules.append((texels["_DissolveTex"][0] - delta + 1.01) / 2.02 - record["Floats"]["_DissolveScheduleOffset"])
        output, = (node for node in tree.nodes if node.type == "OUTPUT_MATERIAL")
        emission = tree.nodes.new("ShaderNodeEmission")
        tree.links.new(emission.outputs[0], output.inputs["Surface"])
        for index, schedule in enumerate(schedules):
            if tree.nodes.get("HOST_DISSOLVE_SCHEDULE"):
                tree.nodes["HOST_DISSOLVE_SCHEDULE"].outputs[0].default_value = schedule
            expected_rgb, expected_alpha = _expected(record, texels, schedule)
            tree.links.new(tree.nodes["Source RGB Clamp Upper Bound 1000"].outputs[0], emission.inputs["Color"])
            probe.update_tag()
            actual_rgb = _render(args.output / f"{material_name}_{index}_rgb.exr")
            tree.links.new(tree.nodes["Ink Source Coverage"].outputs["Result"], emission.inputs["Color"])
            probe.update_tag()
            actual_alpha = _render(args.output / f"{material_name}_{index}_coverage.exr")[0]
            error = max(abs(actual_rgb[channel] - expected_rgb[channel]) for channel in range(3))
            error = max(error, abs(actual_alpha - expected_alpha))
            if error > 2e-4:
                raise AssertionError(f"Ink composition differs: {material_name}, schedule={schedule}, rgb={actual_rgb}/{expected_rgb}, alpha={actual_alpha}/{expected_alpha}")
            comparisons.append({"material": material_name, "case": index, "schedule": schedule,
                                "rgb": list(actual_rgb[:3]), "alpha": actual_alpha, "max_error": error})
        if json.dumps(record, sort_keys=True) != source_json:
            raise AssertionError("Material factory modified the source record")
    plane, = (row for row in records.values() if row["Name"] == "M_fxmap_indie_dg013_inkroad_0_plane_01")
    if not vfxadvance_ink_texture_issues(plane, 0):
        raise AssertionError("Runtime-only zero-tint plane was admitted as a visible texture layer")
    (args.output / "verification.json").write_text(json.dumps({"status": "passed", "comparisons": comparisons,
        "source_records_unchanged": True, "runtime_only_plane_rejected": True,
        "scope": "Real source textures, production dispatch, static snapshots, Cycles composition and partial dissolve controls; no native simulation parity claim"}, indent=2), encoding="utf-8")
    print("VFXADVANCE_INK_REGRESSION_PASSED", flush=True)


if __name__ == "__main__":
    main()
