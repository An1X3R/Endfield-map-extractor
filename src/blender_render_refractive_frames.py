"""Render camera-bound refraction with a fresh background for every output frame."""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import TypedDict

import bpy

sys.path.insert(0, str(Path(__file__).resolve().parent))

from blender_scene_vfxrefract import refresh_vfxrefract_scene_color


class RefractiveFrame(TypedDict):
    frame: int
    image: str
    background: str
    snapshot_metadata: str


def render_refractive_frames(scene: bpy.types.Scene, camera: bpy.types.Object,
                             receivers: tuple[bpy.types.Object, ...],
                             frames: tuple[int, ...], output: Path) -> tuple[RefractiveFrame, ...]:
    """Render an explicit frame list, restoring the caller's snapshot and scene state."""
    if not frames or any(type(frame) is not int for frame in frames):
        raise ValueError("Refraction render requires a nonempty tuple of integer frames")
    if tuple(sorted(set(frames))) != frames:
        raise ValueError("Refraction render frames must be unique and increasing")
    if camera.type != "CAMERA" or camera.name not in scene.objects:
        raise ValueError("Refraction render camera must belong to the scene")
    if not receivers or any(obj.name not in scene.objects for obj in receivers):
        raise ValueError("Refraction render requires receiver objects from the scene")
    materials = {slot.material for obj in receivers for slot in obj.material_slots}
    if not materials or any(material is None or material.get("endfield_fx_branch") !=
                            "vfxrefract_mask_rb_snapshot" for material in materials):
        raise ValueError("Refraction render receivers must exclusively use snapshot materials")
    bindings = tuple((node, node.image) for material in materials for node in
                     material.node_tree.nodes if node.get("endfield_host_scene_color_receiver"))
    if len(bindings) != 2 * len(materials) or any(image is None for _, image in bindings):
        raise ValueError("Refraction render requires two populated background samplers per material")
    snapshots = tuple((material, str(material["endfield_host_scene_color"])) for material in materials)
    prior_audit = scene.get("endfield_scene_color_snapshot")
    settings = scene.render.image_settings
    prior = (scene.frame_current, scene.frame_subframe, scene.camera, scene.render.filepath,
             settings.file_format, settings.color_mode, settings.color_depth)
    output = output.resolve()
    output.mkdir(parents=True, exist_ok=False)
    records: list[RefractiveFrame] = []
    captured: bpy.types.Image | None = None
    try:
        scene.camera = camera
        for frame in frames:
            scene.frame_set(frame)
            background = output / f"background_{frame:06d}.exr"
            captured = refresh_vfxrefract_scene_color(scene, camera, receivers, background)
            metadata = str(captured["endfield_host_scene_color"])
            image_path = output / f"frame_{frame:06d}.png"
            settings.file_format, settings.color_mode, settings.color_depth = "PNG", "RGBA", "8"
            scene.render.filepath = str(image_path)
            bpy.ops.render.render(write_still=True, scene=scene.name)
            if not image_path.is_file():
                raise FileNotFoundError(f"Refraction frame output missing: frame={frame}, path={image_path}")
            records.append({"frame": frame, "image": str(image_path), "background": str(background),
                            "snapshot_metadata": metadata})
            for node, image in bindings:
                node.image = image
            bpy.data.images.remove(captured)
            captured = None
    finally:
        for node, image in bindings:
            node.image = image
        for material, metadata in snapshots:
            material["endfield_host_scene_color"] = metadata
        if prior_audit is not None:
            scene["endfield_scene_color_snapshot"] = prior_audit
        if captured is not None:
            bpy.data.images.remove(captured)
        frame, subframe, scene.camera, scene.render.filepath, settings.file_format, settings.color_mode, settings.color_depth = prior
        scene.frame_set(frame, subframe=subframe)
    return tuple(records)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--blend", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--start", type=int, required=True)
    parser.add_argument("--end", type=int, required=True)
    parser.add_argument("--step", type=int, required=True)
    parser.add_argument("--engine", choices=("BLENDER_EEVEE_NEXT", "CYCLES"), required=True)
    args = parser.parse_args(sys.argv[sys.argv.index("--") + 1:])
    if args.step < 1 or args.end < args.start:
        raise ValueError("Frame range requires a positive step and end >= start")
    bpy.ops.wm.open_mainfile(filepath=str(args.blend.resolve()), load_ui=False, use_scripts=False)
    scene = bpy.context.scene
    if scene.camera is None:
        raise ValueError("Refraction render input has no active camera")
    scene.render.engine = args.engine
    receivers = tuple(obj for obj in scene.objects if any(slot.material is not None and
                      slot.material.get("endfield_fx_branch") == "vfxrefract_mask_rb_snapshot"
                      for slot in obj.material_slots))
    records = render_refractive_frames(scene, scene.camera, receivers,
                                       tuple(range(args.start, args.end + 1, args.step)), args.output)
    (args.output / "receipt.json").write_text(json.dumps({
        "format": "EndfieldRefractionFrameRender/1", "source": str(args.blend.resolve()),
        "engine": args.engine, "frames": records,
        "boundary": "Offline two-pass frames; viewport snapshots remain camera-bound. Not native live SceneColor."
    }, indent=2), encoding="utf-8")


if __name__ == "__main__":
    main()
