"""Build bounded water surfaces through the existing dual-engine optics core."""
from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import replace
import json
import math
from pathlib import Path
import struct
from typing import TypedDict

import bmesh
import bpy
from mathutils import Matrix, Vector

from blender_build_map02_chunk_20260727 import import_obj, join_submeshes, unity_matrix
from blender_scene_water import WaterOptics, adapt_water_surface, configure_water_scene, create_water_volume
from blender_scene_water_depth import WaterBottomGeometryMissing, WaterDepthAudit, WaterDepthResult, sample_water_depths
from endfield_scene_water_materials import read_water_material_row


class WaterBuildRecord(TypedDict):
    entity_id: int
    status: str
    detail: str
    vertices: int
    missing_bottom_vertices: int


def clip_water_surface(obj: bpy.types.Object, bounds: Mapping[str, float]) -> bool:
    """Clip a private mesh copy in world space; adjacent batches share boundaries."""
    obj.data.transform(obj.matrix_world)
    obj.matrix_world = Matrix.Identity(4)
    mesh = bmesh.new()
    try:
        mesh.from_mesh(obj.data)
        planes = (
            ((bounds["xmin"], 0, 0), (-1, 0, 0)),
            ((bounds["xmax"], 0, 0), (1, 0, 0)),
            ((0, -bounds["zmin"], 0), (0, 1, 0)),
            ((0, -bounds["zmax"], 0), (0, -1, 0)),
        )
        for origin, normal in planes:
            if not mesh.faces:
                break
            bmesh.ops.bisect_plane(
                mesh, geom=[*mesh.verts, *mesh.edges, *mesh.faces], dist=0.000001,
                plane_co=Vector(origin), plane_no=Vector(normal),
                clear_outer=True, clear_inner=False)
        loose = [vertex for vertex in mesh.verts if not vertex.link_faces]
        if loose:
            bmesh.ops.delete(mesh, geom=loose, context="VERTS")
        mesh.to_mesh(obj.data)
    finally:
        mesh.free()
    obj.data.update()
    return bool(obj.data.polygons)


def _drop_surface(obj: bpy.types.Object) -> None:
    mesh = obj.data
    bpy.data.objects.remove(obj, do_unlink=True)
    if mesh.users == 0:
        bpy.data.meshes.remove(mesh)


def refine_water_depth_samples(obj: bpy.types.Object, spacing: float) -> None:
    """Bound sampling spacing on large source quads without changing their shape."""
    if not math.isfinite(spacing) or spacing <= 0:
        raise ValueError(f"WATER_DEPTH_SAMPLE_SPACING_INVALID spacing={spacing}")
    mesh = bmesh.new()
    try:
        mesh.from_mesh(obj.data)
        while True:
            edges = [edge for edge in mesh.edges if edge.calc_length() > spacing]
            if not edges:
                break
            if len(mesh.verts) + len(edges) > 100000:
                raise ValueError(f"WATER_DEPTH_SAMPLE_BUDGET entity={obj.name}; export smaller batches")
            bmesh.ops.subdivide_edges(mesh, edges=edges, cuts=1, use_grid_fill=True)
        mesh.to_mesh(obj.data)
    finally:
        mesh.free()
    obj.data.update()
    obj["endfield_water_depth_max_sample_edge_meters"] = spacing


def _measure_bottoms(
    graph: bpy.types.Depsgraph, waters: tuple[bpy.types.Object, ...],
) -> tuple[WaterDepthAudit, str | None]:
    """Retain absent bottoms for dry-shore classification without inventing hits."""
    try:
        return sample_water_depths(graph, waters, 150.0, 0.005), None
    except WaterBottomGeometryMissing as error:
        rows = tuple(WaterDepthResult(obj.name, tuple(None for _ in obj.data.vertices)) for obj in waters)
        return WaterDepthAudit(rows, 0, 0, ()), str(error)


def build_scene_water(
    geometry_rows: Sequence[Mapping[str, object]],
    material_rows: Sequence[object],
    bounds: Mapping[str, float],
) -> list[WaterBuildRecord]:
    """Report unresolved bottoms instead of inventing bathymetry or opaque water."""
    from blender_scene_water_waves import build_water_normal

    if not geometry_rows:
        return []
    materials = {spec.entity_id: spec for spec in map(read_water_material_row, material_rows)}
    collection = bpy.data.collections.new("Endfield_Water")
    bpy.context.scene.collection.children.link(collection)
    objects: list[tuple[int, bpy.types.Object]] = []
    results: list[WaterBuildRecord] = []
    for row in geometry_rows:
        entity = row["entity_id"]
        if type(entity) is not int:
            raise ValueError("WATER_ENTITY_ID_MUST_BE_INTEGER")
        spec = materials.get(entity)
        matrix = row["matrix"]
        if not isinstance(matrix, (tuple, list)) or len(matrix) != 16 or any(
            type(value) not in (int, float) or not math.isfinite(value) for value in matrix
        ):
            raise ValueError(f"WATER_MATRIX_INVALID entity_id={entity}")
        path = row["mesh_file"]
        if not isinstance(path, str) or not Path(path).is_file():
            raise FileNotFoundError(f"WATER_MESH_PAYLOAD_MISSING entity_id={entity} path={path}")
        imported = import_obj(Path(path))
        if not imported:
            raise ValueError(f"WATER_MESH_EMPTY entity_id={entity} path={path}")
        obj = join_submeshes(imported)
        obj.name = f"WATER_{entity}_{row['name']}"
        for prior in tuple(obj.users_collection):
            prior.objects.unlink(obj)
        collection.objects.link(obj)
        obj.matrix_world = unity_matrix(struct.pack("<16f", *matrix))
        if not clip_water_surface(obj, bounds):
            results.append({"entity_id": entity, "status": "outside_clipped_mesh",
                            "detail": "Source bounds intersect the selection but actual mesh faces do not",
                            "vertices": 0, "missing_bottom_vertices": 0})
            _drop_surface(obj)
            continue
        if spec is None or not spec.sector_complete:
            # The plan retains material failures only for actual clipped surfaces.
            _drop_surface(obj)
            continue
        refine_water_depth_samples(obj, 8.0)
        obj.data.materials.clear()
        obj["entity_id"] = str(entity)
        obj["endfield_category"] = "water"
        obj["endfield_water_render_mask"] = int(row["render_type_mask"])
        obj["endfield_source_mesh"] = json.dumps(row["mesh_asset"])
        obj["endfield_water_config"] = spec.config_resource_path
        objects.append((entity, obj))
    if not objects:
        return results
    bpy.context.view_layer.update()
    depth_audit, bottom_error = _measure_bottoms(
        bpy.context.evaluated_depsgraph_get(), tuple(obj for _, obj in objects))
    heightfields = tuple(obj for obj in bpy.context.scene.objects
                         if obj.type == "MESH" and obj.get("endfield_source_heightfield") is True)
    shore_changed = False
    retained: list[tuple[int, bpy.types.Object]] = []
    for (entity, obj), depth in zip(objects, depth_audit.waters, strict=True):
        if heightfields and any(hit is None for hit in depth.hits):
            from blender_scene_water_shore import clip_water_to_heightfield

            clipped = clip_water_to_heightfield(obj, heightfields, 0.02)
            if clipped:
                shore_changed = True
                if not obj.data.polygons:
                    results.append({"entity_id": entity, "status": "covered_by_terrain",
                                    "detail": "Source water surface lies below the verified heightfield",
                                    "vertices": 0, "missing_bottom_vertices": 0})
                    _drop_surface(obj)
                    continue
                obj["endfield_water_shore_clearance_meters"] = 0.02
                obj["endfield_water_shore_clipped_faces"] = clipped
                refine_water_depth_samples(obj, 8.0)
        retained.append((entity, obj))
    objects = retained
    if not objects:
        return results
    if shore_changed:
        bpy.context.view_layer.update()
        depth_audit, bottom_error = _measure_bottoms(
            bpy.context.evaluated_depsgraph_get(), tuple(obj for _, obj in objects))
    for (entity, obj), depth in zip(objects, depth_audit.waters, strict=True):
        missing = sum(hit is None for hit in depth.hits)
        if missing:
            results.append({"entity_id": entity, "status": "bottom_geometry_missing" if bottom_error else "bottom_geometry_incomplete",
                            "detail": bottom_error or "No opaque bottom hit within 150 m at every clipped surface vertex; include terrain and bottom assets",
                            "vertices": len(obj.data.vertices), "missing_bottom_vertices": missing})
            _drop_surface(obj)
            continue
        spec = materials[entity]
        global_config = json.loads(spec.global_source_json)
        size = global_config["SectorSize"]
        center = global_config["SceneCenterOffset"]
        sectors = tuple(sector for sector in spec.sectors
                        if sector.sector_x * size + center["X"] < bounds["xmax"]
                        and (sector.sector_x + 1) * size + center["X"] > bounds["xmin"]
                        and sector.sector_z * size + center["Y"] < bounds["zmax"]
                        and (sector.sector_z + 1) * size + center["Y"] > bounds["zmin"])
        spec = replace(spec, sectors=sectors)
        config = json.loads(spec.config_source_json)
        roughness = config["Appearance"]["Roughness"]
        if type(roughness) not in (int, float) or not math.isfinite(roughness) or not 0 <= roughness <= 1:
            raise ValueError(f"WATER_ROUGHNESS_INVALID entity_id={entity} value={roughness}")
        optics = WaterOptics(1.333, float(roughness), 0.35, 0.015,
                            (0.08, 0.72, 0.58, 1.0), "endfield_water_depth")
        material = bpy.data.materials.new(f"Endfield_Water_{entity}")
        material.use_nodes = True
        normal = build_water_normal(material, spec)
        adapt_water_surface(material, normal, optics)
        material["endfield_water_config"] = spec.config_resource_path
        material["endfield_source_screen_distortion"] = spec.flow.water_ior
        material["endfield_optics_parameters"] = (
            "Source roughness; retained validated Blender IOR and medium calibration. "
            "Source WaterIor scales screen distortion and is not physical IOR.")
        values = tuple(hit.depth for hit in depth.hits if hit is not None)
        attribute = obj.data.attributes.new(optics.depth_attribute, "FLOAT", "POINT")
        attribute.data.foreach_set("value", values)
        obj.data.materials.append(material)
        for face in obj.data.polygons:
            face.material_index = 0
        volume = create_water_volume(obj, values, material)
        collection.objects.link(volume)
        results.append({"entity_id": entity, "status": "restored", "detail": "Source waves and measured bottom; calibrated dual-engine optics",
                        "vertices": len(obj.data.vertices), "missing_bottom_vertices": 0})
    if any(row["status"] == "restored" for row in results):
        configure_water_scene(bpy.context.scene)
    return results
