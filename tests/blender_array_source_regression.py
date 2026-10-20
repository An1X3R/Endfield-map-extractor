"""Exercise source array meshes against bounded orientation and offset contracts."""
from __future__ import annotations

import argparse
from array import array
from collections.abc import Mapping
from dataclasses import replace
import hashlib
import json
import math
from pathlib import Path
import struct
import sys

import bpy
from mathutils import Matrix, Vector


def mesh_signature(mesh: bpy.types.Mesh) -> str:
    digest = hashlib.sha256()
    for vertex in mesh.vertices:
        digest.update(struct.pack("<3f", *vertex.co))
    for polygon in mesh.polygons:
        digest.update(struct.pack(f"<{len(polygon.vertices)}I", *polygon.vertices))
    for layer in mesh.uv_layers:
        digest.update(layer.name.encode())
        for value in layer.data:
            digest.update(struct.pack("<2f", *value.uv))
    return digest.hexdigest()


def coordinates_error(left: bpy.types.Mesh, right: bpy.types.Mesh) -> float:
    if len(left.vertices) != len(right.vertices):
        raise AssertionError("Compared meshes have different vertex counts")
    return max((a.co - b.co).length for a, b in zip(left.vertices, right.vertices, strict=True))


def change_field(record: Mapping[str, object], section: str, field: str,
                 value: object) -> dict[str, object]:
    entries = record[section]
    if not isinstance(entries, dict):
        raise ValueError(f"Expected source field object: section={section}")
    return {**record, section: {**entries, field: value}}


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--manifest", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args(sys.argv[sys.argv.index("--") + 1:])
    args.output.mkdir(parents=True, exist_ok=False)
    sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
    from blender_export_effect_animation import _particle_sample, _snapshot
    from blender_scene_effects import _mesh_templates, _particle_mesh_instances, _particle_mesh_rotations
    from blender_scene_effect_meshes import _source_mesh_templates
    from blender_scene_effect_materials import make_effect_material
    from blender_scene_effect_vertex_offset import (
        UnsupportedVertexOffset, apply_vertex_offset_research_preview,
        validate_vertex_offset_source, vertex_offset_preview_issues,
    )
    from endfield_scene_material_contract import load_catalog, reference_key
    from extractor_core.particle_snapshot import UnsupportedParticleSnapshot

    manifest_bytes = args.manifest.read_bytes()
    manifest = json.loads(manifest_bytes)
    materials, textures = load_catalog((args.manifest,))
    source_state = repr(materials)
    system = next(row for row in manifest["Particles"]["Systems"]
                  if row["Asset"]["Source"] == "CAB-32ed792337a3adf4decbe942c31843e9"
                  and row["Asset"]["PathId"] == -8627021573827191636)
    pair = next(row for row in manifest["Particles"]["Pairs"]
                if reference_key(row["System"]) == reference_key(system["Asset"]))
    renderer = next(row for row in manifest["Particles"]["Renderers"]
                    if reference_key(row["Asset"]) == reference_key(pair["Renderer"]))
    templates = _mesh_templates(manifest, args.manifest, renderer, {})
    if len(templates) != 1 or len(templates[0]) != 1:
        raise AssertionError("Regression source needs exactly one original mesh template")
    template = templates[0][0]
    source_vertex_count = len(template.vertices)
    template_signature = mesh_signature(template)
    snapshot_time = 0.25
    snapshot = _snapshot(system, snapshot_time, "source-array-regression", (0.0, -9.81, 0.0), None)
    if not snapshot.particles:
        raise AssertionError("Real source snapshot contains no live particles")
    actual = snapshot.particles[0]
    old_errors: dict[str, float] = {}
    for alignment in (0, 2):
        rotations = _particle_mesh_rotations(snapshot.particles, alignment)
        mesh = _particle_mesh_instances(f"Alignment{alignment}", snapshot.particles, templates, rotations)
        expected = []
        for particle in snapshot.particles:
            center = Vector((particle.position[0], -particle.position[2], particle.position[1]))
            historical = Matrix.Rotation(-particle.rotation_radians, 3, "Y")
            for vertex in template.vertices:
                scaled = Vector((vertex.co.x * particle.size[0], vertex.co.y * particle.size[2],
                                 vertex.co.z * particle.size[1]))
                expected.append(center + historical @ scaled)
        error = max((vertex.co - point).length for vertex, point in zip(mesh.vertices, expected, strict=True))
        if error > 1e-4:
            raise AssertionError(f"Historical orientation changed: alignment={alignment}, error={error}")
        old_errors[str(alignment)] = error

    directions = (actual.velocity, (1.0, 0.0, 0.0), (-1.0, 0.0, 0.0),
                  (0.0, 1.0, 0.0), (0.0, -1.0, 0.0), (0.0, 0.0, 1.0), (0.0, 0.0, -1.0))
    direction_rows = []
    for index, velocity in enumerate(directions):
        particle = replace(actual, particle_id=index, velocity=velocity, rotation_radians=0.73)
        rotation = _particle_mesh_rotations((particle,), 4)[0]
        expected = Vector((velocity[0], -velocity[2], velocity[1])).normalized()
        forward = rotation @ Vector((0.0, -1.0, 0.0))
        error = (forward - expected).length
        unspun = _particle_mesh_rotations((replace(particle, rotation_radians=0.0),), 4)[0]
        spin_cosine = (rotation @ Vector((1.0, 0.0, 0.0))).dot(unspun @ Vector((1.0, 0.0, 0.0)))
        if error > 1e-6 or abs(spin_cosine - math.cos(0.73)) > 1e-6:
            raise AssertionError(f"Velocity direction/spin mismatch: velocity={velocity}, error={error}")
        direction_rows.append({"velocity_unity": velocity, "forward_blender": list(forward),
                               "direction_error": error, "spin_cosine": spin_cosine})
    try:
        _particle_mesh_rotations((replace(actual, velocity=(0.0, 0.0, 0.0)),), 4)
    except UnsupportedParticleSnapshot as error:
        zero_error = str(error)
        if "particle=" not in zero_error or "velocity=" not in zero_error:
            raise AssertionError("Zero-velocity failure omitted its source evidence") from error
    else:
        raise AssertionError("Velocity alignment accepted an undefined zero direction")
    try:
        _particle_mesh_instances("MissingRotation", snapshot.particles, templates, ())
    except ValueError as error:
        rotation_error = str(error)
    else:
        raise AssertionError("Missing explicit orientations were accepted")

    camera = bpy.data.objects.new("Regression Camera", bpy.data.cameras.new("Regression Camera"))
    bpy.context.scene.collection.objects.link(camera)
    camera.location = (110.0, -140.0, 100.0)
    bpy.context.view_layer.update()
    neutral = bpy.data.materials.new("Regression Orientation Only No Offset")
    animation_mesh = _particle_sample(system, renderer, Matrix.Identity(4), camera, templates,
        "source-array-regression", (0.0, -9.81, 0.0), "AnimationCall", neutral, None, snapshot_time)
    direct_mesh = _particle_mesh_instances("DirectCall", snapshot.particles, templates,
                                         _particle_mesh_rotations(snapshot.particles, 4))
    animation_error = coordinates_error(animation_mesh, direct_mesh)
    if animation_error > 1e-7:
        raise AssertionError(f"Animation call discarded source orientation: error={animation_error}")

    export = next(row for row in manifest["MeshExports"]
                  if row["Asset"]["Source"] == "CAB-490e4fcfda4514833d973dc0d1d87e18"
                  and row["Asset"]["PathId"] == -2776238626296427948)
    high_meshes = _source_mesh_templates(export, args.manifest)
    if len(high_meshes) != 1:
        raise AssertionError("Regression source needs one indexed high03 mesh")
    base = high_meshes[0]
    base_signature = mesh_signature(base)
    material = materials[("cab-ce3a454d9d78346c836c2a1f2c3b7d7b", -7150795046733755634)]
    reference, sampler = validate_vertex_offset_source(material)
    texture_path = textures[reference_key(reference)]
    image = bpy.data.images.load(str(texture_path[0] / texture_path[1]), check_existing=False)
    image.colorspace_settings.name = "Non-Color"
    image.pack()
    context = (image, sampler, reference["Source"], reference["PathId"],
               Matrix.Identity(4), camera.location, 2.0, 0.0)
    if base.attributes.get("EF_FX_CUSTOM1") is not None or len(base.uv_layers) != 1:
        raise AssertionError("High03 test source unexpectedly has fabricated Custom1 or UV1")
    issues = vertex_offset_preview_issues(material, base, *context)
    if issues:
        raise AssertionError(f"Dead Custom1.Y should not block source1471: issues={issues}")
    displaced = apply_vertex_offset_research_preview(material, base, *context)
    gate1 = apply_vertex_offset_research_preview(material, base, *context[:-1], 1.0)
    source_displacement = coordinates_error(displaced, base)
    if source_displacement <= 1e-5 or coordinates_error(gate1, base) != 0.0:
        raise AssertionError("Source1471 displacement or explicit pass gate failed")
    live_failures = []
    for field, vector in (("_OffsetSpeed", [0.0, -0.1, 1.0, 0.0]),
                          ("_OffsetSpeed", [0.0, -0.1, 0.0, 1.0]),
                          ("_OffsetDir", [0.0, 1.0, 0.0, 1.0])):
        live = change_field(material, "Colors", field, vector)
        live_issues = vertex_offset_preview_issues(live, base, *context)
        if not any("Custom1" in issue or "CUSTOM1" in issue for issue in live_issues):
            raise AssertionError(f"Live missing Custom1.Y was not blocked: field={field}, issues={live_issues}")
        live_failures.append({"field": field, "value": vector, "issues": live_issues})
    uv1 = change_field(material, "Floats", "_OffsetUVSet", 1.0)
    if not vertex_offset_preview_issues(uv1, base, *context):
        raise AssertionError("Unverified UV1 source selection was accepted")

    nonparticle = materials[("cab-e869247d28726d478039dd2bc1149b44", 2313592296477701460)]
    validate_vertex_offset_source(nonparticle)
    nonparticle_reference = next(slot["Texture"] for slot in nonparticle["Textures"]
                                 if slot["Property"] == "_OffsetTex")
    nonparticle_path = textures[reference_key(nonparticle_reference)]
    nonparticle_image = bpy.data.images.load(str(nonparticle_path[0] / nonparticle_path[1]), check_existing=False)
    nonparticle_image.colorspace_settings.name = "Non-Color"
    _, nonparticle_sampler = validate_vertex_offset_source(nonparticle)
    nonparticle_issues = vertex_offset_preview_issues(nonparticle, template, nonparticle_image,
        nonparticle_sampler, nonparticle_reference["Source"], nonparticle_reference["PathId"],
        Matrix.Identity(4), camera.location, 2.0, 0.0)
    if nonparticle_issues:
        raise AssertionError(f"Original1472 InParticle0 route regressed: issues={nonparticle_issues}")

    negative = materials[("cab-7bb30611a8d36e13980d7038ef020e51", -313926047135978944)]
    neg_reference, neg_sampler = validate_vertex_offset_source(negative)
    neg_texture_path = textures[reference_key(neg_reference)]
    neg_image = bpy.data.images.load(str(neg_texture_path[0] / neg_texture_path[1]), check_existing=False)
    neg_image.colorspace_settings.name = "Non-Color"
    neg_image.pack()
    signed_base = template.copy()
    custom = signed_base.color_attributes.new(name="EF_FX_CUSTOM1", type="FLOAT_COLOR", domain="CORNER")
    custom.data.foreach_set("color", array("f", (0.0, 0.0, 0.0, 0.0)) * len(signed_base.loops))
    negative_context = (neg_image, neg_sampler, neg_reference["Source"], neg_reference["PathId"],
                        Matrix.Identity(4), camera.location, 2.0, 0.0)
    signed = apply_vertex_offset_research_preview(negative, signed_base, *negative_context)
    unsigned = apply_vertex_offset_research_preview(
        change_field(negative, "Floats", "_VertCameraOffset", 0.0), signed_base, *negative_context)
    signed_errors = []
    for vertex, result, reference_result in zip(signed_base.vertices, signed.vertices, unsigned.vertices, strict=True):
        radial = (camera.location - vertex.co).normalized()
        expected_delta = radial * negative["Floats"]["_VertCameraOffset"]
        signed_errors.append(((result.co - reference_result.co) - expected_delta).length)
    if max(signed_errors) > 1e-4:
        raise AssertionError(f"Signed source camera displacement changed direction: error={max(signed_errors)}")

    loop_material = materials[("cab-ef4a34a15d62303c72a721bf27eb85bf", 8847452916769241075)]
    loop_reference, loop_sampler = validate_vertex_offset_source(loop_material)
    loop_path = textures[reference_key(loop_reference)]
    loop_image = bpy.data.images.load(str(loop_path[0] / loop_path[1]), check_existing=False)
    loop_image.colorspace_settings.name = "Non-Color"
    loop_image.pack()
    loop_system = next(row for row in manifest["Particles"]["Systems"]
                       if row["Asset"]["Source"] == "CAB-e869247d28726d478039dd2bc1149b44"
                       and row["Asset"]["PathId"] == 3571032213964386834)
    loop_pair = next(row for row in manifest["Particles"]["Pairs"]
                     if reference_key(row["System"]) == reference_key(loop_system["Asset"]))
    loop_renderer = next(row for row in manifest["Particles"]["Renderers"]
                         if reference_key(row["Asset"]) == reference_key(loop_pair["Renderer"]))
    loop_templates = _mesh_templates(manifest, args.manifest, loop_renderer, {})
    loop_blender_material = make_effect_material(loop_material, textures, 0.25,
                                                tuple(loop_renderer["VertexStreams"]))
    loop_template_signatures = [mesh_signature(mesh) for slot in loop_templates for mesh in slot]
    loop_rows = []
    loop_meshes = []
    for seconds in (0.25, 2.0):
        loop_snapshot = _snapshot(loop_system, seconds, "loop-source-regression", (0.0, -9.81, 0.0), None)
        if not loop_snapshot.particles or any(particle.custom1 is None for particle in loop_snapshot.particles):
            raise AssertionError(f"Native enabled CustomData curves produced no source stream: seconds={seconds}")
        loop_mesh = _particle_mesh_instances(f"Source1468Particles{seconds}", loop_snapshot.particles,
            loop_templates, _particle_mesh_rotations(loop_snapshot.particles, loop_renderer["RenderAlignment"]))
        if loop_mesh.color_attributes.get("EF_FX_CUSTOM1") is None:
            raise AssertionError("Real loop particle mesh discarded its source Custom1 stream")
        loop_context = (loop_image, loop_sampler, loop_reference["Source"], loop_reference["PathId"],
                        Matrix.Identity(4), camera.location, seconds, 0.0)
        loop_issues = vertex_offset_preview_issues(loop_material, loop_mesh, *loop_context)
        if loop_issues:
            raise AssertionError(f"Verified1468 source loop remains blocked: seconds={seconds}, issues={loop_issues}")
        loop_displaced = apply_vertex_offset_research_preview(loop_material, loop_mesh, *loop_context)
        loop_export_sample = _particle_sample(loop_system, loop_renderer, Matrix.Identity(4), camera,
            loop_templates, "loop-source-regression", (0.0, -9.81, 0.0),
            f"Source1468Export{seconds}", loop_blender_material, None, seconds)
        loop_export_error = coordinates_error(loop_displaced, loop_export_sample)
        if loop_export_error > 1e-5:
            raise AssertionError(f"Real material factory/export offset differs: seconds={seconds}, error={loop_export_error}")
        loop_displaced.materials.append(loop_blender_material)
        loop_gate1 = apply_vertex_offset_research_preview(loop_material, loop_mesh, *loop_context[:-1], 1.0)
        loop_error = coordinates_error(loop_displaced, loop_mesh)
        if loop_error <= 1e-5 or coordinates_error(loop_gate1, loop_mesh) != 0.0:
            raise AssertionError(f"Real1468 displacement/pass gate failed: seconds={seconds}, offset={loop_error}")
        if list(loop_material["RenderState"]["ValidKeywords"]) != [
                "_SAMPLE_TEX0", "_SAMPLE_TEX1", "_USE_SOFTBLEND", "_USE_VERTOFFSET"]:
            raise AssertionError("Loop material no longer matches the exact verified1468 profile")
        loop_rows.append({"seconds": seconds, "particles": len(loop_snapshot.particles),
                          "source_custom1": [particle.custom1 for particle in loop_snapshot.particles],
                          "max_offset_m": loop_error, "pass_gate1_identity": True,
                          "material_factory_animation_call_error_m": loop_export_error,
                          "vertex_signature_gate0": mesh_signature(loop_displaced)})
        loop_meshes.append((f"Source1468Loop{seconds}Seconds", loop_displaced))
    if loop_template_signatures != [mesh_signature(mesh) for slot in loop_templates for mesh in slot]:
        raise AssertionError("Loop regression modified original source mesh templates")
    if mesh_signature(base) != base_signature or mesh_signature(template) != template_signature:
        raise AssertionError("Regression mutated original source mesh data")
    if repr(materials) != source_state or args.manifest.read_bytes() != manifest_bytes:
        raise AssertionError("Regression mutated source material records or manifest")
    for name, mesh in (("Source1471Base", base), ("Source1471Gate0", displaced),
                       ("Source1471Gate1", gate1), ("SignedCameraDiagnosticCustomY0", signed),
                       ("VelocityAlignedActualSourceSnapshot", animation_mesh), *loop_meshes):
        obj = bpy.data.objects.new(name, mesh)
        bpy.context.scene.collection.objects.link(obj)
        obj["regression_expected_signature"] = mesh_signature(mesh)
    delivery = args.output / "array_source_regression.blend"
    bpy.ops.wm.save_as_mainfile(filepath=str(delivery))
    bpy.ops.wm.open_mainfile(filepath=str(delivery), load_ui=False, use_scripts=False)
    for obj in bpy.context.scene.objects:
        if "regression_expected_signature" in obj and mesh_signature(obj.data) != obj["regression_expected_signature"]:
            raise AssertionError(f"Saved regression geometry changed after reopen: object={obj.name}")
    (args.output / "verification.json").write_text(json.dumps({
        "status": "passed", "manifest": str(args.manifest),
        "manifest_sha256": hashlib.sha256(manifest_bytes).hexdigest(),
        "system": system["Asset"], "renderer": renderer["Asset"],
        "source_snapshot_time_seconds": snapshot_time,
        "source_snapshot_particles": len(snapshot.particles), "source_snapshot_velocity": actual.velocity,
        "source_mesh_vertices": source_vertex_count, "alignment0_2_historical_max_error_m": old_errors,
        "alignment4_forward_and_spin": direction_rows, "zero_velocity_error": zero_error,
        "missing_rotations_error": rotation_error, "animation_call_error_m": animation_error,
        "source1471_material": {"Source": material["Source"], "PathId": material["PathId"]},
        "source1471_mesh": export["Asset"], "source1471_max_offset_m": source_displacement,
        "dead_custom_y_missing_stream_accepted": True, "live_custom_y_missing_stream_failures": live_failures,
        "source1472_in_particle0_missing_custom_y_accepted": True,
        "uv1_still_blocked": True, "pass_gate1_identity": True,
        "negative_camera_material": {"Source": negative["Source"], "PathId": negative["PathId"]},
        "negative_camera_offset_m": negative["Floats"]["_VertCameraOffset"],
        "signed_camera_max_radial_error_m": max(signed_errors),
        "source1468_loop_material": {"Source": loop_material["Source"], "PathId": loop_material["PathId"]},
        "source1468_loop_system": loop_system["Asset"], "source1468_samples": loop_rows,
        "source_inputs_unchanged": True, "saved_and_reopened": True,
        "scope": "Real source meshes/material records and source snapshot; six axis directions and spin are diagnostic inputs. Signed camera test supplies explicit diagnostic Custom1.Y=0, not recovered runtime data. No native renderer parity or appearance claim.",
    }, indent=2), encoding="utf-8")


if __name__ == "__main__":
    main()
