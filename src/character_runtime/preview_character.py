"""Create a source-evidence-driven LOD0 geometry preview from an audited FBX blend."""

from __future__ import annotations

import argparse
import json
import math
import sys
from array import array
from pathlib import Path
from typing import Mapping, Sequence, TypedDict, cast

import bpy
from mathutils import Matrix, Vector


class CliArguments(TypedDict):
    blend: Path
    source_audit: Path
    renderer_export_map: Path | None
    lod_sources: Path
    renderer_visibility: Path
    output_dir: Path
    output_stem: str | None


class RendererVisibilityEvidence(TypedDict):
    source: str
    path_id: int
    renderer_type: str
    game_object_source: str
    game_object_path_id: int
    game_object_name: str
    path: str
    enabled: bool
    active_self: bool
    cast_shadows: int
    platform_specific_cast_shadows: int
    rendering_layer_mask: int
    is_shadows_only: bool


class RendererDecision(TypedDict):
    renderer_identity: str
    renderer_type: str
    native_game_object_identity: str
    native_game_object_path: str
    native_game_object_name: str
    fbx_object_name: str
    fbx_hierarchy_path: str
    lod_index: int | None
    game_object_active: bool
    game_object_active_in_hierarchy: bool
    inactive_source_transform_identities: tuple[str, ...]
    renderer_enabled: bool
    native_cast_shadows: int
    native_platform_specific_cast_shadows: int
    native_rendering_layer_mask: int
    native_shadows_only: bool
    visible: bool
    reason: str
    retained_in_scene: bool


class PackedImageRecord(TypedDict):
    image_name: str
    source_path: str
    bytes: int


class PoseBoneBasisRecord(TypedDict):
    bone_name: str
    matrix_basis_before: list[list[float]]


class ArmaturePoseRecord(TypedDict):
    armature_name: str
    pose_position_before: str
    pose_position_after: str
    bone_count: int
    non_identity_basis_count: int
    maximum_reset_identity_delta: float
    original_pose_basis: list[PoseBoneBasisRecord]


class MeshPoseDeltaRecord(TypedDict):
    mesh_object_name: str
    vertex_count: int
    moved_vertex_count: int
    maximum_world_displacement: float
    mean_world_displacement: float


class StaticBindPoseAudit(TypedDict):
    format: str
    source_blend: str
    initialization: str
    armatures: list[ArmaturePoseRecord]
    visible_mesh_pose_deltas: list[MeshPoseDeltaRecord]


class BoundsRecord(TypedDict):
    minimum: dict[str, float]
    maximum: dict[str, float]


class PreviewReport(TypedDict):
    format: str
    source_blend: str
    source_audit: str
    renderer_export_map: str | None
    lod_sources: str
    renderer_visibility: str
    source_root: str
    static_bind_pose_audit: str
    lod_group_identity: str
    lod_group_enabled: bool
    lod_levels: list[dict[str, object]]
    renderer_decisions: list[RendererDecision]
    summary: dict[str, int]
    bounds: BoundsRecord
    packed_images: list[PackedImageRecord]
    preview_only_objects: list[str]
    render_engine: str
    render_style: str
    output_blend: str
    output_png: str
    output_png_bytes: int
    visual_inspection_performed: bool
    notes: list[str]


def parse_arguments(argv: Sequence[str]) -> CliArguments:
    """Parse explicit source and output paths from Blender's script arguments."""
    script_arguments = list(argv)
    if "--" in script_arguments:
        script_arguments = script_arguments[script_arguments.index("--") + 1:]
    else:
        script_arguments = script_arguments[1:]

    parser = argparse.ArgumentParser(description="Build a source-matched LOD0 geometry preview.")
    parser.add_argument("--blend", type=Path, required=True)
    parser.add_argument("--source-audit", type=Path, required=True)
    parser.add_argument("--renderer-export-map", type=Path)
    parser.add_argument("--lod-sources", type=Path, required=True)
    parser.add_argument("--renderer-visibility", type=Path, required=True)
    parser.add_argument("--output-dir", type=Path, required=True)
    parser.add_argument("--output-stem", type=str)
    arguments = parser.parse_args(script_arguments)
    return {
        "blend": arguments.blend,
        "source_audit": arguments.source_audit,
        "renderer_export_map": arguments.renderer_export_map,
        "lod_sources": arguments.lod_sources,
        "renderer_visibility": arguments.renderer_visibility,
        "output_dir": arguments.output_dir,
        "output_stem": arguments.output_stem,
    }


def require_object(value: object, label: str) -> dict[str, object]:
    """Validate a JSON object and return its string-keyed mapping."""
    if not isinstance(value, dict) or not all(isinstance(key, str) for key in value):
        raise ValueError(f"{label} must be a JSON object with string keys.")
    return cast(dict[str, object], value)


def require_array(value: object, label: str) -> list[object]:
    """Validate a JSON array."""
    if not isinstance(value, list):
        raise ValueError(f"{label} must be a JSON array.")
    return value


def require_string(value: object, label: str) -> str:
    """Validate a non-empty JSON string."""
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{label} must be a non-empty string.")
    return value


def require_boolean(value: object, label: str) -> bool:
    """Validate a JSON boolean without accepting integer substitutes."""
    if not isinstance(value, bool):
        raise ValueError(f"{label} must be a JSON boolean.")
    return value


def require_integer(value: object, label: str) -> int:
    """Validate a JSON integer without accepting boolean values."""
    if not isinstance(value, int) or isinstance(value, bool):
        raise ValueError(f"{label} must be a JSON integer.")
    return value


def read_json_value(path: Path) -> object:
    """Read JSON and report the exact file on parse or I/O errors."""
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as error:
        raise ValueError(f"Invalid JSON in {path} at line {error.lineno}, column {error.colno}: {error.msg}") from error
    except OSError as error:
        raise OSError(f"Could not read JSON input {path}: {error}") from error


def read_json_object(path: Path) -> dict[str, object]:
    """Read a JSON object and report the exact file on shape errors."""
    value = read_json_value(path)
    return require_object(value, f"JSON root in {path}")


def read_renderer_visibility(path: Path, source_audit: dict[str, object]) -> dict[tuple[str, int], RendererVisibilityEvidence]:
    """Validate renderer visibility facts against their embedded TypeTree records."""
    document = read_json_object(path)
    if require_string(document.get("format"), "renderer_visibility.format") != "EndfieldCharacterRendererVisibility/1":
        raise ValueError(f"Unsupported renderer visibility format: {document.get('format')!r}.")

    source_root = require_object(source_audit.get("actual_root"), "source_audit.actual_root")
    visibility_root = require_object(document.get("actual_root"), "renderer_visibility.actual_root")
    source_identity = (
        require_string(source_root.get("source"), "source_audit.actual_root.source"),
        require_integer(source_root.get("path_id"), "source_audit.actual_root.path_id"),
    )
    visibility_identity = (
        require_string(visibility_root.get("source"), "renderer_visibility.actual_root.source"),
        require_integer(visibility_root.get("path_id"), "renderer_visibility.actual_root.path_id"),
    )
    if visibility_identity != source_identity:
        raise ValueError(
            f"Renderer visibility root {visibility_identity} does not match source audit root {source_identity}."
        )

    records: dict[tuple[str, int], RendererVisibilityEvidence] = {}
    for index, raw_record in enumerate(require_array(document.get("renderers"), "renderer_visibility.renderers")):
        label = f"renderer_visibility.renderers[{index}]"
        record = require_object(raw_record, label)
        renderer = require_object(record.get("renderer"), f"{label}.renderer")
        source = require_string(renderer.get("source"), f"{label}.renderer.source")
        path_id = require_integer(renderer.get("path_id"), f"{label}.renderer.path_id")
        renderer_type = require_string(renderer.get("type"), f"{label}.renderer.type")
        identity = (source, path_id)
        if source != source_identity[0]:
            raise ValueError(f"Renderer {identity} belongs to a different source container than audited root {source_identity}.")
        if identity in records:
            raise ValueError(f"Duplicate native renderer visibility identity: {source}:{path_id}.")

        game_object = require_object(record.get("game_object"), f"{label}.game_object")
        game_object_source = require_string(game_object.get("source"), f"{label}.game_object.source")
        game_object_path_id = require_integer(game_object.get("path_id"), f"{label}.game_object.path_id")
        game_object_name = require_string(game_object.get("name"), f"{label}.game_object.name")
        native_path = require_string(record.get("path"), f"{label}.path")
        enabled = require_boolean(record.get("enabled"), f"{label}.enabled")
        active_self = require_boolean(record.get("active_self"), f"{label}.active_self")
        cast_shadows = require_integer(record.get("cast_shadows"), f"{label}.cast_shadows")
        platform_cast_shadows = require_integer(record.get("platform_specific_cast_shadows"), f"{label}.platform_specific_cast_shadows")
        rendering_layer_mask = require_integer(record.get("rendering_layer_mask"), f"{label}.rendering_layer_mask")
        is_shadows_only = require_boolean(record.get("is_shadows_only"), f"{label}.is_shadows_only")
        if cast_shadows not in (0, 1, 2, 3):
            raise ValueError(f"Renderer {source}:{path_id} has unknown native m_CastShadows value {cast_shadows}.")
        if is_shadows_only != (cast_shadows == 3):
            raise ValueError(
                f"Renderer {source}:{path_id} has inconsistent cast_shadows={cast_shadows} and "
                f"is_shadows_only={is_shadows_only}."
            )
        unavailable_fields = require_array(record.get("unavailable_fields"), f"{label}.unavailable_fields")
        if unavailable_fields:
            raise ValueError(f"Renderer {source}:{path_id} has unavailable required source fields: {unavailable_fields}.")

        type_tree = require_object(record.get("renderer_type_tree"), f"{label}.renderer_type_tree")
        if not require_boolean(type_tree.get("available"), f"{label}.renderer_type_tree.available"):
            raise ValueError(f"Renderer {source}:{path_id} has no embedded TypeTree data.")
        type_tree_object = require_object(type_tree.get("object"), f"{label}.renderer_type_tree.object")
        type_tree_identity = (
            require_string(type_tree_object.get("source"), f"{label}.renderer_type_tree.object.source"),
            require_integer(type_tree_object.get("path_id"), f"{label}.renderer_type_tree.object.path_id"),
        )
        type_tree_type = require_string(type_tree_object.get("type"), f"{label}.renderer_type_tree.object.type")
        if type_tree_identity != identity or type_tree_type != renderer_type:
            raise ValueError(f"Renderer {identity} disagrees with its embedded TypeTree object identity/type.")
        type_tree_data = require_object(type_tree.get("data"), f"{label}.renderer_type_tree.data")
        type_tree_game_object = require_object(type_tree_data.get("m_GameObject"), f"{label}.renderer_type_tree.data.m_GameObject")
        if require_integer(type_tree_game_object.get("m_FileID"), f"{label}.renderer_type_tree.data.m_GameObject.m_FileID") != 0:
            raise ValueError(f"Renderer {identity} has an external TypeTree GameObject reference.")
        if require_integer(type_tree_game_object.get("m_PathID"), f"{label}.renderer_type_tree.data.m_GameObject.m_PathID") != game_object_path_id:
            raise ValueError(f"Renderer {identity} has a TypeTree GameObject reference that does not match the visibility row.")
        type_tree_enabled = require_boolean(type_tree_data.get("m_Enabled"), f"{label}.renderer_type_tree.data.m_Enabled")
        type_tree_cast_shadows = require_integer(type_tree_data.get("m_CastShadows"), f"{label}.renderer_type_tree.data.m_CastShadows")
        type_tree_layer_mask = require_integer(type_tree_data.get("m_RenderingLayerMask"), f"{label}.renderer_type_tree.data.m_RenderingLayerMask")
        platform_data = require_object(type_tree_data.get("m_PlatformSpecificCastShadows"), f"{label}.renderer_type_tree.data.m_PlatformSpecificCastShadows")
        type_tree_platform_cast_shadows = require_integer(platform_data.get("defaultParam"), f"{label}.renderer_type_tree.data.m_PlatformSpecificCastShadows.defaultParam")
        if (type_tree_enabled, type_tree_cast_shadows, type_tree_platform_cast_shadows, type_tree_layer_mask) != (
            enabled, cast_shadows, platform_cast_shadows, rendering_layer_mask
        ):
            raise ValueError(f"Renderer {identity} disagrees with its embedded TypeTree visibility fields.")

        records[identity] = {
            "source": source,
            "path_id": path_id,
            "renderer_type": renderer_type,
            "game_object_source": game_object_source,
            "game_object_path_id": game_object_path_id,
            "game_object_name": game_object_name,
            "path": native_path,
            "enabled": enabled,
            "active_self": active_self,
            "cast_shadows": cast_shadows,
            "platform_specific_cast_shadows": platform_cast_shadows,
            "rendering_layer_mask": rendering_layer_mask,
            "is_shadows_only": is_shadows_only,
        }
    return records


def resolve_input_path(value: Path, label: str) -> Path:
    """Resolve a required existing regular file."""
    try:
        path = value.expanduser().resolve(strict=True)
    except OSError as error:
        raise FileNotFoundError(f"{label} does not exist: {value}") from error
    if not path.is_file():
        raise ValueError(f"{label} must be a regular file: {path}")
    return path


def blender_hierarchy_path(obj: bpy.types.Object) -> str:
    """Return a complete parent-to-child Blender object path."""
    parts: list[str] = []
    seen: set[int] = set()
    current: bpy.types.Object | None = obj
    while current is not None:
        pointer = current.as_pointer()
        if pointer in seen:
            raise RuntimeError(f"Parent cycle found while resolving Blender object path for {obj.name}.")
        seen.add(pointer)
        parts.append(current.name)
        current = current.parent
    return "/".join(reversed(parts))


def require_blender_version() -> None:
    """Require the Blender release whose reviewed FBX blend was produced with."""
    if bpy.app.version < (4, 4, 0):
        raise RuntimeError(f"Blender 4.4 or newer is required; found {bpy.app.version_string}.")


def select_root_lod_document(
    lod_documents: object,
    source_audit: dict[str, object],
) -> dict[str, object]:
    """Select the unique native LODGroup attached to the audited source root."""
    root = require_object(source_audit.get("actual_root"), "source_audit.actual_root")
    root_source = require_string(root.get("source"), "source_audit.actual_root.source")
    root_path_id = require_integer(root.get("path_id"), "source_audit.actual_root.path_id")
    if require_string(root.get("type"), "source_audit.actual_root.type") != "GameObject":
        raise ValueError("source_audit.actual_root.type must be GameObject.")
    candidates: list[dict[str, object]] = []
    for index, raw_document in enumerate(require_array(lod_documents, "lod_sources root")):
        document = require_object(raw_document, f"lod_sources[{index}]")
        lod_object = require_object(document.get("object"), f"lod_sources[{index}].object")
        if require_string(lod_object.get("type"), f"lod_sources[{index}].object.type") != "LODGroup":
            raise ValueError(f"lod_sources[{index}].object.type is not LODGroup.")
        data = require_object(document.get("data"), f"lod_sources[{index}].data")
        game_object_reference = require_object(data.get("m_GameObject"), f"lod_sources[{index}].data.m_GameObject")
        file_id = require_integer(game_object_reference.get("m_FileID"), f"lod_sources[{index}].data.m_GameObject.m_FileID")
        path_id = require_integer(game_object_reference.get("m_PathID"), f"lod_sources[{index}].data.m_GameObject.m_PathID")
        source = require_string(lod_object.get("source"), f"lod_sources[{index}].object.source")
        if file_id == 0 and source == root_source and path_id == root_path_id:
            candidates.append(document)
    if len(candidates) != 1:
        raise ValueError(
            f"Expected exactly one native LODGroup attached to audited root {root_source}:{root_path_id}; "
            f"found {len(candidates)}."
        )
    return candidates[0]


def source_lod_membership(lod_document: dict[str, object]) -> tuple[str, str, bool, list[dict[str, object]], dict[tuple[str, int], int]]:
    """Resolve every native LOD renderer reference to exactly one LOD index."""
    lod_object = require_object(lod_document.get("object"), "lod_sources.object")
    if require_string(lod_object.get("type"), "lod_sources.object.type") != "LODGroup":
        raise ValueError(f"Expected a native LODGroup object; found {lod_object.get('type')!r}.")
    source_id = require_string(lod_object.get("source"), "lod_sources.object.source")
    lod_group_path_id = require_integer(lod_object.get("path_id"), "lod_sources.object.path_id")
    available = require_boolean(lod_document.get("available"), "lod_sources.available")
    if not available:
        reason = lod_document.get("reason")
        raise ValueError(f"Native LODGroup data is unavailable: {reason!r}.")
    data = require_object(lod_document.get("data"), "lod_sources.data")
    levels = require_array(data.get("m_LODs"), "lod_sources.data.m_LODs")
    if not levels:
        raise ValueError("Native LODGroup contains no LOD levels.")
    group_enabled = require_boolean(data.get("m_Enabled"), "lod_sources.data.m_Enabled")

    level_records: list[dict[str, object]] = []
    membership: dict[tuple[str, int], int] = {}
    for level_index, raw_level in enumerate(levels):
        level = require_object(raw_level, f"lod_sources.data.m_LODs[{level_index}]")
        screen_height = level.get("screenRelativeHeight")
        if not isinstance(screen_height, (int, float)) or isinstance(screen_height, bool) or not math.isfinite(float(screen_height)):
            raise ValueError(f"LOD level {level_index} has an invalid screenRelativeHeight: {screen_height!r}.")
        raw_renderers = require_array(level.get("renderers"), f"LOD level {level_index}.renderers")
        level_records.append({
            "lod_index": level_index,
            "screen_relative_height": float(screen_height),
            "renderer_count": len(raw_renderers),
        })
        for renderer_index, raw_entry in enumerate(raw_renderers):
            entry = require_object(raw_entry, f"LOD level {level_index}.renderers[{renderer_index}]")
            reference = require_object(entry.get("renderer"), f"LOD level {level_index}.renderers[{renderer_index}].renderer")
            file_id = require_integer(reference.get("m_FileID"), "LOD renderer m_FileID")
            path_id = require_integer(reference.get("m_PathID"), "LOD renderer m_PathID")
            if file_id != 0:
                raise ValueError(f"LOD renderer reference {file_id}:{path_id} is external; this source audit cannot resolve it safely.")
            identity = (source_id, path_id)
            if identity in membership:
                raise ValueError(f"Native renderer identity {source_id}:{path_id} appears in multiple LOD entries.")
            membership[identity] = level_index

    if not membership:
        raise ValueError(f"Native LODGroup {source_id}:{lod_group_path_id} contains no renderer references.")
    return f"{source_id}:{lod_group_path_id}", source_id, group_enabled, level_records, membership


def source_renderer_rows(
    source_audit: dict[str, object],
    lod_source_id: str,
    membership: dict[tuple[str, int], int],
) -> list[tuple[dict[str, object], dict[str, object], dict[str, object], int | None]]:
    """Resolve each renderer identity through its native GameObject transform path."""
    root = require_object(source_audit.get("actual_root"), "source_audit.actual_root")
    root_name = require_string(root.get("name"), "source_audit.actual_root.name")
    transform_index: dict[tuple[str, int], list[dict[str, object]]] = {}
    for index, raw_transform in enumerate(require_array(source_audit.get("transforms"), "source_audit.transforms")):
        transform = require_object(raw_transform, f"source_audit.transforms[{index}]")
        transform_object = require_object(transform.get("object"), f"source_audit.transforms[{index}].object")
        if transform_object.get("type") != "Transform":
            continue
        game_object = require_object(transform.get("game_object"), f"source_audit.transforms[{index}].game_object")
        game_object_identity = (
            require_string(game_object.get("source"), f"source_audit.transforms[{index}].game_object.source"),
            require_integer(game_object.get("path_id"), f"source_audit.transforms[{index}].game_object.path_id"),
        )
        transform_index.setdefault(game_object_identity, []).append(transform)

    renderers = require_array(source_audit.get("renderers"), "source_audit.renderers")
    renderer_index: dict[tuple[str, int], list[dict[str, object]]] = {}
    for index, raw_renderer in enumerate(renderers):
        renderer = require_object(raw_renderer, f"source_audit.renderers[{index}]")
        renderer_object = require_object(renderer.get("object"), f"source_audit.renderers[{index}].object")
        renderer_identity = (
            require_string(renderer_object.get("source"), f"source_audit.renderers[{index}].object.source"),
            require_integer(renderer_object.get("path_id"), f"source_audit.renderers[{index}].object.path_id"),
        )
        renderer_index.setdefault(renderer_identity, []).append(renderer)

    rows: list[tuple[dict[str, object], dict[str, object], dict[str, object], int | None]] = []
    required_identities = set(renderer_index)
    if not set(membership).issubset(required_identities):
        missing = sorted(f"{source}:{path_id}" for source, path_id in set(membership) - required_identities)
        raise ValueError(f"LODGroup renderer references are absent from source_audit.renderers: {missing}.")
    if any(identity[0] != lod_source_id for identity in membership):
        raise ValueError(f"LODGroup source identity {lod_source_id} disagrees with one or more renderer identities.")

    for renderer_identity, renderer_rows in sorted(renderer_index.items(), key=lambda item: (item[0][0], item[0][1])):
        if len(renderer_rows) != 1:
            raise ValueError(f"Renderer identity {renderer_identity[0]}:{renderer_identity[1]} is ambiguous in source_audit.renderers ({len(renderer_rows)} rows).")
        renderer = renderer_rows[0]
        game_object = require_object(renderer.get("game_object"), f"renderer {renderer_identity[1]}.game_object")
        game_object_identity = (
            require_string(game_object.get("source"), f"renderer {renderer_identity[1]}.game_object.source"),
            require_integer(game_object.get("path_id"), f"renderer {renderer_identity[1]}.game_object.path_id"),
        )
        transform_rows = transform_index.get(game_object_identity, [])
        if len(transform_rows) != 1:
            raise ValueError(
                f"Renderer {renderer_identity[0]}:{renderer_identity[1]} resolves to {len(transform_rows)} native GameObject transforms; expected exactly one."
            )
        transform = transform_rows[0]
        native_path = require_string(transform.get("path"), f"transform {game_object_identity[1]}.path")
        renderer_path = require_string(renderer.get("path"), f"renderer {renderer_identity[1]}.path")
        game_object_name = require_string(game_object.get("name"), f"renderer {renderer_identity[1]}.game_object.name")
        transform_game_object = require_object(transform.get("game_object"), f"transform {game_object_identity[1]}.game_object")
        if native_path != renderer_path:
            raise ValueError(
                f"Renderer path and GameObject Transform path disagree for {renderer_identity[0]}:{renderer_identity[1]}: {renderer_path!r} vs {native_path!r}."
            )
        if transform_game_object.get("name") != game_object_name:
            raise ValueError(f"Renderer and Transform GameObject names disagree for native identity {game_object_identity}.")
        path_parts = tuple(part for part in native_path.split("/") if part)
        if not path_parts or path_parts[0] != root_name or path_parts[-1] != game_object_name:
            raise ValueError(
                f"Native GameObject path {native_path!r} is inconsistent with audited root {root_name!r} and name {game_object_name!r}."
            )
        game_object_active = require_boolean(transform.get("game_object_active"), f"transform {game_object_identity[1]}.game_object_active")
        renderer_enabled = require_boolean(renderer.get("enabled"), f"renderer {renderer_identity[1]}.enabled")
        lod_index = membership.get(renderer_identity)
        if lod_index is not None and renderer_identity[0] != lod_source_id:
            raise ValueError(f"LOD membership identity has unexpected source {renderer_identity[0]}.")
        rows.append((renderer, transform, {"root_name": root_name, "native_path": native_path}, lod_index))

    return rows


def identity_from_export_row(value: object, label: str) -> tuple[str, int]:
    """Read a string-preserved source object identity from the renderer export map."""
    identity = require_object(value, label)
    source = require_string(identity.get("source"), f"{label}.source")
    path_id_text = require_string(identity.get("path_id"), f"{label}.path_id")
    try:
        path_id = int(path_id_text, 10)
    except ValueError as error:
        raise ValueError(f"{label}.path_id must be a base-10 integer string: {path_id_text!r}.") from error
    if str(path_id) != path_id_text:
        raise ValueError(f"{label}.path_id must use canonical base-10 formatting: {path_id_text!r}.")
    return source, path_id


def read_renderer_export_names(
    path: Path,
    source_audit: dict[str, object],
    renderer_rows: Sequence[tuple[dict[str, object], dict[str, object], dict[str, object], int | None]],
) -> dict[tuple[str, int], str]:
    """Validate the reader's explicit renderer-to-FBX identity map against the source audit."""
    document = read_json_object(path)
    if require_string(document.get("format"), "renderer_export_map.format") != "EndfieldCharacterRendererExportMap/1":
        raise ValueError(f"Unsupported renderer export map format: {document.get('format')!r}.")
    source_commit = require_string(source_audit.get("commit"), "source_audit.commit")
    if require_string(document.get("commit"), "renderer_export_map.commit") != source_commit:
        raise ValueError("Renderer export map commit does not match source_audit.commit.")

    root = require_object(source_audit.get("actual_root"), "source_audit.actual_root")
    expected_root = (
        require_string(root.get("source"), "source_audit.actual_root.source"),
        require_integer(root.get("path_id"), "source_audit.actual_root.path_id"),
    )
    if identity_from_export_row(document.get("root"), "renderer_export_map.root") != expected_root:
        raise ValueError("Renderer export map root identity does not match source_audit.actual_root.")

    expected: dict[tuple[str, int], tuple[tuple[str, int], tuple[str, int], str, str]] = {}
    for renderer, transform, _, _ in renderer_rows:
        renderer_object = require_object(renderer.get("object"), "source renderer object")
        renderer_identity = (
            require_string(renderer_object.get("source"), "source renderer object.source"),
            require_integer(renderer_object.get("path_id"), "source renderer object.path_id"),
        )
        game_object = require_object(renderer.get("game_object"), "source renderer game_object")
        game_object_identity = (
            require_string(game_object.get("source"), "source renderer game_object.source"),
            require_integer(game_object.get("path_id"), "source renderer game_object.path_id"),
        )
        mesh = require_object(renderer.get("mesh"), "source renderer mesh")
        mesh_identity = (
            require_string(mesh.get("source"), "source renderer mesh.source"),
            require_integer(mesh.get("path_id"), "source renderer mesh.path_id"),
        )
        source_path = require_string(transform.get("path"), "source renderer transform.path")
        source_name = require_string(game_object.get("name"), "source renderer game_object.name")
        if renderer_identity in expected:
            raise ValueError(f"Source renderer identity is duplicated while validating export map: {renderer_identity}.")
        expected[renderer_identity] = (game_object_identity, mesh_identity, source_path, source_name)

    names: dict[tuple[str, int], str] = {}
    export_paths: set[str] = set()
    raw_rows = require_array(document.get("renderers"), "renderer_export_map.renderers")
    for index, raw_row in enumerate(raw_rows):
        row = require_object(raw_row, f"renderer_export_map.renderers[{index}]")
        renderer_identity = identity_from_export_row(row.get("renderer_identity"), f"renderer_export_map.renderers[{index}].renderer_identity")
        expected_values = expected.get(renderer_identity)
        if expected_values is None:
            raise ValueError(f"Renderer export map contains an identity absent from source_audit: {renderer_identity}.")
        game_object_identity = identity_from_export_row(row.get("game_object_identity"), f"renderer_export_map.renderers[{index}].game_object_identity")
        mesh_identity = identity_from_export_row(row.get("mesh_identity"), f"renderer_export_map.renderers[{index}].mesh_identity")
        source_path = require_string(row.get("source_path"), f"renderer_export_map.renderers[{index}].source_path")
        source_name = require_string(row.get("source_name"), f"renderer_export_map.renderers[{index}].source_name")
        expected_game_object, expected_mesh, expected_path, expected_name = expected_values
        if (game_object_identity, mesh_identity, source_path, source_name) != (expected_game_object, expected_mesh, expected_path, expected_name):
            raise ValueError(f"Renderer export map source metadata differs from source_audit for {renderer_identity}.")
        export_name = require_string(row.get("export_name"), f"renderer_export_map.renderers[{index}].export_name")
        if "/" in export_name or "\\" in export_name:
            raise ValueError(f"Renderer export name must be a single object name: {export_name!r}.")
        export_path = require_string(row.get("export_path"), f"renderer_export_map.renderers[{index}].export_path")
        if export_path.rsplit("/", 1)[-1] != export_name:
            raise ValueError(f"Renderer export path does not end with its explicit export name: {export_path!r}.")
        if renderer_identity in names:
            raise ValueError(f"Renderer export map repeats source identity {renderer_identity}.")
        names[renderer_identity] = export_name
        if export_path in export_paths:
            raise ValueError(f"Renderer export path is duplicated in the explicit identity map: {export_path!r}.")
        export_paths.add(export_path)

    if set(names) != set(expected):
        missing = sorted(set(expected) - set(names))
        extra = sorted(set(names) - set(expected))
        raise ValueError(f"Renderer export map does not cover the source audit exactly; missing={missing}, extra={extra}.")
    if len(set(names.values())) != len(names):
        raise ValueError("Renderer export map contains duplicate FBX object names.")
    return names


def verify_renderer_visibility_coverage(
    visibility: dict[tuple[str, int], RendererVisibilityEvidence],
    renderer_rows: Sequence[tuple[dict[str, object], dict[str, object], dict[str, object], int | None]],
) -> int:
    """Require exact renderer identity and source-path coverage before using cast modes."""
    source_identities: set[tuple[str, int]] = set()
    for renderer, transform, path_metadata, _ in renderer_rows:
        renderer_object = require_object(renderer.get("object"), "source renderer object")
        identity = (
            require_string(renderer_object.get("source"), "source renderer object.source"),
            require_integer(renderer_object.get("path_id"), "source renderer object.path_id"),
        )
        source_identities.add(identity)
        evidence = visibility.get(identity)
        if evidence is None:
            raise ValueError(f"Renderer visibility evidence is missing source renderer {identity[0]}:{identity[1]}.")
        game_object = require_object(renderer.get("game_object"), f"source renderer {identity[1]}.game_object")
        expected_game_object_identity = (
            require_string(game_object.get("source"), f"source renderer {identity[1]}.game_object.source"),
            require_integer(game_object.get("path_id"), f"source renderer {identity[1]}.game_object.path_id"),
        )
        expected_game_object_name = require_string(game_object.get("name"), f"source renderer {identity[1]}.game_object.name")
        expected_path = require_string(transform.get("path"), f"source transform {identity[1]}.path")
        expected_renderer_type = require_string(renderer_object.get("type"), f"source renderer {identity[1]}.object.type")
        expected_enabled = require_boolean(renderer.get("enabled"), f"source renderer {identity[1]}.enabled")
        expected_active = require_boolean(transform.get("game_object_active"), f"source transform {identity[1]}.game_object_active")
        if (evidence["game_object_source"], evidence["game_object_path_id"]) != expected_game_object_identity:
            raise ValueError(f"Renderer visibility GameObject identity does not match source renderer {identity[0]}:{identity[1]}.")
        if evidence["game_object_name"] != expected_game_object_name or evidence["path"] != expected_path:
            raise ValueError(f"Renderer visibility path/name does not match source renderer {identity[0]}:{identity[1]}.")
        if evidence["renderer_type"] != expected_renderer_type:
            raise ValueError(f"Renderer visibility type does not match source renderer {identity[0]}:{identity[1]}.")
        if evidence["enabled"] != expected_enabled or evidence["active_self"] != expected_active:
            raise ValueError(f"Renderer visibility active/enabled flags do not match source renderer {identity[0]}:{identity[1]}.")

    visibility_identities = set(visibility)
    if source_identities != visibility_identities:
        missing = sorted(f"{source}:{path_id}" for source, path_id in source_identities - visibility_identities)
        extra = sorted(f"{source}:{path_id}" for source, path_id in visibility_identities - source_identities)
        raise ValueError(f"Renderer visibility identity coverage differs from source audit; missing={missing}, extra={extra}.")
    return sum(1 for evidence in visibility.values() if evidence["is_shadows_only"])


def map_renderers_to_fbx_objects(
    renderer_rows: Sequence[tuple[dict[str, object], dict[str, object], dict[str, object], int | None]],
    visibility: dict[tuple[str, int], RendererVisibilityEvidence],
    renderer_export_names: Mapping[tuple[str, int], str] | None,
) -> list[tuple[dict[str, object], dict[str, object], int | None, bpy.types.Object, str, RendererVisibilityEvidence]]:
    """Match each source GameObject name to one unique mesh under the audited FBX root."""
    mesh_objects = [obj for obj in bpy.context.scene.objects if obj.type == "MESH"]
    if not mesh_objects:
        raise RuntimeError("The input Blender file contains no mesh objects to match against source renderers.")
    blender_paths = {obj.as_pointer(): blender_hierarchy_path(obj) for obj in mesh_objects}
    mapped_objects: set[int] = set()
    matches: list[tuple[dict[str, object], dict[str, object], int | None, bpy.types.Object, str, RendererVisibilityEvidence]] = []

    for renderer, transform, path_metadata, lod_index in renderer_rows:
        game_object = require_object(renderer.get("game_object"), "source renderer game_object")
        game_object_name = require_string(game_object.get("name"), "source renderer game_object.name")
        native_path = require_string(path_metadata.get("native_path"), "native GameObject path")
        root_name = require_string(path_metadata.get("root_name"), "audited root name")
        renderer_object = require_object(renderer.get("object"), "source renderer object")
        renderer_source = require_string(renderer_object.get("source"), "source renderer identity source")
        renderer_path_id = require_integer(renderer_object.get("path_id"), "source renderer identity path_id")
        if renderer_export_names is None:
            match_name = game_object_name
        else:
            export_name = renderer_export_names.get((renderer_source, renderer_path_id))
            if export_name is None:
                raise ValueError(f"Explicit renderer export map has no source identity {renderer_source}:{renderer_path_id}.")
            match_name = export_name
        candidates = [
            obj for obj in mesh_objects
            if obj.name == match_name and blender_paths[obj.as_pointer()].split("/")[0] == root_name
        ]
        if len(candidates) != 1:
            candidate_paths = sorted(blender_paths[obj.as_pointer()] for obj in candidates)
            raise ValueError(
                f"Native renderer {renderer_source}:{renderer_path_id} at GameObject path {native_path!r} matched {len(candidates)} FBX hierarchy objects; expected exactly one. Candidates={candidate_paths}."
            )
        obj = candidates[0]
        pointer = obj.as_pointer()
        if pointer in mapped_objects:
            raise ValueError(
                f"Multiple native renderer identities map to the same FBX hierarchy path {blender_paths[pointer]!r}."
            )
        mapped_objects.add(pointer)
        native_visibility = visibility[(renderer_source, renderer_path_id)]
        matches.append((renderer, transform, lod_index, obj, blender_paths[pointer], native_visibility))

    unmatched_mesh_paths = sorted(blender_paths[pointer] for pointer in blender_paths if pointer not in mapped_objects)
    if unmatched_mesh_paths:
        raise ValueError(f"FBX mesh objects have no unique source renderer identity: {unmatched_mesh_paths}.")
    return matches


class SourceHierarchyActivity(TypedDict):
    active_in_hierarchy: bool
    inactive_transform_identities: tuple[str, ...]


def source_hierarchy_activity(source_audit: Mapping[str, object]) -> dict[tuple[str, int], SourceHierarchyActivity]:
    """Compute Unity activeInHierarchy from exact source Transform parent identities."""
    nodes: dict[tuple[str, int], tuple[tuple[str, int], bool, tuple[str, int] | None]] = {}
    for index, raw in enumerate(require_array(source_audit.get("transforms"), "source_audit.transforms")):
        row = require_object(raw, f"source transform {index}")
        identity = require_object(row.get("object"), f"source transform {index}.object")
        source = require_string(identity.get("source"), f"source transform {index}.source").casefold()
        path_id = require_integer(identity.get("path_id"), f"source transform {index}.path_id")
        if require_string(identity.get("type"), f"source transform {index}.type") != "Transform":
            raise ValueError(f"Source hierarchy object is not Transform: {source}:{path_id}.")
        key = (source, path_id)
        if key in nodes:
            raise ValueError(f"Duplicate source Transform identity: {source}:{path_id}.")
        game_object = require_object(row.get("game_object"), f"source transform {index}.game_object")
        game_key = (require_string(game_object.get("source"), f"source transform {index}.game_object.source").casefold(), require_integer(game_object.get("path_id"), f"source transform {index}.game_object.path_id"))
        active_self = require_boolean(row.get("game_object_active"), f"source transform {index}.game_object_active")
        parent = require_object(row.get("parent"), f"source transform {index}.parent")
        parent_file = require_integer(parent.get("file_id"), f"source transform {index}.parent.file_id")
        parent_id = require_integer(parent.get("path_id"), f"source transform {index}.parent.path_id")
        parent_key: tuple[str, int] | None = None
        if parent_id == 0:
            if parent_file != 0 or parent.get("resolved") is not None:
                raise ValueError(f"Invalid null source Transform parent: {source}:{path_id}.")
        else:
            resolved = require_object(parent.get("resolved"), f"source transform {index}.parent.resolved")
            parent_source = require_string(resolved.get("source"), f"source transform {index}.parent.resolved.source").casefold()
            resolved_id = require_integer(resolved.get("path_id"), f"source transform {index}.parent.resolved.path_id")
            if resolved_id != parent_id or require_string(resolved.get("type"), f"source transform {index}.parent.resolved.type") != "Transform" or (parent_file == 0 and parent_source != source):
                raise ValueError(f"Source Transform parent pointer and resolved identity disagree: {source}:{path_id}.")
            parent_key = (parent_source, parent_id)
        nodes[key] = (game_key, active_self, parent_key)
    states: dict[tuple[str, int], SourceHierarchyActivity] = {}
    for start in nodes:
        chain: list[tuple[str, int]] = []
        seen: set[tuple[str, int]] = set()
        current: tuple[str, int] | None = start
        while current is not None and current not in states:
            if current in seen:
                raise ValueError(f"Cycle in source Transform hierarchy: {current[0]}:{current[1]}.")
            if current not in nodes:
                raise ValueError(f"Source Transform ancestor is missing: {current[0]}:{current[1]}, child={start[0]}:{start[1]}.")
            seen.add(current)
            chain.append(current)
            current = nodes[current][2]
        inherited: SourceHierarchyActivity = states[current] if current is not None else {"active_in_hierarchy": True, "inactive_transform_identities": ()}
        for identity in reversed(chain):
            _, active_self, _ = nodes[identity]
            inactive = inherited["inactive_transform_identities"] + (() if active_self else (f"{identity[0]}:{identity[1]}",))
            inherited = {"active_in_hierarchy": active_self and inherited["active_in_hierarchy"], "inactive_transform_identities": inactive}
            states[identity] = inherited
    result: dict[tuple[str, int], SourceHierarchyActivity] = {}
    for identity, (game_key, _, _) in nodes.items():
        if game_key in result:
            raise ValueError(f"GameObject resolves to multiple source Transforms: {game_key[0]}:{game_key[1]}.")
        result[game_key] = states[identity]
    return result


def visibility_decision(
    lod_index: int | None,
    game_object_active: bool,
    renderer_enabled: bool,
    native_shadows_only: bool,
) -> tuple[bool, str]:
    """Apply native LOD, active, enabled, and shadow-only evidence."""
    if lod_index is not None and lod_index > 0:
        return False, f"native_lod_level_{lod_index}_excluded_from_lod0_preview"
    if not game_object_active:
        return False, "source_game_object_inactive"
    if not renderer_enabled:
        return False, "source_renderer_disabled"
    if native_shadows_only:
        return False, "native_cast_shadows_shadows_only_excluded_from_color_geometry"
    if lod_index == 0:
        return True, "native_lod_level_0_game_object_active_renderer_enabled"
    return True, "not_in_native_lod_group_source_game_object_active_renderer_enabled"


def set_mesh_visibility(
    matches: Sequence[tuple[dict[str, object], dict[str, object], int | None, bpy.types.Object, str, RendererVisibilityEvidence]],
    hierarchy_activity: Mapping[tuple[str, int], SourceHierarchyActivity],
) -> tuple[list[RendererDecision], list[bpy.types.Object]]:
    """Set viewport/render flags and retain every source mesh object in the scene."""
    decisions: list[RendererDecision] = []
    visible_objects: list[bpy.types.Object] = []
    for renderer, transform, lod_index, obj, fbx_path, native_visibility in matches:
        game_object = require_object(renderer.get("game_object"), f"renderer {obj.name}.game_object")
        renderer_identity = require_object(renderer.get("object"), f"renderer {obj.name}.object")
        game_object_identity = (
            require_string(game_object.get("source"), f"renderer {obj.name}.game_object.source"),
            require_integer(game_object.get("path_id"), f"renderer {obj.name}.game_object.path_id"),
        )
        renderer_source = require_string(renderer_identity.get("source"), f"renderer {obj.name}.object.source")
        renderer_path_id = require_integer(renderer_identity.get("path_id"), f"renderer {obj.name}.object.path_id")
        game_object_active = require_boolean(transform.get("game_object_active"), f"transform {obj.name}.game_object_active")
        renderer_enabled = require_boolean(renderer.get("enabled"), f"renderer {obj.name}.enabled")
        hierarchy = hierarchy_activity[(game_object_identity[0].casefold(), game_object_identity[1])]
        visible, reason = visibility_decision(
            lod_index,
            hierarchy["active_in_hierarchy"],
            renderer_enabled,
            native_visibility["is_shadows_only"],
        )
        if game_object_active and not hierarchy["active_in_hierarchy"]:
            reason = "source_ancestor_game_object_inactive"
        obj.hide_render = not visible
        obj.hide_viewport = not visible
        obj.hide_set(not visible)
        if visible:
            visible_objects.append(obj)
        decisions.append({
            "renderer_identity": f"{renderer_source}:{renderer_path_id}",
            "renderer_type": require_string(renderer_identity.get("type"), f"renderer {obj.name}.object.type"),
            "native_game_object_identity": f"{game_object_identity[0]}:{game_object_identity[1]}",
            "native_game_object_path": require_string(transform.get("path"), f"transform {obj.name}.path"),
            "native_game_object_name": require_string(game_object.get("name"), f"renderer {obj.name}.game_object.name"),
            "fbx_object_name": obj.name,
            "fbx_hierarchy_path": fbx_path,
            "lod_index": lod_index,
            "game_object_active": game_object_active,
            "game_object_active_in_hierarchy": hierarchy["active_in_hierarchy"],
            "inactive_source_transform_identities": hierarchy["inactive_transform_identities"],
            "renderer_enabled": renderer_enabled,
            "native_cast_shadows": native_visibility["cast_shadows"],
            "native_platform_specific_cast_shadows": native_visibility["platform_specific_cast_shadows"],
            "native_rendering_layer_mask": native_visibility["rendering_layer_mask"],
            "native_shadows_only": native_visibility["is_shadows_only"],
            "visible": visible,
            "reason": reason,
            "retained_in_scene": bpy.data.objects.get(obj.name) == obj,
        })
    if not visible_objects:
        raise RuntimeError("Source active/enabled evidence leaves no mesh visible for the LOD0 preview.")
    return decisions, visible_objects


def visible_world_points(mesh_objects: Sequence[bpy.types.Object]) -> list[Vector]:
    """Collect evaluated world-space vertices for camera framing."""
    dependency_graph = bpy.context.evaluated_depsgraph_get()
    points: list[Vector] = []
    for mesh_object in mesh_objects:
        evaluated_object = mesh_object.evaluated_get(dependency_graph)
        evaluated_mesh = evaluated_object.to_mesh()
        try:
            world_matrix = evaluated_object.matrix_world.copy()
            points.extend(world_matrix @ vertex.co for vertex in evaluated_mesh.vertices)
        finally:
            evaluated_object.to_mesh_clear()
    if not points:
        raise RuntimeError("Visible source renderer meshes produced no evaluated vertices for preview framing.")
    if any(not all(math.isfinite(float(component)) for component in point) for point in points):
        raise RuntimeError("Visible source renderer geometry contains non-finite world coordinates.")
    return points


def vector_bounds(points: Sequence[Vector]) -> tuple[Vector, Vector]:
    """Return min and max vectors for a non-empty point list."""
    if not points:
        raise ValueError("Cannot compute geometry bounds from an empty point list.")
    minimum = Vector((
        min(point.x for point in points),
        min(point.y for point in points),
        min(point.z for point in points),
    ))
    maximum = Vector((
        max(point.x for point in points),
        max(point.y for point in points),
        max(point.z for point in points),
    ))
    return minimum, maximum


def create_preview_collection() -> bpy.types.Collection:
    """Create a collection for camera, lighting, and ground preview fixtures."""
    collection = bpy.data.collections.new("PREVIEW_ONLY")
    bpy.context.scene.collection.children.link(collection)
    return collection


def create_preview_ground(collection: bpy.types.Collection, center: Vector, minimum_z: float, extent: float) -> bpy.types.Object:
    """Create a neutral, preview-only ground plane below the source geometry."""
    ground_mesh = bpy.data.meshes.new("PREVIEW_ONLY_GroundMesh")
    half_size = extent * 2.5
    ground_mesh.from_pydata(
        [(-half_size, -half_size, 0.0), (half_size, -half_size, 0.0),
         (half_size, half_size, 0.0), (-half_size, half_size, 0.0)],
        [],
        [(0, 1, 2, 3)],
    )
    ground_mesh.update()
    ground = bpy.data.objects.new("PREVIEW_ONLY_Ground", ground_mesh)
    collection.objects.link(ground)
    ground.location = (center.x, center.y, minimum_z - extent * 0.025)
    ground.color = (0.12, 0.15, 0.19, 1.0)
    return ground


def create_preview_light(collection: bpy.types.Collection, name: str, location: Vector, target: Vector, extent: float, energy: float) -> bpy.types.Object:
    """Create an area-light fixture for optional non-Workbench inspection."""
    light_data = bpy.data.lights.new(name, type="AREA")
    light_data.energy = energy
    light_data.shape = "DISK"
    light_data.size = extent
    light = bpy.data.objects.new(name, light_data)
    collection.objects.link(light)
    light.location = location
    light.rotation_euler = (target - location).to_track_quat("-Z", "Y").to_euler()
    return light


def configure_preview(
    visible_meshes: Sequence[bpy.types.Object],
    output_png: Path,
) -> tuple[list[str], tuple[Vector, Vector]]:
    """Configure neutral Workbench rendering and add preview-only fixtures."""
    scene = bpy.context.scene
    scene.render.engine = "BLENDER_WORKBENCH"
    scene.render.resolution_x = 1024
    scene.render.resolution_y = 1280
    scene.render.resolution_percentage = 100
    scene.render.image_settings.file_format = "PNG"
    scene.render.image_settings.color_mode = "RGB"
    scene.render.film_transparent = False
    scene.render.filepath = str(output_png)
    scene.display.shading.light = "STUDIO"
    scene.display.shading.color_type = "OBJECT"
    scene.display.shading.background_type = "WORLD"
    scene.display.shading.show_shadows = True
    scene.display.shading.show_cavity = True
    scene.world.color = (0.035, 0.045, 0.06)

    for mesh_object in visible_meshes:
        mesh_object.color = (0.62, 0.66, 0.72, 1.0)

    points = visible_world_points(visible_meshes)
    minimum, maximum = vector_bounds(points)
    center = (minimum + maximum) * 0.5
    dimensions = maximum - minimum
    extent = max(float(dimensions.x), float(dimensions.y), float(dimensions.z))
    if not math.isfinite(extent) or extent <= 0.0:
        raise RuntimeError(f"Visible source geometry has invalid bounds: {tuple(dimensions)}.")

    preview_collection = create_preview_collection()
    ground = create_preview_ground(preview_collection, center, minimum.z, extent)
    camera_data = bpy.data.cameras.new("PREVIEW_ONLY_CameraData")
    camera_data.type = "ORTHO"
    camera_data.clip_start = max(extent * 0.001, 1e-5)
    camera_data.clip_end = extent * 12.0
    camera = bpy.data.objects.new("PREVIEW_ONLY_Camera", camera_data)
    preview_collection.objects.link(camera)
    direction = Vector((0.38, -1.0, 0.18)).normalized()
    camera.location = center + direction * extent * 4.0
    camera.rotation_euler = (center - camera.location).to_track_quat("-Z", "Y").to_euler()
    scene.camera = camera
    camera_data.ortho_scale = 1.0
    camera_rotation = camera.rotation_euler.to_quaternion()
    projected = [camera_rotation.inverted() @ (point - center) for point in points]
    half_width = max(abs(point.x) for point in projected)
    half_height = max(abs(point.y) for point in projected)
    frame = camera_data.view_frame(scene=scene)
    frame_half_width = max(abs(point.x) for point in frame)
    frame_half_height = max(abs(point.y) for point in frame)
    if frame_half_width <= 0.0 or frame_half_height <= 0.0:
        raise RuntimeError("Blender returned an invalid orthographic camera frame.")
    camera_data.ortho_scale = max(half_width / frame_half_width, half_height / frame_half_height) * 1.18

    key_light = create_preview_light(
        preview_collection,
        "PREVIEW_ONLY_KeyLight",
        center + Vector((extent * 2.0, -extent * 2.8, extent * 2.6)),
        center,
        extent * 1.8,
        900.0,
    )
    fill_light = create_preview_light(
        preview_collection,
        "PREVIEW_ONLY_FillLight",
        center + Vector((-extent * 2.1, -extent * 1.4, extent * 1.3)),
        center,
        extent * 2.2,
        450.0,
    )
    bpy.context.view_layer.update()
    return [camera.name, key_light.name, fill_light.name, ground.name], (minimum, maximum)


def referenced_mesh_images() -> list[bpy.types.Image]:
    """Return unique images referenced by materials assigned to scene meshes."""
    materials = {
        slot.material
        for obj in bpy.context.scene.objects
        if obj.type == "MESH"
        for slot in obj.material_slots
        if slot.material is not None
    }
    images: dict[int, bpy.types.Image] = {}
    for material in materials:
        if not material.use_nodes or material.node_tree is None:
            continue
        for node in material.node_tree.nodes:
            if node.type == "TEX_IMAGE" and node.image is not None:
                images[node.image.as_pointer()] = node.image
    return sorted(images.values(), key=lambda image: image.name.casefold())


def pack_referenced_mesh_images() -> list[PackedImageRecord]:
    """Pack existing external images used by mesh materials and reject missing files."""
    records: list[PackedImageRecord] = []
    for image in referenced_mesh_images():
        if image.packed_file is not None:
            packed_size = len(image.packed_file.data)
            records.append({
                "image_name": image.name,
                "source_path": image.filepath,
                "bytes": packed_size,
            })
            continue
        if image.source != "FILE":
            raise RuntimeError(
                f"Mesh material image {image.name!r} uses unsupported external image source {image.source!r}; "
                "the preview project cannot guarantee a self-contained image dependency."
            )
        resolved_path = Path(bpy.path.abspath(image.filepath)).resolve()
        if not resolved_path.is_file():
            raise FileNotFoundError(
                f"Mesh material image {image.name!r} is referenced but its source file is missing: "
                f"{image.filepath!r} resolved to {str(resolved_path)!r}."
            )
        try:
            image.pack()
        except RuntimeError as error:
            raise RuntimeError(
                f"Could not pack mesh material image {image.name!r} from {resolved_path}: {error}"
            ) from error
        if image.packed_file is None:
            raise RuntimeError(f"Blender did not retain packed image data for {image.name!r}.")
        records.append({
            "image_name": image.name,
            "source_path": str(resolved_path),
            "bytes": resolved_path.stat().st_size,
        })
    return records


def verify_saved_image_dependencies(expected_images: Sequence[PackedImageRecord]) -> None:
    """Confirm the saved blend reopened with all mesh-material images packed."""
    actual_images = referenced_mesh_images()
    actual_names = {image.name for image in actual_images}
    expected_names = {record["image_name"] for record in expected_images}
    if actual_names != expected_names:
        raise RuntimeError(
            f"Saved blend mesh-material image set changed: expected {sorted(expected_names)}, "
            f"found {sorted(actual_names)}."
        )
    unpacked = sorted(image.name for image in actual_images if image.packed_file is None)
    if unpacked:
        raise RuntimeError(f"Saved blend contains un-packed mesh-material image dependencies: {unpacked}.")


def verify_saved_renderer_visibility(decisions: Sequence[RendererDecision]) -> None:
    """Confirm source mesh objects and visibility flags survived reopening the project."""
    missing_objects: list[str] = []
    mismatched_flags: list[str] = []
    for decision in decisions:
        obj = bpy.data.objects.get(decision["fbx_object_name"])
        if obj is None or obj.type != "MESH":
            missing_objects.append(decision["fbx_hierarchy_path"])
            continue
        expected_hidden = not decision["visible"]
        if obj.hide_render != expected_hidden or obj.hide_viewport != expected_hidden or obj.hide_get() != expected_hidden:
            mismatched_flags.append(decision["fbx_hierarchy_path"])
    if missing_objects:
        raise RuntimeError(f"Saved blend is missing renderer mesh objects: {missing_objects}.")
    if mismatched_flags:
        raise RuntimeError(f"Saved blend renderer visibility flags differ from source decisions: {mismatched_flags}.")


def animation_data_blockers(owner: bpy.types.ID, label: str) -> list[str]:
    """Return active animation and driver sources attached to one Blender ID."""
    animation_data = getattr(owner, "animation_data", None)
    if animation_data is None:
        return []
    blockers: list[str] = []
    if animation_data.action is not None:
        blockers.append(f"{label} action {animation_data.action.name!r}")
    if len(animation_data.nla_tracks) > 0:
        blockers.append(f"{label} has {len(animation_data.nla_tracks)} NLA tracks")
    if len(animation_data.drivers) > 0:
        blockers.append(f"{label} has {len(animation_data.drivers)} drivers")
    return blockers


def require_static_pose_scene(scene: bpy.types.Scene) -> None:
    """Reject scene animation or constraints before resetting imported bind pose offsets."""
    blockers = animation_data_blockers(scene, "Scene")
    for obj in scene.objects:
        if len(obj.constraints) > 0:
            blockers.append(f"object {obj.name!r} has {len(obj.constraints)} constraints")
        blockers.extend(animation_data_blockers(obj, f"object {obj.name!r}"))
        data_block = obj.data
        if data_block is not None:
            blockers.extend(animation_data_blockers(data_block, f"data {data_block.name!r}"))
            shape_keys = getattr(data_block, "shape_keys", None)
            if shape_keys is not None:
                blockers.extend(animation_data_blockers(shape_keys, f"shape keys {shape_keys.name!r}"))
        if obj.type == "ARMATURE":
            for pose_bone in obj.pose.bones:
                if len(pose_bone.constraints) > 0:
                    blockers.append(f"pose bone {obj.name!r}/{pose_bone.name!r} has {len(pose_bone.constraints)} constraints")
    if blockers:
        raise RuntimeError(
            "Static bind-pose preview refuses to clear pose transforms while animation, drivers, or constraints are present: "
            f"{sorted(blockers)}. Export a static source without these live rig controls or preview it through an animation-aware path."
        )


def matrix_rows(matrix: Matrix) -> list[list[float]]:
    """Convert a Blender matrix to finite JSON-compatible rows."""
    rows = [[float(matrix[row][column]) for column in range(4)] for row in range(4)]
    if any(not math.isfinite(value) for row in rows for value in row):
        raise RuntimeError("Imported armature pose contains non-finite matrix_basis values.")
    return rows


def evaluated_world_coordinates(mesh_objects: Sequence[bpy.types.Object]) -> dict[str, array]:
    """Capture evaluated world-space mesh vertices in compact double arrays."""
    dependency_graph = bpy.context.evaluated_depsgraph_get()
    coordinates: dict[str, array] = {}
    for mesh_object in mesh_objects:
        if mesh_object.type != "MESH":
            raise TypeError(f"Visible preview object {mesh_object.name!r} is not a mesh.")
        if mesh_object.name in coordinates:
            raise RuntimeError(f"Visible preview mesh object name is duplicated: {mesh_object.name!r}.")
        evaluated_object = mesh_object.evaluated_get(dependency_graph)
        evaluated_mesh = evaluated_object.to_mesh()
        flattened = array("d")
        try:
            world_matrix = evaluated_object.matrix_world.copy()
            for vertex in evaluated_mesh.vertices:
                point = world_matrix @ vertex.co
                if not all(math.isfinite(float(component)) for component in point):
                    raise RuntimeError(
                        f"Visible preview mesh {mesh_object.name!r} contains non-finite evaluated coordinates."
                    )
                flattened.extend((float(point.x), float(point.y), float(point.z)))
        finally:
            evaluated_object.to_mesh_clear()
        if len(flattened) == 0:
            raise RuntimeError(f"Visible preview mesh {mesh_object.name!r} produced no evaluated vertices.")
        coordinates[mesh_object.name] = flattened
    if not coordinates:
        raise RuntimeError("No visible meshes were supplied for the static bind-pose displacement audit.")
    return coordinates


def compare_mesh_world_coordinates(
    before: Mapping[str, array],
    after: Mapping[str, array],
    moved_vertex_threshold: float,
) -> list[MeshPoseDeltaRecord]:
    """Summarize actual per-vertex world displacement between two evaluated poses."""
    if set(before) != set(after):
        raise RuntimeError(
            f"Visible mesh set changed during static bind-pose initialization: "
            f"before={sorted(before)}, after={sorted(after)}."
        )
    records: list[MeshPoseDeltaRecord] = []
    for mesh_name in sorted(before):
        prior = before[mesh_name]
        current = after[mesh_name]
        if len(prior) != len(current) or len(prior) % 3 != 0:
            raise RuntimeError(
                f"Visible mesh {mesh_name!r} changed vertex coordinate count during static bind-pose initialization: "
                f"before={len(prior)}, after={len(current)}."
            )
        vertex_count = len(prior) // 3
        moved_vertex_count = 0
        maximum_displacement = 0.0
        displacement_sum = 0.0
        for offset in range(0, len(prior), 3):
            dx = current[offset] - prior[offset]
            dy = current[offset + 1] - prior[offset + 1]
            dz = current[offset + 2] - prior[offset + 2]
            displacement = math.sqrt(dx * dx + dy * dy + dz * dz)
            maximum_displacement = max(maximum_displacement, displacement)
            displacement_sum += displacement
            if displacement > moved_vertex_threshold:
                moved_vertex_count += 1
        records.append({
            "mesh_object_name": mesh_name,
            "vertex_count": vertex_count,
            "moved_vertex_count": moved_vertex_count,
            "maximum_world_displacement": maximum_displacement,
            "mean_world_displacement": displacement_sum / vertex_count,
        })
    return records


def reset_static_bind_pose(
    scene: bpy.types.Scene,
    source_blend: Path,
    visible_meshes: Sequence[bpy.types.Object],
) -> StaticBindPoseAudit:
    """Reset only imported pose offsets while preserving the editable armature in POSE mode."""
    require_static_pose_scene(scene)
    imported_pose_coordinates = evaluated_world_coordinates(visible_meshes)
    armatures = sorted((obj for obj in scene.objects if obj.type == "ARMATURE"), key=lambda item: item.name)
    records: list[ArmaturePoseRecord] = []
    for armature in armatures:
        original_basis: list[PoseBoneBasisRecord] = []
        non_identity_basis_count = 0
        for pose_bone in sorted(armature.pose.bones, key=lambda item: item.name):
            basis = pose_bone.matrix_basis.copy()
            delta = max(
                abs(float(basis[row][column]) - (1.0 if row == column else 0.0))
                for row in range(4)
                for column in range(4)
            )
            if delta > 1e-6:
                non_identity_basis_count += 1
            original_basis.append({
                "bone_name": pose_bone.name,
                "matrix_basis_before": matrix_rows(basis),
            })

        pose_position_before = armature.data.pose_position
        armature.data.pose_position = "POSE"
        for pose_bone in armature.pose.bones:
            pose_bone.matrix_basis = Matrix.Identity(4)

        maximum_reset_identity_delta = 0.0
        for pose_bone in armature.pose.bones:
            basis = pose_bone.matrix_basis
            maximum_reset_identity_delta = max(
                maximum_reset_identity_delta,
                max(
                    abs(float(basis[row][column]) - (1.0 if row == column else 0.0))
                    for row in range(4)
                    for column in range(4)
                ),
            )
        if maximum_reset_identity_delta > 1e-6:
            raise RuntimeError(
                f"Could not initialize armature {armature.name!r} to static bind pose; "
                f"maximum matrix_basis identity delta={maximum_reset_identity_delta:.9g}."
            )
        records.append({
            "armature_name": armature.name,
            "pose_position_before": pose_position_before,
            "pose_position_after": armature.data.pose_position,
            "bone_count": len(armature.pose.bones),
            "non_identity_basis_count": non_identity_basis_count,
            "maximum_reset_identity_delta": maximum_reset_identity_delta,
            "original_pose_basis": original_basis,
        })
    bpy.context.view_layer.update()
    reset_pose_coordinates = evaluated_world_coordinates(visible_meshes)
    return {
        "format": "EndfieldCharacterStaticBindPoseAudit/1",
        "source_blend": str(source_blend),
        "initialization": "Set imported armature pose-bone matrix_basis to identity in POSE mode; original values are preserved below.",
        "armatures": records,
        "visible_mesh_pose_deltas": compare_mesh_world_coordinates(
            imported_pose_coordinates,
            reset_pose_coordinates,
            1e-6,
        ),
    }


def verify_saved_static_bind_pose(audit: StaticBindPoseAudit) -> None:
    """Confirm the saved preview retains POSE mode with identity basis and editable bones."""
    mismatches: list[str] = []
    for record in audit["armatures"]:
        armature = bpy.data.objects.get(record["armature_name"])
        if armature is None or armature.type != "ARMATURE":
            mismatches.append(f"missing armature {record['armature_name']!r}")
            continue
        if armature.data.pose_position != "POSE":
            mismatches.append(f"armature {armature.name!r} has pose_position={armature.data.pose_position!r}")
        if len(armature.pose.bones) != record["bone_count"]:
            mismatches.append(
                f"armature {armature.name!r} bone count changed from {record['bone_count']} to {len(armature.pose.bones)}"
            )
            continue
        for pose_bone in armature.pose.bones:
            basis = pose_bone.matrix_basis
            delta = max(
                abs(float(basis[row][column]) - (1.0 if row == column else 0.0))
                for row in range(4)
                for column in range(4)
            )
            if delta > 1e-6:
                mismatches.append(f"armature {armature.name!r} bone {pose_bone.name!r} has non-identity basis delta {delta:.9g}")
    if mismatches:
        raise RuntimeError(f"Saved preview did not preserve static bind-pose initialization: {mismatches}.")


def write_selection_report(path: Path, report: PreviewReport) -> None:
    """Write a strict UTF-8 report after render output is verified."""
    path.write_text(json.dumps(report, ensure_ascii=False, indent=2, allow_nan=False) + "\n", encoding="utf-8")


def write_static_bind_pose_audit(path: Path, audit: StaticBindPoseAudit) -> None:
    """Write original imported pose bases and the applied static bind-pose reset."""
    path.write_text(json.dumps(audit, ensure_ascii=False, indent=2, allow_nan=False) + "\n", encoding="utf-8")


def run_preview(arguments: CliArguments) -> PreviewReport:
    """Open the reviewed blend, match source LOD renderers, and render one preview."""
    require_blender_version()
    blend_path = resolve_input_path(arguments["blend"], "--blend")
    audit_path = resolve_input_path(arguments["source_audit"], "--source-audit")
    export_map_input = arguments["renderer_export_map"]
    export_map_path = resolve_input_path(export_map_input, "--renderer-export-map") if export_map_input is not None else None
    lod_path = resolve_input_path(arguments["lod_sources"], "--lod-sources")
    visibility_path = resolve_input_path(arguments["renderer_visibility"], "--renderer-visibility")
    output_dir = arguments["output_dir"].expanduser().resolve()
    output_dir.mkdir(parents=True, exist_ok=True)
    output_stem = arguments["output_stem"] or blend_path.stem
    if not output_stem or any(character not in "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789._-" for character in output_stem):
        raise ValueError(f"--output-stem contains unsupported characters: {output_stem!r}.")
    output_blend = output_dir / f"{output_stem}_review.blend"
    output_png = output_dir / f"{output_stem}_preview.png"
    report_path = output_dir / f"{output_stem}_selection_report.json"
    pose_audit_path = output_dir / f"{output_stem}_static_bind_pose_audit.json"
    existing_outputs = [str(path) for path in (output_blend, output_png, report_path, pose_audit_path) if path.exists()]
    if existing_outputs:
        raise FileExistsError(f"Refusing to overwrite preview outputs: {existing_outputs}.")

    source_audit = read_json_object(audit_path)
    if require_string(source_audit.get("format"), "source_audit.format") != "EndfieldCharacterSourceAudit/1":
        raise ValueError(f"Unsupported source audit format: {source_audit.get('format')!r}.")
    visibility_records = read_renderer_visibility(visibility_path, source_audit)
    lod_group_document = select_root_lod_document(read_json_value(lod_path), source_audit)
    lod_group_identity, lod_source_id, lod_group_enabled, lod_levels, membership = source_lod_membership(lod_group_document)
    renderer_rows = source_renderer_rows(source_audit, lod_source_id, membership)
    shadow_only_renderer_count = verify_renderer_visibility_coverage(visibility_records, renderer_rows)
    renderer_export_names = read_renderer_export_names(export_map_path, source_audit, renderer_rows) if export_map_path is not None else None

    try:
        open_result = bpy.ops.wm.open_mainfile(filepath=str(blend_path))
    except RuntimeError as error:
        raise RuntimeError(f"Could not open audited source blend {blend_path}: {error}") from error
    if "FINISHED" not in open_result:
        raise RuntimeError(f"Blender did not finish opening {blend_path}; operator result={sorted(open_result)}.")

    matches = map_renderers_to_fbx_objects(renderer_rows, visibility_records, renderer_export_names)
    decisions, visible_meshes = set_mesh_visibility(matches, source_hierarchy_activity(source_audit))
    static_bind_pose_audit = reset_static_bind_pose(bpy.context.scene, blend_path, visible_meshes)
    preview_only_objects, bounds = configure_preview(visible_meshes, output_png)
    packed_images = pack_referenced_mesh_images()
    save_result = bpy.ops.wm.save_as_mainfile(filepath=str(output_blend))
    if "FINISHED" not in save_result or not output_blend.is_file() or output_blend.stat().st_size <= 0:
        raise RuntimeError(f"Blender did not create a non-empty preview project at {output_blend}; result={sorted(save_result)}.")

    reopen_result = bpy.ops.wm.open_mainfile(filepath=str(output_blend))
    if "FINISHED" not in reopen_result:
        raise RuntimeError(f"Blender did not reopen the saved preview project {output_blend}; result={sorted(reopen_result)}.")
    verify_saved_image_dependencies(packed_images)
    verify_saved_renderer_visibility(decisions)
    verify_saved_static_bind_pose(static_bind_pose_audit)

    render_result = bpy.ops.render.render(write_still=True)
    if "FINISHED" not in render_result or not output_png.is_file() or output_png.stat().st_size <= 0:
        raise RuntimeError(f"Blender did not create a non-empty preview image at {output_png}; result={sorted(render_result)}.")

    write_static_bind_pose_audit(pose_audit_path, static_bind_pose_audit)
    visible_count = sum(1 for decision in decisions if decision["visible"])
    hidden_count = len(decisions) - visible_count
    report: PreviewReport = {
        "format": "CharacterLodPreviewSelection/2",
        "source_blend": str(blend_path),
        "source_audit": str(audit_path),
        "renderer_export_map": str(export_map_path) if export_map_path is not None else None,
        "lod_sources": str(lod_path),
        "renderer_visibility": str(visibility_path),
        "source_root": require_string(require_object(source_audit.get("actual_root"), "source_audit.actual_root").get("name"), "source_audit.actual_root.name"),
        "static_bind_pose_audit": str(pose_audit_path),
        "lod_group_identity": lod_group_identity,
        "lod_group_enabled": lod_group_enabled,
        "lod_levels": lod_levels,
        "renderer_decisions": decisions,
        "summary": {
            "source_renderer_count": len(decisions),
            "visible_renderer_count": visible_count,
            "hidden_renderer_count": hidden_count,
            "matched_fbx_mesh_count": len(matches),
            "native_shadows_only_renderer_count": shadow_only_renderer_count,
            "preview_only_object_count": len(preview_only_objects),
            "packed_image_count": len(packed_images),
        },
        "bounds": {
            "minimum": {"x": float(bounds[0].x), "y": float(bounds[0].y), "z": float(bounds[0].z)},
            "maximum": {"x": float(bounds[1].x), "y": float(bounds[1].y), "z": float(bounds[1].z)},
        },
        "packed_images": packed_images,
        "preview_only_objects": preview_only_objects,
        "render_engine": bpy.context.scene.render.engine,
        "render_style": "Workbench neutral object-color geometry; source material fidelity is not represented.",
        "output_blend": str(output_blend),
        "output_png": str(output_png),
        "output_png_bytes": output_png.stat().st_size,
        "visual_inspection_performed": False,
        "notes": [
            "LOD selection follows native renderer identity and native GameObject Transform path, then requires a unique mesh-object name match under the audited FBX root; intermediate hierarchy segments are not compared.",
            "Renderer visibility uses an exact source/pathID match across all native renderer rows; embedded TypeTree fields must agree with the dedicated reader.",
            "Native CastShadows value 3 is treated as shadows-only and hidden from color geometry; no object or mesh datablock is removed.",
            "Other non-LOD renderers remain visible only when source GameObject and renderer flags are both enabled and the renderer is not shadows-only.",
            "Camera, lights, ground, and neutral object colors exist only in this preview project; source material datablocks were not edited.",
            "Images referenced by mesh-assigned material nodes were packed into the saved blend and verified after reopening.",
            "Static bind-pose initialization sets imported pose-bone matrix_basis values to identity in POSE mode; the original values are preserved in the linked audit, and the source blend is not modified.",
            "The PNG is a geometry preview and does not verify material, texture, animation, or in-game appearance.",
        ],
    }
    write_selection_report(report_path, report)
    print(json.dumps({
        "status": "preview_created",
        "visible_renderer_count": visible_count,
        "hidden_renderer_count": hidden_count,
        "blend": str(output_blend),
        "png": str(output_png),
        "selection_report": str(report_path),
    }, ensure_ascii=False, allow_nan=False))
    return report


def main(argv: Sequence[str]) -> None:
    """Run the parameterized character preview operation."""
    run_preview(parse_arguments(argv))


if __name__ == "__main__":
    main(sys.argv)
