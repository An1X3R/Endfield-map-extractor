"""Build alpha-aware shadow proxy geometry node groups."""
from __future__ import annotations

from math import isfinite

import bpy


def _number(value: object, name: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError(f"{name} must be numeric, got {type(value).__name__}")
    result = float(value)
    if not isfinite(result):
        raise ValueError(f"{name} must be finite, got {result}")
    return result


def _uv_pair(value: tuple[float, float], name: str) -> tuple[float, float]:
    if not isinstance(value, tuple) or len(value) != 2:
        raise ValueError(f"{name} must be a two-number tuple")
    return _number(value[0], f"{name}[0]"), _number(value[1], f"{name}[1]")


def _validate_common(
    name: str,
    packed_image: bpy.types.Image,
    uv_layer: str,
    uv_scale: tuple[float, float],
    uv_offset: tuple[float, float],
    color_alpha: float,
    subdivision_level: int,
) -> tuple[float, float, float, float, float]:
    if not isinstance(name, str) or not name.strip():
        raise ValueError("name must be a non-empty string")
    if len(name) > 63:
        raise ValueError("name must not exceed Blender's 63-character node-tree limit")
    if not isinstance(packed_image, bpy.types.Image):
        raise TypeError("packed_image must be a Blender Image datablock")
    registered = bpy.data.images.get(packed_image.name)
    if registered is None or registered.as_pointer() != packed_image.as_pointer():
        raise ValueError("packed_image is not registered in bpy.data.images")
    if packed_image.packed_file is None:
        raise ValueError(f"Image {packed_image.name!r} must be packed into the blend file")
    if not packed_image.has_data or min(packed_image.size) < 1:
        raise ValueError(f"Image {packed_image.name!r} has no pixel data")
    if not isinstance(uv_layer, str) or not uv_layer.strip():
        raise ValueError("uv_layer must be a non-empty UV attribute name")

    scale_x, scale_y = _uv_pair(uv_scale, "uv_scale")
    offset_x, offset_y = _uv_pair(uv_offset, "uv_offset")
    alpha = _number(color_alpha, "color_alpha")
    if alpha < 0.0 or alpha > 1.0:
        raise ValueError(f"color_alpha must be from 0 through 1, got {alpha}")
    if isinstance(subdivision_level, bool) or not isinstance(subdivision_level, int):
        raise ValueError("subdivision_level must be an integer from 0 through 2")
    if subdivision_level < 0 or subdivision_level > 2:
        raise ValueError(f"subdivision_level must be from 0 through 2, got {subdivision_level}")
    return scale_x, scale_y, offset_x, offset_y, alpha


def _node(
    tree: bpy.types.NodeTree,
    identifier: str,
    label: str,
    x: float,
    y: float,
) -> bpy.types.Node:
    if len(label) > 63:
        raise ValueError(f"Node label exceeds Blender's 63-character limit: {label}")
    node = tree.nodes.new(identifier)
    node.label = label
    node.location = (x, y)
    return node


def _alpha_graph(
    name: str,
    packed_image: bpy.types.Image,
    uv_layer: str,
    uv_scale: tuple[float, float],
    uv_offset: tuple[float, float],
    color_alpha: float,
    subdivision_level: int,
) -> tuple[bpy.types.NodeTree, bpy.types.NodeSocket, bpy.types.NodeSocket, bpy.types.Node]:
    scale_x, scale_y, offset_x, offset_y, alpha = _validate_common(
        name,
        packed_image,
        uv_layer,
        uv_scale,
        uv_offset,
        color_alpha,
        subdivision_level,
    )

    tree = bpy.data.node_groups.new(name, "GeometryNodeTree")
    tree.interface.new_socket(name="Geometry", in_out="INPUT", socket_type="NodeSocketGeometry")
    tree.interface.new_socket(name="Geometry", in_out="OUTPUT", socket_type="NodeSocketGeometry")
    group_input = _node(tree, "NodeGroupInput", "Proxy mesh input", -1000.0, 420.0)
    group_output = _node(tree, "NodeGroupOutput", "Alpha-filtered proxy", 1100.0, 420.0)

    subdivide = _node(tree, "GeometryNodeSubdivideMesh", "Subdivide before alpha sampling", -760.0, 420.0)
    subdivide.inputs["Level"].default_value = subdivision_level
    tree.links.new(group_input.outputs["Geometry"], subdivide.inputs["Mesh"])

    uv = _node(tree, "GeometryNodeInputNamedAttribute", "Read source UV layer", -1000.0, 40.0)
    uv.data_type = "FLOAT_VECTOR"
    uv.inputs["Name"].default_value = uv_layer

    uv_scale_node = _node(tree, "ShaderNodeVectorMath", "Apply UV scale", -740.0, 40.0)
    uv_scale_node.operation = "MULTIPLY"
    uv_scale_node.inputs[0].default_value = (scale_x, scale_y, 1.0)
    tree.links.new(uv.outputs["Attribute"], uv_scale_node.inputs[1])

    uv_offset_node = _node(tree, "ShaderNodeVectorMath", "Apply UV offset", -500.0, 40.0)
    uv_offset_node.operation = "ADD"
    uv_offset_node.inputs[0].default_value = (offset_x, offset_y, 0.0)
    tree.links.new(uv_scale_node.outputs["Vector"], uv_offset_node.inputs[1])

    texture = _node(tree, "GeometryNodeImageTexture", "Sample packed BaseMap alpha", -240.0, 40.0)
    texture.interpolation = "Linear"
    texture.extension = "REPEAT"
    texture.inputs["Image"].default_value = packed_image
    tree.links.new(uv_offset_node.outputs["Vector"], texture.inputs["Vector"])

    alpha_product = _node(tree, "ShaderNodeMath", "BaseMap alpha times BaseColor alpha", 20.0, 40.0)
    alpha_product.operation = "MULTIPLY"
    alpha_product.inputs[1].default_value = alpha
    tree.links.new(texture.outputs["Alpha"], alpha_product.inputs[0])

    tree["alpha_image"] = packed_image.name
    tree["uv_layer"] = uv_layer
    tree["uv_scale"] = [scale_x, scale_y]
    tree["uv_offset"] = [offset_x, offset_y]
    tree["color_alpha"] = alpha
    tree["subdivision_level"] = subdivision_level
    tree["sampling_domain"] = "FACE after subdivision"
    return tree, subdivide.outputs["Mesh"], alpha_product.outputs["Value"], group_output


def alpha_clip(
    name: str,
    packed_image: bpy.types.Image,
    uv_layer: str,
    uv_scale: tuple[float, float],
    uv_offset: tuple[float, float],
    color_alpha: float,
    threshold: float,
    subdivision_level: int,
) -> bpy.types.NodeTree:
    """Delete subdivided proxy faces where sampled alpha is below threshold."""
    clip_threshold = _number(threshold, "threshold")
    if clip_threshold < 0.0 or clip_threshold > 1.0:
        raise ValueError(f"threshold must be from 0 through 1, got {clip_threshold}")

    tree, geometry, alpha, group_output = _alpha_graph(
        name,
        packed_image,
        uv_layer,
        uv_scale,
        uv_offset,
        color_alpha,
        subdivision_level,
    )
    remove_test = _node(tree, "ShaderNodeMath", "Delete where alpha is below threshold", 280.0, 40.0)
    remove_test.operation = "LESS_THAN"
    remove_test.inputs[1].default_value = clip_threshold
    tree.links.new(alpha, remove_test.inputs[0])

    delete = _node(tree, "GeometryNodeDeleteGeometry", "Delete transparent faces", 560.0, 420.0)
    delete.domain = "FACE"
    tree.links.new(geometry, delete.inputs["Geometry"])
    tree.links.new(remove_test.outputs["Value"], delete.inputs["Selection"])
    tree.links.new(delete.outputs["Geometry"], group_output.inputs["Geometry"])

    tree["alpha_mode"] = "clip"
    tree["threshold"] = clip_threshold
    tree["coverage_formula"] = "BaseMap.a * BaseColor.a >= threshold"
    return tree


def alpha_dither(
    name: str,
    packed_image: bpy.types.Image,
    uv_layer: str,
    uv_scale: tuple[float, float],
    uv_offset: tuple[float, float],
    color_alpha: float,
    seed: int,
    subdivision_level: int,
) -> bpy.types.NodeTree:
    """Delete faces using stable per-face coverage as a screen-door approximation."""
    if isinstance(seed, bool) or not isinstance(seed, int):
        raise ValueError(f"seed must be an integer, got {type(seed).__name__}")
    if seed < -2147483648 or seed > 2147483647:
        raise ValueError(f"seed must fit a signed 32-bit integer, got {seed}")

    tree, geometry, alpha, group_output = _alpha_graph(
        name,
        packed_image,
        uv_layer,
        uv_scale,
        uv_offset,
        color_alpha,
        subdivision_level,
    )
    coverage = _node(tree, "ShaderNodeMath", "Square root alpha coverage", 280.0, 40.0)
    coverage.operation = "SQRT"
    tree.links.new(alpha, coverage.inputs[0])

    index = _node(tree, "GeometryNodeInputIndex", "Stable face index", 20.0, -300.0)
    random = _node(tree, "FunctionNodeRandomValue", "Stable face-hash approximation", 280.0, -300.0)
    random.data_type = "FLOAT"
    if any(
        not random.inputs[index].enabled or random.inputs[index].bl_idname != "NodeSocketInt"
        for index in (7, 8)
    ):
        raise RuntimeError("Blender Random Value node has an unexpected ID/Seed socket layout")
    if any(
        not random.inputs[index].enabled or random.inputs[index].bl_idname != "NodeSocketFloat"
        for index in (2, 3)
    ) or not random.outputs[1].enabled or random.outputs[1].bl_idname != "NodeSocketFloat":
        raise RuntimeError("Blender Random Value FLOAT sockets do not match the expected API")
    random.inputs[2].default_value = 0.0
    random.inputs[3].default_value = 1.0
    random.inputs[8].default_value = seed
    tree.links.new(index.outputs["Index"], random.inputs[7])

    remove_test = _node(tree, "ShaderNodeMath", "Delete above alpha coverage", 560.0, 40.0)
    remove_test.operation = "GREATER_THAN"
    tree.links.new(random.outputs[1], remove_test.inputs[0])
    tree.links.new(coverage.outputs["Value"], remove_test.inputs[1])

    delete = _node(tree, "GeometryNodeDeleteGeometry", "Delete dithered faces", 820.0, 420.0)
    delete.domain = "FACE"
    tree.links.new(geometry, delete.inputs["Geometry"])
    tree.links.new(remove_test.outputs["Value"], delete.inputs["Selection"])
    tree.links.new(delete.outputs["Geometry"], group_output.inputs["Geometry"])

    tree["alpha_mode"] = "dither"
    tree["dither_seed"] = seed
    tree["coverage_formula"] = "sqrt(BaseMap.a * BaseColor.a)"
    tree["dither_semantics"] = "Stable per-face hash approximation; not native screen-pixel dithering"
    return tree
