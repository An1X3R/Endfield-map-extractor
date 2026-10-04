from __future__ import annotations

import argparse
import json
import math
import re
import sqlite3
import struct
import sys
from collections import defaultdict
from pathlib import Path

import bpy
from mathutils import Matrix, Vector


STRICT_ECS_GEOMETRY_LOD_EXCEPTIONS = {
    "S_mine_excavator_decal+1_001_02_lod1": {
        "lod": 1,
        "mesh_ref": (
            "cab-85f665dedc28d55f4ff4f1c8bcc95818",
            -3270064687873396116,
        ),
        "mesh_name": "s_mine_excavator_decal_1_001_02_lod1",
        "mesh_container": (
            "assets/beyond/arts/environment/sceneassets/com/prop/"
            "prop_com_excavator+1_001/models/"
            "s_prop_com_decal+1_001_02_lod1.fbx"
        ),
        "mesh_id": "ae0da766c31ce107",
        "evidence_mode": "reviewed_exact_lod1_entity_mesh_exception",
    },
}

MAP_NAME = "map02"
MAP_DISPLAY_NAME = "Wuling"
TERRAIN_ORIGIN_X = -2048.0
TERRAIN_ORIGIN_Z = -2048.0
TERRAIN_BASE_Y = 0.0
TERRAIN_HEIGHT_SCALE = 730.0
TERRAIN_HEIGHT_DENOMINATOR = 32768.0
SCENEPROBE_OBJ_X_UNMIRROR = Matrix.Diagonal((-1.0, 1.0, 1.0, 1.0))


def arguments() -> argparse.Namespace:
    argv = sys.argv[sys.argv.index("--") + 1:] if "--" in sys.argv else []
    parser = argparse.ArgumentParser(description="Build a bounded map02 Wuling scene")
    parser.add_argument("--work-root", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--assets-db", type=Path,
                        help="Alternate resolution database; defaults to work-root/assets.sqlite")
    parser.add_argument(
        "--terrain-surface", type=Path,
        help="Optional extracted terrain surface directory containing terrain_surface.json and baked atlases",
    )
    parser.add_argument("--extra-probe", type=Path, action="append", default=[],
                        help="Additional EndfieldSceneProbe output; may be repeated")
    parser.add_argument(
        "--ecs-material-bindings", type=Path,
        help="Optional reviewed entity_base to ordered material-reference bindings",
    )
    parser.add_argument(
        "--ecs-geometry-bindings", type=Path,
        help="Optional explicit ECS renderer Mesh overrides validated by exact asset reference",
    )
    parser.add_argument(
        "--ecs-instance-bindings", type=Path,
        help="Optional reviewed per-instance ECS Mesh/material variants keyed by exact entity_id",
    )
    parser.add_argument(
        "--reviewed-family-material-bindings", type=Path,
        help="Optional authored-family consensus bindings, kept distinct from ECS evidence",
    )
    parser.add_argument("--no-pack-textures", action="store_true")
    parser.add_argument("--geometry-only", action="store_true",
                        help="Skip game material/texture reconstruction and use neutral geometry")
    parser.add_argument(
        "--import-game-lights", action="store_true",
        help="Import verified original game lights when a light manifest is supplied",
    )
    parser.add_argument(
        "--add-preview-sun", action="store_true",
        help="Add a clearly labelled non-original sun for convenient material preview",
    )
    parser.add_argument("--xmin", type=float)
    parser.add_argument("--xmax", type=float)
    parser.add_argument("--zmin", type=float)
    parser.add_argument("--zmax", type=float)
    parser.add_argument("--include-regex", help="Only keep instances whose base or full name matches this regex")
    parser.add_argument("--exclude-regex", help="Drop instances whose base or full name matches this regex")
    parser.add_argument(
        "--terrain-footprint-only", action="store_true",
        help="Keep only instances whose XZ position falls on an exported terrain tile",
    )
    parser.add_argument(
        "--group-by-streaming-region", action="store_true",
        help="Place instancers in collections named after their InitChunkData region",
    )
    parser.add_argument("--marker-regex", help="Add red diagnostic mesh markers at matching entity positions")
    parser.add_argument(
        "--experimental-newplant-family-materials", action="store_true",
        help="Test a separately labelled material-slot family rule for unbound new-plant meshes",
    )
    parser.add_argument(
        "--experimental-map01-family-materials", action="store_true",
        help="Test labelled same-family palettes for otherwise unbound map01 meshes",
    )
    parser.add_argument(
        "--experimental-newplant-geometry-aliases", action="store_true",
        help="Test explicit runtime-name aliases for unresolved new-plant geometry",
    )
    parser.add_argument(
        "--experimental-tystair-geometry-aliases", action="store_true",
        help="Test exact map01 runtime aliases to the reused base01 tystair LOD0 meshes",
    )
    parser.add_argument(
        "--quarantine-newplant-state-outliers", action="store_true",
        help="Move only strongly terrain-inconsistent new-plant 03a-03g state instances to a hidden collection",
    )
    return parser.parse_args(argv)


def ref_key(ref: dict) -> tuple[str, int]:
    return ref["Source"].casefold(), int(ref["PathId"])


def safe_name(value: str, limit: int = 58) -> str:
    value = re.sub(r"[^0-9A-Za-z_.-]+", "_", value).strip("_") or "unnamed"
    return value[:limit]


def lod_family_name(value: str) -> str:
    """Canonical exact asset stem used only for cross-LOD Renderer evidence."""
    stem = re.sub(r"_lod\d+$", "", value, flags=re.I).casefold()
    # Unity object names in these prefabs sometimes serialize the authored
    # '+1_' separator as '_1_' while the standalone model bundle preserves
    # '+1_'.  Treat only this punctuation spelling as equivalent.
    return stem.replace("+1_", "_1_")


def hlod_material_name(value: str) -> str | None:
    """Return the exact lv009 HLOD material name for a probed mesh subasset."""

    match = re.fullmatch(
        r"s_hlod(?P<lod>[012])_-?\d+_-?\d+_cluster_(?P<cid>-?\d+)",
        str(value),
        re.IGNORECASE,
    )
    if match is None:
        return None
    return f"M_auto_generated_HLOD{match.group('lod')}_map02_lv009_art_{int(match.group('cid'))}"


def streaming_region(source_path: str) -> str:
    match = re.search(r"InitChunkData_(-?\d+)_(-?\d+)_0_0\.bytes$", source_path, re.I)
    if match:
        return f"Region_{int(match.group(1))}_{int(match.group(2))}"
    if "InitChunkData_Global" in source_path:
        return "Region_Global"
    return "Region_Other"


def unity_matrix(blob: bytes) -> Matrix:
    values = struct.unpack("<16f", blob)
    source = Matrix(tuple(tuple(values[column * 4 + row] for column in range(4)) for row in range(4)))
    basis = Matrix(((1, 0, 0, 0), (0, 0, -1, 0), (0, 1, 0, 0), (0, 0, 0, 1)))
    return basis @ source @ basis.inverted()


def import_obj(path: Path) -> list[bpy.types.Object]:
    before = set(bpy.data.objects)
    bpy.ops.wm.obj_import(
        filepath=str(path), forward_axis="NEGATIVE_Z", up_axis="Y",
        use_split_objects=False, use_split_groups=True,
    )
    imported = [obj for obj in set(bpy.data.objects) - before if obj.type == "MESH"]
    # Blender stores the OBJ Y-up -> Blender Z-up conversion on the temporary
    # imported object. The builder keeps only its mesh datablock, so bake that
    # matrix into the vertices before discarding the object.
    #
    # EndfieldSceneProbe/1 writes OBJ vertices and normals as (-x, y, z) and
    # reverses triangle winding. Undo that exporter reflection after the axis
    # conversion; otherwise asymmetric meshes are mirrored around their entity
    # pivot even though the authoritative ECS world matrix is correct.
    for obj in imported:
        obj.data.transform(obj.matrix_world)
        obj.data.transform(SCENEPROBE_OBJ_X_UNMIRROR)
        obj.data.flip_normals()
        obj.data.update()
        obj.matrix_world = Matrix.Identity(4)
    def group_order(obj):
        match = re.search(r"_(\d+)$", obj.name)
        return (int(match.group(1)) if match else 0, obj.name.casefold())
    return sorted(imported, key=group_order)


def join_submeshes(objects: list[bpy.types.Object]) -> bpy.types.Object:
    if len(objects) == 1:
        return objects[0]
    bpy.ops.object.select_all(action="DESELECT")
    for obj in objects:
        obj.hide_set(False)
        obj.select_set(True)
    result = objects[0]
    bpy.context.view_layer.objects.active = result
    bpy.ops.object.join()
    return result


def experimental_newplant_material_names(model_name: str, submesh_count: int) -> list[str]:
    """Return a review-only family palette; never enabled in normal exports."""
    stem = re.sub(r"_lod\d+$", "", model_name, flags=re.I).casefold()
    match = re.search(r"newplant\+1_001_(\d+)[a-z]*$", stem)
    if not match or submesh_count < 1:
        return []
    group = match.group(1)
    palettes = {
        "01": [
            "M_build_map01_newplant+1_001_01", "M_build_map01_newplant+1_001_02",
            "M_build_map01_newplant+1_001_03", "M_build_map01_oldplant+1_001_06e",
        ],
        "02": (["M_build_map01_newplant+1_001_02"] if submesh_count == 1 else [
            "M_build_map01_newplant+1_001_01b", "M_build_map01_oldplant+1_001_06e",
            "M_mod_map01_building+1_019_100",
        ]),
        "03": [
            "M_build_map01_oldplant+1_001_06e", "M_build_map01_newplant+1_001_01b",
            "M_mod_map01_building+1_019_100", "M_mod_map01_ruins+1_001_01",
        ],
        "04": (["M_build_map01_newplant+1_001_05"] if submesh_count == 1 else [
            "M_mdecal_com_road+1_001_08a", "M_build_map01_newplant+1_001_05",
            "M_build_map01_newplant+1_001_01", "M_mod_map01_building+1_019_100",
            "M_mod_map01_road+1_001_13",
        ]),
        "05": [
            "M_build_map01_newplant+1_001_05", "M_build_map01_newplant+1_001_06",
            "M_build_map01_newplant+1_001_01", "M_mod_map01_building+1_019_100",
            "M_mod_map01_ruins+1_001_01",
        ],
        "06": [
            "M_build_map01_newplant+1_001_06", "M_build_map01_newplant+1_001_05",
            "M_build_map01_newplant+1_001_01", "M_mod_map01_building+1_019_100",
            "M_mod_map01_ruins+1_001_01",
        ],
        "07": [
            "M_build_map01_newplant+1_001_05", "M_build_map01_newplant+1_001_01",
            "M_mod_map01_building+1_019_100", "M_mod_map01_road+1_001_13",
            "M_mdecal_com_road+1_001_08a",
        ],
        "09": [
            "M_build_map01_newplant+1_001_01", "M_build_map01_newplant+1_001_02",
            "M_build_map01_newplant+1_001_03", "M_build_map01_oldplant+1_001_06e",
        ],
    }
    palette = palettes.get(group, [])
    return palette[:submesh_count] if len(palette) >= submesh_count else []


def exact_map01_alias_material_name(model_name: str) -> str | None:
    """Map runtime mesh aliases to the exact authored material basename."""
    stem = re.sub(r"_lod\d+$", "", model_name, flags=re.I)
    aliases = (
        ("s_prop_tundra_", "M_prop_map01_"),
        ("s_prop_", "M_prop_map01_"),
        ("s_module_", "M_mod_map01_"),
        ("s_mod_map01_", "M_mod_map01_"),
        ("s_build_map01_", "M_build_map01_"),
        ("s_rock_map01_", "M_rock_map01_"),
        ("s_erosion_tundra_", "M_erosion_map01_"),
        ("s_anm_tundra_", "M_prop_map01_"),
        ("s_farmod_tundra_", "M_farmod_map01_"),
    )
    lowered = stem.casefold()
    for prefix, replacement in aliases:
        if lowered.startswith(prefix):
            return replacement + stem[len(prefix):]
    return None


def experimental_map01_family_material_names(model_name: str, submesh_count: int) -> list[str]:
    """Review-only palettes from the exact authored family; never a default rule."""
    stem = re.sub(r"_lod\d+$", "", model_name, flags=re.I).casefold()
    # The 029 kit contains several geometry variants whose exact-numbered
    # material asset does not exist.  Their authored siblings demonstrate the
    # shared semantic surface (floor/shell/wall/indoor), so keep these explicit
    # instead of applying a fuzzy numeric-neighbour rule to unrelated kits.
    building_029_shared = {
        "s_module_building+1_029_floor_02": ["M_mod_map01_building+1_029_floor_01"],
        "s_module_building+1_029_floor_05": ["M_mod_map01_building+1_029_floor_01"],
        "s_module_building+1_029_floor_06": ["M_mod_map01_building+1_029_floor_01"],
        "s_module_building+1_029_shell_03": ["M_mod_map01_building+1_029_shell_02"],
        "s_module_building+1_029_shell_05": ["M_mod_map01_building+1_029_shell_01"],
        "s_module_building+1_029_shell_06": ["M_mod_map01_building+1_029_shell_01"],
        "s_module_building+1_029_indoor_02": ["M_mod_map01_building+1_019_01k"],
        "s_module_building+1_029_outdoor_01": ["M_mod_map01_building+1_029_12"],
        "s_module_building+1_029_wall_02": ["M_mod_map01_building+1_029_wall_01"],
        "s_module_building+1_029_lightboard_01": ["M_mod_map01_building+1_029_08"],
        "s_module_building+1_029_lightboard_02": ["M_mod_map01_building+1_029_08"],
    }
    palette = building_029_shared.get(stem)
    if palette:
        return [palette[index % len(palette)] for index in range(submesh_count)]
    families = (
        # All eight authored base01 stair geometry variants share the only
        # material asset in their exact kit directory.
        ("s_mod_base01_tystair+1_001_", ["M_mod_base01_tystair+1_001_01"]),
        ("s_mod_map01_wall+1_005_", ["M_mod_map01_wall+1_005_01"]),
        ("s_build_map01_station+1_002_", [
            "M_build_map01_stationt+1_001_01", "M_build_map01_stationt+1_001_02",
        ]),
        ("s_mod_map01_warehouse+1_001_", [
            "M_mod_map01_warehouse+1_001_01", "M_mod_map01_warehouse+1_001_02",
            "M_mod_map01_warehouse+1_001_03",
        ]),
        ("s_mod_map01_ruins+2_002_", [
            "M_mod_map01_ruins+2_002_01a", "M_mod_map01_ruins+2_002_02",
        ]),
        ("s_module_maintenancewall+1_001_", [
            "M_mod_map01_maintenancewall+1_001_01", "M_mod_map01_maintenancewall+1_001_03",
        ]),
        ("s_module_building+1_015_combine_03", [
            "M_mod_map01_building+1_015_combine_03_mask_01",
            "M_mod_map01_building+1_015_combine_03_mask_03",
        ]),
        ("s_module_tywall+1_002_", ["M_mod_map01_tywall+1_003_01"]),
        ("s_mod_map01_tywall+1_002_", ["M_mod_map01_tywall+1_003_01"]),
        ("s_prop_tundra_lv007line+1_001_", ["M_prop_map01_pipe+1_003_01"]),
        ("s_anm_tundra_zmdmachine+1_003_01", ["M_mod_map01_bridge+1_002_03"]),
        ("s_module_relic+1_001_floor_", [
            "M_mod_map01_relic+1_001_floor_01", "M_mod_map01_relic+1_001_floor_01a",
        ]),
        ("s_farmod_tundra_medium+1_001_14", ["M_farmod_map01_medium+2_001_14"]),
        # building+1_019 uses per-entity ECS material sequences. A cyclic
        # family palette visibly misassigned facade, roof, and floor slots, so
        # unresolved members must remain neutral until authoritative evidence
        # is available through --ecs-material-bindings.
        ("s_mod_map01_road+1_003_", [
            "M_mod_map01_road+1_003_01", "M_mod_map01_road+1_003_05",
        ]),
        ("s_prop_groundpile+1_001_", ["M_prop_map01_groundpile+1_001_01"]),
        ("s_module_steelframe+1_002_", ["M_mod_map01_steelframe+1_002_01"]),
        ("s_module_building+1_014_roof_", [
            "M_mod_map01_building+1_014_03", "M_mod_map01_building+1_014_04",
            "M_mod_map01_building+1_014_05", "M_mod_map01_building+1_014_05a",
        ]),
        ("s_prop_pipe+1_001_", [
            "M_prop_map01_pipe+1_001_01", "M_prop_map01_pipe+1_001_02",
        ]),
        ("s_prop_guardrail+1_001_", ["M_prop_map01_guardrail+1_001_03"]),
        ("s_mod_map01_ruins+1_001_", ["M_mod_map01_ruins+1_001_01"]),
        ("s_prop_tymachine+1_006_", [
            "M_module_buildingspace+1_001_01", "M_module_buildingspace+1_001_02",
        ]),
    )
    for prefix, palette in families:
        if stem.startswith(prefix):
            # A few direct meshes contain more OBJ groups than the sibling
            # prefab exposes unique materials. Repeating the authored family
            # palette is safer than inventing unrelated materials and remains
            # explicitly experimental in template metadata.
            return [palette[index % len(palette)] for index in range(submesh_count)]
    return []


def _input(node, *names):
    for name in names:
        if name in node.inputs:
            return node.inputs[name]
    return None


def layout_shader_node_tree(node_tree: bpy.types.NodeTree) -> None:
    """Arrange generated shader nodes without changing graph semantics."""
    nodes = list(node_tree.nodes)
    if not nodes:
        return

    outgoing: dict[str, list[tuple[str, str]]] = defaultdict(list)
    for link in node_tree.links:
        outgoing[link.from_node.name].append(
            (link.to_node.name, link.to_socket.name)
        )

    node_by_name = {node.name: node for node in nodes}
    output_names = {
        node.name for node in nodes if node.type == "OUTPUT_MATERIAL"
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
            else:
                lanes.append(terminal_lane(target_name, trail | {name}))
        value = sum(lanes) / len(lanes) if lanes else -1120.0
        lane_cache[name] = value
        return value

    columns: dict[int, list[bpy.types.Node]] = defaultdict(list)
    for node in nodes:
        columns[distance_to_output(node.name, frozenset())].append(node)

    for depth, column in columns.items():
        column.sort(
            key=lambda node: (
                -terminal_lane(node.name, frozenset()),
                node.type,
                node.name.casefold(),
            )
        )
        previous_y = None
        for node in column:
            desired_y = terminal_lane(node.name, frozenset())
            y = desired_y
            if previous_y is not None:
                y = min(y, previous_y - 220.0)
            node.location = (900.0 - depth * 300.0, y)
            previous_y = y


def _texture_node(nodes, links, image, prop: str, slot: dict):
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


def create_material(record: dict, texture_files: dict,
                    pack_textures: bool) -> bpy.types.Material:
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
    # This exact material was validated against its ECS slot, authored Layer1
    # textures and the dedicated 019_30 blend mask. Keep the rule narrow until
    # other LayerBlend variants have equivalent render evidence.
    reviewed_layer_blend = (
        record.get("Name", "").casefold() == "m_mod_map01_building+1_019_27d"
        and floats.get("_LayerBlend", 0.0) > 0.5
    )
    base_used = normal_used = mro_used = emissive_used = mask_used = False
    base_texture = None
    mask_texture = None
    layer_base_texture = None
    layer_mask_texture = None
    for slot in record.get("Textures", []):
        ref = slot.get("Texture")
        texture_entry = texture_files.get(ref_key(ref)) if ref else None
        if not texture_entry:
            continue
        root, relative = texture_entry
        prop = slot["Property"]
        role = ("base" if prop in base_names and not base_used else
                "normal" if prop in normal_names and not normal_used else
                "mro" if prop in mro_names and not mro_used else
                "emissive" if prop in emissive_names and not emissive_used else None)
        if prop == "_MaskMap" and tri_channel_mask and not mask_used:
            role = "mask"
        elif prop == "_Layer1BaseMap" and reviewed_layer_blend:
            role = "layer_base"
        elif prop == "_LayerBlendMaskMap" and reviewed_layer_blend:
            role = "layer_mask"
        if role is None:
            continue
        image_path = root / relative.replace("\\", "/")
        if not image_path.exists():
            continue
        image = bpy.data.images.load(str(image_path), check_existing=True)
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
            # Tri-channel materials recolour the base albedo with an RGB mask.
            # Defer the Base Color link until the mask node chain is available.
            if tri_channel_mask:
                pass
            elif tuple(base_color) != (1.0, 1.0, 1.0, 1.0):
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
        elif role == "layer_base":
            image.colorspace_settings.name = "sRGB"
            image.alpha_mode = "CHANNEL_PACKED"
            layer_base_texture = texture
        elif role == "layer_mask":
            image.colorspace_settings.name = "Non-Color"
            image.alpha_mode = "CHANNEL_PACKED"
            layer_mask_texture = texture
        elif role == "normal":
            image.colorspace_settings.name = "Non-Color"
            image.alpha_mode = "CHANNEL_PACKED"
            normal = nodes.new("ShaderNodeNormalMap")
            normal.inputs["Strength"].default_value = max(0.0, floats.get("_NormalScale", 1.0))
            packed_nro = bool(ref and str(ref.get("Name", "")).casefold().endswith("_nro"))
            if packed_nro:
                # Endfield NRO textures store tangent-space normal X/Y in R/G,
                # roughness in B and occlusion in A. Feeding RGB directly into
                # Blender's Normal Map node interprets roughness as normal Z and
                # creates severe black/white faceting on otherwise smooth paint.
                separate_nro = nodes.new("ShaderNodeSeparateColor")
                separate_nro.label = "Endfield NRO: R/G normal, B roughness, A occlusion"
                links.new(texture.outputs["Color"], separate_nro.inputs["Color"])

                signed = []
                for channel in ("Red", "Green"):
                    multiply = nodes.new("ShaderNodeMath")
                    multiply.operation = "MULTIPLY"
                    multiply.inputs[1].default_value = 2.0
                    links.new(separate_nro.outputs[channel], multiply.inputs[0])
                    subtract = nodes.new("ShaderNodeMath")
                    subtract.operation = "SUBTRACT"
                    subtract.inputs[1].default_value = 1.0
                    links.new(multiply.outputs[0], subtract.inputs[0])
                    signed.append(subtract.outputs[0])

                squares = []
                for component in signed:
                    square = nodes.new("ShaderNodeMath")
                    square.operation = "MULTIPLY"
                    links.new(component, square.inputs[0])
                    links.new(component, square.inputs[1])
                    squares.append(square.outputs[0])
                sum_xy = nodes.new("ShaderNodeMath")
                sum_xy.operation = "ADD"
                links.new(squares[0], sum_xy.inputs[0])
                links.new(squares[1], sum_xy.inputs[1])
                remaining = nodes.new("ShaderNodeMath")
                remaining.operation = "SUBTRACT"
                remaining.inputs[0].default_value = 1.0
                links.new(sum_xy.outputs[0], remaining.inputs[1])
                clamp = nodes.new("ShaderNodeMath")
                clamp.operation = "MAXIMUM"
                clamp.inputs[1].default_value = 0.0
                links.new(remaining.outputs[0], clamp.inputs[0])
                z_value = nodes.new("ShaderNodeMath")
                z_value.operation = "SQRT"
                links.new(clamp.outputs[0], z_value.inputs[0])

                encoded = []
                for component in (*signed, z_value.outputs[0]):
                    scale_half = nodes.new("ShaderNodeMath")
                    scale_half.operation = "MULTIPLY"
                    scale_half.inputs[1].default_value = 0.5
                    links.new(component, scale_half.inputs[0])
                    add_half = nodes.new("ShaderNodeMath")
                    add_half.operation = "ADD"
                    add_half.inputs[1].default_value = 0.5
                    links.new(scale_half.outputs[0], add_half.inputs[0])
                    encoded.append(add_half.outputs[0])
                combine = nodes.new("ShaderNodeCombineColor")
                combine.label = "Reconstructed tangent normal from NRO R/G"
                for channel, component in zip(("Red", "Green", "Blue"), encoded):
                    links.new(component, combine.inputs[channel])
                links.new(combine.outputs["Color"], normal.inputs["Color"])
                # NRO blue is the material's authored per-pixel roughness.
                links.new(separate_nro.outputs["Blue"], _input(principled, "Roughness"))
                result["endfield_packed_nro_reconstruction"] = (
                    "normal XY reconstructed from R/G; B drives roughness; A retained"
                )
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
            links.new(separate.outputs["Green"], _input(principled, "Roughness"))
            mro_used = True
        elif role == "emissive" and (floats.get("_EnableEmissiveMap", 0.0) > 0.5 or
                                      floats.get("_EnableBakedEmissive", 0.0) > 0.5):
            image.colorspace_settings.name = "sRGB"
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
        albedo.label = "Endfield base albedo 脳 tri-channel tint"
        albedo.blend_type = "MULTIPLY"
        albedo.inputs[0].default_value = 1.0
        links.new(base_texture.outputs["Color"], albedo.inputs[1])
        links.new(current, albedo.inputs[2])
        links.new(albedo.outputs[0], _input(principled, "Base Color"))
        result["endfield_tri_channel_mask_reconstruction"] = (
            "BaseColorMap multiplied by sequential R/G/B authored albedo tints, "
            "using each tint alpha as its authored blend strength"
        )
    if reviewed_layer_blend and layer_base_texture is not None and layer_mask_texture is not None:
        base_input = _input(principled, "Base Color")
        if not base_input.links:
            raise RuntimeError(f"Reviewed LayerBlend material has no base color: {record['Name']}")
        current_color = base_input.links[0].from_socket
        links.remove(base_input.links[0])
        separate = nodes.new("ShaderNodeSeparateColor")
        separate.label = "Endfield reviewed LayerBlend mask"
        links.new(layer_mask_texture.outputs["Color"], separate.inputs["Color"])
        invert = nodes.new("ShaderNodeInvert")
        invert.label = "Endfield 019_30 inverse red LayerBlend mask"
        invert.inputs["Fac"].default_value = 1.0
        links.new(separate.outputs["Red"], invert.inputs["Color"])
        layer_color = layer_base_texture.outputs["Color"]
        layer_tint = colors.get("_Layer1TintColor", [1.0, 1.0, 1.0, 1.0])
        if tuple(layer_tint) != (1.0, 1.0, 1.0, 1.0):
            tint = nodes.new("ShaderNodeMixRGB")
            tint.label = "Endfield _Layer1TintColor"
            tint.blend_type = "MULTIPLY"
            tint.inputs[0].default_value = 1.0
            tint.inputs[2].default_value = layer_tint
            links.new(layer_color, tint.inputs[1])
            layer_color = tint.outputs[0]
        mix = nodes.new("ShaderNodeMixRGB")
        mix.label = "Endfield reviewed 019_27d Layer1"
        mix.blend_type = "MIX"
        links.new(invert.outputs["Color"], mix.inputs[0])
        links.new(current_color, mix.inputs[1])
        links.new(layer_color, mix.inputs[2])
        links.new(mix.outputs["Color"], base_input)
        result["endfield_reviewed_layer_blend"] = (
            "M_mod_map01_building+1_019_27d: inverse red _LayerBlendMaskMap mixes Layer1"
        )
    if alpha_test:
        threshold = min(1.0, max(0.0, floats.get("_AlphaClipThreshold", 0.5)))
        result.alpha_threshold = threshold
        if hasattr(result, "surface_render_method"):
            result.surface_render_method = "DITHERED"
        result["endfield_alpha_clip_threshold"] = threshold
    if alpha_blend:
        if hasattr(result, "surface_render_method"):
            result.surface_render_method = "DITHERED"
        result["endfield_alpha_blend"] = True
    result["endfield_material_source"] = f'{record.get("Source", "unknown")}:{record.get("PathId", 0)}'
    result["endfield_mro_interpretation"] = "R=metallic, G=roughness, B=occlusion (B retained in node graph only)"
    layout_shader_node_tree(result.node_tree)
    result["endfield_node_layout"] = (
        "generated left-to-right by output distance and Principled input lane"
    )
    return result


def make_instancer(base: str, template: bpy.types.Object, rows: list[tuple],
                   collection: bpy.types.Collection) -> bpy.types.Object:
    locations, rotations, scales = [], [], []
    for row in rows:
        matrix = unity_matrix(row[2])
        location, rotation, scale = matrix.decompose()
        locations.append(tuple(location))
        rotations.append(tuple(rotation.to_euler("XYZ")))
        scales.append(tuple(scale))
    mesh = bpy.data.meshes.new("points_" + safe_name(base))
    mesh.from_pydata(locations, [], [])
    rotation_attr = mesh.attributes.new("ef_rotation", "FLOAT_VECTOR", "POINT")
    scale_attr = mesh.attributes.new("ef_scale", "FLOAT_VECTOR", "POINT")
    for index in range(len(rows)):
        rotation_attr.data[index].vector = rotations[index]
        scale_attr.data[index].vector = scales[index]
    obj = bpy.data.objects.new("INST_" + safe_name(base), mesh)
    collection.objects.link(obj)
    group = bpy.data.node_groups.new("GN_" + safe_name(base), "GeometryNodeTree")
    group.interface.new_socket(name="Geometry", in_out="INPUT", socket_type="NodeSocketGeometry")
    group.interface.new_socket(name="Geometry", in_out="OUTPUT", socket_type="NodeSocketGeometry")
    group_input = group.nodes.new("NodeGroupInput")
    output = group.nodes.new("NodeGroupOutput")
    object_info = group.nodes.new("GeometryNodeObjectInfo")
    object_info.inputs["Object"].default_value = template
    object_info.transform_space = "ORIGINAL"
    object_info.inputs["As Instance"].default_value = True
    rotation = group.nodes.new("GeometryNodeInputNamedAttribute")
    rotation.data_type = "FLOAT_VECTOR"
    rotation.inputs["Name"].default_value = "ef_rotation"
    scale = group.nodes.new("GeometryNodeInputNamedAttribute")
    scale.data_type = "FLOAT_VECTOR"
    scale.inputs["Name"].default_value = "ef_scale"
    instance = group.nodes.new("GeometryNodeInstanceOnPoints")
    group.links.new(group_input.outputs["Geometry"], instance.inputs["Points"])
    group.links.new(object_info.outputs["Geometry"], instance.inputs["Instance"])
    group.links.new(rotation.outputs["Attribute"], instance.inputs["Rotation"])
    group.links.new(scale.outputs["Attribute"], instance.inputs["Scale"])
    group.links.new(instance.outputs["Instances"], output.inputs["Geometry"])
    group_input.location = (-720.0, 300.0)
    object_info.location = (-720.0, 60.0)
    rotation.location = (-720.0, -180.0)
    scale.location = (-720.0, -400.0)
    instance.location = (-240.0, 80.0)
    output.location = (180.0, 80.0)
    group["endfield_node_layout"] = (
        "generated left-to-right: points/template/attributes -> instances -> output"
    )
    modifier = obj.modifiers.new("Endfield instances", "NODES")
    modifier.node_group = group
    obj["entity_base"] = base
    obj["instance_count"] = len(rows)
    obj["placement_source"] = "InitChunkData type18/type27 authoritative matrix"
    return obj


def build_terrain(database: Path, collection: bpy.types.Collection,
                  bounds: tuple[float, float, float, float] | None,
                  surface_root: Path | None = None, pack_textures: bool = True) -> tuple[int, str]:
    material_status = "height geometry only; splat/material reconstruction pending"
    surface_manifest = None
    if surface_root is not None:
        manifest_path = surface_root / "terrain_surface.json"
        if not manifest_path.exists():
            raise RuntimeError(f"Missing terrain surface manifest: {manifest_path}")
        surface_manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        atlas_bounds = surface_manifest["atlas_world_bounds"]
        if bounds is not None and (
            atlas_bounds[1] <= bounds[0] or atlas_bounds[0] >= bounds[1]
            or atlas_bounds[3] <= bounds[2] or atlas_bounds[2] >= bounds[3]
        ):
            return 0, "terrain surface outside the selected bounds"
        albedo_path = surface_root / surface_manifest["albedo"]
        if not albedo_path.exists():
            raise RuntimeError(f"Missing terrain albedo atlas: {albedo_path}")
        normal_path = surface_root / surface_manifest["normal"]
        if not normal_path.exists():
            raise RuntimeError(f"Missing terrain normal atlas: {normal_path}")
        material = bpy.data.materials.new("Terrain_GameBakedAlbedo")
        material.use_nodes = True
        material.diffuse_color = (0.3, 0.3, 0.3, 1.0)
        nodes = material.node_tree.nodes
        links = material.node_tree.links
        nodes.clear()
        output_node = nodes.new("ShaderNodeOutputMaterial")
        shader = nodes.new("ShaderNodeBsdfPrincipled")
        shader.inputs["Roughness"].default_value = 0.86
        uv_node = nodes.new("ShaderNodeUVMap")
        uv_node.uv_map = "TerrainWorldUV"
        image_node = nodes.new("ShaderNodeTexImage")
        image = bpy.data.images.load(str(albedo_path), check_existing=True)
        image.colorspace_settings.name = "Non-Color"
        if pack_textures and not image.packed_file:
            image.pack()
        image_node.image = image
        image_node.extension = "CLIP"
        normal_image_node = nodes.new("ShaderNodeTexImage")
        normal_image = bpy.data.images.load(str(normal_path), check_existing=True)
        normal_image.colorspace_settings.name = "Non-Color"
        normal_image.alpha_mode = "CHANNEL_PACKED"
        if pack_textures and not normal_image.packed_file:
            normal_image.pack()
        normal_image_node.image = normal_image
        normal_image_node.extension = "CLIP"
        separate_normal = nodes.new("ShaderNodeSeparateColor")
        separate_normal.mode = "RGB"
        invert_green = nodes.new("ShaderNodeMath")
        invert_green.operation = "SUBTRACT"
        invert_green.inputs[0].default_value = 1.0
        combine_normal = nodes.new("ShaderNodeCombineColor")
        combine_normal.mode = "RGB"
        normal_map = nodes.new("ShaderNodeNormalMap")
        normal_map.space = "TANGENT"
        normal_map.uv_map = "TerrainWorldUV"
        links.new(uv_node.outputs["UV"], image_node.inputs["Vector"])
        links.new(uv_node.outputs["UV"], normal_image_node.inputs["Vector"])
        links.new(image_node.outputs["Color"], shader.inputs["Base Color"])
        links.new(normal_image_node.outputs["Color"], separate_normal.inputs["Color"])
        links.new(separate_normal.outputs["Red"], combine_normal.inputs["Red"])
        links.new(separate_normal.outputs["Green"], invert_green.inputs[1])
        links.new(invert_green.outputs[0], combine_normal.inputs["Green"])
        links.new(separate_normal.outputs["Blue"], combine_normal.inputs["Blue"])
        links.new(combine_normal.outputs["Color"], normal_map.inputs["Color"])
        links.new(normal_map.outputs["Normal"], shader.inputs["Normal"])
        links.new(shader.outputs["BSDF"], output_node.inputs["Surface"])
        layout_shader_node_tree(material.node_tree)
        material["endfield_node_layout"] = (
            "generated left-to-right by output distance and Principled input lane"
        )
        material["endfield_terrain_surface_source"] = str(manifest_path)
        material["endfield_terrain_albedo_semantics"] = (
            "Authoritative HGTerrain A pages; DXT5 decoded, 2-pixel page border removed, linear UNorm"
        )
        material["endfield_terrain_normal_semantics"] = (
            "Authoritative HGTerrain N pages; DirectX tangent-space green inverted for Blender"
        )
        material["endfield_terrain_normal_green_inverted"] = True
        material_status = (
            "authoritative baked albedo and tangent-space normal atlases; "
            "terrain splat micro-detail retained for later refinement"
        )
    else:
        material = bpy.data.materials.new("Terrain_HeightOnly_NotTextured")
        material.diffuse_color = (0.18, 0.25, 0.11, 1.0)
    terrain_origin = surface_manifest["tile_world_origin"] if surface_manifest else [TERRAIN_ORIGIN_X, TERRAIN_ORIGIN_Z]
    tile_size = float(surface_manifest["tile_world_size"]) if surface_manifest else 64.0
    base_y = float(surface_manifest["terrain_position"][1]) if surface_manifest else TERRAIN_BASE_Y
    height_scale = float(surface_manifest["height_scale"]) if surface_manifest else TERRAIN_HEIGHT_SCALE
    height_denominator = float(surface_manifest.get("height_denominator", 32768.0)) if surface_manifest else TERRAIN_HEIGHT_DENOMINATOR
    atlas_bounds = surface_manifest["atlas_world_bounds"] if surface_manifest else None
    connection = sqlite3.connect(database.resolve().as_uri() + "?mode=ro", uri=True)
    count = 0
    try:
        for tile_x, tile_z, width, height, payload in connection.execute(
                "SELECT x,z,width,height,heights FROM tiles ORDER BY z,x"):
            origin_x = float(terrain_origin[0]) + tile_x * tile_size
            origin_z = float(terrain_origin[1]) + tile_z * tile_size
            if bounds is not None:
                xmin, xmax, zmin, zmax = bounds
                if origin_x >= xmax or origin_x + tile_size <= xmin or origin_z >= zmax or origin_z + tile_size <= zmin:
                    continue
            heights = struct.unpack(f"<{width * height}H", payload)
            vertices = [(origin_x + x * tile_size / (width - 1), -(origin_z + z * tile_size / (height - 1)),
                         base_y + heights[z * width + x] * height_scale / height_denominator)
                        for z in range(height) for x in range(width)]
            faces = []
            for z in range(height - 1):
                row = z * width
                for x in range(width - 1):
                    a = row + x
                    corners = (a, a + width, a + width + 1, a + 1)
                    if all(heights[corner] < 32768 for corner in corners):
                        faces.append(corners)
            used = sorted({index for face in faces for index in face})
            remap = {old: new for new, old in enumerate(used)}
            compact_vertices = [vertices[index] for index in used]
            compact_faces = [tuple(remap[index] for index in face) for face in faces]
            mesh = bpy.data.meshes.new(f"terrain_{tile_x}_{tile_z}")
            mesh.from_pydata(compact_vertices, [], compact_faces)
            if surface_manifest is not None:
                uv_layer = mesh.uv_layers.new(name="TerrainWorldUV")
                for polygon in mesh.polygons:
                    for loop_index in polygon.loop_indices:
                        vertex = mesh.vertices[mesh.loops[loop_index].vertex_index].co
                        unity_z = -vertex.y
                        uv_layer.data[loop_index].uv = (
                            (vertex.x - atlas_bounds[0]) / (atlas_bounds[1] - atlas_bounds[0]),
                            1.0 - (unity_z - atlas_bounds[2]) / (atlas_bounds[3] - atlas_bounds[2]),
                        )
            mesh.materials.append(material)
            obj = bpy.data.objects.new(f"Terrain_{tile_x:02d}_{tile_z:02d}", mesh)
            collection.objects.link(obj)
            obj["unity_tile"] = f"{tile_x},{tile_z}"
            count += 1
            if count % 50 == 0:
                print(f"terrain={count}", flush=True)
    finally:
        connection.close()
    return count, material_status


def load_terrain_samples(database: Path) -> dict[tuple[int, int], tuple[int, int, tuple[int, ...]]]:
    connection = sqlite3.connect(f"file:{database}?mode=ro", uri=True)
    try:
        return {
            (int(tile_x), int(tile_z)): (
                int(width), int(height), struct.unpack(f"<{width * height}H", payload)
            )
            for tile_x, tile_z, width, height, payload in connection.execute(
                "SELECT x,z,width,height,heights FROM tiles"
            )
        }
    finally:
        connection.close()


def terrain_height_at(samples, unity_x: float, unity_z: float) -> float | None:
    tile_x = math.floor((unity_x - TERRAIN_ORIGIN_X) / 64.0)
    tile_z = math.floor((unity_z - TERRAIN_ORIGIN_Z) / 64.0)
    tile = samples.get((tile_x, tile_z))
    if tile is None:
        return None
    width, height, values = tile
    local_x = min(width - 1, max(0, round(unity_x - (TERRAIN_ORIGIN_X + tile_x * 64.0))))
    local_z = min(height - 1, max(0, round(unity_z - (TERRAIN_ORIGIN_Z + tile_z * 64.0))))
    value = values[local_z * width + local_x]
    return None if value >= 32768 else TERRAIN_BASE_Y + value * TERRAIN_HEIGHT_SCALE / TERRAIN_HEIGHT_DENOMINATOR


def newplant_state_outlier(row: tuple, template: bpy.types.Object, terrain_samples) -> tuple[bool, dict]:
    """Detect only the visually proven 03f/03g alternate-state fragments.

    The filter is intentionally narrow. It never snaps or changes a transform;
    suspect instances remain in a hidden collection so the source data is
    recoverable and reviewable.
    """
    base = (row[0] or "").casefold()
    if not re.fullmatch(r"p_building_newplant\+1_001_03[a-g]", base):
        return False, {}
    transform = unity_matrix(row[2])
    points = [transform @ Vector(corner) for corner in template.bound_box]
    minimum = [min(point[axis] for point in points) for axis in range(3)]
    maximum = [max(point[axis] for point in points) for axis in range(3)]
    unity_x = (minimum[0] + maximum[0]) * 0.5
    unity_z = -(minimum[1] + maximum[1]) * 0.5
    terrain_height = terrain_height_at(terrain_samples, unity_x, unity_z)
    if terrain_height is None:
        return False, {}
    bottom_delta = minimum[2] - terrain_height
    # Canonical pieces in this family sit within roughly 26 m of the sampled
    # terrain because several are upper-floor attachments. The screenshot-
    # verified state fragments begin above 32 m (and extend past 135 m), so a
    # 30 m threshold separates them without snapping legitimate structure.
    suspect = bottom_delta > 30.0 or maximum[2] < terrain_height - 20.0
    return suspect, {
        "terrain_height": terrain_height,
        "bottom_delta": bottom_delta,
        "bounds_min": minimum,
        "bounds_max": maximum,
    }


def main() -> None:
    args = arguments()
    work_root = args.work_root.resolve()
    output = args.output.resolve()
    assets_db = args.assets_db.resolve() if args.assets_db else work_root / "assets.sqlite"
    if output.exists():
        raise RuntimeError(f"Refusing to overwrite existing output: {output}")
    bound_values = (args.xmin, args.xmax, args.zmin, args.zmax)
    if any(value is not None for value in bound_values):
        if not all(value is not None for value in bound_values):
            raise RuntimeError("Region export requires xmin, xmax, zmin and zmax together")
        bounds = tuple(float(value) for value in bound_values)
        if bounds[1] <= bounds[0] or bounds[3] <= bounds[2]:
            raise RuntimeError("Region maximums must be greater than minimums")
    else:
        bounds = None
    if not all((work_root / name).exists() for name in
               ("instances.sqlite", "terrain.sqlite", "scene_probe")) or not assets_db.exists():
        raise RuntimeError(f"Incomplete full-map workspace: {work_root}")
    probe_roots = [work_root / "scene_probe", *(path.resolve() for path in args.extra_probe)]
    manifests = []
    for probe_root in probe_roots:
        manifest_path = probe_root / "scene_manifest.json"
        if not manifest_path.exists():
            raise RuntimeError(f"Missing probe manifest: {manifest_path}")
        manifests.append((probe_root, json.loads(manifest_path.read_text(encoding="utf-8"))))

    bpy.ops.object.select_all(action="SELECT")
    bpy.ops.object.delete(use_global=False)
    models = bpy.data.collections.new("Map02_Models_Instanced")
    templates_collection = bpy.data.collections.new("_Map02_Source_Templates")
    terrain_collection = bpy.data.collections.new("Map02_Terrain")
    unresolved_collection = bpy.data.collections.new("_Map02_Unresolved_Points")
    quarantine_collection = bpy.data.collections.new("_Map02_Quarantined_State_Instances")
    game_lights_collection = bpy.data.collections.new("Endfield_Game_Lights")
    preview_lights_collection = bpy.data.collections.new("Endfield_Preview_Lights")
    for collection in (models, templates_collection, terrain_collection, unresolved_collection,
                       quarantine_collection,
                       game_lights_collection, preview_lights_collection):
        bpy.context.scene.collection.children.link(collection)
    # Keep the collection dependency-graph enabled: Object Info nodes need it.
    # Individual source objects can be hidden without suppressing GN evaluation.
    templates_collection.hide_viewport = False
    templates_collection.hide_render = True
    unresolved_collection.hide_viewport = unresolved_collection.hide_render = True
    quarantine_collection.hide_viewport = quarantine_collection.hide_render = True
    quarantine_collection["note"] = (
        "Original transforms preserved; hidden because a narrow terrain audit marked these "
        "new-plant alternate-state fragments as strongly above or below the active terrain."
    )

    texture_files = {}
    material_records = {}
    material_candidates_by_name = defaultdict(list)
    mesh_by_ref = {}
    mesh_candidates_by_ref = defaultdict(list)
    mesh_by_source_name = {}
    mesh_candidates_by_asset_name = defaultdict(list)
    mesh_containers_by_ref = defaultdict(set)
    material_containers_by_ref = defaultdict(set)
    nodes_by_source = defaultdict(list)
    renderer_material_sequences = defaultdict(set)
    renderer_material_sequences_by_asset_name = defaultdict(set)
    renderer_material_sequences_by_source_lod_family = defaultdict(set)
    renderer_material_sequences_by_lod_family = defaultdict(set)
    for probe_root, manifest in manifests:
        for row in manifest["TextureExports"]:
            if row.get("File"):
                texture_files.setdefault(ref_key(row["Asset"]), (probe_root, row["File"]))
        for row in manifest["Materials"]:
            material_records.setdefault(ref_key(row), row)
            material_candidates_by_name[row["Name"].casefold()].append(row)
        for original in manifest["MeshExports"]:
            if not original.get("File"):
                continue
            row = dict(original)
            row["_ProbeRoot"] = str(probe_root)
            mesh_by_ref.setdefault(ref_key(row["Asset"]), row)
            mesh_candidates_by_ref[ref_key(row["Asset"])].append(row)
            mesh_by_source_name.setdefault(
                (row["Asset"]["Source"].casefold(), row["Asset"]["Name"].casefold()), row
            )
            mesh_candidates_by_asset_name[row["Asset"]["Name"].casefold()].append(row)
        for container in manifest.get("Containers", []):
            asset = container.get("Asset") or {}
            if (
                str(asset.get("Type", "")).casefold() == "mesh"
                and asset.get("Source")
                and "PathId" in asset
                and container.get("Container")
            ):
                mesh_containers_by_ref[ref_key(asset)].add(
                    str(container["Container"]).casefold()
                )
            elif (
                str(asset.get("Type", "")).casefold() == "material"
                and asset.get("Source")
                and "PathId" in asset
                and container.get("Container")
            ):
                material_containers_by_ref[ref_key(asset)].add(
                    str(container["Container"]).casefold()
                )
        for node in manifest["Nodes"]:
            nodes_by_source[node["Source"].casefold()].append(node)
            if node.get("Mesh") and node.get("Materials"):
                mesh_ref = node["Mesh"]
                sequence = tuple(ref_key(material) for material in node["Materials"])
                renderer_material_sequences[ref_key(mesh_ref)].add(sequence)
                renderer_material_sequences_by_asset_name[
                    mesh_ref.get("Name", "").casefold().replace("+1_", "_1_")
                ].add(sequence)
                lod_family = lod_family_name(mesh_ref.get("Name", ""))
                if lod_family:
                    renderer_material_sequences_by_source_lod_family[
                        (mesh_ref["Source"].casefold(), lod_family)
                    ].add(sequence)
                    renderer_material_sequences_by_lod_family[lod_family].add(sequence)

    terrain_spans = None
    if args.terrain_footprint_only:
        terrain_db = sqlite3.connect(work_root / "terrain.sqlite")
        try:
            terrain_spans = {
                int(tile_z): (int(min_x), int(max_x))
                for tile_z, min_x, max_x in terrain_db.execute(
                    "SELECT z,MIN(x),MAX(x) FROM tiles GROUP BY z"
                )
            }
        finally:
            terrain_db.close()

    instances = sqlite3.connect(work_root / "instances.sqlite")
    global_instance_counts = {
        entity_base: int(count)
        for entity_base, count in instances.execute(
            "SELECT entity_base,COUNT(*) FROM instances GROUP BY entity_base"
        )
    }
    if bounds is None:
        selected_rows = instances.execute(
            "SELECT entity_base,entity_id,matrix_col_major,name,x,z,source_path "
            "FROM instances ORDER BY entity_base,entity_id").fetchall()
    else:
        selected_rows = instances.execute(
            "SELECT entity_base,entity_id,matrix_col_major,name,x,z,source_path FROM instances "
            "WHERE x>=? AND x<? AND z>=? AND z<? ORDER BY entity_base,entity_id", bounds).fetchall()
    instances.close()
    terrain_rejected = 0
    if terrain_spans is not None:
        before = len(selected_rows)
        footprint_rows = []
        for row in selected_rows:
            tile_x = math.floor((row[4] - TERRAIN_ORIGIN_X) / 64.0)
            tile_z = math.floor((row[5] - TERRAIN_ORIGIN_Z) / 64.0)
            span = terrain_spans.get(tile_z)
            if span is not None and span[0] <= tile_x <= span[1]:
                footprint_rows.append(row)
        selected_rows = footprint_rows
        terrain_rejected = before - len(selected_rows)
        print(f"terrain_footprint_rejected={terrain_rejected}", flush=True)
    if args.include_regex:
        include = re.compile(args.include_regex, re.I)
        selected_rows = [row for row in selected_rows if include.search(row[0] or "") or include.search(row[3] or "")]
    if args.exclude_regex:
        exclude = re.compile(args.exclude_regex, re.I)
        selected_rows = [row for row in selected_rows
                         if not (exclude.search(row[0] or "") or exclude.search(row[3] or ""))]
    selected_bases = {row[0] for row in selected_rows}

    assets = sqlite3.connect(f"file:{assets_db}?mode=ro", uri=True)
    resolutions = assets.execute(
        "SELECT entity_base,method,root_cab,model_asset_name FROM resolutions ORDER BY entity_base").fetchall()
    assets.close()
    template_plans = {}
    exact_model_material_bindings = 0
    exact_hlod_material_bindings = 0
    exact_mesh_material_bindings = 0
    exact_map01_alias_material_bindings = 0
    reverse_renderer_material_bindings = 0
    reverse_renderer_asset_name_bindings = 0
    reverse_renderer_source_lod_family_bindings = 0
    reverse_renderer_lod_family_bindings = 0
    ambiguous_model_material_bindings = 0
    for base, method, root_cab, model_name in resolutions:
        if base not in selected_bases:
            continue
        if method == "prefab":
            candidates = [node for node in nodes_by_source.get(root_cab.casefold(), [])
                          if node.get("Mesh") and node["Name"].casefold().endswith("_lod0")]
            if candidates:
                node = candidates[0]
                export = mesh_by_ref.get(ref_key(node["Mesh"]))
                if export:
                    template_plans[base] = (export, node.get("Materials") or [], node["Name"])
        elif method == "model":
            export = mesh_by_source_name.get((root_cab.casefold(), model_name.casefold()))
            if export:
                # Runtime decorations sometimes serialize only a direct Mesh
                # reference, omitting the prefab Renderer. Accept a material
                # only when the entity variant has an exact P_ -> M_ name
                # counterpart; never use fuzzy or neighbouring suffixes.
                expected = ("M_" + base[2:]) if base.casefold().startswith("p_") else ("M_" + base)
                entity_candidates = material_candidates_by_name.get(expected.casefold(), [])
                unique_entity_candidates = {
                    (candidate["Source"].casefold(), int(candidate["PathId"])): candidate
                    for candidate in entity_candidates
                }
                material = (next(iter(unique_entity_candidates.values()))
                            if len(unique_entity_candidates) == 1 else None)
                refs = []
                if material is not None:
                    refs = [{"Source": material["Source"], "PathId": material["PathId"]}]
                    exact_model_material_bindings += 1
                elif unique_entity_candidates:
                    ambiguous_model_material_bindings += 1
                if not refs:
                    mesh_stem = re.sub(r"_lod\d+$", "", model_name, flags=re.I)
                    mesh_expected = (("M_" + mesh_stem[2:]) if mesh_stem.casefold().startswith("s_")
                                     else ("M_" + mesh_stem))
                    mesh_candidates = material_candidates_by_name.get(mesh_expected.casefold(), [])
                    unique_mesh_candidates = {
                        (candidate["Source"].casefold(), int(candidate["PathId"])): candidate
                        for candidate in mesh_candidates
                    }
                    if len(unique_mesh_candidates) == 1:
                        material = next(iter(unique_mesh_candidates.values()))
                        refs = [{"Source": material["Source"], "PathId": material["PathId"]}]
                        exact_mesh_material_bindings += 1
                    elif unique_mesh_candidates:
                        ambiguous_model_material_bindings += 1
                if not refs:
                    alias_expected = exact_map01_alias_material_name(model_name)
                    alias_candidates = (
                        material_candidates_by_name.get(alias_expected.casefold(), [])
                        if alias_expected else []
                    )
                    unique_alias_candidates = {
                        (candidate["Source"].casefold(), int(candidate["PathId"])): candidate
                        for candidate in alias_candidates
                    }
                    if len(unique_alias_candidates) == 1:
                        material = next(iter(unique_alias_candidates.values()))
                        refs = [{"Source": material["Source"], "PathId": material["PathId"]}]
                        exact_map01_alias_material_bindings += 1
                    elif unique_alias_candidates:
                        ambiguous_model_material_bindings += 1
                if not refs:
                    sequences = renderer_material_sequences.get(ref_key(export["Asset"]), set())
                    if len(sequences) == 1:
                        refs = [
                            {"Source": source, "PathId": path_id}
                            for source, path_id in next(iter(sequences))
                        ]
                        reverse_renderer_material_bindings += 1
                    elif sequences:
                        ambiguous_model_material_bindings += 1
                if not refs:
                    # A duplicated standalone model CAB can still correspond
                    # to an exact prefab LOD mesh name.  Only normalize the
                    # observed '+1_' versus '_1_' punctuation spelling and
                    # require every matching exact LOD name to agree.
                    asset_name = model_name.casefold().replace("+1_", "_1_")
                    sequences = renderer_material_sequences_by_asset_name.get(asset_name, set())
                    if len(sequences) == 1:
                        refs = [
                            {"Source": source, "PathId": path_id}
                            for source, path_id in next(iter(sequences))
                        ]
                        reverse_renderer_asset_name_bindings += 1
                    elif sequences:
                        ambiguous_model_material_bindings += 1
                if not refs:
                    # Prefabs commonly omit LOD0 while preserving identical
                    # Renderer material slots on LOD1..LOD4.  Inherit only
                    # when every observed LOD in the exact source CAB agrees.
                    lod_family = lod_family_name(model_name)
                    sequences = renderer_material_sequences_by_source_lod_family.get(
                        (export["Asset"]["Source"].casefold(), lod_family), set()
                    )
                    if len(sequences) == 1:
                        refs = [
                            {"Source": source, "PathId": path_id}
                            for source, path_id in next(iter(sequences))
                        ]
                        reverse_renderer_source_lod_family_bindings += 1
                    elif sequences:
                        ambiguous_model_material_bindings += 1
                if not refs:
                    # Some model bundles duplicate an identically named mesh
                    # under another CAB.  Accept the name-only LOD family only
                    # if all probes, across all CABs, still agree on exactly one
                    # ordered material sequence.  This is deliberately not a
                    # fuzzy family or suffix-neighbour match.
                    lod_family = lod_family_name(model_name)
                    sequences = renderer_material_sequences_by_lod_family.get(lod_family, set())
                    if len(sequences) == 1:
                        refs = [
                            {"Source": source, "PathId": path_id}
                            for source, path_id in next(iter(sequences))
                        ]
                        reverse_renderer_lod_family_bindings += 1
                    elif sequences:
                        ambiguous_model_material_bindings += 1
                if not refs:
                    hlod_material = hlod_material_name(model_name)
                    if hlod_material is not None:
                        hlod_candidates = material_candidates_by_name.get(hlod_material.casefold(), [])
                        unique_hlod_candidates = {
                            (candidate["Source"].casefold(), int(candidate["PathId"])): candidate
                            for candidate in hlod_candidates
                        }
                        if len(unique_hlod_candidates) == 1:
                            material = next(iter(unique_hlod_candidates.values()))
                            refs = [{"Source": material["Source"], "PathId": material["PathId"]}]
                            exact_hlod_material_bindings += 1
                        elif unique_hlod_candidates:
                            ambiguous_model_material_bindings += 1
                template_plans[base] = (export, refs, model_name)
    authoritative_ecs_material_bases = set()
    ecs_material_binding_path = None
    reviewed_family_material_bases = set()
    reviewed_family_material_binding_path = None
    authoritative_ecs_geometry_bases = set()
    ecs_geometry_binding_path = None
    ecs_geometry_expected_slots = {}
    ecs_geometry_expected_material_refs = {}
    ecs_geometry_expected_material_ids = {}
    ecs_geometry_binding_methods = {}
    ecs_instance_binding_path = None
    ecs_instance_bound_bases = set()
    ecs_instance_plan_keys = set()
    ecs_instance_plan_original_bases = {}
    ecs_instance_plan_variants = {}
    ecs_instance_plan_metadata = {}
    ecs_instance_plan_key_by_entity_id = {}
    ecs_instance_database_count = 0
    experimental_geometry_aliases = set()
    experimental_tystair_geometry_aliases = set()
    if args.experimental_newplant_geometry_aliases:
        # Runtime entity names omit the authored "tundra" token for four
        # adjacent new-plant pieces. Keep this explicit and review-labelled;
        # no fuzzy filename matching is allowed here.
        aliases = {
            "P_building_newplant+1_001_01b": "P_building_tundra_newplant+1_001_01b",
            "P_building_newplant+1_001_01c": "P_building_tundra_newplant+1_001_01c",
            "P_building_newplant+1_001_01d": "P_building_tundra_newplant+1_001_01d",
            "P_building_newplant+1_001_01g": "P_building_tundra_newplant+1_001_01g",
        }
        for alias, target in aliases.items():
            if alias in selected_bases and alias not in template_plans and target in template_plans:
                template_plans[alias] = template_plans[target]
                experimental_geometry_aliases.add(alias)
        print(f"experimental_newplant_geometry_aliases={len(experimental_geometry_aliases)}", flush=True)
    if args.experimental_tystair_geometry_aliases:
        # These map01 runtime entities reuse exact-numbered base01 authored
        # meshes. The relationship is deliberately enumerated and only
        # accepted when the LOD0 asset name is unique across all probes.
        aliases = {
            "P_mod_map01_tystair+1_001_s01": "S_mod_base01_tystair+1_001_01_lod0",
            "P_mod_map01_tystair+1_001_s02": "S_mod_base01_tystair+1_001_02_lod0",
            "P_mod_map01_tystair+1_001_s05": "S_mod_base01_tystair+1_001_05_lod0",
            "P_module_tystair+1_001_s04": "S_mod_base01_tystair+1_001_04_lod0",
            "P_module_tystair+1_001_s05": "S_mod_base01_tystair+1_001_05_lod0",
            "P_module_tystair+1_001_s06": "S_mod_base01_tystair+1_001_06_lod0",
            "P_module_tystair+1_001_s07": "S_mod_base01_tystair+1_001_07_lod0",
            "P_module_tystair+1_001_s08": "S_mod_base01_tystair+1_001_08_lod0",
        }
        for alias, asset_name in aliases.items():
            candidates = mesh_candidates_by_asset_name.get(asset_name.casefold(), [])
            unique_candidates = {
                ref_key(candidate["Asset"]): candidate for candidate in candidates
            }
            if alias in selected_bases and alias not in template_plans and len(unique_candidates) == 1:
                template_plans[alias] = (next(iter(unique_candidates.values())), [], asset_name)
                experimental_tystair_geometry_aliases.add(alias)
        print(
            f"experimental_tystair_geometry_aliases={len(experimental_tystair_geometry_aliases)}",
            flush=True,
        )
    if args.ecs_geometry_bindings:
        ecs_geometry_binding_path = args.ecs_geometry_bindings.resolve()
        payload = json.loads(
            ecs_geometry_binding_path.read_text(encoding="utf-8")
        )
        geometry_format = payload.get("format")
        if geometry_format not in {
            "EndfieldEcsGeometryBindings/1",
            "EndfieldEcsGeometryBindings/2",
        }:
            raise RuntimeError(
                f"Unsupported ECS geometry binding format: {geometry_format!r}"
            )
        geometry_version = int(str(geometry_format).rsplit("/", 1)[1])
        geometry_rows = payload.get("rows", [])
        geometry_row_bases = [row.get("entity_base") for row in geometry_rows]
        if (
            any(not base for base in geometry_row_bases)
            or len(set(geometry_row_bases)) != len(geometry_row_bases)
        ):
            raise RuntimeError("ECS geometry bindings contain missing or duplicate bases")
        for row in geometry_rows:
            base = row["entity_base"]
            if base not in selected_bases:
                continue
            confidence = str(row.get("confidence") or "")
            if confidence not in {
                "authoritative_ecs_renderer_mesh_ids",
                (
                    "authoritative_ecs_renderer_slot_ids_and_exact_"
                    "mesh_container"
                ),
            }:
                raise RuntimeError(
                    f"Unsupported ECS geometry confidence for {base}: "
                    f"{confidence!r}"
                )
            if base not in template_plans:
                raise RuntimeError(f"ECS geometry binding has no resolved geometry: {base}")
            if (
                base in experimental_geometry_aliases
                or base in experimental_tystair_geometry_aliases
            ):
                raise RuntimeError(
                    f"ECS geometry binding overlaps an experimental alias: {base}"
                )
            replaces = row.get("replaces") or {}
            mesh = row.get("mesh") or {}
            required = ("Source", "PathId", "Name", "Container")
            if any(key not in replaces for key in required):
                raise RuntimeError(
                    f"ECS geometry replacement descriptor is incomplete: {base}"
                )
            if any(key not in mesh for key in required):
                raise RuntimeError(
                    f"ECS geometry target descriptor is incomplete: {base}"
                )
            old_export, _old_refs, old_note = template_plans[base]
            if (
                ref_key(old_export["Asset"]) != ref_key(replaces)
                or old_export["Asset"]["Name"].casefold()
                != str(replaces["Name"]).casefold()
                or str(old_note).casefold() != str(replaces["Name"]).casefold()
            ):
                raise RuntimeError(
                    f"ECS geometry binding no longer matches current source geometry: {base}"
                )
            old_containers = mesh_containers_by_ref.get(ref_key(replaces), set())
            if old_containers != {str(replaces["Container"]).casefold()}:
                raise RuntimeError(
                    f"ECS geometry replacement container mismatch for {base}: "
                    f"{sorted(old_containers)}"
                )
            target_key = ref_key(mesh)
            target_candidates = mesh_candidates_by_ref.get(target_key, [])
            if not target_candidates:
                raise RuntimeError(
                    f"ECS geometry target is absent from probes: {base} {target_key}"
                )
            if any(
                candidate["Asset"]["Name"].casefold()
                != str(mesh["Name"]).casefold()
                for candidate in target_candidates
            ):
                raise RuntimeError(
                    f"ECS geometry target reference has conflicting names: {base}"
                )
            target_containers = mesh_containers_by_ref.get(target_key, set())
            if target_containers != {str(mesh["Container"]).casefold()}:
                raise RuntimeError(
                    f"ECS geometry target container mismatch for {base}: "
                    f"{sorted(target_containers)}"
                )
            target_name = str(mesh["Name"])
            if re.search(r"(?:_col\d*|collision|shadowproxy)", target_name, re.I):
                raise RuntimeError(
                    f"ECS geometry target is a collider or shadow proxy: {base}"
                )
            lod_match = re.search(r"_lod(\d+)$", target_name, re.I)
            if not lod_match:
                raise RuntimeError(
                    f"ECS geometry target has no explicit LOD suffix: {base}"
                )
            target_lod = int(lod_match.group(1))
            expected_slots = int(row.get("expected_submesh_slots", 0))
            if expected_slots <= 0:
                raise RuntimeError(
                    f"ECS geometry binding has no valid slot count: {base}"
                )
            if geometry_version == 1 and expected_slots != 1:
                raise RuntimeError(
                    f"ECS geometry binding v1 requires exactly one slot: {base}"
                )
            ecs = row.get("ecs") or {}
            component_type = int(ecs.get("component_type", -1))
            stride = int(ecs.get("stride", -1))
            expected_stride = {
                68: 48,
                69: 88,
                70: 168,
                71: 328,
                72: 648,
            }.get(component_type)
            if expected_stride is None or stride != expected_stride:
                raise RuntimeError(
                    f"ECS geometry component layout is invalid for {base}: "
                    f"type={component_type} stride={stride}"
                )
            if geometry_version == 2:
                populated_pairs = int(
                    ecs.get("populated_pair_count", 0)
                )
                pair_capacity = {
                    68: 1,
                    69: 2,
                    70: 4,
                    71: 8,
                    72: 16,
                }[component_type]
                if not expected_slots <= populated_pairs <= pair_capacity:
                    raise RuntimeError(
                        f"ECS geometry populated pair count is invalid: "
                        f"{base} count={populated_pairs}"
                    )
            uses_ordered_materials = geometry_version == 2
            if uses_ordered_materials and "materials" not in row:
                raise RuntimeError(
                    f"ECS geometry v2 row lacks ordered materials: {base}"
                )
            if not uses_ordered_materials and "materials" in row:
                raise RuntimeError(
                    f"Ordered ECS geometry materials require format v2: {base}"
                )
            if uses_ordered_materials:
                binding_materials = row.get("materials") or []
                ecs_mesh_lod = int(ecs.get("mesh_lod", -1))
                ecs_mesh_id = str(ecs.get("mesh_id", ""))
                ecs_mesh_path = str(ecs.get("mesh_path", ""))
                ecs_material_ids = [
                    str(value) for value in (ecs.get("material_ids") or [])
                ]
                ecs_material_paths = [
                    str(value) for value in (ecs.get("material_paths") or [])
                ]
                if (
                    len(binding_materials) != expected_slots
                    or len(ecs_material_ids) != expected_slots
                    or len(ecs_material_paths) != expected_slots
                ):
                    raise RuntimeError(
                        f"ECS geometry v2 ordered slot metadata is invalid: {base}"
                    )
            else:
                binding_materials = []
                ecs_mesh_lod = 0
                ecs_mesh_id = str(ecs.get("lod0_mesh_id", ""))
                ecs_mesh_path = str(ecs.get("lod0_mesh_path", ""))
                ecs_material_ids = [str(ecs.get("material_id", ""))]
                ecs_material_paths = []
            expected_ecs_path = f"{mesh['Container']}##{mesh['Name']}"
            if (
                ecs_mesh_lod != target_lod
                or ecs_mesh_path.casefold() != expected_ecs_path.casefold()
            ):
                raise RuntimeError(
                    f"ECS geometry path or LOD disagrees with target Mesh: {base}"
                )
            if not re.fullmatch(r"[0-9a-f]{16}", ecs_mesh_id, re.I):
                raise RuntimeError(f"ECS geometry Mesh ID is invalid: {base}")
            if any(
                not re.fullmatch(r"[0-9a-f]{16}", value, re.I)
                for value in ecs_material_ids
            ):
                raise RuntimeError(
                    f"ECS geometry material ID list is invalid: {base}"
                )
            if target_lod != 0:
                exception = STRICT_ECS_GEOMETRY_LOD_EXCEPTIONS.get(base)
                evidence = row.get("evidence") or {}
                if (
                    geometry_version != 2
                    or exception is None
                    or target_lod != int(exception["lod"])
                    or target_key != exception["mesh_ref"]
                    or target_name.casefold() != exception["mesh_name"]
                    or str(mesh["Container"]).casefold()
                    != exception["mesh_container"]
                    or ecs_mesh_id.casefold() != exception["mesh_id"]
                    or evidence.get("mode") != exception["evidence_mode"]
                    or not re.fullmatch(
                        r"[0-9a-f]{64}",
                        str(evidence.get("slot_audit_sha256", "")),
                        re.I,
                    )
                    or not re.fullmatch(
                        r"[0-9a-f]{64}",
                        str(evidence.get("record_count_audit_sha256", "")),
                        re.I,
                    )
                    or not re.fullmatch(
                        r"[0-9a-f]{64}",
                        str(evidence.get("mesh_probe_manifest_sha256", "")),
                        re.I,
                    )
                ):
                    raise RuntimeError(
                        f"ECS geometry non-LOD0 target is not the reviewed "
                        f"exact exception: {base}"
                    )

            descriptor_required = ("Source", "PathId", "Name")
            if confidence == "authoritative_ecs_renderer_mesh_ids":
                renderer = row.get("renderer") or {}
                prefab = renderer.get("prefab_root") or {}
                if any(key not in renderer for key in descriptor_required):
                    raise RuntimeError(
                        f"ECS geometry Renderer descriptor is incomplete: {base}"
                    )
                if not binding_materials:
                    renderer_material = renderer.get("material") or {}
                    binding_materials = [renderer_material]
                if any(
                    any(key not in material for key in descriptor_required)
                    for material in binding_materials
                ):
                    raise RuntimeError(
                        "ECS geometry Renderer material descriptors are "
                        f"incomplete: {base}"
                    )
                if any(key not in prefab for key in descriptor_required):
                    raise RuntimeError(
                        f"ECS geometry Prefab descriptor is incomplete: {base}"
                    )
                renderer_matches = [
                    node
                    for node in nodes_by_source.get(
                        str(renderer["Source"]).casefold(), []
                    )
                    if int(node.get("PathId", 0))
                    == int(renderer["PathId"])
                    and str(node.get("Name", "")).casefold()
                    == str(renderer["Name"]).casefold()
                ]
                renderer_signatures = {
                    json.dumps(node, sort_keys=True, ensure_ascii=False)
                    for node in renderer_matches
                }
                if len(renderer_signatures) != 1:
                    raise RuntimeError(
                        "ECS geometry Renderer is missing or conflicting: "
                        f"{base}"
                    )
                renderer_node = renderer_matches[0]
                renderer_mesh = renderer_node.get("Mesh") or {}
                renderer_materials = renderer_node.get("Materials") or []
                if (
                    ref_key(renderer_mesh) != target_key
                    or str(renderer_mesh.get("Name", "")).casefold()
                    != target_name.casefold()
                    or renderer_node.get("RendererType") != "MeshRenderer"
                    or list(renderer_node.get("LocalPosition") or [])
                    != [0, 0, 0]
                    or list(renderer_node.get("LocalRotation") or [])
                    != [0, 0, 0, 1]
                    or list(renderer_node.get("LocalScale") or [])
                    != [1, 1, 1]
                    or len(renderer_materials) != expected_slots
                    or len(binding_materials) != expected_slots
                    or any(
                        ref_key(actual) != ref_key(expected)
                        or str(actual.get("Name", "")).casefold()
                        != str(expected["Name"]).casefold()
                        for actual, expected in zip(
                            renderer_materials, binding_materials
                        )
                    )
                ):
                    raise RuntimeError(
                        "ECS geometry Renderer evidence is incompatible: "
                        f"{base}"
                    )
                prefab_matches = [
                    node
                    for node in nodes_by_source.get(
                        str(prefab["Source"]).casefold(), []
                    )
                    if int(node.get("PathId", 0)) == int(prefab["PathId"])
                    and str(node.get("Name", "")).casefold()
                    == str(prefab["Name"]).casefold()
                ]
                prefab_signatures = {
                    json.dumps(node, sort_keys=True, ensure_ascii=False)
                    for node in prefab_matches
                }
                if len(prefab_signatures) != 1:
                    raise RuntimeError(
                        "ECS geometry Prefab root is missing or conflicting: "
                        f"{base}"
                    )
                prefab_node = prefab_matches[0]
                renderer_parent = renderer_node.get("Parent") or {}
                if (
                    not prefab_node.get("IsRoot")
                    or list(prefab_node.get("LocalPosition") or [])
                    != [0, 0, 0]
                    or list(prefab_node.get("LocalRotation") or [])
                    != [0, 0, 0, 1]
                    or list(prefab_node.get("LocalScale") or [])
                    != [1, 1, 1]
                    or str(renderer_parent.get("Source", "")).casefold()
                    != str(prefab_node.get("Source", "")).casefold()
                    or int(renderer_parent.get("PathId", 0))
                    != int(prefab_node.get("TransformPathId", 0))
                ):
                    raise RuntimeError(
                        "ECS geometry Prefab transform evidence is "
                        f"incompatible: {base}"
                    )
            else:
                if row.get("renderer"):
                    raise RuntimeError(
                        "ECS slot-only geometry evidence must not claim a "
                        f"Prefab Renderer: {base}"
                    )
                if not binding_materials:
                    binding_materials = [row.get("material") or {}]
                material_required = (
                    "Source",
                    "PathId",
                    "Name",
                    "Container",
                    "MaterialId",
                )
                if any(
                    any(key not in material for key in material_required)
                    for material in binding_materials
                ):
                    raise RuntimeError(
                        "ECS slot-only geometry material descriptors are "
                        f"incomplete: {base}"
                    )
                if len(binding_materials) != expected_slots or any(
                    str(material["MaterialId"]).casefold()
                    != ecs_material_ids[index].casefold()
                    or ref_key(material) not in material_records
                    or str(
                        material_records[ref_key(material)].get("Name", "")
                    ).casefold()
                    != str(material["Name"]).casefold()
                    for index, material in enumerate(binding_materials)
                ):
                    raise RuntimeError(
                        "ECS slot-only geometry material evidence is "
                        f"incompatible: {base}"
                    )
                evidence = row.get("evidence") or {}
                expected_evidence_mode = (
                    STRICT_ECS_GEOMETRY_LOD_EXCEPTIONS[base][
                        "evidence_mode"
                    ]
                    if target_lod != 0
                    else "fixed_ecs_renderer_slots_plus_exact_mesh_container"
                )
                if (
                    evidence.get("mode")
                    != expected_evidence_mode
                    or not re.fullmatch(
                        r"[0-9a-f]{64}",
                        str(evidence.get("slot_audit_sha256", "")),
                        re.I,
                    )
                    or not re.fullmatch(
                        r"[0-9a-f]{64}",
                        str(
                            evidence.get(
                                "mesh_probe_manifest_sha256", ""
                            )
                        ),
                        re.I,
                    )
                    or int(
                        evidence.get(
                            "reviewed_prefab_probe_compatible_nodes", -1
                        )
                    )
                    != 0
                ):
                    raise RuntimeError(
                        "ECS slot-only geometry evidence descriptor is "
                        f"invalid: {base}"
                    )
                populated_pairs = int(
                    ecs.get("populated_pair_count", 0)
                )
                pair_capacity = {
                    68: 1,
                    69: 2,
                    70: 4,
                    71: 8,
                    72: 16,
                }[component_type]
                if not expected_slots <= populated_pairs <= pair_capacity:
                    raise RuntimeError(
                        "ECS slot-only populated pair count is invalid: "
                        f"{base} count={populated_pairs}"
                    )

            if uses_ordered_materials:
                material_required = (
                    "MaterialId",
                    "Source",
                    "PathId",
                    "Name",
                    "Container",
                )
                if any(
                    any(key not in material for key in material_required)
                    for material in binding_materials
                ):
                    raise RuntimeError(
                        f"ECS geometry v2 material descriptors are "
                        f"incomplete: {base}"
                    )
                for slot, material in enumerate(binding_materials):
                    material_key = ref_key(material)
                    if (
                        str(material["MaterialId"]).casefold()
                        != ecs_material_ids[slot].casefold()
                        or str(material["Container"]).casefold()
                        != ecs_material_paths[slot].casefold()
                        or material_key not in material_records
                        or str(
                            material_records[material_key].get("Name", "")
                        ).casefold()
                        != str(material["Name"]).casefold()
                        or material_containers_by_ref.get(
                            material_key, set()
                        )
                        != {str(material["Container"]).casefold()}
                    ):
                        raise RuntimeError(
                            f"ECS geometry v2 material slot evidence "
                            f"disagrees at slot {slot}: {base}"
                        )
            expected_instances = int(ecs.get("instance_count", -1))
            if global_instance_counts.get(base, 0) != expected_instances:
                raise RuntimeError(
                    f"ECS geometry binding instance count is stale for {base}: "
                    f"database={global_instance_counts.get(base, 0)} "
                    f"sidecar={expected_instances}"
                )
            template_plans[base] = (
                mesh_by_ref[target_key],
                [],
                target_name,
            )
            authoritative_ecs_geometry_bases.add(base)
            ecs_geometry_expected_slots[base] = expected_slots
            ecs_geometry_expected_material_refs[base] = [
                ref_key(material) for material in binding_materials
            ]
            ecs_geometry_expected_material_ids[base] = [
                value.casefold() for value in ecs_material_ids
            ]
            ecs_geometry_binding_methods[base] = confidence
        print(
            f"authoritative_ecs_geometry_bindings={len(authoritative_ecs_geometry_bases)}",
            flush=True,
        )
    ecs_material_expected_slots = {}
    if args.ecs_material_bindings:
        # Geometry aliases are resolved first so authoritative material rows
        # can validate against the same final template plan used by import.
        ecs_material_binding_path = args.ecs_material_bindings.resolve()
        payload = json.loads(ecs_material_binding_path.read_text(encoding="utf-8"))
        if payload.get("format") != "EndfieldEcsMaterialBindings/1":
            raise RuntimeError(
                f"Unsupported ECS material binding format: {payload.get('format')!r}"
            )
        ecs_rows = payload.get("rows", [])
        ecs_row_bases = [row.get("entity_base") for row in ecs_rows]
        if (
            any(not base for base in ecs_row_bases)
            or len(set(ecs_row_bases)) != len(ecs_row_bases)
        ):
            raise RuntimeError("ECS material bindings contain missing or duplicate bases")
        for row in ecs_rows:
            base = row.get("entity_base")
            if base not in selected_bases:
                continue
            if base not in template_plans:
                raise RuntimeError(f"ECS material binding has no resolved geometry: {base}")
            materials = row.get("materials") or []
            if not materials:
                raise RuntimeError(f"ECS material binding has no ordered materials: {base}")
            expected_slots = row.get("expected_submesh_slots")
            if expected_slots is not None:
                expected_slots = int(expected_slots)
                if expected_slots <= 0 or expected_slots != len(materials):
                    raise RuntimeError(
                        f"ECS material binding slot metadata is invalid for {base}: "
                        f"expected={expected_slots} materials={len(materials)}"
                    )
                ecs_material_expected_slots[base] = expected_slots
            if base in authoritative_ecs_geometry_bases:
                material_refs = [
                    ref_key(material) for material in materials
                ]
                material_ids = [
                    str(material.get("MaterialId", "")).casefold()
                    for material in materials
                ]
                if (
                    len(materials) != ecs_geometry_expected_slots[base]
                    or material_refs
                    != ecs_geometry_expected_material_refs[base]
                    or material_ids
                    != ecs_geometry_expected_material_ids[base]
                ):
                    raise RuntimeError(
                        f"ECS geometry/material evidence disagrees for {base}"
                    )
            refs = [
                {"Source": material["Source"], "PathId": int(material["PathId"])}
                for material in materials
            ]
            missing = [ref for ref in refs if ref_key(ref) not in material_records]
            if missing:
                raise RuntimeError(f"ECS material records are absent from probes for {base}: {missing}")
            export, _old_refs, note = template_plans[base]
            template_plans[base] = (export, refs, note)
            authoritative_ecs_material_bases.add(base)
        print(
            f"authoritative_ecs_material_bindings={len(authoritative_ecs_material_bases)}",
            flush=True,
        )
    missing_geometry_materials = (
        authoritative_ecs_geometry_bases - authoritative_ecs_material_bases
    )
    if missing_geometry_materials:
        raise RuntimeError(
            "ECS geometry overrides require authoritative ECS materials: "
            f"{sorted(missing_geometry_materials)}"
        )
    for base in authoritative_ecs_geometry_bases:
        if ecs_material_expected_slots.get(base) != ecs_geometry_expected_slots[base]:
            raise RuntimeError(
                f"ECS geometry/material slot metadata disagrees for {base}"
            )
    if args.reviewed_family_material_bindings:
        reviewed_family_material_binding_path = args.reviewed_family_material_bindings.resolve()
        payload = json.loads(
            reviewed_family_material_binding_path.read_text(encoding="utf-8")
        )
        if payload.get("format") != "EndfieldReviewedFamilyMaterialBindings/1":
            raise RuntimeError(
                f"Unsupported reviewed family binding format: {payload.get('format')!r}"
            )
        for row in payload.get("rows", []):
            base = row.get("entity_base")
            if base not in selected_bases:
                continue
            if base in authoritative_ecs_material_bases:
                raise RuntimeError(f"Reviewed family binding overlaps ECS binding: {base}")
            if base not in template_plans:
                raise RuntimeError(
                    f"Reviewed family binding has no resolved geometry: {base}"
                )
            materials = row.get("materials") or []
            if not materials:
                raise RuntimeError(
                    f"Reviewed family binding has no ordered materials: {base}"
                )
            refs = [
                {"Source": material["Source"], "PathId": int(material["PathId"])}
                for material in materials
            ]
            missing = [ref for ref in refs if ref_key(ref) not in material_records]
            if missing:
                raise RuntimeError(
                    f"Reviewed family material records are absent from probes for {base}: {missing}"
                )
            export, _old_refs, note = template_plans[base]
            template_plans[base] = (export, refs, note)
            reviewed_family_material_bases.add(base)
        print(
            f"reviewed_family_material_bindings={len(reviewed_family_material_bases)}",
            flush=True,
        )
    if args.ecs_instance_bindings:
        ecs_instance_binding_path = args.ecs_instance_bindings.resolve()
        payload = json.loads(
            ecs_instance_binding_path.read_text(encoding="utf-8")
        )
        instance_format = payload.get("format")
        if instance_format not in {
            "EndfieldEcsInstanceBindings/1",
            "EndfieldEcsInstanceBindings/2",
        }:
            raise RuntimeError(
                f"Unsupported ECS instance binding format: {instance_format!r}"
            )
        instance_version = int(str(instance_format).rsplit("/", 1)[1])
        if not payload.get("read_only_game_evidence"):
            raise RuntimeError(
                "ECS instance bindings must identify read-only game evidence"
            )
        instance_binding_rows = payload.get("rows", [])
        if not instance_binding_rows:
            raise RuntimeError("ECS instance bindings contain no rows")
        rows_by_instance_base = defaultdict(list)
        seen_instance_variants = set()
        for binding in instance_binding_rows:
            base = str(binding.get("entity_base") or "")
            variant = str(binding.get("variant_id") or "")
            if (
                not base
                or not re.fullmatch(r"[0-9A-Za-z_.-]{1,96}", variant)
                or (base, variant.casefold()) in seen_instance_variants
            ):
                raise RuntimeError(
                    f"ECS instance binding has a missing, invalid, or "
                    f"duplicate variant: {base!r} {variant!r}"
                )
            seen_instance_variants.add((base, variant.casefold()))
            rows_by_instance_base[base].append(binding)

        instance_database = sqlite3.connect(
            f"file:{work_root / 'instances.sqlite'}?mode=ro", uri=True
        )
        try:
            for base, bindings in rows_by_instance_base.items():
                database_instances = {
                    int(entity_id): {
                        "entity_id": int(entity_id),
                        "name": str(name),
                        "entity_base": str(entity_base),
                        "source_path": str(source_path),
                        "group_index": int(group_index),
                        "entity_index": int(entity_index),
                    }
                    for (
                        entity_id,
                        name,
                        entity_base,
                        source_path,
                        group_index,
                        entity_index,
                    ) in instance_database.execute(
                        "SELECT entity_id,name,entity_base,source_path,"
                        "group_index,entity_index FROM instances "
                        "WHERE entity_base=? ORDER BY entity_id",
                        (base,),
                    )
                }
                if not database_instances:
                    raise RuntimeError(
                        f"ECS instance binding base is absent from database: {base}"
                    )
                if global_instance_counts.get(base, 0) != len(database_instances):
                    raise RuntimeError(
                        f"ECS instance database count disagrees for {base}"
                    )
                if base in selected_bases:
                    if base not in template_plans:
                        raise RuntimeError(
                            f"ECS instance binding has no resolved source "
                            f"geometry: {base}"
                        )
                    if (
                        base in authoritative_ecs_material_bases
                        or base in authoritative_ecs_geometry_bases
                        or base in reviewed_family_material_bases
                    ):
                        raise RuntimeError(
                            f"ECS instance binding overlaps a base-level "
                            f"authoritative binding: {base}"
                        )
                    source_export, _source_refs, source_note = template_plans[base]
                else:
                    source_export = None
                    source_note = None

                covered_entity_ids = set()
                new_plan_keys = []
                existing_template_names = {
                    safe_name(key): key
                    for key in template_plans
                    if key != base
                }
                for binding in bindings:
                    variant = str(binding["variant_id"])
                    confidence = str(binding.get("confidence") or "")
                    if confidence != (
                        "authoritative_ecs_per_instance_renderer_signature"
                    ):
                        raise RuntimeError(
                            f"Unsupported ECS instance confidence for "
                            f"{base}/{variant}: {confidence!r}"
                        )
                    replaces = binding.get("replaces") or {}
                    mesh = binding.get("mesh") or {}
                    materials = binding.get("materials") or []
                    required = ("Source", "PathId", "Name", "Container")
                    if any(key not in replaces for key in required):
                        raise RuntimeError(
                            f"ECS instance source descriptor is incomplete: "
                            f"{base}/{variant}"
                        )
                    if any(key not in mesh for key in required):
                        raise RuntimeError(
                            f"ECS instance Mesh descriptor is incomplete: "
                            f"{base}/{variant}"
                        )
                    expected_slots = int(
                        binding.get("expected_submesh_slots", 0)
                    )
                    if expected_slots <= 0 or len(materials) != expected_slots:
                        raise RuntimeError(
                            f"ECS instance slot metadata is invalid: "
                            f"{base}/{variant}"
                        )
                    if instance_version == 1 and expected_slots != 1:
                        raise RuntimeError(
                            "ECS instance binding v1 requires exactly one "
                            f"Mesh submesh and material: {base}/{variant}"
                        )
                    material_required = (
                        "MaterialId",
                        "Source",
                        "PathId",
                        "Name",
                        "Container",
                    )
                    if any(
                        any(key not in material for key in material_required)
                        for material in materials
                    ):
                        raise RuntimeError(
                            f"ECS instance material descriptors are incomplete: "
                            f"{base}/{variant}"
                        )

                    old_containers = mesh_containers_by_ref.get(
                        ref_key(replaces), set()
                    )
                    if old_containers != {
                        str(replaces["Container"]).casefold()
                    }:
                        raise RuntimeError(
                            f"ECS instance source Mesh container mismatch: "
                            f"{base}/{variant} {sorted(old_containers)}"
                        )
                    if source_export is not None and (
                        ref_key(source_export["Asset"]) != ref_key(replaces)
                        or str(source_export["Asset"].get("Name", "")).casefold()
                        != str(replaces["Name"]).casefold()
                        or str(source_note).casefold()
                        != str(replaces["Name"]).casefold()
                    ):
                        raise RuntimeError(
                            f"ECS instance binding no longer matches source "
                            f"geometry: {base}/{variant}"
                        )

                    target_key = ref_key(mesh)
                    target_candidates = mesh_candidates_by_ref.get(
                        target_key, []
                    )
                    if not target_candidates or any(
                        str(candidate["Asset"].get("Name", "")).casefold()
                        != str(mesh["Name"]).casefold()
                        for candidate in target_candidates
                    ):
                        raise RuntimeError(
                            f"ECS instance target Mesh is absent or "
                            f"conflicting: {base}/{variant}"
                        )
                    target_containers = mesh_containers_by_ref.get(
                        target_key, set()
                    )
                    if target_containers != {
                        str(mesh["Container"]).casefold()
                    }:
                        raise RuntimeError(
                            f"ECS instance target Mesh container mismatch: "
                            f"{base}/{variant} {sorted(target_containers)}"
                        )
                    target_name = str(mesh["Name"])
                    if (
                        not re.search(r"_lod0$", target_name, re.I)
                        or re.search(
                            r"(?:_col\d*|collision|shadowproxy)",
                            target_name,
                            re.I,
                        )
                    ):
                        raise RuntimeError(
                            f"ECS instance target is not a visible LOD0 Mesh: "
                            f"{base}/{variant}"
                        )

                    for slot, material in enumerate(materials):
                        material_key = ref_key(material)
                        if material_key not in material_records:
                            raise RuntimeError(
                                f"ECS instance material is absent from probes: "
                                f"{base}/{variant} slot={slot}"
                            )
                        if (
                            str(
                                material_records[material_key].get("Name", "")
                            ).casefold()
                            != str(material["Name"]).casefold()
                        ):
                            raise RuntimeError(
                                f"ECS instance material name mismatch: "
                                f"{base}/{variant} slot={slot}"
                            )
                        material_containers = (
                            material_containers_by_ref.get(material_key, set())
                        )
                        if material_containers != {
                            str(material["Container"]).casefold()
                        }:
                            raise RuntimeError(
                                f"ECS instance material container mismatch: "
                                f"{base}/{variant} slot={slot} "
                                f"{sorted(material_containers)}"
                            )

                    ecs = binding.get("ecs") or {}
                    component_type = int(ecs.get("component_type", -1))
                    stride = int(ecs.get("stride", -1))
                    expected_stride = {
                        68: 48,
                        69: 88,
                        70: 168,
                        71: 328,
                        72: 648,
                    }.get(component_type)
                    if expected_stride is None or stride != expected_stride:
                        raise RuntimeError(
                            f"ECS instance component layout is invalid: "
                            f"{base}/{variant}"
                        )
                    populated_pairs = int(
                        ecs.get("populated_pair_count", 0)
                    )
                    pair_capacity = {
                        68: 1,
                        69: 2,
                        70: 4,
                        71: 8,
                        72: 16,
                    }[component_type]
                    if not expected_slots <= populated_pairs <= pair_capacity:
                        raise RuntimeError(
                            f"ECS instance populated pair count is invalid: "
                            f"{base}/{variant}"
                        )
                    if instance_version == 1:
                        material_ids = [str(ecs.get("material_id", ""))]
                        material_paths = [str(ecs.get("material_path", ""))]
                        mesh_lod = 0
                    else:
                        material_ids = [
                            str(value)
                            for value in (ecs.get("material_ids") or [])
                        ]
                        material_paths = [
                            str(value)
                            for value in (ecs.get("material_paths") or [])
                        ]
                        mesh_lod = int(ecs.get("mesh_lod", -1))
                    mesh_id = str(ecs.get("mesh_id", ""))
                    if (
                        mesh_lod != 0
                        or len(material_ids) != expected_slots
                        or len(material_paths) != expected_slots
                        or any(
                            not re.fullmatch(r"[0-9a-f]{16}", value, re.I)
                            for value in material_ids
                        )
                        or not re.fullmatch(r"[0-9a-f]{16}", mesh_id, re.I)
                        or [
                            str(material["MaterialId"]).casefold()
                            for material in materials
                        ]
                        != [value.casefold() for value in material_ids]
                        or [
                            str(material["Container"]).casefold()
                            for material in materials
                        ]
                        != [value.casefold() for value in material_paths]
                        or str(ecs.get("mesh_path", "")).casefold()
                        != (
                            f"{mesh['Container']}##{mesh['Name']}"
                        ).casefold()
                    ):
                        raise RuntimeError(
                            f"ECS instance signature disagrees with asset "
                            f"descriptors: {base}/{variant}"
                        )

                    exact_instances = binding.get("instances") or []
                    if (
                        len(exact_instances)
                        != int(ecs.get("database_instance_count", -1))
                    ):
                        raise RuntimeError(
                            f"ECS instance count metadata is stale: "
                            f"{base}/{variant}"
                        )
                    plan_key = f"{base}@@ECS@@{variant}"
                    plan_safe_name = safe_name(plan_key)
                    if plan_safe_name in existing_template_names:
                        raise RuntimeError(
                            f"ECS instance template name collides after "
                            f"sanitizing: {base}/{variant}"
                        )
                    existing_template_names[plan_safe_name] = plan_key
                    for exact in exact_instances:
                        entity_id = int(exact.get("entity_id", -1))
                        database = database_instances.get(entity_id)
                        if (
                            database is None
                            or entity_id in covered_entity_ids
                            or str(exact.get("name", "")) != database["name"]
                            or str(exact.get("source_path", ""))
                            != database["source_path"]
                            or int(exact.get("group_index", -1))
                            != database["group_index"]
                            or int(exact.get("entity_index", -1))
                            != database["entity_index"]
                        ):
                            raise RuntimeError(
                                f"ECS instance identity is missing, "
                                f"duplicated, or stale: {base}/{variant} "
                                f"entity_id={entity_id}"
                            )
                        covered_entity_ids.add(entity_id)
                        ecs_instance_plan_key_by_entity_id[entity_id] = (
                            plan_key
                        )

                    if base in selected_bases:
                        template_plans[plan_key] = (
                            mesh_by_ref[target_key],
                            [
                                {
                                    "Source": material["Source"],
                                    "PathId": int(material["PathId"]),
                                }
                                for material in materials
                            ],
                            target_name,
                        )
                        ecs_material_expected_slots[plan_key] = expected_slots
                        ecs_instance_plan_keys.add(plan_key)
                        ecs_instance_plan_original_bases[plan_key] = base
                        ecs_instance_plan_variants[plan_key] = variant
                        ecs_instance_plan_metadata[plan_key] = {
                            "confidence": confidence,
                            "material_ids": [
                                value.casefold() for value in material_ids
                            ],
                            "mesh_id": mesh_id.casefold(),
                            "database_instance_count": len(exact_instances),
                        }
                        new_plan_keys.append(plan_key)

                if covered_entity_ids != set(database_instances):
                    missing = sorted(
                        set(database_instances) - covered_entity_ids
                    )
                    extra = sorted(
                        covered_entity_ids - set(database_instances)
                    )
                    raise RuntimeError(
                        f"ECS instance bindings do not exactly cover {base}: "
                        f"missing={missing} extra={extra}"
                    )
                ecs_instance_database_count += len(database_instances)
                ecs_instance_bound_bases.add(base)
                if base in selected_bases:
                    template_plans.pop(base)
                    if not new_plan_keys:
                        raise RuntimeError(
                            f"ECS instance binding created no selected "
                            f"variants: {base}"
                        )
        finally:
            instance_database.close()
        print(
            f"authoritative_ecs_instance_variants="
            f"{len(ecs_instance_plan_keys)} "
            f"database_instances={ecs_instance_database_count}",
            flush=True,
        )
    print(f"template_plans={len(template_plans)}", flush=True)

    material_cache = {}
    templates = {}
    geometry_material = None
    fallback_material = None
    submesh_material_binding_count = 0
    submesh_material_mismatch_count = 0
    experimental_family_material_binding_count = 0
    experimental_map01_family_material_binding_count = 0
    if args.geometry_only:
        geometry_material = bpy.data.materials.new("Geometry_Only_Neutral")
        geometry_material.diffuse_color = (0.42, 0.47, 0.52, 1.0)
        geometry_material.use_nodes = True
        principled = next(node for node in geometry_material.node_tree.nodes if node.type == "BSDF_PRINCIPLED")
        principled.inputs["Base Color"].default_value = (0.42, 0.47, 0.52, 1.0)
        principled.inputs["Roughness"].default_value = 0.72
    else:
        fallback_material = bpy.data.materials.new("Unbound_Model_Material_Neutral")
        fallback_material.diffuse_color = (0.38, 0.41, 0.45, 1.0)
        fallback_material.use_nodes = True
        principled = next(node for node in fallback_material.node_tree.nodes if node.type == "BSDF_PRINCIPLED")
        principled.inputs["Base Color"].default_value = (0.38, 0.41, 0.45, 1.0)
        principled.inputs["Roughness"].default_value = 0.75
        fallback_material["note"] = "No verified renderer-to-material binding; intentionally neutral"
    for index, (base, (export, refs, note)) in enumerate(template_plans.items(), 1):
        imported = import_obj(Path(export["_ProbeRoot"]) / export["File"].replace("\\", "/"))
        if not imported:
            if (
                base in authoritative_ecs_geometry_bases
                or base in ecs_material_expected_slots
            ):
                raise RuntimeError(
                    f"Authoritative ECS geometry imported no submeshes: {base}"
                )
            continue
        effective_refs = list(refs)
        if base in ecs_material_expected_slots:
            expected_slots = ecs_material_expected_slots[base]
            if (
                len(imported) != expected_slots
                or len(effective_refs) != expected_slots
                or any(len(obj.data.polygons) == 0 for obj in imported)
            ):
                raise RuntimeError(
                    f"ECS material slot count mismatch for {base}: "
                    f"submeshes={len(imported)} materials={len(effective_refs)} "
                    f"expected={expected_slots}"
                )
        if base in reviewed_family_material_bases and len(imported) != len(effective_refs):
            raise RuntimeError(
                f"Reviewed family slot count mismatch for {base}: "
                f"submeshes={len(imported)} materials={len(effective_refs)}"
            )
        experimental_binding = False
        experimental_map01_binding = False
        if (not effective_refs and args.experimental_newplant_family_materials and
                not args.geometry_only):
            names = experimental_newplant_material_names(note, len(imported))
            candidates = []
            for name in names:
                unique = {
                    (row["Source"].casefold(), int(row["PathId"])): row
                    for row in material_candidates_by_name.get(name.casefold(), [])
                }
                if len(unique) != 1:
                    candidates = []
                    break
                candidates.append(next(iter(unique.values())))
            if candidates:
                effective_refs = [
                    {"Source": row["Source"], "PathId": row["PathId"]} for row in candidates
                ]
                experimental_binding = True
                experimental_family_material_binding_count += 1
        if (not effective_refs and args.experimental_map01_family_materials and
                not args.geometry_only):
            names = experimental_map01_family_material_names(note, len(imported))
            candidates = []
            for name in names:
                unique = {
                    (row["Source"].casefold(), int(row["PathId"])): row
                    for row in material_candidates_by_name.get(name.casefold(), [])
                }
                if len(unique) != 1:
                    candidates = []
                    break
                candidates.append(next(iter(unique.values())))
            if candidates:
                effective_refs = [
                    {"Source": row["Source"], "PathId": row["PathId"]} for row in candidates
                ]
                experimental_map01_binding = True
                experimental_map01_family_material_binding_count += 1
        assigned_materials = []
        if args.geometry_only:
            assigned_materials = [geometry_material]
        else:
            for ref in effective_refs:
                k = ref_key(ref)
                if k not in material_cache and k in material_records:
                    material_cache[k] = create_material(material_records[k], texture_files,
                                                        not args.no_pack_textures)
                if k in material_cache:
                    assigned_materials.append(material_cache[k])
            if not assigned_materials:
                assigned_materials = [fallback_material]
        if (
            base in ecs_material_expected_slots
            and len(assigned_materials) != ecs_material_expected_slots[base]
        ):
            raise RuntimeError(
                f"ECS material construction failed for one or more slots: {base}"
            )
        if len(imported) > 1 and len(assigned_materials) >= len(imported):
            submesh_material_binding_count += 1
        elif len(imported) > 1 and len(assigned_materials) != 1:
            submesh_material_mismatch_count += 1
        preserve_ordered_slots = (
            len(imported) > 1
            and len(assigned_materials) >= len(imported)
        )
        final_submesh_materials = []
        join_slot_materials = []
        temporary_join_materials = []
        used_join_material_pointers = set()
        for submesh_index, obj in enumerate(imported):
            obj.data.materials.clear()
            if len(assigned_materials) == 1:
                material = assigned_materials[0]
            elif submesh_index < len(assigned_materials):
                material = assigned_materials[submesh_index]
            else:
                material = fallback_material
            final_submesh_materials.append(material)
            join_material = material
            if (
                preserve_ordered_slots
                and join_material.as_pointer()
                in used_join_material_pointers
            ):
                join_material = material.copy()
                join_material.name = (
                    material.name
                    + f"__TEMP_JOIN_SLOT_{submesh_index:02d}"
                )
                join_material["endfield_temporary_join_slot"] = True
                temporary_join_materials.append(join_material)
            used_join_material_pointers.add(join_material.as_pointer())
            join_slot_materials.append(join_material)
            obj.data.materials.append(join_material)
        source = join_submeshes(imported)
        mesh = source.data
        if preserve_ordered_slots:
            joined_slots = list(mesh.materials)
            expected_pointers = {
                material.as_pointer()
                for material in join_slot_materials
            }
            actual_pointers = {
                material.as_pointer()
                for material in joined_slots
                if material is not None
            }
            if (
                len(joined_slots) != len(join_slot_materials)
                or actual_pointers != expected_pointers
            ):
                raise RuntimeError(
                    "Blender Join did not preserve unique temporary "
                    f"submesh slots: {base}"
                )
            pointer_to_final_index = {
                material.as_pointer(): index
                for index, material in enumerate(join_slot_materials)
            }
            joined_index_to_final_index = {
                index: pointer_to_final_index[material.as_pointer()]
                for index, material in enumerate(joined_slots)
            }
            final_polygon_indices = [
                joined_index_to_final_index[polygon.material_index]
                for polygon in mesh.polygons
            ]
            mesh.materials.clear()
            for material in final_submesh_materials:
                mesh.materials.append(material)
            for polygon, material_index in zip(mesh.polygons, final_polygon_indices):
                polygon.material_index = material_index
            for material in temporary_join_materials:
                bpy.data.materials.remove(material, do_unlink=True)
        mesh.name = "mesh_" + safe_name(base)
        template = bpy.data.objects.new("SRC_" + safe_name(base), mesh)
        templates_collection.objects.link(template)
        template.hide_set(True)
        template.hide_render = True
        template["source_asset"] = note
        template["source_coordinate_correction"] = (
            "EndfieldSceneProbe/1 OBJ (-x,y,z) unmirrored on Blender local X; "
            "triangle winding restored"
        )
        if base in ecs_instance_plan_keys:
            metadata = ecs_instance_plan_metadata[base]
            template["geometry_binding_method"] = metadata["confidence"]
            template["entity_base"] = ecs_instance_plan_original_bases[base]
            template["instance_binding_variant"] = (
                ecs_instance_plan_variants[base]
            )
            template["instance_binding_plan_key"] = base
            template["ecs_material_ids"] = json.dumps(
                metadata["material_ids"]
            )
            if len(metadata["material_ids"]) == 1:
                template["ecs_material_id"] = metadata["material_ids"][0]
            template["ecs_mesh_id"] = metadata["mesh_id"]
            template["database_instance_count"] = metadata[
                "database_instance_count"
            ]
        elif base in authoritative_ecs_geometry_bases:
            template["geometry_binding_method"] = (
                ecs_geometry_binding_methods[base]
            )
        elif base in experimental_geometry_aliases:
            template["geometry_binding_method"] = "experimental_explicit_newplant_runtime_alias"
            template["geometry_alias_review_required"] = True
        elif base in experimental_tystair_geometry_aliases:
            template["geometry_binding_method"] = "experimental_exact_base01_tystair_runtime_alias"
            template["geometry_alias_review_required"] = True
        template["material_binding_method"] = (
            "authoritative_ecs_per_instance_renderer_signature"
            if base in ecs_instance_plan_keys else
            "authoritative_ecs_init_chunk_material_ids"
            if base in authoritative_ecs_material_bases else
            "reviewed_authored_family_consensus"
            if base in reviewed_family_material_bases else
            "experimental_map01_family_palette" if experimental_map01_binding else
            "experimental_newplant_family_palette" if experimental_binding else
            ("verified_reference_or_exact_name" if refs else "unbound_neutral")
        )
        templates[base] = template
        bpy.data.objects.remove(source, do_unlink=True)
        if index % 100 == 0:
            print(f"templates={index}/{len(template_plans)}", flush=True)

    rows_by_base = defaultdict(list)
    quarantined_rows_by_base = defaultdict(list)
    quarantine_audit = []
    terrain_samples = (
        load_terrain_samples(work_root / "terrain.sqlite")
        if args.quarantine_newplant_state_outliers else {}
    )
    unresolved_positions = []
    ecs_instance_selected_count = 0
    for row in selected_rows:
        plan_key = ecs_instance_plan_key_by_entity_id.get(row[1], row[0])
        if plan_key in templates:
            if plan_key != row[0]:
                ecs_instance_selected_count += 1
            if args.quarantine_newplant_state_outliers:
                suspect, audit = newplant_state_outlier(
                    row, templates[plan_key], terrain_samples
                )
                if suspect:
                    quarantined_rows_by_base[plan_key].append(row)
                    quarantine_audit.append({
                        "entity_base": row[0],
                        "template_plan_key": plan_key,
                        "entity_id": row[1],
                        "name": row[3],
                        **audit,
                    })
                    continue
            region = streaming_region(row[6]) if args.group_by_streaming_region else None
            rows_by_base[(region, plan_key)].append(row)
        else:
            unresolved_positions.append(tuple(unity_matrix(row[2]).translation))
    region_collections = {}
    placed = 0
    for index, ((region, base), rows) in enumerate(rows_by_base.items(), 1):
        target_collection = models
        if region is not None:
            if region not in region_collections:
                target_collection = bpy.data.collections.new(region)
                models.children.link(target_collection)
                region_collections[region] = target_collection
            else:
                target_collection = region_collections[region]
        instancer = make_instancer(base, templates[base], rows, target_collection)
        if base in ecs_instance_plan_keys:
            instancer["entity_base"] = ecs_instance_plan_original_bases[base]
            instancer["instance_binding_variant"] = (
                ecs_instance_plan_variants[base]
            )
            instancer["instance_binding_plan_key"] = base
            instancer["instance_binding_method"] = (
                ecs_instance_plan_metadata[base]["confidence"]
            )
        if region is not None:
            instancer["streaming_region"] = region
        placed += len(rows)
        if index % 100 == 0:
            print(f"instancers={index}/{len(rows_by_base)} placed={placed}", flush=True)

    quarantined_count = 0
    for base, rows in quarantined_rows_by_base.items():
        instancer = make_instancer(base + "_QUARANTINED", templates[base], rows, quarantine_collection)
        instancer["quarantine_reason"] = "Strong terrain inconsistency in screenshot-validated 03a-03g alternate-state family"
        instancer["original_entity_base"] = ecs_instance_plan_original_bases.get(
            base, base
        )
        quarantined_count += len(rows)
    quarantine_collection["quarantined_instance_count"] = quarantined_count
    quarantine_collection["audit_json"] = json.dumps(quarantine_audit, ensure_ascii=False)
    print(f"quarantined_newplant_state_instances={quarantined_count}", flush=True)

    unresolved_mesh = bpy.data.meshes.new("unresolved_entity_points")
    unresolved_mesh.from_pydata(unresolved_positions, [], [])
    unresolved_obj = bpy.data.objects.new("Unresolved_Entity_Positions", unresolved_mesh)
    unresolved_collection.objects.link(unresolved_obj)
    unresolved_obj["unresolved_count"] = len(unresolved_positions)
    unresolved_obj["note"] = "Positions retained; no confidently matched mesh template yet"

    marker_count = 0
    if args.marker_regex:
        marker_pattern = re.compile(args.marker_regex, re.I)
        marker_collection = bpy.data.collections.new("Diagnostic_Landmark_Markers")
        bpy.context.scene.collection.children.link(marker_collection)
        marker_material = bpy.data.materials.new("Diagnostic_Red_NotGameMaterial")
        marker_material.diffuse_color = (1.0, 0.02, 0.01, 1.0)
        marker_material.use_nodes = True
        marker_principled = next(node for node in marker_material.node_tree.nodes if node.type == "BSDF_PRINCIPLED")
        marker_principled.inputs["Base Color"].default_value = (1.0, 0.01, 0.005, 1.0)
        marker_principled.inputs["Emission Color"].default_value = (1.0, 0.0, 0.0, 1.0)
        marker_principled.inputs["Emission Strength"].default_value = 3.0
        for base, entity_id, matrix_blob, name, _x, _z, _source_path in selected_rows:
            if not (marker_pattern.search(base or "") or marker_pattern.search(name or "")):
                continue
            bpy.ops.mesh.primitive_ico_sphere_add(subdivisions=2, radius=3.0,
                                                  location=unity_matrix(matrix_blob).translation)
            marker = bpy.context.object
            marker.name = "LANDMARK_MARKER_" + safe_name(name, 80)
            for collection in list(marker.users_collection):
                collection.objects.unlink(marker)
            marker_collection.objects.link(marker)
            marker.data.materials.append(marker_material)
            marker["entity_id"] = entity_id
            marker["source_name"] = name
            marker["note"] = "Red diagnostic marker; not original game geometry"
            marker_count += 1

    terrain_database = work_root / "terrain.sqlite"
    if args.terrain_surface:
        terrain_database = args.terrain_surface.resolve() / "terrain.sqlite"
    terrain_count, terrain_material_status = build_terrain(
        terrain_database,
        terrain_collection,
        bounds,
        args.terrain_surface.resolve() if args.terrain_surface else None,
        not args.no_pack_textures,
    )
    imported_game_light_count = 0
    # The current probes do not yet expose authoritative Light components. Keep
    # the requested collection and switch in the file schema now, but never
    # mislabel a guessed light as original game data.
    if args.import_game_lights:
        print("game_lights=0 (no authoritative light manifest supplied)", flush=True)
    preview_light_count = 0
    if args.add_preview_sun:
        light_data = bpy.data.lights.new("Preview_Sun_NotOriginal", "SUN")
        light_data.energy = 2.0
        light = bpy.data.objects.new("Preview_Sun_NotOriginal", light_data)
        preview_lights_collection.objects.link(light)
        light.rotation_euler = (0.65, -0.35, -0.55)
        light["note"] = "Convenience preview light; not extracted from the game"
        preview_light_count = 1

    scene = bpy.context.scene
    scene["source_game"] = "Arknights: Endfield"
    scene["source_map"] = "map02 / Wuling"
    scene["unity_region_xz"] = "complete" if bounds is None else str(bounds)
    scene["resolved_instance_count"] = placed
    scene["unresolved_instance_count"] = len(unresolved_positions)
    scene["instancer_count"] = len(rows_by_base)
    scene["terrain_tile_count"] = terrain_count
    scene["include_regex"] = args.include_regex or "all"
    scene["exclude_regex"] = args.exclude_regex or "none"
    scene["geometry_only"] = bool(args.geometry_only)
    scene["terrain_footprint_only"] = bool(args.terrain_footprint_only)
    scene["terrain_footprint_rejected_count"] = terrain_rejected
    scene["grouped_by_streaming_region"] = bool(args.group_by_streaming_region)
    scene["streaming_region_collection_count"] = len(region_collections)
    scene["asset_resolution_db"] = str(assets_db)
    scene["source_coordinate_correction"] = (
        "EndfieldSceneProbe/1 OBJ local X unmirror with restored winding"
    )
    scene["source_coordinate_correction_version"] = 1
    scene["exact_model_material_binding_count"] = exact_model_material_bindings
    scene["exact_hlod_material_binding_count"] = exact_hlod_material_bindings
    scene["exact_mesh_material_binding_count"] = exact_mesh_material_bindings
    scene["exact_map01_alias_material_binding_count"] = exact_map01_alias_material_bindings
    scene["reverse_renderer_material_binding_count"] = reverse_renderer_material_bindings
    scene["reverse_renderer_asset_name_binding_count"] = reverse_renderer_asset_name_bindings
    scene["reverse_renderer_source_lod_family_binding_count"] = (
        reverse_renderer_source_lod_family_bindings)
    scene["reverse_renderer_lod_family_binding_count"] = reverse_renderer_lod_family_bindings
    scene["ecs_material_bindings"] = str(ecs_material_binding_path or "")
    scene["authoritative_ecs_material_binding_count"] = len(authoritative_ecs_material_bases)
    scene["ecs_geometry_bindings"] = str(ecs_geometry_binding_path or "")
    scene["authoritative_ecs_geometry_binding_count"] = len(
        authoritative_ecs_geometry_bases
    )
    scene["ecs_instance_bindings"] = str(
        ecs_instance_binding_path or ""
    )
    scene["authoritative_ecs_instance_binding_base_count"] = len(
        ecs_instance_bound_bases
    )
    scene["authoritative_ecs_instance_variant_count"] = len(
        ecs_instance_plan_keys
    )
    scene["authoritative_ecs_instance_database_count"] = (
        ecs_instance_database_count
    )
    scene["authoritative_ecs_instance_selected_count"] = (
        ecs_instance_selected_count
    )
    scene["submesh_material_binding_count"] = submesh_material_binding_count
    scene["submesh_material_mismatch_count"] = submesh_material_mismatch_count
    scene["experimental_newplant_family_materials_enabled"] = bool(
        args.experimental_newplant_family_materials)
    scene["experimental_family_material_binding_count"] = experimental_family_material_binding_count
    scene["experimental_map01_family_materials_enabled"] = bool(
        args.experimental_map01_family_materials)
    scene["experimental_map01_family_material_binding_count"] = (
        experimental_map01_family_material_binding_count)
    scene["experimental_newplant_geometry_aliases_enabled"] = bool(
        args.experimental_newplant_geometry_aliases)
    scene["experimental_newplant_geometry_alias_count"] = len(experimental_geometry_aliases)
    scene["experimental_tystair_geometry_aliases_enabled"] = bool(
        args.experimental_tystair_geometry_aliases)
    scene["experimental_tystair_geometry_alias_count"] = len(experimental_tystair_geometry_aliases)
    scene["quarantine_newplant_state_outliers_enabled"] = bool(
        args.quarantine_newplant_state_outliers)
    scene["quarantined_newplant_state_instance_count"] = quarantined_count
    scene["ambiguous_model_material_binding_count"] = ambiguous_model_material_bindings
    scene["probe_roots"] = ";".join(str(path) for path in probe_roots)
    scene["diagnostic_marker_count"] = marker_count
    scene["terrain_material_status"] = terrain_material_status
    scene["terrain_decode"] = "65x65 uint16; origin XZ=(-2048,-2048); height = base_y + uint16 * 730 / 32768; values>=32768=no-data skirt"
    scene["import_game_lights_requested"] = bool(args.import_game_lights)
    scene["imported_game_light_count"] = imported_game_light_count
    scene["preview_light_count"] = preview_light_count
    scene["preview_sun_status"] = "not added" if not args.add_preview_sun else "not an original game light"
    output.parent.mkdir(parents=True, exist_ok=True)
    bpy.ops.wm.save_as_mainfile(filepath=str(output))
    print(f"Saved {output}; placed={placed}; unresolved={len(unresolved_positions)}; terrain={terrain_count}", flush=True)


if __name__ == "__main__":
    main()
