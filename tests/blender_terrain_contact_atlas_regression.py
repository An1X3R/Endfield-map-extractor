"""Check retained terrain A/N orientation and decoding through both render engines."""
from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys

import bpy
import numpy as np
from numpy.typing import NDArray

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from blender_scene_terrain import atlas_pixels, sample_atlas_normals
from blender_scene_terrain_blend import CONTRACT


def bilinear(pixels: NDArray[np.float32], uv: tuple[float, float]) -> NDArray[np.float64]:
    xy = np.array(uv) * np.array((pixels.shape[1], pixels.shape[0])) - .5
    low = np.floor(xy).astype(np.int64)
    fraction = xy - low
    return sum((pixels[low[1]+dy, low[0]+dx, :3].astype(np.float64)
                * (fraction[0] if dx else 1-fraction[0]) * (fraction[1] if dy else 1-fraction[1])
                for dx, dy in ((0, 0), (1, 0), (0, 1), (1, 1))), np.zeros(3))


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--source", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args(sys.argv[sys.argv.index("--")+1:])
    args.output.mkdir(parents=True, exist_ok=False)
    bpy.ops.wm.open_mainfile(filepath=str(args.source), load_ui=False, use_scripts=False)
    source = next(tree for tree in bpy.data.node_groups if tree.get("endfield_terrain_contact_contract") == CONTRACT)
    manifest = json.loads(Path(source["endfield_terrain_contact_surface"]).read_text(encoding="utf-8-sig"))
    bounds = manifest["atlas_world_bounds"]
    color_pixels = atlas_pixels(source.nodes["Source terrain A"].image)
    normal_pixels = atlas_pixels(source.nodes["Source terrain N"].image)
    for obj in tuple(bpy.data.objects):
        bpy.data.objects.remove(obj, do_unlink=True)
    scene = bpy.context.scene
    scene.render.use_compositing = False
    scene.render.resolution_x = scene.render.resolution_y = 8
    scene.render.resolution_percentage = 100
    scene.render.image_settings.file_format = "OPEN_EXR"
    scene.render.image_settings.color_depth = "32"
    scene.cycles.samples = 1
    scene.cycles.use_denoising = False
    camera = bpy.data.objects.new("Atlas Direction Probe", bpy.data.cameras.new("Atlas Direction Probe"))
    scene.collection.objects.link(camera)
    camera.location.z = 10
    camera.data.type = "ORTHO"
    camera.data.ortho_scale = 1
    scene.camera = camera
    bpy.ops.mesh.primitive_plane_add(size=2)
    material = bpy.data.materials.new("Terrain A N Probe")
    material.use_nodes = True
    bpy.context.object.data.materials.append(material)
    tree = material.node_tree
    entry = tree.nodes.new("ShaderNodeGroup")
    group = source.copy()
    entry.node_tree = group
    position = group.nodes.new("ShaderNodeCombineXYZ")
    geometry = next(node for node in group.nodes if node.type == "NEW_GEOMETRY")
    for link in tuple(geometry.outputs["Position"].links):
        group.links.new(position.outputs[0], link.to_socket)
    emission = tree.nodes.new("ShaderNodeEmission")
    tree.links.new(emission.outputs[0], tree.nodes["Material Output"].inputs["Surface"])
    normal_scale = tree.nodes.new("ShaderNodeVectorMath")
    normal_scale.operation = "MULTIPLY_ADD"
    normal_scale.inputs[1].default_value = (.5, .5, .5)
    normal_scale.inputs[2].default_value = (.5, .5, .5)
    tree.links.new(entry.outputs["Terrain Normal"], normal_scale.inputs[0])
    checks = []
    for index, uv in enumerate(((.113, .179), (.827, .231), (.191, .839), (.743, .787), (.469, .547))):
        position.inputs[0].default_value = bounds[0]+uv[0]*(bounds[1]-bounds[0])
        position.inputs[1].default_value = -bounds[3]+uv[1]*(bounds[3]-bounds[2])
        position.inputs[2].default_value = 300
        expected = {"A": bilinear(color_pixels, uv),
                    "N": sample_atlas_normals(normal_pixels, np.array([uv]), "CLIP")[0]*.5+.5}
        for engine in ("BLENDER_EEVEE_NEXT", "CYCLES"):
            scene.render.engine = engine
            for channel, socket in (("A", entry.outputs["Terrain Color"]), ("N", normal_scale.outputs[0])):
                tree.links.new(socket, emission.inputs["Color"])
                path = args.output/f"{index}_{engine.lower()}_{channel}.exr"
                scene.render.filepath = str(path)
                bpy.ops.render.render(write_still=True)
                image = bpy.data.images.load(str(path), check_existing=False)
                actual = atlas_pixels(image)[4, 4, :3].astype(np.float64)
                bpy.data.images.remove(image)
                error = float(np.max(np.abs(actual-expected[channel])))
                if error > .003:
                    raise AssertionError(f"Terrain source direction or decode mismatch: uv={uv}, channel={channel}, engine={engine}, error={error}, actual={actual}, expected={expected[channel]}")
                checks.append({"uv": uv, "channel": channel, "engine": engine, "max_error": error})
    (args.output/"terrain_contact_atlas_regression.json").write_text(json.dumps({"status": "passed", "checks": checks}, indent=2), encoding="utf-8")
    print(json.dumps({"status": "passed", "max_error": max(row["max_error"] for row in checks)}))


main()
