"""Verify shallow coverage over real receivers in both Blender engines and after save."""
from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys

import bpy

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from blender_scene_water import configure_water_scene
from blender_water_edge_blend import connect_water_edge_blend
from blender_water_transport import bind_water_transport, connect_water_transport_surface
from endfield_water_transport import (WaterEdgeBlend, WaterTransport, evaluate_water_transport,
                                      evaluate_water_scene_color_weight)


def run(output: Path) -> None:
    output.mkdir(parents=True, exist_ok=False)
    bpy.ops.wm.read_factory_settings(use_empty=True)
    scene = bpy.context.scene
    configure_water_scene(scene)
    scene.world = bpy.data.worlds.new("Black reflection")
    scene.world.use_nodes = True
    scene.world.node_tree.nodes["Background"].inputs["Strength"].default_value = 0.0
    background = (0.3, 0.6, 0.9)
    ambient, direct = (0.1, 0.12, 0.15), (0.8, 0.7, 0.6)
    optics = WaterTransport((0.2, 0.05, 0.1), (0.4, 0.15, 0.2), (0.1, 0.08, 0.06), 4., .3, 1., .5)
    # Hand-derived coverage cases include contact, shallow angle, saturated depth and zero source factor.
    cases = ((0., 20., 1., 2., 0.), (.1, 0., .5, 2., .2), (.1, 10., .5, 2., .15),
             (.1, 20., .5, 2., .1), (.5, 20., 1., 2., 1.), (3., 20., .5, 2., 1.),
             (3., 20., 1., 0., 0.))
    expected = []
    fresnel = ((1.333 - 1.) / (1.333 + 1.)) ** 2
    for index, (depth, distance, cosine, factor, coverage) in enumerate(cases):
        bpy.ops.mesh.primitive_plane_add(size=1.95, location=(index * 2 + 1, 0, 0))
        receiver = bpy.context.object
        material = bpy.data.materials.new(f"Receiver_{index}")
        material.use_nodes = True
        emit = material.node_tree.nodes.new("ShaderNodeEmission")
        emit.inputs["Color"].default_value = (*background, 1.)
        material.node_tree.links.new(emit.outputs[0], material.node_tree.nodes["Material Output"].inputs["Surface"])
        receiver.data.materials.append(material)
        bpy.ops.mesh.primitive_plane_add(size=1.9, location=(index * 2 + 1, 0, .5))
        water = bpy.context.object
        material = bpy.data.materials.new(f"Water_{index}")
        material.use_nodes = True
        tree = material.node_tree
        transport = bind_water_transport(tree, optics)
        depth_node = tree.nodes.new("ShaderNodeValue")
        depth_node.outputs[0].default_value = depth
        tree.links.new(depth_node.outputs[0], transport.inputs["Depth"])
        for label, value in (("View Y", 1.), ("Light Y", .8), ("Phase Cosine", .3)):
            transport.inputs[label].default_value = value
        transport.inputs["HOST Ambient Radiance"].default_value = (*ambient, 1.)
        transport.inputs["HOST Direct Radiance"].default_value = (*direct, 1.)
        geometry = tree.nodes.new("ShaderNodeNewGeometry")
        connect_water_transport_surface(material, transport, geometry.outputs["Normal"], 1.333, 0.)
        edge = connect_water_edge_blend(material, geometry.outputs["Normal"], depth_node.outputs[0], WaterEdgeBlend(factor))
        for label, value in (("View Distance", distance), ("View Normal Cosine", cosine)):
            socket = edge.inputs[label]
            for link in tuple(socket.links):
                tree.links.remove(link)
            socket.default_value = value
        water.data.materials.append(material)
        transmission, scatter = evaluate_water_transport(optics, depth, 1., .8, .3, ambient, direct)
        weight = evaluate_water_scene_color_weight(optics, depth, 1., .8, .3, direct)
        expected.append(tuple((1. - coverage) * b + coverage * (1. - fresnel) * ((t + w) * b + s)
                              for b, t, w, s in zip(background, transmission, weight, scatter, strict=True)))
    bpy.ops.object.camera_add(location=(len(cases), 0, 8))
    scene.camera = bpy.context.object
    scene.camera.data.type = "ORTHO"
    scene.camera.data.ortho_scale = len(cases) * 2
    scene.render.resolution_x, scene.render.resolution_y = len(cases) * 64, 64
    scene.render.resolution_percentage = 100
    scene.render.image_settings.file_format = "OPEN_EXR"
    scene.render.image_settings.color_depth = "32"
    scene.cycles.samples = 512
    scene.cycles.use_adaptive_sampling = False
    scene.cycles.use_denoising = False
    scene.eevee.taa_render_samples = 128
    reports = []
    for engine in ("BLENDER_EEVEE_NEXT", "CYCLES"):
        scene.render.engine = engine
        scene.render.filepath = str(output / (engine + ".exr"))
        bpy.ops.render.render(write_still=True)
        image = bpy.data.images.load(scene.render.filepath, check_existing=False)
        pixels = tuple(image.pixels)
        errors = []
        for column, target in enumerate(expected):
            actual = tuple(sum(pixels[4 * (y * scene.render.resolution_x + column * 64 + x) + c]
                               for y in range(28, 36) for x in range(28, 36)) / 64 for c in range(3))
            error = max(abs(a - b) for a, b in zip(actual, target, strict=True))
            if error > .012:
                raise AssertionError(f"Water edge closure mismatch: engine={engine}, case={cases[column]}, actual={actual}, expected={target}")
            errors.append(error)
        bpy.data.images.remove(image)
        reports.append({"engine": engine, "maximum_error": max(errors), "cases": len(cases)})
    bpy.context.preferences.filepaths.save_version = 0
    artifact = output / "edge_regression.blend"
    bpy.ops.wm.save_as_mainfile(filepath=str(artifact))
    bpy.ops.wm.open_mainfile(filepath=str(artifact), use_scripts=False)
    for index, case in enumerate(cases):
        material = bpy.data.materials[f"Water_{index}"]
        if material.get("endfield_edge_blend_contract") != "SourceWaterEdgeBlend/1":
            raise AssertionError(f"Saved edge contract missing: case={index}")
        group = material.node_tree.nodes["EF_Water_Source_Edge_Coverage"]
        if group.inputs["Distance Edge Blend Factor"].default_value != case[3]:
            raise AssertionError(f"Saved source edge factor changed: case={index}")
    (output / "receipt.json").write_text(json.dumps({"status": "passed_saved_reopened", "cases": cases,
        "renders": reports, "artifact": str(artifact), "scope": "Source coverage plus actual host background/surface closure; native screen ray depth not claimed"}, indent=2), encoding="utf-8")


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args(sys.argv[sys.argv.index("--") + 1:])
    run(args.output)
