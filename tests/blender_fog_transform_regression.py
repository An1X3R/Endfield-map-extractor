"""Render object-relative fog height under transformed cards and moving cameras."""
from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys

import bpy
from mathutils import Euler, Matrix, Vector

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from blender_scene_fog_materials import _top_fade


def verify(output_dir: Path) -> None:
    output_dir.mkdir(parents=True, exist_ok=True)
    bpy.ops.wm.read_factory_settings(use_empty=True)
    scene = bpy.context.scene
    mesh = bpy.data.meshes.new("Fog source local plane")
    mesh.from_pydata([(-2., -2., .3), (2., -2., .3), (2., 2., .3), (-2., 2., .3)], [], [(0, 1, 2, 3)])
    card = bpy.data.objects.new("Transformed source fog", mesh)
    scene.collection.objects.link(card)
    parent = bpy.data.objects.new("Nonuniform parent", None)
    scene.collection.objects.link(parent)
    material = bpy.data.materials.new("Object height diagnostic")
    material.use_nodes = True
    card.data.materials.append(material)
    tree = material.node_tree
    geometry = tree.nodes.new("ShaderNodeNewGeometry")
    fade = _top_fade(tree, geometry.outputs["Position"], .6, .65)
    emission = tree.nodes.new("ShaderNodeEmission")
    tree.links.new(fade, emission.inputs["Color"])
    tree.links.new(emission.outputs[0], tree.nodes["Material Output"].inputs["Surface"])
    camera = bpy.data.objects.new("Camera", bpy.data.cameras.new("Camera"))
    scene.collection.objects.link(camera)
    scene.camera = camera
    camera.data.type = "ORTHO"
    camera.data.ortho_scale = .2
    scene.render.resolution_x = scene.render.resolution_y = 32
    scene.render.resolution_percentage = 100
    scene.render.image_settings.file_format = "OPEN_EXR"
    scene.render.image_settings.color_depth = "32"
    scene.cycles.samples = 4
    scene.cycles.use_denoising = False
    scene.eevee.taa_render_samples = 4
    cases = (
        Matrix.Translation((0., 0., 0.)),
        Matrix.Translation((3., -2., 7.)) @ Matrix.Diagonal((1., 2., 3., 1.)),
        Matrix.Translation((-5., 1., 3.)) @ Euler((.4, .7, -.2)).to_matrix().to_4x4() @ Matrix.Diagonal((2., .7, 1.3, 1.)),
        Matrix(((1., .2, .1, 2.), (0., 2., .3, -3.), (.4, -.3, 1.2, 4.), (0., 0., 0., 1.))),
    )
    rows: list[dict[str, object]] = []
    for index, matrix in enumerate(cases):
        if index == 3:
            parent.matrix_world = Matrix.Translation((2., -3., 4.)) @ Matrix.Diagonal((1., 2., 3., 1.))
            card.parent = parent
            card.matrix_basis = Euler((.4, .7, -.2)).to_matrix().to_4x4()
        else:
            card.parent = None
            card.matrix_world = matrix
        bpy.context.view_layer.update()
        matrix = card.matrix_world.copy()
        point = matrix @ Vector((0., 0., .3))
        normal = (matrix.to_3x3().inverted().transposed() @ Vector((0., 0., 1.))).normalized()
        local_up_length = (matrix.to_3x3() @ Vector((0., 0., 1.))).length
        expected = max(0., min(1., (matrix.translation.z - point.z) / local_up_length * .6 + .65))
        for view, shift in enumerate((Vector((0., 0., 0.)), Vector((.4, .3, .5)))):
            camera.location = point + normal * 4. + shift
            camera.rotation_euler = (point - camera.location).to_track_quat("-Z", "Y").to_euler()
            for engine in ("BLENDER_EEVEE_NEXT", "CYCLES"):
                scene.render.engine = engine
                scene.render.filepath = str(output_dir / f"height_{index}_{view}_{engine}.exr")
                bpy.ops.render.render(write_still=True)
                image = bpy.data.images.load(scene.render.filepath, check_existing=False)
                pixels = list(image.pixels)
                value = sum(pixels[4 * (y * 32 + x)] for y in range(12, 20) for x in range(12, 20)) / 64
                bpy.data.images.remove(image)
                error = abs(value - expected)
                if error > .002:
                    raise AssertionError(f"Fog object-height mismatch: case={index}, camera={view}, engine={engine}, expected={expected}, actual={value}")
                rows.append({"case": index, "camera": view, "engine": engine,
                             "expected": expected, "actual": value, "error": error})
    bpy.context.preferences.filepaths.save_version = 0
    path = output_dir / "fog_transform.blend"
    bpy.ops.wm.save_as_mainfile(filepath=str(path))
    bpy.ops.wm.open_mainfile(filepath=str(path), use_scripts=False)
    restored = bpy.data.materials["Object height diagnostic"].node_tree
    if restored.nodes["Fog Source Object Origin"].vector_type != "POINT":
        raise AssertionError("Saved fog origin is not an object-space point transform")
    (output_dir / "validation.json").write_text(json.dumps({
        "status": "two_engines_four_transforms_two_views_verified_saved_reopened", "cases": rows,
    }, indent=2), encoding="utf-8")
    print("FOG_TRANSFORM_VERIFIED", max(float(row["error"]) for row in rows))


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--output-dir", type=Path, required=True)
    args = parser.parse_args(sys.argv[sys.argv.index("--") + 1:])
    verify(args.output_dir)
