"""Exercise weather editing and persistence on a real exported character."""
from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys

import bpy

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'src' / 'character_runtime'))
from character_authoring_validation import material_signature_details
from character_weather_panel import (
    CONTROL_ROWS, attach_weather_panel, verify_weather_panel, weather_groups, weather_socket,
)


def main(arguments: list[str]) -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument('--output', type=Path, required=True)
    output = parser.parse_args(arguments).output.resolve()
    output.mkdir(parents=True, exist_ok=True)
    materials = tuple(material for material in bpy.data.materials if material.use_nodes)
    before = material_signature_details()
    report = attach_weather_panel(materials)
    assert report['status'] == 'attached'
    assert verify_weather_panel(report) == 'passed'
    assert material_signature_details() == before
    supported = tuple(material for material in materials if weather_groups(material))
    controls = weather_groups(supported[0])[0]
    original = {label: weather_socket(controls, label).default_value for label, _ in CONTROL_ROWS}
    wet = weather_socket(controls, 'User Effect Z')
    snow = weather_socket(controls, 'User Effect W')
    wet.default_value = 0.65
    snow.default_value = 0.4
    shared_count = sum(any(tree == controls for tree in weather_groups(material)) for material in supported)
    assert shared_count > 1
    for material in supported:
        for tree in weather_groups(material):
            if tree == controls:
                assert abs(weather_socket(tree, 'User Effect Z').default_value - 0.65) < 1e-6
    bpy.context.preferences.filepaths.save_version = 0
    target = output / 'pelica_weather_controls.blend'
    bpy.ops.wm.save_as_mainfile(filepath=str(target))
    name = controls.name
    bpy.ops.wm.open_mainfile(filepath=str(target), use_scripts=False, load_ui=False)
    controls = bpy.data.node_groups[name]
    assert abs(weather_socket(controls, 'User Effect Z').default_value - 0.65) < 1e-6
    assert abs(weather_socket(controls, 'User Effect W').default_value - 0.4) < 1e-6
    assert verify_weather_panel(report) == 'passed'
    for label, value in original.items():
        weather_socket(controls, label).default_value = value
    assert material_signature_details() == before
    code = bpy.data.texts[report['text']].as_string()
    exec(compile(code, report['text'], 'exec'), {'__name__': '__main__'})
    exec(compile(code, report['text'], 'exec'), {'__name__': '__main__'})
    assert getattr(bpy.types, 'ENDFIELD_PT_material_weather', None) is not None
    obj = next(obj for obj in bpy.context.scene.objects if obj.type == 'MESH' and any(
        slot.material is not None and 'cloth_02' in slot.material.name and weather_groups(slot.material)
        for slot in obj.material_slots))
    for selected in bpy.context.selected_objects:
        selected.select_set(False)
    obj.hide_set(False)
    obj.select_set(True)
    bpy.context.view_layer.objects.active = obj
    obj.active_material_index = next(index for index, slot in enumerate(obj.material_slots)
                                     if slot.material is not None and 'cloth_02' in slot.material.name)
    for area in bpy.context.screen.areas:
        if area.type == 'PROPERTIES':
            area.spaces.active.context = 'MATERIAL'
    bpy.ops.wm.save_as_mainfile(filepath=str(target))
    result = {'status': 'passed', 'panel': report, 'shared_materials': shared_count,
              'original_material_signature': 'unchanged', 'nonzero_values_persist_without_scripts': True,
              'repeated_embedded_registration': 'passed', 'blend': str(target)}
    (output / 'weather_panel_verification.json').write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding='utf-8')
    print(json.dumps(result, ensure_ascii=False))


if __name__ == '__main__':
    main(sys.argv[sys.argv.index('--') + 1:])
