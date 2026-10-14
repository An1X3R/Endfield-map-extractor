"""Render signed water direction inputs in both engines and reopen the graph."""
from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys

import bpy
from mathutils import Vector

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from blender_water_transport import bind_water_transport, connect_water_transport_direction
from endfield_water_transport import WaterTransport


def render_directions(output_dir: Path) -> None:
    output_dir.mkdir(parents=True, exist_ok=True)
    bpy.ops.wm.read_factory_settings(use_empty=True)
    scene = bpy.context.scene
    settings = WaterTransport((0.2, 0.05, 0.1), (0.4, 0.15, 0.2), (0.1, 0.08, 0.06), 4.0, 0.3, 1.0, 0.0)
    bpy.ops.mesh.primitive_plane_add(size=20)
    material = bpy.data.materials.new("Direction diagnostic")
    material.use_nodes = True
    bpy.context.object.data.materials.append(material)
    tree = material.node_tree
    transport = bind_water_transport(tree, settings)
    channels = tree.nodes.new("ShaderNodeCombineXYZ")
    emission = tree.nodes.new("ShaderNodeEmission")
    tree.links.new(channels.outputs[0], emission.inputs["Color"])
    tree.links.new(emission.outputs[0], tree.nodes["Material Output"].inputs["Surface"])
    bpy.ops.object.camera_add(location=(0, -3, 4))
    camera = bpy.context.object
    scene.camera = camera
    camera.data.type = "ORTHO"
    camera.data.ortho_scale = 2
    scene.render.resolution_x = scene.render.resolution_y = 32
    scene.render.resolution_percentage = 100
    scene.render.image_settings.file_format = "OPEN_EXR"
    scene.render.image_settings.color_depth = "32"
    scene.cycles.samples = 4
    scene.cycles.use_denoising = False
    scene.eevee.taa_render_samples = 4
    reports: list[dict[str, object]] = []
    for index, (position, forward) in enumerate((
        ((0.0, -3.0, 4.0), (0.0, 0.6, -0.8)),
        ((0.0, -3.0, 4.0), (0.0, -0.6, 0.8)),
        ((3.0, 0.0, 4.0), (1.0, 0.0, 0.0)),
    )):
        camera.location = position
        camera.rotation_euler = (-Vector(position)).to_track_quat("-Z", "Y").to_euler()
        connect_water_transport_direction(transport, forward)
        for channel, label in enumerate(("View Y", "Light Y", "Phase Cosine")):
            encoded = tree.nodes.new("ShaderNodeMath")
            encoded.operation = "MULTIPLY_ADD"
            encoded.inputs[1].default_value = encoded.inputs[2].default_value = 0.5
            source = transport.inputs[label]
            if source.is_linked:
                tree.links.new(source.links[0].from_socket, encoded.inputs[0])
            else:
                encoded.inputs[0].default_value = source.default_value
            tree.links.new(encoded.outputs[0], channels.inputs[channel])
        view = Vector(position).normalized()
        expected = tuple(0.5 + 0.5 * value for value in (view.z, forward[2], view.dot(Vector(forward))))
        for engine in ("BLENDER_EEVEE_NEXT", "CYCLES"):
            scene.render.engine = engine
            scene.render.filepath = str(output_dir / f"direction_{index}_{engine}.exr")
            bpy.ops.render.render(write_still=True)
            image = bpy.data.images.load(scene.render.filepath, check_existing=False)
            pixels = list(image.pixels)
            actual = tuple(sum(pixels[4 * (y * 32 + x) + channel]
                               for y in range(12, 20) for x in range(12, 20)) / 64 for channel in range(3))
            bpy.data.images.remove(image)
            error = max(abs(a - e) for a, e in zip(actual, expected, strict=True))
            if error > 0.002:
                raise AssertionError(f"Water direction mismatch: engine={engine}, case={index}, expected={expected}, actual={actual}")
            reports.append({"engine": engine, "case": index, "forward": forward,
                            "expected": expected, "actual": actual, "max_error": error})
    bpy.context.preferences.filepaths.save_version = 0
    artifact = output_dir / "water_direction.blend"
    material_name = material.name
    bpy.ops.wm.save_as_mainfile(filepath=str(artifact))
    bpy.ops.wm.open_mainfile(filepath=str(artifact))
    restored = next(node for node in bpy.data.materials[material_name].node_tree.nodes
                    if node.get("endfield_direction_contract") == "SourceLightForwardBlenderZUp/1")
    if tuple(restored["endfield_light_forward_world"]) != (1.0, 0.0, 0.0):
        raise AssertionError("Reopened source light-forward vector changed")
    (output_dir / "validation.json").write_text(json.dumps({
        "status": "dual_engine_rendered_and_reopened", "cases": reports,
        "boundary": "Host geometric view direction only; refracted background view and live source light selection are not reconstructed",
    }, indent=2), encoding="utf-8")
    print("WATER_DIRECTION_VERIFIED", max(row["max_error"] for row in reports))


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--output-dir", type=Path, required=True)
    arguments = parser.parse_args(sys.argv[sys.argv.index("--") + 1:])
    render_directions(arguments.output_dir)
