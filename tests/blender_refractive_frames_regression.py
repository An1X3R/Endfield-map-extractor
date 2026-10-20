"""Verify batched refraction snapshots on a packed, real office scene."""
from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
import sys
from typing import TypedDict, cast

import bpy

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from blender_render_refractive_frames import RefractiveFrame, render_refractive_frames


class SnapshotMetadata(TypedDict):
    format: str
    camera: str
    camera_world_matrix: list[list[float]]
    camera_type: str
    lens: float
    frame: int
    subframe: float
    engine: str
    image_size: list[int]
    excluded_draws: list[str]
    file: str
    sha256: str
    scope: str


class RenderState(TypedDict):
    frame: int
    subframe: float
    camera: str | None
    engine: str
    filepath: str
    file_format: str
    color_mode: str
    color_depth: str
    resolution: list[int]
    cycles_samples: int
    eevee_samples: int


class EngineReceipt(TypedDict):
    engine: str
    caller_state_before: RenderState
    caller_state_after: RenderState
    receiver_images_restored: bool
    image_datablocks_restored: bool
    material_metadata_restored: bool
    scene_audit_restored: bool
    object_set_restored: bool
    requested_frames: list[int]
    metadata_verified_frames: list[int]
    unique_backgrounds: int
    unique_renders: int
    records: list[RefractiveFrame]


class RegressionReceipt(TypedDict):
    status: str
    source_blend: str
    portable_blend: str
    blender_version: str
    source_sha256_before: str
    source_sha256_after: str
    scripts_disabled_for_reopen: bool
    packed_file_images: list[str]
    receiver_images_reloaded_after_reopen: list[str]
    receiver_objects: list[str]
    camera: str
    restore_camera_sentinel: str | None
    render_size: list[int]
    samples: int
    runs: list[EngineReceipt]
    boundary: str


def sha256_file(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def receiver_objects(scene: bpy.types.Scene) -> tuple[bpy.types.Object, ...]:
    receivers = tuple(obj for obj in scene.objects if any(
        slot.material is not None and
        slot.material.get("endfield_fx_branch") == "vfxrefract_mask_rb_snapshot"
        for slot in obj.material_slots))
    if not receivers:
        raise AssertionError("Office sample has no snapshot refraction receiver objects")
    return receivers


def receiver_materials(receivers: tuple[bpy.types.Object, ...]) -> tuple[bpy.types.Material, ...]:
    materials = tuple(sorted({slot.material for obj in receivers for slot in obj.material_slots
                              if slot.material is not None}, key=lambda material: material.name))
    if not materials or any(material.get("endfield_fx_branch") != "vfxrefract_mask_rb_snapshot"
                            for material in materials):
        raise AssertionError("Refraction receivers contain a non-snapshot material")
    return materials


def receiver_bindings(materials: tuple[bpy.types.Material, ...]) -> tuple[
        tuple[bpy.types.Node, bpy.types.Image], ...]:
    bindings = tuple((node, node.image) for material in materials for node in material.node_tree.nodes
                     if node.get("endfield_host_scene_color_receiver"))
    if len(bindings) != 2 * len(materials) or any(image is None for _, image in bindings):
        raise AssertionError("Every snapshot material must have two populated image samplers")
    return cast(tuple[tuple[bpy.types.Node, bpy.types.Image], ...], bindings)


def image_inventory() -> tuple[tuple[int, str, str], ...]:
    return tuple(sorted((image.as_pointer(), image.name, image.source) for image in bpy.data.images
                        if image.name not in {"Render Result", "Viewer Node"}))


def caller_state(scene: bpy.types.Scene) -> RenderState:
    settings = scene.render.image_settings
    return {
        "frame": scene.frame_current,
        "subframe": scene.frame_subframe,
        "camera": scene.camera.name if scene.camera is not None else None,
        "engine": scene.render.engine,
        "filepath": scene.render.filepath,
        "file_format": settings.file_format,
        "color_mode": settings.color_mode,
        "color_depth": settings.color_depth,
        "resolution": [scene.render.resolution_x, scene.render.resolution_y,
                       scene.render.resolution_percentage],
        "cycles_samples": scene.cycles.samples,
        "eevee_samples": scene.eevee.taa_render_samples,
    }


def parse_snapshot_metadata(serialized: str) -> SnapshotMetadata:
    parsed = json.loads(serialized)
    if not isinstance(parsed, dict):
        raise AssertionError("Frame snapshot metadata must be a JSON object")
    required = {"format", "camera", "camera_world_matrix", "camera_type", "lens", "frame",
                "subframe", "engine", "image_size", "excluded_draws", "file", "sha256", "scope"}
    if set(parsed) != required:
        raise AssertionError(f"Unexpected snapshot metadata keys: {sorted(parsed)}")
    if not isinstance(parsed["camera"], str) or not isinstance(parsed["frame"], int):
        raise AssertionError("Snapshot metadata camera and frame must be typed values")
    if not isinstance(parsed["engine"], str) or not isinstance(parsed["file"], str):
        raise AssertionError("Snapshot metadata engine and file must be strings")
    if not isinstance(parsed["image_size"], list) or len(parsed["image_size"]) != 2:
        raise AssertionError("Snapshot metadata image size must contain two dimensions")
    if not all(isinstance(value, int) for value in parsed["image_size"]):
        raise AssertionError("Snapshot metadata image dimensions must be integers")
    if not isinstance(parsed["sha256"], str) or len(parsed["sha256"]) != 64:
        raise AssertionError("Snapshot metadata must carry a SHA-256 digest")
    return cast(SnapshotMetadata, parsed)


def receiver_images(materials: tuple[bpy.types.Material, ...]) -> tuple[bpy.types.Image, ...]:
    images = tuple(sorted({node.image for material in materials for node in material.node_tree.nodes
                           if node.type == "TEX_IMAGE" and node.image is not None}, key=lambda image: image.name))
    if not images:
        raise AssertionError("Refraction receiver materials have no image textures")
    return images


def pack_receiver_images(materials: tuple[bpy.types.Material, ...]) -> list[str]:
    images = receiver_images(materials)
    for image in images:
        if not image.has_data:
            image.reload()
        pixel_count = len(image.pixels)
        if pixel_count == 0 or not image.has_data or min(image.size) <= 0:
            raise AssertionError(f"Receiver image has no loaded pixels before packing: {image.name}")
        if image.packed_file is None:
            image.pack()
        if image.packed_file is None:
            raise AssertionError(f"Scene image did not pack into the portable blend: {image.name}")
    return [image.name for image in images]


def verify_reopened_receiver_images(materials: tuple[bpy.types.Material, ...]) -> list[str]:
    images = receiver_images(materials)
    for image in images:
        if image.packed_file is None:
            raise AssertionError(f"Portable receiver image is unpacked after reopen: {image.name}")
        if not image.has_data:
            image.reload()
        pixel_count = len(image.pixels)
        if pixel_count == 0 or not image.has_data or min(image.size) <= 0:
            raise AssertionError(f"Portable receiver image did not reload after reopen: {image.name}")
    return [image.name for image in images]


def configure_caller_state(scene: bpy.types.Scene, engine: str, output: Path,
                           restore_camera: bpy.types.Object | None) -> None:
    scene.camera = restore_camera
    scene.frame_set(17, subframe=0.375)
    scene.render.engine = engine
    scene.render.resolution_x = 640
    scene.render.resolution_y = 360
    scene.render.resolution_percentage = 100
    scene.cycles.samples = 16
    scene.eevee.taa_render_samples = 16
    scene.render.filepath = str(output / "caller_state_sentinel")
    scene.render.image_settings.file_format = "TIFF"
    scene.render.image_settings.color_mode = "RGB"
    scene.render.image_settings.color_depth = "8"


def verify_engine(scene: bpy.types.Scene, camera: bpy.types.Object,
                  receivers: tuple[bpy.types.Object, ...], materials: tuple[bpy.types.Material, ...],
                  restore_camera: bpy.types.Object | None, engine: str, output: Path) -> EngineReceipt:
    frames = (1, 24, 48)
    run_output = output / ("eevee" if engine == "BLENDER_EEVEE_NEXT" else "cycles")
    configure_caller_state(scene, engine, run_output, restore_camera)
    before_state = caller_state(scene)
    before_bindings = receiver_bindings(materials)
    before_images = image_inventory()
    before_material_metadata = tuple((material.name, str(material["endfield_host_scene_color"]))
                                     for material in materials)
    before_scene_audit = scene.get("endfield_scene_color_snapshot")
    before_objects = tuple(sorted(obj.name for obj in scene.objects))
    records = render_refractive_frames(scene, camera, receivers, frames, run_output)
    after_state = caller_state(scene)
    if before_state != after_state:
        raise AssertionError(f"Render state was not restored for engine {engine}: "
                             f"before={before_state}, after={after_state}")
    after_bindings = receiver_bindings(materials)
    bindings_restored = all(before_node == after_node and before_image == after_image
                            for (before_node, before_image), (after_node, after_image)
                            in zip(before_bindings, after_bindings, strict=True))
    images_restored = before_images == image_inventory()
    material_metadata_restored = before_material_metadata == tuple(
        (material.name, str(material["endfield_host_scene_color"])) for material in materials)
    scene_audit_restored = before_scene_audit == scene.get("endfield_scene_color_snapshot")
    objects_restored = before_objects == tuple(sorted(obj.name for obj in scene.objects))
    if not bindings_restored or not images_restored or not material_metadata_restored:
        raise AssertionError(f"Image or material state accumulated during {engine} render batch")
    if not scene_audit_restored or not objects_restored:
        raise AssertionError(f"Scene state changed during {engine} render batch")
    if tuple(record["frame"] for record in records) != frames:
        raise AssertionError(f"Frame batch returned the wrong order: engine={engine}")
    background_hashes: list[str] = []
    render_hashes: list[str] = []
    metadata_frames: list[int] = []
    for record in records:
        metadata = parse_snapshot_metadata(record["snapshot_metadata"])
        if metadata["camera"] != camera.name or metadata["frame"] != record["frame"]:
            raise AssertionError(f"Snapshot camera/frame mismatch: engine={engine}, frame={record['frame']}")
        if metadata["engine"] != engine or metadata["image_size"] != [640, 360]:
            raise AssertionError(f"Snapshot engine/size mismatch: engine={engine}, frame={record['frame']}")
        background = Path(record["background"])
        render = Path(record["image"])
        if not background.is_file() or not render.is_file():
            raise FileNotFoundError(f"Frame output missing: engine={engine}, frame={record['frame']}")
        if background != Path(metadata["file"]) or sha256_file(background) != metadata["sha256"]:
            raise AssertionError(f"Snapshot file digest mismatch: engine={engine}, frame={record['frame']}")
        if render.read_bytes()[:8] != b"\x89PNG\r\n\x1a\n" or render.stat().st_size <= 8:
            raise AssertionError(f"Frame render is not a nonempty PNG: engine={engine}, frame={record['frame']}")
        background_hashes.append(metadata["sha256"])
        render_hashes.append(sha256_file(render))
        metadata_frames.append(metadata["frame"])
    unique_backgrounds = len(set(background_hashes))
    unique_renders = len(set(render_hashes))
    if unique_backgrounds < 2 or unique_renders < 2:
        raise AssertionError(f"Office frame renders show no temporal change: engine={engine}, "
                             f"unique_backgrounds={unique_backgrounds}, unique_renders={unique_renders}")
    return {
        "engine": engine,
        "caller_state_before": before_state,
        "caller_state_after": after_state,
        "receiver_images_restored": bindings_restored,
        "image_datablocks_restored": images_restored,
        "material_metadata_restored": material_metadata_restored,
        "scene_audit_restored": scene_audit_restored,
        "object_set_restored": objects_restored,
        "requested_frames": list(frames),
        "metadata_verified_frames": metadata_frames,
        "unique_backgrounds": unique_backgrounds,
        "unique_renders": unique_renders,
        "records": list(records),
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args(sys.argv[sys.argv.index("--") + 1:])
    source = args.source.resolve()
    output = args.output.resolve()
    if not source.is_file():
        raise FileNotFoundError(f"Office snapshot blend is missing: {source}")
    if output.exists():
        if not output.is_dir() or next(output.iterdir(), None) is not None:
            raise FileExistsError(f"Regression output must be an empty directory: {output}")
    else:
        output.mkdir(parents=True, exist_ok=False)
    source_hash_before = sha256_file(source)
    bpy.ops.wm.open_mainfile(filepath=str(source), load_ui=False, use_scripts=False)
    source_receivers = receiver_objects(bpy.context.scene)
    source_materials = receiver_materials(source_receivers)
    packed_images = pack_receiver_images(source_materials)
    portable = output / "office_vfxrefract_snapshot_portable.blend"
    bpy.ops.wm.save_as_mainfile(filepath=str(portable))
    if not portable.is_file():
        raise FileNotFoundError(f"Portable scene was not saved: {portable}")
    bpy.ops.wm.open_mainfile(filepath=str(portable), load_ui=False, use_scripts=False)
    scene = bpy.context.scene
    if scene is None or scene.camera is None:
        raise AssertionError("Portable office blend has no active scene camera")
    camera = scene.camera
    receivers = receiver_objects(scene)
    materials = receiver_materials(receivers)
    reloaded_images = verify_reopened_receiver_images(materials)
    unpacked_receiver_images = [image.name for image in receiver_images(materials)
                                if image.packed_file is None]
    if unpacked_receiver_images:
        raise AssertionError(f"Portable blend has unpacked receiver images: {unpacked_receiver_images}")
    cameras = tuple(obj for obj in scene.objects if obj.type == "CAMERA" and obj != camera)
    restore_camera = cameras[0] if cameras else None
    runs = [verify_engine(scene, camera, receivers, materials, restore_camera,
                          "BLENDER_EEVEE_NEXT", output),
            verify_engine(scene, camera, receivers, materials, restore_camera, "CYCLES", output)]
    source_hash_after = sha256_file(source)
    if source_hash_before != source_hash_after:
        raise AssertionError("Original office snapshot blend changed during regression")
    receipt: RegressionReceipt = {
        "status": "passed",
        "source_blend": str(source),
        "portable_blend": str(portable),
        "blender_version": bpy.app.version_string,
        "source_sha256_before": source_hash_before,
        "source_sha256_after": source_hash_after,
        "scripts_disabled_for_reopen": True,
        "packed_file_images": packed_images,
        "receiver_images_reloaded_after_reopen": reloaded_images,
        "receiver_objects": [obj.name for obj in receivers],
        "camera": camera.name,
        "restore_camera_sentinel": restore_camera.name if restore_camera is not None else None,
        "render_size": [640, 360],
        "samples": 16,
        "runs": runs,
        "boundary": "Offline per-frame host background snapshots; not native live SceneColor.",
    }
    (output / "regression_receipt.json").write_text(json.dumps(receipt, indent=2), encoding="utf-8")


if __name__ == "__main__":
    main()
