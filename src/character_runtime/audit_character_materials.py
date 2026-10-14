"""Reopen and validate source-bound character material research without game writes."""
from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
import struct
import sys
from typing import Callable, Sequence

sys.dont_write_bytecode = True
import bpy


def _geometry_digest(object_names: Sequence[str], transform_bytes: Callable[[bpy.types.Object], bytes], shape_blocks: Callable[[bpy.types.Object], Sequence[bpy.types.ShapeKey]]) -> dict[str, str]:
    result: dict[str, str] = {}
    for name in object_names:
        obj = bpy.data.objects[name]
        digest = hashlib.sha256()
        digest.update(transform_bytes(obj))
        for vertex in obj.data.vertices:
            digest.update(struct.pack("<3f", *vertex.co))
            for weight in vertex.groups:
                digest.update(struct.pack("<If", weight.group, weight.weight))
        for polygon in obj.data.polygons:
            digest.update(struct.pack("<I", len(polygon.vertices)))
            for index in polygon.vertices:
                digest.update(struct.pack("<I", index))
        for group in obj.vertex_groups:
            digest.update(group.name.encode("utf-8") + b"\0")
        for layer in obj.data.uv_layers:
            digest.update(layer.name.encode("utf-8") + b"\0")
            for loop in layer.data:
                digest.update(struct.pack("<2f", *loop.uv))
        for block in shape_blocks(obj):
            digest.update(block.name.encode("utf-8") + b"\0")
            for vertex in block.data:
                digest.update(struct.pack("<3f", *vertex.co))
        for modifier in obj.modifiers:
            digest.update((modifier.name + "|" + modifier.type).encode("utf-8") + b"\0")
            if modifier.type == "ARMATURE":
                if modifier.object is None:
                    raise ValueError(f"Armature modifier target missing: {name}")
                digest.update(modifier.object.name.encode("utf-8") + b"\0")
                for bone in modifier.object.data.bones:
                    digest.update(bone.name.encode("utf-8") + b"\0")
                    for row in bone.matrix_local:
                        digest.update(struct.pack("<4f", *row))
        digest.update(bytes((obj.hide_render, obj.hide_viewport, obj.hide_get())))
        result[name] = digest.hexdigest()
    return result


def _world_transform_bytes(obj: bpy.types.Object) -> bytes:
    """Preserve the original evaluated-world digest semantics for research reviews."""
    return b"".join(struct.pack("<4f", *row) for row in obj.matrix_world)


def _source_transform_bytes(obj: bpy.types.Object) -> bytes:
    """Hash source local transforms and parent bindings without dependency-graph caches."""
    chunks: list[bytes] = []
    visited: set[str] = set()
    current: bpy.types.Object | None = obj
    while current is not None:
        if current.name in visited:
            raise ValueError(f"Cyclic source object parenting: {obj.name}/{current.name}")
        visited.add(current.name)
        for value in (current.name, current.parent_type, current.parent_bone):
            chunks.append(value.encode("utf-8") + b"\0")
        for matrix in (current.matrix_basis, current.matrix_parent_inverse):
            chunks.extend(struct.pack("<4f", *row) for row in matrix)
        current = current.parent
    return b"".join(chunks)


def geometry_digest(object_names: Sequence[str]) -> dict[str, str]:
    """Hash evaluated world transforms with the original geometry/skin invariants."""
    return _geometry_digest(object_names, _world_transform_bytes, _all_shape_blocks)


def source_geometry_digest(object_names: Sequence[str]) -> dict[str, str]:
    """Hash source local/parent transforms and geometry independently of hidden world caches."""
    return _geometry_digest(object_names, _source_transform_bytes, _all_shape_blocks)


def _all_shape_blocks(obj: bpy.types.Object) -> Sequence[bpy.types.ShapeKey]:
    return tuple(obj.data.shape_keys.key_blocks) if obj.data.shape_keys is not None else ()


def _no_shape_blocks(obj: bpy.types.Object) -> Sequence[bpy.types.ShapeKey]:
    return ()


def source_geometry_without_shapes(object_names: Sequence[str]) -> dict[str, str]:
    """Check immutable geometry and skin while intentional new shape keys are audited separately."""
    return _geometry_digest(object_names, _source_transform_bytes, _no_shape_blocks)


def main(argv: Sequence[str]) -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    for name in ("baseline-blend", "blend", "report", "output"):
        parser.add_argument("--" + name, required=True, type=Path)
    args = parser.parse_args(list(argv))
    if args.output.exists():
        raise FileExistsError(f"Audit output exists: {args.output}")
    report = json.loads(args.report.read_text(encoding="utf-8"))
    if report["format"] != "EndfieldCharacterMaterialRebuild/1":
        raise ValueError("Unsupported rebuild report")
    object_names = tuple(row["object"] for row in report["renderers"])
    bpy.ops.wm.open_mainfile(filepath=str(args.baseline_blend.resolve()))
    before = geometry_digest(object_names)
    bpy.ops.wm.open_mainfile(filepath=str(args.blend.resolve()))
    after = geometry_digest(object_names)
    if before != after:
        raise ValueError("Rebuilt blend changed source coordinates, topology, UV, skin weights, shape keys, armature bind matrices, or visibility")
    groups = tuple(group for group in bpy.data.node_groups if "source_shader" in group)
    if len(groups) != report["source_materials"]:
        raise ValueError("Reopened blend lost source material groups")
    actual_shaders = sorted({group["source_shader"] for group in groups})
    if actual_shaders != report["actual_shader_families"]:
        raise ValueError("Reopened shader-family selection differs from source")
    archived = tuple(group for group in groups if group.get("archived_unsupported_shader"))
    expected_archived = {tuple(row["identity"]) for row in report["material_support"]["inactive_unsupported"]} if "material_support" in report else set()
    if {tuple(json.loads(group["source_identity"])) for group in archived} != expected_archived:
        raise ValueError("Reopened inactive unsupported shader archive differs from source report")
    visible_materials = {tuple(ref) for row in report["renderers"] if row["visible"] for ref in row["source_materials"]}
    if visible_materials & expected_archived:
        raise ValueError("Unsupported shader archive is referenced by a visible renderer")
    for group in archived:
        outputs = tuple(item for item in group.nodes if item.type == "GROUP_OUTPUT")
        if len(outputs) != 1 or outputs[0].inputs["Coverage"].is_linked or outputs[0].inputs["Coverage"].default_value != 0.0:
            raise ValueError(f"Archive shader coverage is not zero: {group.name}")
        links = tuple(outputs[0].inputs["Shader"].links)
        if len(links) != 1 or links[0].from_node.type != "BSDF_TRANSPARENT":
            raise ValueError(f"Archive shader is not a transparent evidence carrier: {group.name}")
    images = {node.image for group in groups for node in group.nodes if node.type == "TEX_IMAGE"}
    if None in images or len(images) != report["packed_image_roles"] or any(image.packed_file is None for image in images):
        raise ValueError("Reopened blend has missing or unpacked source images")
    environment = bpy.data.node_groups["EF Character Environment - APPROXIMATION"]
    wet = next(node for node in environment.nodes if node.type == "VALUE" and node.label == "Wetness (editable)")
    if wet.outputs[0].default_value != 0.0:
        raise ValueError("Saved blend does not start with dry preview controls")
    extra_controls: list[dict[str, object]] = []
    for material in bpy.data.materials:
        if "source_material_stack" not in material:
            continue
        source_refs = json.loads(material["source_material_stack"])
        group_nodes = tuple(node for node in material.node_tree.nodes if node.type == "GROUP")
        if len(group_nodes) != len(source_refs) or {tuple(json.loads(node.node_tree["source_identity"])) for node in group_nodes} != {tuple(ref) for ref in source_refs}:
            raise ValueError(f"Source pass group references differ from the recorded stack: {material.name}")
        for group in group_nodes:
            if not any(output.is_linked for output in group.outputs):
                raise ValueError(f"Source pass is not connected: {material.name}, {group.node_tree.name}")
        for node in material.node_tree.nodes:
            if node.type == "VALUE" and node.label.startswith("Approx Extra Pass"):
                if not node.outputs[0].is_linked or node.outputs[0].default_value != 1.0:
                    raise ValueError(f"Additional source pass control is inactive or disconnected: {material.name}")
                extra_controls.append({"material": material.name, "node": node.name, "label": node.label, "default_value": 1.0})
    if not extra_controls and report["extra_material_passes_connected"] != 0:
        raise ValueError("No editable extra-pass controls survived reopening")
    text_counts = {prefix: sum(text.name.startswith(prefix) for text in bpy.data.texts) for prefix in ("SOURCE MATERIAL ", "SOURCE TYPETREE ", "SOURCE RENDERER ")}
    if text_counts["SOURCE MATERIAL "] != report["source_materials"] or text_counts["SOURCE TYPETREE "] != report["source_materials"] or text_counts["SOURCE RENDERER "] != len(object_names):
        raise ValueError("Full source parameter or renderer evidence did not survive reopening")
    result = {"format": "EndfieldCharacterMaterialReopenAudit/1", "status": "passed", "game_material_fidelity_complete": False, "source_geometry_and_skin_digest_equal": True, "audited_renderer_count": len(object_names), "source_material_groups": len(groups), "actual_shader_families": actual_shaders, "packed_source_images": len(images), "saved_wetness": 0.0, "extra_controls": extra_controls, "retained_source_text_counts": text_counts, "geometry_digests": after, "pipeline_limitations": report["pipeline_support"]["preserved_not_integrated"]}
    result["archived_unsupported_shader_groups"] = [group.name for group in archived]
    args.output.write_text(json.dumps(result, indent=2), encoding="utf-8")
    print(json.dumps({"status": "reopen_audit_passed", "renderers": len(object_names), "material_groups": len(groups), "packed_images": len(images), "extra_controls": len(extra_controls)}))


if __name__ == "__main__":
    main(sys.argv[sys.argv.index("--") + 1:])
