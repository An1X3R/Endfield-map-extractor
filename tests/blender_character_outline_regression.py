"""Run with Blender --background --factory-startup --disable-autoexec --python-exit-code 1 --python this_file -- --blend path --meters-per-source-width value."""
from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys
from typing import cast

import bpy

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src" / "character_runtime"))
from character_outline import attach_character_outline_hulls


def _matrix(obj: bpy.types.Object) -> tuple[float, ...]:
    return tuple(round(float(value), 6) for row in obj.matrix_world for value in row)


def _source_state(obj: bpy.types.Object) -> dict[str, object]:
    shape_keys = obj.data.shape_keys
    return {
        "data_pointer": obj.data.as_pointer(),
        "vertices": len(obj.data.vertices),
        "edges": len(obj.data.edges),
        "polygons": len(obj.data.polygons),
        "loops": len(obj.data.loops),
        "vertex_groups": tuple(group.name for group in obj.vertex_groups),
        "shape_keys": tuple(key.name for key in shape_keys.key_blocks) if shape_keys is not None else (),
        "modifiers": tuple((modifier.name, modifier.type, modifier.show_viewport, modifier.show_render) for modifier in obj.modifiers),
        "materials": tuple((slot.material.name if slot.material is not None else None, slot.link) for slot in obj.material_slots),
        "parent": obj.parent.name if obj.parent is not None else None,
        "matrix": _matrix(obj),
        "hide_render": obj.hide_render,
        "outline_candidate_source": obj.get("outline_candidate_source"),
    }


def _has_upstream_node(
    tree: bpy.types.NodeTree,
    input_socket: bpy.types.NodeSocket,
    expected: bpy.types.Node,
) -> bool:
    pending = [link.from_node for link in input_socket.links]
    visited: set[int] = set()
    while pending:
        node = pending.pop()
        pointer = node.as_pointer()
        if pointer in visited:
            continue
        if node == expected:
            return True
        visited.add(pointer)
        pending.extend(link.from_node for socket in node.inputs for link in socket.links)
    return False


def _arguments() -> argparse.Namespace:
    if "--" not in sys.argv:
        raise ValueError("Blender arguments must contain -- before --blend")
    argument_index = sys.argv.index("--")
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--blend", type=Path, required=True)
    parser.add_argument("--meters-per-source-width", type=float, required=True)
    return parser.parse_args(sys.argv[argument_index + 1:])


def main() -> None:
    args = _arguments()
    if not args.blend.is_file():
        raise FileNotFoundError(f"Si source blend is unavailable: {args.blend}")
    bpy.ops.wm.open_mainfile(filepath=str(args.blend))
    scene = bpy.context.scene
    source_objects = {
        obj.name: obj
        for obj in scene.objects
        if obj.type == "MESH" and obj.get("source_renderer_pipeline_json") is not None
    }
    if not source_objects:
        raise ValueError(f"Blend has no source renderer metadata: {args.blend}")
    source_states = {name: _source_state(obj) for name, obj in source_objects.items()}
    report = attach_character_outline_hulls(scene, args.meters_per_source_width)
    if report["installed_count"] != 6:
        raise AssertionError(f"Expected six source-gated Si LOD0 renderers: {report}")
    expected_sources = {
        "S_actor_jsspsi_body_01_lod0",
        "S_actor_jsspsi_brow_01_lod0",
        "S_actor_jsspsi_face_01_lod0",
        "S_actor_jsspsi_cloth_01_lod0",
        "S_actor_jsspsi_cloth_02_lod0",
        "S_actor_jsspsi_hair_01_lod0",
    }
    installed_rows = tuple(
        cast(dict[str, object], row)
        for row in cast(list[object], report["rows"])
        if isinstance(row, dict) and row.get("status") == "installed"
    )
    installed_sources = frozenset(str(row["object"]) for row in installed_rows)
    if installed_sources != expected_sources:
        raise AssertionError(f"Si outline renderer selection changed: expected={sorted(expected_sources)} actual={sorted(installed_sources)}")
    hair_row = next(row for row in installed_rows if row["object"] == "S_actor_jsspsi_hair_01_lod0")
    hair_materials = cast(list[object], hair_row["materials"])
    if len(hair_materials) != 1 or not isinstance(hair_materials[0], dict):
        raise AssertionError(f"Si hair source pass mapping changed: {hair_row}")
    hair_material = cast(dict[str, object], hair_materials[0])
    if hair_material["source_pass_count"] != 2 or hair_material["source_pass_appearance_match"] is not True:
        raise AssertionError(f"Si hair outline passes no longer share compatible source appearance: {hair_material}")
    for name, expected_state in source_states.items():
        if _source_state(source_objects[name]) != expected_state:
            raise AssertionError(f"Source mesh or material state changed: {name}")
    for row in installed_rows:
        source = source_objects[str(row["object"])]
        outline = scene.objects[str(row["outline_object"])]
        if any((outline.visible_shadow, outline.visible_diffuse, outline.visible_glossy,
                outline.visible_transmission, outline.visible_volume_scatter)):
            raise AssertionError(f"Outline must not affect Cycles surface lighting: {outline.name}")
        if outline.data != source.data or outline.data.as_pointer() != source.data.as_pointer():
            raise AssertionError(f"Outline copy does not preserve shared source geometry: {source.name}")
        if outline.parent != source.parent or _matrix(outline) != _matrix(source):
            raise AssertionError(f"Outline copy lost source parent or transform: {source.name}")
        if tuple((modifier.name, modifier.type, modifier.show_viewport, modifier.show_render) for modifier in outline.modifiers[:-1]) != source_states[source.name]["modifiers"]:
            raise AssertionError(f"Outline copy did not retain source deformation modifiers: {source.name}")
        if not outline.modifiers or outline.modifiers[-1].type != "NODES":
            raise AssertionError(f"Outline hull is not the final geometry modifier: {source.name}")
        group = outline.modifiers[-1].node_group
        if group is None or group.get("outline_candidate_contract") != "EndfieldCharacterOutlineHull/1":
            raise AssertionError(f"Outline geometry group contract is missing: {source.name}")
        node_types = frozenset(node.type for node in group.nodes)
        if not {"INPUT_NORMAL", "SET_POSITION"}.issubset(node_types) or "FLIP_FACES" in node_types:
            raise AssertionError(f"Outline hull deformation graph is incomplete: {source.name}; nodes={sorted(node_types)}")
        if len(outline.material_slots) != len(source.material_slots):
            raise AssertionError(f"Outline copy changed material slot count: {source.name}")
        if any(slot.link != "OBJECT" or slot.material is None for slot in outline.material_slots):
            raise AssertionError(f"Outline copy has an unbound material slot: {source.name}")
        outline_materials = tuple(
            slot.material for slot in outline.material_slots
            if slot.material is not None and slot.material.get("source_shader") is not None
        )
        if not outline_materials:
            raise AssertionError(f"Outline hull has no source-derived shader material: {source.name}")
        for material in outline_materials:
            material_tree = material.node_tree
            if material_tree is None:
                raise AssertionError(f"Outline material has no shader graph: {material.name}")
            geometry = material_tree.nodes.get("Hull Backfacing")
            mix = material_tree.nodes.get("Backface Outline and Transparent Interior")
            if geometry is None or geometry.type != "NEW_GEOMETRY" or mix is None or mix.type != "MIX_SHADER":
                raise AssertionError(f"Outline material lost its hull culling graph: {material.name}")
            if not _has_upstream_node(material_tree, mix.inputs[0], geometry):
                raise AssertionError(f"Backfacing no longer selects the outline shell: {material.name}")
            transparent_links = tuple(mix.inputs[1].links)
            outline_links = tuple(mix.inputs[2].links)
            if len(transparent_links) != 1 or transparent_links[0].from_node.type != "BSDF_TRANSPARENT":
                raise AssertionError(f"Mix input 1 must retain the transparent hull interior: {material.name}")
            if len(outline_links) != 1 or outline_links[0].from_node.type != "EMISSION":
                raise AssertionError(f"Mix input 2 must retain the ink shader on backfaces: {material.name}")
    cloth_outline = scene.objects["EF Outline Approx S_actor_jsspsi_cloth_01_lod0"].material_slots[0].material
    if cloth_outline is None or cloth_outline.node_tree is None:
        raise AssertionError("Si cloth outline material is unavailable")
    cloth_parameters_raw = cloth_outline.get("source_outline_parameters")
    if not isinstance(cloth_parameters_raw, str):
        raise AssertionError("Si cloth source outline settings were not retained")
    cloth_parameters = json.loads(cloth_parameters_raw)
    if (
        cloth_parameters.get("tint_enabled") != 0.0
        or cloth_parameters.get("use_tint_controls") is not True
        or cloth_parameters.get("brightness") != 0.35
        or cloth_parameters.get("saturation") != 1.5
    ):
        raise AssertionError(f"Si cloth outline color branch does not match compiled source settings: {cloth_parameters}")
    hue = cloth_outline.node_tree.nodes.get("Source Brightness and Saturation (approximate)")
    sampled_base_color = cloth_outline.node_tree.nodes.get("Source BaseMap × BaseColor")
    if hue is None or sampled_base_color is None or abs(float(hue.inputs["Value"].default_value) - 0.35) > 1e-6 or abs(float(hue.inputs["Saturation"].default_value) - 1.5) > 1e-6:
        raise AssertionError("Si cloth source brightness/saturation values are absent from the output graph")
    color_links = tuple(hue.inputs["Color"].links)
    if len(color_links) != 1 or color_links[0].from_node != sampled_base_color:
        raise AssertionError("Si cloth brightness/saturation branch does not use the source albedo color")
    disabled_sources = {
        "S_actor_jsspsi_cloth_03_lod0",
        "S_actor_jsspsi_cloth_07_lod0",
        "S_actor_jsspsi_iris_01_lod0",
    }
    if installed_sources.intersection(disabled_sources):
        raise AssertionError(f"Source-disabled or unsupported renderer received an outline: {sorted(installed_sources.intersection(disabled_sources))}")
    print(json.dumps({
        "status": "passed",
        "blend": str(args.blend.resolve()),
        "rendered": False,
        "saved": False,
        "source_renderer_count": len(source_objects),
        "outline_renderer_count": len(installed_rows),
        "outline_sources": sorted(installed_sources),
        "source_geometry_preserved": True,
        "source_deformation_modifiers_preserved": True,
        "source_material_slots_preserved": True,
        "report": {
            "format": report["format"],
            "scope": report["scope"],
            "artistic_meters_per_source_width": report["artistic_meters_per_source_width"],
            "installed": [
                {
                    "object": row["object"],
                    "outline_object": row["outline_object"],
                    "materials": row["materials"],
                }
                for row in installed_rows
            ],
        },
    }, ensure_ascii=False, sort_keys=True))


main()
