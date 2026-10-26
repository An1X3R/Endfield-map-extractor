"""Place source-qualified MeshRenderer FX nodes without particle simulation."""

from __future__ import annotations

import json
import math
import struct
from pathlib import Path
from typing import Callable, Mapping, Sequence

import bpy

from blender_build_map02_chunk_20260727 import import_obj, unity_matrix
from blender_scene_effect_materials import make_missing_mesh_uv1_reference
from blender_scene_effect_vertex_offset import apply_material_vertex_offset, UnsupportedVertexOffset
from endfield_scene_material_contract import mesh_material_requires_uv1, reference_key, resolve_export_path


MeshMaterialFactory = Callable[[Mapping[str, object], Mapping[str, object], int], bpy.types.Material]


class UnsupportedMeshVertexMapping(NotImplementedError):
    """An OBJ export cannot be paired with its source vertex streams."""


def _record(value: object, context: str) -> Mapping[str, object]:
    if not isinstance(value, dict):
        raise ValueError(f"{context} must be an object")
    return value


def _asset(value: object, kind: str, context: str) -> tuple[str, int]:
    row = _record(value, context)
    source, path_id, actual = row.get("Source"), row.get("PathId"), row.get("Type")
    if not isinstance(source, str) or not source or type(path_id) is not int or actual != kind:
        raise ValueError(f"{context} must be source-qualified {kind}: value={row}")
    return source.casefold(), path_id


def _activity(nodes: Mapping[tuple[str, int], Mapping[str, object]], root: tuple[str, int],
              owner: tuple[str, int]) -> bool | None:
    transforms = {(key[0], node["TransformPathId"]): key for key, node in nodes.items()
                  if type(node.get("TransformPathId")) is int}
    current = owner
    seen: set[tuple[str, int]] = set()
    chain: list[tuple[str, int]] = []
    while current in nodes:
        if current in seen:
            raise ValueError(f"MeshRenderer FX transform cycle: owner={owner}, repeated={current}")
        seen.add(current)
        chain.append(current)
        if current == root:
            for key in chain:
                active = nodes[key].get("GameObjectActive")
                if type(active) is not bool:
                    raise ValueError(f"MeshRenderer FX GameObjectActive missing: node={key}")
                if not active:
                    return False
            return True
        parent = _asset(nodes[current].get("Parent"), "Transform", "MeshRenderer FX parent")
        current = transforms.get(parent, ("", 0))
    return None


def _source_vertices(path: Path) -> tuple[tuple[float, float, float], ...]:
    vertices: list[tuple[float, float, float]] = []
    with path.open("r", encoding="utf-8-sig") as stream:
        for line in stream:
            if line.startswith("v "):
                fields = line.split()
                if len(fields) != 4:
                    raise ValueError(f"Mesh OBJ vertex record invalid: path={path}, line={line.strip()}")
                x, y, z = (float(value) for value in fields[1:])
                if not all(math.isfinite(value) for value in (x, y, z)):
                    raise ValueError(f"Mesh OBJ vertex non-finite: path={path}, line={line.strip()}")
                vertices.append((-x, -z, y))
    return tuple(vertices)


def _vertex_colors(mesh: bpy.types.Mesh, export: Mapping[str, object], manifest_path: Path,
                   obj_path: Path) -> bool:
    vertex_count, components = export.get("VertexCount"), export.get("VertexColorComponents")
    if type(vertex_count) is not int or vertex_count <= 0 or type(components) is not int:
        raise ValueError(f"Mesh vertex metadata missing: export={export}")
    source = _source_vertices(obj_path)
    if len(source) != vertex_count or len(mesh.vertices) != vertex_count:
        return False
    if any(max(abs(mesh.vertices[index].co[axis] - source[index][axis]) for axis in range(3)) > 1e-4
           for index in range(vertex_count)):
        return False
    if components == 0:
        return True
    if components != 4:
        raise NotImplementedError(f"Mesh vertex color components unsupported: components={components}, mesh={obj_path}")
    color_file = export.get("VertexColorsFile")
    if not isinstance(color_file, str) or not color_file:
        raise FileNotFoundError(f"Mesh vertex color file missing: mesh={obj_path}")
    path = resolve_export_path(manifest_path.parent / color_file.replace("\\", "/"))
    data = path.read_bytes()
    if len(data) != vertex_count * 16:
        raise ValueError(f"Mesh RGBA stream length mismatch: path={path}, expected={vertex_count * 16}, actual={len(data)}")
    colors = struct.unpack(f"<{vertex_count * 4}f", data)
    if not all(math.isfinite(value) for value in colors):
        raise ValueError(f"Mesh RGBA stream is non-finite: path={path}")
    attribute = mesh.color_attributes.new(name="EF_FX_COLOR", type="FLOAT_COLOR", domain="POINT")
    attribute.data.foreach_set("color", colors)
    return True


def _vertex_uv1(mesh: bpy.types.Mesh, export: Mapping[str, object], manifest_path: Path) -> None:
    components = export.get("Uv1Components")
    count = export.get("VertexCount")
    if type(components) is not int or type(count) is not int:
        raise ValueError(f"Mesh UV1 metadata missing: export={export}")
    if components == 0:
        return
    if components != 2:
        return
    uv_file = export.get("Uv1File")
    if not isinstance(uv_file, str) or not uv_file:
        raise FileNotFoundError(f"Mesh source UV1 file missing: export={export}")
    path = resolve_export_path(manifest_path.parent / uv_file.replace("\\", "/"))
    data = path.read_bytes()
    if len(data) != count * 8:
        raise ValueError(f"Mesh UV1 stream length mismatch: path={path}, expected={count * 8}, actual={len(data)}")
    values = struct.unpack(f"<{count * 2}f", data)
    if not all(math.isfinite(value) for value in values):
        raise ValueError(f"Mesh UV1 stream is non-finite: path={path}")
    layer = mesh.uv_layers.new(name="EF_FX_UV1")
    layer.data.foreach_set("uv", [values[loop.vertex_index * 2 + axis]
                                  for loop in mesh.loops for axis in range(2)])


def _source_mesh_templates(export: Mapping[str, object], manifest_path: Path) -> tuple[bpy.types.Mesh, ...]:
    obj_path = resolve_export_path(manifest_path.parent / str(export["File"]).replace("\\", "/"))
    imported = import_obj(obj_path)
    try:
        if not imported:
            return ()
        if len(imported) != 1 or not _vertex_colors(imported[0].data, export, manifest_path, obj_path):
            raise UnsupportedMeshVertexMapping(f"FX source OBJ vertex mapping is unsupported: mesh={obj_path}")
        _vertex_uv1(imported[0].data, export, manifest_path)
        return tuple(obj.data for obj in imported)
    finally:
        for obj in imported:
            bpy.data.objects.remove(obj, do_unlink=True)


def build_scene_effect_meshes(effect_instances: Sequence[Mapping[str, object]],
                              collection: bpy.types.Collection,
                              material_factory: MeshMaterialFactory,
                              max_effect_instances: int,
                              snapshot_age_seconds: float,
                              snapshot_camera: bpy.types.Object) -> list[dict[str, object]]:
    """Build MeshRenderer FX and return one evidence-qualified audit row per node."""
    from blender_scene_effects import _anchor_matrix, _blender_matrix, _owner_matrix_root_relative

    if max_effect_instances <= 0:
        raise ValueError(f"MeshRenderer FX instance limit must be positive: limit={max_effect_instances}")
    manifests: dict[Path, Mapping[str, object]] = {}
    templates: dict[tuple[Path, tuple[str, int]], tuple[bpy.types.Mesh, ...]] = {}
    audit: list[dict[str, object]] = []
    for index, instance in enumerate(effect_instances):
        kind = instance.get("component_kind", "particle")
        if kind not in ("mesh_renderer", "mixed"):
            continue
        entity_id = instance.get("entity_id")
        if type(entity_id) is not int:
            raise ValueError(f"MeshRenderer FX entity_id must be integer: index={index}, value={entity_id}")
        if index >= max_effect_instances:
            audit.append({"entity_id": entity_id, "status": "truncated_effect_limit", "limit": max_effect_instances})
            continue
        if instance.get("matrix_space") != "dynamic_effect_anchor":
            raise ValueError(f"MeshRenderer FX matrix space unsupported: entity_id={entity_id}")
        path_value = instance.get("scene_manifest")
        if not isinstance(path_value, str) or not Path(path_value).is_absolute():
            raise ValueError(f"MeshRenderer FX scene_manifest must be absolute: entity_id={entity_id}, value={path_value}")
        manifest_path = resolve_export_path(Path(path_value))
        if not manifest_path.is_file():
            audit.append({"entity_id": entity_id, "status": "skipped_missing_resource",
                          "resource": "mesh_effect_manifest", "path": str(manifest_path),
                          "reason": "Effect SceneProbe manifest was not extracted"})
            continue
        if manifest_path not in manifests:
            manifests[manifest_path] = _record(json.loads(manifest_path.read_text(encoding="utf-8-sig")), "FX manifest")
        manifest = manifests[manifest_path]
        root = _asset(instance.get("prefab_root"), "GameObject", "FX prefab root")
        node_values = manifest.get("Nodes")
        if not isinstance(node_values, list):
            raise ValueError(f"MeshRenderer FX Nodes missing: manifest={manifest_path}")
        nodes = {reference_key(node): _record(node, "FX node") for node in node_values}
        matrix_nodes = {
            key: dict(node) | {"Parent": dict(parent) | {"Source": _asset(parent, "Transform", "FX parent")[0]}}
            for key, node in nodes.items()
            for parent in (_record(node.get("Parent"), "FX node Parent"),)
        }
        exports_value = manifest.get("MeshExports")
        if not isinstance(exports_value, list):
            raise ValueError(f"MeshRenderer FX MeshExports missing: manifest={manifest_path}")
        exports = {_asset(_record(value, "MeshExport").get("Asset"), "Mesh", "MeshExport.Asset"):
                   _record(value, "MeshExport") for value in exports_value}
        materials_value = manifest.get("Materials")
        if not isinstance(materials_value, list):
            raise ValueError(f"MeshRenderer FX Materials missing: manifest={manifest_path}")
        materials = {reference_key(value): _record(value, "FX material") for value in materials_value}
        anchor = _anchor_matrix(instance.get("matrix"))
        for owner, node in nodes.items():
            if node.get("RendererType") != "MeshRenderer":
                continue
            active = _activity(nodes, root, owner)
            if active is None:
                continue
            row: dict[str, object] = {"entity_id": entity_id, "owner_source": owner[0],
                                      "owner_path_id": owner[1]}
            if not active:
                audit.append(row | {"status": "game_object_inactive"})
                continue
            if type(node.get("RendererEnabled")) is not bool:
                raise ValueError(f"MeshRenderer FX enabled flag missing: owner={owner}")
            if not node["RendererEnabled"]:
                audit.append(row | {"status": "renderer_disabled"})
                continue
            renderer_key = _asset(node.get("RendererAsset"), "MeshRenderer", "FX RendererAsset")
            mesh_key = _asset(node.get("Mesh"), "Mesh", "FX node Mesh")
            export = exports.get(mesh_key)
            if export is None or not isinstance(export.get("File"), str):
                audit.append(row | {"status": "mesh_export_missing", "mesh": list(mesh_key)})
                continue
            refs_value = node.get("Materials")
            if not isinstance(refs_value, list) or not refs_value:
                audit.append(row | {"status": "material_slot_missing"})
                continue
            refs = tuple(_record(value, "FX material reference") for value in refs_value)
            cache_key = (manifest_path, mesh_key)
            if cache_key not in templates:
                try:
                    templates[cache_key] = _source_mesh_templates(export, manifest_path)
                except FileNotFoundError as error:
                    audit.append(row | {"status": "skipped_missing_resource", "resource": "mesh_effect_geometry",
                                        "mesh": list(mesh_key), "reason": str(error)})
                    continue
                except UnsupportedMeshVertexMapping:
                    audit.append(row | {"status": "unsupported_mesh_vertex_mapping", "mesh": list(mesh_key)})
                    continue
                if not templates[cache_key]:
                    del templates[cache_key]
                    audit.append(row | {"status": "mesh_import_empty", "mesh": list(mesh_key)})
                    continue
            meshes = templates[cache_key]
            if len(refs) < len(meshes) and len(refs) != 1:
                audit.append(row | {"status": "unsupported_material_slot_count", "materials": len(refs), "submeshes": len(meshes)})
                continue
            # Unity repeats the last submesh for surplus renderer material slots.
            draws = (tuple((index, min(index, len(meshes) - 1)) for index in range(len(refs)))
                     if len(refs) >= len(meshes) else tuple((0, index) for index in range(len(meshes))))
            for material_slot, submesh in draws:
                row = row | {"material_slot": material_slot, "submesh": submesh}
                template = meshes[submesh]
                ref = refs[material_slot]
                material_key = _asset(ref, "Material", "FX mesh material")
                source_material = materials.get(material_key)
                if source_material is None:
                    audit.append(row | {"status": "material_record_missing", "material": list(material_key)})
                    continue
                missing_uv1 = mesh_material_requires_uv1(source_material) and template.uv_layers.get("EF_FX_UV1") is None
                zero_uv1_reference = (missing_uv1 and export["Uv1Components"] == 0
                                      and _record(source_material["Shader"], "FX shader").get("Name") == "HGRP/Effect/VFXBaseV2"
                                      and _record(source_material["Floats"], "FX floats").get("_InParticle") == 0)
                if missing_uv1 and not zero_uv1_reference:
                    audit.append(row | {"status": "unsupported_mesh_uv1", "material": list(material_key),
                                        "reason": "Source material reads UV1 but the mesh has no decoded two-component UV1 stream"})
                    continue
                try:
                    material = material_factory(ref, node, int(export["VertexColorComponents"]))
                    if zero_uv1_reference:
                        material = make_missing_mesh_uv1_reference(material, int(export["Uv1Components"]))
                except FileNotFoundError as error:
                    audit.append(row | {"status": "skipped_missing_resource", "resource": "mesh_effect_material",
                                        "material": list(material_key), "reason": str(error)})
                    continue
                except NotImplementedError as error:
                    audit.append(row | {"status": "unsupported_material", "material": list(material_key), "reason": str(error)})
                    continue
                mesh = template.copy()
                mesh.materials.clear()
                world = _blender_matrix(anchor @ _owner_matrix_root_relative(matrix_nodes, root, owner))
                try:
                    displaced = apply_material_vertex_offset(
                        material, mesh, world, snapshot_camera.matrix_world.translation, snapshot_age_seconds)
                except UnsupportedVertexOffset as error:
                    bpy.data.meshes.remove(mesh)
                    audit.append(row | {"status": "unsupported_vertex_offset", "reason": str(error)})
                    continue
                if displaced is not mesh:
                    bpy.data.meshes.remove(mesh)
                    mesh = displaced
                mesh.materials.append(material)
                suffix = f"{submesh}_{material_slot}" if len(refs) > len(meshes) else str(submesh)
                obj = bpy.data.objects.new(f"EF_FX_MESH_{entity_id}_{owner[1]}_{suffix}", mesh)
                collection.objects.link(obj)
                obj.matrix_world = world
                if "endfield_fx_vertex_offset_scope" in mesh:
                    obj["endfield_fx_vertex_offset_scope"] = mesh["endfield_fx_vertex_offset_scope"]
                obj["endfield_fx_entity_id"] = str(entity_id)
                obj["endfield_fx_renderer_source"] = renderer_key[0]
                obj["endfield_fx_renderer_path_id"] = str(renderer_key[1])
                obj["endfield_fx_mesh_source"] = mesh_key[0]
                obj["endfield_fx_mesh_path_id"] = str(mesh_key[1])
                obj["endfield_fx_material_slot"] = material_slot
                if len(refs) > len(meshes):
                    obj["endfield_fx_draw_order_scope"] = "Source material layers preserved; Blender does not reproduce Unity pass ordering"
                audit.append(row | {"status": "static_preview_partial", "object": obj.name,
                                    "material_status": str(material["endfield_fx_status"]),
                                    "source_vertex_color_components": int(export["VertexColorComponents"]),
                                    "source_uv1_components": int(export["Uv1Components"]),
                                    "uv1_status": ("absent_source_constant_zero_reference" if zero_uv1_reference
                                                   else "source_stream" if template.uv_layers.get("EF_FX_UV1") is not None
                                                   else "not_required"),
                                    "material_limitations": json.loads(material["endfield_fx_limitations"]),
                                    "material": material.name, "submesh": submesh})
    return audit


def build_direct_effect_meshes(rows: Sequence[Mapping[str, object]], collection: bpy.types.Collection,
                               material_factory: MeshMaterialFactory, snapshot_age_seconds: float,
                               snapshot_camera: bpy.types.Object) -> list[dict[str, object]]:
    """Place direct source draws with their actual world matrices and stream evidence."""
    import hashlib

    from endfield_scene_direct_effects_plan import effect_mesh_streams_ready

    manifests: dict[Path, Mapping[str, object]] = {}
    templates: dict[tuple[Path, tuple[str, int]], tuple[bpy.types.Mesh, ...]] = {}
    seen: set[str] = set()
    audit: list[dict[str, object]] = []
    for row in rows:
        identity = row.get("placement_key")
        if not isinstance(identity, str) or not identity or identity in seen:
            raise ValueError(f"Direct FX source placement identity invalid or repeated: identity={identity}")
        seen.add(identity)
        if row.get("matrix_space") != "dynamic_renderer_world":
            raise ValueError(f"Direct FX matrix space unsupported: identity={identity}")
        manifest_path = resolve_export_path(Path(str(row["scene_manifest"])))
        if not manifest_path.is_file():
            audit.append({"placement_key": identity, "status": "skipped_missing_resource",
                          "resource": "direct_effect_manifest", "path": str(manifest_path),
                          "reason": "Effect SceneProbe manifest was not extracted"})
            continue
        if manifest_path not in manifests:
            manifests[manifest_path] = _record(json.loads(manifest_path.read_text("utf-8-sig")), "Direct FX manifest")
        mesh_key = _asset(row["mesh_asset"], "Mesh", "Direct FX mesh asset")
        manifest = manifests[manifest_path]
        exports = manifest.get("MeshExports")
        if not isinstance(exports, list):
            raise ValueError(f"Direct FX source mesh exports missing: manifest={manifest_path}")
        matches = [_record(value, "Direct FX mesh export") for value in exports
                   if _asset(_record(value, "Direct FX mesh export")["Asset"], "Mesh", "Direct FX export asset") == mesh_key]
        if len(matches) != 1 or not effect_mesh_streams_ready(matches[0], manifest_path):
            raise ValueError(f"Direct FX source mesh stream contract changed: identity={identity}, matches={len(matches)}")
        export = matches[0]
        evidence: dict[str, object] = {"placement_key": identity, "source_key": row["source_key"],
            "source_virtual_path": row["source_virtual_path"], "source_pair_index": row["source_pair_index"],
            "chunk_id": row["chunk_id"], "init_scene_index": row["init_scene_index"],
            "group_index": row["group_index"], "record_index": row["record_index"],
            "activation_status": row["activation_status"]}
        pairs = row.get("renderer_pairs")
        if not isinstance(pairs, list) or type(row.get("source_pair_index")) is not int:
            raise ValueError(f"Direct FX source draw pairs missing: identity={identity}")
        pair = _record(pairs[row["source_pair_index"]], "Direct FX selected pair")
        if type(pair.get("submesh")) is not int or type(pair.get("flags")) is not int or pair["flags"] & 1024:
            raise ValueError(f"Direct FX selected source draw is invalid or shadow-only: identity={identity}")
        cache_key = manifest_path, mesh_key
        try:
            if cache_key not in templates:
                templates[cache_key] = _source_mesh_templates(export, manifest_path)
            source_meshes = templates[cache_key]
            submesh = pair["submesh"]
            if not 0 <= submesh < len(source_meshes):
                raise UnsupportedMeshVertexMapping(f"Direct FX source submesh exceeds imported OBJ: identity={identity}, submesh={submesh}, count={len(source_meshes)}")
            material = material_factory(_record(row["material_asset"], "Direct FX material asset"), row,
                                        int(export["VertexColorComponents"]))
            material_record = _record(json.loads(material["endfield_fx_source_material_json"]), "Direct FX material evidence")
            if mesh_material_requires_uv1(material_record) and source_meshes[submesh].uv_layers.get("EF_FX_UV1") is None:
                material = make_missing_mesh_uv1_reference(material, int(export["Uv1Components"]))
        except FileNotFoundError as error:
            audit.append(evidence | {"status": "skipped_missing_resource", "resource": "direct_effect_asset",
                                    "mesh": list(mesh_key), "reason": str(error)})
            continue
        except NotImplementedError as error:
            audit.append(evidence | {"status": "unsupported_direct_effect", "reason": str(error)})
            continue
        values = row["matrix"]
        if not isinstance(values, list) or len(values) != 16 or any(type(value) not in (int, float) or not math.isfinite(value) for value in values):
            raise ValueError(f"Direct FX source world matrix invalid: identity={identity}")
        world = unity_matrix(struct.pack("<16f", *values))
        mesh = source_meshes[submesh].copy()
        try:
            displaced = apply_material_vertex_offset(material, mesh, world,
                snapshot_camera.matrix_world.translation, snapshot_age_seconds)
        except UnsupportedVertexOffset as error:
            bpy.data.meshes.remove(mesh)
            audit.append(evidence | {"status": "unsupported_vertex_offset", "reason": str(error)})
            continue
        if displaced is not mesh:
            bpy.data.meshes.remove(mesh)
            mesh = displaced
        mesh.materials.clear()
        mesh.materials.append(material)
        name = "EF_FX_MESH_DIRECT_" + hashlib.sha256(identity.encode()).hexdigest()[:24]
        obj = bpy.data.objects.new(name, mesh)
        collection.objects.link(obj)
        obj.matrix_world = world
        obj["endfield_fx_direct_source_json"] = json.dumps(dict(row), sort_keys=True, allow_nan=False)
        obj["endfield_fx_source_key"] = str(row["source_key"])
        obj["endfield_fx_placement_key"] = identity
        obj["endfield_fx_activation_status"] = str(row["activation_status"])
        obj["endfield_fx_draw_parameters_status"] = "serialized_initial_not_runtime"
        obj["endfield_fx_runtime_limitations"] = "Dynamic activation, source LOD switching and runtime property overrides are not captured"
        audit.append(evidence | {"status": "static_preview_partial", "object": name,
            "material": material.name, "material_status": str(material["endfield_fx_status"]),
            "source_vertex_color_components": int(export["VertexColorComponents"]),
            "source_uv1_components": int(export["Uv1Components"]),
            "runtime_limitations": obj["endfield_fx_runtime_limitations"],
            "material_limitations": json.loads(material["endfield_fx_limitations"])})
    return audit
