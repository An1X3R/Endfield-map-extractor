"""Validate a real source material's explicit Lit approximation and persisted drivers."""
from __future__ import annotations

import json
import math
from pathlib import Path
import sys

import bpy

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from blender_scene_materials import create_material
from endfield_scene_material_contract import load_catalog, supported_slots, reference_key
from endfield_shader_contract import has_approximate_lit_sweep, has_verified_lit_sweep


def check_material(material: bpy.types.Material) -> None:
    if material.get("endfield_emissive_motion_status") != "blender_approximation_runtime_unresolved":
        raise AssertionError(f"Approximation status is missing: material={material.name}")
    if material.get("endfield_emissive_source_pass") != "unverified_variant_shared_lit_core":
        raise AssertionError(f"Approximation claims an established source pass: material={material.name}")
    for key in ("endfield_emissive_albedo_status", "endfield_emissive_albedo_enable_status"):
        if "verified" in material[key]:
            raise AssertionError(f"Approximation retains a verified formula claim: property={key}, value={material[key]}")
    animation = material.node_tree.animation_data
    if animation is None or len(animation.drivers) < 2:
        raise AssertionError("Source sweep must retain clock and interaction drivers")
    scene = bpy.context.scene
    for curve in animation.drivers:
        if not curve.driver.is_valid:
            raise AssertionError(f"Sweep driver is invalid: path={curve.data_path}")
        if any(target.id != scene for variable in curve.driver.variables for target in variable.targets):
            raise AssertionError("Sweep driver references a different scene")


def render_pixels(scene: bpy.types.Scene, path: Path) -> tuple[float, ...]:
    scene.render.filepath = str(path)
    bpy.ops.render.render(write_still=True)
    image = bpy.data.images.load(str(path), check_existing=False)
    values = tuple(image.pixels[:])
    bpy.data.images.remove(image)
    if not values or not all(math.isfinite(value) for value in values):
        raise AssertionError(f"Render pixels are empty or non-finite: path={path}")
    return values


def main() -> None:
    source, output = (Path(value) for value in sys.argv[sys.argv.index("--") + 1:])
    output.mkdir(parents=True, exist_ok=False)
    records, textures = load_catalog((source,))
    selected = tuple(record for record in records.values() if has_approximate_lit_sweep(record))
    if len(selected) != 1:
        raise AssertionError(f"Bounded test needs one source approximation material: count={len(selected)}")
    record = selected[0]
    if has_verified_lit_sweep(record):
        raise AssertionError("Approximation unexpectedly entered the strict verified gate")
    for slot in supported_slots(record):
        if reference_key(slot["Texture"]) not in textures:
            raise AssertionError(f"Real source texture is unavailable: property={slot['Property']}")
    for obj in tuple(bpy.data.objects):
        bpy.data.objects.remove(obj, do_unlink=True)
    material = create_material(record, textures, True)
    bpy.ops.mesh.primitive_plane_add(size=4.0)
    mesh = bpy.context.object
    mesh.name = "SourceSweepReview"
    mesh.data.materials.append(material)
    bpy.ops.object.camera_add(location=(0.0, 0.0, 10.0))
    scene = bpy.context.scene
    scene.camera = bpy.context.object
    scene.camera.data.type = "ORTHO"
    scene.camera.data.ortho_scale = 4.5
    scene.world.color = (0.0, 0.0, 0.0)
    scene.world.use_nodes = True
    background = next(node for node in scene.world.node_tree.nodes if node.type == "BACKGROUND")
    background.inputs["Strength"].default_value = 0.0
    scene.render.fps = 24
    scene.render.resolution_x = 128
    scene.render.resolution_y = 128
    scene.render.resolution_percentage = 100
    scene.render.image_settings.file_format = "PNG"
    scene.render.dither_intensity = 0.0
    scene.view_settings.view_transform = "Standard"
    scene.cycles.samples = 4
    scene.cycles.use_denoising = False
    scene.frame_set(6)
    check_material(material)
    saved = output / "source_material.blend"
    bpy.ops.wm.save_as_mainfile(filepath=str(saved))
    bpy.ops.wm.open_mainfile(filepath=str(saved), load_ui=False, use_scripts=False)
    scene = bpy.context.scene
    mesh = scene.objects["SourceSweepReview"]
    material = mesh.data.materials[0]
    scene.frame_set(6)
    check_material(material)
    scene.render.engine = "BLENDER_EEVEE_NEXT"
    render_pixels(scene, output / "source_surface.png")
    diagnostic = material.copy()
    diagnostic.name = "SourceSweepEmissionDiagnostic"
    tree = diagnostic.node_tree
    principled = next(node for node in tree.nodes if node.type == "BSDF_PRINCIPLED")
    color = principled.inputs["Emission Color"]
    if not color.is_linked or principled.inputs["Emission Strength"].default_value <= 0:
        raise AssertionError("Approximation must connect emission instead of disabling its strength")
    shader = tree.nodes.new("ShaderNodeEmission")
    tree.links.new(color.links[0].from_socket, shader.inputs["Color"])
    tree.links.new(shader.outputs[0], tree.get_output_node("ALL").inputs["Surface"])
    mesh.data.materials[0] = diagnostic
    rows: list[dict[str, str | float]] = []
    for engine in ("BLENDER_EEVEE_NEXT", "CYCLES"):
        scene.render.engine = engine
        values: list[tuple[float, ...]] = []
        for frame in (6, 18):
            scene["endfield_interaction_sweep_preview"] = 1.0
            scene.frame_set(frame)
            pixels = render_pixels(scene, output / f"{engine}_{frame}_gate1.png")
            values.append(pixels)
        difference = sum(abs(left - right) for index, (left, right) in enumerate(zip(*values, strict=True))
                         if index % 4 != 3) / (len(values[0]) * 0.75)
        if difference <= 1e-5:
            raise AssertionError(f"Source sweep has no measurable frame change: engine={engine}, difference={difference}")
        scene["endfield_interaction_sweep_preview"] = 0.0
        scene.frame_set(18)
        scene.update_tag()
        bpy.context.view_layer.update()
        disabled = render_pixels(scene, output / f"{engine}_18_gate0.png")
        maximum = max(value for index, value in enumerate(disabled) if index % 4 != 3)
        if record["Floats"]["_EmissiveMaskChannel"] == 5 and maximum > 1e-5:
            raise AssertionError(f"Constant-source interaction gate failed: engine={engine}, maximum={maximum}")
        rows.append({"engine": engine, "frame_rgb_mean_absolute_difference": difference,
                     "disabled_rgb_maximum": maximum})
    (output / "audit.json").write_text(json.dumps({
        "material": record["Name"], "source": record["Source"], "path_id": record["PathId"],
        "input": str(source), "blend": str(saved), "source_texture_count": len(textures),
        "status": material["endfield_emissive_motion_status"], "reopen_driver_check": "passed",
        "source_values": json.loads(material["endfield_emissive_sweep_source_values"]),
        "renders": rows, "scope": "Real source material and textures on a review plane; temporal checks isolate production emission. No source mesh or game-runtime parity claim."
    }, indent=2), encoding="utf-8")


if __name__ == "__main__":
    main()
