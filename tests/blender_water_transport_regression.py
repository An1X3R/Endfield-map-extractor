"""Render source transport over known receivers, then reopen the saved host graph."""
from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys

import bpy

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from blender_scene_water import configure_water_scene
from blender_water_transport import bind_water_transport, connect_water_transport_surface
from endfield_water_transport import (WaterTransport, evaluate_water_scene_color_weight,
                                      evaluate_water_transport)


def render_case(output_dir: Path, ior: float, reflection_radiance: tuple[float, float, float]) -> None:
    output_dir.mkdir(parents=True, exist_ok=True)
    bpy.ops.wm.read_factory_settings(use_empty=True)
    scene = bpy.context.scene
    configure_water_scene(scene)
    ambient, direct = (0.1, 0.12, 0.15), (0.8, 0.7, 0.6)
    background = (0.3, 0.6, 0.9)
    scene.world = bpy.data.worlds.new("Known reflection radiance")
    scene.world.use_nodes = True
    scene.world.node_tree.nodes["Background"].inputs["Color"].default_value = (*reflection_radiance, 1.0)
    scene.world.node_tree.nodes["Background"].inputs["Strength"].default_value = 1.0
    fresnel = ((ior - 1.0) / (ior + 1.0)) ** 2
    targets: list[tuple[float, ...]] = []
    influences = (0.0, 0.5, 30.0)
    depths = (0.0, 1.0, 3.0)
    case_inputs: list[tuple[int, float, float]] = []
    hdr_weight_seen = False
    for influence_index, influence in enumerate(influences):
        settings = WaterTransport(
            (0.2, 0.05, 0.1), (0.4, 0.15, 0.2), (0.1, 0.08, 0.06),
            4.0, 0.3, 1.0, influence)
        for depth_index, depth in enumerate(depths):
            index = influence_index * len(depths) + depth_index
            x = index * 2 + 1
            bpy.ops.mesh.primitive_plane_add(size=1.95, location=(x, 0, 0))
            receiver = bpy.context.object
            receiver.name = f"Receiver_{index}"
            receiver_material = bpy.data.materials.new(receiver.name)
            receiver_material.use_nodes = True
            emit = receiver_material.node_tree.nodes.new("ShaderNodeEmission")
            emit.inputs["Color"].default_value = (*background, 1)
            receiver_material.node_tree.links.new(
                emit.outputs[0], receiver_material.node_tree.nodes["Material Output"].inputs["Surface"])
            receiver.data.materials.append(receiver_material)
            bpy.ops.mesh.primitive_plane_add(size=1.9, location=(x, 0, 0.5))
            water = bpy.context.object
            water.name = f"Water_{index}"
            water_material = bpy.data.materials.new(water.name)
            water_material.use_nodes = True
            group = bind_water_transport(water_material.node_tree, settings)
            for label, value in (("Depth", depth), ("View Y", 1), ("Light Y", 0.8), ("Phase Cosine", 0.3)):
                group.inputs[label].default_value = value
            group.inputs["HOST Ambient Radiance"].default_value = (*ambient, 1)
            group.inputs["HOST Direct Radiance"].default_value = (*direct, 1)
            geometry = water_material.node_tree.nodes.new("ShaderNodeNewGeometry")
            connect_water_transport_surface(water_material, group, geometry.outputs["Normal"], ior, 0.0)
            water.data.materials.append(water_material)
            transmission, scattering = evaluate_water_transport(
                settings, depth, 1, 0.8, 0.3, ambient, direct)
            scene_weight = evaluate_water_scene_color_weight(
                settings, depth, 1, 0.8, 0.3, direct)
            raw_background_weight = tuple(t + w for t, w in zip(
                transmission, scene_weight, strict=True))
            if influence == 30.0 and max(raw_background_weight) > 1.0:
                hdr_weight_seen = True
            targets.append(tuple((1.0 - fresnel) * ((t + w) * b + s) + fresnel * r
                                 for t, w, b, s, r in zip(
                                     transmission, scene_weight, background, scattering,
                                     reflection_radiance, strict=True)))
            case_inputs.append((index, influence, depth))
    if not hdr_weight_seen:
        raise AssertionError('HDR scene-color fixture did not produce any refracted background weight above one')
    case_count = len(case_inputs)
    pixels_per_case = 64
    bpy.ops.object.camera_add(location=(case_count, 0, 8))
    scene.camera = bpy.context.object
    scene.camera.data.type = "ORTHO"
    scene.camera.data.ortho_scale = case_count * 2
    scene.render.resolution_x, scene.render.resolution_y = case_count * pixels_per_case, 64
    scene.render.resolution_percentage = 100
    scene.render.image_settings.file_format = "OPEN_EXR"
    scene.render.image_settings.color_depth = "32"
    scene.cycles.samples = 1024
    scene.cycles.use_adaptive_sampling = False
    scene.cycles.use_denoising = False
    reports = []
    for engine in ("CYCLES", "BLENDER_EEVEE_NEXT"):
        scene.render.engine = engine
        scene.render.filepath = str(output_dir / (engine + ".exr"))
        bpy.ops.render.render(write_still=True)
        image = bpy.data.images.load(scene.render.filepath, check_existing=False)
        pixels = list(image.pixels)
        errors = []
        normalized_errors = []
        for column, target in enumerate(targets):
            actual = tuple(sum(pixels[4 * (y * scene.render.resolution_x
                                           + column * pixels_per_case + x) + c]
                               for y in range(28, 36) for x in range(28, 36)) / 64 for c in range(3))
            error = max(abs(a - b) for a, b in zip(actual, target, strict=True))
            # Preserve the unit-radiance tolerance for the HDR closure comparison.
            normalized_error = error / max(1.0, max(target))
            if normalized_error > 0.012:
                raise AssertionError(
                    f"Host water transport mismatch: engine={engine}, ior={ior}, column={column}, "
                    f"scene_color_influence={case_inputs[column][1]}, depth={case_inputs[column][2]}, "
                    f"actual={actual}, expected={target}")
            if case_inputs[column][1] == 30.0 and max(target) > 1.0 and max(actual) <= 1.0:
                raise AssertionError(
                    f"HDR refracted background weight was clamped: engine={engine}, "
                    f"case={case_inputs[column]}, actual={actual}, expected={target}")
            errors.append(error)
            normalized_errors.append(normalized_error)
        reports.append({"engine": engine, "max_error": max(errors),
                        "max_unit_radiance_error": max(normalized_errors), "cases": len(targets)})
        bpy.data.images.remove(image)
    bpy.context.preferences.filepaths.save_version = 0
    artifact = output_dir / "water_transport_host.blend"
    bpy.ops.wm.save_as_mainfile(filepath=str(artifact))
    bpy.ops.wm.open_mainfile(filepath=str(artifact))
    reopened_cases = []
    for index, influence, depth in case_inputs:
        material = bpy.data.materials[f"Water_{index}"]
        group = next(node for node in material.node_tree.nodes if node.type == "GROUP"
                     and node.node_tree.get("endfield_contract") == "SourceWaterTransport/1")
        actual_influence = group.inputs["Scene Color Env Influence"].default_value
        if abs(actual_influence - influence) > 1e-7:
            raise AssertionError(
                f"Reopened scene color influence changed: material={material.name}, "
                f"actual={actual_influence}, expected={influence}")
        refraction = next(node for node in material.node_tree.nodes if node.type == "BSDF_REFRACTION")
        links = refraction.inputs["Color"].links
        if (len(links) != 1 or links[0].from_node != group
                or links[0].from_socket.name != "Refracted Background Weight"):
            raise AssertionError(f"Reopened refraction lost its scene-color background weight: material={material.name}")
        tree = bpy.data.materials[f"Water_{index}"].node_tree
        output = tree.get_output_node("EEVEE")
        link = output.inputs["Thickness"].links[0]
        if link.from_node.type != "VALUE" or link.from_socket.default_value != 0:
            raise AssertionError(f"Open boundary thickness was not preserved: material=Water_{index}")
        reopened_cases.append({"material": material.name, "scene_color_environment_influence": actual_influence,
                               "depth": depth, "refracted_weight_source": links[0].from_socket.name})
    (output_dir / "validation.json").write_text(json.dumps({
        "status": "rendered_and_reopened", "engines": reports,
        "scope": "Normal-incidence host Fresnel composition of (T+W)*B+S and known reflection radiance",
        "ior": ior, "reflection_radiance": reflection_radiance,
        "scene_color_environment_influences": influences, "hdr_weight_above_one_verified": True,
        "reopened_cases": reopened_cases,
        "boundary": "No claim of native final composition, oblique refraction, roughness, shadow transport, caustics or underwater parity",
    }, indent=2), encoding="utf-8")
    print("WATER_TRANSPORT_HOST_VERIFIED", reports)


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--output-dir", type=Path, required=True)
    arguments = parser.parse_args(sys.argv[sys.argv.index("--") + 1:])
    render_case(arguments.output_dir / "unit_ior", 1.0, (0.0, 0.0, 0.0))
    render_case(arguments.output_dir / "physical_ior", 1.333, (0.2, 0.25, 0.3))
