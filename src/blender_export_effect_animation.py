"""Export source-backed particle caches and timed mesh effects from a scene."""
from __future__ import annotations

import argparse
import json
import math
import os
import sys
from collections.abc import Mapping
from functools import partial
from pathlib import Path
from typing import cast

sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.dont_write_bytecode = True

import bpy
from mathutils import Matrix

from blender_scene_effect_animation import (
    EffectMeshSampler,
    animate_effect_material_time,
    bake_effect_geometry_sequence,
    save_effect_cache_delivery,
    save_effect_material_delivery,
)
from blender_scene_effects import (
    _finite,
    _field,
    _integer,
    _record,
    _typed_source_key,
    _anchor_matrix,
    _blender_matrix,
    _components,
    _system_gravity_local,
    _mesh_templates,
    _owner_matrix_root_relative,
    _particle_mesh_aligned,
    _particle_mesh_instances,
    _particle_mesh_rotations,
    _particle_world_matrix,
    _trail_mesh,
)
from endfield_scene_material_contract import resolve_export_path
from blender_scene_effect_vertex_offset import apply_material_vertex_offset
from extractor_core.particle_snapshot import (
    PARTICLE_PARAMETER_SCHEMA_VERSION,
    ParticleSnapshot,
    generate_fixed_emitter_particle_snapshot,
)
from extractor_core.particle_mesh_geometry import ParticleMeshGeometry, load_particle_shape_geometry

def _array(value: object, label: str) -> list[object]:
    if type(value) is not list:
        raise ValueError(f"Expected array: field={label}")
    return cast(list[object], value)


def _object_identity(obj: bpy.types.Object) -> tuple[int, str, int]:
    entity_value = obj.get("endfield_fx_entity_id")
    source_value = obj.get("endfield_fx_system_source")
    path_value = obj.get("endfield_fx_system_path_id")
    if type(entity_value) is not str or type(source_value) is not str or not source_value or type(path_value) is not str:
        raise ValueError(f"FX object identity fields are incomplete or mistyped: object={obj.name}")
    try:
        entity_id = int(entity_value)
        path_id = int(path_value)
    except ValueError as error:
        raise ValueError(f"FX object identity is not decimal: object={obj.name}, entity={entity_value!r}, pathId={path_value!r}") from error
    if str(entity_id) != entity_value or str(path_id) != path_value:
        raise ValueError(f"FX object identity is not canonical decimal: object={obj.name}")
    return entity_id, source_value, path_id


def _source_objects() -> tuple[dict[tuple[int, str, int], bpy.types.Object],
                                dict[tuple[int, str, int], bpy.types.Object]]:
    sources: dict[tuple[int, str, int], bpy.types.Object] = {}
    trails: dict[tuple[int, str, int], bpy.types.Object] = {}
    for obj in bpy.data.objects:
        particle_name = obj.name.startswith("EF_FX_") and not obj.name.startswith("EF_FX_MESH_")
        if obj.type != "MESH":
            if particle_name:
                raise ValueError(f"Particle FX source object is not a mesh: object={obj.name}, type={obj.type}")
            continue
        has_system_source = "endfield_fx_system_source" in obj
        has_system_path = "endfield_fx_system_path_id" in obj
        if not has_system_source and not has_system_path and not particle_name:
            continue
        if has_system_source != has_system_path:
            raise ValueError(f"Particle FX object has a partial source identity: object={obj.name}")
        identity = _object_identity(obj)
        destination = trails if obj.name.startswith("EF_FX_TRAIL_") else sources
        if identity in destination:
            raise ValueError(f"Duplicate source-qualified FX object: identity={identity}, first={destination[identity].name}, second={obj.name}")
        destination[identity] = obj
    return sources, trails


def _validate_schema5(system: Mapping[str, object], manifest_path: Path,
                      system_source: str, system_path_id: int) -> None:
    parameters = _record(_field(system, "Parameters", "System"), "System.Parameters")
    schema = parameters.get("SchemaVersion")
    if type(schema) is not int or schema != PARTICLE_PARAMETER_SCHEMA_VERSION:
        raise ValueError(
            "SceneProbe particle data must be refreshed before animation: "
            f"manifest={manifest_path}, systemSource={system_source}, systemPathId={system_path_id}, "
            f"requiredSchema={PARTICLE_PARAMETER_SCHEMA_VERSION}, actualSchema={schema!r}"
        )


def _matrix_error(actual: Matrix, expected: Matrix) -> float:
    return max(abs(float(actual[row][column] - expected[row][column]))
               for row in range(4) for column in range(4))


def _snapshot(system: Mapping[str, object], seconds: float, identity: str,
              gravity_local: tuple[float, float, float],
              shape_geometry: ParticleMeshGeometry | None) -> ParticleSnapshot:
    snapshot = generate_fixed_emitter_particle_snapshot(system, seconds, identity, 100000, 100000,
                                                        gravity_local, shape_geometry)
    parameters = _record(_field(system, "Parameters", "System"), "System.Parameters")
    initial = _record(_field(parameters, "Initial", "Parameters"), "Parameters.Initial")
    source_limit = _integer(_field(initial, "MaxNumParticles", "Initial"), "Initial.MaxNumParticles")
    # An authored particle cap is distinct from our preview resource budget.
    if min(snapshot.generated, source_limit) > 100000:
        raise ValueError(f"FX sample exceeds preview particle budget: identity={identity}, seconds={seconds}, generated={snapshot.generated}, sourceLimit={source_limit}, previewLimit=100000")
    return snapshot


def _particle_sample(system: Mapping[str, object], renderer: Mapping[str, object],
                     world: Matrix, camera: bpy.types.Object,
                     templates: tuple[tuple[bpy.types.Mesh, ...], ...], identity: str,
                     gravity_local: tuple[float, float, float], object_name: str,
                     material: bpy.types.Material,
                     shape_geometry: ParticleMeshGeometry | None,
                     seconds: float) -> bpy.types.Mesh:
    snapshot = _snapshot(system, seconds, identity, gravity_local, shape_geometry)
    render_mode = _integer(_field(renderer, "RenderMode", "Renderer"), "Renderer.RenderMode")
    if render_mode == 4:
        rotations = _particle_mesh_rotations(snapshot.particles,
            _integer(_field(renderer, "RenderAlignment", "Renderer"), "Renderer.RenderAlignment"))
        mesh = _particle_mesh_instances(object_name, snapshot.particles, templates, rotations)
    else:
        mesh = _particle_mesh_aligned(
            object_name, snapshot.particles, world, camera, render_mode,
            _integer(_field(renderer, "RenderAlignment", "Renderer"), "Renderer.RenderAlignment"),
            _finite(_field(renderer, "VelocityScale", "Renderer"), "Renderer.VelocityScale"),
            _finite(_field(renderer, "LengthScale", "Renderer"), "Renderer.LengthScale"))
    try:
        displaced = apply_material_vertex_offset(
            material, mesh, world, camera.matrix_world.translation, seconds)
    except Exception:
        bpy.data.meshes.remove(mesh)
        raise
    if displaced is not mesh:
        bpy.data.meshes.remove(mesh)
    return displaced


def _trail_sample(system: Mapping[str, object], camera: bpy.types.Object,
                  world: Matrix, identity: str, gravity_local: tuple[float, float, float],
                  object_name: str, shape_geometry: ParticleMeshGeometry | None,
                  seconds: float) -> bpy.types.Mesh:
    snapshot = _snapshot(system, seconds, identity, gravity_local, shape_geometry)
    mesh = _trail_mesh(object_name, snapshot.particles, world, camera)
    if mesh is not None:
        return mesh
    return _particle_mesh_aligned(object_name, (), world, camera, 0, 0, 0.0, 1.0)


def _build_samplers(plan: Mapping[str, object], camera: bpy.types.Object,
                    sources: Mapping[tuple[int, str, int], bpy.types.Object],
                    trails: Mapping[tuple[int, str, int], bpy.types.Object],
                    fps: float, frame_start: int,
                    gravity_world: tuple[float, float, float]) -> tuple[
                        list[EffectMeshSampler], dict[str, bpy.types.Material],
                        list[dict[str, object]], list[dict[str, object]]]:
    instances = _array(_field(plan, "effect_instances", "Plan"), "Plan.effect_instances")
    template_cache: dict[tuple[Path, tuple[str, int]], tuple[bpy.types.Mesh, ...]] = {}
    planned: set[tuple[int, str, int]] = set()
    samplers: list[EffectMeshSampler] = []
    materials: dict[str, bpy.types.Material] = {}
    animated: list[dict[str, object]] = []
    skipped: list[dict[str, object]] = []
    for instance_index, value in enumerate(instances):
        label = f"Plan.effect_instances[{instance_index}]"
        instance = _record(value, label)
        entity_id = _integer(_field(instance, "entity_id", label), f"{label}.entity_id")
        component_kind = instance.get("component_kind", "particle")
        if component_kind not in ("particle", "mesh_renderer", "mixed"):
            raise ValueError(f"Invalid FX component kind: entityId={entity_id}, kind={component_kind!r}")
        if component_kind == "mesh_renderer":
            skipped.append({"entity_id": entity_id, "status": "mesh_renderer_only",
                            "reason": "The bounded particle cache handles particle-system geometry"})
            continue
        if _field(instance, "matrix_space", label) != "dynamic_effect_anchor":
            raise ValueError(f"Unsupported FX matrix space: entityId={entity_id}")
        manifest_text = _field(instance, "scene_manifest", label)
        if type(manifest_text) is not str or not manifest_text:
            raise ValueError(f"FX SceneProbe path is invalid: entityId={entity_id}")
        manifest_source = Path(manifest_text)
        if not manifest_source.is_absolute():
            raise ValueError(f"FX SceneProbe path must be absolute: entityId={entity_id}, path={manifest_source}")
        manifest_path = resolve_export_path(manifest_source)
        if not manifest_path.is_file():
            raise FileNotFoundError(f"FX SceneProbe manifest is missing: entityId={entity_id}, path={manifest_path}")
        manifest = _record(json.loads(manifest_path.read_text(encoding="utf-8-sig")), str(manifest_path))
        if manifest.get("Format") != "EndfieldSceneProbe/1":
            raise ValueError(f"Unsupported FX SceneProbe format: path={manifest_path}, format={manifest.get('Format')!r}")
        root = _typed_source_key(_field(instance, "prefab_root", label), "GameObject", f"{label}.prefab_root")
        nodes, pairs = _components(manifest, root)
        anchor = _anchor_matrix(_field(instance, "matrix", label))
        if not pairs:
            skipped.append({"entity_id": entity_id, "status": "particle_pair_missing",
                            "reason": "The SceneProbe manifest has no paired particle system and renderer"})
        for system, renderer, owner in pairs:
            system_key = _typed_source_key(_field(system, "Asset", "System"), "ParticleSystem", "System.Asset")
            key = (entity_id, system_key[0], system_key[1])
            if key in planned:
                raise ValueError(f"Duplicate FX system in plan: entityId={entity_id}, source={system_key[0]}, pathId={system_key[1]}")
            planned.add(key)
            obj = sources.get(key)
            trail_obj = trails.get(key)
            if obj is None:
                if trail_obj is not None:
                    raise ValueError(f"FX trail has no matching particle object: identity={key}, trail={trail_obj.name}")
                skipped.append({"entity_id": entity_id, "system_source": system_key[0],
                                "system_path_id": system_key[1], "status": "source_object_absent",
                                "reason": "The static scene contains no particle mesh for this plan component"})
                continue
            enabled = _field(renderer, "Enabled", "Renderer")
            if type(enabled) is not bool:
                raise ValueError(f"Renderer.Enabled must be boolean: identity={key}, value={enabled!r}")
            if not enabled:
                raise ValueError(f"Static particle object maps to a disabled renderer: identity={key}, object={obj.name}")
            _validate_schema5(system, manifest_path, system_key[0], system_key[1])
            unity_world = _particle_world_matrix(
                anchor @ _owner_matrix_root_relative(nodes, root, owner), nodes[owner],
                _integer(_field(system, "ScalingMode", "System"), "System.ScalingMode"))
            world = _blender_matrix(unity_world)
            matrix_error = _matrix_error(obj.matrix_world, world)
            if not math.isfinite(matrix_error) or matrix_error > 1e-5:
                raise ValueError(
                    f"Source FX object transform does not match plan anchor and CAB component: "
                    f"identity={key}, object={obj.name}, maxMatrixError={matrix_error}"
                )
            if trail_obj is not None:
                trail_matrix_error = _matrix_error(trail_obj.matrix_world, world)
                if not math.isfinite(trail_matrix_error) or trail_matrix_error > 1e-5:
                    raise ValueError(f"FX trail transform does not match its source component: identity={key}, trail={trail_obj.name}, maxMatrixError={trail_matrix_error}")
            if obj.modifiers or obj.constraints:
                raise ValueError(f"Source FX object must have no modifiers or constraints: identity={key}, object={obj.name}")
            if (_integer(_field(system, "MoveWithTransform", "System"), "System.MoveWithTransform") == 1
                    and (obj.parent is not None or obj.animation_data is not None)):
                raise ValueError(f"World-space source preview requires a fixed, unparented emitter without transform animation: identity={key}, object={obj.name}")
            if trail_obj is not None and (trail_obj.modifiers or trail_obj.constraints):
                raise ValueError(f"Source FX trail must have no modifiers or constraints: identity={key}, object={trail_obj.name}")
            gravity_local = _system_gravity_local(gravity_world, unity_world, system)
            identity = f"{entity_id}:{system_key[0]}:{system_key[1]}"
            shape_geometry = load_particle_shape_geometry(manifest, manifest_path, system)
            initial_snapshot = _snapshot(system, frame_start / fps, identity, gravity_local, shape_geometry)
            render_mode = _integer(_field(renderer, "RenderMode", "Renderer"), "Renderer.RenderMode")
            if render_mode not in (0, 1, 4):
                raise ValueError(f"Source FX object has unsupported renderer mode: identity={key}, mode={render_mode}")
            templates = (_mesh_templates(manifest, manifest_path, renderer, template_cache)
                         if render_mode == 4 else ())
            if len(obj.material_slots) != 1 or obj.material_slots[0].material is None:
                raise ValueError(f"Particle animation requires one source material: identity={key}, object={obj.name}")
            samplers.append(EffectMeshSampler(obj, partial(
                _particle_sample, system, renderer, world, camera, templates,
                identity, gravity_local, obj.name, obj.material_slots[0].material, shape_geometry)))
            trail_name: str | None = None
            if trail_obj is not None:
                samplers.append(EffectMeshSampler(trail_obj, partial(
                    _trail_sample, system, camera, world, identity, gravity_local, trail_obj.name, shape_geometry)))
                trail_name = trail_obj.name
            object_materials = tuple(slot.material for slot in obj.material_slots if slot.material)
            if not object_materials:
                raise ValueError(f"Source FX object has no material: identity={key}, object={obj.name}")
            for material in object_materials:
                materials[material.name] = material
            if trail_obj is not None:
                if not trail_obj.material_slots or any(slot.material is None for slot in trail_obj.material_slots):
                    raise ValueError(f"Source FX trail has an empty material slot: identity={key}, object={trail_obj.name}")
                for slot in trail_obj.material_slots:
                    material = slot.material
                    if material is None:
                        raise ValueError(f"Source FX trail has an empty material slot: object={trail_obj.name}")
                    materials[material.name] = material
            animated.append({
                "entity_id": entity_id, "system_source": system_key[0],
                "system_path_id": system_key[1], "owner_source": owner[0],
                "owner_path_id": owner[1], "manifest": str(manifest_path),
                "object": obj.name, "trail_object": trail_name,
                "transform_verified": True,
                "initial_particles": len(initial_snapshot.particles),
                "initial_particles_omitted_by_source_limit": initial_snapshot.truncated,
                "unsupported_modules": list(initial_snapshot.unsupported_modules),
                "preview_scope": initial_snapshot.preview_scope,
                "vertex_offset_scope": obj.get("endfield_fx_vertex_offset_scope"),
            })
    unmatched = sorted(set(sources) - planned)
    unmatched_trails = sorted(set(trails) - planned)
    if unmatched or unmatched_trails:
        raise ValueError(f"Source scene contains unplanned FX objects: systems={unmatched[:8]}, trails={unmatched_trails[:8]}")
    return samplers, materials, animated, skipped


def _mesh_effect_materials(scene: bpy.types.Scene) -> dict[str, bpy.types.Material]:
    """Collect source-qualified mesh FX materials from objects present in the scene."""
    return {
        material.name: material
        for obj in scene.objects
        if obj.type == "MESH" and ("endfield_fx_renderer_source" in obj
            or ("endfield_fx_direct_source_json" in obj and "endfield_fx_placement_key" in obj)
            or any(material is not None and material.get("endfield_fx_fog_simple_shader") == "HGRP/FogSimple"
                   for material in obj.data.materials))
        for material in obj.data.materials
        if material is not None
    }


def _arguments() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--plan", type=Path, required=True)
    parser.add_argument("--source-blend", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--frame-start", type=int, required=True)
    parser.add_argument("--frame-end", type=int, required=True)
    parser.add_argument("--max-systems", type=int, required=True)
    parser.add_argument("--camera", required=True,
                        help="Exact reference camera object name assigned to the output scene")
    return parser.parse_args(sys.argv[sys.argv.index("--") + 1:])


def main() -> None:
    args = _arguments()
    plan_path = args.plan.expanduser().resolve(strict=True)
    source_blend = args.source_blend.expanduser().resolve(strict=True)
    output = args.output.expanduser().resolve()
    if not plan_path.is_file() or not source_blend.is_file():
        raise FileNotFoundError(f"Animation inputs must be files: plan={plan_path}, sourceBlend={source_blend}")
    if source_blend.suffix.casefold() != ".blend" or output.suffix.casefold() != ".blend":
        raise ValueError(f"Animation source and output must be .blend files: source={source_blend}, output={output}")
    if not output.is_absolute() or args.max_systems <= 0:
        raise ValueError(f"Animation output must be absolute and max-systems positive: output={output}, maxSystems={args.max_systems}")
    if args.frame_start < 1 or args.frame_end < args.frame_start or args.frame_end - args.frame_start >= 240:
        raise ValueError(f"Animation frame interval must be 1 to 240 bounded frames: start={args.frame_start}, end={args.frame_end}")
    cache_path = output.with_suffix(".abc")
    audit_path = output.with_suffix(".animation.json")
    audit_temporary = output.with_suffix(".animation.json.partial")
    destinations = (output, cache_path, audit_path, audit_temporary)
    if any(path.exists() for path in destinations):
        raise FileExistsError(f"Animation delivery paths must be new: paths={[str(path) for path in destinations if path.exists()]}")
    if source_blend in destinations or plan_path in destinations:
        raise ValueError(f"Animation output collides with an input: plan={plan_path}, sourceBlend={source_blend}, output={output}")

    plan = _record(json.loads(plan_path.read_text(encoding="utf-8-sig")), str(plan_path))
    map_id = _field(plan, "map_id", "Plan")
    gravity_record = _record(_field(plan, "effect_physics", "Plan"), "Plan.effect_physics")
    gravity_values = _array(_field(gravity_record, "gravity", "Plan.effect_physics"), "Plan.effect_physics.gravity")
    if len(gravity_values) != 3:
        raise ValueError(f"FX gravity must contain three values: count={len(gravity_values)}")
    gravity_world = tuple(_finite(value, f"Plan.effect_physics.gravity[{index}]")
                          for index, value in enumerate(gravity_values))

    bpy.ops.wm.open_mainfile(filepath=str(source_blend))
    scene = bpy.context.scene
    if type(map_id) is not str or scene.get("endfield_map") != map_id:
        raise ValueError(f"Source blend and plan maps differ: sceneMap={scene.get('endfield_map')!r}, planMap={map_id!r}")
    camera = bpy.data.objects.get(args.camera)
    if camera is None or camera.type != "CAMERA":
        raise ValueError(f"Explicit reference camera is missing or not a camera object: name={args.camera!r}")
    scene.camera = camera
    bpy.context.view_layer.update()
    fps_base = float(scene.render.fps_base)
    if fps_base <= 0:
        raise ValueError(f"Source blend frame-rate base is invalid: fpsBase={fps_base}")
    fps = scene.render.fps / fps_base
    if not math.isfinite(fps) or fps <= 0:
        raise ValueError(f"Source blend frame rate is invalid: fps={fps}")
    sources, trails = _source_objects()
    samplers, materials, animated_rows, skipped = _build_samplers(
        plan, camera, sources, trails, fps, args.frame_start, gravity_world)
    if len(animated_rows) > args.max_systems:
        raise ValueError(f"Animation system limit exceeded: selected={len(animated_rows)}, maxSystems={args.max_systems}; narrow the source scene")

    output.parent.mkdir(parents=True, exist_ok=True)
    mesh_materials = _mesh_effect_materials(scene)
    materials = {**materials, **mesh_materials}
    driven_materials: list[str] = []
    materials_without_time: list[str] = []
    for name in sorted(materials):
        material = materials[name]
        if material.node_tree is None:
            raise ValueError(f"FX material has no node tree: material={name}")
        if material.node_tree.nodes.get("EF FX Source Time Seconds") is None:
            materials_without_time.append(name)
        else:
            animate_effect_material_time(material, scene)
            driven_materials.append(name)
    if not samplers and not driven_materials:
        raise ValueError(
            f"No source-backed particle geometry or timed mesh materials can be animated: "
            f"skipped={skipped[:8]}, materialsWithoutTime={materials_without_time}")
    audit = {
        "format": "EndfieldBoundedEffectAnimation/1",
        "source_blend": str(source_blend),
        "source_plan": str(plan_path),
        "output": str(output),
        "cache": str(cache_path) if samplers else None,
        "frames": [args.frame_start, args.frame_end],
        "fps": fps,
        "camera": camera.name,
        "camera_orientation_baked": bool(samplers),
        "playback_mode": "particle_cache_and_material_time" if samplers else "material_time_only",
        "animated_systems": animated_rows,
        "skipped_components": skipped,
        "uv_time_driver_materials": driven_materials,
        "mesh_uv_time_driver_materials": sorted(set(driven_materials) & mesh_materials.keys()),
        "materials_without_source_time_input": materials_without_time,
        "limitations": [*([
            "Deterministic previews do not reproduce Unity simulation or start phase",
            "Billboard orientation uses the fixed reference camera",
            "The cache covers only the requested frame interval",
            "Unsupported particle modules remain unreconstructed",
        ] if samplers else []),
            "UV drivers affect all users of selected materials",
            "Mesh material UV animation does not resimulate mesh vertex offsets or runtime activation",
            "Camera-bound SceneColor snapshots are not refreshed by material clocks",
        ],
    }
    cache_created = False
    blend_created = False
    audit_created = False
    temporary_created = False
    try:
        with audit_temporary.open("x", encoding="utf-8", newline="\n") as stream:
            temporary_created = True
            stream.write(json.dumps(audit, ensure_ascii=False, indent=2) + "\n")
            stream.flush()
            os.fsync(stream.fileno())
        if samplers:
            bake_effect_geometry_sequence(samplers, cache_path, args.frame_start,
                                         args.frame_end, 500000)
            cache_created = True
        for image in bpy.data.images:
            if image.source == "FILE" and image.users and not image.packed_file:
                image.pack()
        scene.frame_start = args.frame_start
        scene.frame_end = args.frame_end
        scene.frame_set(args.frame_start)
        scene["endfield_fx_animation_contract"] = (
            "EndfieldBoundedParticlePlayback/1" if samplers else "EndfieldMaterialTimePlayback/1")
        scene["endfield_fx_animation_scope"] = (
            "selected_source_subset_fixed_reference_camera" if samplers else "selected_source_material_time")
        if samplers:
            save_effect_cache_delivery(cache_path, output)
        else:
            save_effect_material_delivery(output)
        blend_created = True
        os.link(audit_temporary, audit_path)
        audit_created = True
        audit_temporary.unlink()
        temporary_created = False
    except Exception as error:
        cleanup_errors: list[str] = []
        for path, created in ((audit_path, audit_created), (audit_temporary, temporary_created),
                              (output, blend_created), (cache_path, cache_created)):
            if created and path.exists():
                try:
                    path.unlink()
                except OSError as cleanup_error:
                    cleanup_errors.append(f"path={path}, error={cleanup_error}")
        if cleanup_errors:
            error.add_note("Failed to clean partial animation delivery: " + "; ".join(cleanup_errors))
        raise
    print(json.dumps({"status": "completed", "output": str(output), "cache": str(cache_path) if samplers else None,
                      "audit": str(audit_path), "animatedSystems": len(animated_rows),
                      "skippedComponents": len(skipped), "frames": [args.frame_start, args.frame_end]}))


if __name__ == "__main__":
    main()
