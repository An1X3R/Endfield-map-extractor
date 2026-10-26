"""Render declared optional effects in real exports without changing delivery files."""
from __future__ import annotations

import argparse
import hashlib
import json
import math
from pathlib import Path
import sys
from typing import Literal, cast

import bpy
from mathutils import Vector
import numpy as np

sys.dont_write_bytecode = True
sys.path[:0] = [str(Path(__file__).resolve().parents[1] / 'src'),
                str(Path(__file__).resolve().parents[1] / 'src/character_runtime')]
from character_material_authoring import shader_dependencies
from character_potential_build import verify_potential_appearance
from character_potential_controls import PROPERTY
from character_shader_uniforms import verify_shader_uniforms
from character_weather_panel import clothing_effect_controls


def _record(value: object, label: str) -> dict[str, object]:
    if not isinstance(value, dict) or any(not isinstance(key, str) for key in value):
        raise TypeError(f'Expected a JSON object: {label}')
    return cast(dict[str, object], value)


def _rows(value: object, label: str) -> list[object]:
    if not isinstance(value, list):
        raise TypeError(f'Expected a JSON array: {label}')
    return cast(list[object], value)


def _names(value: object, label: str) -> tuple[str, ...]:
    rows = _rows(value, label)
    if any(not isinstance(item, str) for item in rows):
        raise TypeError(f'Expected string names: {label}')
    return cast(tuple[str, ...], tuple(rows))


def _identity(value: object) -> tuple[str, int]:
    parts = _rows(value, 'source material identity')
    if len(parts) != 2 or not isinstance(parts[0], str) or type(parts[1]) is not int:
        raise TypeError(f'Invalid source material identity: {value!r}')
    return parts[0].casefold(), parts[1]


def _source_keywords() -> dict[tuple[str, int], tuple[str, ...]]:
    keywords: dict[str, tuple[str, ...]] = {}
    for text in bpy.data.texts:
        if not text.name.startswith('SOURCE TYPETREE '):
            continue
        row = _record(json.loads(text.as_string()), text.name)
        name = row.get('m_Name')
        if not isinstance(name, str):
            raise TypeError(f'Source TypeTree has no material name: {text.name}')
        values = _names(row.get('m_ValidKeywords'), text.name + '.m_ValidKeywords')
        if name in keywords and keywords[name] != values:
            raise ValueError(f'Ambiguous saved source material keywords: {name}')
        keywords[name] = values
    result: dict[tuple[str, int], tuple[str, ...]] = {}
    for text in bpy.data.texts:
        if not text.name.startswith('SOURCE MATERIAL '):
            continue
        row = _record(json.loads(text.as_string()), text.name)
        source = _record(row.get('object'), text.name + '.object')
        key = _identity([source.get('source'), source.get('path_id')])
        name = source.get('name')
        if not isinstance(name, str) or name not in keywords:
            raise ValueError(f'Missing exact source TypeTree keywords: {text.name}')
        result[key] = keywords[name]
    return result


def _bindings(scene: bpy.types.Scene) -> tuple[object, ...]:
    result: list[object] = []
    for obj in sorted(scene.objects, key=lambda item: item.name):
        if obj.type not in {'MESH', 'CURVES', 'ARMATURE', 'EMPTY'}:
            continue
        geometry = (len(obj.data.vertices), len(obj.data.polygons), len(obj.data.loops)) if obj.type == 'MESH' else (
            (len(obj.data.points), len(obj.data.curves)) if obj.type == 'CURVES' else ())
        shapes = tuple((key.name, float(key.value)) for key in obj.data.shape_keys.key_blocks) if (
            obj.type == 'MESH' and obj.data.shape_keys is not None) else ()
        pose = tuple((bone.name, tuple(value for row in bone.matrix_basis for value in row))
                     for bone in obj.pose.bones) if obj.type == 'ARMATURE' else ()
        modifiers = tuple((modifier.name, modifier.type,
                           getattr(modifier, 'object', None).name if getattr(modifier, 'object', None) else None,
                           getattr(modifier, 'use_vertex_groups', None)) for modifier in obj.modifiers)
        result.append((obj.name, obj.type, obj.data.as_pointer() if obj.data else None, geometry, shapes,
                       obj.parent.name if obj.parent else None, obj.parent_type, obj.parent_bone,
                       tuple(value for row in obj.matrix_world for value in row),
                       tuple(value for row in obj.matrix_parent_inverse for value in row),
                       tuple(group.name for group in obj.vertex_groups) if obj.type == 'MESH' else (),
                       modifiers, pose))
    return tuple(result)


def _set_potential(owner: bpy.types.Object, value: bool, scene: bpy.types.Scene) -> None:
    owner[PROPERTY] = value
    owner.update_tag()
    scene.frame_set(scene.frame_current)
    bpy.context.view_layer.update()


def _check_bindings(scene: bpy.types.Scene, original: tuple[object, ...]) -> None:
    if _bindings(scene) != original:
        raise AssertionError('Effect switching changed geometry counts, data, transforms, shape state or rig binding')


def _materials(scene: bpy.types.Scene) -> tuple[bpy.types.Material, ...]:
    return tuple({slot.material.as_pointer(): slot.material for obj in scene.objects
                  if obj.type in {'MESH', 'CURVES'} and not obj.hide_render
                  for slot in obj.material_slots if slot.material is not None}.values())


def _frame_camera(scene: bpy.types.Scene, view: Literal['front', 'rear', 'side']) -> dict[str, object]:
    if scene.camera is None:
        raise ValueError('The real export has no camera')
    objects = tuple(obj for obj in scene.objects if obj.type == 'MESH' and not obj.hide_render
                    and 'source_renderer_identity' in obj and len(obj.data.vertices))
    if not objects:
        raise ValueError('No visible source renderer bounds for the comparison view')
    corners = tuple(obj.matrix_world @ Vector(corner) for obj in objects for corner in obj.bound_box)
    low = Vector(tuple(min(point[axis] for point in corners) for axis in range(3)))
    high = Vector(tuple(max(point[axis] for point in corners) for axis in range(3)))
    height = high.z - low.z
    if not math.isfinite(height) or height <= 0.:
        raise ValueError(f'Invalid source renderer height: {height}')
    center = (low + high) * .5
    center.z = low.z + height * (.5 if view == 'side' else .65)
    camera = scene.camera
    direction = {'front': (0., -1., 0.), 'rear': (0., 1., 0.), 'side': (1., 0., 0.)}[view]
    camera.location = center + Vector(direction) * height * 3.
    camera.rotation_euler = (center - camera.location).to_track_quat('-Z', 'Y').to_euler()
    camera.data.type = 'ORTHO'
    width = high.y - low.y if view == 'side' else high.x - low.x
    camera.data.ortho_scale = max(height * (1.1 if view == 'side' else .78), width * 1.1 * 640. / 512.)
    framing = ' full body' if view == 'side' else ' upper body'
    return {'view': view + framing + '; exported Blender -Y convention',
            'camera': camera.name, 'orthographic_scale': camera.data.ortho_scale,
            'source_bounds': [list(low), list(high)]}


def _render(scene: bpy.types.Scene, path: Path) -> np.ndarray:
    scene.render.filepath = str(path)
    bpy.ops.render.render(write_still=True, scene=scene.name)
    image = bpy.data.images.load(str(path), check_existing=False)
    values = np.empty(len(image.pixels), dtype=np.float32)
    image.pixels.foreach_get(values)
    bpy.data.images.remove(image)
    if not np.isfinite(values).all():
        raise AssertionError(f'Non-finite rendered pixels: {path}')
    return values.reshape((-1, 4))[:, :3]


def _difference(first: np.ndarray, second: np.ndarray) -> dict[str, object]:
    delta = np.abs(first - second)
    return {'max_rgb_delta': float(delta.max()), 'mean_rgb_delta': float(delta.mean()),
            'changed_rgb_components': int(np.count_nonzero(delta))}


def main(arguments: list[str]) -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--blend', type=Path, required=True)
    parser.add_argument('--output', type=Path, required=True)
    parser.add_argument('--view', choices=('front', 'rear', 'side'), required=True)
    args = parser.parse_args(arguments)
    source = args.blend.resolve(strict=True)
    source_hash = hashlib.sha256(source.read_bytes()).hexdigest()
    args.output.mkdir(parents=True, exist_ok=False)
    bpy.ops.wm.open_mainfile(filepath=str(source), use_scripts=False, load_ui=False)
    scene = bpy.context.scene
    bpy.context.view_layer.update()
    original_bindings = _bindings(scene)
    missing: list[dict[str, object]] = []
    untested: list[str] = []
    owners = tuple(obj for obj in scene.objects if 'endfield_potential_build_report' in obj)
    if len(owners) > 1:
        raise ValueError('Multiple saved potential receipts exist in one character export')
    owner = owners[0] if owners else None
    potential: dict[str, object] = {'status': 'receipt_absent', 'effective_objects': []}
    controlled: tuple[str, ...] = ()
    original_potential = False
    if owner is not None:
        receipt = _record(json.loads(owner['endfield_potential_build_report']), 'potential receipt')
        potential = {'verification': verify_potential_appearance(receipt, scene),
                     'status': receipt['status'], 'pending': receipt.get('pending', [])}
        if potential['pending']:
            missing.append({'route': 'potential', 'reason': 'Saved source receipt retains pending items',
                            'pending': potential['pending']})
        applied = _record(receipt.get('applied'), 'potential.applied')
        potential['route'] = {'owner': owner.name, 'property': PROPERTY,
                              'overlay_identities': applied.get('overlay_identities'),
                              'display_effects': applied.get('display_effects')}
        names = list(_names(applied.get('overlay_objects'), 'potential overlay objects'))
        for value in _rows(applied.get('display_effects'), 'potential display effects'):
            names.extend(_names(_record(value, 'display effect').get('objects', []), 'display objects'))
        controlled = tuple(name for name in names if scene.objects[name].type in {'MESH', 'CURVES', 'LIGHT'}
                           and (scene.objects[name].type != 'MESH' or len(scene.objects[name].data.polygons)))
        potential['effective_objects'] = list(controlled)
        original_potential = bool(owner[PROPERTY])
        _set_potential(owner, True, scene)
    materials = _materials(scene)
    keywords = _source_keywords()
    sockets: dict[int, bpy.types.NodeSocket] = {}
    routes: list[dict[str, object]] = []
    for material in materials:
        identities = tuple(_identity(value) for value in _rows(
            json.loads(material.get('source_material_stack', '[]')), material.name + '.source_material_stack'))
        if any(key not in keywords for key in identities):
            raise ValueError(f'Material stack has no exact saved source keyword evidence: {material.name}')
        selected = tuple(key for key in identities if '_CHARACTER_VFX_SPECIAL' in keywords[key])
        controls = clothing_effect_controls(material)
        if controls and not selected:
            raise AssertionError(f'Clothing controls have no source-selected special VFX: {material.name}')
        dependencies = shader_dependencies(material)
        for socket in controls:
            if (socket.id_data.as_pointer(), socket.node.as_pointer()) not in dependencies.nodes:
                raise AssertionError(f'Inactive clothing control exposed: {material.name}/{socket.node.name}')
            sockets[socket.as_pointer()] = socket
        if selected and not controls:
            missing.append({'route': 'clothing', 'material': material.name,
                            'source_identities': list(selected), 'reason': 'Source keyword has no live strength control'})
        if selected or controls:
            routes.append({'material': material.name, 'source_identities': list(selected),
                           'controls': [{'tree': socket.id_data.name, 'node': socket.node.name,
                                         'path': socket.path_from_id('default_value'),
                                         'saved_strength': float(socket.default_value)} for socket in controls]})
    uniforms = verify_shader_uniforms(materials, tuple(scene.objects), scene)
    original_strengths = tuple((socket, float(socket.default_value)) for socket in sockets.values())
    renders: dict[str, object] = {}
    try:
        if owner is not None:
            _set_potential(owner, False, scene)
        camera = _frame_camera(scene, args.view)
        scene.render.engine = 'BLENDER_EEVEE_NEXT'
        scene.render.resolution_x, scene.render.resolution_y = 512, 640
        scene.render.resolution_percentage = 100
        scene.eevee.taa_render_samples = 32
        scene.render.image_settings.file_format = 'PNG'
        base = _render(scene, args.output / 'base.png')
        _check_bindings(scene, original_bindings)
        if owner is not None and controlled:
            _set_potential(owner, True, scene)
            maximum = _render(scene, args.output / 'maximum.png')
            _check_bindings(scene, original_bindings)
            difference = _difference(base, maximum)
            renders['potential'] = difference
            if difference['changed_rgb_components'] == 0:
                untested.append('Potential visibility verified; no pixel contribution in the fixed ' + args.view + ' view')
        else:
            untested.append('No effective saved potential objects; maximum render not applicable')
        if sockets:
            for socket in sockets.values():
                socket.default_value = 0.
            bpy.context.view_layer.update()
            disabled = _render(scene, args.output / 'special_off.png')
            _check_bindings(scene, original_bindings)
            for socket in sockets.values():
                socket.default_value = 1.
            bpy.context.view_layer.update()
            enabled = _render(scene, args.output / 'special_on.png')
            _check_bindings(scene, original_bindings)
            difference = _difference(disabled, enabled)
            renders['clothing'] = difference
            if difference['changed_rgb_components'] == 0:
                untested.append('Live clothing controls exist; no pixel contribution in the fixed ' + args.view + ' view')
        else:
            untested.append('No source-selected live clothing effect; on/off render not applicable')
        _check_bindings(scene, original_bindings)
    finally:
        for socket, value in original_strengths:
            socket.default_value = value
        if owner is not None:
            _set_potential(owner, original_potential, scene)
    _check_bindings(scene, original_bindings)
    if hashlib.sha256(source.read_bytes()).hexdigest() != source_hash:
        raise AssertionError('Regression changed the saved delivery file')
    result = {'status': 'partial' if missing or any('no pixel contribution' in item for item in untested) else 'passed',
              'source': str(source), 'source_sha256': source_hash, 'frame': scene.frame_current,
              'engine': scene.render.engine, 'camera': camera, 'routes': routes,
              'unique_clothing_controls': len(sockets), 'potential': potential,
              'renders': renders, 'missing': missing, 'untested': untested, 'uniforms': uniforms,
              'no_fabricated_clothing_controls': True, 'geometry_counts_and_bindings_unchanged': True,
              'delivery_unchanged': True, 'scripts_disabled': True}
    (args.output / 'receipt.json').write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding='utf-8')
    print('OPTIONAL_EFFECTS_CROSS_REGRESSION', result['status'], str(args.output / 'receipt.json'), flush=True)


if __name__ == '__main__':
    main(sys.argv[sys.argv.index('--') + 1:])
