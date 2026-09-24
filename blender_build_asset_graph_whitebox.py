"""Build a small read-only whitebox preview from an asset-resolution database.

This is a visualization helper, not a replacement extractor. It consumes the
existing instance database, resolution table, and SceneProbe OBJ exports. The
whitebox deliberately excludes HLOD assets and uses one neutral material.
"""

from __future__ import annotations

import argparse
import json
import math
import re
import sqlite3
import struct
import sys
from collections import Counter, defaultdict
from pathlib import Path
from typing import Any

import bpy
from mathutils import Matrix, Vector


AXIS_CONTRACT = "(X,Y,Z)=(Unity X,-Unity Z,Unity Y)"
OBJ_X_UNMIRROR = Matrix.Diagonal((-1.0, 1.0, 1.0, 1.0))
HLOD_RE = re.compile(r"(?:^|[/_])hlod\d+(?:[/_]|$)", re.IGNORECASE)
LOD_RE = re.compile(r"_lod(\d+)(?:_|$)", re.IGNORECASE)


def arguments() -> argparse.Namespace:
    argv = sys.argv[sys.argv.index("--") + 1 :] if "--" in sys.argv else []
    parser = argparse.ArgumentParser(description="Build a bounded Endfield whitebox preview")
    parser.add_argument("--map-id", choices=("map01", "map02"), required=True)
    parser.add_argument("--work-root", type=Path, required=True)
    parser.add_argument("--assets-db", type=Path, required=True)
    parser.add_argument("--scene-probe", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--xmin", type=float, required=True)
    parser.add_argument("--xmax", type=float, required=True)
    parser.add_argument("--zmin", type=float, required=True)
    parser.add_argument("--zmax", type=float, required=True)
    parser.add_argument("--label", default="asset-graph-whitebox")
    parser.add_argument("--ground-z", type=float, default=0.0)
    return parser.parse_args(argv)


def is_hlod(value: str | None) -> bool:
    return bool(HLOD_RE.search(str(value or "").replace("\\", "/")))


def lod_number(value: str | None) -> int:
    match = LOD_RE.search(str(value or ""))
    return int(match.group(1)) if match else 99


def unity_matrix(blob: bytes) -> Matrix:
    values = struct.unpack("<16f", blob)
    source = Matrix(
        tuple(tuple(values[column * 4 + row] for column in range(4)) for row in range(4))
    )
    basis = Matrix(
        ((1, 0, 0, 0), (0, 0, -1, 0), (0, 1, 0, 0), (0, 0, 0, 1))
    )
    return basis @ source @ basis.inverted()


def safe_name(value: str, limit: int = 62) -> str:
    return re.sub(r"[^0-9A-Za-z_.-]+", "_", value).strip("_")[:limit] or "unnamed"


def ref_key(value: dict[str, Any]) -> tuple[str, int] | None:
    source = value.get("Source")
    path_id = value.get("PathId")
    if not isinstance(source, str) or path_id is None:
        return None
    try:
        return source.casefold(), int(path_id)
    except (TypeError, ValueError):
        return None


def import_obj(path: Path) -> bpy.types.Mesh | None:
    before = set(bpy.data.objects)
    bpy.ops.wm.obj_import(
        filepath=str(path),
        forward_axis="NEGATIVE_Z",
        up_axis="Y",
        use_split_objects=False,
        use_split_groups=True,
    )
    imported = [obj for obj in set(bpy.data.objects) - before if obj.type == "MESH"]
    if not imported:
        return None
    for obj in imported:
        obj.data.transform(obj.matrix_world)
        obj.data.transform(OBJ_X_UNMIRROR)
        obj.data.flip_normals()
        obj.data.update()
        obj.matrix_world = Matrix.Identity(4)
    if len(imported) > 1:
        bpy.ops.object.select_all(action="DESELECT")
        for obj in imported:
            obj.select_set(True)
        bpy.context.view_layer.objects.active = imported[0]
        bpy.ops.object.join()
    result = imported[0]
    mesh = result.data.copy()
    bpy.data.objects.remove(result, do_unlink=True)
    return mesh


def make_neutral_material() -> bpy.types.Material:
    material = bpy.data.materials.new("Whitebox_Neutral_Material")
    material.diffuse_color = (0.72, 0.76, 0.80, 1.0)
    material.use_nodes = True
    principled = next(node for node in material.node_tree.nodes if node.type == "BSDF_PRINCIPLED")
    principled.inputs["Base Color"].default_value = (0.72, 0.76, 0.80, 1.0)
    principled.inputs["Roughness"].default_value = 0.88
    material["note"] = "Whitebox preview material; not an extracted game material"
    return material


def look_at(camera: bpy.types.Object, target: Vector) -> None:
    camera.rotation_euler = (target - camera.location).to_track_quat("-Z", "Y").to_euler()


def build_scene(args: argparse.Namespace) -> dict[str, Any]:
    bounds = (args.xmin, args.xmax, args.zmin, args.zmax)
    if args.xmax <= args.xmin or args.zmax <= args.zmin:
        raise RuntimeError("Whitebox bounds must have positive area")
    work_root = args.work_root.resolve()
    assets_db = args.assets_db.resolve()
    # Keep junctions/symlinks intact. Blender's OBJ importer rejects otherwise
    # valid files when the extracted SceneProbe path exceeds its Windows path
    # handling limit; callers may provide a short external junction.
    probe_root = args.scene_probe.absolute()
    output = args.output.resolve()
    if output.exists():
        raise RuntimeError(f"Refusing to overwrite whitebox output: {output}")
    instance_db = work_root / "instances.sqlite"
    manifest_path = probe_root / "scene_manifest.json"
    for required in (instance_db, assets_db, manifest_path):
        if not required.is_file():
            raise RuntimeError(f"Missing whitebox input: {required}")
    manifest = json.loads(manifest_path.read_text(encoding="utf-8-sig"))
    if manifest.get("Format") != "EndfieldSceneProbe/1":
        raise RuntimeError(f"Unsupported SceneProbe manifest: {manifest_path}")

    mesh_by_ref: dict[tuple[str, int], dict[str, Any]] = {}
    mesh_by_source_name: dict[tuple[str, str], dict[str, Any]] = {}
    mesh_export_count = 0
    for entry in manifest.get("MeshExports") or []:
        asset = entry.get("Asset") or {}
        key = ref_key(asset)
        file_name = entry.get("File")
        if key is None or not isinstance(file_name, str) or is_hlod(asset.get("Name")):
            continue
        row = {"asset": asset, "file": file_name}
        mesh_by_ref[key] = row
        mesh_by_source_name[(str(asset.get("Source")).casefold(), str(asset.get("Name")).casefold())] = row
        mesh_export_count += 1

    nodes_by_source: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for node in manifest.get("Nodes") or []:
        source = node.get("Source")
        if isinstance(source, str):
            nodes_by_source[source.casefold()].append(node)

    def export_for_resolution(method: str, root_cab: str | None, model_name: str | None) -> dict[str, Any] | None:
        if not root_cab:
            return None
        if method == "model" and model_name:
            exact = mesh_by_source_name.get((root_cab.casefold(), model_name.casefold()))
            if exact is not None:
                return exact
        if method != "prefab":
            return None
        candidates = [
            node
            for node in nodes_by_source.get(root_cab.casefold(), [])
            if node.get("RendererType") == "MeshRenderer"
            and isinstance(node.get("Mesh"), dict)
            and not is_hlod((node.get("Mesh") or {}).get("Name"))
        ]
        candidates.sort(key=lambda node: (lod_number((node.get("Mesh") or {}).get("Name")), str(node.get("Name") or "").casefold()))
        for node in candidates:
            key = ref_key(node["Mesh"])
            if key in mesh_by_ref:
                return mesh_by_ref[key]
        return None

    resolution_by_base: dict[str, tuple[str, str | None, str | None]] = {}
    connection = sqlite3.connect(f"file:{assets_db}?mode=ro", uri=True)
    try:
        for base, method, root_cab, model_name in connection.execute(
            "SELECT entity_base,method,root_cab,model_asset_name FROM resolutions ORDER BY entity_base"
        ):
            resolution_by_base[str(base)] = (str(method), root_cab, model_name)
    finally:
        connection.close()

    connection = sqlite3.connect(f"file:{instance_db}?mode=ro", uri=True)
    try:
        rows = connection.execute(
            "SELECT entity_base,entity_id,matrix_col_major,name,x,z,source_path "
            "FROM instances WHERE x>=? AND x<? AND z>=? AND z<? "
            "ORDER BY entity_base,entity_id",
            bounds,
        ).fetchall()
    finally:
        connection.close()

    bpy.ops.object.select_all(action="SELECT")
    bpy.ops.object.delete(use_global=False)
    instances_collection = bpy.data.collections.new("Whitebox_Instances")
    templates_collection = bpy.data.collections.new("Whitebox_Templates")
    unresolved_collection = bpy.data.collections.new("Whitebox_Unresolved")
    bpy.context.scene.collection.children.link(instances_collection)
    bpy.context.scene.collection.children.link(templates_collection)
    bpy.context.scene.collection.children.link(unresolved_collection)
    templates_collection.hide_render = True
    templates_collection.hide_viewport = True

    neutral = make_neutral_material()
    mesh_cache: dict[str, bpy.types.Mesh] = {}
    templates: dict[str, bpy.types.Object] = {}
    unresolved_positions: list[tuple[float, float, float]] = []
    placed = 0
    base_counts: Counter[str] = Counter()
    resolution_counts: Counter[str] = Counter()
    plan_counts: Counter[str] = Counter()

    for base, entity_id, matrix_blob, name, _x, _z, _source_path in rows:
        base = str(base)
        method, root_cab, model_name = resolution_by_base.get(base, ("unresolved", None, None))
        resolution_counts[method] += 1
        plan = export_for_resolution(method, root_cab, model_name)
        plan_counts[method if plan is not None else f"{method}_no_plan"] += 1
        if plan is None:
            unresolved_positions.append(tuple(unity_matrix(matrix_blob).translation))
            continue
        file_name = str(plan["file"]).replace("\\", "/")
        mesh_path = probe_root / file_name
        if not mesh_path.is_file():
            unresolved_positions.append(tuple(unity_matrix(matrix_blob).translation))
            continue
        cache_key = str(mesh_path).casefold()
        mesh = mesh_cache.get(cache_key)
        if mesh is None:
            mesh = import_obj(mesh_path)
            if mesh is None:
                unresolved_positions.append(tuple(unity_matrix(matrix_blob).translation))
                continue
            mesh.name = "WhiteboxMesh_" + safe_name(base)
            mesh.materials.clear()
            mesh.materials.append(neutral)
            mesh_cache[cache_key] = mesh
            template = bpy.data.objects.new("Template_" + safe_name(base), mesh)
            templates_collection.objects.link(template)
            template.hide_set(True)
            templates[base] = template
        elif base not in templates:
            template = bpy.data.objects.new("Template_" + safe_name(base), mesh)
            templates_collection.objects.link(template)
            template.hide_set(True)
            templates[base] = template
        obj = bpy.data.objects.new(f"WB_{safe_name(base)}_{int(entity_id)}", mesh)
        instances_collection.objects.link(obj)
        obj.matrix_world = unity_matrix(matrix_blob)
        obj["entity_base"] = base
        obj["entity_id"] = int(entity_id)
        obj["resolution_method"] = method
        obj["source_mesh"] = str(plan["asset"].get("Name") or "")
        obj["source_cab"] = str(root_cab or "")
        base_counts[base] += 1
        placed += 1

    unresolved_mesh = bpy.data.meshes.new("unresolved_positions")
    unresolved_mesh.from_pydata(unresolved_positions, [], [])
    unresolved_obj = bpy.data.objects.new("Unresolved_Entity_Positions", unresolved_mesh)
    unresolved_collection.objects.link(unresolved_obj)
    unresolved_obj.display_type = "WIRE"
    unresolved_obj["count"] = len(unresolved_positions)

    ground = bpy.data.meshes.new("Whitebox_Ground_Mesh")
    cx = (args.xmin + args.xmax) / 2.0
    cy = -(args.zmin + args.zmax) / 2.0
    sx = args.xmax - args.xmin
    sy = args.zmax - args.zmin
    ground.from_pydata(
        [(args.xmin, -args.zmin, args.ground_z), (args.xmax, -args.zmin, args.ground_z),
         (args.xmax, -args.zmax, args.ground_z), (args.xmin, -args.zmax, args.ground_z)],
        [], [(0, 1, 2, 3)],
    )
    ground_obj = bpy.data.objects.new("Whitebox_Ground_Reference", ground)
    instances_collection.objects.link(ground_obj)
    ground_mat = bpy.data.materials.new("Whitebox_Ground_Reference_Material")
    ground_mat.diffuse_color = (0.24, 0.28, 0.32, 1.0)
    ground_mat.use_nodes = True
    ground_principled = next(node for node in ground_mat.node_tree.nodes if node.type == "BSDF_PRINCIPLED")
    ground_principled.inputs["Base Color"].default_value = (0.24, 0.28, 0.32, 1.0)
    ground_principled.inputs["Roughness"].default_value = 1.0
    ground.materials.append(ground_mat)

    scene = bpy.context.scene
    scene.render.engine = "BLENDER_EEVEE_NEXT"
    scene.render.resolution_x = 1000
    scene.render.resolution_y = 760
    scene.render.resolution_percentage = 100
    scene.render.image_settings.file_format = "PNG"
    scene.render.film_transparent = False
    scene.world.color = (0.025, 0.035, 0.045)
    visible_objects = [obj for obj in bpy.data.objects if obj.name.startswith("WB_")]
    world_points = [
        obj.matrix_world @ Vector(corner)
        for obj in visible_objects
        for corner in obj.bound_box
    ]
    if world_points:
        min_x = min(point.x for point in world_points)
        max_x = max(point.x for point in world_points)
        min_y = min(point.y for point in world_points)
        max_y = max(point.y for point in world_points)
        min_z = min(point.z for point in world_points)
        max_z = max(point.z for point in world_points)
        view_center = Vector(
            ((min_x + max_x) / 2.0, (min_y + max_y) / 2.0, (min_z + max_z) / 2.0)
        )
        view_span = max(max_x - min_x, max_y - min_y, (max_z - min_z) * 0.8, 128.0)
    else:
        view_center = Vector((cx, cy, args.ground_z))
        view_span = max(sx, sy, 128.0)
    camera_data = bpy.data.cameras.new("Whitebox_Camera")
    camera_data.type = "ORTHO"
    camera_data.ortho_scale = view_span * 1.45
    camera_data.clip_end = max(10000.0, view_span * 4.0)
    camera = bpy.data.objects.new("Whitebox_Camera", camera_data)
    scene.collection.objects.link(camera)
    camera.location = (
        view_center.x + view_span * 0.62,
        view_center.y - view_span * 0.72,
        view_center.z + view_span * 1.15,
    )
    look_at(camera, view_center)
    camera_data.lens = 52
    scene.camera = camera
    light_data = bpy.data.lights.new("Whitebox_Key", "AREA")
    light_data.energy = 1800
    light_data.shape = "DISK"
    light_data.size = view_span * 0.9
    light = bpy.data.objects.new("Whitebox_Key", light_data)
    scene.collection.objects.link(light)
    light.location = (
        view_center.x + view_span * 0.15,
        view_center.y - view_span * 0.2,
        view_center.z + view_span * 1.5,
    )
    look_at(light, view_center)
    sun_data = bpy.data.lights.new("Whitebox_Fill", "SUN")
    sun_data.energy = 2.2
    sun = bpy.data.objects.new("Whitebox_Fill", sun_data)
    scene.collection.objects.link(sun)
    sun.rotation_euler = (0.35, -0.25, -0.6)

    scene["whitebox_format"] = "EndfieldAssetGraphWhitebox/1"
    scene["whitebox_label"] = args.label
    scene["map_id"] = args.map_id
    scene["bounds_unity_xz"] = json.dumps(bounds)
    scene["axis_contract"] = AXIS_CONTRACT
    scene["source_instances"] = str(instance_db)
    scene["source_assets_db"] = str(assets_db)
    scene["source_scene_probe"] = str(manifest_path)
    scene["placed_instance_count"] = placed
    scene["unresolved_instance_count"] = len(unresolved_positions)
    scene["template_count"] = len(templates)
    scene["excluded_hlod_by_contract"] = True
    scene.render.filepath = str(output.with_suffix(".png"))
    output.parent.mkdir(parents=True, exist_ok=True)
    bpy.ops.wm.save_as_mainfile(filepath=str(output), check_existing=False)
    bpy.ops.render.render(write_still=True)
    bpy.ops.wm.save_as_mainfile(filepath=str(output), check_existing=False)
    return {
        "format": "EndfieldAssetGraphWhitebox/1",
        "label": args.label,
        "mapId": args.map_id,
        "bounds": dict(zip(("xmin", "xmax", "zmin", "zmax"), bounds)),
        "placedInstances": placed,
        "unresolvedInstances": len(unresolved_positions),
        "templateCount": len(templates),
        "meshCacheCount": len(mesh_cache),
        "resolutionCounts": dict(resolution_counts),
        "outputBlend": str(output),
        "outputPng": str(output.with_suffix(".png")),
        "axisContract": AXIS_CONTRACT,
        "excludedHlod": True,
        "diagnostics": {
            "meshExportCount": mesh_export_count,
            "modelLookupCount": len(mesh_by_source_name),
            "planCounts": dict(plan_counts),
        },
    }


if __name__ == "__main__":
    result = build_scene(arguments())
    print(json.dumps(result, ensure_ascii=False, indent=2, sort_keys=True))
