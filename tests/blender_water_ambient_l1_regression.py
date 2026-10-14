"""Render signed source SH axes in both engines after saving and reopening."""
from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys

import bpy

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from blender_water_transport import bind_water_transport, connect_water_transport_ambient_l1
from endfield_water_transport import WaterTransport, evaluate_water_ambient_l1


def run(output: Path) -> None:
    output.mkdir(parents=True, exist_ok=False)
    bpy.ops.wm.read_factory_settings(use_empty=True)
    bpy.context.preferences.filepaths.save_version = 0
    scene = bpy.context.scene
    coefficients = ((0.2, -0.3, 0.4, 0.1), (-0.1, 0.4, 0.2, 0.2), (0.3, 0.1, -0.5, 0.15))
    scale = 1.7
    settings = WaterTransport((0.2, 0.2, 0.2), (0.4, 0.4, 0.4), (0.1, 0.1, 0.1), 2., 0.3, 1., 0.)
    expected: list[tuple[float, float, float]] = []
    for index in range(6):
        axis, sign = index // 2, 1.0 if index % 2 == 0 else -1.0
        source_normal = tuple(sign if component == axis else 0.0 for component in range(3))
        target = tuple(max(0.0, channel[3] + sign * channel[axis]) * scale for channel in coefficients)
        if evaluate_water_ambient_l1(coefficients, source_normal, scale) != target:
            raise AssertionError(f"Source axis evaluation differs: axis={axis}, sign={sign}")
        expected.append(target)
        bpy.ops.mesh.primitive_plane_add(size=1.95, location=(index * 2, 0, 0))
        material = bpy.data.materials.new(f"Source SH axis {index}")
        material.use_nodes = True
        bpy.context.object.data.materials.append(material)
        tree = material.node_tree
        transport = bind_water_transport(tree, settings)
        normal = tree.nodes.new("ShaderNodeCombineXYZ")
        for slot, component in zip(normal.inputs, (source_normal[0], -source_normal[2], source_normal[1]), strict=True):
            slot.default_value = component
        connect_water_transport_ambient_l1(transport, normal.outputs[0], coefficients, scale)
        emission = tree.nodes.new("ShaderNodeEmission")
        tree.links.new(transport.inputs["HOST Ambient Radiance"].links[0].from_socket, emission.inputs["Color"])
        tree.links.new(emission.outputs[0], tree.nodes["Material Output"].inputs["Surface"])
    bpy.ops.object.camera_add(location=(5, 0, 10))
    scene.camera = bpy.context.object
    scene.camera.data.type = "ORTHO"
    scene.camera.data.ortho_scale = 12
    scene.render.resolution_x, scene.render.resolution_y = 384, 64
    scene.render.resolution_percentage = 100
    scene.render.image_settings.file_format = "OPEN_EXR"
    scene.render.image_settings.color_depth = "32"
    scene.cycles.samples = 1
    scene.cycles.use_denoising = False
    scene.eevee.taa_render_samples = 8
    artifact = output / "source_sky_l1.blend"
    bpy.ops.wm.save_as_mainfile(filepath=str(artifact))
    bpy.ops.wm.open_mainfile(filepath=str(artifact), use_scripts=False, load_ui=False)
    scene = bpy.context.scene
    reports: list[dict[str, object]] = []
    for engine in ("CYCLES", "BLENDER_EEVEE_NEXT"):
        scene.render.engine = engine
        scene.render.filepath = str(output / f"{engine}.exr")
        bpy.ops.render.render(write_still=True)
        image = bpy.data.images.load(scene.render.filepath, check_existing=False)
        pixels = list(image.pixels)
        errors = []
        for index, target in enumerate(expected):
            actual = tuple(sum(pixels[4 * (y * 384 + index * 64 + x) + channel]
                for y in range(28, 36) for x in range(28, 36)) / 64 for channel in range(3))
            error = max(abs(left - right) for left, right in zip(actual, target, strict=True))
            # Eevee's intermediate shading buffer stores half-precision radiance.
            tolerance = 0.002 if engine == "BLENDER_EEVEE_NEXT" else 2e-5
            if error > tolerance:
                raise AssertionError(f"Reopened SH shader mismatch: engine={engine}, axis_case={index}, actual={actual}, expected={target}")
            errors.append(error)
        reports.append({"engine": engine, "six_axis_max_error": max(errors)})
        bpy.data.images.remove(image)
    receipt = {"status": "saved_reopened_rendered", "artifact": str(artifact), "reports": reports,
        "scope": "Synthetic signed coefficients verify orientation, channel order, clamping and scaling; no native volume selection claimed"}
    (output / "verification.json").write_text(json.dumps(receipt, indent=2), encoding="utf-8")
    print(json.dumps(receipt))


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args(sys.argv[sys.argv.index("--") + 1:])
    run(args.output)
