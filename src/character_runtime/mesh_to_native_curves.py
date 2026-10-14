"""Create a deterministic PCA centerline proxy from mesh islands in Blender."""

from __future__ import annotations

import argparse
import hashlib
import json
import sys
import time
from array import array
from collections import deque
from pathlib import Path
from typing import Sequence

import bpy
import numpy as np


def parse_arguments(argv: Sequence[str]) -> tuple[Path, str, str, Path, int, float, int, Path, Path | None, float]:
    values = list(argv)
    if "--" in values:
        values = values[values.index("--") + 1 :]
    else:
        values = values[1:]
    parser = argparse.ArgumentParser()
    parser.add_argument("--blend", type=Path, required=True)
    parser.add_argument("--mesh-name", required=True)
    parser.add_argument("--label", required=True)
    parser.add_argument("--output-dir", type=Path, required=True)
    parser.add_argument("--point-count", type=int, required=True)
    parser.add_argument("--radius-scale", type=float, required=True)
    parser.add_argument("--render-size", type=int, required=True)
    parser.add_argument("--color-image-path", type=Path, required=True)
    parser.add_argument("--mask-image-path", required=True)
    parser.add_argument("--mask-threshold", type=float, required=True)
    arguments = parser.parse_args(values)
    return (
        arguments.blend.resolve(),
        arguments.mesh_name,
        arguments.label,
        arguments.output_dir.resolve(),
        arguments.point_count,
        arguments.radius_scale,
        arguments.render_size,
        arguments.color_image_path.resolve(),
        None if arguments.mask_image_path.upper() == "NONE" else Path(arguments.mask_image_path).resolve(),
        arguments.mask_threshold,
    )


def read_mesh_components(mesh: bpy.types.Mesh) -> tuple[tuple[int, ...], ...]:
    adjacency: list[list[int]] = [[] for _ in mesh.vertices]
    for edge in mesh.edges:
        left, right = edge.vertices
        adjacency[left].append(right)
        adjacency[right].append(left)
    visited = bytearray(len(mesh.vertices))
    components: list[tuple[int, ...]] = []
    for start in range(len(mesh.vertices)):
        if visited[start]:
            continue
        visited[start] = 1
        queue: deque[int] = deque((start,))
        component: list[int] = []
        while queue:
            vertex_index = queue.popleft()
            component.append(vertex_index)
            for neighbor in adjacency[vertex_index]:
                if not visited[neighbor]:
                    visited[neighbor] = 1
                    queue.append(neighbor)
        components.append(tuple(component))
    return tuple(components)


def read_vertex_uvs(mesh: bpy.types.Mesh) -> np.ndarray | None:
    if not mesh.uv_layers:
        return None
    layer = mesh.uv_layers.active
    if layer is None:
        return None
    uv_sums = np.zeros((len(mesh.vertices), 2), dtype=np.float64)
    uv_counts = np.zeros(len(mesh.vertices), dtype=np.float64)
    for loop in mesh.loops:
        uv_sums[loop.vertex_index] += np.asarray(layer.data[loop.index].uv, dtype=np.float64)
        uv_counts[loop.vertex_index] += 1.0
    valid = uv_counts > 0.0
    uv_sums[valid] /= uv_counts[valid, None]
    return uv_sums


def load_source_image(image_path: Path, color_space: str, minimum_channels: int) -> bpy.types.Image:
    if not image_path.is_file():
        raise FileNotFoundError(f"Source texture image does not exist: {image_path}")
    image = bpy.data.images.load(str(image_path), check_existing=False)
    if image.size[0] <= 0 or image.size[1] <= 0 or image.channels < minimum_channels:
        raise ValueError(
            f"Source texture image cannot supply {minimum_channels} channels: "
            f"{image_path} has {image.size[0]}x{image.size[1]}x{image.channels}"
        )
    image.colorspace_settings.name = color_space
    return image


def image_pixel_array(image: bpy.types.Image) -> np.ndarray:
    width = int(image.size[0])
    height = int(image.size[1])
    channels = int(image.channels)
    flat = np.empty(width * height * channels, dtype=np.float32)
    image.pixels.foreach_get(flat)
    pixels = flat.reshape((height, width, channels))
    if channels == 4:
        return pixels
    alpha = np.ones((height, width, 1), dtype=np.float32)
    return np.concatenate((pixels[:, :, :3], alpha), axis=2)


def sample_image_pixels(pixels: np.ndarray, uvs: np.ndarray) -> np.ndarray:
    if pixels.ndim != 3 or pixels.shape[2] != 4:
        raise ValueError("Image pixels must have four channels")
    if uvs.ndim != 2 or uvs.shape[1] != 2:
        raise ValueError("UV coordinates must have shape (point_count, 2)")
    height, width, _ = pixels.shape
    wrapped = np.mod(uvs, 1.0)
    x = wrapped[:, 0] * float(width - 1)
    y = wrapped[:, 1] * float(height - 1)
    x0 = np.floor(x).astype(np.int64)
    y0 = np.floor(y).astype(np.int64)
    x1 = np.minimum(x0 + 1, width - 1)
    y1 = np.minimum(y0 + 1, height - 1)
    tx = (x - x0)[:, None]
    ty = (y - y0)[:, None]
    lower = pixels[y0, x0] * (1.0 - tx) + pixels[y0, x1] * tx
    upper = pixels[y1, x0] * (1.0 - tx) + pixels[y1, x1] * tx
    return lower * (1.0 - ty) + upper * ty


def make_sampled_curve_material(name: str) -> bpy.types.Material:
    material = bpy.data.materials.new(name)
    material.use_nodes = True
    if material.node_tree is None:
        raise RuntimeError(f"Material node tree could not be created: {name}")
    tree = material.node_tree
    tree.nodes.clear()
    output = tree.nodes.new("ShaderNodeOutputMaterial")
    shader = tree.nodes.new("ShaderNodeBsdfPrincipled")
    color_attribute = tree.nodes.new("ShaderNodeAttribute")
    color_attribute.attribute_name = "hair_source_color"
    alpha_attribute = tree.nodes.new("ShaderNodeAttribute")
    alpha_attribute.attribute_name = "hair_source_alpha"
    tree.links.new(color_attribute.outputs["Color"], shader.inputs["Base Color"])
    tree.links.new(alpha_attribute.outputs["Fac"], shader.inputs["Alpha"])
    tree.links.new(shader.outputs["BSDF"], output.inputs["Surface"])
    shader.inputs["Roughness"].default_value = 0.62
    if hasattr(material, "surface_render_method"):
        material.surface_render_method = "DITHERED"
    return material


def sample_component_centerline(
    component_positions: np.ndarray,
    component_uvs: np.ndarray | None,
    point_count: int,
    radius_scale: float,
) -> tuple[np.ndarray, np.ndarray, np.ndarray | None]:
    if len(component_positions) < 2:
        raise ValueError("A mesh island needs at least two vertices to produce a curve")
    center = component_positions.mean(axis=0)
    centered = component_positions - center
    covariance = centered.T @ centered / float(len(component_positions))
    eigenvalues, eigenvectors = np.linalg.eigh(covariance)
    axis = eigenvectors[:, int(np.argmax(eigenvalues))]
    dominant_axis = int(np.argmax(np.abs(axis)))
    if axis[dominant_axis] < 0.0:
        axis = -axis
    projection = centered @ axis
    minimum = float(projection.min())
    maximum = float(projection.max())
    if maximum - minimum <= 1.0e-8:
        raise ValueError("A mesh island has no measurable principal-axis extent")
    full_radial_offsets = centered - np.outer(projection, axis)
    full_cross_section_radius = float(np.percentile(np.linalg.norm(full_radial_offsets, axis=1), 90.0))
    boundaries = np.linspace(minimum, maximum, point_count + 1)
    centerline = np.zeros((point_count, 3), dtype=np.float64)
    radii = np.zeros(point_count, dtype=np.float64)
    uv_samples = np.zeros((point_count, 2), dtype=np.float64) if component_uvs is not None else None
    for sample_index in range(point_count):
        lower = boundaries[sample_index]
        upper = boundaries[sample_index + 1]
        if sample_index == point_count - 1:
            selected = (projection >= lower) & (projection <= upper)
        else:
            selected = (projection >= lower) & (projection < upper)
        if not np.any(selected):
            target = (lower + upper) * 0.5
            nearest = int(np.argmin(np.abs(projection - target)))
            selected[nearest] = True
        sample_positions = component_positions[selected]
        sample_center = sample_positions.mean(axis=0)
        centerline[sample_index] = sample_center
        radial_offsets = sample_positions - sample_center
        radial_offsets -= np.outer(radial_offsets @ axis, axis)
        radius = float(np.percentile(np.linalg.norm(radial_offsets, axis=1), 90.0))
        radii[sample_index] = max(radius, full_cross_section_radius) * radius_scale
        radii[sample_index] = max(radii[sample_index], 1.0e-5)
        if uv_samples is not None and component_uvs is not None:
            uv_samples[sample_index] = component_uvs[selected].mean(axis=0)
    return centerline, radii, uv_samples


def build_mesh_component_curve_proxy(
    source_object: bpy.types.Object,
    source_mesh: bpy.types.Mesh,
    target_collection: bpy.types.Collection,
    component_indices: tuple[int, ...],
    point_count: int,
    radius_scale: float,
    color_image: bpy.types.Image,
    mask_image: bpy.types.Image | None,
    mask_threshold: float,
    object_name: str,
) -> tuple[bpy.types.Object, dict[str, object]]:
    if source_object.type != "MESH":
        raise TypeError(f"Expected a mesh source object, got {source_object.type!r}")
    if point_count < 2:
        raise ValueError("point_count must be at least two")
    if radius_scale <= 0.0:
        raise ValueError("radius_scale must be positive")
    if mask_threshold < 0.0 or mask_threshold > 1.0:
        raise ValueError("mask_threshold must be in [0, 1]")
    components = read_mesh_components(source_mesh)
    if not component_indices:
        raise ValueError("At least one component index must be selected")
    if len(set(component_indices)) != len(component_indices):
        raise ValueError("component_indices must not contain duplicates")
    if any(index < 0 or index >= len(components) for index in component_indices):
        raise IndexError(f"A selected component index is outside 0..{len(components) - 1}")
    vertex_positions = np.empty((len(source_mesh.vertices), 3), dtype=np.float64)
    source_mesh.vertices.foreach_get("co", vertex_positions.ravel())
    vertex_uvs = read_vertex_uvs(source_mesh)
    paths: list[np.ndarray] = []
    radii: list[np.ndarray] = []
    uv_paths: list[np.ndarray] = []
    skipped_components: list[int] = []
    for component_index in component_indices:
        vertex_indices = np.asarray(components[component_index], dtype=np.int64)
        component_positions = vertex_positions[vertex_indices]
        component_uvs = vertex_uvs[vertex_indices] if vertex_uvs is not None else None
        try:
            centerline, curve_radii, uv_samples = sample_component_centerline(
                component_positions,
                component_uvs,
                point_count,
                radius_scale,
            )
        except ValueError:
            skipped_components.append(component_index)
            continue
        paths.append(centerline)
        radii.append(curve_radii)
        if uv_samples is not None:
            uv_paths.append(uv_samples)
    if not paths:
        raise ValueError("No selected mesh islands produced a usable centerline")
    if len(uv_paths) != len(paths):
        raise ValueError("Source mesh has no usable UV coordinates for texture sampling")
    all_uvs = np.concatenate(uv_paths, axis=0).astype(np.float32, copy=False)
    sampled_colors = sample_image_pixels(image_pixel_array(color_image), all_uvs)
    if mask_image is None:
        sampled_alpha = np.ones(len(all_uvs), dtype=np.float32)
        mask_thresholded = sampled_alpha.copy()
        source_mask_pixel_visible_fraction = None
    else:
        if mask_image.channels < 4:
            raise ValueError(f"Mask image has no alpha channel: {mask_image.name}")
        mask_pixels = image_pixel_array(mask_image)
        source_mask_alpha = mask_pixels[:, :, 3]
        source_mask_pixel_visible_fraction = float(np.mean(source_mask_alpha >= mask_threshold))
        sampled_alpha = sample_image_pixels(mask_pixels, all_uvs)[:, 3]
        mask_thresholded = (sampled_alpha >= mask_threshold).astype(np.float32)
    curves_data = bpy.data.hair_curves.new(object_name + " Data")
    curves_data.add_curves([point_count] * len(paths))
    positions = np.concatenate(paths, axis=0).astype(np.float32, copy=False)
    radii_values = np.concatenate(radii, axis=0).astype(np.float32, copy=False)
    curves_data.attributes["position"].data.foreach_set("vector", array("f", positions.ravel()))
    radius_attribute = curves_data.attributes.get("radius")
    if radius_attribute is None:
        radius_attribute = curves_data.attributes.new("radius", "FLOAT", "POINT")
    radius_attribute.data.foreach_set("value", array("f", radii_values))
    uv_attribute = curves_data.attributes.new("UVMap", "FLOAT2", "POINT")
    uv_attribute.data.foreach_set("vector", array("f", all_uvs.ravel()))
    color_attribute = curves_data.attributes.new("hair_source_color", "FLOAT_COLOR", "POINT")
    color_attribute.data.foreach_set("color", array("f", sampled_colors.ravel()))
    alpha_attribute = curves_data.attributes.new("hair_source_alpha", "FLOAT", "POINT")
    alpha_attribute.data.foreach_set("value", array("f", mask_thresholded))
    curves_data.materials.append(make_sampled_curve_material(object_name + " Source Map Material"))
    curve_object = bpy.data.objects.new(object_name, curves_data)
    target_collection.objects.link(curve_object)
    curve_object.matrix_world = source_object.matrix_world.copy()
    report: dict[str, object] = {
        "proxy_object_type": curve_object.type,
        "source_object": source_object.name,
        "source_mesh": source_mesh.name,
        "mesh_component_count": len(components),
        "selected_component_count": len(component_indices),
        "curve_count": len(paths),
        "points_per_curve": point_count,
        "radius_scale": radius_scale,
        "skipped_component_indices": skipped_components,
        "has_source_uv_attribute": True,
        "source_color_image": color_image.name,
        "source_mask_image": mask_image.name if mask_image is not None else None,
        "mask_threshold": mask_threshold if mask_image is not None else None,
        "sampled_color_linear_mean": [float(value) for value in sampled_colors[:, :3].mean(axis=0)],
        "sampled_color_linear_min": [float(value) for value in sampled_colors[:, :3].min(axis=0)],
        "sampled_color_linear_max": [float(value) for value in sampled_colors[:, :3].max(axis=0)],
        "sampled_alpha_mean_before_threshold": float(sampled_alpha.mean()),
        "sampled_visible_fraction_after_threshold": float(mask_thresholded.mean()),
        "source_mask_pixel_visible_fraction": source_mask_pixel_visible_fraction,
        "direction_method": "sign-canonicalized PCA major axis; root and tip are not inferred",
        "shape_method": "piecewise centroid samples and 90th-percentile cross-section radii from each connected mesh island",
        "color_method": "source color image bilinearly sampled at source UV values interpolated along each centerline",
        "mask_method": "source alpha is sampled and thresholded only along each centerline; the card's full 2D alpha footprint is not represented",
    }
    return curve_object, report


def render_still(scene: bpy.types.Scene, output_path: Path) -> float:
    scene.render.filepath = str(output_path)
    started = time.perf_counter()
    result = bpy.ops.render.render(write_still=True)
    elapsed = time.perf_counter() - started
    if "FINISHED" not in result or not output_path.is_file():
        raise RuntimeError(f"Blender did not write render output: {output_path}")
    return elapsed


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as source:
        while block := source.read(8 * 1024 * 1024):
            digest.update(block)
    return digest.hexdigest()


def main(argv: Sequence[str]) -> None:
    source_blend, mesh_name, label, output_dir, point_count, radius_scale, render_size, color_image_path, mask_image_path, mask_threshold = parse_arguments(argv)
    if not source_blend.is_file():
        raise FileNotFoundError(f"Source blend not found: {source_blend}")
    if render_size < 128:
        raise ValueError("render_size must be at least 128 pixels")
    output_dir.mkdir(parents=True, exist_ok=True)
    if not bpy.ops.wm.open_mainfile(filepath=str(source_blend)):
        raise RuntimeError(f"Could not open source blend: {source_blend}")
    source_object = bpy.data.objects.get(mesh_name)
    if source_object is None:
        raise KeyError(f"Mesh object not found in blend: {mesh_name}")
    if source_object.type != "MESH":
        raise TypeError(f"Named source object is not a mesh: {mesh_name}")
    color_image = load_source_image(color_image_path, "sRGB", 3)
    mask_image = None if mask_image_path is None else load_source_image(mask_image_path, "Non-Color", 4)
    scene = bpy.context.scene
    if scene.camera is None:
        raise RuntimeError("Source scene has no active camera")
    depsgraph = bpy.context.evaluated_depsgraph_get()
    evaluated_object = source_object.evaluated_get(depsgraph)
    evaluated_mesh = evaluated_object.to_mesh(preserve_all_data_layers=True, depsgraph=depsgraph)
    try:
        proxy_collection = bpy.data.collections.new(label + " Hair Curve Proxy - PCA Inference")
        scene.collection.children.link(proxy_collection)
        component_indices = tuple(range(len(read_mesh_components(evaluated_mesh))))
        proxy_object, proxy_report = build_mesh_component_curve_proxy(
            source_object,
            evaluated_mesh,
            proxy_collection,
            component_indices,
            point_count,
            radius_scale,
            color_image,
            mask_image,
            mask_threshold,
            label + " Native Curves - PCA Proxy",
        )
    finally:
        evaluated_object.to_mesh_clear()
    color_image.pack()
    if mask_image is not None:
        mask_image.pack()
    proxy_collection.hide_render = False
    proxy_object.hide_render = True
    scene.render.engine = "CYCLES"
    scene.render.resolution_x = render_size
    scene.render.resolution_y = int(round(render_size * 4.0 / 3.0))
    scene.render.resolution_percentage = 100
    scene.cycles.samples = min(scene.cycles.samples, 24)
    scene.cycles.use_denoising = True
    scene.render.image_settings.file_format = "PNG"
    scene.render.film_transparent = False
    original_path = output_dir / f"{label.lower()}_original_mesh.png"
    proxy_path = output_dir / f"{label.lower()}_native_curve_proxy.png"
    source_obj_original_hidden = source_object.hide_render
    proxy_original_hidden = proxy_object.hide_render
    collection_original_hidden = proxy_collection.hide_render
    source_object.hide_render = False
    proxy_object.hide_render = True
    original_seconds = render_still(scene, original_path)
    source_object.hide_render = True
    proxy_collection.hide_render = False
    proxy_object.hide_render = False
    proxy_seconds = render_still(scene, proxy_path)
    source_object.hide_render = source_obj_original_hidden
    proxy_object.hide_render = proxy_original_hidden
    proxy_collection.hide_render = True
    blend_path = output_dir / f"{label.lower()}_native_curve_proxy.blend"
    bpy.ops.wm.save_as_mainfile(filepath=str(blend_path))
    source_hash = sha256_file(source_blend)
    report = {
        "format": "EndfieldHairNativeCurveExperiment/1",
        "label": label,
        "blender_version": bpy.app.version_string,
        "source_blend": str(source_blend),
        "source_blend_sha256": source_hash,
        "source_color_image": {
            "path": str(color_image_path),
            "name": color_image.name,
            "sha256": sha256_file(color_image_path),
            "colorspace": color_image.colorspace_settings.name,
        },
        "source_mask_image": None if mask_image is None else {
            "path": str(mask_image_path),
            "name": mask_image.name,
            "sha256": sha256_file(mask_image_path),
            "colorspace": mask_image.colorspace_settings.name,
        },
        "source_object": mesh_name,
        "saved_proxy_blend": str(blend_path),
        "proxy_collection": proxy_collection.name,
        "saved_default_state": "original mesh visible; proxy collection hidden from render",
        "native_curves_rendered_in": "Cycles",
        "render_resolution": [render_size, int(round(render_size * 4.0 / 3.0))],
        "original_render": {"path": str(original_path), "seconds": original_seconds},
        "proxy_render": {"path": str(proxy_path), "seconds": proxy_seconds},
        "proxy": proxy_report,
        "limitations": [
            "The proxy is a static geometry approximation and has no armature deformation weights.",
            "Per-island PCA axes are geometric estimates, not source root-to-tip strand guides.",
            "Only centerline UV samples are carried; source card silhouette and 2D alpha masks are not preserved.",
            "Original source mesh remains in the saved blend and can be restored by enabling its collection and hiding the proxy collection.",
        ],
    }
    report_path = output_dir / f"{label.lower()}_experiment.json"
    report_path.write_text(json.dumps(report, indent=2), encoding="utf-8")
    print(json.dumps({"report": str(report_path), "blend": str(blend_path), "original": str(original_path), "proxy": str(proxy_path), "curves": proxy_report["curve_count"]}, indent=2))


if __name__ == "__main__":
    main(sys.argv)
