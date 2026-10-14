"""Verify the host screen-distortion node with rendered normals and an independent ray oracle."""
from __future__ import annotations

import argparse
import hashlib
import json
import math
from pathlib import Path
import sys
from typing import Callable, TypedDict

import bpy

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from blender_water_screen_distortion import (
    create_water_screen_distortion,
    water_camera_perspective,
    water_screen_distortion_group,
)


Vector3 = tuple[float, float, float]
Vector2 = tuple[float, float]


class DistortionCase(TypedDict):
    name: str
    wave: Vector3
    viewer: Vector3
    window: Vector2
    depth: float
    view_distance: float
    source_distortion: float
    ior: float
    perspective: float


def add(left: Vector3, right: Vector3) -> Vector3:
    return (left[0] + right[0], left[1] + right[1], left[2] + right[2])


def subtract(left: Vector3, right: Vector3) -> Vector3:
    return (left[0] - right[0], left[1] - right[1], left[2] - right[2])


def scale(vector: Vector3, amount: float) -> Vector3:
    return (vector[0] * amount, vector[1] * amount, vector[2] * amount)


def dot(left: Vector3, right: Vector3) -> float:
    return left[0] * right[0] + left[1] * right[1] + left[2] * right[2]


def length(vector: Vector3) -> float:
    return math.sqrt(dot(vector, vector))


def normalize(vector: Vector3) -> Vector3:
    magnitude = length(vector)
    if not math.isfinite(magnitude) or magnitude <= 1e-12:
        raise ValueError(f"Cannot normalize vector: {vector}")
    return scale(vector, 1.0 / magnitude)


def clamp(value: float) -> float:
    return min(1.0, max(0.0, value))


def source_fade(window: Vector2, view_distance: float, perspective: float) -> float:
    edge = min(1.0 - abs(2.0 * window[0] - 1.0),
               1.0 - abs(2.0 * window[1] - 1.0))
    far = clamp((0.19999998807907104 - edge) * 5.0)
    near = clamp(1.25 - edge)
    fade_distance = 1.0 + perspective * (view_distance - 1.0)
    near_weight = 1.0 - clamp((fade_distance - 0.75) * 4.0)
    return far + (near - far) * near_weight


def local_bottom_target(case: DistortionCase) -> tuple[Vector3 | None, bool, float]:
    viewer = normalize(case["viewer"])
    incident = scale(viewer, -1.0)
    depth = case["depth"]
    view_distance = case["view_distance"]
    fade = source_fade(case["window"], view_distance, case["perspective"])
    if (case["perspective"] not in (0.0, 1.0) or viewer[2] <= 0.0
            or view_distance <= 0.0 or depth < 0.0):
        return None, False, fade
    if depth == 0.0:
        target = incident
    else:
        wave = normalize((case["wave"][0] * case["source_distortion"],
                          case["wave"][1] * case["source_distortion"],
                          case["wave"][2]))
        horizontal = (wave[0] * (1.0 - fade), wave[1] * (1.0 - fade), 0.0)
        source_ray_depth = depth / viewer[2]
        projected_surface_point = scale(horizontal, -min(source_ray_depth, 0.5))
        if case["perspective"] == 0.0:
            parameter = (-depth - projected_surface_point[2]) / incident[2]
            bottom_hit = add(projected_surface_point, scale(incident, parameter))
        else:
            camera = scale(viewer, view_distance)
            denominator = projected_surface_point[2] - camera[2]
            if abs(denominator) <= 1e-12:
                return None, False, fade
            parameter = (-depth - camera[2]) / denominator
            bottom_hit = add(camera, scale(subtract(projected_surface_point, camera), parameter))
        target = normalize(bottom_hit)
    return target, dot(incident, target) > 1.0 / case["ior"], fade


def refract_air_to_water(incident: Vector3, normal: Vector3, ior: float) -> Vector3 | None:
    incident = normalize(incident)
    normal = normalize(normal)
    cosine = -dot(normal, incident)
    if cosine <= 0.0:
        return None
    eta = 1.0 / ior
    radicand = 1.0 - eta * eta * (1.0 - cosine * cosine)
    if radicand < -1e-8:
        return None
    result = add(scale(incident, eta),
                 scale(normal, eta * cosine - math.sqrt(max(0.0, radicand))))
    return normalize(result)


def diagnostic_cases() -> list[DistortionCase]:
    wave = normalize((0.31, -0.18, 0.934))
    view = normalize((0.29, -0.17, 0.94))
    return [
        {"name": "flat_perspective", "wave": (0.0, 0.0, 1.0), "viewer": (0.0, 0.0, 1.0),
         "window": (0.5, 0.5), "depth": 2.0, "view_distance": 5.0,
         "source_distortion": 0.8, "ior": 1.333, "perspective": 1.0},
        {"name": "perturbed_perspective", "wave": wave, "viewer": view,
         "window": (0.5, 0.5), "depth": 1.6, "view_distance": 5.0,
         "source_distortion": 0.7, "ior": 1.333, "perspective": 1.0},
        {"name": "zero_depth_straight_ray", "wave": wave, "viewer": view,
         "window": (0.5, 0.5), "depth": 0.0, "view_distance": 5.0,
         "source_distortion": 0.9, "ior": 1.333, "perspective": 1.0},
        {"name": "shallow_bottom", "wave": wave, "viewer": view,
         "window": (0.5, 0.5), "depth": 0.08, "view_distance": 5.0,
         "source_distortion": 0.7, "ior": 1.333, "perspective": 1.0},
        {"name": "deep_bottom", "wave": wave, "viewer": normalize((0.12, -0.08, 0.99)),
         "window": (0.5, 0.5), "depth": 8.0, "view_distance": 7.0,
         "source_distortion": 0.32, "ior": 1.333, "perspective": 1.0},
        {"name": "screen_edge_fade", "wave": wave, "viewer": view,
         "window": (0.02, 0.5), "depth": 1.6, "view_distance": 5.0,
         "source_distortion": 0.7, "ior": 1.333, "perspective": 1.0},
        {"name": "perspective_near_fade_full", "wave": wave, "viewer": view,
         "window": (0.05, 0.5), "depth": 1.6, "view_distance": 0.75,
         "source_distortion": 0.7, "ior": 1.333, "perspective": 1.0},
        {"name": "perspective_near_fade_blended", "wave": wave, "viewer": view,
         "window": (0.05, 0.5), "depth": 1.6, "view_distance": 0.875,
         "source_distortion": 0.7, "ior": 1.333, "perspective": 1.0},
        {"name": "orthographic_fade_near_distance", "wave": wave, "viewer": view,
         "window": (0.05, 0.5), "depth": 1.6, "view_distance": 0.75,
         "source_distortion": 0.7, "ior": 1.333, "perspective": 0.0},
        {"name": "orthographic_fade_far_distance", "wave": wave, "viewer": view,
         "window": (0.05, 0.5), "depth": 1.6, "view_distance": 20.0,
         "source_distortion": 0.7, "ior": 1.333, "perspective": 0.0},
        {"name": "orthographic_perturbed", "wave": wave, "viewer": view,
         "window": (0.5, 0.5), "depth": 1.6, "view_distance": 5.0,
         "source_distortion": 0.7, "ior": 1.333, "perspective": 0.0},
        {"name": "underwater_view_rejected", "wave": wave, "viewer": (0.6, 0.0, -0.8),
         "window": (0.5, 0.5), "depth": 1.0, "view_distance": 4.0,
         "source_distortion": 0.7, "ior": 1.333, "perspective": 1.0},
        {"name": "snell_branch_rejected", "wave": (1.0, 0.0, 0.0), "viewer": (0.0, 0.0, 1.0),
         "window": (0.5, 0.5), "depth": 0.01, "view_distance": 10.0,
         "source_distortion": 1.0, "ior": 1.333, "perspective": 0.0},
        {"name": "negative_depth_rejected", "wave": wave, "viewer": view,
         "window": (0.5, 0.5), "depth": -0.2, "view_distance": 5.0,
         "source_distortion": 0.7, "ior": 1.333, "perspective": 1.0},
        {"name": "zero_view_distance_rejected", "wave": wave, "viewer": view,
         "window": (0.5, 0.5), "depth": 1.0, "view_distance": 0.0,
         "source_distortion": 0.7, "ior": 1.333, "perspective": 1.0},
        {"name": "fractional_projection_rejected", "wave": wave, "viewer": view,
         "window": (0.5, 0.5), "depth": 1.0, "view_distance": 5.0,
         "source_distortion": 0.7, "ior": 1.333, "perspective": 0.5},
    ]


def create_material(case: DistortionCase, group: bpy.types.NodeTree,
                    index: int, output_kind: str) -> bpy.types.Material:
    material = bpy.data.materials.new(f"Screen distortion {index:02d} {output_kind}")
    material.use_nodes = True
    tree = material.node_tree
    if tree is None:
        raise RuntimeError(f"Node tree unavailable for {material.name}")
    tree.nodes.clear()
    group_node = tree.nodes.new("ShaderNodeGroup")
    group_node.node_tree = group
    for name, value in (
        ("Wave Normal", case["wave"]),
        ("To Viewer", case["viewer"]),
        ("Window", (case["window"][0], case["window"][1], 0.0)),
        ("Depth", case["depth"]),
        ("View Distance", case["view_distance"]),
        ("Source Distortion", case["source_distortion"]),
        ("Host IOR", case["ior"]),
        ("Host Perspective", case["perspective"]),
    ):
        group_node.inputs[name].default_value = value
    emission = tree.nodes.new("ShaderNodeEmission")
    output = tree.nodes.new("ShaderNodeOutputMaterial")
    if output_kind == "normal":
        half = tree.nodes.new("ShaderNodeVectorMath")
        half.operation = "SCALE"
        half.inputs["Scale"].default_value = 0.5
        tree.links.new(group_node.outputs["Transmission Normal"], half.inputs[0])
        encoded = tree.nodes.new("ShaderNodeVectorMath")
        encoded.operation = "ADD"
        encoded.inputs[1].default_value = (0.5, 0.5, 0.5)
        tree.links.new(half.outputs["Vector"], encoded.inputs[0])
        tree.links.new(encoded.outputs["Vector"], emission.inputs["Color"])
    elif output_kind == "valid":
        channels = tree.nodes.new("ShaderNodeCombineColor")
        channels.mode = "RGB"
        for channel in ("Red", "Green", "Blue"):
            tree.links.new(group_node.outputs["Valid Domain"], channels.inputs[channel])
        tree.links.new(channels.outputs["Color"], emission.inputs["Color"])
    else:
        raise ValueError(f"Unknown output kind: {output_kind}")
    tree.links.new(emission.outputs["Emission"], output.inputs["Surface"])
    return material


def constructor_inputs(name: str) -> tuple[bpy.types.Material, bpy.types.NodeSocket, bpy.types.NodeSocket]:
    material = bpy.data.materials.new(name)
    material.use_nodes = True
    tree = material.node_tree
    if tree is None:
        raise RuntimeError(f"Node tree unavailable for {name}")
    tree.nodes.clear()
    wave = tree.nodes.new("ShaderNodeCombineXYZ")
    depth = tree.nodes.new("ShaderNodeValue")
    return material, wave.outputs["Vector"], depth.outputs["Value"]


def require_value_error(label: str, call: Callable[[], object], expected_fragment: str) -> str:
    try:
        call()
    except ValueError as error:
        message = str(error)
        if expected_fragment not in message:
            raise AssertionError(f"{label} raised an unexpected error: {message}") from error
        return message
    raise AssertionError(f"{label} accepted invalid input")


def validate_camera_helpers(camera: bpy.types.Object) -> dict[str, object]:
    camera.data.type = "PERSP"
    perspective = water_camera_perspective(camera)
    camera.data.type = "ORTHO"
    orthographic = water_camera_perspective(camera)
    camera.data.type = "PANO"
    panorama_error = require_value_error(
        "panorama camera", lambda: water_camera_perspective(camera), "PERSP or ORTHO")
    camera.data.type = "ORTHO"
    mesh = bpy.data.meshes.new("Non-camera projection probe mesh")
    non_camera = bpy.data.objects.new("Non-camera projection probe", mesh)
    non_camera_error = require_value_error(
        "non-camera object", lambda: water_camera_perspective(non_camera), "PERSP or ORTHO")
    camera_free = water_camera_perspective(None)
    if (perspective, orthographic, camera_free) != (1.0, 0.0, 1.0):
        raise AssertionError(
            f"Unexpected camera projection mapping: {(perspective, orthographic, camera_free)}")
    return {
        "perspective_camera": perspective,
        "orthographic_camera": orthographic,
        "panorama_rejected_with": panorama_error,
        "non_camera_rejected_with": non_camera_error,
        "camera_free_export_default": camera_free,
    }


def validate_constructor(camera: bpy.types.Object) -> dict[str, object]:
    valid_modes: list[dict[str, object]] = []
    for camera_type, expected in (("PERSP", 1.0), ("ORTHO", 0.0)):
        camera.data.type = camera_type
        perspective = water_camera_perspective(camera)
        material, normal, depth = constructor_inputs(f"Constructor {camera_type} probe")
        node = create_water_screen_distortion(material, normal, depth, 0.7, 1.333, perspective)
        actual = float(node.inputs["Host Perspective"].default_value)
        if actual != expected or material.get("endfield_screen_distortion_contract") != "HostWaterScreenDistortion/2":
            raise AssertionError(
                f"Constructor did not preserve {camera_type} projection: {actual}")
        valid_modes.append({"camera_type": camera_type, "host_perspective": actual})

    bad_projection_material, bad_projection_normal, bad_projection_depth = constructor_inputs(
        "Constructor invalid projection probe")
    bad_projection = require_value_error(
        "fractional constructor projection",
        lambda: create_water_screen_distortion(
            bad_projection_material, bad_projection_normal, bad_projection_depth, 0.7, 1.333, 0.5),
        "projection must be 0")
    bad_ior_material, bad_ior_normal, bad_ior_depth = constructor_inputs("Constructor invalid IOR probe")
    bad_ior = require_value_error(
        "invalid constructor IOR",
        lambda: create_water_screen_distortion(
            bad_ior_material, bad_ior_normal, bad_ior_depth, 0.7, 1.0, 1.0),
        "IOR must exceed one")
    camera.data.type = "ORTHO"
    return {
        "accepted_modes": valid_modes,
        "fractional_projection_rejected_with": bad_projection,
        "invalid_ior_rejected_with": bad_ior,
    }


def make_scene(cases: list[DistortionCase]) -> tuple[
        list[tuple[bpy.types.Object, bpy.types.Material, bpy.types.Material]], int, int,
        dict[str, object]]:
    bpy.ops.wm.read_factory_settings(use_empty=True)
    scene = bpy.context.scene
    columns = 4
    rows = math.ceil(len(cases) / columns)
    cell_size = 64
    scene.render.resolution_x = columns * cell_size
    scene.render.resolution_y = rows * cell_size
    scene.render.resolution_percentage = 100
    scene.render.image_settings.file_format = "OPEN_EXR"
    scene.render.image_settings.color_mode = "RGB"
    scene.render.image_settings.color_depth = "32"
    scene.render.image_settings.exr_codec = "ZIP"
    scene.view_settings.view_transform = "Standard"
    scene.view_settings.look = "None"
    scene.view_settings.exposure = 0.0
    scene.view_settings.gamma = 1.0
    scene.render.film_transparent = False
    scene.cycles.samples = 1
    scene.cycles.use_denoising = False
    scene.eevee.taa_render_samples = 1

    camera_data = bpy.data.cameras.new("Numeric oracle camera")
    camera = bpy.data.objects.new("Numeric oracle camera", camera_data)
    scene.collection.objects.link(camera)
    camera.location = (0.0, 0.0, 10.0)
    camera.rotation_euler = (0.0, 0.0, 0.0)
    camera_data.type = "ORTHO"
    camera_data.ortho_scale = float(columns)
    scene.camera = camera
    camera_report = validate_camera_helpers(camera)
    constructor_report = validate_constructor(camera)
    camera_report["constructor"] = constructor_report
    camera.data.type = "ORTHO"

    group = water_screen_distortion_group()
    chart: list[tuple[bpy.types.Object, bpy.types.Material, bpy.types.Material]] = []
    for index, case in enumerate(cases):
        row = index // columns
        column = index % columns
        x = column - (columns - 1) / 2.0
        y = (rows - 1) / 2.0 - row
        bpy.ops.mesh.primitive_plane_add(size=0.8, location=(x, y, 0.0))
        obj = bpy.context.object
        obj.name = f"Case {index:02d} {case['name']}"
        normal_material = create_material(case, group, index, "normal")
        valid_material = create_material(case, group, index, "valid")
        obj.data.materials.append(normal_material)
        chart.append((obj, normal_material, valid_material))
    return chart, columns, cell_size, camera_report


def render_chart(output_dir: Path, cases: list[DistortionCase],
                 chart: list[tuple[bpy.types.Object, bpy.types.Material, bpy.types.Material]],
                 columns: int, cell_size: int, engine: str,
                 output_kind: str) -> list[Vector3]:
    scene = bpy.context.scene
    scene.render.engine = engine
    for obj, normal_material, valid_material in chart:
        obj.data.materials[0] = normal_material if output_kind == "normal" else valid_material
    image_path = output_dir / f"{engine.lower()}_{output_kind}.exr"
    scene.render.filepath = str(image_path)
    bpy.ops.render.render(write_still=True)
    image = bpy.data.images.load(str(image_path), check_existing=False)
    pixels = image.pixels
    width = image.size[0]
    height = image.size[1]
    samples: list[Vector3] = []
    rows = math.ceil(len(cases) / columns)
    for index in range(len(cases)):
        row = index // columns
        column = index % columns
        center_x = column * cell_size + cell_size // 2
        center_y = (rows - 1 - row) * cell_size + cell_size // 2
        channels: list[float] = []
        for channel in range(3):
            total = 0.0
            count = 0
            for y in range(center_y - 3, center_y + 3):
                for x in range(center_x - 3, center_x + 3):
                    if x < 0 or x >= width or y < 0 or y >= height:
                        raise AssertionError(f"Sample escaped image bounds: {(x, y)}")
                    total += float(pixels[4 * (y * width + x) + channel])
                    count += 1
            channels.append(total / count)
        samples.append((channels[0], channels[1], channels[2]))
    bpy.data.images.remove(image)
    return samples


def run(output_dir: Path) -> None:
    if bpy.app.version < (4, 4, 0):
        raise RuntimeError(f"Blender 4.4 or newer is required, got {bpy.app.version_string}")
    output_dir.mkdir(parents=True, exist_ok=False)
    cases = diagnostic_cases()
    chart, columns, cell_size, camera_report = make_scene(cases)
    module_path = Path(__file__).resolve().parents[1] / "src" / "blender_water_screen_distortion.py"
    module_hash = hashlib.sha256(module_path.read_bytes()).hexdigest()
    reports: list[dict[str, object]] = []
    for engine in ("BLENDER_EEVEE_NEXT", "CYCLES"):
        normal_samples = render_chart(output_dir, cases, chart, columns, cell_size, engine, "normal")
        valid_samples = render_chart(output_dir, cases, chart, columns, cell_size, engine, "valid")
        for index, case in enumerate(cases):
            encoded = normal_samples[index]
            actual_normal = (2.0 * encoded[0] - 1.0,
                             2.0 * encoded[1] - 1.0,
                             2.0 * encoded[2] - 1.0)
            normal_length_error = abs(length(actual_normal) - 1.0)
            target, expected_valid, fade = local_bottom_target(case)
            actual_valid = sum(valid_samples[index]) / 3.0
            valid_error = abs(actual_valid - float(expected_valid))
            if valid_error > 0.002:
                raise AssertionError(
                    f"Valid-domain mismatch engine={engine} case={case['name']} "
                    f"expected={expected_valid} actual={actual_valid}"
                )
            if normal_length_error > 0.003:
                raise AssertionError(
                    f"Rendered normal is not unit length engine={engine} case={case['name']} "
                    f"error={normal_length_error} encoded={encoded}"
                )
            refracted: Vector3 | None = None
            direction_error: float | None = None
            if target is not None and expected_valid:
                incident = scale(normalize(case["viewer"]), -1.0)
                refracted = refract_air_to_water(incident, actual_normal, case["ior"])
                if refracted is None:
                    raise AssertionError(
                        f"Rendered normal has no entering Snell solution engine={engine} "
                        f"case={case['name']} normal={actual_normal}"
                    )
                direction_error = max(abs(actual - expected)
                                      for actual, expected in zip(refracted, target, strict=True))
                tolerance = 0.003 if engine == "BLENDER_EEVEE_NEXT" else 0.0005
                if direction_error > tolerance:
                    raise AssertionError(
                        f"Snell direction mismatch engine={engine} case={case['name']} "
                        f"error={direction_error} target={target} refracted={refracted} "
                        f"normal={actual_normal}"
                    )
            reports.append({
                "engine": engine,
                "case": case["name"],
                "source_fade": fade,
                "expected_valid_domain": expected_valid,
                "rendered_valid_domain": actual_valid,
                "valid_domain_error": valid_error,
                "expected_geometric_transmission": target,
                "rendered_transmission_normal": actual_normal,
                "rendered_normal_length_error": normal_length_error,
                "snell_refracted_direction": refracted,
                "snell_direction_max_component_error": direction_error,
            })
    receipt = {
        "format": "EndfieldWaterScreenDistortionNumericRegression/1",
        "status": "passed",
        "blender_version": bpy.app.version_string,
        "production_module": str(module_path),
        "production_module_sha256": module_hash,
        "rendered_engines": ["BLENDER_EEVEE_NEXT", "CYCLES"],
        "case_count": len(cases),
        "camera_projection_helper": camera_report,
        "oracle": (
            "Independent surface-point to local-bottom projection geometry; rendered normal is "
            "validated by Snell refraction against the geometric target direction."
        ),
        "cases": reports,
        "boundary": (
            "Host approximation only. Does not establish native depth-buffer sampling, source GPU "
            "screen taps, arbitrary-bottom intersections, or runtime draw admission."
        ),
    }
    (output_dir / "validation.json").write_text(json.dumps(receipt, indent=2), encoding="utf-8")
    print("WATER_SCREEN_DISTORTION_REGRESSION_PASS", len(reports),
          max(row["snell_direction_max_component_error"] or 0.0 for row in reports))


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", type=Path, required=True)
    arguments = parser.parse_args(sys.argv[sys.argv.index("--") + 1:])
    run(arguments.output)
