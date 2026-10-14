"""Restore validated expression bake outputs from compact runtime caches."""
from __future__ import annotations

from array import array
import copy
import hashlib
import json
import math
from pathlib import Path
import sys
import zlib

import bpy

from character_expression_bake_cache import (CachedController, CachedExpressionBake, CachedShapeKey,
    expression_cache_key, read_expression_cache, write_expression_cache)


def _record(value: object, context: str) -> dict[str, object]:
    if not isinstance(value, dict) or any(not isinstance(key, str) for key in value):
        raise TypeError(f'Expected object: {context}')
    return value


def _text(value: object, context: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise TypeError(f'Expected nonempty string: {context}')
    return value


def _matrix_values(matrix: object) -> list[list[float]]:
    return [[float(value) for value in row] for row in matrix]


def _driver_rows(data: object) -> list[dict[str, object]]:
    animation = data.animation_data
    if animation is None:
        return []
    rows: list[dict[str, object]] = []
    for curve in animation.drivers:
        driver = curve.driver
        variables = []
        for variable in driver.variables:
            targets = []
            for target in variable.targets:
                targets.append({
                    'id_name': target.id.name_full if target.id else None,
                    'data_path': target.data_path,
                    'bone_target': target.bone_target,
                    'transform_type': target.transform_type,
                    'transform_space': target.transform_space,
                })
            variables.append({'name': variable.name, 'type': variable.type, 'targets': targets})
        rows.append({'data_path': curve.data_path, 'array_index': curve.array_index,
                     'driver_type': driver.type, 'expression': driver.expression,
                     'variables': variables})
    return rows


def _expression_scene_state(armature: bpy.types.Object, meshes: tuple[bpy.types.Object, ...]) -> dict[str, object]:
    from character_authoring_validation import shape_signatures
    from audit_character_materials import source_geometry_without_shapes

    mesh_names = tuple(obj.name for obj in meshes)
    mesh_state = []
    for obj in meshes:
        keys = obj.data.shape_keys
        shape_rows = []
        if keys is not None:
            for key in keys.key_blocks:
                shape_rows.append({
                    'name': key.name,
                    'relative_key': key.relative_key.name,
                    'value': float(key.value),
                    'slider_min': float(key.slider_min),
                    'slider_max': float(key.slider_max),
                    'vertex_group': key.vertex_group,
                    'interpolation': key.interpolation,
                    'mute': bool(key.mute),
                })
        mesh_state.append({
            'object_name': obj.name,
            'mesh_data_name': obj.data.name,
            'mesh_data_users': obj.data.users,
            'vertex_count': len(obj.data.vertices),
            'matrix_world': _matrix_values(obj.matrix_world),
            'hide_render': bool(obj.hide_render),
            'hide_get': bool(obj.hide_get()),
            'armature_modifiers': [
                {
                    'name': modifier.name,
                    'object_name': modifier.object.name_full if modifier.object else None,
                    'show_viewport': bool(modifier.show_viewport),
                    'show_render': bool(modifier.show_render),
                    'show_in_editmode': bool(modifier.show_in_editmode),
                    'show_on_cage': bool(modifier.show_on_cage),
                    'use_deform_preserve_volume': bool(modifier.use_deform_preserve_volume),
                    'use_vertex_groups': bool(modifier.use_vertex_groups),
                    'use_bone_envelopes': bool(modifier.use_bone_envelopes),
                    'vertex_group': modifier.vertex_group,
                    'invert_vertex_group': bool(modifier.invert_vertex_group),
                    'use_multi_modifier': bool(modifier.use_multi_modifier),
                }
                for modifier in obj.modifiers if modifier.type == 'ARMATURE'
            ],
            'shape_keys_relative': bool(keys.use_relative) if keys is not None else None,
            'shapes': shape_rows,
            'shape_key_drivers': _driver_rows(keys) if keys is not None else [],
        })
    controller_rows = []
    expression_formats = frozenset(('EndfieldBasicExpressionControls/1',
                                    'EndfieldIndependentExpressionControls/1'))
    for obj in sorted(bpy.data.objects, key=lambda item: item.name):
        controls_format = obj.get('expression_controls_format')
        if controls_format not in expression_formats:
            continue
        controller_rows.append({
            'object_name': obj.name,
            'controls_format': controls_format,
            'properties': [(name, float(obj[name])) for name in obj.keys()
                           if name != 'expression_controls_format'],
            'matrix_world': _matrix_values(obj.matrix_world),
            'display_type': obj.empty_display_type,
            'display_size': float(obj.empty_display_size),
        })
    return {
        'geometry_without_shapes': source_geometry_without_shapes(mesh_names),
        'shape_signatures': shape_signatures(mesh_names),
        'meshes': mesh_state,
        'armature': {
            'object_name': armature.name,
            'pose_position': armature.data.pose_position,
            'matrix_world': _matrix_values(armature.matrix_world),
            'bones': [
                {
                    'name': bone.name,
                    'parent': bone.parent.name if bone.parent else None,
                    'matrix_local': _matrix_values(bone.matrix_local),
                    'use_deform': bool(bone.use_deform),
                    'head_radius': float(bone.head_radius),
                    'tail_radius': float(bone.tail_radius),
                    'envelope_distance': float(bone.envelope_distance),
                    'envelope_weight': float(bone.envelope_weight),
                }
                for bone in sorted(armature.data.bones, key=lambda item: item.name)
            ],
            'pose_bones': [{'name': bone.name, 'matrix_basis': _matrix_values(bone.matrix_basis)}
                           for bone in armature.pose.bones],
            'drivers': _driver_rows(armature),
        },
        'expression_controllers': controller_rows,
    }


def _canonical_digest(value: object) -> str:
    payload = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(',', ':'),
                         allow_nan=False).encode('utf-8')
    return hashlib.sha256(payload).hexdigest()


def _read_source_document(path: Path, context: str) -> dict[str, object]:
    resolved = path.resolve(strict=True)
    return _record(json.loads(resolved.read_text(encoding='utf-8-sig')), context)


def _semantic_table_digest(path: Path, context: str) -> str:
    table = copy.deepcopy(_read_source_document(path, context))
    provenance = _record(table.get('provenance'), f'{context}.provenance')
    provenance.pop('source_audit_sha256', None)
    return _canonical_digest(table)


def _semantic_manifest_digest(path: Path) -> str:
    manifest = copy.deepcopy(_read_source_document(path, 'expression table bake manifest'))
    manifest.pop('source_document_path', None)
    return _canonical_digest(manifest)


def _semantic_audit_digest(path: Path) -> str:
    audit = _read_source_document(path, 'source audit')
    animators = audit.get('animators')
    transforms = audit.get('transforms')
    if not isinstance(animators, list) or not isinstance(transforms, list):
        raise TypeError('Expression cache source audit requires animator and transform arrays')
    avatar_identities: set[tuple[str, int]] = set()
    for index, value in enumerate(animators):
        animator = _record(value, f'source audit.animators[{index}]')
        avatar = animator.get('avatar')
        if not isinstance(avatar, dict) or avatar.get('resolved') is None:
            continue
        resolved = _record(avatar.get('resolved'), f'source audit.animators[{index}].avatar.resolved')
        if resolved.get('type') == 'Avatar':
            source = _text(resolved.get('source'), 'resolved Avatar CAB').casefold()
            path_id = resolved.get('path_id')
            if type(path_id) is not int:
                raise TypeError('Expression cache source audit Avatar PathID must be an integer')
            avatar_identities.add((source, path_id))
    semantic_transforms = []
    for index, value in enumerate(transforms):
        row = _record(value, f'source audit.transforms[{index}]')
        raw_path = _text(row.get('path'), f'source audit.transforms[{index}].path')
        if '/' not in raw_path:
            continue
        semantic_transforms.append({
            'path': raw_path.split('/', maxsplit=1)[1],
            'position': row.get('position'),
            'rotation': row.get('rotation'),
            'scale': row.get('scale'),
        })
    semantic_transforms.sort(key=lambda row: (str(row['path']), _canonical_digest(row)))
    return _canonical_digest({
        'format': audit.get('format'),
        'avatar_identities': sorted(avatar_identities),
        'transforms': semantic_transforms,
    })


def _basic_expression_source_rows(table_path: Path, audit_path: Path) -> list[dict[str, str]]:
    return [
        {'role': 'expression_table', 'semantic_sha256': _semantic_table_digest(table_path, 'expression source table')},
        {'role': 'source_audit_semantics', 'semantic_sha256': _semantic_audit_digest(audit_path)},
    ]


def _independent_expression_source_rows(manifest_path: Path, table_path: Path,
                                        audit_path: Path) -> list[dict[str, str]]:
    return [
        {'role': 'expression_manifest', 'semantic_sha256': _semantic_manifest_digest(manifest_path)},
        {'role': 'expression_table', 'semantic_sha256': _semantic_table_digest(table_path, 'independent expression source table')},
        {'role': 'source_audit_semantics', 'semantic_sha256': _semantic_audit_digest(audit_path)},
    ]


def _expression_code_rows() -> list[dict[str, str]]:
    root = Path(__file__).resolve().parent
    names = (
        'character_authoring.py',
        'character_expression_bake_adapter.py',
        'character_expression_bake_cache.py',
        'basic_expression_shape_baker.py',
        'independent_expression_table_baker.py',
        'character_expression_shape_keys.py',
        'character_authoring_validation.py',
        'character_authoring_identity.py',
        'character_authoring_unweighted_leaf.py',
        'character_expression_material_controls.py',
        'audit_character_materials.py',
    )
    rows = []
    for name in names:
        path = root / name
        with path.open('rb') as stream:
            rows.append({'module': name, 'sha256': hashlib.file_digest(stream, 'sha256').hexdigest()})
    return rows


def _expression_cache_key(stage: str, character_id: str, armature: bpy.types.Object,
                          meshes: tuple[bpy.types.Object, ...], source_rows: list[dict[str, str]],
                          controller_name: str, tolerance: float) -> str:
    return expression_cache_key({
        'format': 'EndfieldExpressionBakeInputs/1',
        'stage': stage,
        'character_id': character_id,
        'rig_name': armature.name,
        'controller_name': controller_name,
        'tolerance': tolerance,
        'blender_version': bpy.app.version_string,
        'coordinate_encoding': 'IEEE-754-f32-little-endian',
        'sources': source_rows,
        'code': _expression_code_rows(),
        'scene': _expression_scene_state(armature, meshes),
    })


def _new_shape_names(meshes: tuple[bpy.types.Object, ...]) -> dict[str, frozenset[str]]:
    return {obj.name: frozenset(key.name for key in obj.data.shape_keys.key_blocks)
            if obj.data.shape_keys else frozenset() for obj in meshes}


def _shape_driver_property(key: bpy.types.ShapeKey, controller: bpy.types.Object) -> str | None:
    keys = key.id_data
    if keys.animation_data is None:
        return None
    path = key.path_from_id('value')
    curves = tuple(curve for curve in keys.animation_data.drivers if curve.data_path == path)
    if not curves:
        return None
    if len(curves) != 1:
        raise ValueError(f'Expression cache found multiple drivers for one shape key: {key.name}, count={len(curves)}')
    driver = curves[0].driver
    if driver.type != 'SCRIPTED' or driver.expression != 'v' or len(driver.variables) != 1:
        raise ValueError(f'Expression cache found an unsupported shape-key driver: {key.name}')
    variable = driver.variables[0]
    target = variable.targets[0]
    if variable.name != 'v' or variable.type != 'SINGLE_PROP' or target.id != controller:
        raise ValueError(f'Expression cache driver does not target its stage controller: {key.name}')
    if not target.data_path:
        raise ValueError(f'Expression cache driver has an empty controller property: {key.name}')
    return target.data_path


def _capture_expression_bake(receipt: dict[str, object], meshes: tuple[bpy.types.Object, ...],
                             previous_names: dict[str, frozenset[str]]) -> CachedExpressionBake:
    controller_row = _record(receipt.get('controller'), 'expression cache receipt.controller')
    controller_name = _text(controller_row.get('object_name'), 'expression cache controller name')
    controller = bpy.data.objects.get(controller_name)
    if controller is None:
        raise ValueError(f'Expression cache controller disappeared after baking: {controller_name}')
    controls_format = controller.get('expression_controls_format')
    if not isinstance(controls_format, str):
        raise ValueError(f'Expression cache controller has no controls format: {controller_name}')
    shape_keys: list[CachedShapeKey] = []
    for obj in meshes:
        keys = obj.data.shape_keys
        if keys is None:
            continue
        for key in keys.key_blocks:
            if key.name in previous_names[obj.name]:
                continue
            coordinates = array('f', [0.0]) * (len(key.data) * 3)
            key.data.foreach_get('co', coordinates)
            if coordinates.itemsize != 4:
                raise ValueError(f'Expression cache requires four-byte Blender coordinates: {coordinates.itemsize}')
            if sys.byteorder != 'little':
                coordinates.byteswap()
            raw_coordinates = coordinates.tobytes()
            shape_keys.append(CachedShapeKey(
                obj.name, key.name, key.relative_key.name, len(key.data), zlib.compress(raw_coordinates, 3),
                hashlib.sha256(raw_coordinates).hexdigest(), _shape_driver_property(key, controller),
                float(key.value), float(key.slider_min), float(key.slider_max), key.vertex_group,
                key.interpolation, bool(key.mute)))
    properties = []
    for name in controller.keys():
        if name == 'expression_controls_format':
            continue
        value = controller[name]
        if type(value) not in (int, float):
            raise TypeError(f'Expression cache controller property is not numeric: {controller_name}/{name}')
        properties.append((name, float(value)))
    cached_controller = CachedController(controller.name, controls_format, tuple(properties),
                                         controller.empty_display_type, float(controller.empty_display_size))
    return CachedExpressionBake(copy.deepcopy(receipt), tuple(shape_keys), (cached_controller,))


def _remove_shape_key_driver(key: bpy.types.ShapeKey) -> None:
    keys = key.id_data
    if keys.animation_data is None:
        return
    path = key.path_from_id('value')
    if any(curve.data_path == path for curve in keys.animation_data.drivers):
        key.driver_remove('value')


def _restore_mesh_data(meshes: tuple[bpy.types.Object, ...], originals: dict[str, bpy.types.Mesh]) -> None:
    for obj in meshes:
        original = originals.get(obj.name)
        if original is None or obj.data == original:
            continue
        copied = obj.data
        obj.data = original
        if copied.users == 0:
            bpy.data.meshes.remove(copied)


def _isolate_shared_meshes(meshes: tuple[bpy.types.Object, ...]) -> tuple[dict[str, bpy.types.Mesh], list[dict[str, object]]]:
    originals: dict[str, bpy.types.Mesh] = {}
    rows: list[dict[str, object]] = []
    try:
        for obj in meshes:
            original = obj.data
            if original.users <= 1:
                continue
            originals[obj.name] = original
            rows.append({'object_name': obj.name, 'original_mesh_data': original.name,
                         'original_user_count': original.users})
            obj.data = original.copy()
            if obj.data.users != 1:
                raise ValueError(f'Cached expression mesh data is still shared after isolation: {obj.name}, users={obj.data.users}')
    except Exception:
        _restore_mesh_data(meshes, originals)
        raise
    return originals, rows


def _attach_cached_driver(key: bpy.types.ShapeKey, controller: bpy.types.Object, property_path: str) -> None:
    if property_path.startswith('["') and property_path.endswith('"]'):
        property_name = property_path[2:-2]
        if property_name not in controller:
            raise ValueError(f'Cached expression controller property is missing: {controller.name}/{property_name}')
    else:
        raise ValueError(f'Cached expression driver property path is unsupported: {property_path}')
    driver = key.driver_add('value').driver
    driver.type = 'SCRIPTED'
    driver.expression = 'v'
    variable = driver.variables.new()
    variable.name = 'v'
    variable.type = 'SINGLE_PROP'
    variable.targets[0].id_type = 'OBJECT'
    variable.targets[0].id = controller
    variable.targets[0].data_path = property_path


def _restore_expression_bake(entry: CachedExpressionBake, meshes: tuple[bpy.types.Object, ...],
                             originals: dict[str, bpy.types.Mesh]) -> dict[str, object]:
    mesh_by_name = {obj.name: obj for obj in meshes}
    expected_names = {name: {key.name for key in obj.data.shape_keys.key_blocks} if obj.data.shape_keys else set()
                      for name, obj in mesh_by_name.items()}
    for shape in entry.shape_keys:
        obj = mesh_by_name.get(shape.object_name)
        if obj is None:
            raise ValueError(f'Cached expression mesh is absent from the selected rig: {shape.object_name}')
        if len(obj.data.vertices) != shape.vertex_count:
            raise ValueError(f'Cached expression vertex count differs: {shape.object_name}, cached={shape.vertex_count}, current={len(obj.data.vertices)}')
        if shape.shape_key_name in expected_names[obj.name]:
            raise FileExistsError(f'Cached expression shape key already exists and will not be replaced: {obj.name}/{shape.shape_key_name}')
        expected_names[obj.name].add(shape.shape_key_name)
    controller_by_name: dict[str, bpy.types.Object] = {}
    for controller in entry.controllers:
        if bpy.data.objects.get(controller.object_name) is not None:
            raise FileExistsError(f'Cached expression controller already exists and will not be replaced: {controller.object_name}')
    for shape in entry.shape_keys:
        if shape.relative_key_name not in expected_names[shape.object_name]:
            raise ValueError(f'Cached expression relative shape key is absent: {shape.object_name}/{shape.relative_key_name}')
    created_keys: list[tuple[bpy.types.Object, bpy.types.ShapeKey]] = []
    created_controllers: list[bpy.types.Object] = []
    try:
        for cached in entry.controllers:
            controller = bpy.data.objects.new(cached.object_name, None)
            bpy.context.collection.objects.link(controller)
            controller.empty_display_type = cached.display_type
            controller.empty_display_size = cached.display_size
            controller['expression_controls_format'] = cached.controls_format
            for name, value in cached.properties:
                controller[name] = value
            controller_by_name[controller.name] = controller
            created_controllers.append(controller)
            if controller.name != cached.object_name:
                raise FileExistsError(f'Cached expression controller name was remapped: expected={cached.object_name}, actual={controller.name}')
        for cached in entry.shape_keys:
            obj = mesh_by_name[cached.object_name]
            if obj.data.shape_keys is None:
                if cached.shape_key_name != 'Basis' or cached.relative_key_name != 'Basis':
                    raise ValueError(f'Cached expression lacks its required Basis key: {obj.name}/{cached.shape_key_name}')
                key = obj.shape_key_add(name='Basis', from_mix=False)
            else:
                key = obj.shape_key_add(name=cached.shape_key_name, from_mix=False)
            created_keys.append((obj, key))
            if key.name != cached.shape_key_name:
                raise FileExistsError(f'Cached expression shape key name was remapped: expected={cached.shape_key_name}, actual={key.name}')
            keys = obj.data.shape_keys
            relative_key = keys.key_blocks.get(cached.relative_key_name)
            if relative_key is None:
                raise ValueError(f'Cached expression relative key disappeared during restore: {obj.name}/{cached.relative_key_name}')
            key.relative_key = relative_key
            key.slider_min = cached.slider_min
            key.slider_max = cached.slider_max
            key.vertex_group = cached.vertex_group
            key.interpolation = cached.interpolation
            key.mute = cached.mute
            key.value = cached.value
            raw_coordinates = zlib.decompress(cached.coordinate_block)
            coordinates = array('f')
            coordinates.frombytes(raw_coordinates)
            if sys.byteorder != 'little':
                coordinates.byteswap()
            if len(coordinates) != cached.vertex_count * 3:
                raise ValueError(f'Cached expression coordinate count differs: {obj.name}/{key.name}')
            key.data.foreach_set('co', coordinates)
            if cached.driver_property is not None:
                if len(controller_by_name) != 1:
                    raise ValueError(f'Cached expression driver requires exactly one stage controller: {key.name}')
                _attach_cached_driver(key, next(iter(controller_by_name.values())), cached.driver_property)
                key.value = 0.0
        bpy.context.view_layer.update()
    except Exception:
        for obj, key in reversed(created_keys):
            _remove_shape_key_driver(key)
            obj.shape_key_remove(key)
        for controller in created_controllers:
            if bpy.data.objects.get(controller.name) is controller:
                bpy.data.objects.remove(controller, do_unlink=True)
        _restore_mesh_data(meshes, originals)
        bpy.context.view_layer.update()
        raise
    return copy.deepcopy(entry.receipt)


def _restore_basic_expression_bake(entry: CachedExpressionBake,
                                   meshes: tuple[bpy.types.Object, ...]) -> dict[str, object]:
    originals, copied_rows = _isolate_shared_meshes(meshes)
    try:
        cached_receipt = _record(entry.receipt.get('selection'), 'cached basic receipt.selection')
        expected_copies = cached_receipt.get('isolated_shared_mesh_data')
        if expected_copies != copied_rows:
            raise ValueError(f'Cached mesh isolation differs from the recorded source state: cached={expected_copies}, current={copied_rows}')
        return _restore_expression_bake(entry, meshes, originals)
    except Exception:
        _restore_mesh_data(meshes, originals)
        raise


def _restore_independent_expression_bake(entry: CachedExpressionBake,
                                          meshes: tuple[bpy.types.Object, ...]) -> dict[str, object]:
    return _restore_expression_bake(entry, meshes, {})


def _rebase_basic_receipt(receipt: dict[str, object], table_path: Path, audit_path: Path) -> dict[str, object]:
    result = copy.deepcopy(receipt)
    source_table = _record(result.get('source_table'), 'basic receipt.source_table')
    source_audit = _record(result.get('source_audit'), 'basic receipt.source_audit')
    table_payload = table_path.read_bytes()
    audit_payload = audit_path.read_bytes()
    source_table['file'] = table_path.name
    source_table['sha256'] = hashlib.sha256(table_payload).hexdigest()
    source_audit['file'] = audit_path.name
    source_audit['sha256'] = hashlib.sha256(audit_payload).hexdigest()
    return result


def _rebase_independent_receipt(receipt: dict[str, object], table_path: Path,
                                audit_path: Path) -> dict[str, object]:
    result = copy.deepcopy(receipt)
    source_table = _record(result.get('source_document'), 'ear receipt.source_document')
    source_audit = _record(result.get('source_audit'), 'ear receipt.source_audit')
    table_payload = table_path.read_bytes()
    audit_payload = audit_path.read_bytes()
    source_table['file'] = table_path.name
    source_table['bytes'] = len(table_payload)
    source_table['sha256'] = hashlib.sha256(table_payload).hexdigest()
    source_audit['file'] = audit_path.name
    source_audit['sha256'] = hashlib.sha256(audit_payload).hexdigest()
    return result


def _with_cache_status(receipt: dict[str, object], cache_key: str,
                       status: str) -> dict[str, object]:
    if status not in ('miss', 'hit'):
        raise ValueError(f'Unsupported expression cache status: {status}')
    result = copy.deepcopy(receipt)
    result['bake_cache'] = {'status': status, 'key': cache_key}
    return result


def _validated_basic_expression_inputs(character_id: str, source_audit: Path, armature: bpy.types.Object,
                                       table_path: Path, tolerance: float) -> tuple[tuple[bpy.types.Object, ...], list[dict[str, str]]]:
    from basic_expression_shape_baker import (BASIC_EXPRESSION_DEFINITIONS, IDENTITY,
        _decode_source, _matrix_error, _read_json, _selected_rig_meshes)

    if armature.type != 'ARMATURE':
        raise ValueError(f'Explicit facial rig does not exist as an Armature: {armature.name}')
    if (armature.data.pose_position != 'POSE' or armature.animation_data or armature.constraints
            or any(bone.constraints for bone in armature.pose.bones)):
        raise ValueError(f'Basic expression baking requires a static source rig without Action or constraints: {armature.name}')
    basis_errors = {bone.name: _matrix_error(bone.matrix_basis, IDENTITY) for bone in armature.pose.bones}
    nonidentity = {name: value for name, value in basis_errors.items() if value > tolerance}
    if nonidentity:
        raise ValueError(f'Basic expression baking requires the audited identity pose: rig={armature.name}, residuals={list(nonidentity.items())[:5]}')
    table, table_payload, _ = _read_json(table_path, 'expression source table')
    audit, audit_payload, audit_resolved = _read_json(source_audit, 'source audit')
    if audit.get('format') != 'EndfieldCharacterSourceAudit/1':
        raise ValueError(f'Unsupported source audit format: {audit_resolved}')
    decoded = _decode_source(table, audit, table_payload, audit_payload, armature,
                             BASIC_EXPRESSION_DEFINITIONS)
    if decoded.character_id != character_id:
        raise ValueError(f'Basic expression source belongs to a different character: {decoded.character_id}')
    meshes = _selected_rig_meshes(armature)
    return meshes, _basic_expression_source_rows(table_path, source_audit)


def _validated_independent_expression_inputs(character_id: str, source_audit: Path, armature: bpy.types.Object,
                                             manifest_path: Path, tolerance: float) -> tuple[Path, tuple[bpy.types.Object, ...], list[dict[str, str]]]:
    import independent_expression_table_baker as independent_baker
    from basic_expression_shape_baker import BasicExpressionDefinition

    manifest_resolved = manifest_path.resolve(strict=True)
    manifest = independent_baker._manifest(manifest_resolved)
    if manifest.character_id != character_id:
        raise ValueError(f'Independent expression table belongs to a different character: {manifest_path}')
    table_path = (manifest_resolved.parent / manifest.source_document_path).resolve(strict=True)
    table, table_payload, _ = independent_baker.basic_baker._read_json(
        table_path, 'independent expression source table')
    audit, audit_payload, audit_resolved = independent_baker.basic_baker._read_json(
        source_audit, 'source audit')
    if audit.get('format') != 'EndfieldCharacterSourceAudit/1':
        raise ValueError(f'Unsupported source audit format: {audit_resolved}')
    independent_baker._verify_table_identity(manifest, table, table_payload, audit_payload)
    independent_baker._verify_control_partition(table, manifest)
    if armature.type != 'ARMATURE' or armature.data.pose_position != 'POSE':
        raise ValueError(f'Explicit independent expression rig is missing or disabled: {armature.name}')
    if armature.animation_data and armature.animation_data.action:
        raise ValueError(f'Independent expression endpoints must be baked before body Actions are attached: {armature.name}')
    if armature.constraints or any(bone.constraints for bone in armature.pose.bones):
        raise ValueError(f'Independent expression rig has constraints: {armature.name}')
    independent_baker._identity_pose(armature, tolerance)
    definitions = tuple(BasicExpressionDefinition(name, name, (), 'ear_source_table', ())
                        for name in manifest.endpoint_control_names)
    decoded = independent_baker._decode_source(table, audit, table_payload, audit_payload,
                                                armature, definitions)
    if decoded.character_id != manifest.character_id:
        raise ValueError('Decoded ear source belongs to a different character')
    avatar = decoded.rig_data.avatar_identity
    avatar_cab = _text(manifest.source_identity.get('avatar_cab'), 'identity.avatar_cab')
    avatar_path_id = _text(manifest.source_identity.get('avatar_path_id'), 'identity.avatar_path_id')
    if avatar[0].casefold() != avatar_cab.casefold() or str(avatar[1]) != avatar_path_id:
        raise ValueError('Decoded rig Avatar identity differs from the ear-table manifest')
    meshes = independent_baker._selected_rig_meshes(armature)
    for obj in bpy.data.objects:
        if obj.get('expression_controls_format') != 'EndfieldBasicExpressionControls/1':
            continue
        values = {key: float(value) for key, value in obj.items() if key != 'expression_controls_format'}
        if any(abs(value) > tolerance for value in values.values()):
            raise ValueError(f'Existing basic facial controls must be neutral before ear baking: controller={obj.name}, values={values}')
    endpoint_names = frozenset(manifest.endpoint_control_names)
    for obj in meshes:
        if obj.data.users != 1:
            raise ValueError(f'Independent expression baking requires single-user mesh data: {obj.name}, users={obj.data.users}')
        active_skin = tuple(modifier for modifier in obj.modifiers if modifier.show_viewport and modifier.type == 'ARMATURE')
        if len(active_skin) != 1 or active_skin[0].object != armature:
            raise ValueError(f'Independent expression mesh is not bound to exactly the selected rig: {obj.name}')
        keys = obj.data.shape_keys
        if keys and (not keys.use_relative or endpoint_names.intersection(key.name for key in keys.key_blocks)
                     or any(abs(float(key.value)) > tolerance for key in keys.key_blocks)):
            raise ValueError(f'Existing shape keys must be relative, uniquely named, and neutral before independent expression baking: {obj.name}')
    return table_path, meshes, _independent_expression_source_rows(manifest_resolved, table_path,
                                                                    source_audit)


def apply_basic_expression_cache(cache_root: Path, character_id: str, source_audit: Path,
                                  armature: bpy.types.Object, table_path: Path,
                                  controller_name: str, tolerance: float) -> dict[str, object]:
    from basic_expression_shape_baker import apply_basic_expressions

    meshes, source_rows = _validated_basic_expression_inputs(character_id, source_audit,
                                                              armature, table_path, tolerance)
    key = _expression_cache_key('basic_mmd', character_id, armature, meshes,
                                source_rows, controller_name, tolerance)
    cached = read_expression_cache(cache_root, key)
    if cached is None:
        previous_names = _new_shape_names(meshes)
        receipt = apply_basic_expressions(table_path, source_audit, armature.name,
                                          controller_name, tolerance)
        write_expression_cache(cache_root, key, _capture_expression_bake(receipt, meshes, previous_names))
        return _with_cache_status(receipt, key, 'miss')
    restored = _restore_basic_expression_bake(cached, meshes)
    return _with_cache_status(_rebase_basic_receipt(restored, table_path, source_audit),
                              key, 'hit')


def apply_independent_expression_cache(cache_root: Path, character_id: str, source_audit: Path,
                                        armature: bpy.types.Object, manifest_path: Path,
                                        controller_name: str,
                                        tolerance: float) -> dict[str, object]:
    from independent_expression_table_baker import bake_expression_table_endpoints

    table_path, meshes, source_rows = _validated_independent_expression_inputs(
        character_id, source_audit, armature, manifest_path, tolerance)
    key = _expression_cache_key('independent_expression_table', character_id, armature, meshes,
                                source_rows, controller_name, tolerance)
    cached = read_expression_cache(cache_root, key)
    if cached is None:
        previous_names = _new_shape_names(meshes)
        receipt = bake_expression_table_endpoints(manifest_path, source_audit,
                                                   armature.name, controller_name, tolerance)
        write_expression_cache(cache_root, key, _capture_expression_bake(receipt, meshes, previous_names))
        return _with_cache_status(receipt, key, 'miss')
    restored = _restore_independent_expression_bake(cached, meshes)
    return _with_cache_status(_rebase_independent_receipt(restored, table_path, source_audit),
                              key, 'hit')


