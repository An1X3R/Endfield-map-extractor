"""Render source and portable water before and after host foam presentation controls."""
from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
import sys

import bpy

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from blender_scene_water_foam import reconnect_source_water_foam


_ENGINES = (("eevee", "BLENDER_EEVEE_NEXT"), ("cycles", "CYCLES"))
_READABILITY_NODE = "EF_Water_Host_Foam_Readability"
_TINT_NODE = "EF_Water_Host_Foam_Tint"


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _foam_materials() -> list[bpy.types.Material]:
    water_objects = [
        obj for obj in bpy.context.scene.objects
        if obj.type == "MESH" and obj.get("endfield_category") == "water"
    ]
    materials = {
        material.as_pointer(): material
        for obj in water_objects
        for material in obj.data.materials
        if material is not None
        and material.get("endfield_water_foam_status") == "source_mask_host_surface"
    }
    if not materials:
        raise AssertionError("Review blend has no water material with source foam")
    return sorted(materials.values(), key=lambda material: material.name.casefold())


def _render(output_path: Path, engine: str) -> dict[str, object]:
    scene = bpy.context.scene
    if scene.camera is None:
        raise AssertionError(f"Review scene has no active camera: engine={engine}")
    scene.render.engine = engine
    scene.render.image_settings.file_format = "PNG"
    scene.render.filepath = str(output_path)
    scene.render.use_file_extension = True
    bpy.ops.render.render(write_still=True)
    if not output_path.exists():
        raise AssertionError(f"Blender did not write the requested render: path={output_path}")
    return {
        "path": str(output_path),
        "sha256": _sha256(output_path),
        "width": scene.render.resolution_x * scene.render.resolution_percentage // 100,
        "height": scene.render.resolution_y * scene.render.resolution_percentage // 100,
    }


def _render_engines(directory: Path, stem: str) -> dict[str, dict[str, object]]:
    renders: dict[str, dict[str, object]] = {}
    for short_name, engine in _ENGINES:
        renders[short_name] = _render(directory / f"{stem}_{short_name}.png", engine)
    return renders


def _readability_values(materials: list[bpy.types.Material]) -> list[float]:
    values: list[float] = []
    for material in materials:
        node = material.node_tree.nodes.get(_READABILITY_NODE)
        if node is None or node.bl_idname != "ShaderNodeValue":
            raise AssertionError(f"Saved material is missing its readability control: material={material.name}")
        values.append(float(node.outputs[0].default_value))
    return values


def _socket_value(socket: bpy.types.NodeSocket) -> object:
    try:
        value = socket.default_value
    except AttributeError:
        return None
    if isinstance(value, (bool, int, float, str)):
        return value
    try:
        return [float(channel) for channel in value]
    except TypeError:
        return str(value)


def _source_mask_signature(material: bpy.types.Material) -> str:
    tree = material.node_tree
    coverage = tree.nodes.get("EF_Water_Foam_Coverage")
    if coverage is None:
        raise AssertionError(f"Source foam coverage is missing: material={material.name}")
    pending = [coverage]
    visited: dict[int, bpy.types.Node] = {}
    while pending:
        node = pending.pop()
        pointer = node.as_pointer()
        if pointer in visited:
            continue
        visited[pointer] = node
        pending.extend(
            link.from_node
            for socket in node.inputs
            for link in socket.links
        )
    rows: list[dict[str, object]] = []
    for node in sorted(visited.values(), key=lambda current: current.name.casefold()):
        row: dict[str, object] = {
            "name": node.name,
            "type": node.bl_idname,
            "inputs": [
                {
                    "name": socket.name,
                    "default": _socket_value(socket),
                    "links": sorted(
                        (link.from_node.name, link.from_socket.name)
                        for link in socket.links
                    ),
                }
                for socket in node.inputs
            ],
        }
        for property_name in ("operation", "mode", "blend_type", "extension", "interpolation"):
            property_value = getattr(node, property_name, None)
            if isinstance(property_value, (bool, int, float, str)):
                row[property_name] = property_value
        rows.append(row)
    serialized = json.dumps(rows, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(serialized.encode("utf-8")).hexdigest()


def _source_foam_values(material: bpy.types.Material) -> dict[str, object]:
    foam = material.node_tree.nodes.get("EF_Water_Foam_Surface")
    if foam is None:
        raise AssertionError(f"Source foam surface is missing: material={material.name}")
    result: dict[str, object] = {}
    for socket_name in ("Base Color", "Roughness"):
        socket = foam.inputs.get(socket_name)
        if socket is None:
            raise AssertionError(f"Source foam input is missing: material={material.name}, input={socket_name}")
        result[socket_name] = {
            "default": _socket_value(socket),
            "links": sorted(
                (link.from_node.name, link.from_socket.name)
                for link in socket.links
            ),
        }
    return result


def _set_readability(materials: list[bpy.types.Material], value: float) -> None:
    for material in materials:
        nodes = material.node_tree.nodes
        readability = nodes.get(_READABILITY_NODE)
        tint = nodes.get(_TINT_NODE)
        if readability is None or tint is None:
            raise AssertionError(f"Foam presentation controls are incomplete: material={material.name}")
        readability.outputs[0].default_value = value
        tint.outputs[0].default_value = (1.0, 0.96, 0.84, 1.0)


def _pixel_comparison(first_path: Path, second_path: Path) -> dict[str, object]:
    first = bpy.data.images.load(str(first_path), check_existing=False)
    second = bpy.data.images.load(str(second_path), check_existing=False)
    try:
        if first.size[:] != second.size[:]:
            raise AssertionError(f"Render dimensions differ: first={first.size[:]}, second={second.size[:]}")
        first_values = first.pixels[:]
        second_values = second.pixels[:]
        if len(first_values) != len(second_values) or len(first_values) % 4:
            raise AssertionError("Rendered pixel arrays have inconsistent lengths")
        total_absolute_difference = 0.0
        maximum_absolute_difference = 0.0
        changed_pixels = 0
        for offset in range(0, len(first_values), 4):
            pixel_changed = False
            for channel in range(3):
                difference = abs(float(first_values[offset + channel]) - float(second_values[offset + channel]))
                total_absolute_difference += difference
                maximum_absolute_difference = max(maximum_absolute_difference, difference)
                pixel_changed = pixel_changed or difference > 1e-7
            changed_pixels += int(pixel_changed)
        channel_count = (len(first_values) // 4) * 3
        return {
            "changed_rgb_pixels": changed_pixels,
            "mean_absolute_rgb_difference": total_absolute_difference / channel_count,
            "maximum_absolute_rgb_difference": maximum_absolute_difference,
        }
    finally:
        bpy.data.images.remove(first)
        bpy.data.images.remove(second)


def _verify_recipe(receipt_path: Path, sample_path: Path) -> dict[str, object]:
    receipt = json.loads(receipt_path.read_text(encoding="utf-8"))
    recorded_blend = Path(receipt["review_blend"]).resolve()
    if recorded_blend != sample_path.resolve():
        raise AssertionError(f"Review blend does not match its recipe receipt: receipt={receipt_path}")
    plan_path = Path(receipt["plan"])
    if not plan_path.is_file():
        raise AssertionError(f"Recipe plan is missing: path={plan_path}")
    return {
        "receipt": str(receipt_path),
        "plan": str(plan_path),
        "plan_sha256": receipt.get("plan_sha256"),
        "review_blend_sha256": receipt.get("review_sha256"),
    }


def _exercise_sample(
    sample_name: str,
    original_path: Path,
    receipt_path: Path,
    output_directory: Path,
) -> dict[str, object]:
    recipe = _verify_recipe(receipt_path, original_path)
    sample_directory = output_directory / sample_name
    sample_directory.mkdir(parents=True, exist_ok=True)

    bpy.ops.wm.open_mainfile(filepath=str(original_path), use_scripts=False)
    materials = _foam_materials()
    baseline = _render_engines(sample_directory, "baseline")

    materials = _foam_materials()
    source_state_before = {
        material.name: {
            "coverage_signature": _source_mask_signature(material),
            "foam_values": _source_foam_values(material),
        }
        for material in materials
    }
    for material in materials:
        reconnect_source_water_foam(material)
    source_state_after = {
        material.name: {
            "coverage_signature": _source_mask_signature(material),
            "foam_values": _source_foam_values(material),
        }
        for material in materials
    }
    if source_state_before != source_state_after:
        raise AssertionError(f"Host presentation changed source foam data: sample={sample_name}")
    neutral_before_save = _render_engines(sample_directory, "neutral_before_save")
    bpy.ops.file.pack_all()
    neutral_path = sample_directory / f"{sample_name}_host_foam_neutral.blend"
    bpy.ops.wm.save_as_mainfile(filepath=str(neutral_path))

    bpy.ops.wm.open_mainfile(filepath=str(neutral_path), use_scripts=False)
    materials = _foam_materials()
    neutral_values = _readability_values(materials)
    if any(value != 0.0 for value in neutral_values):
        raise AssertionError(f"Saved/reopened neutral controls changed value: sample={sample_name}, values={neutral_values}")
    neutral = _render_engines(sample_directory, "neutral")
    neutral_before_save_comparisons = {
        engine_name: _pixel_comparison(
            Path(baseline[engine_name]["path"]),
            Path(neutral_before_save[engine_name]["path"]),
        )
        for engine_name, _engine in _ENGINES
    }
    neutral_comparisons = {
        engine_name: _pixel_comparison(
            Path(baseline[engine_name]["path"]), Path(neutral[engine_name]["path"])
        )
        for engine_name, _engine in _ENGINES
    }
    if any(result["changed_rgb_pixels"] != 0 for result in neutral_comparisons.values()):
        raise AssertionError(
            f"Neutral host foam changed rendered pixels: sample={sample_name}, "
            f"before_save={neutral_before_save_comparisons}, after_reopen={neutral_comparisons}"
        )

    _set_readability(materials, 0.75)
    enhanced_path = sample_directory / f"{sample_name}_host_foam_enhanced.blend"
    bpy.ops.wm.save_as_mainfile(filepath=str(enhanced_path))
    bpy.ops.wm.open_mainfile(filepath=str(enhanced_path), use_scripts=False)
    materials = _foam_materials()
    enhanced_values = _readability_values(materials)
    if any(abs(value - 0.75) > 1e-7 for value in enhanced_values):
        raise AssertionError(f"Saved/reopened enhanced controls changed value: sample={sample_name}, values={enhanced_values}")
    enhanced = _render_engines(sample_directory, "enhanced")
    enhancement_comparisons = {
        engine_name: _pixel_comparison(
            Path(neutral[engine_name]["path"]), Path(enhanced[engine_name]["path"])
        )
        for engine_name, _engine in _ENGINES
    }
    if any(result["changed_rgb_pixels"] == 0 for result in enhancement_comparisons.values()):
        raise AssertionError(f"Enhanced host foam did not affect either renderer: sample={sample_name}")
    return {
        "sample": sample_name,
        "original_blend": str(original_path),
        "original_sha256": _sha256(original_path),
        "recipe": recipe,
        "water_materials": [material.name for material in materials],
        "saved_neutral_blend": str(neutral_path),
        "saved_enhanced_blend": str(enhanced_path),
        "neutral_values_after_reopen": neutral_values,
        "enhanced_values_after_reopen": enhanced_values,
        "source_state_preserved": source_state_before == source_state_after,
        "source_state": source_state_before,
        "renders": {
            "baseline": baseline,
            "neutral_before_save": neutral_before_save,
            "neutral": neutral,
            "enhanced": enhanced,
        },
        "baseline_vs_neutral_before_save": neutral_before_save_comparisons,
        "baseline_vs_neutral": neutral_comparisons,
        "neutral_vs_enhanced": enhancement_comparisons,
    }


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--source-blend", type=Path, required=True)
    parser.add_argument("--source-receipt", type=Path, required=True)
    parser.add_argument("--portable-blend", type=Path, required=True)
    parser.add_argument("--portable-receipt", type=Path, required=True)
    parser.add_argument("--output-directory", type=Path, required=True)
    args = parser.parse_args(sys.argv[sys.argv.index("--") + 1:])
    args.output_directory.mkdir(parents=True, exist_ok=True)
    results = [
        _exercise_sample("source", args.source_blend, args.source_receipt, args.output_directory),
        _exercise_sample("portable", args.portable_blend, args.portable_receipt, args.output_directory),
    ]
    receipt_path = args.output_directory / "host_foam_presentation_receipt.json"
    receipt_path.write_text(
        json.dumps({"status": "passed", "results": results}, indent=2),
        encoding="utf-8",
    )


if __name__ == "__main__":
    main()
