"""Compose portable scenes with the existing exact geometry and material adapters."""
from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
import struct
import sys
from collections.abc import Mapping, Sequence

sys.path.insert(0, str(Path(__file__).resolve().parent))

import bpy
from mathutils import Vector

from blender_build_map02_chunk_20260727 import import_obj, join_submeshes, unity_matrix, build_terrain
from blender_scene_materials import create_material, layout_shader_node_trees
from blender_scene_terrain import adapt_scene_terrain, adapt_scene_terrain_mesh_normals
from blender_terrain_atlas import crop_terrain_atlases
from blender_terrain_detail import apply_scene_detail
from endfield_scene_material_contract import MaterialRecord, load_catalog, reference_key, resolve_export_path
from endfield_scene_export_plan import load_scene_export_plan, PlanInstance

TERRAIN_SAMPLER_BUDGET = 24
EFFECT_SNAPSHOT_AGE_SECONDS = 2.1


def undrawn_material() -> bpy.types.Material:
    name = "Endfield_Runtime_Undrawn_Submesh"
    material = bpy.data.materials.get(name)
    if material is None:
        material = bpy.data.materials.new(name)
        material.use_nodes = True
        nodes, links = material.node_tree.nodes, material.node_tree.links
        nodes.clear()
        transparent = nodes.new("ShaderNodeBsdfTransparent")
        output = nodes.new("ShaderNodeOutputMaterial")
        links.new(transparent.outputs[0], output.inputs["Surface"])
        material["endfield_runtime_role"] = "no_non_shadow_draw_for_submesh"
    return material


def mesh_identity(row: PlanInstance) -> str:
    return hashlib.sha256(json.dumps([row["mesh_file"], row["material_refs"]], sort_keys=True).encode()).hexdigest()


def scene_signature() -> dict[str, str]:
    """Record geometry, UVs, particle colors, transforms and slots before saving."""
    signatures = {}
    for obj in bpy.context.scene.objects:
        digest = hashlib.sha256()
        digest.update(str(tuple(tuple(row) for row in obj.matrix_world)).encode())
        if obj.type == "MESH":
            for vertex in obj.data.vertices:
                digest.update(struct.pack("<3f", *vertex.co))
            for polygon in obj.data.polygons:
                digest.update(str((tuple(polygon.vertices), polygon.material_index)).encode())
            for uv in obj.data.uv_layers:
                for value in uv.data:
                    digest.update(struct.pack("<2f", *value.uv))
            for attribute in obj.data.color_attributes:
                digest.update(str((attribute.name, attribute.domain, attribute.data_type)).encode())
                for value in attribute.data:
                    digest.update(struct.pack("<4f", *value.color))
            digest.update(str(tuple(material.name if material else None for material in obj.data.materials)).encode())
        signatures[obj.name] = digest.hexdigest()
    return signatures


def image_sampler_count(node_tree: bpy.types.NodeTree) -> int:
    """Count unique image sampler configurations in a node tree and its groups."""
    sampler_keys: set[tuple[int, str, str, str]] = set()
    visited_groups: set[int] = set()
    pending = [node_tree]
    while pending:
        current = pending.pop()
        tree_pointer = current.as_pointer()
        if tree_pointer in visited_groups:
            continue
        visited_groups.add(tree_pointer)
        for node in current.nodes:
            if node.type == "TEX_IMAGE":
                image = node.image
                if image is not None:
                    sampler_keys.add((image.as_pointer(), node.interpolation, node.extension, node.projection))
            elif node.type == "GROUP" and node.node_tree is not None:
                pending.append(node.node_tree)
    return len(sampler_keys)


def audit_terrain_sampler_budget(scene: bpy.types.Scene) -> list[dict[str, int | str]]:
    """Audit terrain-detail materials used by mesh polygons against the Eevee budget."""
    used_materials: dict[int, bpy.types.Material] = {}
    for obj in scene.objects:
        if obj.type != "MESH":
            continue
        used_slots = {polygon.material_index for polygon in obj.data.polygons}
        for index, slot in enumerate(obj.material_slots):
            material = slot.material
            if index in used_slots and material is not None:
                used_materials[material.as_pointer()] = material

    audit: list[dict[str, int | str]] = []
    for material in used_materials.values():
        if material.get("endfield_terrain_detail_contract") != "HGTerrainDetail/1":
            continue
        if not material.use_nodes or material.node_tree is None:
            raise ValueError(f"Terrain detail material has no shader node tree: {material.name}")
        sampler_count = image_sampler_count(material.node_tree)
        audit.append({"material": material.name, "samplers": sampler_count, "budget": TERRAIN_SAMPLER_BUDGET})
        if sampler_count > TERRAIN_SAMPLER_BUDGET:
            raise RuntimeError(
                f"TERRAIN_SAMPLER_BUDGET_EXCEEDED material={material.name} "
                f"samplers={sampler_count} budget={TERRAIN_SAMPLER_BUDGET}"
            )
    return sorted(audit, key=lambda row: str(row["material"]))


def project_decals(manifest: Path, materials: Path) -> list[dict[str, object]]:
    from blender_add_projected_decals import read_decals, read_materials, receiver_index, project_decal

    decals = read_decals(manifest)
    specs = read_materials(materials)
    if not decals:
        return []
    receivers, grid = receiver_index()
    collection = bpy.data.collections.new("Endfield_Projected_Decals")
    bpy.context.scene.collection.children.link(collection)
    arrays, projected = {}, {}
    result = []
    ordered = sorted(decals, key=lambda row: (specs[row.material_path.casefold()].floats.get("_DrawOrder", 0), row.entity_id))
    minimum = min(specs[row.material_path.casefold()].floats.get("_DrawOrder", 0) for row in ordered)
    for decal in ordered:
        spec = specs[decal.material_path.casefold()]
        unsupported = [name for name in ("_UseWaterFlow", "_UseWetness", "_UseSludgeDecal", "_UseDecalFootPrint",
                                        "_UVNoiseStrength", "_UVArcStrengthX", "_UVArcStrengthY") if spec.floats.get(name, 0) != 0]
        if unsupported or decal.packed_flags:
            result.append({"entity_id": decal.entity_id, "status": "unsupported_decal_feature", "features": unsupported})
            continue
        bias = 0.001 + (spec.floats.get("_DrawOrder", 0) - minimum) * 0.00001
        polygons, triangles = project_decal(decal, spec, receivers, grid, arrays, projected, collection, bias)
        result.append({"entity_id": decal.entity_id, "status": "restored" if polygons else "no_receiver_surface",
                       "polygons": polygons, "source_triangles": triangles})
    return result


def build_instances(
    rows: list[PlanInstance],
    collection: bpy.types.Collection,
    materials: dict[tuple[str, int], MaterialRecord],
    textures: dict[tuple[str, int], tuple[Path, str]],
) -> None:
    """Reuse exact scene slots for visible assets and temporary water bottoms."""
    material_cache: dict[tuple[str, int], bpy.types.Material] = {}
    mesh_cache: dict[str, bpy.types.Mesh] = {}
    for row in rows:
        key = mesh_identity(row)
        if key not in mesh_cache:
            imported = import_obj(Path(row["mesh_file"]))
            if len(imported) != len(row["material_refs"]):
                raise ValueError(f"Source submesh count changed: entity={row['entity_id']}")
            for obj, reference in zip(imported, row["material_refs"], strict=True):
                if reference is None:
                    material = undrawn_material()
                else:
                    identity = reference_key(reference)
                    if identity not in material_cache:
                        material_cache[identity] = create_material(materials[identity], textures, True)
                    material = material_cache[identity]
                obj.data.materials.clear()
                obj.data.materials.append(material)
                for polygon in obj.data.polygons:
                    polygon.material_index = 0
            template = join_submeshes(imported)
            mesh_cache[key] = template.data
            bpy.data.objects.remove(template, do_unlink=True)
        obj = bpy.data.objects.new(f"ENTITY_{row['entity_id']}_{row['entity_base']}", mesh_cache[key])
        collection.objects.link(obj)
        obj.matrix_world = unity_matrix(struct.pack("<16f", *row["matrix"]))
        obj["entity_id"] = str(row["entity_id"])
        obj["entity_base"] = row["entity_base"]
        obj["source_asset"] = row["mesh_asset"].get("Name", row["mesh_path"])
        obj["endfield_source_mesh"] = json.dumps(row["mesh_asset"])
        obj["endfield_material_refs"] = json.dumps(row["material_refs"])
        obj["endfield_category"] = row["category"]


def effect_preview_camera(instances: Sequence[Mapping[str, object]], scene: bpy.types.Scene) -> bpy.types.Object:
    """Retain the reference camera used to orient a deterministic FX snapshot."""
    anchors = [unity_matrix(struct.pack("<16f", *row["matrix"])).translation for row in instances]
    center = sum(anchors, Vector()) / len(anchors)
    radius = max(6.0, max((point - center).length for point in anchors))
    camera_data = bpy.data.cameras.new("Endfield_FX_Snapshot_Camera")
    camera = bpy.data.objects.new(camera_data.name, camera_data)
    scene.collection.objects.link(camera)
    camera.location = center + Vector((radius * 1.6, -radius * 2.4, radius * 1.2))
    camera.rotation_euler = (center - camera.location).to_track_quat("-Z", "Y").to_euler()
    camera.data.clip_end = max(1000.0, radius * 10.0)
    camera["endfield_fx_role"] = "Static billboard orientation reference; moving the camera does not resimulate particles"
    scene.camera = camera
    bpy.context.view_layer.update()
    for screen in bpy.data.screens:
        for area in screen.areas:
            if area.type == "VIEW_3D":
                area.spaces.active.region_3d.view_location = center
                area.spaces.active.region_3d.view_distance = radius * 4.0
    return camera


def build_effect_previews(plan: Mapping[str, object], materials: dict[tuple[str, int], MaterialRecord],
                          textures: dict[tuple[str, int], tuple[Path, str]]) -> list[dict[str, object]]:
    """Use source materials and report unsupported FX components separately."""
    from blender_scene_effects import build_scene_effects
    from blender_scene_effect_materials import make_effect_material

    instances = plan.get("effect_instances", [])
    if not isinstance(instances, list):
        raise ValueError("Scene effect_instances must be an array")
    if not instances:
        return []
    age = EFFECT_SNAPSHOT_AGE_SECONDS
    scene = bpy.context.scene
    collection = bpy.data.collections.new("Endfield_Effects_Static_Preview")
    scene.collection.children.link(collection)
    camera = effect_preview_camera(instances, scene)

    def material_factory(reference: Mapping[str, object], renderer: Mapping[str, object],
                         system: Mapping[str, object]) -> bpy.types.Material:
        identity = reference_key(reference)
        if identity not in materials:
            raise FileNotFoundError(f"Particle material was not extracted: reference={identity}")
        streams = renderer.get("VertexStreams")
        if not isinstance(streams, list) or any(type(value) is not int for value in streams):
            raise ValueError(f"Particle renderer vertex streams must be an integer array: reference={identity}")
        return make_effect_material(materials[identity], textures, age, tuple(streams))

    scene["endfield_fx_snapshot_age_seconds"] = age
    scene["endfield_fx_snapshot_contract"] = "EndfieldStaticParticlePreview/1"
    physics = plan.get("effect_physics")
    if not isinstance(physics, dict):
        raise ValueError("Effect previews require captured game physics; regenerate the scene plan")
    gravity = physics.get("gravity")
    if not isinstance(gravity, list) or len(gravity) != 3:
        raise ValueError("Effect physics gravity must be a Unity Vector3")
    return build_scene_effects(instances, collection, material_factory, age, camera, 2000, 100000,
                               tuple(float(component) for component in gravity))


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    for name in ("plan", "output", "audit"):
        parser.add_argument("--" + name, type=Path, required=True)
    args = parser.parse_args(sys.argv[sys.argv.index("--") + 1:])
    if args.output.exists() or args.audit.exists():
        raise FileExistsError("Scene output and audit must be new files")
    if bpy.app.version < (4, 4, 0):
        raise RuntimeError("Portable scene materials require Blender 4.4 or newer")
    plan = load_scene_export_plan(args.plan)
    materials, textures = load_catalog(tuple(Path(path) for path in plan["scene_manifests"]))
    bpy.ops.object.select_all(action="SELECT")
    bpy.ops.object.delete(use_global=False)
    collection = bpy.data.collections.new("Endfield_Scene")
    bpy.context.scene.collection.children.link(collection)
    build_instances(plan["instances"], collection, materials, textures)
    terrain_collection = bpy.data.collections.new("Endfield_Terrain")
    bpy.context.scene.collection.children.link(terrain_collection)
    bounds = tuple(plan["bounds"][key] for key in ("xmin", "xmax", "zmin", "zmax"))
    for surface in plan["terrain_surfaces"]:
        root = resolve_export_path(Path(surface))
        metadata = json.loads((root / "terrain_surface.json").read_text(encoding="utf-8-sig"))
        build_terrain(root / metadata["height_database"], terrain_collection, bounds, root, False)
    for obj in terrain_collection.objects:
        if obj.type == "MESH":
            obj["endfield_source_heightfield"] = True
    terrain_materials = adapt_scene_terrain(bpy.context.scene)
    crops = crop_terrain_atlases(bpy.context.scene)
    terrain_normals = adapt_scene_terrain_mesh_normals(bpy.context.scene)
    terrain_details = [{"source": path, "materials": apply_scene_detail(bpy.context.scene, resolve_export_path(Path(path)))}
                       for path in plan.get("terrain_details", [])]
    bpy.context.view_layer.update()
    decals = project_decals(Path(plan["decal_manifest"]), Path(plan["decal_materials"]))
    from blender_scene_water_build import build_scene_water

    depth_collection = bpy.data.collections.new("Endfield_Water_Depth_Context")
    materials_before_depth = set(bpy.data.materials)
    images_before_depth = set(bpy.data.images)
    groups_before_depth = set(bpy.data.node_groups)
    meshes_before_depth = set(bpy.data.meshes)
    scene = bpy.context.scene
    scene.collection.children.link(depth_collection)
    depth_bounds = (bounds[0] - 0.001, bounds[1] + 0.001, bounds[2] - 0.001, bounds[3] + 0.001)
    for surface in plan.get("water_depth_surfaces", []):
        root = resolve_export_path(Path(surface))
        metadata = json.loads((root / "terrain_surface.json").read_text(encoding="utf-8-sig"))
        # Include adjacent source tiles for rays exactly on the delivery boundary.
        build_terrain(root / metadata["height_database"], depth_collection, depth_bounds, root, False)
        for obj in depth_collection.objects:
            if obj.type == "MESH":
                obj.data.materials.clear()
                obj["endfield_source_heightfield"] = True
                obj["endfield_water_bottom_context"] = "source_heightfield_without_surface_material"
    build_instances(plan.get("water_depth_instances", []), depth_collection, materials, textures)
    bpy.context.view_layer.update()
    water = build_scene_water(plan.get("water_geometry", []), plan.get("water_materials", []), plan["bounds"])
    for obj in tuple(depth_collection.objects):
        mesh = obj.data if obj.type == "MESH" else None
        bpy.data.objects.remove(obj, do_unlink=True)
        if mesh is not None and mesh.users == 0:
            bpy.data.meshes.remove(mesh)
    bpy.data.collections.remove(depth_collection)
    for mesh in set(bpy.data.meshes) - meshes_before_depth:
        if mesh.users == 0:
            bpy.data.meshes.remove(mesh)
    for material in set(bpy.data.materials) - materials_before_depth:
        if material.users == 0:
            bpy.data.materials.remove(material)
    while unused_groups := [group for group in set(bpy.data.node_groups) - groups_before_depth if group.users == 0]:
        for group in unused_groups:
            bpy.data.node_groups.remove(group)
    for image in set(bpy.data.images) - images_before_depth:
        if image.users == 0:
            bpy.data.images.remove(image)
    effects = build_effect_previews(plan, materials, textures)
    terrain_sampler_audit = audit_terrain_sampler_budget(bpy.context.scene)
    layout_trees = layout_shader_node_trees(tuple(
        material.node_tree for material in bpy.data.materials
        if material.use_nodes and material.users and material.node_tree is not None
    ))
    for image in bpy.data.images:
        if image.source == "FILE" and image.users and not image.packed_file:
            image.pack()
    bpy.context.view_layer.update()
    scene = bpy.context.scene
    scene.render.engine = "BLENDER_EEVEE_NEXT"
    scene["endfield_export_contract"] = "EndfieldPortableScenePlan/1"
    scene["endfield_map"] = plan["map_id"]
    scene["endfield_build_plan"] = args.plan.name
    outside_water = {row["entity_id"] for row in water if row["status"] == "outside_clipped_mesh"}
    active_pending = [row for row in plan["pending"]
                      if row["entity_id"] not in outside_water or row["component"] != "water_materials"]
    pending = (len(active_pending) + sum(row["status"] != "restored" for row in decals)
               + sum(row["status"] not in ("restored", "outside_clipped_mesh", "covered_by_terrain") for row in water)
               + sum(row["status"] not in ("static_preview", "renderer_disabled", "no_alive_particles_at_snapshot") for row in effects))
    scene["endfield_unresolved_resources"] = pending
    scene["endfield_source_axis"] = "Blender (X,Y,Z) = Unity (X,-Z,Y)"
    empty = not any(obj.type == "MESH" and obj.data.polygons for obj in scene.objects)
    if empty and (pending or set(plan["category_filter"]) != {"water"}):
        raise ValueError(
            "The selected categories have no reconstructable geometry; "
            f"plan_pending={len(plan['pending'])}; water_build={json.dumps(water)}; "
            f"water_depth_pending={len(plan.get('water_depth_pending', []))}; effects={json.dumps(effects)}; "
            "inspect scene_plan.json for resource details")
    scene["endfield_empty_selection"] = empty
    expected = scene_signature()
    scene.render.image_settings.file_format = "PNG"
    bpy.ops.wm.save_as_mainfile(filepath=str(args.output))
    bpy.ops.wm.open_mainfile(filepath=str(args.output))
    if scene_signature() != expected:
        raise ValueError("Saved scene geometry, transforms, UVs, particle colors or material slots changed on reopen")
    missing = [image.name for image in bpy.data.images if image.source == "FILE" and image.users and not image.packed_file]
    if missing:
        raise ValueError(f"Saved scene contains unpacked textures: {missing}")
    args.audit.write_text(json.dumps({"format": "EndfieldPortableSceneAudit/1", "reopen_verified": True,
        "instances": len(plan["instances"]), "pending": pending, "objects": len(expected), "empty_selection": empty,
        "terrain_materials": terrain_materials, "terrain_crops": crops, "terrain_normals": terrain_normals,
        "terrain_details": terrain_details, "terrain_sampler_audit": terrain_sampler_audit,
        "node_layout": {"contract": "EndfieldNodeLayout/2", "trees": layout_trees},
        "decals": decals, "water": water, "effects": effects,
        "effect_summary": {
            "source_instances": len(plan.get("effect_instances", [])),
            "component_rows": len(effects),
            "preview_components": sum(row["status"] in ("static_preview", "static_preview_partial") for row in effects),
            "preview_particles": sum(int(row.get("particles", 0)) for row in effects),
            "partial_components": sum(row["status"] == "static_preview_partial" for row in effects),
            "pending_includes_partial_previews": True,
        },
        "effect_snapshot": {"age_seconds": EFFECT_SNAPSHOT_AGE_SECONDS, "camera": "Endfield_FX_Snapshot_Camera"} if effects else None,
        "effect_physics": plan.get("effect_physics"),
        "water_depth_context_pending": plan.get("water_depth_pending", []), "limitations": ["Effects use camera-oriented static previews; Unity particle simulation, volume lighting and fog are not reconstructed",
        "Water uses calibrated Blender medium optics; source screen buffers, foam, caustics and interaction are not reproduced",
        "Terrain stochastic projection, POM and cliff C are not reconstructed"]}, indent=2), encoding="utf-8")


if __name__ == "__main__":
    main()
