from __future__ import annotations

import argparse
import hashlib
import json
import math
import sys
from collections import Counter
from pathlib import Path
from typing import Callable, Sequence

sys.path.insert(0, str(Path(__file__).resolve().parent))

import bpy
from mathutils import Vector

from character_eye_lighting import (
    EYE_SOURCE_SHADER,
    TANGENT_SIGN_ATTRIBUTE,
    build_eye_matcap_branch,
    source_matcap_color,
    source_parameters,
)
from character_hgrp_diffuse import build_hgrp_diffuse

def file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as source:
        for chunk in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def exact_source_record(group: bpy.types.ShaderNodeTree) -> tuple[dict[str, object], dict[str, object]]:
    identity = json.loads(group["source_identity"])
    material_records = [json.loads(text.as_string()) for text in bpy.data.texts if text.name.startswith("SOURCE MATERIAL ")]
    type_tree_records = [json.loads(text.as_string()) for text in bpy.data.texts if text.name.startswith("SOURCE TYPETREE ")]
    matches = [
        (material, type_tree)
        for material in material_records
        for type_tree in type_tree_records
        if (
            material["object"]["source"].casefold(),
            material["object"]["path_id"],
        ) == (identity[0].casefold(), identity[1])
        and type_tree.get("m_Name") == material["object"]["name"]
    ]
    if len(matches) != 1:
        raise ValueError(f"Exact source eye material evidence is absent or ambiguous: {group.name}, {identity}")
    return matches[0]


def source_keywords(group: bpy.types.ShaderNodeTree) -> tuple[str, ...]:
    _, type_tree = exact_source_record(group)
    keywords = tuple(type_tree["m_ValidKeywords"])
    if len(set(keywords)) != len(keywords):
        raise ValueError(f"Duplicate source eye keywords: {group.name}")
    return keywords


def source_eye_groups() -> tuple[bpy.types.ShaderNodeTree, ...]:
    candidates = tuple(group for group in bpy.data.node_groups if group.get("source_shader") == EYE_SOURCE_SHADER)
    visible_group_pointers = {
        node.node_tree.as_pointer()
        for obj in bpy.context.scene.objects
        if obj.type == "MESH" and not obj.hide_render
        for slot in obj.material_slots
        if slot.material is not None and slot.material.use_nodes and slot.material.node_tree is not None
        for node in slot.material.node_tree.nodes
        if node.type == "GROUP" and node.node_tree is not None and node.node_tree.get("source_shader") == EYE_SOURCE_SHADER
    }
    selected = tuple(group for group in candidates if group.as_pointer() in visible_group_pointers)
    if not selected:
        raise ValueError("No source Eye groups are referenced by visible scene mesh materials")
    feature_keywords = (
        ("_UseMatcap", "_MATCAP_ON"),
        ("_EyeHighLight", "_EYE_HIGHLIGHT"),
        ("_UseDiffRampMap", "_DIFF_RAMP_ON"),
    )
    for group in selected:
        parameters = source_parameters(group)
        keywords = set(source_keywords(group))
        feature_values = (parameters.use_matcap, parameters.use_eye_highlight, parameters.use_diff_ramp)
        for (property_name, keyword), value in zip(feature_keywords, feature_values, strict=True):
            property_enabled = value == 1.0
            keyword_enabled = keyword in keywords
            if property_enabled != keyword_enabled:
                raise ValueError(
                    f"Source Eye switch and shader keyword disagree: group={group.name}, "
                    f"property={property_name}, value={property_enabled}, keyword={keyword}, enabled={keyword_enabled}"
                )
    return selected


def patch_eye_groups(
    layout: Callable[[bpy.types.NodeTree], None],
    shared_state_group: bpy.types.ShaderNodeTree,
) -> list[dict[str, str]]:
    """Callable merge point for the existing character material preparation pipeline."""
    groups = source_eye_groups()
    diffuse_group = build_hgrp_diffuse("EF HGRP Eye ComputeNPRDiffuse - Verified Equations")
    layout(diffuse_group)
    patched: list[dict[str, str]] = []
    for group in groups:
        source_parameters(group)
        result = build_eye_matcap_branch(group, layout, shared_state_group, diffuse_group)
        patched.append(result)
    return patched


def eye_renderer_objects(groups: tuple[bpy.types.ShaderNodeTree, ...]) -> tuple[bpy.types.Object, ...]:
    group_pointers = {group.as_pointer() for group in groups}
    objects = tuple(
        obj for obj in bpy.context.scene.objects
        if obj.type == "MESH"
        and not obj.hide_render
        and any(
            slot.material is not None
            and slot.material.use_nodes
            and slot.material.node_tree is not None
            and any(
                node.type == "GROUP" and node.node_tree is not None and node.node_tree.as_pointer() in group_pointers
                for node in slot.material.node_tree.nodes
            )
            for slot in obj.material_slots
        )
    )
    if not objects:
        raise ValueError(f"No renderer meshes use the selected source eye groups: {[group.name for group in groups]}")
    return objects


def add_tangent_sign_attribute(objects: tuple[bpy.types.Object, ...]) -> list[dict[str, object]]:
    """Store Blender's UV0 corner tangent handedness for source frame reconstruction."""
    processed: dict[int, dict[str, object]] = {}
    active_matcap_groups = {
        group.as_pointer()
        for group in source_eye_groups()
        if source_parameters(group).use_matcap == 1.0
    }
    for obj in objects:
        has_active_matcap = any(
            slot.material is not None
            and slot.material.use_nodes
            and slot.material.node_tree is not None
            and any(
                item.type == "GROUP"
                and item.node_tree is not None
                and item.node_tree.as_pointer() in active_matcap_groups
                for item in slot.material.node_tree.nodes
            )
            for slot in obj.material_slots
        )
        if not has_active_matcap:
            continue
        mesh = obj.data
        mesh_key = mesh.as_pointer()
        if mesh_key in processed:
            continue
        uv_layers = tuple(layer.name for layer in mesh.uv_layers)
        if "UV0" not in uv_layers:
            raise ValueError(f"Source eye renderer is missing UV0: object={obj.name}, uv_layers={uv_layers}")
        mesh.calc_tangents(uvmap="UV0")
        signs = tuple(float(loop.bitangent_sign) for loop in mesh.loops)
        if not signs or any(not math.isfinite(value) or abs(abs(value) - 1.0) > 1.0e-5 for value in signs):
            raise ValueError(f"Source eye tangent handedness is invalid: object={obj.name}, values={Counter(signs)}")
        attribute = mesh.attributes.get(TANGENT_SIGN_ATTRIBUTE)
        if attribute is None:
            attribute = mesh.attributes.new(name=TANGENT_SIGN_ATTRIBUTE, type="FLOAT", domain="CORNER")
        elif attribute.data_type != "FLOAT" or attribute.domain != "CORNER":
            raise ValueError(f"Existing tangent handedness attribute has an incompatible layout: object={obj.name}")
        for index, value in enumerate(signs):
            attribute.data[index].value = value
        mesh.free_tangents()
        processed[mesh_key] = {
            "mesh": mesh.name,
            "vertex_count": len(mesh.vertices),
            "loop_count": len(mesh.loops),
            "sign_counts": {str(sign): count for sign, count in sorted(Counter(signs).items())},
        }
    return list(processed.values())


def _set_group_input(node: bpy.types.Node, name: str, value: float | tuple[float, float, float, float]) -> None:
    socket = node.inputs.get(name)
    if socket is None:
        raise ValueError(f"Patched eye material instance is missing input: node={node.name}, socket={name}")
    if socket.is_linked:
        raise ValueError(f"Eye preview control has an unexpected external link: node={node.name}, socket={name}")
    socket.default_value = value


def eye_material_instances(groups: tuple[bpy.types.ShaderNodeTree, ...]) -> tuple[bpy.types.Node, ...]:
    group_pointers = {group.as_pointer() for group in groups}
    instances = tuple(
        node for material in bpy.data.materials if material.use_nodes and material.node_tree is not None
        for node in material.node_tree.nodes
        if node.type == "GROUP" and node.node_tree is not None and node.node_tree.as_pointer() in group_pointers
    )
    if not instances:
        raise ValueError(f"No source material instances reference the eye groups: {[group.name for group in groups]}")
    return instances


def set_eye_controls(
    instances: tuple[bpy.types.Node, ...],
    matcap_enabled: float,
    highlight_enabled: float,
    cp13: tuple[float, float, float],
    parallax_scale: float,
) -> None:
    values = (matcap_enabled, highlight_enabled, *cp13, parallax_scale)
    if not all(math.isfinite(value) for value in values) or any(value not in (0.0, 1.0) for value in (matcap_enabled, highlight_enabled)) or parallax_scale < 0.0:
        raise ValueError("Eye preview toggles and CP13 values must be finite")
    for instance in instances:
        for name, value in (
            ("Eye Parallax Scale", parallax_scale),
            ("CP13 Eye Base Light Multiplier", cp13[0]),
            ("CP13 Eye Highlight Multiplier", cp13[1]),
            ("CP13 Eye Scattering Multiplier", cp13[2]),
            ("Eye Shadow Weight", 1.0),
            ("Matcap Enabled", matcap_enabled),
            ("Eye Highlight Enabled", highlight_enabled),
        ):
            _set_group_input(instance, name, value)


def set_eye_response_mix(instances: tuple[bpy.types.Node, ...], response_mix: float) -> None:
    if not math.isfinite(response_mix) or not 0.0 <= response_mix <= 1.0:
        raise ValueError("Eye response mix must be finite and within [0, 1]")
    for instance in instances:
        _set_group_input(instance, "Eye Response Mix", response_mix)


def _weather_control(label: str) -> bpy.types.NodeSocket:
    matches = tuple(
        node.outputs[0]
        for tree in bpy.data.node_groups
        for node in tree.nodes
        if node.type == "VALUE" and node.label == label
    )
    if len(matches) != 1:
        raise ValueError(f"Expected one explicit weather control: label={label}, matches={len(matches)}")
    return matches[0]


def set_dry_weather_state() -> dict[str, float]:
    controls = {
        "User Override Weight": 1.0,
        "User Water Height": -10.0,
        "User Effect X": 0.0,
        "User Effect Y": 0.0,
        "User Effect Z": 0.0,
        "User Effect W": 0.0,
        "User Snow Intensity 0-1": 0.0,
    }
    for label, value in controls.items():
        _weather_control(label).default_value = value
    return {"rain": 0.0, "snow": 0.0, "water_height_meters": -10.0, "wetness": 0.0}


def read_weather_state() -> dict[str, float]:
    return {
        "rain": float(_weather_control("User Effect X").default_value),
        "snow": float(_weather_control("User Snow Intensity 0-1").default_value),
        "water_height_meters": float(_weather_control("User Water Height").default_value),
        "wetness": float(_weather_control("User Effect Z").default_value),
    }


def shared_preview_state_receipt(group: bpy.types.ShaderNodeTree) -> dict[str, object]:
    values: dict[str, object] = {}
    for label, node_type in (
        ("CP0 Z Ambient Floor", "VALUE"),
        ("Light Color", "RGB"),
        ("Light Intensity", "VALUE"),
        ("Ambient Color", "RGB"),
        ("Ambient Intensity", "VALUE"),
    ):
        matches = tuple(node for node in group.nodes if node.type == node_type and node.label == label)
        if len(matches) != 1:
            raise ValueError(f"Expected one shared HGRP preview state value: group={group.name}, label={label}, matches={len(matches)}")
        raw = matches[0].outputs[0].default_value
        value: object = tuple(float(channel) for channel in raw) if node_type == "RGB" else float(raw)
        components = value if isinstance(value, tuple) else (value,)
        if not all(math.isfinite(component) for component in components):
            raise ValueError(f"Shared HGRP preview value is non-finite: group={group.name}, label={label}")
        values[label] = list(value) if isinstance(value, tuple) else value
    values["runtime_final_volume_captured"] = False
    return values


def _bounds_center(objects: tuple[bpy.types.Object, ...]) -> tuple[Vector, float]:
    visible_points = tuple(
        obj.matrix_world @ Vector(corner)
        for obj in objects if not obj.hide_render
        for corner in obj.bound_box
    )
    if not visible_points:
        raise ValueError("No visible source iris geometry is available for a close-up")
    minimum = Vector(tuple(min(point[axis] for point in visible_points) for axis in range(3)))
    maximum = Vector(tuple(max(point[axis] for point in visible_points) for axis in range(3)))
    return (minimum + maximum) * 0.5, max(maximum - minimum)


def configure_eye_camera(scene: bpy.types.Scene, center: Vector, offset: Vector) -> None:
    camera = scene.camera
    if camera is None or camera.type != "CAMERA":
        raise ValueError("A preview camera is required for the eye close-up")
    camera.data.type = "ORTHO"
    camera.data.ortho_scale = 0.18
    camera.data.clip_start = 0.001
    camera.data.clip_end = 100.0
    camera.location = center + offset
    camera.rotation_euler = (center - camera.location).to_track_quat("-Z", "Y").to_euler()


def render_case(
    output: Path,
    name: str,
    engine: str,
    center: Vector,
    instances: tuple[bpy.types.Node, ...],
    matcap_enabled: float,
    highlight_enabled: float,
    cp13: tuple[float, float, float],
    parallax_scale: float,
    offset: Vector,
) -> dict[str, object]:
    scene = bpy.context.scene
    set_eye_controls(instances, matcap_enabled, highlight_enabled, cp13, parallax_scale)
    configure_eye_camera(scene, center, offset)
    scene.render.engine = engine
    scene.render.resolution_x = 768
    scene.render.resolution_y = 512
    scene.render.resolution_percentage = 100
    scene.render.image_settings.file_format = "PNG"
    scene.render.image_settings.color_mode = "RGBA"
    scene.render.film_transparent = False
    scene.render.filepath = str(output / name)
    if engine == "CYCLES":
        scene.cycles.device = "CPU"
        scene.cycles.samples = 16
        scene.cycles.use_denoising = True
    elif engine == "BLENDER_EEVEE_NEXT":
        scene.eevee.taa_render_samples = 32
    else:
        raise ValueError(f"Unsupported eye verification engine: {engine}")
    bpy.ops.render.render(write_still=True)
    image = output / name
    if not image.is_file() or image.stat().st_size == 0:
        raise ValueError(f"Eye render did not produce an image: {image}")
    return {
        "path": str(image.resolve()),
        "engine": engine,
        "matcap_enabled": matcap_enabled,
        "eye_highlight_enabled": highlight_enabled,
        "cp13_preview_override": list(cp13),
        "camera_offset": list(offset),
        "width": scene.render.resolution_x,
        "height": scene.render.resolution_y,
        "bytes": image.stat().st_size,
        "sha256": file_sha256(image),
    }


def mean_pixel_difference(first: Path, second: Path) -> float:
    a = bpy.data.images.load(str(first), check_existing=False)
    b = bpy.data.images.load(str(second), check_existing=False)
    try:
        if tuple(a.size) != tuple(b.size):
            raise ValueError(f"Cannot compare eye renders with different sizes: {first}, {second}")
        left = a.pixels[:]
        right = b.pixels[:]
        if len(left) != len(right) or not left:
            raise ValueError(f"Eye render pixel buffers are invalid: {first}, {second}")
        return sum(abs(float(x) - float(y)) for x, y in zip(left, right)) / len(left)
    finally:
        bpy.data.images.remove(a)
        bpy.data.images.remove(b)


def main(
    blend: Path,
    output: Path,
    single_character_root: Path,
    repository_src: Path,
    shader_reference: Path,
    constructor_source: Path,
) -> None:
    paths = (blend, shader_reference, constructor_source)
    if any(not path.is_file() for path in paths):
        raise FileNotFoundError(f"Required eye evidence input is missing: {paths}")
    if output.exists():
        raise FileExistsError(f"Eye stage output already exists: {output}")
    material_audit_path = blend.parent / "material_reopen_audit.json"
    if not material_audit_path.is_file():
        raise FileNotFoundError(f"Stage13 material audit is missing: {material_audit_path}")
    sys.path.insert(0, str(single_character_root.resolve()))
    sys.path.insert(0, str(repository_src.resolve()))
    from audit_character_materials import geometry_digest
    from blender_scene_materials import layout_shader_node_tree

    audit = json.loads(material_audit_path.read_text(encoding="utf-8"))
    renderer_names = tuple(audit["geometry_digests"])
    if not renderer_names or audit["status"] != "passed":
        raise ValueError("Stage13 source material audit is not a passing full renderer audit")
    bpy.ops.wm.open_mainfile(filepath=str(blend.resolve()))
    before_geometry = geometry_digest(renderer_names)
    groups = source_eye_groups()
    state_candidates = tuple(
        group for group in bpy.data.node_groups
        if {
            item.name
            for item in group.interface.items_tree
            if item.item_type == "SOCKET" and item.in_out == "OUTPUT"
        }.issuperset({"Ambient Color", "Light Color", "Ambient Intensity", "Light Intensity", "CP0 Z Ambient Floor"})
    )
    if len(state_candidates) != 1:
        raise ValueError(f"Expected one shared HGRP preview state group, found {len(state_candidates)}")
    shared_state_group, = state_candidates
    fixture_groups = tuple(
        group for group in groups
        if exact_source_record(group)[0]["object"]["name"] == "M_actor_pelica_iris_01"
    )
    if len(fixture_groups) != 1:
        raise ValueError(f"Expected one Perlica iris fixture group, found {len(fixture_groups)}")
    group, = fixture_groups
    material, type_tree = exact_source_record(group)
    source_keywords_value = tuple(type_tree["m_ValidKeywords"])
    params = source_parameters(group)
    objects = eye_renderer_objects((group,))
    tangent_signs = add_tangent_sign_attribute(objects)
    patch_results = patch_eye_groups(layout_shader_node_tree, shared_state_group)
    instances = eye_material_instances((group,))
    dry_state = set_dry_weather_state()
    set_eye_response_mix(instances, 1.0)
    set_eye_controls(instances, 1.0, 1.0, (0.0, 0.0, 0.0), params.parallax_scale)
    center, extent = _bounds_center(objects)
    output.mkdir(parents=True)
    scene = bpy.context.scene
    front_offset = Vector((0.0, -0.32, 0.0))
    angled_offset = Vector((0.06, -0.32, 0.035))
    renders: list[dict[str, object]] = []
    for engine in ("BLENDER_EEVEE_NEXT", "CYCLES"):
        prefix = "eevee" if engine == "BLENDER_EEVEE_NEXT" else "cycles"
        renders.append(render_case(output, f"{prefix}_front_matcap_off_highlight_off.png", engine, center, instances, 0.0, 0.0, (0.0, 0.0, 0.0), params.parallax_scale, front_offset))
        renders.append(render_case(output, f"{prefix}_front_matcap_on_highlight_off.png", engine, center, instances, 1.0, 0.0, (0.0, 0.0, 0.0), params.parallax_scale, front_offset))
        renders.append(render_case(output, f"{prefix}_front_matcap_off_highlight_on.png", engine, center, instances, 0.0, 1.0, (0.0, 0.0, 0.0), params.parallax_scale, front_offset))
        renders.append(render_case(output, f"{prefix}_front_matcap_on_highlight_on.png", engine, center, instances, 1.0, 1.0, (0.0, 0.0, 0.0), params.parallax_scale, front_offset))
        renders.append(render_case(output, f"{prefix}_angled_matcap_on_highlight_on.png", engine, center, instances, 1.0, 1.0, (0.0, 0.0, 0.0), params.parallax_scale, angled_offset))
        renders.append(render_case(output, f"{prefix}_angled_matcap_on_highlight_on_parallax_off.png", engine, center, instances, 1.0, 1.0, (0.0, 0.0, 0.0), 0.0, angled_offset))
    differences = {
        "eevee_matcap_toggle": mean_pixel_difference(
            output / "eevee_front_matcap_off_highlight_off.png",
            output / "eevee_front_matcap_on_highlight_off.png",
        ),
        "eevee_eye_highlight_toggle": mean_pixel_difference(
            output / "eevee_front_matcap_off_highlight_off.png",
            output / "eevee_front_matcap_off_highlight_on.png",
        ),
        "eevee_view_angle": mean_pixel_difference(
            output / "eevee_front_matcap_on_highlight_on.png",
            output / "eevee_angled_matcap_on_highlight_on.png",
        ),
        "eevee_parallax_toggle": mean_pixel_difference(
            output / "eevee_angled_matcap_on_highlight_on_parallax_off.png",
            output / "eevee_angled_matcap_on_highlight_on.png",
        ),
        "cycles_matcap_toggle": mean_pixel_difference(
            output / "cycles_front_matcap_off_highlight_off.png",
            output / "cycles_front_matcap_on_highlight_off.png",
        ),
        "cycles_eye_highlight_toggle": mean_pixel_difference(
            output / "cycles_front_matcap_off_highlight_off.png",
            output / "cycles_front_matcap_off_highlight_on.png",
        ),
        "cycles_view_angle": mean_pixel_difference(
            output / "cycles_front_matcap_on_highlight_on.png",
            output / "cycles_angled_matcap_on_highlight_on.png",
        ),
        "cycles_parallax_toggle": mean_pixel_difference(
            output / "cycles_angled_matcap_on_highlight_on_parallax_off.png",
            output / "cycles_angled_matcap_on_highlight_on.png",
        ),
    }
    if any(value <= 1.0e-6 for value in differences.values()):
        raise AssertionError(f"Eye Matcap/highlight/view controls produced no measurable image change: {differences}")

    shared_state_values = shared_preview_state_receipt(shared_state_group)
    shared_state_name = shared_state_group.name
    renderer_object_names = tuple(obj.name for obj in objects)
    eye_source_shader = str(group["source_shader"])
    set_eye_controls(instances, 1.0, 1.0, (0.0, 0.0, 0.0), params.parallax_scale)
    configure_eye_camera(scene, center, front_offset)
    bpy.data.texts.new("EYE HGRP STATE BOUNDARY.txt").write(
        "Perlica uses the source Eye ComputeNPRDiffuse equations and Eye dark/lit branches with explicit shared HGRP preview state. Eye Response Mix defaults to the source branch; zero selects the Blender surface baseline for A/B comparison. CP11.w, CP12.x, CP13, avatar customization, and screen-shadow state are not captured runtime values. Static EyeTintColor and AvatarCustomizeEnable material values are preserved. Eye punctual-light accumulation and subsurface contribution remain outside this patch. Rain and snow are zero. Ruri GLSL is the fixed reference; no fresh compiled Eye shader export is claimed.\n"
    )
    target_blend = output / "perlica_eye_matcap.blend"
    bpy.ops.wm.save_as_mainfile(filepath=str(target_blend))
    bpy.ops.wm.open_mainfile(filepath=str(target_blend))
    after_geometry = geometry_digest(renderer_names)
    if before_geometry != after_geometry:
        raise AssertionError("Eye stage changed audited renderer transforms, topology, positions, UVs, skin weights, or visibility")
    reopened_groups = tuple(
        candidate for candidate in source_eye_groups()
        if exact_source_record(candidate)[0]["object"]["name"] == "M_actor_pelica_iris_01"
    )
    if len(reopened_groups) != 1:
        raise AssertionError(f"Perlica iris group count changed after reopen: {len(reopened_groups)}")
    reopened_group, = reopened_groups
    if reopened_group.get("eye_lighting_scope") is None:
        raise AssertionError("Eye Matcap branch marker did not survive reopen")
    reopened_matcap = tuple(node for node in reopened_group.nodes if node.type == "TEX_IMAGE" and node.label.startswith("_MatcapTex"))
    if len(reopened_matcap) != 1 or reopened_matcap[0].image is None or not reopened_matcap[0].image.packed_file:
        raise AssertionError("Packed source Matcap image did not survive reopen")
    if read_weather_state() != dry_state:
        raise AssertionError("Dry-weather stage state changed")
    numeric_sample = (0.2, 0.4, 0.6, 0.8)
    numeric_full = (0.5, 1.0, 1.5)
    numeric_intensity = 0.4
    numeric_actual = source_matcap_color(numeric_sample, params.matcap_color, numeric_intensity, numeric_full)
    numeric_expected = tuple(
        (numeric_sample[index] * params.matcap_color[3] + numeric_sample[3] * params.matcap_color[index])
        * numeric_intensity * numeric_full[index]
        for index in range(3)
    )
    if max(abs(a - b) for a, b in zip(numeric_actual, numeric_expected)) > 1.0e-12:
        raise AssertionError("Source Matcap RGBA tint formula failed its numeric boundary check")

    eye_texture_slot = next(
        item for item in material["textures"]
        if item["slot"] == "_MatcapTex"
    )
    report = {
        "format": "EndfieldCharacterEyeHgrpStage/1",
        "status": "rendered_and_reopened",
        "input_blend": str(blend.resolve()),
        "input_sha256": file_sha256(blend),
        "output_blend": str(target_blend.resolve()),
        "output_sha256": file_sha256(target_blend),
        "geometry_digest_preserved": True,
        "audited_renderer_count": len(renderer_names),
        "eye_renderer_objects": list(renderer_object_names),
        "eye_source_material": material["object"],
        "eye_source_shader": eye_source_shader,
        "eye_source_keywords": source_keywords_value,
        "source_matcap_binding": {
            "slot": "_MatcapTex",
            "pointer": eye_texture_slot["reference"],
            "image_name": reopened_matcap[0].image.name,
            "image_size": list(reopened_matcap[0].image.size),
            "colorspace": reopened_matcap[0].image.colorspace_settings.name,
            "packed": reopened_matcap[0].image.packed_file is not None,
            "material_scale": eye_texture_slot["scale"],
            "material_offset": eye_texture_slot["offset"],
        },
        "source_eye_parameters": {
            "parallax_scale": params.parallax_scale,
            "matcap_normal_scale": params.normal_scale,
            "matcap_color_rgba": list(params.matcap_color),
            "eye_highlight_color_rgba": list(params.highlight_color),
            "eye_scattering_color_rgba": list(params.scattering_color),
            "eye_tint_color_rgba": list(params.eye_tint_color),
            "avatar_customize_enabled_source": params.avatar_customize_enabled,
            "metallic_source": params.metallic,
            "alpha_premultiply_source": params.alpha_premultiply,
            "use_matcap": params.use_matcap,
            "eye_highlight": params.use_eye_highlight,
            "diffuse_ramp_enabled": params.use_diff_ramp,
        },
        "eye_response_mix": {
            "value": 1.0,
            "default_branch": "source Eye ComputeNPRDiffuse",
            "zero_branch": "Blender surface baseline",
            "eye_local_only": True,
        },
        "source_formula_reference": {
            "path": str(shader_reference.resolve()),
            "sha256": file_sha256(shader_reference),
            "branches": {
                "iris_mask_parallax_and_normal": "Ruri_Endfield_Uber.glsl:1806-1835",
                "eye_highlight_and_view_ramp": "Ruri_Endfield_Uber.glsl:1865-1889",
                "eye_ComputeNPRDiffuse_and_dark_lit_selection": "Ruri_Endfield_Uber.glsl:1908-1927",
                "Matcap_rgba_tint_and_HGRP_weight": "Ruri_Endfield_Uber.glsl:1935-1946",
                "eye_mainLit_desaturation_and_CP13": "Ruri_Endfield_Uber.glsl:1947-1974",
            },
        },
        "hgrp_state_boundary": {
            "shared_state_group": shared_state_name,
            "full_diffuse": "Per-pixel ComputeNPRDiffuse local equations use the explicit shared HGRP state group and Eye-specific ramp/shadow inputs.",
            "shared_state_preview_values": shared_state_values,
            "source_blended_light_intensity": 1.0,
            "host_light_color_bridge": "shared Light Color multiplied by shared Light Intensity once",
            "shared_preview_sun_strength_folded_into_light_color": True,
            "eye_ramp_cp11_w_preview": 0.0,
            "eye_ramp_cp12_x_preview": 0.0,
            "eye_ramp_bias_runtime_captured": False,
            "cp13_constructor_initial": [0.0, 0.0, 0.0],
            "cp13_preview_override": [0.0, 0.0, 0.0],
            "cp13_constructor_source": str(constructor_source.resolve()),
            "cp13_constructor_sha256": file_sha256(constructor_source),
            "eye_shadow_weight": 1.0,
            "runtime_final_volume_captured": False,
            "artist_light_direction_and_state_changed": False,
        },
        "weather": dry_state,
        "uv_tangent_signs": tangent_signs,
        "patch_result": patch_results,
        "render_differences_mean_abs_rgba": differences,
        "numeric_matcap_rgba_check": {
            "sample_rgba": list(numeric_sample),
            "tint_rgba": list(params.matcap_color),
            "intensity": numeric_intensity,
            "full_diffuse_rgb": list(numeric_full),
            "result_rgb": list(numeric_actual),
            "source_equation": "(sample.rgb * tint.a + sample.a * tint.rgb) * (intensity * fullDiffuse)",
        },
        "renders": renders,
        "limitations": [
            "CP0/1/6/7/12 and host light color/strength are explicit shared preview inputs, not captured final game state; source blendedLightInt remains 1.0, with host strength folded into the source light-color input exactly once.",
            "CP11.w, CP12.x, CP13.xyz, avatar customization override, screen-shadow weight, and FBXRotationFix runtime state are not captured; CP11.w and CP12.x default to zero, and the source material tint/enable values are preserved.",
            "The Eye ComputeNPRDiffuse function, ramp coordinates, dark/lit selection, Matcap term, and mainLit desaturation are connected; final Endfield_EyesPunctualLights and subsurface specular accumulation are not implemented here.",
            "The CP13 direct preview term is multiplied by the existing artist direct-intensity control; that control is not a captured game light-loop value.",
            "UV0 tangent frame is reconstructed from Blender UV tangents and stored per-corner; source tangent handedness is not independently compared with a native mesh sidecar.",
            "Ruri GLSL is the fixed shader reference; this receipt does not claim a fresh compiled Eye shader export.",
            "The preview is a bind-pose eye close-up with static weather controls set to zero.",
        ],
    }
    (output / "verification.json").write_text(json.dumps(report, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")


def _parse_args(argv: Sequence[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--blend", required=True, type=Path)
    parser.add_argument("--output-dir", required=True, type=Path)
    parser.add_argument("--single-character-root", required=True, type=Path)
    parser.add_argument("--repository-src", required=True, type=Path)
    parser.add_argument("--shader-reference", required=True, type=Path)
    parser.add_argument("--constructor-source", required=True, type=Path)
    return parser.parse_args(list(argv))


if __name__ == "__main__":
    args = _parse_args(sys.argv[sys.argv.index("--") + 1:])
    main(args.blend, args.output_dir, args.single_character_root, args.repository_src, args.shader_reference, args.constructor_source)

