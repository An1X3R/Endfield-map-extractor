"""Patch an audited material blend with source silk/weather equations, explicitly."""
from __future__ import annotations

import argparse
import hashlib
import json
import math
from pathlib import Path
import sys
from typing import Callable, cast

import bpy

sys.dont_write_bytecode = True
sys.path.insert(0, str(Path(__file__).resolve().parent))
from audit_character_materials import geometry_digest
from character_node_graph import node, color_mix, math_node
from character_weather_nodes import WeatherUniforms, WeatherOverride, make_weather_controls, make_weather_controls_from_per_draw, make_silk_surface, scalar_mix, evaluate_ruri_wetness, pack_native_preview_word, unpack_native_weather_word
from character_weather_per_draw import WeatherPerDrawSource, make_weather_per_draw_source


def file_hash(path: Path) -> str:
    with path.open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def object_value(value: object, label: str) -> dict[str, object]:
    if not isinstance(value, dict) or any(not isinstance(key, str) for key in value):
        raise ValueError(f"Required object is invalid: {label}")
    return cast(dict[str, object], value)


def number(value: object, label: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
        raise ValueError(f"Required finite number is invalid: {label}")
    return float(value)


def four_values(value: object, label: str) -> tuple[float, float, float, float]:
    if not isinstance(value, list) or len(value) != 4:
        raise ValueError(f"Required four-channel array is invalid: {label}")
    return cast(tuple[float, float, float, float], tuple(number(item, label) for item in value))


def weather_source(raw: dict[str, object]) -> tuple[WeatherUniforms, WeatherPerDrawSource | None]:
    has_uniforms = "source_uniforms" in raw
    has_per_draw = "source_per_draw" in raw
    if has_uniforms == has_per_draw:
        raise ValueError("Weather state must provide exactly one of source_uniforms or source_per_draw")
    if has_per_draw:
        source = object_value(raw["source_per_draw"], "source per-draw input")
        if source.get("format") != "EndfieldNativePerDrawWeatherInput/1":
            raise ValueError(f"Unsupported source per-draw format: {source.get('format')!r}")
        packed_word_bits = source.get("packed_word_bits")
        if type(packed_word_bits) is not int or not 0 <= packed_word_bits <= 0xFFFFFFFF:
            raise ValueError("source_per_draw.packed_word_bits must be a uint32 integer")
        provenance = source.get("provenance")
        if not isinstance(provenance, str) or not provenance.strip():
            raise ValueError("source_per_draw.provenance must be a nonempty string")
        native_source = make_weather_per_draw_source(
            packed_word_bits,
            number(source.get("wet_effect_world_space_height"), "wet_effect_world_space_height"),
            provenance,
        )
        uniforms = WeatherUniforms(
            native_source.channels.normalized(),
            native_source.wet_effect_world_space_height,
            0.0,
            0.0,
            native_source.provenance,
        )
        return uniforms, native_source

    source = object_value(raw["source_uniforms"], "source uniforms")
    provenance = source.get("provenance")
    if not isinstance(provenance, str):
        raise ValueError("Source uniform provenance must be a string")
    uniforms = WeatherUniforms(
        four_values(source["effect"], "effect"),
        number(source["water_height"], "water_height"),
        number(source["preview_enabled"], "preview_enabled"),
        number(source["preview_water_height"], "preview_water_height"),
        provenance,
    )
    return uniforms, None


def material_rows(path: Path) -> dict[tuple[str, int], tuple[str, ...]]:
    raw: object = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(raw, list):
        raise ValueError("Material sources must be an array")
    result: dict[tuple[str, int], tuple[str, ...]] = {}
    for raw_row in raw:
        row = object_value(raw_row, "material row")
        source = object_value(row["object"], "material identity")
        data = object_value(row["data"], "material TypeTree")
        cab, path_id = source["source"], source["path_id"]
        keywords = data["m_ValidKeywords"]
        if not isinstance(cab, str) or isinstance(path_id, bool) or not isinstance(path_id, int) or not isinstance(keywords, list) or any(not isinstance(item, str) for item in keywords):
            raise ValueError("Material identity/keywords have invalid types")
        identity = (cab.casefold(), path_id)
        if identity in result:
            raise ValueError(f"Duplicate material identity: {identity}")
        result[identity] = tuple(keywords)
    return result


def labelled(tree: bpy.types.NodeTree, label: str) -> bpy.types.Node:
    matches = tuple(item for item in tree.nodes if item.label == label)
    if len(matches) != 1:
        raise ValueError(f"Expected one baseline semantic node: group={tree.name}, label={label}, found={len(matches)}")
    return matches[0]


def splice(tree: bpy.types.NodeTree, old: bpy.types.NodeSocket, new: bpy.types.NodeSocket, consumers: tuple[bpy.types.NodeSocket, ...]) -> None:
    if not consumers:
        raise ValueError(f"Baseline channel has no consumers: {tree.name}/{old.name}")
    for target in consumers:
        tree.links.new(new, target)


def patch_silk(tree: bpy.types.ShaderNodeTree, weather: bpy.types.ShaderNodeTree, layout: Callable[[bpy.types.NodeTree], None]) -> dict[str, object]:
    floats_raw = object_value(json.loads(tree["source_floats"]), "source floats")
    colors_raw = object_value(json.loads(tree["source_colors"]), "source colors")
    floats = {key: number(value, key) for key, value in floats_raw.items()}
    colors = {key: four_values(value, key) for key, value in colors_raw.items()}
    silk_tree = make_silk_surface(floats, colors, layout)
    source_color = labelled(tree, "Approximate wet surface color").outputs[0]
    roughness = labelled(tree, "Approximate wet roughness").outputs[0]
    colors_to = tuple(link.to_socket for link in source_color.links)
    roughness_to = tuple(link.to_socket for link in roughness.links)
    bsdfs = tuple(item for item in tree.nodes if item.type == "BSDF_PRINCIPLED")
    bases = tuple(item for item in tree.nodes if item.get("source_slot") == "_BaseMap")
    if len(bsdfs) != 1 or len(bases) != 1:
        raise ValueError(f"Baseline source surface/base texture is ambiguous: {tree.name}")
    bsdf, base = bsdfs[0], bases[0]
    specular_to = bsdf.inputs["Specular IOR Level"]
    if len(specular_to.links) != 1:
        raise ValueError("Baseline specular channel requires one explicit source")
    original_specular = specular_to.links[0].from_socket
    geometry = node(tree, "ShaderNodeNewGeometry", "Weather world geometry", "Weather Binding")
    xyz = node(tree, "ShaderNodeSeparateXYZ", "Meter height: Blender Z = Unity Y", "Weather Binding")
    tree.links.new(geometry.outputs["Position"], xyz.inputs[0])
    state = node(tree, "ShaderNodeGroup", "Explicit weather uniforms", "Weather Binding")
    state.node_tree = weather
    tree.links.new(xyz.outputs["Z"], state.inputs["World Height"])
    silk = node(tree, "ShaderNodeGroup", "Fixed Ruri SilkStockingsSurface", "Weather Binding")
    silk.node_tree = silk_tree
    tree.links.new(source_color, silk.inputs["Albedo"])
    tree.links.new(roughness, silk.inputs["Roughness"])
    tree.links.new(base.outputs["Alpha"], silk.inputs["Base Alpha"])
    tree.links.new(state.outputs["Wetness"], silk.inputs["Wetness"])
    normal_source = bsdf.inputs["Normal"].links[0].from_socket if bsdf.inputs["Normal"].is_linked else geometry.outputs["Normal"]
    tree.links.new(normal_source, silk.inputs["Normal"])
    view = node(tree, "ShaderNodeVectorMath", "View toward camera", "Weather Binding")
    view.operation = "SCALE"
    # Geometry Incoming already points from the surface toward the ray origin.
    view.inputs["Scale"].default_value = 1.0
    tree.links.new(geometry.outputs["Incoming"], view.inputs[0])
    tree.links.new(view.outputs[0], silk.inputs["View"])
    if floats["_SilkStockingsAdvance"] > 0.5:
        masks = tuple(item for item in tree.nodes if item.get("source_slot") == "_SilkStockingsMask")
        if len(masks) != 1 or masks[0].image is None or masks[0].image.colorspace_settings.name != "Non-Color":
            raise ValueError(f"Advanced silk requires an exact Non-Color source mask: {tree.name}")
        split = node(tree, "ShaderNodeSeparateColor", "Source silk mask RGB", "Weather Binding")
        tree.links.new(masks[0].outputs["Color"], split.inputs[0])
        for channel, output in zip("RGB", ("Red", "Green", "Blue")):
            tree.links.new(split.outputs[output], silk.inputs["Mask " + channel])
        tree.links.new(masks[0].outputs["Alpha"], silk.inputs["Mask A"])
    weight = node(tree, "ShaderNodeValue", "Source Silk Equation Weight (editable)", "Weather Binding")
    weight.outputs[0].default_value = 1.0
    albedo = color_mix(tree, "MIX", weight.outputs[0], source_color, silk.outputs["Albedo"], "Silk source equation enable", "Weather Binding")
    rough = scalar_mix(tree, weight.outputs[0], roughness, silk.outputs["Roughness"], "Silk roughness enable")
    specular = scalar_mix(tree, weight.outputs[0], original_specular, silk.outputs["Specular Intensity"], "Silk intensity to Principled - Approximation")
    splice(tree, source_color, albedo, colors_to)
    splice(tree, roughness, rough, roughness_to)
    tree.links.new(specular, specular_to)
    tree["weather_scope"] = "Fixed Ruri silk equations; explicit unbound weather state; specular lobe uses approximate Principled response; opaque alpha unchanged"
    layout(tree)
    return {"identity": json.loads(tree["source_identity"]), "group": tree.name, "silk_group": silk_tree.name, "advanced": floats["_SilkStockingsAdvance"], "source_dry_wet_tint_equal": colors["_SilkStockingsDryColor"] == colors["_SilkStockingsWetColor"], "source_specular_wetness_invariant": floats["_SilkStockingsSpecularMinAtMinWetness"] == 1.0, "coverage_changes_surface_alpha": False}


def main(argv: list[str]) -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    for name in ("blend", "materials", "state", "scene-source-dir", "output"):
        parser.add_argument("--" + name, type=Path, required=True)
    args = parser.parse_args(argv)
    if args.output.exists():
        raise FileExistsError(f"Output already exists: {args.output}")
    raw = object_value(json.loads(args.state.read_text(encoding="utf-8")), "weather state")
    uniforms, per_draw_source = weather_source(raw)
    user = object_value(raw["user_override"], "user override")
    override = WeatherOverride(number(user["weight"], "override weight"), four_values(user["effect"], "override effect"), number(user["water_height"], "override height"))
    expected = [(height, evaluate_ruri_wetness(uniforms, override, height)) for height in (-1.0, 0.0, 0.5, 1.0, 2.0)]
    sys.path.insert(0, str(args.scene_source_dir.resolve()))
    from blender_scene_materials import layout_shader_node_tree
    keywords = material_rows(args.materials)
    bpy.ops.wm.open_mainfile(filepath=str(args.blend.resolve()))
    object_names = tuple(obj.name for obj in bpy.data.objects if obj.type == "MESH")
    before = geometry_digest(object_names)
    groups = tuple(group for group in bpy.data.node_groups if "source_identity" in group and "source_shader" in group)
    if len(groups) != len(keywords):
        raise ValueError("Blend source groups do not cover every source material")
    for group in groups:
        ident = json.loads(group["source_identity"])
        if (ident[0].casefold(), ident[1]) not in keywords:
            raise ValueError(f"Blend material identity absent in provided source packet: {ident}")
    weather = (make_weather_controls_from_per_draw(per_draw_source, override, layout_shader_node_tree)
               if per_draw_source is not None else make_weather_controls(uniforms, override, layout_shader_node_tree))
    rows = [patch_silk(group, weather, layout_shader_node_tree) for group in groups if "_SILK_STOCKINGS" in keywords[(json.loads(group["source_identity"])[0].casefold(), json.loads(group["source_identity"])[1])]]
    weather_name = weather.name
    args.output.mkdir(parents=True)
    blend = args.output / "character_weather.blend"
    bpy.ops.wm.save_as_mainfile(filepath=str(blend.resolve()))
    bpy.ops.wm.open_mainfile(filepath=str(blend.resolve()))
    after = geometry_digest(object_names)
    if before != after:
        raise ValueError("Weather patch/reopen changed geometry, UV, weights, bind matrices or visibility")
    if weather_name not in bpy.data.node_groups or any(row["silk_group"] not in bpy.data.node_groups for row in rows):
        raise ValueError("Reopened weather/silk groups missing")
    if per_draw_source is not None:
        saved_weather = bpy.data.node_groups[weather_name]
        if saved_weather.get("source_input_kind") != "explicit_native_per_draw_word":
            raise ValueError("Reopened weather group lost the explicit per-draw source kind")
        if saved_weather.get("source_per_draw_packed_word_bits_hex") != f"0x{per_draw_source.packed_word_bits:08x}":
            raise ValueError("Reopened weather group changed raw packed uint32 bits")
        if saved_weather.get("source_per_draw_height_float32_bits_hex") != f"0x{per_draw_source.wet_effect_world_space_height_bits:08x}":
            raise ValueError("Reopened weather group changed raw height float32 bits")
        if saved_weather.get("source_per_draw_runtime_capture_verified") is not False:
            raise ValueError("Reopened weather group overstated runtime capture status")
        expected_channels = {"rain": per_draw_source.channels.rain, "wet": per_draw_source.channels.wet,
                             "global_wet": per_draw_source.channels.global_wet, "snow": per_draw_source.channels.snow}
        if json.loads(saved_weather["source_per_draw_channels_json"]) != expected_channels:
            raise ValueError("Reopened weather group changed decoded per-draw bytes")
        expected_sources = {"Source Effect " + axis: value for axis, value in zip("XYZW", per_draw_source.channels.normalized())}
        expected_sources.update({"Source Water Height": per_draw_source.wet_effect_world_space_height,
                                 "Source Preview Enable": 0.0, "Source Preview Water Height": 0.0})
        for label, expected_value in expected_sources.items():
            actual = float(labelled(saved_weather, label).outputs[0].default_value)
            if not math.isclose(actual, expected_value, rel_tol=0.0, abs_tol=1e-7):
                raise ValueError(f"Reopened per-draw control differs: label={label}, actual={actual}, expected={expected_value}")
        expected_overrides = {"User Override Weight": override.weight, "User Water Height": override.water_height}
        expected_overrides.update({"User Effect " + axis: value for axis, value in zip("XYZW", override.effect)})
        for label, expected_value in expected_overrides.items():
            actual = float(labelled(saved_weather, label).outputs[0].default_value)
            if not math.isclose(actual, expected_value, rel_tol=0.0, abs_tol=1e-7):
                raise ValueError(f"Reopened editable user override differs: label={label}, actual={actual}, expected={expected_value}")
    report = {"format": "EndfieldCharacterWeatherPatch/1", "status": "patch_and_reopen_passed", "game_weather_fidelity_complete": False, "inputs": {str(path.resolve()): file_hash(path) for path in (args.blend, args.materials, args.state)}, "source_scripts": {str(path): file_hash(path) for path in (Path(__file__).resolve(), Path(__file__).with_name("character_weather_nodes.py"), Path(__file__).with_name("character_weather_per_draw.py"), args.scene_source_dir / "blender_scene_materials.py")}, "output": str(blend.resolve()), "output_sha256": file_hash(blend), "geometry_preserved": True, "materials": len(groups), "silk_materials": rows, "state": raw, "weather_source_kind": "explicit_native_per_draw_word_input" if per_draw_source is not None else "explicit_uniforms", "height_mask_reference_scope": "Current host weather equation only; no runtime capture or draw-space parity is asserted.", "height_mask_reference": expected, "native_preview_word_rain_0_snow_1": f"{pack_native_preview_word(0.0,1.0):08x}", "decoded_native_preview": unpack_native_weather_word(pack_native_preview_word(0.0,1.0)), "snow": "Real global texture extracted separately; snow sample/channel/blend consumer incomplete, therefore no fabricated coverage", "controls": {"shared_weather": weather_name, "silk_response": "Source Silk Equation Weight (editable); material node fixed Ruri SilkStockingsSurface inputs preserve exact source values and permit explicit user edits", "per_draw_source_word_bits": f"0x{per_draw_source.packed_word_bits:08x}" if per_draw_source is not None else None, "user_override_preserved": True}, "limits": ["Ruri weather max formula is not asserted equal to all native archived additive rain paths", "Material _WetEffectIntensity and _RainEffectIntensity are preserved, not guessed into per-draw packed state", "Silk source current dry/wet colors may be identical, so wetness need not visibly darken it", "HGRP exact anisotropic/specular light lobe and snow/rain texture sampling remain incomplete", "No source opaque alpha is changed; source coverage only drives silk rim arithmetic"]}
    (args.output / "weather_patch_receipt.json").write_text(json.dumps(report, indent=2, ensure_ascii=False), encoding="utf-8")
    print(json.dumps({"status": report["status"], "silk_materials": len(rows), "output": str(blend)}))


if __name__ == "__main__":
    main(sys.argv[sys.argv.index("--") + 1:])
