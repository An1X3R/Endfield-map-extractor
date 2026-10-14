"""Research preview of source spline models, without native emitter simulation."""
from __future__ import annotations

import bisect
import hashlib
import json
import math
from pathlib import Path
from typing import Mapping, NamedTuple, cast

import bpy
from mathutils import Matrix, Vector

from blender_build_map02_chunk_20260727 import import_obj
from blender_scene_effect_materials import make_mesh_effect_material
from blender_scene_effect_meshes import _vertex_colors, _vertex_uv1
from blender_scene_effects import _anchor_matrix, _blender_matrix, _owner_matrix_root_relative
from blender_scene_materials import apply_emission_mask, create_material
from endfield_scene_material_contract import load_catalog, reference_key, require_object, resolve_export_path


class WorldPath(NamedTuple):
    points: tuple[Vector, ...]
    distances: tuple[float, ...]


def _array(value: object, context: str) -> list[object]:
    if not isinstance(value, list):
        raise ValueError(f"Spline preview array missing: field={context}")
    return cast(list[object], value)


def _number(value: object, context: str) -> float:
    if type(value) not in (int, float) or not math.isfinite(cast(float, value)):
        raise ValueError(f"Spline preview finite number missing: field={context}, value={value}")
    return float(cast(float, value))


def _text(value: object, context: str) -> str:
    if not isinstance(value, str) or not value:
        raise ValueError(f"Spline preview text missing: field={context}")
    return value


def _vector(value: object, context: str) -> Vector:
    values = _array(value, context)
    if len(values) != 3:
        raise ValueError(f"Spline preview Vector3 invalid: field={context}, value={value}")
    return Vector(tuple(_number(item, context) for item in values))


def sample_world_path(segments: list[object], active_segment_count: int,
                      world: Matrix, samples_per_segment: int) -> WorldPath:
    """Sample active baked segments only; terminal cache entries are not curves."""
    if not 0 < active_segment_count <= len(segments) or samples_per_segment < 2:
        raise ValueError("Spline preview active segment/sample count invalid")
    points: list[Vector] = []
    distances: list[float] = []
    for segment_index, value in enumerate(segments[:active_segment_count]):
        row = require_object(value, "spline segment")
        controls = tuple(_vector(item, "control_points_unity")
                         for item in _array(row.get("control_points_unity"), "control_points_unity"))
        if len(controls) != 4:
            raise ValueError("Spline cubic segment must contain four source controls")
        for index in range(samples_per_segment + 1):
            if segment_index and index == 0:
                continue
            t = index / samples_per_segment
            local = ((1 - t) ** 3 * controls[0] + 3 * (1 - t) ** 2 * t * controls[1]
                     + 3 * (1 - t) * t * t * controls[2] + t ** 3 * controls[3])
            point = world @ local
            distances.append((distances[-1] + (point - points[-1]).length) if points else 0.0)
            points.append(point)
    if distances[-1] <= 0:
        raise ValueError("Spline preview active world path has zero length")
    return WorldPath(tuple(points), tuple(distances))


def world_path_position(path: WorldPath, distance: float) -> Vector:
    """Interpolate world arc length without wrapping an open endpoint."""
    if distance <= 0:
        return path.points[0].copy()
    if distance >= path.distances[-1]:
        return path.points[-1].copy()
    upper = bisect.bisect_right(path.distances, distance)
    lower = upper - 1
    span = path.distances[upper] - path.distances[lower]
    return path.points[lower].lerp(path.points[upper], (distance - path.distances[lower]) / span)


def _at_position(world: Matrix, point: Vector, relative: Matrix) -> Matrix:
    result = _blender_matrix(world)
    result.translation = Vector((point.x, -point.z, point.y))
    return result @ _blender_matrix(relative)


def _animate(obj: bpy.types.Object, path: WorldPath, world: Matrix, relative: Matrix,
             speed: float, interval: float, fps: float, duration: float) -> None:
    arrival = path.distances[-1] / speed
    times = sorted({0.0, duration, arrival,
                    *(min(index * interval, duration) for index in range(math.ceil(duration / interval) + 1))})
    for time in times:
        obj.matrix_world = _at_position(world, world_path_position(path, time * speed), relative)
        obj.keyframe_insert(data_path="location", frame=1 + time * fps)
    obj.hide_render = obj.hide_viewport = False
    obj.keyframe_insert(data_path="hide_render", frame=1)
    obj.keyframe_insert(data_path="hide_viewport", frame=1)
    obj.hide_render = obj.hide_viewport = True
    obj.keyframe_insert(data_path="hide_render", frame=2 + math.ceil(arrival * fps))
    obj.keyframe_insert(data_path="hide_viewport", frame=2 + math.ceil(arrival * fps))
    if obj.animation_data is None or obj.animation_data.action is None:
        raise ValueError(f"Spline preview object animation missing: object={obj.name}")
    for curve in obj.animation_data.action.fcurves:
        for key in curve.keyframe_points:
            key.interpolation = "LINEAR" if curve.data_path == "location" else "CONSTANT"


def build_spline_preview(input_path: Path, collection: bpy.types.Collection,
                         duration: float, fps: float) -> list[dict[str, object]]:
    """Import representative LOD0 draws and animate a declared world-speed reference."""
    data = require_object(json.loads(input_path.read_text(encoding="utf-8-sig")), "spline preview input")
    if data.get("format") != "EndfieldSplinePreviewInput/1" or duration <= 0 or fps <= 0:
        raise ValueError("Spline preview input format/duration/fps invalid")
    preview = require_object(data.get("preview"), "preview")
    if (preview.get("representative_models_per_source_emitter_per_placement") != 1
            or preview.get("native_emission_rate_used_for_count") is not False
            or preview.get("size_rotation_alpha_curves_applied") is not False
            or preview.get("speed_coordinate_space") != "unity_world_units_per_second"
            or _number(preview.get("preview_phase_fraction"), "preview_phase_fraction") != 0):
        raise ValueError("Spline research preview requires one reference model and no inferred native curves/phase")
    speed = _number(preview.get("editable_preview_speed_units_per_second"), "preview speed")
    interval = _number(preview.get("keyframe_sample_interval_seconds"), "keyframe interval")
    if speed <= 0 or interval <= 0:
        raise ValueError("Spline preview speed and sampling interval must be positive")
    paths = tuple(resolve_export_path(Path(_text(item, "source manifest")))
                  for item in _array(data.get("source_manifests"), "source_manifests"))
    materials, textures = load_catalog(paths)
    nodes: dict[tuple[str, int], Mapping[str, object]] = {}
    exports: dict[tuple[str, int], tuple[Path, Mapping[str, object]]] = {}
    for path in paths:
        manifest = require_object(json.loads(path.read_text(encoding="utf-8-sig")), "source manifest")
        for value in _array(manifest.get("Nodes"), "Nodes"):
            node = require_object(value, "source node")
            parent = require_object(node.get("Parent"), "source node parent")
            nodes[reference_key(node)] = {**node, "Parent": {
                **parent, "Source": _text(parent.get("Source"), "parent source").casefold()}}
        for value in _array(manifest.get("MeshExports"), "MeshExports"):
            export = require_object(value, "mesh export")
            exports[reference_key(export.get("Asset"))] = (path, export)
    audit: list[dict[str, object]] = []
    templates: dict[str, tuple[bpy.types.Mesh, ...]] = {}
    material_cache: dict[tuple[str, int], bpy.types.Material] = {}
    for placement_value in _array(data.get("placements"), "placements"):
        placement = require_object(placement_value, "placement")
        placement_key = _text(placement.get("key"), "placement key")
        anchor = _anchor_matrix(placement.get("matrix_unity_column_major"))
        for emitter_value in _array(data.get("emitters"), "emitters"):
            emitter = require_object(emitter_value, "emitter")
            name = _text(emitter.get("name"), "emitter name")
            metadata = Path(_text(emitter.get("metadata_path"), "emitter metadata"))
            if hashlib.sha256(metadata.read_bytes()).hexdigest() != emitter.get("metadata_sha256"):
                raise ValueError(f"Spline source descriptor hash mismatch: path={metadata}")
            topology = require_object(emitter.get("raw_spline"), "raw spline topology")
            raw_path = Path(_text(topology.get("metadata_path"), "raw spline metadata"))
            if hashlib.sha256(raw_path.read_bytes()).hexdigest() != topology.get("metadata_sha256"):
                raise ValueError(f"Spline source topology hash mismatch: path={raw_path}")
            active = topology.get("active_segment_count")
            knot_count = topology.get("knot_count")
            if (topology.get("closed") is not False or type(active) is not int
                    or type(knot_count) is not int or active != knot_count - 1):
                raise ValueError(f"Spline reference requires verified open topology: emitter={name}")
            world = anchor @ _anchor_matrix(emitter.get("object_to_world_unity_column_major"))
            path = sample_world_path(_array(emitter.get("segments"), "segments"), active, world, 512)
            lod = require_object(emitter.get("lod_group"), "lod group")
            root = reference_key(lod.get("root_game_object"))
            for draw_index, draw_value in enumerate(_array(lod.get("draws"), "LOD0 draws")):
                draw = require_object(draw_value, "LOD0 draw")
                identity = f"{placement_key}:{name}:{draw_index}"
                template_key = json.dumps(draw, sort_keys=True)
                row: dict[str, object] = {"placement_key": placement_key, "emitter": name,
                    "draw_index": draw_index, "mesh": draw.get("mesh"), "materials": draw.get("materials"),
                    "renderer": draw.get("renderer"), "owner_game_object": draw.get("owner_game_object"),
                    "descriptor_metadata": str(metadata), "raw_spline": topology,
                    "world_arc_length": path.distances[-1], "preview_speed_unity_world_units_per_second": speed,
                    "active_segment_count": active, "arrival_seconds": path.distances[-1] / speed}
                if template_key not in templates:
                    manifest_path, export = exports[reference_key(draw.get("mesh"))]
                    obj_path = resolve_export_path(manifest_path.parent / _text(export.get("File"), "mesh file").replace("\\", "/"))
                    imported = import_obj(obj_path)
                    references = _array(draw.get("materials"), "draw materials")
                    if len(imported) != len(references):
                        raise ValueError(f"Spline LOD0 source draw/submesh mismatch: identity={identity}")
                    try:
                        for obj, reference in zip(imported, references, strict=True):
                            if len(imported) != 1 or not _vertex_colors(obj.data, export, manifest_path, obj_path):
                                raise NotImplementedError(f"Spline source vertex mapping unsupported: mesh={obj_path}")
                            _vertex_uv1(obj.data, export, manifest_path)
                            key = reference_key(reference)
                            record = materials[key]
                            if key not in material_cache:
                                if record["Shader"].get("Name") in ("HGRP/Lit", "HGRP/LitEffect"):
                                    material_cache[key] = apply_emission_mask(create_material(record, textures, True), record)
                                else:
                                    material_cache[key] = make_mesh_effect_material(record, textures, 0.0, int(export["VertexColorComponents"]))
                                material_cache[key]["endfield_spline_source_material_json"] = json.dumps(record, sort_keys=True)
                            obj.data.materials.clear()
                            obj.data.materials.append(material_cache[key])
                        templates[template_key] = tuple(obj.data for obj in imported)
                    except NotImplementedError as error:
                        row.update(status="unsupported_source_draw", reason=str(error))
                        audit.append(row)
                        continue
                    finally:
                        for obj in imported:
                            bpy.data.objects.remove(obj, do_unlink=True)
                relative = _owner_matrix_root_relative(nodes, root, reference_key(draw.get("owner_game_object")))
                names: list[str] = []
                for mesh in templates[template_key]:
                    obj = bpy.data.objects.new(f"SPLINE_REF_{len(audit):03d}_{name}_{draw_index}", mesh)
                    collection.objects.link(obj)
                    obj["endfield_spline_preview_identity"] = identity
                    obj["endfield_spline_preview_status"] = "representative_reference_not_native_simulation"
                    obj["endfield_spline_source_draw_json"] = json.dumps(draw, sort_keys=True)
                    obj["endfield_spline_source_placement_json"] = json.dumps(placement, sort_keys=True)
                    obj["endfield_spline_source_descriptor_json"] = json.dumps(emitter, sort_keys=True)
                    _animate(obj, path, world, relative, speed, interval, fps, duration)
                    names.append(obj.name)
                row.update(status="representative_source_draw_partial", objects=names)
                audit.append(row)
    return audit
