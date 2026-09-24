"""Compose portable scenes with the existing exact geometry and material adapters."""
from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
import struct
import sys

sys.path.insert(0, str(Path(__file__).resolve().parent))

import bpy

from blender_build_map02_chunk_20260727 import import_obj, join_submeshes, unity_matrix, build_terrain
from blender_scene_materials import create_material
from blender_scene_terrain import adapt_scene_terrain, adapt_scene_terrain_mesh_normals
from blender_terrain_atlas import crop_terrain_atlases
from endfield_scene_material_contract import load_catalog, reference_key
from endfield_scene_export_plan import load_scene_export_plan, PlanInstance


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
    """Record geometry, UVs, transforms and ordered slots before saving."""
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
            digest.update(str(tuple(material.name if material else None for material in obj.data.materials)).encode())
        signatures[obj.name] = digest.hexdigest()
    return signatures


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
    material_cache: dict[tuple[str, int], bpy.types.Material] = {}
    mesh_cache: dict[str, bpy.types.Mesh] = {}
    for row in plan["instances"]:
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
    terrain_collection = bpy.data.collections.new("Endfield_Terrain")
    bpy.context.scene.collection.children.link(terrain_collection)
    bounds = tuple(plan["bounds"][key] for key in ("xmin", "xmax", "zmin", "zmax"))
    for surface in plan["terrain_surfaces"]:
        root = Path(surface)
        metadata = json.loads((root / "terrain_surface.json").read_text(encoding="utf-8-sig"))
        build_terrain(root / metadata["height_database"], terrain_collection, bounds, root, False)
    terrain_materials = adapt_scene_terrain(bpy.context.scene)
    crops = crop_terrain_atlases(bpy.context.scene)
    terrain_normals = adapt_scene_terrain_mesh_normals(bpy.context.scene)
    bpy.context.view_layer.update()
    decals = project_decals(Path(plan["decal_manifest"]), Path(plan["decal_materials"]))
    for image in bpy.data.images:
        if image.source == "FILE" and image.users and not image.packed_file:
            image.pack()
    scene = bpy.context.scene
    scene.render.engine = "BLENDER_EEVEE_NEXT"
    scene["endfield_export_contract"] = "EndfieldPortableScenePlan/1"
    scene["endfield_map"] = plan["map_id"]
    scene["endfield_build_plan"] = args.plan.name
    scene["endfield_unresolved_resources"] = len(plan["pending"])
    scene["endfield_source_axis"] = "Blender (X,Y,Z) = Unity (X,-Z,Y)"
    if not plan["instances"] and not any(obj.type == "MESH" and obj.data.polygons for obj in scene.objects):
        raise ValueError("The selected categories have no reconstructable geometry; inspect scene_plan.json pending resources")
    expected = scene_signature()
    scene.render.image_settings.file_format = "PNG"
    bpy.ops.wm.save_as_mainfile(filepath=str(args.output))
    bpy.ops.wm.open_mainfile(filepath=str(args.output))
    if scene_signature() != expected:
        raise ValueError("Saved scene geometry, transforms, UVs or material slots changed on reopen")
    missing = [image.name for image in bpy.data.images if image.source == "FILE" and image.users and not image.packed_file]
    if missing:
        raise ValueError(f"Saved scene contains unpacked textures: {missing}")
    pending = len(plan["pending"]) + sum(row["status"] != "restored" for row in decals)
    args.audit.write_text(json.dumps({"format": "EndfieldPortableSceneAudit/1", "reopen_verified": True,
        "instances": len(plan["instances"]), "pending": pending, "objects": len(expected),
        "terrain_materials": terrain_materials, "terrain_crops": crops, "terrain_normals": terrain_normals,
        "decals": decals, "limitations": ["Particle simulation, volume lighting and fog are not reconstructed",
        "Terrain micro-detail requires source surface metadata; base atlas and world normals are restored"]}, indent=2), encoding="utf-8")


if __name__ == "__main__":
    main()
