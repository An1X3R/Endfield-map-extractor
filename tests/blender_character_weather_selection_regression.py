"""Reject legacy Silk controls in a real simplified-weather Pelica export."""
from __future__ import annotations

import argparse
from collections.abc import Callable
import json
from pathlib import Path
import sys
from typing import cast

import bpy

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'src' / 'character_runtime'))
from character_authoring_validation import material_signature_details
from character_material_authoring import shader_dependencies
from character_material_variant import material_trees, source_materials
from character_weather_panel import attach_weather_panel, verify_weather_panel, weather_groups


def main(arguments: list[str]) -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument('--output', type=Path, required=True)
    output = parser.parse_args(arguments).output.resolve()
    output.mkdir(parents=True, exist_ok=True)
    scene = bpy.context.scene
    options = json.loads(scene['endfield_material_variant'])['options']
    assert options['mode'] == 'simplified' and options['weather']['quality'] == 'simple'
    materials = source_materials(scene)
    before = material_signature_details()
    legacy = {tree.name for material in materials for tree in material_trees(material)
              if any(item.item_type == 'SOCKET' and item.in_out == 'OUTPUT'
                     and item.name == 'Native Global Wetness' for item in tree.interface.items_tree)}
    assert legacy, 'The real regression fixture must retain its legacy Silk weather controls'
    labels = {'wet': 'User Effect Z', 'snow': 'User Effect W', 'rain': 'User Effect X'}
    expected = {labels[effect] for effect in options['weather']['effects']}
    checked: list[str] = []
    for material in materials:
        groups = weather_groups(material)
        if not groups:
            continue
        dependencies = shader_dependencies(material)
        for tree in groups:
            assert tree.get('endfield_simple_weather'), f'Legacy controls exposed: {material.name}/{tree.name}'
            controls = tuple(node for node in tree.nodes if node.type == 'VALUE' and node.label.startswith('User '))
            assert {node.label for node in controls} == expected, (material.name, expected)
            assert all((tree.as_pointer(), node.as_pointer()) in dependencies.nodes for node in controls), material.name
        checked.append(material.name)
    assert checked, 'The real fixture must contain active simple weather controls'
    panel = attach_weather_panel(materials)
    assert verify_weather_panel(panel) == 'passed'
    assert material_signature_details() == before, 'Panel filtering changed shader evaluation'
    bpy.context.preferences.filepaths.save_version = 0
    target = output / 'pelica_selected_weather_panel.blend'
    bpy.ops.wm.save_as_mainfile(filepath=str(target))
    bpy.ops.wm.open_mainfile(filepath=str(target), use_scripts=False, load_ui=False)
    assert verify_weather_panel(panel) == 'passed'
    assert material_signature_details()['sha256'] == before['sha256'], 'Panel save/reopen changed persistent shader evaluation'
    code = bpy.data.texts[panel['text']].as_string()
    namespace: dict[str, object] = {'__name__': '__main__'}
    exec(compile(code, panel['text'], 'exec'), namespace)
    embedded_resolver = cast(Callable[[bpy.types.Material], tuple[bpy.types.NodeTree, ...]], namespace['weather_groups'])
    for material in source_materials(bpy.context.scene):
        embedded_groups = embedded_resolver(material)
        assert all(tree.get('endfield_simple_weather') for tree in embedded_groups), material.name
    document = {'status': 'passed', 'source': 'Actual API simplified/full-light/simple-weather Pelica export',
                'materials_checked': checked, 'retained_legacy_groups': sorted(legacy),
                'legacy_controls_hidden': True, 'selected_controls_reach_material_output': True,
                'shader_signature_unchanged': True, 'embedded_panel_reopened': True, 'blend': str(target)}
    (output / 'weather_selection_regression.json').write_text(
        json.dumps(document, ensure_ascii=False, indent=2), encoding='utf-8')
    print(json.dumps(document, ensure_ascii=False))


if __name__ == '__main__':
    main(sys.argv[sys.argv.index('--') + 1:])
