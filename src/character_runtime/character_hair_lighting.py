from __future__ import annotations

"""Build source-selected Hair normals and strand highlights without enabling disabled maps."""

from dataclasses import asdict, dataclass
import json
import math
from collections.abc import Mapping
from typing import Literal

import bpy


@dataclass(frozen=True, slots=True)
class HairLineMapBinding:
    file_id: int
    path_id: int
    scale: tuple[float, float]
    offset: tuple[float, float]

    def validate(self) -> None:
        if isinstance(self.file_id, bool) or not isinstance(self.file_id, int):
            raise TypeError("Source _LineMap FileID must be an integer")
        if isinstance(self.path_id, bool) or not isinstance(self.path_id, int):
            raise TypeError("Source _LineMap PathID must be an integer")
        if self.path_id == 0:
            raise ValueError("Source _LineMap must reference a nonzero PathID")
        if len(self.scale) != 2 or len(self.offset) != 2:
            raise ValueError("Source _LineMap scale and offset must each contain two components")
        if any(not math.isfinite(value) for value in (*self.scale, *self.offset)):
            raise ValueError("Source _LineMap scale and offset must be finite")


def line_map_binding_from_source_slot(source_slot: Mapping[str, object]) -> HairLineMapBinding:
    """Parse the exact texture reference and UV transform from a Unity material slot."""
    texture = _mapping(source_slot.get("m_Texture"), "_LineMap.m_Texture")
    scale_raw = _mapping(source_slot.get("m_Scale"), "_LineMap.m_Scale")
    offset_raw = _mapping(source_slot.get("m_Offset"), "_LineMap.m_Offset")
    file_id = texture.get("m_FileID")
    path_id = texture.get("m_PathID")
    scale_values = (scale_raw.get("x"), scale_raw.get("y"))
    offset_values = (offset_raw.get("x"), offset_raw.get("y"))
    if isinstance(file_id, bool) or not isinstance(file_id, int):
        raise TypeError(f"Source _LineMap FileID must be an integer, got {file_id!r}")
    if isinstance(path_id, bool) or not isinstance(path_id, int):
        raise TypeError(f"Source _LineMap PathID must be an integer, got {path_id!r}")
    if any(isinstance(value, bool) or not isinstance(value, (int, float)) for value in (*scale_values, *offset_values)):
        raise TypeError("Source _LineMap scale and offset must contain numeric x/y components")
    binding = HairLineMapBinding(
        file_id=file_id,
        path_id=path_id,
        scale=(float(scale_values[0]), float(scale_values[1])),
        offset=(float(offset_values[0]), float(offset_values[1])),
    )
    binding.validate()
    return binding


def line_map_binding_from_material(material_data: Mapping[str, object]) -> HairLineMapBinding:
    """Find and parse the named _LineMap slot from embedded material TypeTree data."""
    saved = _mapping(material_data.get("m_SavedProperties"), "m_SavedProperties")
    texture_slots = saved.get("m_TexEnvs")
    if not isinstance(texture_slots, list):
        raise TypeError("Source material m_TexEnvs must be an array")
    matches = tuple(
        row for row in texture_slots
        if isinstance(row, Mapping) and row.get("Key") == "_LineMap"
    )
    if len(matches) != 1:
        raise ValueError(f"Expected one source _LineMap slot, found {len(matches)}")
    slot_value = _mapping(matches[0].get("Value"), "_LineMap slot Value")
    return line_map_binding_from_source_slot(slot_value)


def bind_source_line_map(tree: bpy.types.NodeTree, binding: HairLineMapBinding) -> str:
    """Attach an explicit source-slot identity to the existing packed LineMap image node."""
    binding.validate()
    node = _find_image_node(tree, "_LineMap")
    if node.image is None or node.image.packed_file is None:
        raise ValueError(f"Source _LineMap image must be packed in {tree.name}")
    if node.image.colorspace_settings.name != "Non-Color":
        raise ValueError(f"Source _LineMap image must use Non-Color data in {tree.name}")
    for key, expected in (("source_file_id", binding.file_id), ("source_path_id", binding.path_id)):
        existing = node.get(key)
        if existing is not None and str(existing) != str(expected):
            raise ValueError(f"Source _LineMap image {key} mismatch in {tree.name}: image={existing!r}, slot={expected}")
    node["source_file_id"] = str(binding.file_id)
    node["source_path_id"] = str(binding.path_id)
    return node.name


def _mapping(value: object, context: str) -> Mapping[str, object]:
    if not isinstance(value, Mapping):
        raise TypeError(f"{context} must be an object")
    if not all(isinstance(key, str) for key in value):
        raise TypeError(f"{context} keys must all be strings")
    return value


@dataclass(frozen=True, slots=True)
class HairShaderFrame:
    source_x_in_blender_world: tuple[float, float, float]
    source_y_in_blender_world: tuple[float, float, float]
    source_z_in_blender_world: tuple[float, float, float]
    source_world_y_in_blender_world: tuple[float, float, float]
    frame_status: Literal["fbx_root_frame_proxy_unverified", "runtime_verified"]

    def validate(self) -> None:
        axes = (
            self.source_x_in_blender_world,
            self.source_y_in_blender_world,
            self.source_z_in_blender_world,
        )
        if any(len(axis) != 3 for axis in (*axes, self.source_world_y_in_blender_world)):
            raise ValueError("Hair source-axis bridge vectors must have three components")
        if any(not math.isfinite(value) for axis in (*axes, self.source_world_y_in_blender_world) for value in axis):
            raise ValueError("Hair source-axis bridge vectors must be finite")
        for index, axis in enumerate(axes):
            length_squared = sum(value * value for value in axis)
            if abs(length_squared - 1.0) > 1e-5:
                raise ValueError(f"Hair shader-frame axis {index} must be unit length, got squared length {length_squared}")
            for other in axes[index + 1:]:
                if abs(sum(axis[component] * other[component] for component in range(3))) > 1e-5:
                    raise ValueError("Hair shader-frame axes must be orthogonal")
        world_y_length_squared = sum(value * value for value in self.source_world_y_in_blender_world)
        if abs(world_y_length_squared - 1.0) > 1e-5:
            raise ValueError(f"Hair source world Y axis must be unit length, got squared length {world_y_length_squared}")
        if self.frame_status not in ("fbx_root_frame_proxy_unverified", "runtime_verified"):
            raise ValueError(f"Unsupported hair shader-frame status: {self.frame_status!r}")


@dataclass(frozen=True, slots=True)
class HairSpecularParameters:
    anisotropy_dir_x: float
    anisotropy_edge_fade: float
    anisotropy_intensity: float
    anisotropy_range2: float
    anisotropy_value: float
    anisotropy_value2: float
    anisotropy_color2: tuple[float, float, float, float]
    spec_bump_scale: float
    use_metallic_gloss_map: bool
    use_bump_map: bool
    use_spec_bump_map: bool
    use_spec_ramp_map: bool
    use_specular_line: bool
    use_line_map: bool
    line_amount: float
    line_intensity: float
    line_range: float
    line_saturation: float
    line_value: float
    line_map_file_id: int
    line_map_path_id: int
    line_map_scale: tuple[float, float]
    line_map_offset: tuple[float, float]
    shader_frame: HairShaderFrame

    def validate(self) -> None:
        scalars = (
            self.anisotropy_dir_x,
            self.anisotropy_edge_fade,
            self.anisotropy_intensity,
            self.anisotropy_range2,
            self.anisotropy_value,
            self.anisotropy_value2,
            self.spec_bump_scale,
            self.line_amount,
            self.line_intensity,
            self.line_range,
            self.line_saturation,
            self.line_value,
            *self.line_map_scale,
            *self.line_map_offset,
            *self.anisotropy_color2,
        )
        if any(not math.isfinite(value) for value in scalars):
            raise ValueError("Hair source shader parameters must all be finite")
        if len(self.anisotropy_color2) != 4:
            raise ValueError("_AnisotropyColor2 must contain four components")
        variant = (self.use_bump_map, self.use_spec_bump_map, self.use_metallic_gloss_map, self.use_spec_ramp_map)
        if variant not in ((True, True, True, True), (True, False, True, True), (False, False, False, False)):
            raise ValueError(f"Hair feature combination has no verified implementation: {variant}")
        if len(self.line_map_scale) != 2 or len(self.line_map_offset) != 2:
            raise ValueError("Source line-map scale and offset must each contain two components")
        if self.line_amount < 0.0:
            raise ValueError("Source _LineAmount must be nonnegative")
        if not 0.0 <= self.line_intensity <= 1.0:
            raise ValueError("Source _LineIntensity must be within [0, 1]")
        HairLineMapBinding(
            file_id=self.line_map_file_id,
            path_id=self.line_map_path_id,
            scale=self.line_map_scale,
            offset=self.line_map_offset,
        ).validate()
        self.shader_frame.validate()


@dataclass(frozen=True, slots=True)
class HairBranchReceipt:
    source_variant: str
    active_specular_lobes: int
    hair_tree: str
    light_tree: str
    specular_normal_texture: str
    specular_ramp_texture: str
    specular_enable_node: str
    source_exponents: tuple[int, int]
    specular_line_pass_ported: bool
    shader_frame: HairShaderFrame
    cast_shadow_preview: float | None
    cast_shadow_binding: str
    specular_normal_uv_map: str
    metallic_tangent_blend: bool
    metallic_tangent_handedness: str
    native_diffuse_group: str
    full_diffuse_output: str
    ambient_diffuse_intensity_output: str
    cast_blend_output: str
    cp13_w_constructor_value: float
    source_response_node: str
    source_response_value: float
    line_map_texture: str
    line_map_file_id: int
    line_map_path_id: int
    line_map_uv_scale: tuple[float, float]
    line_map_uv_offset: tuple[float, float]


def hair_variant(tree: bpy.types.NodeTree) -> str:
    """Classify only three compiled Hair variants with matching source switches and keywords."""
    if tree.get("source_shader") != "HGRP/CharacterNPR_Hair":
        raise ValueError(f"Expected a source Hair group: {tree.name}")
    floats = _mapping(json.loads(tree["source_floats"]), "Hair source floats")
    # Keywords are read from the exact source TypeTree matched by material identity.
    from patch_character_hgrp import source_keywords
    keywords = set(source_keywords(tree))
    names = ("_UseBumpMap", "_UseSpecBumpMap", "_UseMetallicGlossMap", "_UseSpecRampMap")
    tokens = ("_NORMALMAP", "_SPECULAR_NORMALMAP", "_METALLICSPECGLOSSMAP", "_SPEC_RAMP_ON")
    values: list[bool] = []
    for name, token in zip(names, tokens):
        value = floats[name]
        if isinstance(value, bool) or not isinstance(value, (int, float)) or value not in (0, 1):
            raise ValueError(f"Hair feature switch must be binary: {tree.name}/{name}={value!r}")
        enabled = value == 1
        if enabled != (token in keywords):
            raise ValueError(f"Hair switch/keyword disagree: {tree.name}/{name}/{token}")
        values.append(enabled)
    variants = {(True, True, True, True): "split_normals_mapped_ramp",
                (True, False, True, True): "shared_bump_normal_mapped_ramp",
                (False, False, False, False): "geometric_normal_scalar_single_lobe"}
    return variants.get(tuple(values), "branch_not_implemented")


def _source_scalar(tree: bpy.types.NodeTree, name: str) -> float:
    values = _mapping(json.loads(tree["source_floats"]), "Hair source floats")
    value = values[name]
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
        raise ValueError(f"Hair source scalar must be finite: {tree.name}/{name}={value!r}")
    return float(value)


def _variant_specular_normal(tree: bpy.types.NodeTree, parameters: HairSpecularParameters) -> tuple[bpy.types.NodeSocket, bpy.types.Node | None]:
    """Use the compiled source normal and repair all existing diffuse-normal consumers."""
    if parameters.use_spec_bump_map:
        split = _find_node(tree, "_SplitNormalMap RG / BA", "ShaderNodeSeparateColor")
        image = _find_image_node(tree, "_SplitNormalMap")
        if image.image.colorspace_settings.name != "Non-Color":
            raise ValueError(f"Hair SplitNormalMap must be Non-Color: {tree.name}")
        return _decode_specular_normal(tree, split, image.outputs["Alpha"], parameters.spec_bump_scale), image
    if not parameters.use_bump_map:
        geometry = _node(tree, "ShaderNodeNewGeometry", "Hair source geometric normal - normal maps disabled")
        return _normalize(tree, geometry.outputs["Normal"], "Normalize unmapped Hair source normal"), None
    # The shared-normal compiled variant samples BumpMap, not SplitNormalMap RG.
    from rebuild_character_materials import decode_normal
    image = _find_image_node(tree, "_BumpMap")
    if image.image.colorspace_settings.name != "Non-Color":
        raise ValueError(f"Hair BumpMap must be Non-Color: {tree.name}")
    bsdf = _find_node(tree, "CharacterNPR_Hair shared Eevee / Cycles surface", "ShaderNodeBsdfPrincipled")
    if len(bsdf.inputs["Normal"].links) != 1:
        raise ValueError(f"Expected one mapped baseline Hair normal: {tree.name}")
    previous = bsdf.inputs["Normal"].links[0].from_socket
    destinations = tuple(link.to_socket for link in previous.links)
    entry = _find_node(tree, "Editable Source and Preview Parameters", "NodeGroupInput")
    surface = decode_normal(tree, image, entry.outputs["Normal Strength"], True, "Hair source shared _BumpMap")
    for destination in destinations:
        tree.links.new(surface, destination)
    normal = _normalize(tree, surface, "Normalize Hair shared diffuse/specular BumpMap normal")
    tree["hair_shared_normal_source"] = "Compiled NORMALMAP without SPECULAR_NORMALMAP: BumpMap R*A/G; all prior normal consumers replaced."
    return normal, image


def source_parameters(
    tree: bpy.types.NodeTree,
    shader_frame: HairShaderFrame,
    line_map_binding: HairLineMapBinding,
) -> HairSpecularParameters:
    """Read the verified HGRP hair controls stored on a material node group."""
    if tree.get("source_shader") != "HGRP/CharacterNPR_Hair":
        raise ValueError(f"Expected an HGRP hair source group, got {tree.get('source_shader')!r} in {tree.name}")
    raw_floats = tree.get("source_floats")
    raw_colors = tree.get("source_colors")
    if not isinstance(raw_floats, str) or not isinstance(raw_colors, str):
        raise ValueError(f"Source float/color evidence is missing from {tree.name}")
    floats = json.loads(raw_floats)
    colors = json.loads(raw_colors)
    if not isinstance(floats, dict) or not isinstance(colors, dict):
        raise TypeError(f"Source float/color evidence must decode to objects in {tree.name}")
    for key, value in floats.items():
        if not isinstance(key, str) or isinstance(value, bool) or not isinstance(value, (int, float)):
            raise TypeError(f"Source float evidence has an invalid entry in {tree.name}: {key!r}={value!r}")
    required = (
        "_AnisotropyDirX",
        "_AnisotropyEdgeFade",
        "_AnisotropyIntensity",
        "_AnisotropyRange2",
        "_AnisotropyValue",
        "_AnisotropyValue2",
        "_SpecBumpScale",
        "_UseMetallicGlossMap",
        "_UseBumpMap",
        "_UseSpecBumpMap",
        "_UseSpecRampMap",
        "_SpecularLine",
        "_UseLineMap",
        "_LineAmount",
        "_LineIntensity",
        "_LineRange",
        "_LineSaturation",
        "_LineValue",
    )
    missing = tuple(key for key in required if key not in floats)
    if missing:
        raise ValueError(f"Source hair group is missing required float properties: {missing}")
    color = colors.get("_AnisotropyColor2")
    if not isinstance(color, list) or len(color) != 4 or any(isinstance(value, bool) or not isinstance(value, (int, float)) for value in color):
        raise ValueError(f"Source hair group has invalid _AnisotropyColor2: {color!r}")
    if not isinstance(line_map_binding, HairLineMapBinding):
        raise TypeError("source_parameters requires an explicit HairLineMapBinding from the source material slot")
    line_map_binding.validate()
    parameters = HairSpecularParameters(
        anisotropy_dir_x=float(floats["_AnisotropyDirX"]),
        anisotropy_edge_fade=float(floats["_AnisotropyEdgeFade"]),
        anisotropy_intensity=float(floats["_AnisotropyIntensity"]),
        anisotropy_range2=float(floats["_AnisotropyRange2"]),
        anisotropy_value=float(floats["_AnisotropyValue"]),
        anisotropy_value2=float(floats["_AnisotropyValue2"]),
        anisotropy_color2=tuple(float(value) for value in color),
        spec_bump_scale=float(floats["_SpecBumpScale"]),
        use_metallic_gloss_map=float(floats["_UseMetallicGlossMap"]) > 0.5,
        use_bump_map=float(floats["_UseBumpMap"]) > 0.5,
        use_spec_bump_map=float(floats["_UseSpecBumpMap"]) > 0.5,
        use_spec_ramp_map=float(floats["_UseSpecRampMap"]) > 0.5,
        use_specular_line=float(floats["_SpecularLine"]) > 0.5,
        use_line_map=float(floats["_UseLineMap"]) > 0.5,
        line_amount=float(floats["_LineAmount"]),
        line_intensity=float(floats["_LineIntensity"]),
        line_range=float(floats["_LineRange"]),
        line_saturation=float(floats["_LineSaturation"]),
        line_value=float(floats["_LineValue"]),
        line_map_file_id=line_map_binding.file_id,
        line_map_path_id=line_map_binding.path_id,
        line_map_scale=line_map_binding.scale,
        line_map_offset=line_map_binding.offset,
        shader_frame=shader_frame,
    )
    parameters.validate()
    if hair_variant(tree) == "branch_not_implemented":
        raise ValueError(f"Hair source variant is not implemented: {tree.name}")
    return parameters


def _node(tree: bpy.types.NodeTree, kind: str, label: str) -> bpy.types.Node:
    result = tree.nodes.new(kind)
    result.label = label
    result["endfield_layout_section"] = "HGRP Hair Lighting"
    return result


def _math(
    tree: bpy.types.NodeTree,
    operation: str,
    first: bpy.types.NodeSocket | float,
    second: bpy.types.NodeSocket | float,
    label: str,
) -> bpy.types.NodeSocket:
    result = _node(tree, "ShaderNodeMath", label)
    result.operation = operation
    for index, value in enumerate((first, second)):
        if isinstance(value, bpy.types.NodeSocket):
            tree.links.new(value, result.inputs[index])
        else:
            result.inputs[index].default_value = float(value)
    return result.outputs[0]


def _vector_math(
    tree: bpy.types.NodeTree,
    operation: str,
    first: bpy.types.NodeSocket,
    second: bpy.types.NodeSocket | None,
    label: str,
) -> bpy.types.Node:
    result = _node(tree, "ShaderNodeVectorMath", label)
    result.operation = operation
    tree.links.new(first, result.inputs[0])
    if second is not None:
        tree.links.new(second, result.inputs[1])
    return result


def _find_node(tree: bpy.types.NodeTree, label: str, node_type: str) -> bpy.types.Node:
    matches = tuple(node for node in tree.nodes if node.label == label and node.bl_idname == node_type)
    if len(matches) != 1:
        raise ValueError(f"Expected one {node_type} labeled {label!r} in {tree.name}, found {len(matches)}")
    return matches[0]


def _find_image_node(tree: bpy.types.NodeTree, label_prefix: str) -> bpy.types.Node:
    matches = tuple(
        node for node in tree.nodes
        if node.type == "TEX_IMAGE" and node.label.startswith(label_prefix) and node.image is not None
    )
    if len(matches) != 1:
        raise ValueError(f"Expected one packed image node beginning {label_prefix!r} in {tree.name}, found {len(matches)}")
    return matches[0]


def _normalize(tree: bpy.types.NodeTree, socket: bpy.types.NodeSocket, label: str) -> bpy.types.NodeSocket:
    return _vector_math(tree, "NORMALIZE", socket, None, label).outputs[0]


def _cross(tree: bpy.types.NodeTree, first: bpy.types.NodeSocket, second: bpy.types.NodeSocket, label: str) -> bpy.types.NodeSocket:
    return _vector_math(tree, "CROSS_PRODUCT", first, second, label).outputs[0]


def _dot(tree: bpy.types.NodeTree, first: bpy.types.NodeSocket, second: bpy.types.NodeSocket, label: str) -> bpy.types.NodeSocket:
    return _vector_math(tree, "DOT_PRODUCT", first, second, label).outputs[1]


def _scale(tree: bpy.types.NodeTree, vector: bpy.types.NodeSocket, scalar: bpy.types.NodeSocket, label: str) -> bpy.types.NodeSocket:
    result = _node(tree, "ShaderNodeVectorMath", label)
    result.operation = "SCALE"
    tree.links.new(vector, result.inputs[0])
    tree.links.new(scalar, result.inputs[3])
    return result.outputs[0]


def _combine_xyz(
    tree: bpy.types.NodeTree,
    x: bpy.types.NodeSocket | float,
    y: bpy.types.NodeSocket | float,
    z: bpy.types.NodeSocket | float,
    label: str,
) -> bpy.types.NodeSocket:
    result = _node(tree, "ShaderNodeCombineXYZ", label)
    for name, value in (("X", x), ("Y", y), ("Z", z)):
        if isinstance(value, bpy.types.NodeSocket):
            tree.links.new(value, result.inputs[name])
        else:
            result.inputs[name].default_value = float(value)
    return result.outputs[0]


def _decode_specular_normal(
    tree: bpy.types.NodeTree,
    split: bpy.types.Node,
    alpha: bpy.types.NodeSocket,
    strength: float,
) -> bpy.types.NodeSocket:
    x = _math(tree, "SUBTRACT", _math(tree, "MULTIPLY", split.outputs["Blue"], 2.0, "Hair split BA blue x2"), 1.0, "Hair split BA normal X")
    y = _math(tree, "SUBTRACT", _math(tree, "MULTIPLY", alpha, 2.0, "Hair split BA alpha x2"), 1.0, "Hair split BA normal Y")
    x = _math(tree, "MULTIPLY", x, strength, "Hair spec normal scale X")
    y = _math(tree, "MULTIPLY", y, strength, "Hair spec normal scale Y")
    x2 = _math(tree, "MULTIPLY", x, x, "Hair spec normal X squared")
    y2 = _math(tree, "MULTIPLY", y, y, "Hair spec normal Y squared")
    xy2 = _math(tree, "ADD", x2, y2, "Hair spec normal XY length squared")
    z2 = _math(tree, "MAXIMUM", _math(tree, "SUBTRACT", 1.0, xy2, "Hair spec normal Z squared raw"), 0.0, "Hair spec normal Z squared clamp")
    z = _math(tree, "SQRT", z2, 0.0, "Hair spec normal positive Z")
    encoded = _node(tree, "ShaderNodeCombineColor", "Hair independent spec normal RGB encoding")
    encoded.mode = "RGB"
    for channel, value in (("Red", x), ("Green", y), ("Blue", z)):
        tree.links.new(_math(tree, "ADD", _math(tree, "MULTIPLY", value, 0.5, "Hair spec normal half range"), 0.5, "Hair spec normal RGB encoding"), encoded.inputs[channel])
    normal_map = _node(tree, "ShaderNodeNormalMap", "Hair specular normal from SplitNormal BA")
    normal_map.space = "TANGENT"
    normal_map.uv_map = "UV0"
    normal_map.inputs["Strength"].default_value = 1.0
    tree.links.new(encoded.outputs["Color"], normal_map.inputs["Color"])
    return _normalize(tree, normal_map.outputs["Normal"], "Normalize independent hair spec normal")


def _shader_frame_axes(
    tree: bpy.types.NodeTree,
    frame: HairShaderFrame,
) -> tuple[bpy.types.NodeSocket, bpy.types.NodeSocket, bpy.types.NodeSocket]:
    sources = (
        ("X", frame.source_x_in_blender_world),
        ("Y", frame.source_y_in_blender_world),
        ("Z", frame.source_z_in_blender_world),
    )
    axes: list[bpy.types.NodeSocket] = []
    for label, vector in sources:
        axes.append(_combine_xyz(tree, *vector, f"HGRP explicit shader-frame axis {label}"))
    return axes[0], axes[1], axes[2]


def _constant_scalar(tree: bpy.types.NodeTree, value: float, label: str) -> bpy.types.NodeSocket:
    node = _node(tree, "ShaderNodeValue", label)
    node.outputs[0].default_value = value
    return node.outputs[0]


def hair_cast_shadow_source(tree: bpy.types.NodeTree) -> bpy.types.NodeSocket:
    """Share the exact live cast-visibility producer used by Hair diffuse."""
    if tree.get("source_shader") != "HGRP/CharacterNPR_Hair":
        raise ValueError(f"Hair cast visibility requires the exact source shader: {tree.name}")
    diffuse = _find_node(tree, "Verified local HGRP diffuse equations", "ShaderNodeGroup")
    if "Cast Shadow" not in diffuse.inputs or len(diffuse.inputs["Cast Shadow"].links) != 1:
        raise ValueError(f"Hair diffuse cast visibility must have one explicit source: {tree.name}")
    return diffuse.inputs["Cast Shadow"].links[0].from_socket


def _edge_fade(
    tree: bpy.types.NodeTree,
    specular_normal: bpy.types.NodeSocket,
    view_direction: bpy.types.NodeSocket,
    source_axis_x: bpy.types.NodeSocket,
    source_axis_z: bpy.types.NodeSocket,
    power: float,
) -> bpy.types.NodeSocket:
    normal_x = _dot(tree, specular_normal, source_axis_x, "Hair spec normal source X component")
    normal_z = _dot(tree, specular_normal, source_axis_z, "Hair spec normal source Z component")
    view_x = _dot(tree, view_direction, source_axis_x, "Hair view source X component")
    view_z = _dot(tree, view_direction, source_axis_z, "Hair view source Z component")
    normal_length_sq = _math(tree, "ADD", _math(tree, "MULTIPLY", normal_x, normal_x, "Hair normal source X squared"), _math(tree, "MULTIPLY", normal_z, normal_z, "Hair normal source Z squared"), "Hair normal source XZ squared")
    view_length_sq = _math(tree, "ADD", _math(tree, "MULTIPLY", view_x, view_x, "Hair view source X squared"), _math(tree, "MULTIPLY", view_z, view_z, "Hair view source Z squared"), "Hair view source XZ squared")
    normal_length = _math(tree, "SQRT", _math(tree, "MAXIMUM", normal_length_sq, 1e-12, "Hair normal source XZ safe length"), 0.0, "Hair normal source XZ length")
    view_length = _math(tree, "SQRT", _math(tree, "MAXIMUM", view_length_sq, 1e-12, "Hair view source XZ safe length"), 0.0, "Hair view source XZ length")
    normal_x = _math(tree, "DIVIDE", normal_x, normal_length, "Hair normalized normal source X")
    normal_z = _math(tree, "DIVIDE", normal_z, normal_length, "Hair normalized normal source Z")
    view_x = _math(tree, "DIVIDE", view_x, view_length, "Hair normalized view source X")
    view_z = _math(tree, "DIVIDE", view_z, view_length, "Hair normalized view source Z")
    edge_dot = _math(tree, "ADD", _math(tree, "MULTIPLY", normal_x, view_x, "Hair edge X dot"), _math(tree, "MULTIPLY", normal_z, view_z, "Hair edge Z dot"), "Hair source XZ edge dot")
    edge_dot = _math(tree, "MAXIMUM", edge_dot, 0.0, "Hair edge dot lower clamp")
    edge_dot = _math(tree, "MINIMUM", edge_dot, 1.0, "Hair edge dot upper clamp")
    return _math(tree, "POWER", edge_dot, power, "HGRP hair edge fade")


def _line_modulated_diffuse(
    tree: bpy.types.NodeTree,
    diffuse_source: bpy.types.NodeSocket,
    specular_level: bpy.types.NodeSocket,
    strand1_max: bpy.types.NodeSocket,
    specular_normal: bpy.types.NodeSocket,
    modulated_bitangent: bpy.types.NodeSocket,
    half_vector: bpy.types.NodeSocket,
    parameters: HairSpecularParameters,
) -> tuple[bpy.types.NodeSocket, bpy.types.Node | None]:
    if not parameters.use_specular_line:
        return diffuse_source, None
    uv_node = _node(tree, "ShaderNodeUVMap", "HGRP source line UV0")
    uv_node.uv_map = "UV0"
    uv_components = _node(tree, "ShaderNodeSeparateXYZ", "HGRP source line UV components")
    tree.links.new(uv_node.outputs["UV"], uv_components.inputs["Vector"])
    uv_period = _math(tree, "MULTIPLY", uv_components.outputs["X"], parameters.line_amount, "HGRP source procedural line period")
    uv_fraction = _math(tree, "FRACT", uv_period, 0.0, "HGRP source procedural line fraction")
    uv_half = _math(tree, "SUBTRACT", uv_fraction, 0.5, "HGRP source procedural line threshold")
    line_proc = _math(tree, "CEIL", _math(tree, "MAXIMUM", uv_half, 0.0, "HGRP source procedural line lower clamp"), 0.0, "HGRP source procedural line mask")

    if parameters.use_line_map:
        line_map = _find_image_node(tree, "_LineMap")
        if line_map.image.colorspace_settings.name != "Non-Color":
            raise ValueError("Source LineMap must remain in Non-Color space")
        if (
            str(line_map.get("source_file_id", "")) != str(parameters.line_map_file_id)
            or str(line_map.get("source_path_id", "")) != str(parameters.line_map_path_id)
        ):
            raise ValueError(
                f"Source LineMap identity mismatch in {tree.name}: "
                f"image=({line_map.get('source_file_id')!r}, {line_map.get('source_path_id')!r}), "
                f"slot=({parameters.line_map_file_id}, {parameters.line_map_path_id})"
            )
        line_map.interpolation = "Linear"
        line_map.extension = "REPEAT"
        uv_scale = _combine_xyz(tree, parameters.line_map_scale[0], parameters.line_map_scale[1], 1.0, "HGRP source LineMap UV scale")
        uv_offset = _combine_xyz(tree, parameters.line_map_offset[0], parameters.line_map_offset[1], 0.0, "HGRP source LineMap UV offset")
        uv_scaled = _vector_math(tree, "MULTIPLY", uv_node.outputs["UV"], uv_scale, "HGRP source LineMap UV scale application")
        uv_mapped = _vector_math(tree, "ADD", uv_scaled.outputs[0], uv_offset, "HGRP source LineMap UV offset application")
        tree.links.new(uv_mapped.outputs[0], line_map.inputs["Vector"])
        line_map_components = _node(tree, "ShaderNodeSeparateColor", "HGRP source LineMap channels")
        line_map_components.mode = "RGB"
        tree.links.new(line_map.outputs["Color"], line_map_components.inputs["Color"])
        line_map_value = line_map_components.outputs["Red"]
    else:
        line_map = None
        line_map_value = _constant_scalar(tree, 0.0, "HGRP source LineMap disabled")

    line_map_blend = _math(
        tree,
        "ADD",
        _math(
            tree,
            "MULTIPLY",
            _math(
                tree,
                "ADD",
                _math(tree, "MULTIPLY", line_proc, -1.0, "HGRP source line mask subtraction"),
                _math(tree, "SUBTRACT", 1.0, line_map_value, "HGRP source LineMap inverse"),
                "HGRP source LineMap blend input",
            ),
            1.0 if parameters.use_line_map else 0.0,
            "HGRP source LineMap blend gate",
        ),
        line_proc,
        "HGRP source LineMap/procedural blend",
    )
    line_blend = _math(
        tree,
        "ADD",
        _math(tree, "MULTIPLY", line_map_blend, parameters.line_intensity, "HGRP source line blend intensity"),
        1.0 - parameters.line_intensity,
        "HGRP source line blend final",
    )
    line_shift = parameters.line_value * 2.0 - 1.0
    line_tangent_sum = _vector_math(
        tree,
        "ADD",
        _scale(tree, specular_normal, _constant_scalar(tree, line_shift, "HGRP source line normal shift"), "HGRP source line normal contribution"),
        modulated_bitangent,
        "HGRP source line tangent sum",
    )
    line_tangent = _normalize(tree, line_tangent_sum.outputs[0], "Normalize HGRP source line tangent")
    line_tdot_h = _dot(tree, line_tangent, half_vector, "HGRP source line tangent dot half")
    line_sin_sq = _math(tree, "MAXIMUM", _math(tree, "SUBTRACT", 1.0, _math(tree, "MULTIPLY", line_tdot_h, line_tdot_h, "HGRP source line TdotH squared"), "HGRP source line sin squared raw"), 0.0, "HGRP source line sin squared minimum")
    line_sin_root = _math(tree, "SQRT", line_sin_sq, 0.0, "HGRP source line sin T H root")
    line_sin = _math(tree, "MAXIMUM", line_sin_root, 0.0001, "HGRP source line sin T H")
    line_exponent = float(math.trunc(max(1.0 - parameters.line_range, 0.0) * 200.0))
    line_lobe = _math(tree, "POWER", line_sin, line_exponent, "HGRP source line exponent result")
    line_mix = _math(
        tree,
        "SUBTRACT",
        _math(
            tree,
            "ADD",
            line_blend,
            _math(
                tree,
                "MULTIPLY",
                _math(tree, "SUBTRACT", 1.0, line_blend, "HGRP source line blend inverse"),
                strand1_max,
                "HGRP source line blend by strand maximum",
            ),
            "HGRP source line mix sum",
        ),
        1.0,
        "HGRP source line mix minus one",
    )
    line_mod_delta = _math(
        tree,
        "MULTIPLY",
        specular_level,
        _math(tree, "MULTIPLY", line_mix, line_lobe, "HGRP source line mix by exponent"),
        "HGRP source line modulation delta",
    )
    line_mod = _math(tree, "ADD", 1.0, line_mod_delta, "HGRP source line modulation factor")
    diffuse_lit = _scale(tree, diffuse_source, line_mod, "HGRP source line-modulated diffuse")
    luminance_axis = _combine_xyz(tree, 0.2126, 0.7152, 0.0722, "HGRP source luminance coefficients")
    diffuse_luminance = _dot(tree, diffuse_lit, luminance_axis, "HGRP source line-modulated diffuse luminance")
    luminance_color = _combine_xyz(tree, diffuse_luminance, diffuse_luminance, diffuse_luminance, "HGRP source luminance color")
    diffuse_chroma = _vector_math(tree, "SUBTRACT", diffuse_lit, luminance_color, "HGRP source line-modulated diffuse chroma")
    line_saturation_factor = _math(
        tree,
        "ADD",
        _math(tree, "MULTIPLY", line_mod, 1.0 - parameters.line_saturation, "HGRP source line saturation modulation"),
        parameters.line_saturation,
        "HGRP source line saturation factor",
    )
    saturated_chroma = _scale(tree, diffuse_chroma.outputs[0], line_saturation_factor, "HGRP source line saturated chroma")
    line_diffuse = _vector_math(tree, "ADD", saturated_chroma, luminance_color, "HGRP source line-modulated diffuse output")
    return line_diffuse.outputs[0], line_map


def apply_hair_specular_branch(
    hair_tree: bpy.types.NodeTree,
    lighting_tree: bpy.types.NodeTree,
    parameters: HairSpecularParameters,
) -> HairBranchReceipt:
    """Install an explicit HGRP hair split-normal and dual-strand specular branch."""
    parameters.validate()
    switches = tuple(_source_scalar(hair_tree, name) == 1. for name in ("_UseBumpMap", "_UseSpecBumpMap", "_UseMetallicGlossMap", "_UseSpecRampMap"))
    if switches != (parameters.use_bump_map, parameters.use_spec_bump_map, parameters.use_metallic_gloss_map, parameters.use_spec_ramp_map):
        raise ValueError(f"Hair parameters must preserve the actual source feature switches: {hair_tree.name}")
    if hair_tree.get("hair_lighting_patch"):
        raise ValueError(f"Hair specular branch is already installed in {hair_tree.name}")
    if hair_variant(hair_tree) == "branch_not_implemented":
        raise ValueError(f"Hair source variant is not implemented: {hair_tree.name}")
    metallic_gloss = _find_node(hair_tree, "R metallic / G specular / B AO / A smoothness", "ShaderNodeSeparateColor") if parameters.use_metallic_gloss_map else None
    metallic_gloss_image_node = _find_image_node(hair_tree, "_MetallicGlossMap") if parameters.use_metallic_gloss_map else None
    ramp_template = _find_image_node(hair_tree, "_SpecRampMap") if parameters.use_spec_ramp_map else None
    if ramp_template is not None and ramp_template.image.colorspace_settings.name != "Non-Color":
        raise ValueError("SpecRampMap must remain in Non-Color space")
    emission = _find_node(hair_tree, "Artist-lit NPR diffuse - shared engines", "ShaderNodeEmission")
    emission_input = emission.inputs["Color"]
    if len(emission_input.links) != 1:
        raise ValueError(f"Hair NPR diffuse emission must have one source in {hair_tree.name}")
    diffuse_source = emission_input.links[0].from_socket
    diffuse_nodes = tuple(
        node for node in hair_tree.nodes
        if node.type == "GROUP" and node.label == "Verified local HGRP diffuse equations"
    )
    if len(diffuse_nodes) != 1:
        raise ValueError(
            f"Hair requires one attached Verified local HGRP diffuse equations group before specular patching; "
            f"found {len(diffuse_nodes)} in {hair_tree.name}. Call patch_character_hgrp.attach_diffuse(hair, shared_state, shared_diffuse) first."
        )
    diffuse_node = diffuse_nodes[0]
    diffuse_tree = diffuse_node.node_tree
    if diffuse_tree is None or "Local ComputeNPRDiffuse arithmetic only" not in str(diffuse_tree.get("scope", "")):
        raise ValueError(f"Hair diffuse group is not the verified local HGRP arithmetic graph: {diffuse_node.name}")
    diffuse_outputs = ("Full Diffuse", "Ambient Diffuse Intensity", "Cast Blend")
    missing_diffuse_outputs = tuple(name for name in diffuse_outputs if name not in diffuse_node.outputs)
    if missing_diffuse_outputs:
        raise ValueError(f"Attached HGRP diffuse graph is missing required Hair specular outputs: {missing_diffuse_outputs}")
    state_nodes = tuple(
        node for node in hair_tree.nodes
        if node.type == "GROUP" and node.label == "Explicit HGRP state and preview inputs"
    )
    if len(state_nodes) != 1:
        raise ValueError(f"Hair requires exactly one attached explicit HGRP state group, found {len(state_nodes)} in {hair_tree.name}")
    light_nodes = tuple(
        node for node in hair_tree.nodes
        if node.type == "GROUP" and node.node_tree == lighting_tree
    )
    if len(light_nodes) != 1:
        raise ValueError(f"Expected one shared lighting_tree instance in {hair_tree.name}, found {len(light_nodes)}")
    light_node = light_nodes[0]
    if "Direction" not in light_node.outputs:
        raise ValueError("Shared lighting_tree must expose Direction for the Hair half-vector")
    spec_normal, split_image_node = _variant_specular_normal(hair_tree, parameters)

    geometry = _node(hair_tree, "ShaderNodeNewGeometry", "Hair strand view input")
    view_direction = _normalize(hair_tree, geometry.outputs["Incoming"], "Normalize Geometry Incoming toward camera")
    light_direction = _normalize(hair_tree, light_node.outputs["Direction"], "Normalize lighting_tree.Direction for hair")
    source_axis_x, source_axis_y, source_axis_z = _shader_frame_axes(hair_tree, parameters.shader_frame)
    source_world_y = _combine_xyz(
        hair_tree,
        parameters.shader_frame.source_world_y_in_blender_world[0],
        parameters.shader_frame.source_world_y_in_blender_world[1],
        parameters.shader_frame.source_world_y_in_blender_world[2],
        "HGRP source world Y axis in Blender world",
    )
    light_y_source = _dot(hair_tree, light_direction, source_world_y, "HGRP adjusted light source-world Y")
    cast_shadow = hair_cast_shadow_source(hair_tree)
    one_minus_cast = _math(hair_tree, "SUBTRACT", 1.0, cast_shadow, "HGRP inverse cast-shadow preview")
    light_y_blend = _math(
        hair_tree,
        "ADD",
        _math(hair_tree, "MULTIPLY", 0.5, one_minus_cast, "HGRP shadow fallback light Y"),
        _math(hair_tree, "MULTIPLY", light_y_source, cast_shadow, "HGRP cast-shadow light Y"),
        "HGRP source light Y cast-shadow blend",
    )
    light_cast = _scale(hair_tree, light_direction, cast_shadow, "HGRP adjusted light direction times cast shadow")
    view_x = _dot(hair_tree, view_direction, source_axis_x, "HGRP view source X projection")
    view_z = _dot(hair_tree, view_direction, source_axis_z, "HGRP view source Z projection")
    world_contrib_x = _scale(hair_tree, source_axis_x, view_x, "HGRP world contribution source X")
    world_contrib_y = _scale(hair_tree, source_axis_y, light_y_blend, "HGRP world contribution source Y")
    world_contrib_z = _scale(hair_tree, source_axis_z, view_z, "HGRP world contribution source Z")
    world_contrib_xy = _vector_math(hair_tree, "ADD", world_contrib_x, world_contrib_y, "HGRP world contribution XY")
    world_contrib = _vector_math(hair_tree, "ADD", world_contrib_xy.outputs[0], world_contrib_z, "HGRP world contribution XYZ")
    world_contrib_scaled = _scale(hair_tree, world_contrib.outputs[0], _constant_scalar(hair_tree, 2.0, "HGRP source world contribution scale 2"), "HGRP source world contribution times 2")
    light_and_world = _vector_math(hair_tree, "ADD", light_cast, world_contrib_scaled, "HGRP source light plus world contribution")
    normalized_light_world = _normalize(hair_tree, light_and_world.outputs[0], "Normalize HGRP light/world half input")
    half_sum = _vector_math(hair_tree, "ADD", normalized_light_world, view_direction, "HGRP source H sum with view")
    half_vector = _normalize(hair_tree, half_sum.outputs[0], "Normalize source H half vector")

    anisotropy_x = _scale(hair_tree, source_axis_x, _constant_scalar(hair_tree, parameters.anisotropy_dir_x, "HGRP source anisotropy X parameter"), "HGRP anisotropy source X contribution")
    anisotropy_sum = _vector_math(hair_tree, "ADD", anisotropy_x, source_axis_y, "HGRP source anisotropy axis sum")
    anisotropy_direction = _normalize(hair_tree, anisotropy_sum.outputs[0], "Normalize HGRP source anisotropy direction")
    base_bitangent = _cross(hair_tree, spec_normal, anisotropy_direction, "HGRP hair source bitangent base")
    source_tangent = _node(hair_tree, "ShaderNodeTangent", "HGRP source tangent blend from UV0")
    source_tangent.direction_type = "UV_MAP"
    source_tangent.uv_map = "UV0"
    metallic = metallic_gloss.outputs["Red"] if parameters.use_metallic_gloss_map else _constant_scalar(hair_tree, _source_scalar(hair_tree, "_Metallic"), "Hair source scalar Metallic")
    one_minus_metallic = _math(hair_tree, "SUBTRACT", 1.0, metallic, "HGRP nonmetallic tangent blend weight")
    base_bitangent_blend = _scale(hair_tree, base_bitangent, one_minus_metallic, "HGRP nonmetallic bitangent blend")
    source_tangent_blend = _scale(hair_tree, source_tangent.outputs["Tangent"], metallic, "HGRP source tangent blend by metallic map")
    blended_bitangent = _vector_math(hair_tree, "ADD", base_bitangent_blend, source_tangent_blend, "HGRP metallic tangent bitangent blend")
    mod_bitangent = _cross(hair_tree, spec_normal, blended_bitangent.outputs[0], "HGRP hair shifted bitangent")
    tangent_sign = _constant_scalar(hair_tree, 1.0, "HGRP tangent handedness preview +1")
    mod_bitangent = _scale(hair_tree, mod_bitangent, tangent_sign, "HGRP handedness-scaled bitangent")
    edge = _edge_fade(hair_tree, spec_normal, view_direction, source_axis_x, source_axis_z, parameters.anisotropy_edge_fade)
    spec_level = metallic_gloss.outputs["Green"] if metallic_gloss is not None else _constant_scalar(hair_tree, _source_scalar(hair_tree, "_Specular"), "Hair source scalar Specular")
    smoothness = metallic_gloss_image_node.outputs["Alpha"] if metallic_gloss_image_node is not None else _constant_scalar(hair_tree, _source_scalar(hair_tree, "_Smoothness"), "Hair source scalar Smoothness")

    strand_results: list[bpy.types.NodeSocket] = []
    strand_maxima: list[bpy.types.NodeSocket] = []
    lobe_tdot_values: list[bpy.types.NodeSocket] = []
    ramp = None
    if ramp_template is not None:
        ramp = _node(hair_tree, "ShaderNodeTexImage", "HGRP strand 1 source spec ramp lookup")
        ramp.image = ramp_template.image
        ramp.interpolation = "Linear"
        ramp.extension = "EXTEND"
    for lobe_index, shift_value in ((1, parameters.anisotropy_value), (2, parameters.anisotropy_value2)):
        shift = _math(hair_tree, "SUBTRACT", _math(hair_tree, "MULTIPLY", shift_value, 2.0, f"Hair strand {lobe_index} source shift x2"), 1.0, f"Hair strand {lobe_index} source shift")
        hair_shift = _scale(hair_tree, spec_normal, shift, f"Hair strand {lobe_index} shifted spec normal")
        shifted_tangent_sum = _vector_math(hair_tree, "ADD", mod_bitangent, hair_shift, f"Hair strand {lobe_index} tangent sum")
        shifted_tangent = _normalize(hair_tree, shifted_tangent_sum.outputs[0], f"Normalize hair strand {lobe_index} tangent")
        tdot_h = _dot(hair_tree, shifted_tangent, half_vector, f"Hair strand {lobe_index} tangent dot half")
        sin_squared = _math(hair_tree, "MAXIMUM", _math(hair_tree, "SUBTRACT", 1.0, _math(hair_tree, "MULTIPLY", tdot_h, tdot_h, f"Hair strand {lobe_index} TdotH squared"), f"Hair strand {lobe_index} sin squared raw"), 0.0, f"Hair strand {lobe_index} sin squared floor")
        sin_root = _math(hair_tree, "SQRT", sin_squared, 0.0, f"Hair strand {lobe_index} sin T H root")
        sin_term = _math(hair_tree, "MAXIMUM", sin_root, 0.0001, f"Hair strand {lobe_index} sin T H source floor")
        exponent = 200.0 if lobe_index == 1 else float(math.trunc(max(1.0 - parameters.anisotropy_range2, 0.0) * 200.0))
        lobe = _math(hair_tree, "POWER", sin_term, exponent, f"Hair strand {lobe_index} source exponent")
        if not parameters.use_spec_ramp_map:
            if lobe_index == 1:
                strand_max = _math(hair_tree, "MULTIPLY", edge, lobe, "Hair unmapped source single lobe")
                dielectric = _scale(hair_tree, _combine_xyz(hair_tree, 1., 1., 1., "Hair scalar dielectric white"), _math(hair_tree, "MULTIPLY", spec_level, .04, "Hair scalar dielectric specular"), "Hair scalar dielectric color")
                # Compiled Hair forces post-weather F0 metallic to zero; source
                # metallic still participates independently in tangent blending.
                intensity = _math(hair_tree, "MULTIPLY", strand_max, parameters.anisotropy_intensity * 5., "Hair unmapped source lobe intensity")
                strand_results.append(_scale(hair_tree, dielectric, intensity, "Hair unmapped scalar strand contribution"))
                strand_maxima.append(strand_max)
            else:
                # The compiled no-SPEC_RAMP_ON variant has no second colored lobe.
                strand_results.append(_combine_xyz(hair_tree, 0., 0., 0., "Hair no-ramp second lobe source zero"))
                strand_max = strand_maxima[0]
        elif lobe_index == 1:
            strand_raw = _math(hair_tree, "MULTIPLY", lobe, spec_level, "HGRP strand 1 spec level raw")
            strand_nonnegative = _math(hair_tree, "MAXIMUM", strand_raw, 0.0, "HGRP strand 1 spec level lower clamp")
            strand = _math(hair_tree, "MINIMUM", strand_nonnegative, 1.0, "HGRP strand 1 spec level saturate")
            ramp_u = strand
            positive_half = _math(hair_tree, "GREATER_THAN", tdot_h, 0.0, "HGRP strand 1 ramp half")
            edge_squared = _math(hair_tree, "MULTIPLY", edge, edge, "HGRP strand 1 edge squared")
            ramp_v = _math(hair_tree, "MULTIPLY", edge_squared, positive_half, "HGRP strand 1 ramp V")
            ramp_uv = _combine_xyz(hair_tree, ramp_u, ramp_v, 0.0, "HGRP strand 1 spec ramp coordinates")
            hair_tree.links.new(ramp_uv, ramp.inputs["Vector"])
            edge_strand = _math(hair_tree, "MULTIPLY", edge, strand, "HGRP strand 1 edge scale")
            strand_color = _scale(hair_tree, ramp.outputs["Color"], edge_strand, "HGRP strand 1 ramp color scale")
            ramp_components = _node(hair_tree, "ShaderNodeSeparateColor", "HGRP strand 1 spec ramp maximum")
            ramp_components.mode = "RGB"
            hair_tree.links.new(strand_color, ramp_components.inputs["Color"])
            max_rg = _math(hair_tree, "MAXIMUM", ramp_components.outputs["Red"], ramp_components.outputs["Green"], "HGRP strand 1 ramp max RG")
            strand_max = _math(hair_tree, "MAXIMUM", max_rg, ramp_components.outputs["Blue"], "HGRP strand 1 ramp max RGB")
            diel_spec = _math(hair_tree, "MULTIPLY", spec_level, 0.04, "HGRP dielectric spec scale")
            lobe_scale = _math(hair_tree, "MULTIPLY", diel_spec, parameters.anisotropy_intensity * 5.0, "HGRP strand 1 source intensity")
            strand_results.append(_scale(hair_tree, strand_color, lobe_scale, "HGRP strand 1 colored ramp contribution"))
            strand_maxima.append(strand_max)
        else:
            edge_scale = _math(hair_tree, "MULTIPLY", edge, lobe, "HGRP strand 2 edge lobe")
            smooth_scale = _math(hair_tree, "MULTIPLY", edge_scale, smoothness, "HGRP strand 2 smoothness")
            strand_max = strand_maxima[0]
            lobe_gap = _math(hair_tree, "SUBTRACT", 1.0, strand_max, "HGRP strand 2 source lobe gap")
            lobe_scale = _math(hair_tree, "MULTIPLY", smooth_scale, lobe_gap, "HGRP strand 2 lobe scale")
            color = _node(hair_tree, "ShaderNodeRGB", "HGRP source _AnisotropyColor2")
            color.outputs[0].default_value = parameters.anisotropy_color2
            strand_results.append(_scale(hair_tree, color.outputs[0], lobe_scale, "HGRP strand 2 colored contribution"))
        strand_maxima.append(strand_max) if lobe_index == 2 else None
        lobe_tdot_values.append(tdot_h)

    spec_add = _vector_math(hair_tree, "ADD", strand_results[0], strand_results[1], "HGRP dual strand specular sum")
    line_diffuse, line_map = _line_modulated_diffuse(
        hair_tree,
        diffuse_source,
        spec_level,
        strand_maxima[0],
        spec_normal,
        mod_bitangent,
        half_vector,
        parameters,
    )
    enable = _node(hair_tree, "ShaderNodeValue", "Hair HGRP Specular Enable - diagnostic switch")
    enable.outputs[0].default_value = 1.0
    specular_enabled = _scale(hair_tree, spec_add.outputs[0], enable.outputs[0], "HGRP hair specular enable")
    full_diffuse_spec = _vector_math(
        hair_tree,
        "MULTIPLY",
        specular_enabled,
        diffuse_node.outputs["Full Diffuse"],
        "HGRP hair source specular times Full Diffuse",
    )
    cast_blend_half = _math(hair_tree, "MULTIPLY", diffuse_node.outputs["Cast Blend"], 0.5, "HGRP hair Cast Blend half")
    cast_ambient_weight = _math(hair_tree, "ADD", cast_blend_half, 0.5, "HGRP hair source specular ambient cast weight")
    specular_ambient = _math(
        hair_tree,
        "MULTIPLY",
        diffuse_node.outputs["Ambient Diffuse Intensity"],
        cast_ambient_weight,
        "HGRP hair source specular ambient intensity",
    )
    specular_ambient_color = _scale(
        hair_tree,
        full_diffuse_spec.outputs[0],
        specular_ambient,
        "HGRP hair source specular Full Diffuse times ambient intensity",
    )
    cp13_w = _constant_scalar(hair_tree, 1.0, "HGRP Hair CP13.w constructor initial value 1")
    specular_cp13 = _scale(hair_tree, specular_ambient_color, cp13_w, "HGRP Hair source specular CP13.w")

    addition = _node(hair_tree, "ShaderNodeMixRGB", "HGRP hair dual-strand specular added to NPR light")
    addition.blend_type = "ADD"
    addition.use_clamp = False
    addition.inputs[0].default_value = 1.0
    hair_tree.links.new(line_diffuse, addition.inputs[1])
    hair_tree.links.new(specular_cp13, addition.inputs[2])
    response_mix = _find_node(hair_tree, "Editable native-sampling response / baseline", "ShaderNodeMixShader")
    shader_sockets = tuple(socket for socket in response_mix.inputs if socket.type == "SHADER")
    source_shader_indices = tuple(
        index for index, socket in enumerate(shader_sockets)
        if any(link.from_node == emission for link in socket.links)
    )
    if len(shader_sockets) != 2 or source_shader_indices != (1,):
        raise ValueError(
            f"Hair Source Response=1 requires the HGRP emission on the second Mix Shader input; "
            f"found shader inputs={len(shader_sockets)}, source indices={source_shader_indices} in {hair_tree.name}"
        )
    source_response = _node(hair_tree, "ShaderNodeValue", "Hair Source Response - independent explicit value")
    source_response.outputs[0].default_value = 1.0
    for link in tuple(response_mix.inputs["Fac"].links):
        hair_tree.links.remove(link)
    hair_tree.links.new(source_response.outputs[0], response_mix.inputs["Fac"])
    if len(response_mix.inputs["Fac"].links) != 1 or response_mix.inputs["Fac"].links[0].from_node != source_response:
        raise ValueError("Hair final Source Response is not independently fixed to the explicit value")
    for link in tuple(emission_input.links):
        hair_tree.links.remove(link)
    hair_tree.links.new(addition.outputs[0], emission_input)

    hair_tree["hair_lighting_patch"] = "HGRP Hair source diffuse, split-normal BA, dual-strand response, and independent source response"
    hair_tree["hair_lighting_source"] = "Ruri reconstructed GLSL Endfield_Hair 2033-2165 plus current compiled Hair source variants; explicit host frame, not runtime capture"
    hair_tree["hair_source_variant"] = hair_variant(hair_tree)
    hair_tree["hair_specular_f0_boundary"] = "Current compiled split/shared/scalar branches force F0 metallic to zero; 0.04 * source specular. Metallic tangent blending remains independent."
    hair_tree["hair_lighting_parameters"] = json.dumps(asdict(parameters), separators=(",", ":"), sort_keys=True)
    hair_tree["hair_lighting_shader_frame"] = json.dumps(asdict(parameters.shader_frame), separators=(",", ":"), sort_keys=True)
    return HairBranchReceipt(
        source_variant=hair_variant(hair_tree),
        active_specular_lobes=2 if parameters.use_spec_ramp_map else 1,
        hair_tree=hair_tree.name,
        light_tree=lighting_tree.name,
        specular_normal_texture=split_image_node.image.name if split_image_node is not None else "",
        specular_ramp_texture=ramp_template.image.name if ramp_template is not None else "",
        specular_enable_node=enable.name,
        source_exponents=(200, int(math.trunc(max(1.0 - parameters.anisotropy_range2, 0.0) * 200.0))),
        specular_line_pass_ported=parameters.use_specular_line,
        shader_frame=parameters.shader_frame,
        cast_shadow_preview=None,
        cast_shadow_binding="Same live upstream producer as verified Hair diffuse Cast Shadow",
        specular_normal_uv_map="UV0",
        metallic_tangent_blend=parameters.use_metallic_gloss_map,
        metallic_tangent_handedness="+1 host preview; native tangent handedness is not captured",
        native_diffuse_group=diffuse_tree.name,
        full_diffuse_output=diffuse_node.outputs["Full Diffuse"].name,
        ambient_diffuse_intensity_output=diffuse_node.outputs["Ambient Diffuse Intensity"].name,
        cast_blend_output=diffuse_node.outputs["Cast Blend"].name,
        cp13_w_constructor_value=float(cp13_w.node.outputs[0].default_value),
        source_response_node=source_response.name,
        source_response_value=float(source_response.outputs[0].default_value),
        line_map_texture=line_map.image.name if line_map is not None and line_map.image is not None else "",
        line_map_file_id=parameters.line_map_file_id,
        line_map_path_id=parameters.line_map_path_id,
        line_map_uv_scale=parameters.line_map_scale,
        line_map_uv_offset=parameters.line_map_offset,
    )


def set_hair_specular_enabled(hair_tree: bpy.types.NodeTree, enabled: bool) -> None:
    """Set the explicit preview switch for the reconstructed dual-strand term."""
    node = _find_node(hair_tree, "Hair HGRP Specular Enable - diagnostic switch", "ShaderNodeValue")
    node.outputs[0].default_value = 1.0 if enabled else 0.0
