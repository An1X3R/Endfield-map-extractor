"""Build NPC materials from actual AvatarMesh configuration and source identities.

Configuration mesh instances are never represented as Unity Renderer components.
Unknown runtime pipeline fields stay unknown; disabled unresolved material slots
are preserved as non-rendering archives rather than guessed shader surfaces.
"""
from __future__ import annotations

import argparse
from dataclasses import dataclass
import hashlib
import json
from pathlib import Path
import re
import sys
from types import MappingProxyType
from typing import Callable, Mapping, Sequence, TYPE_CHECKING

if TYPE_CHECKING:
    from rebuild_character_materials import MaterialSource

sys.dont_write_bytecode = True
import bpy
from mathutils import Vector


@dataclass(frozen=True)
class NpcMaterialBinding:
    identity: tuple[str, int] | None
    export_name: str
    source_json: str


@dataclass(frozen=True)
class NpcMeshInstance:
    instance_id: str
    export_name: str
    mesh_identity: tuple[str, int]
    mesh_sha256: str
    slot_index: int
    lod_index: int
    renderer_index: int
    submesh_count: int
    configured_disabled: bool
    bindings: tuple[NpcMaterialBinding, ...]
    source_json: str


def require_object(value: object, context: str) -> dict[str, object]:
    if not isinstance(value, dict) or any(not isinstance(key, str) for key in value):
        raise TypeError(f"Expected source object: {context}.")
    return value


def require_array(value: object, context: str) -> list[object]:
    if not isinstance(value, list):
        raise TypeError(f"Expected source array: {context}.")
    return value


def require_string(value: object, context: str) -> str:
    if not isinstance(value, str) or not value:
        raise TypeError(f"Expected nonempty source string: {context}.")
    return value


def require_integer(value: object, context: str) -> int:
    if type(value) is not int:
        raise TypeError(f"Expected source integer: {context}.")
    return value


def require_export_name(value: object, context: str) -> str:
    """Require an exact Blender 4.4 identity name without ID-name truncation."""
    name = require_string(value, context)
    if "\0" in name or len(name.encode("utf-8")) > 63:
        raise ValueError(f"Export identity name exceeds Blender 4.4 ID bounds: {context}.")
    return name


def source_identity(value: object, context: str) -> tuple[str, int]:
    row = require_object(value, context)
    raw_id = row["path_id"]
    if isinstance(raw_id, str) and re.fullmatch(r"-?[0-9]+", raw_id):
        path_id = int(raw_id)
    else:
        path_id = require_integer(raw_id, context + ".path_id")
    if path_id == 0:
        raise ValueError(f"Source identity is null: {context}.")
    return require_string(row["source"], context + ".source").casefold(), path_id


def read_instances(assembly: Mapping[str, object]) -> tuple[NpcMeshInstance, ...]:
    """Validate independent configuration identities without creating components."""
    if assembly["format"] != "EndfieldNpcAssembly/1":
        raise ValueError("Unsupported NPC assembly evidence format.")
    configuration = require_object(assembly["avatar_mesh_configuration"], "avatar_mesh_configuration")
    configuration_key = source_identity(configuration["object"], "avatar_mesh_configuration.object")
    instances: list[NpcMeshInstance] = []
    keys: set[tuple[int, int, int]] = set()
    names: set[str] = set()
    for raw in require_array(assembly["renderers"], "assembly.renderers"):
        row = require_object(raw, "config mesh instance")
        if row["source_identity_kind"] != "npc_config_mesh_instance":
            raise ValueError("NPC instance does not declare its configuration identity kind.")
        if row["source_renderer_enabled"] is not None or row["cast_shadows"] is not None:
            raise ValueError("Configuration assembly must not invent runtime Renderer fields.")
        key = tuple(require_integer(row[name], name) for name in ("slot_index", "lod_index", "renderer_index"))
        if min(key) < 0 or key in keys:
            raise ValueError(f"Invalid or duplicate NPC configuration instance: {key}.")
        keys.add(key)
        name = require_export_name(row["export_name"], "export_name")
        if name in names:
            raise ValueError(f"Duplicate exported NPC mesh name: {name}.")
        names.add(name)
        disabled = row["configured_renderer_disabled"]
        if type(disabled) is not bool:
            raise TypeError(f"Source configuration renderer-disable field unavailable: {name}.")
        mesh = require_object(row["mesh"], "mesh")
        mesh_key = source_identity(mesh, "mesh")
        if mesh["type"] != "Mesh":
            raise ValueError(f"Configuration target is not an actual Mesh: {name}.")
        mesh_hash = require_string(mesh["raw_data_sha256"], "mesh.raw_data_sha256").casefold()
        if not re.fullmatch(r"[0-9a-f]{64}", mesh_hash):
            raise ValueError(f"Invalid source Mesh SHA256: {name}.")
        count = require_integer(row["submesh_count"], "submesh_count")
        if count < 1:
            raise ValueError(f"Source Mesh has no submesh: {name}.")
        bindings: list[NpcMaterialBinding] = []
        for item in require_array(row["material_bindings"], "material_bindings"):
            binding = require_object(item, "material binding")
            status = require_string(binding["status"], "material binding.status")
            identity: tuple[str, int] | None = None
            if status == "resolved":
                if require_object(binding["object"], "material binding.object")["type"] != "Material":
                    raise ValueError(f"Configuration material target is not an actual Material: {name}.")
                identity = source_identity(binding["object"], "material binding.object")
            elif status == "unresolved":
                if not disabled or binding["object"] is not None or binding["configured_disabled"] is not True:
                    raise ValueError(f"Unresolved material is not an explicitly disabled configuration archive: {name}.")
            else:
                raise ValueError(f"Unsupported source material binding status: {status}, mesh={name}.")
            bindings.append(NpcMaterialBinding(identity, require_export_name(binding["fbx_material_name"], "fbx_material_name"), json.dumps(binding, sort_keys=True)))
        if len(bindings) < count:
            raise ValueError(f"NPC material bindings are fewer than source submeshes: {name}.")
        instance_id = f"npc_config:{configuration_key[0]}:{configuration_key[1]}:{key[0]}:{key[1]}:{key[2]}"
        instances.append(NpcMeshInstance(instance_id, name, mesh_key, mesh_hash, key[0], key[1], key[2], count, disabled, tuple(bindings), json.dumps(row, sort_keys=True)))
    if not instances:
        raise ValueError("NPC assembly has no configuration mesh instances.")
    return tuple(instances)


def archive_unresolved(instance: NpcMeshInstance, index: int) -> bpy.types.Material:
    """Retain disabled unresolved evidence through a transparent archive carrier."""
    if not instance.configured_disabled:
        raise ValueError(f"Unresolved source archive cannot be visible: {instance.instance_id}.")
    material = bpy.data.materials.new(f"NPC Archived Unresolved {instance.export_name} slot {index}")
    material.use_nodes = True
    material["archived_unresolved_source"] = True
    material["source_config_mesh_instance"] = instance.instance_id
    material["scope"] = "Disabled source material unresolved; non-rendering evidence archive, not shader restoration."
    tree = material.node_tree
    tree.nodes.clear()
    output = tree.nodes.new("ShaderNodeOutputMaterial")
    output.label = "Disabled unresolved archive output"
    transparent = tree.nodes.new("ShaderNodeBsdfTransparent")
    transparent.label = "Non-rendering archive carrier"
    tree.links.new(transparent.outputs[0], output.inputs["Surface"])
    return material


def refresh_source_transforms(objects: Sequence[bpy.types.Object]) -> None:
    """Evaluate hidden source transforms after pose initialization without changing visibility."""
    states = tuple((obj, obj.hide_viewport, obj.hide_get(), obj.matrix_basis.copy(), obj.parent, obj.matrix_parent_inverse.copy()) for obj in objects)
    try:
        for obj, _, _, _, _, _ in states:
            obj.hide_viewport = False
            obj.hide_set(False)
        bpy.context.view_layer.update()
    finally:
        for obj, hidden_viewport, hidden_layer, _, _, _ in states:
            obj.hide_viewport = hidden_viewport
            obj.hide_set(hidden_layer)
        bpy.context.view_layer.update()
    for obj, _, _, basis, parent, inverse in states:
        if obj.matrix_basis != basis or obj.parent != parent or obj.matrix_parent_inverse != inverse:
            raise AssertionError(f"Hidden source transform evaluation changed source local placement: {obj.name}.")


def bind_instances(instances: Sequence[NpcMeshInstance], selected_lod: int, sources: Mapping[tuple[str, int], MaterialSource], groups: Mapping[tuple[str, int], bpy.types.ShaderNodeTree], layout: Callable[[bpy.types.NodeTree], None]) -> tuple[bpy.types.Object, ...]:
    from rebuild_character_materials import surface_stack
    objects: list[bpy.types.Object] = []
    stacks: dict[tuple[tuple[str, int], ...], bpy.types.Material] = {}
    for instance in instances:
        obj = bpy.data.objects.get(instance.export_name)
        if obj is None or obj.type != "MESH":
            raise ValueError(f"Exact exported configuration Mesh missing: {instance.export_name}.")
        if len(obj.material_slots) != instance.submesh_count:
            raise ValueError(f"NPC source/FBX submesh slot mismatch: {instance.export_name}.")
        if not obj.data.uv_layers or obj.data.uv_layers[0].name != "UV0":
            raise ValueError(f"NPC imported Mesh requires verified UV0: {instance.export_name}.")
        for index in range(instance.submesh_count):
            if obj.material_slots[index].material is None or obj.material_slots[index].material.name != instance.bindings[index].export_name:
                raise ValueError(f"FBX/config material placeholder identity mismatch: {instance.export_name}, submesh={index}.")
            ordered = instance.bindings[index:] if index == instance.submesh_count - 1 else (instance.bindings[index],)
            if any(binding.identity is None for binding in ordered):
                material = archive_unresolved(instance, index)
            else:
                refs = tuple(binding.identity for binding in ordered if binding.identity is not None)
                if refs not in stacks:
                    stacks[refs] = surface_stack(tuple(sources[identity] for identity in refs), groups, layout)
                material = stacks[refs]
            obj.material_slots[index].link = "OBJECT"
            obj.material_slots[index].material = material
        obj["source_renderer_identity"] = instance.instance_id
        obj["source_identity_kind"] = "npc_config_mesh_instance"
        obj["source_mesh_identity"] = json.dumps(instance.mesh_identity)
        obj["source_mesh_raw_sha256"] = instance.mesh_sha256
        obj["source_config_mesh_instance_json"] = instance.source_json
        obj["source_runtime_pipeline_scope"] = "Unity Renderer/GameObject unavailable; runtime lighting/shadow fields unknown."
        hidden = instance.configured_disabled or instance.lod_index != selected_lod
        obj.hide_render = hidden
        obj.hide_viewport = hidden
        obj.hide_set(hidden)
        objects.append(obj)
    return tuple(objects)


def main(args: argparse.Namespace) -> None:
    if args.output.exists():
        raise FileExistsError(args.output)
    if min(args.width, args.height, args.samples) < 1 or args.lod_index < 0:
        raise ValueError("NPC render dimensions/samples and selected LOD are invalid.")
    sys.path.insert(0, str(args.module_dir))
    sys.path.insert(0, str(args.repository_src))
    from blender_scene_materials import layout_shader_node_tree
    from rebuild_character_materials import read_json, object_value, array_value, read_material_sources, SourcePacket, SUPPORTED_SHADERS, load_images, material_group, surface_stack, make_environment, file_hash, render_view
    from character_npr_nodes import ArtistLighting, make_artist_lighting
    from preview_character import reset_static_bind_pose, verify_saved_static_bind_pose, write_static_bind_pose_audit, configure_preview
    from audit_character_materials import geometry_digest
    assembly = require_object(read_json(args.assembly), "assembly")
    instances = read_instances(assembly)
    source_dir = args.assembly.parent
    shader = object_value(read_json(source_dir / "shader_identity.json"), "NPC actual shader evidence")
    if shader["format"] != "EndfieldCharacterShaderIdentity/1":
        raise ValueError("Unsupported NPC Shader identity evidence format.")
    for field, path in (("assembly_sha256", args.assembly), ("materials_sha256", source_dir / "materials.json")):
        if require_string(shader[field], field).casefold() != file_hash(path):
            raise ValueError(f"NPC Shader evidence belongs to different source data: {field}, {path}.")
    for raw in array_value(shader["shaders"], "NPC shader rows"):
        row = object_value(raw, "NPC shader row")
        if object_value(row["shader"], "NPC shader identity")["type"] != "Shader":
            raise ValueError("NPC Shader identity evidence has a non-Shader source object.")
        if require_integer(row["object_bytes"], "object_bytes") != require_integer(row["type_tree_bytes_read"], "type_tree_bytes_read"):
            raise ValueError("NPC Shader identity evidence has an incomplete TypeTree read.")
    records = tuple(object_value(row, "NPC actual material") for row in array_value(read_json(source_dir / "materials.json"), "NPC materials"))
    visible = frozenset(binding.identity for instance in instances if not instance.configured_disabled and instance.lod_index == args.lod_index for binding in instance.bindings if binding.identity is not None)
    materials, textures = read_material_sources(source_dir, records, shader, visible)
    sources = {source.identity: source for source in materials}
    if any(binding.identity not in sources for instance in instances for binding in instance.bindings if binding.identity is not None):
        raise ValueError("NPC resolved material binding is absent from verified material evidence.")
    input_paths = (args.assembly, args.blend, source_dir / "materials.json", source_dir / "material_sources.json", source_dir / "source_texture_artifacts.json", source_dir / "shader_identity.json")
    packet = SourcePacket(materials, textures, (), (), MappingProxyType({}), MappingProxyType({str(path): file_hash(path) for path in input_paths}), visible)
    bpy.ops.wm.open_mainfile(filepath=str(args.blend), use_scripts=False)
    meshes = tuple(obj for obj in bpy.data.objects if obj.type == "MESH")
    if {obj.name for obj in meshes} != {instance.export_name for instance in instances}:
        raise ValueError("Imported FBX Mesh inventory differs from exact NPC assembly names.")
    for instance in instances:
        obj = bpy.data.objects[instance.export_name]
        hidden = instance.configured_disabled or instance.lod_index != args.lod_index
        obj.hide_render = hidden
        obj.hide_viewport = hidden
        obj.hide_set(hidden)
    visible_objects = tuple(obj for obj in meshes if not obj.hide_render)
    if not visible_objects:
        raise ValueError("Selected NPC configuration LOD has no visible Mesh.")
    pose = reset_static_bind_pose(bpy.context.scene, args.blend, visible_objects)
    refresh_source_transforms(meshes)
    before = geometry_digest(tuple(obj.name for obj in meshes))
    before_state = {obj.name: {"matrix_world": [list(row) for row in obj.matrix_world], "visibility": [obj.hide_render, obj.hide_viewport, obj.hide_get()]} for obj in meshes}
    lighting = make_artist_lighting(ArtistLighting((.7, -1., .6), .9, .15, 0., .25, .75), layout_shader_node_tree)
    environment = make_environment(layout_shader_node_tree)
    images = load_images(packet)
    groups = {source.identity: material_group(source, packet, environment, images, lighting, layout_shader_node_tree) for source in materials}
    for source in materials:
        bpy.data.texts.new("SOURCE MATERIAL " + source.name + ".json").write(source.source_json)
        bpy.data.texts.new("SOURCE TYPETREE " + source.name + ".json").write(source.tree_json)
        archive = surface_stack((source,), groups, layout_shader_node_tree)
        archive.use_fake_user = True
    objects = bind_instances(instances, args.lod_index, sources, groups, layout_shader_node_tree)
    refresh_source_transforms(objects)
    object_names = tuple(obj.name for obj in objects)
    packed_image_names = tuple(image.name for image in images.values())
    if geometry_digest(object_names) != before:
        raise AssertionError("NPC material binding changed geometry, weights, UVs or visibility.")
    args.output.mkdir(parents=True)
    write_static_bind_pose_audit(args.output / "static_bind_pose_audit.json", pose)
    bpy.data.texts.new("SOURCE NPC ASSEMBLY.json").write(json.dumps(assembly, sort_keys=True))
    for instance in instances:
        bpy.data.texts.new("SOURCE NPC CONFIG " + instance.instance_id + ".json").write(instance.source_json)
    _, bounds = configure_preview(visible_objects, args.output / "geometry_reference.png")
    center = (bounds[0] + bounds[1]) * .5
    extent = max(bounds[1] - bounds[0])
    bpy.data.objects["PREVIEW_ONLY_Ground"].hide_render = True
    scene = bpy.context.scene
    scene.world.use_nodes = True
    scene.world.node_tree.nodes["Background"].inputs["Color"].default_value = (.045, .045, .045, 1.)
    scene.world.node_tree.nodes["Background"].inputs["Strength"].default_value = .35
    scene.view_settings.view_transform = "AgX"
    renders = [render_view(args.output, "front_eevee.png", "BLENDER_EEVEE_NEXT", Vector((0., -1., .05)), center, extent, args.width, args.height, args.samples), render_view(args.output, "side_eevee.png", "BLENDER_EEVEE_NEXT", Vector((1., -.05, .05)), center, extent, args.width, args.height, args.samples), render_view(args.output, "front_cycles_smoke.png", "CYCLES", Vector((0., -1., .05)), center, extent, 192, 256, min(args.samples, 16))]
    blend = args.output / "character_materials.blend"
    bpy.ops.wm.save_as_mainfile(filepath=str(blend))
    bpy.ops.wm.open_mainfile(filepath=str(blend), use_scripts=False)
    bpy.context.view_layer.update()
    verify_saved_static_bind_pose(pose)
    refresh_source_transforms(tuple(bpy.data.objects[name] for name in object_names))
    reopened = geometry_digest(object_names)
    if reopened != before:
        changed = tuple(name for name in object_names if reopened[name] != before[name])
        after_state = {name: {"matrix_world": [list(row) for row in bpy.data.objects[name].matrix_world], "visibility": [bpy.data.objects[name].hide_render, bpy.data.objects[name].hide_viewport, bpy.data.objects[name].hide_get()]} for name in changed}
        (args.output / "geometry_failure.json").write_text(json.dumps({"before": before, "reopened": reopened, "changed_objects": changed, "before_state": {name: before_state[name] for name in changed}, "after_state": after_state}, indent=2), encoding="utf-8")
        raise AssertionError(f"NPC saved geometry, weights, UVs or visibility changed: {changed}.")
    if any(bpy.data.images[name].packed_file is None for name in packed_image_names):
        raise AssertionError("NPC source image is not packed after reopen.")
    customization_records: list[dict[str, object]] = []
    for source in materials:
        if source.shader != "HGRP/CharacterNPR" or "_CUSTOMIZE_AVATAR" not in source.keywords:
            continue
        matches = tuple(group for group in bpy.data.node_groups
                        if group.get("source_identity") == json.dumps(source.identity))
        if len(matches) != 1:
            raise AssertionError(f"Avatar customization source node group did not reopen uniquely: {source.identity}.")
        group = matches[0]
        customization = json.loads(group.get("source_avatar_customization", "{}"))
        if customization.get("keyword") != "_CUSTOMIZE_AVATAR":
            raise AssertionError(f"Avatar customization formula evidence did not reopen: {source.identity}.")
        interface_names = {item.name for item in group.interface.items_tree if item.item_type == "SOCKET" and item.in_out == "INPUT"}
        required_controls = {"Avatar Customize Enabled", "Customize Base Color",
                             "Customize Base Tint Color", "Customize Add Tint Color"}
        if not required_controls.issubset(interface_names):
            raise AssertionError(f"Avatar customization controls did not reopen: {source.identity}.")
        base_map = next((binding for binding in source.textures if binding.name == "_BaseMap"), None)
        if base_map is None:
            raise AssertionError(f"Avatar customization source has no exact BaseMap binding: {source.identity}.")
        texture = packet.textures[base_map.identity]
        customization_records.append({
            "material_identity": list(source.identity),
            "material_name": source.name,
            "base_map_source_identity": list(texture.identity),
            "base_map_name": texture.name,
            "base_map_sha256": texture.sha256,
            "base_map_png": str(texture.path),
            "avatar_customize_enabled": source.floats["_AvatarCustomizeEnable"],
            "customize_colors": {name: source.colors[name] for name in
                                 ("_CustomizeBaseColor", "_CustomizeBaseTintColor", "_CustomizeAddTintColor")},
            "formula": customization["formula"],
            "editable_controls_reopened": sorted(required_controls),
            "node_group_reopened": True,
            "compiled_sample_assignment_verified": False,
            "shader_formula_evidence": "Formula transcribed from the existing shaders_1.2.4 compiled export; shader_identity.json reads current installed package objects, but no cross-build object hash establishes that this compiled variant is identical to the current package.",
            "sample_assignment_gap": "The extracted compiled branch omits the BaseMap sample write before the first customization use; exact material BaseMap identity and channels are known, but compiled sampler-to-branch dataflow is not closed.",
        })
    report = {"format": "EndfieldNpcMaterialSample/1", "status": "rendered_reopened", "source_identity_kind": "npc_config_mesh_instance", "input_hashes": dict(packet.input_hashes), "source_mesh_instances": len(instances), "visible_mesh_instances": len(visible_objects), "resolved_materials": len(materials), "actual_shader_families": sorted({source.shader for source in materials}), "visible_material_identities": sorted(visible), "packed_image_roles": len(images), "source_geometry_preserved": True, "source_bind_pose_editable": True, "source_runtime_renderer_fields_known": False, "unresolved_archives": [json.loads(instance.source_json) for instance in instances if any(binding.identity is None for binding in instance.bindings)], "output": str(blend), "sha256": file_hash(blend), "script_sha256": file_hash(Path(__file__)), "shared_material_module_sha256": file_hash(args.module_dir / "rebuild_character_materials.py"), "renders": renders, "limitations": ["Configuration-selected LOD and disabled state are not captured Unity Renderer activeInHierarchy or runtime platform selection.", "No Unity Renderer TypeTree, native cast-shadow/layer fields, animation or complete HGRP runtime fidelity is fabricated.", "Disabled unresolved material evidence is archived without resolving or inventing its shader."]}
    report["avatar_customization"] = customization_records
    report["material_support"] = {
        "visible_supported": [{"identity": source.identity, "shader": source.shader} for source in materials if source.identity in visible],
        "inactive_unsupported": [{"identity": source.identity, "shader": source.shader} for source in materials if source.shader not in SUPPORTED_SHADERS],
    }
    (args.output / "verification.json").write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps({"status": report["status"], "source_mesh_instances": len(instances), "visible_mesh_instances": len(visible_objects), "output": str(blend)}))


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    for name in ("assembly", "blend", "output", "module-dir", "repository-src"):
        parser.add_argument("--" + name, required=True, type=Path)
    for name in ("width", "height", "samples", "lod-index"):
        parser.add_argument("--" + name, required=True, type=int)
    main(parser.parse_args(sys.argv[sys.argv.index("--") + 1:]))
