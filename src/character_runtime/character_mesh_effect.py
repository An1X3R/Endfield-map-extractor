"""Adapt scene VFXBaseV2 materials to character material groups with explicit mesh evidence."""
from __future__ import annotations

from dataclasses import dataclass
import json
import math
from pathlib import Path
from types import MappingProxyType
from typing import TYPE_CHECKING, Callable, Mapping, Sequence, cast

import bpy

from character_material_authoring import select_nodes, shader_editor
from endfield_scene_material_contract import AssetReference, MaterialRecord, TextureSlot
from blender_scene_effect_materials import make_mesh_effect_material
from character_npr_vfx import CHARACTER_VFX_SHADER, MESH_EFFECT_SHADERS, character_vfx_preview_record
from character_windows_paths import io_path

if TYPE_CHECKING:
    from build_npc_material_sample import NpcMeshInstance
    from rebuild_character_materials import MaterialSource, SourcePacket, TextureSource


Identity = tuple[str, int]
VFX_SHADER = "HGRP/Effect/VFXBaseV2"
VFX_SNAPSHOT_TIME = 0.0


@dataclass(frozen=True)
class MeshColorSource:
    """Describe the color attribute observed on the imported Blender mesh objects."""

    components: int
    name: str | None
    mesh_names: tuple[str, ...]

    def __post_init__(self) -> None:
        if self.components not in (0, 4):
            raise ValueError(f"Unsupported imported mesh color component count: {self.components}")
        if (self.components == 0 and self.name is not None) or (self.components == 4 and not self.name):
            raise ValueError(f"Mesh color evidence has inconsistent name/count: components={self.components}, name={self.name!r}")
        if not self.mesh_names or len(set(self.mesh_names)) != len(self.mesh_names):
            raise ValueError(f"Mesh color evidence requires unique mesh names: {self.mesh_names}")


def mesh_color_source(obj: bpy.types.Object) -> MeshColorSource:
    """Read the selected imported Blender color attribute without changing mesh data."""
    if obj.type != "MESH":
        raise TypeError(f"Mesh color evidence requires a Mesh object: object={obj.name}, type={obj.type}")
    attributes = tuple(obj.data.color_attributes)
    if not attributes:
        return MeshColorSource(0, None, (obj.name,))
    active = obj.data.color_attributes.active_color
    if len(attributes) == 1:
        attribute = attributes[0]
    elif active is not None:
        matches = tuple(item for item in attributes if item.as_pointer() == active.as_pointer())
        if len(matches) != 1:
            raise ValueError(f"Active imported color attribute is ambiguous: mesh={obj.name}, attributes={[item.name for item in attributes]}")
        attribute = matches[0]
    else:
        raise ValueError(f"Imported mesh has multiple color attributes and no active selection: mesh={obj.name}, attributes={[item.name for item in attributes]}")
    if attribute.domain not in ("POINT", "CORNER") or attribute.data_type not in ("FLOAT_COLOR", "BYTE_COLOR"):
        raise ValueError(f"Unsupported imported color attribute layout: mesh={obj.name}, attribute={attribute.name}, domain={attribute.domain}, type={attribute.data_type}")
    expected_count = len(obj.data.vertices) if attribute.domain == "POINT" else len(obj.data.loops)
    if len(attribute.data) != expected_count:
        raise ValueError(f"Imported color attribute length differs from its domain: mesh={obj.name}, attribute={attribute.name}, expected={expected_count}, actual={len(attribute.data)}")
    return MeshColorSource(4, attribute.name, (obj.name,))


def collect_mesh_color_sources(
    instances: Sequence[NpcMeshInstance],
    objects: Mapping[str, bpy.types.Object],
    sources: Mapping[Identity, MaterialSource],
) -> Mapping[Identity, MeshColorSource]:
    """Associate each source material with consistent color evidence from its imported meshes."""
    observed: dict[Identity, list[MeshColorSource]] = {}
    for instance in instances:
        obj = objects.get(instance.export_name)
        if obj is None:
            raise KeyError(f"Imported NPC mesh is missing for color evidence: instance={instance.instance_id}, mesh={instance.export_name}")
        color_source = mesh_color_source(obj)
        for binding in instance.bindings:
            if binding.identity is None:
                continue
            if binding.identity not in sources:
                raise KeyError(f"NPC material binding is absent from the verified source map: instance={instance.instance_id}, identity={binding.identity}")
            observed.setdefault(binding.identity, []).append(color_source)
    result: dict[Identity, MeshColorSource] = {}
    for identity, rows in observed.items():
        layouts = {(row.components, row.name) for row in rows}
        if len(layouts) != 1:
            evidence = [(row.mesh_names, row.components, row.name) for row in rows]
            raise ValueError(f"Shared material has conflicting imported mesh color layouts: material={identity}, evidence={evidence}")
        components, name = next(iter(layouts))
        mesh_names = tuple(sorted({mesh_name for row in rows for mesh_name in row.mesh_names}))
        result[identity] = MeshColorSource(components, name, mesh_names)
    return MappingProxyType(result)


def collect_character_mesh_color_sources(packet: SourcePacket) -> Mapping[Identity, MeshColorSource]:
    """Resolve source material color layouts through the audited character renderer mapping."""
    sources = {source.identity: source for source in packet.materials}
    renderers = {_source_identity(row['object'], 'renderer.object'): row for row in packet.renderers}
    observed: dict[Identity, list[MeshColorSource]] = {}
    for decision in packet.selection:
        key = _string(decision['renderer_identity'], 'renderer_identity')
        cab, path_id = key.rsplit(':', 1)
        renderer = renderers[(cab.casefold(), int(path_id))]
        name = _string(decision['fbx_object_name'], 'fbx_object_name')
        obj = bpy.data.objects.get(name)
        if obj is None or obj.type != 'MESH':
            raise ValueError(f'Imported character mesh is absent for VFX color evidence: object={name}')
        for raw in _array(renderer['materials'], 'renderer.materials'):
            identity = _source_identity(_object(raw, 'material.pointer')['resolved'], 'material.resolved')
            if sources[identity].shader in MESH_EFFECT_SHADERS:
                observed.setdefault(identity, []).append(mesh_color_source(obj))
    result: dict[Identity, MeshColorSource] = {}
    for identity, rows in observed.items():
        layouts = {(row.components, row.name) for row in rows}
        if len(layouts) != 1:
            raise ValueError(f'Shared character VFX material has conflicting imported color layouts: material={identity}; layouts={layouts}')
        components, name = next(iter(layouts))
        result[identity] = MeshColorSource(components, name, tuple(sorted({mesh for row in rows for mesh in row.mesh_names})))
    return MappingProxyType(result)


def _object(value: object, context: str) -> dict[str, object]:
    if not isinstance(value, dict) or any(not isinstance(key, str) for key in value):
        raise TypeError(f"Expected object: context={context}")
    return cast(dict[str, object], value)


def _array(value: object, context: str) -> list[object]:
    if not isinstance(value, list):
        raise TypeError(f"Expected array: context={context}")
    return value


def _string(value: object, context: str) -> str:
    if not isinstance(value, str) or not value:
        raise TypeError(f"Expected nonempty string: context={context}, value={value!r}")
    return value


def _integer(value: object, context: str) -> int:
    if type(value) is not int:
        raise TypeError(f"Expected integer: context={context}, value={value!r}")
    return value


def _number_list(value: object, length: int, context: str) -> list[float]:
    rows = _array(value, context)
    if len(rows) != length or any(type(item) not in (int, float) or not math.isfinite(float(item)) for item in rows):
        raise ValueError(f"Expected finite numeric array: context={context}, expected_length={length}, value={value!r}")
    return [float(item) for item in rows]


def _source_identity(value: object, context: str) -> Identity:
    row = _object(value, context)
    source = _string(row.get("source"), f"{context}.source")
    path_id = _integer(row.get("path_id"), f"{context}.path_id")
    return source.casefold(), path_id


def _texture_path_key(path: Path) -> str:
    return str(io_path(path).resolve()).casefold()


def _scene_record(source: MaterialSource, packet: SourcePacket) -> tuple[MaterialRecord, Mapping[Identity, tuple[Path, str]], dict[str, object]]:
    """Translate exact character material evidence into the existing scene material contract."""
    raw = _object(json.loads(source.source_json), "character.material_source_json")
    raw_tree = _object(json.loads(source.tree_json), "character.material_tree_json")
    material_object = _object(raw.get("object"), "character.material.object")
    if _source_identity(material_object, "character.material.object") != source.identity:
        raise ValueError(f"Material source identity changed during scene adaptation: material={source.identity}")
    material_name = _string(material_object.get("name"), "character.material.name")
    if material_name != source.name:
        raise ValueError(f"Material name changed during scene adaptation: identity={source.identity}, parsed={source.name}, raw={material_name}")
    shader_pointer = _object(raw.get("shader"), "character.material.shader")
    shader_identity = _source_identity(shader_pointer.get("resolved"), "character.material.shader.resolved")
    tree_shader = _object(raw_tree.get("m_Shader"), "character.material_tree.m_Shader")
    if _integer(tree_shader.get("m_PathID"), "character.material_tree.m_Shader.m_PathID") != _integer(shader_pointer.get("path_id"), "character.material.shader.path_id"):
        raise ValueError(f"Material and TypeTree shader path IDs disagree: material={source.identity}")
    if _integer(tree_shader.get("m_FileID"), "character.material_tree.m_Shader.m_FileID") != _integer(shader_pointer.get("file_id"), "character.material.shader.file_id"):
        raise ValueError(f"Material and TypeTree shader file IDs disagree: material={source.identity}")

    path_map: dict[Identity, tuple[Path, str]] = {}
    for identity, texture in packet.textures.items():
        if identity != texture.identity:
            raise ValueError(f"Verified source texture map key differs from its record: map_key={identity}, texture={texture.identity}")
        path = io_path(texture.path)
        if not path.is_file():
            raise FileNotFoundError(f"Verified source texture disappeared before scene adaptation: identity={identity}, path={texture.path}")
        path_map[identity] = (path.parent, path.name)

    texture_slots: list[TextureSlot] = []
    for raw_slot in _array(raw.get("textures"), "character.material.textures"):
        slot = _object(raw_slot, "character.material.texture_slot")
        property_name = _string(slot.get("slot"), "character.material.texture_slot.slot")
        pointer = _object(slot.get("reference"), f"character.material.texture_slot[{property_name}].reference")
        path_id = _integer(pointer.get("path_id"), f"character.material.texture_slot[{property_name}].path_id")
        resolved = pointer.get("resolved")
        texture_reference: AssetReference | None
        if path_id == 0:
            if resolved is not None or _integer(pointer.get("file_id"), f"character.material.texture_slot[{property_name}].file_id") != 0:
                raise ValueError(f"Null source texture pointer is inconsistent: material={source.identity}, property={property_name}")
            texture_reference = None
        else:
            texture_identity = _source_identity(resolved, f"character.material.texture_slot[{property_name}].resolved")
            texture = packet.textures.get(texture_identity)
            if texture is None:
                raise FileNotFoundError(f"Referenced source texture is absent from verified packet: material={source.identity}, property={property_name}, identity={texture_identity}")
            if texture_identity not in path_map:
                raise KeyError(f"Verified source texture has no scene path mapping: identity={texture_identity}")
            if texture.color_space not in (0, 1):
                raise ValueError(f"Source texture color space is unsupported: identity={texture_identity}, color_space={texture.color_space}")
            texture_reference = {
                "Source": texture_identity[0],
                "PathId": texture_identity[1],
                "Name": texture.name,
                "ColorSpace": texture.color_space,
            }
        texture_slots.append({
            "Property": property_name,
            "Texture": texture_reference,
            "Scale": _number_list(slot.get("scale"), 2, f"character.material.texture_slot[{property_name}].scale"),
            "Offset": _number_list(slot.get("offset"), 2, f"character.material.texture_slot[{property_name}].offset"),
        })

    raw_keywords = _array(raw_tree.get("m_ValidKeywords"), "character.material_tree.m_ValidKeywords")
    keywords = [_string(keyword, "character.material_tree.keyword") for keyword in raw_keywords]
    if tuple(keywords) != source.keywords:
        raise ValueError(f"Material keywords changed after source parsing: material={source.identity}")
    raw_disabled = raw_tree.get("m_DisabledShaderPasses")
    if raw_disabled is None:
        disabled: list[str] = []
    else:
        disabled = [_string(value, "character.material_tree.disabled_shader_pass")
                    for value in _array(raw_disabled, "character.material_tree.m_DisabledShaderPasses")]
    custom_render_queue = _integer(raw_tree.get("m_CustomRenderQueue"), "character.material_tree.m_CustomRenderQueue")
    defaults = [{"Property": name, "Name": value, "Dimension": 2}
                for name, value in sorted(source.null_texture_defaults.items())]
    record: MaterialRecord = {
        "Name": source.name,
        "Source": source.identity[0],
        "PathId": source.identity[1],
        "Shader": {
            "Source": shader_identity[0],
            "PathId": shader_identity[1],
            "Name": source.shader,
            "TextureDefaults": defaults,
        },
        "Textures": texture_slots,
        "Floats": dict(source.floats),
        "Colors": {name: list(values) for name, values in source.colors.items()},
        "RenderState": {
            "DisabledShaderPasses": disabled,
            "ValidKeywords": keywords,
            "CustomRenderQueue": custom_render_queue,
        },
    }
    raw_state = {
        "m_DisabledShaderPasses": raw_disabled,
        "m_ValidKeywords": raw_tree.get("m_ValidKeywords"),
        "m_CustomRenderQueue": raw_tree.get("m_CustomRenderQueue"),
    }
    return record, MappingProxyType(path_map), raw_state


def _tree_animation_paths(tree: bpy.types.NodeTree, trail: frozenset[int]) -> tuple[str, ...]:
    pointer = tree.as_pointer()
    if pointer in trail:
        raise ValueError(f"Recursive VFX node group while checking driver safety: tree={tree.name}")
    paths: list[str] = []
    animation = tree.animation_data
    if animation is not None:
        if animation.action is not None:
            paths.append(f"{tree.name}:action={animation.action.name}")
        paths.extend(f"{tree.name}:{driver.data_path}" for driver in animation.drivers)
    for item in tree.nodes:
        if item.type == "GROUP" and item.node_tree is not None:
            paths.extend(_tree_animation_paths(item.node_tree, trail | {pointer}))
    return tuple(paths)


def _validate_snapshot_time(tree: bpy.types.NodeTree, snapshot_time: float, stage: str) -> None:
    if _tree_animation_paths(tree, frozenset()):
        raise ValueError(f"VFX source node tree contains animation or drivers at {stage}: tree={tree.name}")
    nodes = tuple(item for item in tree.nodes if item.name == "EF FX Source Time Seconds")
    if len(nodes) != 1 or len(nodes[0].outputs) != 1:
        raise ValueError(f"VFX source time node is missing or ambiguous at {stage}: tree={tree.name}, count={len(nodes)}")
    value = nodes[0].outputs[0].default_value
    if type(value) not in (int, float) or not math.isfinite(float(value)) or not math.isclose(float(value), snapshot_time, rel_tol=0.0, abs_tol=1e-8):
        raise ValueError(f"VFX source time changed at {stage}: tree={tree.name}, expected={snapshot_time}, actual={value!r}")


def _factory_image_nodes(tree: bpy.types.NodeTree, trail: frozenset[int]) -> tuple[bpy.types.Node, ...]:
    pointer = tree.as_pointer()
    if pointer in trail:
        raise ValueError(f"Recursive VFX node group while validating source images: tree={tree.name}")
    result: list[bpy.types.Node] = []
    for item in tree.nodes:
        if item.type == "TEX_IMAGE":
            if item.image is None:
                raise ValueError(f"VFX factory produced an image node without an image: tree={tree.name}, node={item.name}")
            result.append(item)
        elif item.type == "GROUP" and item.node_tree is not None:
            result.extend(_factory_image_nodes(item.node_tree, trail | {pointer}))
    return tuple(result)


def _tag_and_pack_factory_images(
    material: bpy.types.Material,
    texture_paths: Mapping[Identity, tuple[Path, str]],
    packet: SourcePacket,
    record: MaterialRecord,
    images: Mapping[tuple[Identity, str, str], bpy.types.Image],
) -> None:
    by_path: dict[str, TextureSource] = {}
    for texture in packet.textures.values():
        key = _texture_path_key(texture.path)
        if key in by_path and by_path[key].identity != texture.identity:
            raise ValueError(f"Distinct source textures resolve to the same exported image path: path={texture.path}, identities={by_path[key].identity}/{texture.identity}")
        by_path[key] = texture
    slot_names = {slot["Property"] for slot in record["Textures"]}
    for sample in _factory_image_nodes(material.node_tree, frozenset()):
        image = sample.image
        if image is None:
            raise ValueError(f"VFX factory image disappeared during validation: material={material.name}, node={sample.name}")
        image_path = Path(bpy.path.abspath(image.filepath)).resolve()
        texture = by_path.get(_texture_path_key(image_path))
        if texture is None:
            raise FileNotFoundError(f"VFX factory image does not resolve to a verified source texture: material={material.name}, node={sample.name}, path={image_path}")
        if texture.identity not in texture_paths:
            raise KeyError(f"VFX factory image texture has no contract path mapping: identity={texture.identity}")
        expected_space = "Non-Color" if texture.color_space == 0 else "sRGB"
        if image.colorspace_settings.name != expected_space:
            raise ValueError(f"VFX factory image color space differs from source evidence: node={sample.name}, expected={expected_space}, actual={image.colorspace_settings.name}")
        shared = images.get((texture.identity, expected_space, image.alpha_mode))
        if shared is None:
            raise KeyError(f"VFX source image role is absent from the character image cache: identity={texture.identity}, colorspace={expected_space}, node={sample.name}")
        if shared.colorspace_settings.name != expected_space or shared.alpha_mode != image.alpha_mode:
            raise ValueError(f"VFX cached image decoding differs from the factory source role: identity={texture.identity}, expected_colorspace={expected_space}, actual_colorspace={shared.colorspace_settings.name}, expected_alpha={image.alpha_mode}, actual_alpha={shared.alpha_mode}")
        if shared.packed_file is None:
            raise ValueError(f"VFX cached source image is not packed: identity={texture.identity}, image={shared.name}")
        sample.image = shared
        prefix = "Source "
        if sample.name.startswith(prefix):
            slot = sample.name[len(prefix):]
            if slot not in slot_names:
                raise ValueError(f"VFX factory sampler node has no matching serialized material slot: node={sample.name}, material={material.name}")
            sample["source_slot"] = slot
        else:
            sample["source_slot"] = sample.name
        sample["source_identity"] = json.dumps(texture.identity)
        sample["source_sha256"] = texture.sha256
        sample["source_color_space"] = texture.color_space
        sample["source_sampler_scope"] = "Not serialized in the character source packet; scene factory applies its explicit Linear/REPEAT preview and records this limitation."
        if image != shared and image.users == 0:
            bpy.data.images.remove(image)


def _bind_vertex_color_nodes(
    tree: bpy.types.NodeTree,
    source: MaterialSource,
    color_source: MeshColorSource,
) -> None:
    vertex_nodes = tuple(item for item in tree.nodes if item.type == "VERTEX_COLOR")
    missing_color_nodes = tuple(item for item in tree.nodes if item.name == "MeshRenderer Missing COLOR Reference White")
    disabled = source.floats.get("_DisableVertColor", 0.0) == 1.0
    if disabled:
        if vertex_nodes or missing_color_nodes:
            raise ValueError(f"VFX factory color node conflicts with disabled vertex color: material={source.identity}")
        return
    if color_source.components == 4:
        if color_source.name is None or len(vertex_nodes) != 1 or missing_color_nodes:
            raise ValueError(f"VFX factory vertex-color node differs from imported mesh evidence: material={source.identity}, mesh_color={color_source}, vertex_nodes={len(vertex_nodes)}, missing_reference_nodes={len(missing_color_nodes)}")
        vertex_nodes[0].layer_name = color_source.name
        vertex_nodes[0]["source_mesh_color_attribute"] = color_source.name
    elif color_source.components == 0:
        if vertex_nodes or len(missing_color_nodes) != 1:
            raise ValueError(f"VFX factory missing-COLOR reference differs from imported mesh evidence: material={source.identity}, mesh_color={color_source}, vertex_nodes={len(vertex_nodes)}, missing_reference_nodes={len(missing_color_nodes)}")
    else:
        raise ValueError(f"Unsupported VFX mesh color evidence: material={source.identity}, components={color_source.components}")


def _group_factory_surface(
    material: bpy.types.Material,
    source: MaterialSource,
    color_source: MeshColorSource,
    raw_render_state: Mapping[str, object],
    layout: Callable[[bpy.types.NodeTree], None],
) -> bpy.types.ShaderNodeTree:
    if not material.use_nodes or material.node_tree is None:
        raise ValueError(f"Scene VFX factory returned a material without nodes: material={material.name}")
    tree = material.node_tree
    if source.shader == CHARACTER_VFX_SHADER:
        for uv in (item for item in tree.nodes if item.type == 'UVMAP'):
            if uv.uv_map == 'UVMap':
                uv.uv_map = 'UV0'
            elif uv.uv_map == 'EF_FX_UV1':
                uv.uv_map = 'UV1'
    _validate_snapshot_time(tree, VFX_SNAPSHOT_TIME, "before node grouping")
    _bind_vertex_color_nodes(tree, source, color_source)
    material_outputs = tuple(item for item in tree.nodes if item.type == "OUTPUT_MATERIAL" and item.is_active_output)
    if len(material_outputs) != 1:
        raise ValueError(f"Scene VFX factory must return one active material output: material={material.name}, count={len(material_outputs)}")
    surface_links = tuple(material_outputs[0].inputs["Surface"].links)
    if len(surface_links) != 1 or surface_links[0].from_socket.type != "SHADER":
        raise ValueError(f"Scene VFX factory surface must have one shader input: material={material.name}, links={len(surface_links)}")
    selected = tuple(item for item in tree.nodes if item.type != "OUTPUT_MATERIAL")
    if not selected:
        raise ValueError(f"Scene VFX factory returned an empty surface graph: material={material.name}")
    with shader_editor(tree):
        select_nodes(tree, selected)
        result = bpy.ops.node.group_make()
        if result != {"FINISHED"}:
            raise RuntimeError(f"Native Blender node.group_make failed for scene VFX material: material={material.name}, result={result}")
    group_node = tree.nodes.active
    if group_node is None or group_node.type != "GROUP" or group_node.node_tree is None:
        raise RuntimeError(f"Native Blender node.group_make did not leave an active source group: material={material.name}")
    group_tree = group_node.node_tree
    outer_nodes = tuple(item for item in tree.nodes if item.type != "FRAME")
    if len(outer_nodes) != 2 or {item.type for item in outer_nodes} != {"GROUP", "OUTPUT_MATERIAL"}:
        raise ValueError(f"Scene VFX grouping left unexpected outer nodes: material={material.name}, nodes={[item.type for item in outer_nodes]}")
    _validate_snapshot_time(group_tree, VFX_SNAPSHOT_TIME, "after node grouping")
    output_sockets = tuple(item for item in group_tree.interface.items_tree
                           if item.item_type == "SOCKET" and item.in_out == "OUTPUT")
    if len(output_sockets) != 1 or output_sockets[0].socket_type != "NodeSocketShader":
        raise ValueError(f"Scene VFX group requires exactly one shader output from group_make: material={material.name}, outputs=[(item.name, item.socket_type) for item in output_sockets]")
    output_sockets[0].name = "Shader"
    group_tree.interface.new_socket(name="Coverage", in_out="OUTPUT", socket_type="NodeSocketFloat")
    group_tree.interface.new_socket(name="Multiply Factor", in_out="OUTPUT", socket_type="NodeSocketColor")
    group_outputs = tuple(item for item in group_tree.nodes if item.type == "GROUP_OUTPUT" and item.is_active_output)
    if len(group_outputs) != 1:
        raise ValueError(f"Scene VFX group must have one active group output: material={material.name}, count={len(group_outputs)}")
    group_output = group_outputs[0]
    if group_output.inputs["Coverage"].is_linked or group_output.inputs["Multiply Factor"].is_linked:
        raise ValueError(f"Scene VFX constant pass outputs unexpectedly became linked: material={material.name}")
    group_output.inputs["Coverage"].default_value = 1.0
    group_output.inputs["Multiply Factor"].default_value = (1.0, 1.0, 1.0, 1.0)
    output = next(item for item in tree.nodes if item.type == "OUTPUT_MATERIAL" and item.is_active_output)
    final_links = tuple(output.inputs["Surface"].links)
    if len(final_links) != 1 or final_links[0].from_node != group_node or final_links[0].from_socket.name != "Shader":
        raise ValueError(f"Scene VFX group output is not bound to the original material surface: material={material.name}")
    interface_names = {item.name for item in group_tree.interface.items_tree
                       if item.item_type == "SOCKET" and item.in_out == "OUTPUT"}
    if interface_names != {"Shader", "Coverage", "Multiply Factor"}:
        raise ValueError(f"Scene VFX group outputs differ from character pass contract: material={material.name}, outputs={sorted(interface_names)}")

    group_tree.name = "EF Character VFX " + source.name + " " + str(source.identity[1])
    group_node.label = source.shader + " / scene factory surface"
    group_tree["source_shader"] = source.shader
    group_tree["source_identity"] = json.dumps(source.identity)
    group_tree["source_floats"] = json.dumps(dict(source.floats), sort_keys=True)
    group_tree["source_colors"] = json.dumps({name: list(value) for name, value in source.colors.items()}, sort_keys=True)
    group_tree["source_keywords"] = json.dumps(source.keywords)
    group_tree["source_render_state"] = json.dumps(dict(raw_render_state), sort_keys=True)
    group_tree["source_mesh_color_evidence"] = json.dumps({
        "components": color_source.components,
        "name": color_source.name,
        "mesh_names": color_source.mesh_names,
        "scope": "Imported Blender mesh color attributes only; native Unity mesh stream identity is not established.",
    }, sort_keys=True)
    group_tree["source_adapter"] = "scene.make_mesh_effect_material / bpy.ops.node.group_make"
    group_tree["scope"] = "Existing scene VFXBaseV2 surface wrapped as a character material pass; snapshot-time approximation and scene-factory limitations remain explicit."
    for key in ("endfield_fx_source_material", "endfield_fx_source", "endfield_fx_path_id",
                "endfield_fx_source_material_json", "endfield_fx_status", "endfield_fx_limitations",
                "endfield_fx_custom1_channels", "endfield_character_vfx_normal_preview",
                "endfield_character_vfx_mesh_uv_weights", "endfield_character_vfx_missing_uv1_reference"):
        if key in material:
            group_tree[key] = material[key]
    if "endfield_fx_limitations" not in group_tree:
        raise ValueError(f"Scene VFX factory did not preserve its limitation record: material={material.name}")
    layout(group_tree)
    _validate_snapshot_time(group_tree, VFX_SNAPSHOT_TIME, "after group metadata and layout")
    if material.users != 0:
        raise ValueError(f"Temporary scene VFX material unexpectedly has users after wrapping: material={material.name}, users={material.users}")
    bpy.data.materials.remove(material)
    return group_tree


def material_group(
    source: MaterialSource,
    packet: SourcePacket,
    images: Mapping[tuple[Identity, str, str], bpy.types.Image],
    color_source: MeshColorSource | None,
    layout: Callable[[bpy.types.NodeTree], None],
) -> bpy.types.ShaderNodeTree:
    """Build an existing scene VFX factory material as a character source pass."""
    if source.shader not in MESH_EFFECT_SHADERS:
        raise ValueError(f"Character mesh effect adapter only supports {VFX_SHADER}: material={source.identity}, shader={source.shader}")
    if color_source is None:
        raise ValueError(f"VFXBaseV2 material requires actual imported Mesh color evidence: material={source.identity}")
    record, texture_paths, raw_render_state = _scene_record(source, packet)
    surface_record = record
    if source.shader == CHARACTER_VFX_SHADER and source.floats['_UseVertexOffset'] == 1.0:
        from character_vfx_vertex_offset import require_installed_offset
        require_installed_offset(source, color_source.mesh_names)
        # Geometry Nodes own this source stage; the surface factory must not displace twice.
        surface_record = {**record, 'Floats': {**record['Floats'], '_UseVertexOffset': 0.0}}
    preview_record = character_vfx_preview_record(surface_record) if source.shader == CHARACTER_VFX_SHADER else record
    factory_record = preview_record
    if source.shader == CHARACTER_VFX_SHADER:
        from character_npr_vfx_uv import character_vfx_mesh_uv_record
        factory_record = character_vfx_mesh_uv_record(preview_record, color_source.mesh_names)
    material = make_mesh_effect_material(factory_record, dict(texture_paths), VFX_SNAPSHOT_TIME, color_source.components)
    if source.shader == CHARACTER_VFX_SHADER:
        from character_npr_vfx_uv import apply_character_vfx_mesh_uv_weights
        apply_character_vfx_mesh_uv_weights(material, preview_record, color_source.mesh_names)
    if source.shader == CHARACTER_VFX_SHADER and source.floats['_EnableNormalMap'] == 1.0:
        from character_npr_vfx_normal import apply_character_vfx_fresnel_normal
        apply_character_vfx_fresnel_normal(material, record, source, packet, texture_paths, color_source.mesh_names, VFX_SNAPSHOT_TIME)
    _tag_and_pack_factory_images(material, texture_paths, packet, preview_record, images)
    group = _group_factory_surface(material, source, color_source, raw_render_state, layout)
    if source.shader == CHARACTER_VFX_SHADER:
        group['source_adapter'] = 'EndfieldCharacterNprVfxNamedRoles/1'
        group['endfield_fx_source_material_json'] = json.dumps(record, sort_keys=True)
        group['source_vfx_preview_record'] = json.dumps(preview_record, sort_keys=True)
        if source.floats.get('_UsePosYAsScreenV', 0.0) != preview_record['Floats'].get('_UsePosYAsScreenV', 0.0):
            group['source_inactive_screen_modifier'] = 'Source _UsePosYAsScreenV is retained in source_floats; the host adapter prunes it only when every active named texture role has zero screen-UV weight.'
        group['source_sampler_slot_scope'] = 'Sampler source_slot names refer to host preview aliases; the native bindings are preserved in endfield_fx_source_material_json.'
        group['scope'] = 'Current CharacterNPR_VFX named texture roles and source values adapted to existing host VFX nodes. Shader program equivalence, native lighting, separate depth draws and screen effects remain unverified.'
        group['native_shader_semantics_verified'] = False
        limitations = _array(json.loads(group['endfield_fx_limitations']), 'VFX limitations')
        limitations.append('CharacterNPR_VFX named roles use the shared host preview graph; current compiled shader equivalence is not audited.')
        if source.floats['_UseVertexOffset'] == 1.0:
            from character_vfx_vertex_offset import CONTRACT, LIMITATION
            group['source_vertex_offset_geometry_contract'] = CONTRACT
            limitations.append(LIMITATION)
        if source.floats['_Bi_Disturb'] == 1.0:
            limitations.append('Bidirectional disturbance uses two opposed half-strength samples of the actual source texture; native time phases and flow composition remain unverified.')
        if source.floats['_ZWriteWithAlphaTest'] == 1.0:
            limitations.append('Separate native alpha-tested depth draw is archived; the color material remains visible in host single-surface composition.')
        group['endfield_fx_limitations'] = json.dumps(limitations)
    return group


def verify_fractional_disturbance_weights(
    group: bpy.types.ShaderNodeTree,
    source: MaterialSource,
) -> tuple[str, ...]:
    """Verify fractional source coefficients reach the scene factory's UV nodes."""
    if source.shader != VFX_SHADER or group.get("source_identity") != json.dumps(source.identity):
        raise ValueError(f"Disturbance regression received a mismatched VFX group: material={source.identity}, group={group.name}")
    properties = [("_MainTex", "_MainTexUseDisturb")]
    properties.extend((f"_SampleTex{index}", f"_SampleTex{index}UseDisturb")
                      for index in range(5) if source.floats.get(f"_UseSampleTex{index}", 0) != 0)
    verified: list[str] = []
    for name, property_name in properties:
        weight = source.floats.get(property_name, 0.0)
        if type(weight) not in (int, float) or not math.isfinite(weight) or not 0 <= weight <= 1:
            raise ValueError(f"Source disturbance weight is outside [0, 1]: material={source.identity}, property={property_name}, value={weight!r}")
        node_name = f"{name} Disturbance Weight"
        weight_node = group.nodes.get(node_name)
        if weight in (0, 1):
            if weight_node is not None:
                raise AssertionError(f"Endpoint disturbance weight unexpectedly uses a scale node: material={source.identity}, property={property_name}, node={node_name}")
            continue
        if weight_node is None or weight_node.type != "VECT_MATH" or weight_node.operation != "SCALE":
            raise AssertionError(f"Fractional disturbance weight lacks its vector scale node: material={source.identity}, property={property_name}, node={node_name}")
        factor = weight_node.inputs[3]
        if factor.is_linked or type(factor.default_value) not in (int, float) or not math.isclose(float(factor.default_value), float(weight), rel_tol=0.0, abs_tol=1e-8):
            raise AssertionError(f"Fractional disturbance scale differs from its source value: material={source.identity}, property={property_name}, expected={weight}, actual={factor.default_value!r}")
        uv_node = group.nodes.get(f"{name} Distorted UV")
        if uv_node is None or not any(
            link.from_node == weight_node and link.to_node == uv_node and link.to_socket == uv_node.inputs[1]
            for link in group.links
        ):
            raise AssertionError(f"Fractional disturbance scale is not connected to its UV sampling node: material={source.identity}, property={property_name}, node={node_name}")
        verified.append(f"{property_name}={float(weight):g}")
    return tuple(verified)


def pack_group_images(groups: Mapping[Identity, bpy.types.ShaderNodeTree]) -> tuple[str, ...]:
    """Pack every image reachable from built source material groups for portable saved blends."""
    images: dict[int, bpy.types.Image] = {}
    for identity, tree in groups.items():
        for sample in _factory_image_nodes(tree, frozenset()):
            if sample.image is None:
                raise ValueError(f"Built material group contains an image sampler without data: material={identity}, node={sample.name}")
            images[sample.image.as_pointer()] = sample.image
    names: list[str] = []
    for image in images.values():
        if image.packed_file is None:
            image.pack()
        if image.packed_file is None:
            raise AssertionError(f"Built source material group image did not pack: image={image.name}")
        names.append(image.name)
    return tuple(sorted(names))
