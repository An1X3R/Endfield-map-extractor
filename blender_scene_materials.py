"""Reusable static surface approximation, adapted from the preserved Stage65 builder.

No region or material-name rules are permitted in this module. Unsupported
shader features remain explicit audit limitations, not fidelity claims.
"""
from __future__ import annotations
from collections import defaultdict
import hashlib
import json
from pathlib import Path
import bpy
from endfield_shader_contract import surface_family, normal_layout, uses_mro, uses_normal_map, base_texture_mode, emission_source, emissive_map_type
from blender_hgrp_surface import packed_normal_group, remap_roughness, apply_authored_albedo, apply_engine_surface
from endfield_scene_material_contract import MaterialRecord, TextureSlot, AssetReference, supported_slots, has_procedural_foliage_emission


def ref_key(ref: AssetReference) -> tuple[str, int]:
    return ref['Source'].casefold(), int(ref['PathId'])


def texture_image(path: Path, colorspace: str, alpha_mode: str) -> bpy.types.Image:
    """Share identical texture roles while isolating colour and packed-data uses."""
    identity = str(path.resolve()).casefold() + "|" + colorspace + "|" + alpha_mode
    name = "EF_TEXTURE_" + hashlib.sha256(identity.encode()).hexdigest()[:40]
    image = bpy.data.images.get(name)
    if image is None:
        image = bpy.data.images.load(str(path), check_existing=False)
        image.name = name
        image.colorspace_settings.name = colorspace
        image.alpha_mode = alpha_mode
    elif image.colorspace_settings.name != colorspace or image.alpha_mode != alpha_mode:
        raise ValueError(f"Shared texture role was modified: image={name}, path={path}")
    return image


def _input(node: bpy.types.Node, *names: str) -> bpy.types.NodeSocket:
    for name in names:
        if name in node.inputs:
            return node.inputs[name]
    raise ValueError(f"Required shader input missing: node={node.name}, names={names}")


def layout_shader_node_tree(node_tree: bpy.types.NodeTree) -> None:
    """Arrange shader nodes in connected lanes and reusable semantic frames."""
    frame_marker = "endfield_layout_frame"
    nodes = [node for node in node_tree.nodes if node.type != "FRAME"]
    if not nodes:
        return
    for node in nodes:
        if node.label.strip():
            continue
        if node.type in {"MATH", "VECT_MATH"}:
            operation = node.bl_rna.properties["operation"].enum_items[node.operation].name
            node.label = f"{'Vector' if node.type == 'VECT_MATH' else 'Math'}: {operation}"
        elif node.type == "OUTPUT_MATERIAL":
            node.label = {
                "CYCLES": "Cycles Output",
                "EEVEE": "Eevee Output",
                "ALL": "All Engines Output",
            }[node.target]
        elif node.type == "GROUP_OUTPUT":
            node.label = "Group Outputs"
        elif node.type == "GROUP_INPUT":
            node.label = "Group Inputs"

    def node_size(node: bpy.types.Node) -> tuple[float, float]:
        visible_inputs = sum(
            3 if socket.type == "VECTOR" and not socket.is_linked and not socket.hide_value else 1
            for socket in node.inputs if not socket.hide
        )
        visible_outputs = sum(not socket.hide for socket in node.outputs)
        socket_height = 80.0 + 28.0 * max(visible_inputs, visible_outputs)
        panel_height = {"TEX_IMAGE": 340.0, "RGB": 340.0, "VALTORGB": 300.0,
                        "CURVE_RGB": 380.0, "CURVE_VEC": 380.0, "NORMAL_MAP": 240.0,
                        "BUMP": 260.0, "MAPPING": 380.0}.get(node.type, 0.0)
        preview_height = 160.0 if node.show_preview else 0.0
        height = 80.0 if node.hide else max(160.0, socket_height, panel_height) + preview_height
        return max(float(node.width), float(node.dimensions.x), 140.0), max(
            height, float(node.height), float(node.dimensions.y)
        )

    outgoing: dict[str, list[tuple[str, str]]] = defaultdict(list)
    for link in node_tree.links:
        outgoing[link.from_node.name].append(
            (link.to_node.name, link.to_socket.name)
        )

    node_by_name = {node.name: node for node in nodes}
    output_names = {
        node.name for node in nodes
        if node.type == "GROUP_OUTPUT" or node.type.startswith("OUTPUT_")
    }
    depth_cache: dict[str, int] = {}

    def distance_to_output(name: str, trail: frozenset[str]) -> int:
        if name in depth_cache:
            return depth_cache[name]
        if name in trail or name in output_names or not outgoing.get(name):
            return 0
        value = max(
            1 + distance_to_output(target, trail | {name})
            for target, _socket in outgoing[name]
        )
        depth_cache[name] = value
        return value

    socket_lanes = {
        "Base Color": 720.0,
        "Metallic": 360.0,
        "Roughness": 120.0,
        "Normal": -280.0,
        "Emission Color": -620.0,
        "Emission": -620.0,
        "Alpha": -900.0,
    }
    lane_cache: dict[str, float] = {}

    def socket_purpose(socket_name: str) -> str:
        name = socket_name.casefold()
        if "normal" in name:
            return "Normal"
        if "emiss" in name:
            return "Emission"
        if "color" in name or "albedo" in name or "alpha" in name:
            return "Color"
        if "rough" in name or "metal" in name or "surface" in name or "bsdf" in name:
            return "Surface"
        return "Shared"

    def socket_lane(socket_name: str) -> float:
        purpose = socket_purpose(socket_name)
        return {"Color": 720.0, "Surface": 120.0, "Normal": -280.0,
                "Emission": -620.0, "Shared": -1120.0}[purpose]

    def terminal_lane(name: str, trail: frozenset[str]) -> float:
        if name in lane_cache:
            return lane_cache[name]
        if name in trail:
            return -1120.0
        lanes = []
        for target_name, socket_name in outgoing.get(name, []):
            target = node_by_name[target_name]
            if target.type == "BSDF_PRINCIPLED":
                lanes.append(socket_lanes.get(socket_name, 0.0))
            elif target.type == "GROUP_OUTPUT":
                lanes.append(socket_lane(socket_name))
            else:
                lanes.append(terminal_lane(target_name, trail | {name}))
        value = sum(lanes) / len(lanes) if lanes else (0.0 if name in output_names else -1120.0)
        lane_cache[name] = value
        return value

    purpose_cache: dict[str, frozenset[str]] = {}

    def terminal_purposes(name: str, trail: frozenset[str]) -> frozenset[str]:
        if name in purpose_cache:
            return purpose_cache[name]
        if name in trail:
            return frozenset({"Shared"})
        purposes: set[str] = set()
        for target_name, socket_name in outgoing.get(name, []):
            target = node_by_name[target_name]
            if target.type in {"BSDF_PRINCIPLED", "GROUP_OUTPUT"}:
                purposes.add(socket_purpose(socket_name))
            elif target.name in output_names:
                purposes.add("Surface")
            else:
                purposes.update(terminal_purposes(target_name, trail | {name}))
        result = frozenset(purposes)
        purpose_cache[name] = result
        return result

    def section_for(node: bpy.types.Node) -> str:
        explicit = node.get("endfield_layout_section")
        if explicit is not None and not isinstance(explicit, str):
            raise TypeError(f"Layout section must be a string: tree={node_tree.name}, node={node.name}")
        if explicit and explicit.strip():
            return " ".join(explicit.split())
        if node.name in output_names:
            return "Engine Outputs"
        if node.type == "GROUP_INPUT":
            return "Group Inputs"
        purposes = terminal_purposes(node.name, frozenset())
        purpose = next(iter(purposes)) if len(purposes) == 1 else "Shared"
        if len(purposes) != 1:
            label = node.label.casefold()
            for word, category in (("normal", "Normal"), ("emiss", "Emission"),
                                   ("albedo", "Color"), ("color", "Color"),
                                   ("rough", "Surface")):
                if word in label:
                    purpose = category
                    break
        if node.type.startswith("TEX_") or node.type in {"MAPPING", "UVMAP", "ATTRIBUTE", "VERTEX_COLOR"}:
            return f"Texture Inputs - {purpose}"
        if node.type.startswith("BSDF_") or node.type in {"MIX_SHADER", "ADD_SHADER"}:
            return "Surface"
        return {"Color": "Surface Color", "Surface": "Surface",
                "Normal": "Normal", "Emission": "Emission", "Shared": "Shared"}[purpose]

    columns: dict[int, list[bpy.types.Node]] = defaultdict(list)
    for node in nodes:
        if node.parent is not None and not node.parent.get(frame_marker, False):
            continue
        columns[distance_to_output(node.name, frozenset())].append(node)
    if not columns:
        return
    deepest = max(columns)
    for node in nodes:
        if node.type == "GROUP_INPUT" and not outgoing.get(node.name) and node in columns[0]:
            columns[0].remove(node)
            columns[deepest + 1].append(node)

    existing_frames: dict[str, bpy.types.Node] = {}
    for frame in node_tree.nodes:
        if frame.type != "FRAME" or not frame.get(frame_marker, False):
            continue
        key = str(frame.get("endfield_layout_key", ""))
        if key in existing_frames:
            raise ValueError(f"Duplicate generated layout frame: tree={node_tree.name}, key={key}")
        existing_frames[key] = frame

    depth_widths = {
        depth: max(node_size(node)[0] for node in column) + 52.0
        for depth, column in columns.items() if column
    }
    column_x: dict[int, float] = {}
    manual_right = max(
        (
            float(node.location_absolute.x) + (
                float(node.width) if node.type == "FRAME" else node_size(node)[0]
            )
            for node in node_tree.nodes
            if (node.type == "FRAME" and not node.get(frame_marker, False))
            or (node.type != "FRAME" and node.parent is not None
                and not node.parent.get(frame_marker, False))
        ),
        default=-160.0,
    )
    cursor_x = max(0.0, manual_right + 160.0)
    for depth in sorted(depth_widths, reverse=True):
        column_x[depth] = cursor_x
        cursor_x += depth_widths[depth] + 110.0

    sections: dict[str, dict[int, list[bpy.types.Node]]] = defaultdict(dict)
    for depth, column in columns.items():
        for node in column:
            section = section_for(node)
            sections[section].setdefault(depth, []).append(node)
    section_order = sorted(
        sections,
        key=lambda section: (
            -max(terminal_lane(node.name, frozenset())
                 for column in sections[section].values() for node in column),
            section,
        ),
    )
    used_keys: set[str] = set()
    cursor_y = 950.0
    for section in section_order:
        by_depth = sections[section]
        notes: set[str] = set()
        for column in by_depth.values():
            for node in column:
                note = node.get("endfield_layout_note")
                if note is not None and not isinstance(note, str):
                    raise TypeError(f"Layout note must be a string: tree={node_tree.name}, node={node.name}")
                if note and note.strip():
                    notes.add(" ".join(note.split()))
        default_notes = {
            "Group Inputs": "Group parameters and source values.",
            "Engine Outputs": "Final Blender engine connections.",
            "Surface Color": "Base color and tint operations.",
            "Surface": "Surface shading and response.",
            "Normal": "Normal decoding and direction.",
            "Emission": "Emission color and strength.",
            "Shared": "Values reused across branches.",
        }
        note_text = "; ".join(sorted(notes)) if notes else (
            "Textures and UV coordinates." if section.startswith("Texture Inputs")
            else default_notes.get(section, "Connected shader operations.")
        )
        frame_label = f"{section} | {note_text}"
        used_keys.add(section)
        frame = existing_frames.get(section)
        if frame is None:
            frame = node_tree.nodes.new("NodeFrame")
            frame[frame_marker] = True
            frame["endfield_layout_key"] = section
            existing_frames[section] = frame
        frame.name = f"EF Layout {section}"
        frame.label = frame_label
        if frame.label != frame_label:
            raise ValueError(f"Layout annotation exceeds Blender's label limit: tree={node_tree.name}, section={section}, label={frame_label}")
        frame.label_size = 20
        frame.shrink = False
        frame_left = min(column_x[depth] for depth in by_depth)
        frame.width = max(
            max(column_x[depth] + depth_widths[depth] for depth in by_depth) - frame_left,
            56.0 + 11.0 * len(frame_label),
        )
        frame.height = max(
            82.0 + sum(node_size(node)[1] for node in column) + 32.0 * (len(column) - 1)
            for column in by_depth.values()
        )
        desired_y = max(
            terminal_lane(node.name, frozenset())
            for column in by_depth.values() for node in column
        ) + 90.0
        frame_y = min(cursor_y, desired_y)
        frame.location = (frame_left, frame_y)
        for depth, column in by_depth.items():
            node_y = -56.0
            for node in sorted(column, key=lambda current: (
                    -terminal_lane(current.name, frozenset()),
                    current.type, current.name.casefold())):
                node.parent = frame
                node.location = (column_x[depth] - frame_left + 26.0, node_y)
                node_y -= node_size(node)[1] + 32.0
        cursor_y = frame_y - frame.height - 64.0
    for key, frame in existing_frames.items():
        if key not in used_keys and not any(node.parent == frame for node in node_tree.nodes):
            node_tree.nodes.remove(frame)


def layout_shader_node_trees(node_trees: tuple[bpy.types.NodeTree, ...]) -> int:
    """Arrange reachable shader groups once, including nested shared groups."""
    pending = list(node_trees)
    visited: set[int] = set()
    while pending:
        tree = pending.pop()
        identity = tree.as_pointer()
        if identity in visited:
            continue
        visited.add(identity)
        pending.extend(node.node_tree for node in tree.nodes
                       if node.type == "GROUP" and node.node_tree is not None)
        layout_shader_node_tree(tree)
    return len(visited)


def _texture_node(nodes: bpy.types.Nodes, links: bpy.types.NodeLinks,
                  image: bpy.types.Image, prop: str, slot: TextureSlot) -> bpy.types.Node:
    texture = nodes.new("ShaderNodeTexImage")
    texture.image = image
    texture.label = prop
    scale = slot.get("Scale") or [1.0, 1.0]
    offset = slot.get("Offset") or [0.0, 0.0]
    if tuple(scale[:2]) != (1.0, 1.0) or tuple(offset[:2]) != (0.0, 0.0):
        uv = nodes.new("ShaderNodeTexCoord")
        mapping = nodes.new("ShaderNodeMapping")
        mapping.label = f"{prop} scale/offset"
        mapping.inputs["Scale"].default_value[:2] = scale[:2]
        mapping.inputs["Location"].default_value[:2] = offset[:2]
        links.new(uv.outputs["UV"], mapping.inputs["Vector"])
        links.new(mapping.outputs["Vector"], texture.inputs["Vector"])
    return texture


def create_material(record: MaterialRecord, texture_files: dict[tuple[str, int], tuple[Path, str]],
                    pack_textures: bool) -> bpy.types.Material:
    family = surface_family(record)
    layout = normal_layout(record)
    result = bpy.data.materials.new(record["Name"])
    result.use_nodes = True
    nodes = result.node_tree.nodes
    links = result.node_tree.links
    principled = next(node for node in nodes if node.type == "BSDF_PRINCIPLED")
    colors = record.get("Colors") or {}
    floats = record.get("Floats") or {}
    base_color = colors.get("_BaseColor", [1.0, 1.0, 1.0, 1.0])
    _input(principled, "Base Color").default_value = base_color
    _input(principled, "Metallic").default_value = min(1.0, max(0.0, floats.get("_Metallic", 0.0)))
    roughness = floats.get("_Roughness", floats.get("_RoughnessMax", 0.65))
    _input(principled, "Roughness").default_value = min(1.0, max(0.0, roughness))
    specular = _input(principled, "Specular IOR Level", "Specular")
    if specular is not None:
        specular.default_value = min(1.0, max(0.0, floats.get("_Specular", 0.5)))
    base_names = {"_BaseColorMap", "_BaseMap", "_MainTex"}
    normal_names = {"_NormalMap", "_BumpMap"}
    mro_names = {"_MROMap"}
    emissive_names = {"_EmissiveMap"}
    # Many Endfield opaque textures use their alpha channel for packed masks,
    # not surface opacity. Linking alpha unconditionally makes whole buildings
    # vanish in Blender material preview. Only authored alpha-test or
    # alpha-blend shaders may drive the Principled alpha input.
    alpha_test = floats.get("_EnableAlphaTest", 0.0) > 0.5
    alpha_blend = floats.get("_EnableAlphaBlend", 0.0) > 0.5
    surface_alpha = alpha_test or alpha_blend
    tri_channel_mask = floats.get("_EnableTriChannelMask", 0.0) > 0.5
    emission_selector = (emission_source(record) if family in ("lit", "trunk", "transparent") and
                         (floats.get("_EnableEmissiveMap", 0.0) > 0.5 or floats.get("_EnableBakedEmissive", 0.0) > 0.5)
                         else None)
    base_used = normal_used = mro_used = emissive_used = mask_used = False
    base_texture = None
    mask_texture = None
    for slot in supported_slots(record):
        ref = slot.get("Texture")
        texture_entry = texture_files.get(ref_key(ref)) if ref else None
        prop = slot["Property"]
        role = ("base" if prop in base_names and not base_used else
                "normal" if prop in normal_names and not normal_used else
                "mro" if prop in mro_names and not mro_used else
                "emissive" if prop in emissive_names and not emissive_used else None)
        if prop == "_MaskMap" and tri_channel_mask and not mask_used:
            role = "mask"
        if prop == "_NormalMap" and emission_selector in ("normal_blue", "normal_alpha") and not uses_normal_map(record):
            role = "emission_source"
        if prop == "_MROMap" and emission_selector == "mro_alpha" and not uses_mro(record):
            role = "emission_source"
        if role is None:
            continue
        if texture_entry is None:
            raise ValueError(f"Supported texture export missing: material={record['Name']}, property={prop}, reference={ref}")
        root, relative = texture_entry
        image_path = root / relative.replace("\\", "/")
        if not image_path.is_file():
            raise FileNotFoundError(image_path)
        color_space = ref.get("ColorSpace") if ref else None
        if role == "emissive" and color_space is not None and (type(color_space) is not int or color_space not in (0, 1)):
            raise ValueError(f"Invalid emissive texture ColorSpace: material={record['Name']}, property={prop}, value={color_space}")
        colorspace = ("Non-Color" if color_space == 0 else "sRGB") if role == "emissive" else (
            "sRGB" if role == "base" else "Non-Color")
        if role == "emissive" and color_space is None:
            result["endfield_emissive_color_space_status"] = "color_space_unverified_legacy_srgb"
        elif role == "emissive":
            result["endfield_emissive_color_space_status"] = "source_linear" if color_space == 0 else "source_srgb"
        alpha_mode = "STRAIGHT" if role == "base" and surface_alpha else "CHANNEL_PACKED"
        image = texture_image(image_path, colorspace, alpha_mode)
        if pack_textures and not image.packed_file:
            image.pack()
        texture = _texture_node(nodes, links, image, prop, slot)
        if role == "base":
            image.colorspace_settings.name = "sRGB"
            # Endfield commonly stores masks in the alpha channel of otherwise
            # opaque base-color PNGs. Blender's default straight/premultiplied
            # handling can multiply RGB by an all-zero alpha and turn the
            # surface black even when Alpha is not connected.
            image.alpha_mode = "STRAIGHT" if surface_alpha else "CHANNEL_PACKED"
            base_texture = texture
            # Keep authored albedo connected even when an optional mask is unbound.
            # A bound tri-channel mask replaces this input later.
            if tuple(base_color) != (1.0, 1.0, 1.0, 1.0):
                tint = nodes.new("ShaderNodeMixRGB")
                tint.blend_type = "MULTIPLY"
                tint.inputs[0].default_value = 1.0
                tint.inputs[2].default_value = base_color
                links.new(texture.outputs["Color"], tint.inputs[1])
                links.new(tint.outputs["Color"], _input(principled, "Base Color"))
            else:
                links.new(texture.outputs["Color"], _input(principled, "Base Color"))
            if surface_alpha:
                links.new(texture.outputs["Alpha"], _input(principled, "Alpha"))
            base_used = True
        elif role == "mask":
            image.colorspace_settings.name = "Non-Color"
            image.alpha_mode = "CHANNEL_PACKED"
            mask_texture = texture
            mask_used = True
        elif role == "emission_source":
            image.colorspace_settings.name = "Non-Color"
            image.alpha_mode = "CHANNEL_PACKED"
        elif role == "normal":
            image.colorspace_settings.name = "Non-Color"
            image.alpha_mode = "CHANNEL_PACKED"
            normal = nodes.new("ShaderNodeNormalMap")
            normal.inputs["Strength"].default_value = max(0.0, floats.get("_NormalScale", 1.0))
            packed_nro = family == "unverified" and bool(ref and str(ref.get("Name", "")).casefold().endswith("_nro"))
            if layout != "rgb" or packed_nro:
                decode = nodes.new("ShaderNodeGroup")
                decode.node_tree = packed_normal_group()
                decode.label = "HGRP packed normal and roughness"
                decode.inputs["Multiply Red By Alpha"].default_value = float(layout == "rag")
                decode.inputs["Normal Strength"].default_value = max(0.0, floats.get("_NormalScale", 1.0))
                normal.inputs["Strength"].default_value = 1.0
                links.new(texture.outputs["Color"], decode.inputs["Packed RGB"])
                links.new(texture.outputs["Alpha"], decode.inputs["Packed Alpha"])
                links.new(decode.outputs["Encoded Normal"], normal.inputs["Color"])
                if not uses_mro(record) or packed_nro:
                    if family == "unverified":
                        links.new(decode.outputs["Blue"], _input(principled, "Roughness"))
                    else:
                        remap_roughness(result, decode.outputs["Blue"], _input(principled, "Roughness"), record)
                result["endfield_packed_nro_reconstruction"] = "Shader-directed normal XY; Z reconstructed before XY strength; authored roughness range"
            else:
                links.new(texture.outputs["Color"], normal.inputs["Color"])
            links.new(normal.outputs["Normal"], _input(principled, "Normal"))
            normal_used = True
        elif role == "mro":
            image.colorspace_settings.name = "Non-Color"
            image.alpha_mode = "CHANNEL_PACKED"
            separate = nodes.new("ShaderNodeSeparateColor")
            separate.label = "MRO: R metallic, G roughness, B occlusion"
            links.new(texture.outputs["Color"], separate.inputs["Color"])
            links.new(separate.outputs["Red"], _input(principled, "Metallic"))
            if family == "unverified":
                links.new(separate.outputs["Green"], _input(principled, "Roughness"))
            else:
                remap_roughness(result, separate.outputs["Green"], _input(principled, "Roughness"), record)
            mro_used = True
        elif role == "emissive" and (floats.get("_EnableEmissiveMap", 0.0) > 0.5 or
                                      floats.get("_EnableBakedEmissive", 0.0) > 0.5):
            emission_color = colors.get("_EmissiveColor", [1.0, 1.0, 1.0, 1.0])
            tint = nodes.new("ShaderNodeMixRGB")
            tint.blend_type = "MULTIPLY"
            tint.inputs[0].default_value = 1.0
            tint.inputs[2].default_value = emission_color
            links.new(texture.outputs["Color"], tint.inputs[1])
            links.new(tint.outputs["Color"], _input(principled, "Emission Color", "Emission"))
            _input(principled, "Emission Strength").default_value = max(
                0.0, floats.get("_EmissiveIntensity", floats.get("_EmissiveRemap", 1.0)))
            emissive_used = True
    if tri_channel_mask and base_texture is not None and mask_texture is not None:
        # Endfield's common environment shader uses the mask RGB channels to
        # replace the global BaseColor tint with three authored linear colours.
        # The channels are normally mutually exclusive; sequential mixes also
        # remain stable on softly blended mask edges.
        separate = nodes.new("ShaderNodeSeparateColor")
        separate.label = "Endfield tri-channel albedo mask"
        links.new(mask_texture.outputs["Color"], separate.inputs["Color"])
        current = None
        base_tint = nodes.new("ShaderNodeRGB")
        base_tint.label = "Endfield _BaseColor"
        base_tint.outputs[0].default_value = base_color
        current = base_tint.outputs[0]
        for channel, color_name in (
                ("Red", "_MaskAlbedoR"),
                ("Green", "_MaskAlbedoG"),
                ("Blue", "_MaskAlbedoB")):
            tint = colors.get(color_name, base_color)
            mix = nodes.new("ShaderNodeMixRGB")
            mix.label = f"Endfield {color_name}"
            mix.blend_type = "MIX"
            strength = float(tint[3]) if len(tint) > 3 else 1.0
            if strength < 0.9999:
                factor = nodes.new("ShaderNodeMath")
                factor.label = f"Endfield {color_name} strength"
                factor.operation = "MULTIPLY"
                factor.inputs[1].default_value = max(0.0, min(1.0, strength))
                links.new(separate.outputs[channel], factor.inputs[0])
                links.new(factor.outputs[0], mix.inputs[0])
            else:
                links.new(separate.outputs[channel], mix.inputs[0])
            links.new(current, mix.inputs[1])
            mix.inputs[2].default_value = (*tint[:3], 1.0)
            current = mix.outputs[0]
        albedo = nodes.new("ShaderNodeMixRGB")
        albedo.label = "Endfield base albedo x tri-channel tint"
        albedo.blend_type = "MULTIPLY"
        albedo.inputs[0].default_value = 1.0
        links.new(base_texture.outputs["Color"], albedo.inputs[1])
        links.new(current, albedo.inputs[2])
        links.new(albedo.outputs[0], _input(principled, "Base Color"))
        result["endfield_tri_channel_mask_reconstruction"] = (
            "BaseColorMap multiplied by sequential R/G/B authored albedo tints, "
            "using each tint alpha as its authored blend strength"
        )
    if family != "unverified":
        if family in ("lit", "trunk", "transparent") and base_texture_mode(record) == 1 and base_texture is not None:
            links.new(base_texture.outputs["Alpha"], _input(principled, "Metallic"))
        if family in ("leaf", "card") and not normal_used:
            _input(principled, "Roughness").default_value = (floats.get("_RoughnessMin", 0.0) + floats.get("_RoughnessMax", 1.0)) * 0.5
        apply_authored_albedo(result, principled, record)
    if alpha_test:
        threshold = min(1.0, max(0.0, floats.get("_AlphaClipThreshold", 0.5)))
        alpha = _input(principled, "Alpha")
        if alpha.links:
            source = alpha.links[0].from_socket
            links.remove(alpha.links[0])
            clip = nodes.new("ShaderNodeMath")
            clip.operation = "GREATER_THAN"
            clip.inputs[1].default_value = threshold
            links.new(source, clip.inputs[0])
            links.new(clip.outputs[0], alpha)
        if hasattr(result, "surface_render_method"):
            result.surface_render_method = "DITHERED"
        result["endfield_alpha_clip_threshold"] = threshold
    if alpha_blend:
        if hasattr(result, "surface_render_method"):
            result.surface_render_method = "DITHERED"
        result["endfield_alpha_blend"] = True
    result["endfield_material_source"] = f'{record.get("Source", "unknown")}:{record.get("PathId", 0)}'
    result["endfield_mro_interpretation"] = "R=metallic, G=roughness, B=occlusion (B retained in node graph only)"
    apply_emission_mask(result, record)
    result["endfield_surface_contract"] = "EndfieldStaticSurface/1"
    result["endfield_surface_fidelity"] = "approximation_not_game_shader"
    result["endfield_shader_reference"] = json.dumps(record["Shader"])
    if "RenderState" in record:
        result["endfield_original_render_state"] = json.dumps(record["RenderState"])
    result["endfield_render_pipeline_status"] = "source_metadata_retained_pipeline_not_reconstructed"
    if family != "unverified":
        apply_engine_surface(result, principled, record)
    layout_shader_node_trees((result.node_tree,))
    result["endfield_node_layout"] = (
        "EndfieldNodeLayout/2: connected lanes with semantic frames and notes"
    )
    return result


def _emission_input(principled: bpy.types.Node | None) -> bpy.types.NodeSocket | None:
    if principled is None:
        return None
    for name in ("Emission Color", "Emission"):
        socket = principled.inputs.get(name)
        if socket is not None:
            return socket
    return None

def _mask_socket(nodes: bpy.types.Nodes, links: bpy.types.NodeLinks,
                 emissive_texture: bpy.types.Node, channel: int) -> bpy.types.NodeSocket | None:
    if channel == 3:
        return emissive_texture.outputs.get("Alpha")
    separate = nodes.new("ShaderNodeSeparateColor")
    separate.label = f"Endfield emissive mask channel {channel}"
    links.new(emissive_texture.outputs["Color"], separate.inputs["Color"])
    return {
        0: separate.outputs.get("Red"),
        1: separate.outputs.get("Green"),
        2: separate.outputs.get("Blue"),
    }.get(channel)


def _tinted_emission(nodes: bpy.types.Nodes, links: bpy.types.NodeLinks,
                     mask: bpy.types.NodeSocket, color: list[float],
                     label: str) -> bpy.types.NodeSocket:
    tint = nodes.new("ShaderNodeRGB")
    tint.label = label
    tint.outputs[0].default_value = (*color[:3], 1.0)
    multiply = nodes.new("ShaderNodeMixRGB")
    multiply.blend_type = "MULTIPLY"
    multiply.inputs[0].default_value = 1.0
    multiply.label = label + " mask"
    links.new(tint.outputs[0], multiply.inputs[1])
    links.new(mask, multiply.inputs[2])
    return multiply.outputs["Color"]


def _apply_verified_emission(material: bpy.types.Material, record: MaterialRecord) -> bpy.types.Material:
    source = emission_source(record)
    source_property = {"emissive_map": "_EmissiveMap", "base_alpha": "_BaseColorMap",
                       "normal_blue": "_NormalMap", "normal_alpha": "_NormalMap",
                       "mro_alpha": "_MROMap"}.get(source)
    if source_property is not None and not any(
        slot["Property"] == source_property and slot["Texture"] is not None for slot in record["Textures"]
    ):
        material["endfield_emissive_source"] = source
        material["endfield_emissive_mask_status"] = "deferred_authored_unbound_source"
        material["endfield_emissive_missing_property"] = source_property
        return material
    colors = record.get("Colors") or {}
    authored = colors.get("_EmissiveColor")
    if authored is None or len(authored) < 3:
        raise ValueError(f"Verified emission lacks _EmissiveColor: material={record['Name']}")
    nodes = material.node_tree.nodes
    links = material.node_tree.links
    principled = next((node for node in nodes if node.type == "BSDF_PRINCIPLED"), None)
    emission = _emission_input(principled)
    if emission is None or principled.inputs.get("Emission Strength") is None:
        raise ValueError(f"Verified emission lacks Principled socket: material={record['Name']}")
    for link in list(emission.links):
        links.remove(link)
    floats = record["Floats"]
    strength = floats.get("_EmissiveIntensity", 1.0)
    _input(principled, "Emission Strength").default_value = max(0.0, float(strength))
    if floats.get("_EmissiveRemap", 1.0) != 1.0:
        material["endfield_emissive_remap_status"] = "authored_remap_not_reconstructed"
    material["endfield_emissive_source"] = source
    material["endfield_emissive_mask_channel"] = int(floats["_EmissiveMaskChannel"])
    material["endfield_emissive_mask_contract"] = "Lit/Trunk/LitTransparent serialized source selector"
    if source == "constant":
        emission.default_value = (*authored[:3], 1.0)
        material["endfield_emissive_mask_status"] = "applied_constant_no_texture"
    else:
        prop, component = {
            "emissive_map": ("_EmissiveMap", "Red"),
            "base_alpha": ("_BaseColorMap", "Alpha"),
            "normal_blue": ("_NormalMap", "Blue"),
            "normal_alpha": ("_NormalMap", "Alpha"),
            "mro_alpha": ("_MROMap", "Alpha"),
        }[source]
        texture = next((node for node in nodes if node.type == "TEX_IMAGE" and node.label == prop), None)
        if texture is None or texture.image is None:
            raise ValueError(f"Verified emission lacks source node: material={record['Name']}, property={prop}")
        if component == "Alpha":
            mask = texture.outputs["Alpha"]
        else:
            separate = nodes.new("ShaderNodeSeparateColor")
            separate.label = f"{prop} emissive channels"
            links.new(texture.outputs["Color"], separate.inputs["Color"])
            mask = separate.outputs[component]
        result = _tinted_emission(nodes, links, mask, authored, "Endfield _EmissiveColor")
        if source == "emissive_map" and emissive_map_type(record) == 1:
            for component, color_name in (("Green", "_EmissiveColorG"),
                                          ("Blue", "_EmissiveColorB"),
                                          ("Alpha", "_EmissiveColorA")):
                extra = colors.get(color_name)
                if extra is None or len(extra) < 3:
                    raise ValueError(f"RGBA emission lacks {color_name}: material={record['Name']}")
                channel = texture.outputs["Alpha"] if component == "Alpha" else separate.outputs[component]
                contribution = _tinted_emission(nodes, links, channel, extra, f"Endfield {color_name}")
                add = nodes.new("ShaderNodeMixRGB")
                add.blend_type = "ADD"
                add.use_clamp = False
                add.inputs[0].default_value = 1.0
                links.new(result, add.inputs[1])
                links.new(contribution, add.inputs[2])
                result = add.outputs["Color"]
        links.new(result, emission)
        material["endfield_emissive_mask_status"] = "applied_source_channel_static_approximation"
    if floats.get("_EnableEmissiveAnimSweep", 0.0) > 0.5:
        material["endfield_emissive_motion_status"] = "authored_sweep_not_reconstructed"
    if source == "constant" and floats.get("_EmissiveNoMapUseFresnel", 0.0) > 0.5:
        material["endfield_emissive_fresnel_status"] = "authored_fresnel_not_reconstructed"
    return material

def apply_emission_mask(material: bpy.types.Material, record: MaterialRecord) -> bpy.types.Material:
    if has_procedural_foliage_emission(record):
        material["endfield_emissive_mask_status"] = "deferred_procedural_foliage_glow"
        return material
    floats = record.get("Floats") or {}
    enabled = (
        float(floats.get("_EnableEmissiveMap", 0.0)) > 0.5
        or float(floats.get("_EnableBakedEmissive", 0.0)) > 0.5
    )
    if not enabled:
        return material
    family = surface_family(record)
    if family in ("lit", "trunk", "transparent"):
        return _apply_verified_emission(material, record)
    if family in ("leaf", "card"):
        material["endfield_emissive_mask_status"] = "deferred_foliage_source_enum"
        return material
    if any(slot["Property"] == "_EmissiveMap" and slot["Texture"] is None for slot in record["Textures"]):
        return material
    raw_channel = floats.get("_EmissiveMaskChannel")
    if raw_channel is None:
        return material
    if raw_channel not in (0, 1, 2, 3):
        raise ValueError(f"Unsupported emission mask channel: material={record['Name']}, channel={raw_channel}")
    channel = int(raw_channel)

    nodes = material.node_tree.nodes
    links = material.node_tree.links
    principled = next((node for node in nodes if node.type == "BSDF_PRINCIPLED"), None)
    emission = _emission_input(principled)
    emissive_texture = next(
        (node for node in nodes if node.type == "TEX_IMAGE" and node.label == "_EmissiveMap"),
        None,
    )
    if emissive_texture is None or emission is None:
        raise ValueError(f"Enabled emission mask lacks texture or socket: material={record['Name']}")

    # The selected component is authored as data, not display colour. Isolate
    # the image datablock before changing colorspace because Blender normally
    # reuses a loaded image across materials, and another material may consume
    # the same file as authored RGB emission.
    if emissive_texture.image is not None:
        mask_image = emissive_texture.image.copy()
        mask_image.name = f"{emissive_texture.image.name}__EndfieldEmissionMask"
        mask_image.colorspace_settings.name = "Non-Color"
        mask_image.alpha_mode = "CHANNEL_PACKED"
        emissive_texture.image = mask_image
    mask = _mask_socket(nodes, links, emissive_texture, channel)
    if mask is None:
        raise ValueError(f"Emission mask channel unavailable: material={record['Name']}, channel={channel}")

    for link in list(emission.links):
        links.remove(link)
    authored_tint = nodes.new("ShaderNodeRGB")
    authored_tint.label = "Endfield authored EmissiveColor"
    color = (record.get("Colors") or {}).get("_EmissiveColor", [1.0, 1.0, 1.0, 1.0])
    authored_tint.outputs[0].default_value = (*color[:3], 1.0)
    multiply = nodes.new("ShaderNodeMixRGB")
    multiply.blend_type = "MULTIPLY"
    multiply.label = "Endfield emissive single-channel mask"
    multiply.inputs[0].default_value = 1.0
    links.new(authored_tint.outputs[0], multiply.inputs[1])
    links.new(mask, multiply.inputs[2])
    links.new(multiply.outputs["Color"], emission)
    material["endfield_emissive_mask_channel"] = channel
    material["endfield_emissive_mask_status"] = "applied_single_channel"
    material["endfield_emissive_mask_contract"] = "0=R,1=G,2=B,3=A"
    return material
