"""Compose portable scenes with the existing exact geometry and material adapters."""
from __future__ import annotations

import argparse
import hashlib
import json
import numpy as np
from pathlib import Path
import struct
import sys
from collections.abc import Mapping, Sequence

sys.path.insert(0, str(Path(__file__).resolve().parent))

import bpy
from mathutils import Matrix, Vector

from blender_build_map02_chunk_20260727 import import_obj, join_submeshes, unity_matrix, build_terrain
from blender_scene_materials import create_material, layout_shader_node_trees
from blender_scene_terrain import adapt_scene_terrain, adapt_scene_terrain_mesh_normals
from blender_terrain_atlas import crop_terrain_atlases
from blender_terrain_detail import apply_scene_detail
from endfield_scene_material_contract import MaterialRecord, reference_key, resolve_export_path
from endfield_scene_material_handoff import load_plan_material_catalog
from endfield_scene_export_plan import load_scene_export_plan, PlanInstance
from endfield_scene_dynamic_geometry_plan import DynamicGeometryRow
from endfield_scene_mesh_uv_contract import required_uv_channels

TERRAIN_SAMPLER_BUDGET = 24
EFFECT_SNAPSHOT_AGE_SECONDS = 2.1
LIGHT_PREVIEW_PUNCTUAL_WATTS_PER_SOURCE_INTENSITY = 1.0
LIGHT_PREVIEW_SUN_WATTS_PER_SQUARE_METER_PER_SOURCE_INTENSITY = 1.0


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


def mesh_identity(row: PlanInstance | DynamicGeometryRow) -> str:
    selection = row.get("selected_submeshes", list(range(len(row["material_refs"]))))
    return hashlib.sha256(json.dumps([row["mesh_file"], row["material_refs"], selection,
                                     row.get("source_uv_streams", [])], sort_keys=True).encode()).hexdigest()


_Hash = type(hashlib.sha256())


def _signature_bytes(digest: _Hash, label: str, payload: bytes) -> None:
    digest.update(struct.pack("<Q", len(label)))
    digest.update(label.encode("utf-8"))
    digest.update(struct.pack("<Q", len(payload)))
    digest.update(payload)


def _signature_text(digest: _Hash, label: str, value: str) -> None:
    _signature_bytes(digest, label, value.encode("utf-8"))


def _signature_value(digest: _Hash, label: str, value: object) -> None:
    if value is None:
        _signature_bytes(digest, label, b"n")
    elif isinstance(value, bool):
        _signature_bytes(digest, label, b"b1" if value else b"b0")
    elif isinstance(value, int):
        _signature_bytes(digest, label, b"i" + struct.pack("<q", value))
    elif isinstance(value, float):
        _signature_bytes(digest, label, b"f" + struct.pack("<d", value))
    elif isinstance(value, str):
        _signature_bytes(digest, label, b"s" + value.encode("utf-8"))
    elif isinstance(value, Sequence):
        digest.update(struct.pack("<Q", len(label)))
        digest.update(label.encode("utf-8"))
        digest.update(b"t" + struct.pack("<Q", len(value)))
        for index, item in enumerate(value):
            _signature_value(digest, str(index), item)
    else:
        raise TypeError(f"Unsupported scene signature value: label={label}, type={type(value).__name__}")


def _signature_array(digest: _Hash, label: str, values: np.ndarray, dtype: str) -> None:
    stable = np.ascontiguousarray(values, dtype=np.dtype(dtype))
    digest.update(struct.pack("<Q", len(label)))
    digest.update(label.encode("utf-8"))
    digest.update(struct.pack("<Q", stable.ndim))
    for dimension in stable.shape:
        digest.update(struct.pack("<Q", dimension))
    if stable.nbytes:
        digest.update(memoryview(stable).cast("B"))


def _mesh_signature(mesh: bpy.types.Mesh) -> bytes:
    digest = hashlib.sha256()
    digest.update(b"EndfieldMeshSignature/2")

    coordinates = np.empty(len(mesh.vertices) * 3, dtype=np.float32)
    if coordinates.size:
        mesh.vertices.foreach_get("co", coordinates)
    _signature_array(digest, "vertex_coordinates", coordinates.reshape((-1, 3)), "<f4")

    loop_vertices = np.empty(len(mesh.loops), dtype=np.int32)
    if loop_vertices.size:
        mesh.loops.foreach_get("vertex_index", loop_vertices)
    _signature_array(digest, "loop_vertex_indices", loop_vertices, "<i4")
    loop_starts = np.empty(len(mesh.polygons), dtype=np.int32)
    if loop_starts.size:
        mesh.polygons.foreach_get("loop_start", loop_starts)
    _signature_array(digest, "polygon_loop_starts", loop_starts, "<i4")
    loop_totals = np.empty(len(mesh.polygons), dtype=np.int32)
    if loop_totals.size:
        mesh.polygons.foreach_get("loop_total", loop_totals)
    _signature_array(digest, "polygon_loop_totals", loop_totals, "<i4")
    material_indices = np.empty(len(mesh.polygons), dtype=np.int32)
    if material_indices.size:
        mesh.polygons.foreach_get("material_index", material_indices)
    _signature_array(digest, "polygon_material_indices", material_indices, "<i4")

    for index, layer in enumerate(mesh.uv_layers):
        _signature_text(digest, f"uv_layer_{index}_name", layer.name)
        coordinates = np.empty(len(layer.data) * 2, dtype=np.float32)
        if coordinates.size:
            layer.data.foreach_get("uv", coordinates)
        _signature_array(digest, f"uv_layer_{index}_coordinates", coordinates.reshape((-1, 2)), "<f4")

    for index, attribute in enumerate(mesh.color_attributes):
        _signature_text(digest, f"color_attribute_{index}_name", attribute.name)
        _signature_text(digest, f"color_attribute_{index}_domain", attribute.domain)
        _signature_text(digest, f"color_attribute_{index}_type", attribute.data_type)
        colors = np.empty(len(attribute.data) * 4, dtype=np.float32)
        if colors.size:
            attribute.data.foreach_get("color", colors)
        _signature_array(digest, f"color_attribute_{index}_values", colors.reshape((-1, 4)), "<f4")

    origin = mesh.attributes.get("endfield_receiver_world_origin")
    if origin is not None:
        _signature_text(digest, "receiver_origin_name", origin.name)
        _signature_text(digest, "receiver_origin_domain", origin.domain)
        _signature_text(digest, "receiver_origin_type", origin.data_type)
        vectors = np.empty(len(origin.data) * 3, dtype=np.float32)
        if vectors.size:
            origin.data.foreach_get("vector", vectors)
        _signature_array(digest, "receiver_origin_values", vectors.reshape((-1, 3)), "<f4")

    digest.update(struct.pack("<Q", len(mesh.materials)))
    for index, material in enumerate(mesh.materials):
        _signature_value(digest, f"material_slot_{index}", material.name if material else None)
    return digest.digest()


def scene_signature() -> dict[str, str]:
    """Record geometry, attributes, transforms, slots and light settings before saving."""
    signatures: dict[str, str] = {}
    mesh_fingerprints: dict[int, bytes] = {}
    for obj in bpy.context.scene.objects:
        digest = hashlib.sha256()
        digest.update(b"EndfieldObjectSignature/2")
        matrix = np.empty(16, dtype=np.float64)
        matrix[:] = [component for row in obj.matrix_world for component in row]
        _signature_array(digest, "matrix_world", matrix.reshape((4, 4)), "<f8")
        if "endfield_draw_parameters_status" in obj:
            _signature_text(digest, "draw_parameters_status", obj["endfield_draw_parameters_status"])
            if "endfield_serialized_draw_parameters" in obj:
                parameters = tuple(obj["endfield_serialized_draw_parameters"])
                if len(parameters) != 20:
                    raise ValueError(f"Expected 20 serialized draw parameters: object={obj.name}, count={len(parameters)}")
                digest.update(struct.pack("<20f", *parameters))
        if obj.type == "MESH":
            mesh_pointer = obj.data.as_pointer()
            if mesh_pointer not in mesh_fingerprints:
                mesh_fingerprints[mesh_pointer] = _mesh_signature(obj.data)
            _signature_bytes(digest, "mesh_fingerprint", mesh_fingerprints[mesh_pointer])
            _signature_value(digest, "effective_material_slots", tuple(
                (slot.link, slot.material.name if slot.material is not None else None)
                for slot in obj.material_slots))
        elif obj.type == "LIGHT":
            data = obj.data
            values = {"type": data.type, "color": tuple(data.color), "energy": data.energy,
                      "use_shadow": data.use_shadow, "use_nodes": data.use_nodes}
            for name in ("use_custom_distance", "cutoff_distance", "spot_size", "spot_blend",
                         "shadow_soft_size", "angle", "shape", "size", "size_y"):
                if hasattr(data, name):
                    values[name] = getattr(data, name)
            for name, value in sorted(values.items()):
                _signature_value(digest, f"light_{name}", value)
            if data.use_nodes and data.node_tree is not None:
                nodes = [node for node in sorted(data.node_tree.nodes, key=lambda item: item.name)
                         if node.type != "FRAME"]
                digest.update(struct.pack("<Q", len(nodes)))
                for node_index, node in enumerate(nodes):
                    _signature_text(digest, f"node_{node_index}_name", node.name)
                    _signature_text(digest, f"node_{node_index}_type", node.bl_idname)
                    _signature_value(digest, f"node_{node_index}_operation", getattr(node, "operation", None))
                    for socket in node.inputs:
                        if hasattr(socket, "default_value"):
                            value = socket.default_value
                            serialized = value if isinstance(value, (int, float, str, bool)) else tuple(value)
                            _signature_text(digest, f"node_{node_index}_socket", socket.identifier)
                            _signature_value(digest, f"node_{node_index}_socket_value", serialized)
                links = sorted((link.from_node.name, link.from_socket.identifier,
                                link.to_node.name, link.to_socket.identifier)
                               for link in data.node_tree.links)
                digest.update(struct.pack("<Q", len(links)))
                for link_index, link in enumerate(links):
                    _signature_value(digest, f"link_{link_index}", link)
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
        if any(slot.property == "_SampleTex1" for slot in spec.textures) and spec.floats["_SampleTex1UVSet"] != 0:
            unsupported.append("_SampleTex1UVSet")
        if unsupported or decal.packed_flags:
            result.append({"entity_id": decal.entity_id, "status": "unsupported_decal_feature", "features": unsupported})
            continue
        bias = 0.001 + (spec.floats.get("_DrawOrder", 0) - minimum) * 0.00001
        polygons, triangles = project_decal(decal, spec, receivers, grid, arrays, projected, collection, bias)
        result.append({"entity_id": decal.entity_id, "status": "restored" if polygons else "no_receiver_surface",
                       "polygons": polygons, "source_triangles": triangles})
    return result


def build_instances(
    rows: Sequence[PlanInstance | DynamicGeometryRow],
    collection: bpy.types.Collection,
    materials: dict[tuple[str, int], MaterialRecord],
    textures: dict[tuple[str, int], tuple[Path, str]],
) -> None:
    """Reuse exact scene slots for visible assets and temporary water bottoms."""
    material_cache: dict[tuple[str, int], bpy.types.Material] = {}
    mesh_cache: dict[str, bpy.types.Mesh] = {}
    placements: list[tuple[bpy.types.Object, Matrix]] = []
    for row in rows:
        placement_identity = row.get("source_key", row.get("entity_id"))
        selected = set(row.get("selected_submeshes", range(len(row["material_refs"]))))
        needed_uv = {channel for index, reference in enumerate(row["material_refs"])
                     if index in selected and reference is not None
                     for channel in required_uv_channels(materials[reference_key(reference)])}
        source_uv = [stream for stream in row.get("source_uv_streams", []) if stream["channel"] in needed_uv]
        if needed_uv != {stream["channel"] for stream in source_uv}:
            raise ValueError(f"Ordinary mesh source UV plan is stale or missing; regenerate exact mesh streams: placement={placement_identity}, required={sorted(needed_uv)}")
        key = mesh_identity(row)
        if key not in mesh_cache:
            imported = import_obj(Path(row["mesh_file"]))
            if len(imported) != len(row["material_refs"]):
                raise ValueError(f"Source submesh count changed: placement={placement_identity}")
            kept: list[bpy.types.Object] = []
            for submesh, (obj, reference) in enumerate(zip(imported, row["material_refs"], strict=True)):
                if submesh not in selected:
                    mesh = obj.data
                    bpy.data.objects.remove(obj, do_unlink=True)
                    if mesh.users == 0:
                        bpy.data.meshes.remove(mesh)
                    continue
                kept.append(obj)
                if source_uv:
                    from blender_scene_mesh_uvs import apply_source_uv_streams

                    apply_source_uv_streams(obj.data, source_uv)
                if reference is None:
                    material = undrawn_material()
                else:
                    material_key = reference_key(reference)
                    if material_key not in material_cache:
                        record = materials[material_key]
                        if record["Shader"].get("Name") == "HGRP/FogSimple":
                            from blender_scene_fog_materials import make_fog_material

                            material_cache[material_key] = make_fog_material(
                                record, textures, EFFECT_SNAPSHOT_AGE_SECONDS, None)
                        else:
                            material_cache[material_key] = create_material(record, textures, True)
                    material = material_cache[material_key]
                obj.data.materials.clear()
                obj.data.materials.append(material)
                for polygon in obj.data.polygons:
                    polygon.material_index = 0
            if not kept:
                raise ValueError(f"Source submesh selection is empty: placement={placement_identity}")
            template = join_submeshes(kept)
            mesh_cache[key] = template.data
            bpy.data.objects.remove(template, do_unlink=True)
        namespace = "DYNAMIC" if "source_key" in row else "ENTITY"
        obj = bpy.data.objects.new(f"{namespace}_{placement_identity}_{row['entity_base']}", mesh_cache[key])
        placements.append((obj, unity_matrix(struct.pack("<16f", *row["matrix"]))))
        if "source_key" in row:
            obj["endfield_dynamic_geometry_source_key"] = row["source_key"]
            obj["endfield_dynamic_geometry_source_json"] = json.dumps(row)
            obj["endfield_dynamic_geometry_status"] = "source_geometry_preview_activation_and_camera_lod_unverified"
        else:
            obj["entity_id"] = str(row["entity_id"])
            obj["endfield_static_source_json"] = json.dumps({
                name: row[name] for name in ("entity_id", "source_path", "source_sha256", "group_index", "entity_index")})
        obj["entity_base"] = row["entity_base"]
        obj["source_asset"] = row["mesh_asset"].get("Name", row["mesh_path"])
        obj["endfield_source_mesh"] = json.dumps(row["mesh_asset"])
        obj["endfield_material_refs"] = json.dumps(row["material_refs"])
        obj["endfield_selected_source_submeshes"] = json.dumps(row.get("selected_submeshes", list(range(len(row["material_refs"])))))
        if source_uv:
            obj["endfield_source_uv_streams_json"] = json.dumps(source_uv)
        obj["endfield_category"] = row["category"]
        parameters = row.get("serialized_draw_parameters")
        obj["endfield_draw_parameters_status"] = (
            "serialized_initial_not_runtime" if parameters is not None else "unavailable_in_plan")
        if parameters is not None:
            obj["endfield_serialized_draw_parameters"] = parameters

    # Reserve names in source order without growing the view layer during imports and joins.
    for obj, matrix in placements:
        collection.objects.link(obj)
        obj.matrix_world = matrix


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
    from blender_scene_effect_meshes import build_scene_effect_meshes, build_direct_effect_meshes
    from blender_scene_effect_materials import make_effect_material, make_mesh_effect_material

    instances = plan.get("effect_instances", [])
    if not isinstance(instances, list):
        raise ValueError("Scene effect_instances must be an array")
    direct = plan.get("direct_effect_renderers", [])
    if not isinstance(direct, list):
        raise ValueError("Scene direct_effect_renderers must be an array")
    if not instances and not direct:
        return []
    for instance in instances:
        if not isinstance(instance, dict) or instance.get("component_kind", "particle") not in (
                "particle", "mesh_renderer", "mixed"):
            raise ValueError(f"Scene effect has an invalid component kind: instance={instance}")
    particle_instances = [row for row in instances if row.get("component_kind", "particle") != "mesh_renderer"]
    mesh_instances = [row for row in instances if row.get("component_kind", "particle") != "particle"]
    snapshot = plan.get("scene_color_snapshot")
    age = snapshot["time_seconds"] if snapshot is not None else EFFECT_SNAPSHOT_AGE_SECONDS
    scene = bpy.context.scene
    collection = bpy.data.collections.new("Endfield_Effects_Static_Preview")
    scene.collection.children.link(collection)
    camera = scene.camera if snapshot is not None else effect_preview_camera([*instances, *direct], scene)
    if camera is None:
        raise ValueError("Explicit SceneColor camera must be created before effect geometry")

    def material_factory(reference: Mapping[str, object], renderer: Mapping[str, object],
                         system: Mapping[str, object]) -> bpy.types.Material:
        identity = reference_key(reference)
        if identity not in materials:
            raise FileNotFoundError(f"Particle material was not extracted: reference={identity}")
        streams = renderer.get("VertexStreams")
        if not isinstance(streams, list) or any(type(value) is not int for value in streams):
            raise ValueError(f"Particle renderer vertex streams must be an integer array: reference={identity}")
        return make_effect_material(materials[identity], textures, age, tuple(streams))

    def mesh_material_factory(reference: Mapping[str, object], node: Mapping[str, object],
                              vertex_color_components: int) -> bpy.types.Material:
        identity = reference_key(reference)
        if identity not in materials:
            raise FileNotFoundError(f"MeshRenderer FX material was not extracted: reference={identity}")
        if snapshot is not None and materials[identity]["Shader"].get("Name") == "HGRP/Effect/VFXRefract":
            from blender_scene_vfxrefract import make_vfxrefract_capture_material

            return make_vfxrefract_capture_material(materials[identity], vertex_color_components)
        return make_mesh_effect_material(materials[identity], textures, age, vertex_color_components)

    scene["endfield_fx_snapshot_age_seconds"] = age
    scene["endfield_fx_snapshot_contract"] = "EndfieldStaticParticlePreview/1"
    audit: list[dict[str, object]] = []
    if particle_instances:
        physics = plan.get("effect_physics")
        if not isinstance(physics, dict):
            raise ValueError("Effect previews require captured game physics; regenerate the scene plan")
        gravity = physics.get("gravity")
        if not isinstance(gravity, list) or len(gravity) != 3:
            raise ValueError("Effect physics gravity must be a Unity Vector3")
        audit.extend(build_scene_effects(particle_instances, collection, material_factory, age, camera,
                                        2000, 100000, tuple(float(component) for component in gravity)))
    audit.extend(build_scene_effect_meshes(mesh_instances, collection, mesh_material_factory, 2000, age, camera))
    audit.extend(build_direct_effect_meshes(direct, collection, mesh_material_factory, age, camera))
    return audit


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
    if "water_transport_lighting" in plan:
        pending_water = [row for row in plan["pending"]
                         if row["component"] in {"water_geometry", "water_materials"}]
        if not plan["water_materials"] or pending_water:
            raise ValueError(f"Water transport lighting requires ready water geometry and materials: pending={pending_water}")
    materials, textures = load_plan_material_catalog(plan)
    bpy.ops.object.select_all(action="SELECT")
    bpy.ops.object.delete(use_global=False)
    if "scene_color_snapshot" in plan:
        from blender_scene_vfxrefract import create_scene_color_camera

        create_scene_color_camera(bpy.context.scene, plan["scene_color_snapshot"])
    collection = bpy.data.collections.new("Endfield_Scene")
    build_instances(plan["instances"], collection, materials, textures)
    dynamic_collection = bpy.data.collections.new("Endfield_Dynamic_Geometry_Reference")
    build_instances(plan["dynamic_geometry_instances"], dynamic_collection, materials, textures)
    bpy.context.scene.collection.children.link(collection)
    bpy.context.scene.collection.children.link(dynamic_collection)
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
    surface_collection = bpy.data.collections.new("Endfield_Surface_Effects_Static_Preview")
    scene = bpy.context.scene
    scene.collection.children.link(surface_collection)
    build_instances(plan.get("surface_effect_instances", []), surface_collection, materials, textures)
    for obj in surface_collection.objects:
        obj["endfield_fx_surface_source_json"] = obj["endfield_static_source_json"]
    effects = build_effect_previews(plan, materials, textures)
    fog_depth = None
    if "fog_depth" in plan:
        from blender_scene_fog_depth import apply_scene_fog_depth

        fog_depth = apply_scene_fog_depth(scene, plan["fog_depth"]["subdivision_level"])
    from blender_scene_lights import build_scene_lights

    light_collection = bpy.data.collections.new("Endfield_Lights_Preview")
    scene.collection.children.link(light_collection)
    lights = build_scene_lights(plan["light_instances"], light_collection,
                                LIGHT_PREVIEW_PUNCTUAL_WATTS_PER_SOURCE_INTENSITY,
                                LIGHT_PREVIEW_SUN_WATTS_PER_SQUARE_METER_PER_SOURCE_INTENSITY, 4096)
    water_transport = None
    if "water_transport_lighting" in plan:
        from blender_scene_water_transport import apply_scene_water_transport
        from endfield_water_transport import read_water_transport_lighting

        water_transport = apply_scene_water_transport(
            scene, plan["water_materials"], read_water_transport_lighting(plan["water_transport_lighting"]))
        water = [{**row, "detail": "Source transport coefficients and measured bottom; explicit host lighting"}
                 if row["entity_id"] in water_transport["entity_ids"] else row for row in water]
    from blender_scene_environment import setup_environment

    scene.render.engine = (plan["scene_color_snapshot"]["engine"]
                           if "scene_color_snapshot" in plan else "BLENDER_EEVEE_NEXT")
    environment = setup_environment(scene)
    scene_color_snapshot = None
    if "scene_color_snapshot" in plan:
        from blender_scene_vfxrefract import finalize_scene_vfxrefract, verify_scene_vfxrefract

        for image in bpy.data.images:
            if image.source == "FILE" and image.users and not image.packed_file:
                image.pack()
        scene_color_snapshot = finalize_scene_vfxrefract(
            scene, textures, plan["scene_color_snapshot"], args.output.with_suffix(".scene_color.exr"))
        verify_scene_vfxrefract(scene, scene_color_snapshot)
        effects = [dict(row, material=scene.objects[row["object"]].material_slots[0].material.name,
                        material_status="offline_camera_snapshot_refraction",
                        material_limitations=json.loads(scene.objects[row["object"]].material_slots[0].material["endfield_fx_limitations"]))
                   if row.get("material_status") == "awaiting_host_scene_color" else row for row in effects]
    used_materials = sorted({slot.material for obj in scene.objects if obj.type == "MESH"
                             for slot in obj.material_slots if slot.material is not None},
                            key=lambda material: material.name)
    material_pending = [{"material": material.name, "component": "emission_sweep",
                         "reason": material["endfield_emissive_motion_reason"],
                         "status": material["endfield_emissive_motion_status"]}
                        for material in used_materials
                        if material.get("endfield_emissive_motion_status") in (
                            "unsupported_sweep_variant", "source_formula_preview_runtime_unresolved")]
    material_pending.extend({"material": material.name, "component": "fog_surface",
                             "reason": material["endfield_fx_limitations"],
                             "status": material["endfield_fx_status"]}
                            for material in used_materials
                            if material.get("endfield_fx_fog_simple_shader") == "HGRP/FogSimple")
    material_pending.extend({"material": material.name, "component": "top_layer_runtime_inputs",
                             "reason": material["endfield_top_layer_limitations"],
                             "status": "source_formula_preview_runtime_unresolved"}
                            for material in used_materials
                            if material.get("endfield_top_layer_contract"))
    terrain_sampler_audit = audit_terrain_sampler_budget(bpy.context.scene)
    layout_trees = layout_shader_node_trees(tuple(
        data.node_tree for data in (*bpy.data.materials, *bpy.data.lights, *bpy.data.worlds)
        if data.use_nodes and data.users and data.node_tree is not None
    ))
    for image in bpy.data.images:
        if image.source == "FILE" and image.users and not image.packed_file:
            image.pack()
    bpy.context.view_layer.update()
    scene = bpy.context.scene
    scene.render.engine = (plan["scene_color_snapshot"]["engine"]
                           if "scene_color_snapshot" in plan else "BLENDER_EEVEE_NEXT")
    scene["endfield_export_contract"] = "EndfieldPortableScenePlan/1"
    scene["endfield_map"] = plan["map_id"]
    scene["endfield_build_plan"] = args.plan.name
    outside_water = {row["entity_id"] for row in water if row["status"] == "outside_clipped_mesh"}
    active_pending = [row for row in plan["pending"]
                      if row["entity_id"] not in outside_water or row["component"] != "water_materials"]
    pending = (len(active_pending) + len(plan.get("direct_effect_pending", [])) + len(plan["dynamic_geometry_pending"])
               + sum(row["status"] != "restored" for row in decals)
               + sum(row["status"] not in ("restored", "outside_clipped_mesh", "covered_by_terrain") for row in water)
               + sum(row["status"] not in ("static_preview", "renderer_disabled", "game_object_inactive", "no_alive_particles_at_snapshot") for row in effects)
               + len(lights) + len(material_pending))
    scene["endfield_unresolved_resources"] = pending
    scene["endfield_source_axis"] = "Blender (X,Y,Z) = Unity (X,-Z,Y)"
    empty = not any((obj.type == "MESH" and obj.data.polygons) or obj.type == "LIGHT" for obj in scene.objects)
    if empty and (pending or set(plan["category_filter"]) != {"water"}):
        raise ValueError(
            "The selected categories have no reconstructable geometry; "
            f"plan_pending={len(plan['pending'])}; water_build={json.dumps(water)}; "
            f"water_depth_pending={len(plan.get('water_depth_pending', []))}; effects={json.dumps(effects)}; lights={json.dumps(lights)}; "
            "inspect scene_plan.json for resource details")
    scene["endfield_empty_selection"] = empty
    expected = scene_signature()
    scene.render.image_settings.file_format = "PNG"
    bpy.ops.wm.save_as_mainfile(filepath=str(args.output))
    bpy.ops.wm.open_mainfile(filepath=str(args.output))
    if scene_signature() != expected:
        raise ValueError("Saved scene geometry, transforms, UVs, particle colors, material slots or light settings changed on reopen")
    if setup_environment(bpy.context.scene) != environment:
        raise ValueError("Saved scene reference environment controls or preserved source World changed on reopen")
    if scene_color_snapshot is not None:
        verify_scene_vfxrefract(bpy.context.scene, scene_color_snapshot)
    missing = [image.name for image in bpy.data.images if image.source == "FILE" and image.users and not image.packed_file]
    if missing:
        raise ValueError(f"Saved scene contains unpacked textures: {missing}")
    args.audit.write_text(json.dumps({"format": "EndfieldPortableSceneAudit/1", "reopen_verified": True,
        "instances": len(plan["instances"]) + len(plan["dynamic_geometry_instances"]),
        "static_instances": len(plan["instances"]), "dynamic_geometry_instances": len(plan["dynamic_geometry_instances"]),
        "surface_effect_instances": len(plan.get("surface_effect_instances", [])), "pending": pending, "objects": len(expected), "empty_selection": empty,
        "terrain_materials": terrain_materials, "terrain_crops": crops, "terrain_normals": terrain_normals,
        "terrain_details": terrain_details, "terrain_sampler_audit": terrain_sampler_audit,
        "node_layout": {"contract": "EndfieldNodeLayout/2", "trees": layout_trees},
        "decals": decals, "water": water, "water_transport": water_transport,
        "effects": effects, "fog_depth": fog_depth, "lights": lights, "environment": environment,
        "scene_color_snapshot": scene_color_snapshot,
        "direct_effect_pending": plan.get("direct_effect_pending", []),
        "dynamic_geometry_pending": plan["dynamic_geometry_pending"],
        "dynamic_geometry_role_exclusions": [
            {"source_key": row["source_key"], **exclusion}
            for row in plan["dynamic_geometry_instances"] for exclusion in row.get("excluded_source_draw_roles", [])],
        "dynamic_geometry_status": "authored_base_lod_reference_with_unverified_runtime_activation",
        "material_pending": material_pending,
        "light_summary": {"source_instances": len(plan["light_instances"]),
                          "preview_lights": sum(row["status"] == "source_light_preview_partial" for row in lights),
                          "pending_includes_uncalibrated_previews": True},
        "effect_summary": {
            "source_instances": len(plan.get("effect_instances", [])),
            "component_rows": len(effects),
            "preview_components": sum(row["status"] in ("static_preview", "static_preview_partial") for row in effects),
            "preview_particles": sum(int(row.get("particles", 0)) for row in effects),
            "partial_components": sum(row["status"] == "static_preview_partial" for row in effects),
            "pending_includes_partial_previews": True,
        },
        "effect_snapshot": {"age_seconds": bpy.context.scene["endfield_fx_snapshot_age_seconds"],
                            "camera": bpy.context.scene.camera.name} if effects else None,
        "effect_physics": plan.get("effect_physics"),
        "water_depth_context_pending": plan.get("water_depth_pending", []), "limitations": ["This scene stores source-driven effect snapshots; native runtime activation, unsupported particle modules and full volumetric lighting remain unverified",
        "Supported fog branches use source formulas and optional host camera-depth samples; native screen depth, exposure and grading remain unverified",
        "Ordinary Dynamic ECS geometry uses the authored base LOD; runtime activation and camera-selected LOD remain unverified",
        (water_transport["boundary"] if water_transport else
         "Supported water branches include Blender medium optics and source-guided shoreline/foam; native screen buffers, caustics and runtime interactions remain unverified"),
        "Native lights preserve source transforms; intensity units and runtime activation remain uncalibrated previews",
        "Reference environment fill is editable and non-native; disabling it restores the preserved source World fill",
        "Light centers use half-open chunk ownership; off-region light contributors are excluded and directional lights belong to one selection batch",
        "Terrain stochastic projection, POM and cliff C are not reconstructed"]}, indent=2), encoding="utf-8")


if __name__ == "__main__":
    main()
