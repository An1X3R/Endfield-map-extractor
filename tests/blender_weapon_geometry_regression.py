"""Exercise static weapon geometry imports and the existing skinned character path."""
from __future__ import annotations

import argparse
import json
import math
from pathlib import Path
import shutil
import sys
import tempfile
from collections.abc import Callable, Sequence

import bpy
import numpy as np
from mathutils import Matrix, Quaternion, Vector

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'src' / 'character_runtime'))

from character_animation_binding import fields, integer, numbers, rows, text
from character_geometry import import_character_geometry, import_static_geometry, read_geometry_payload


def require(condition: bool, message: str) -> None:
    if not condition:
        raise AssertionError(message)


def source_array(spec_value: object, payload: bytes, components: int, dtype_name: str) -> np.ndarray:
    spec = fields(spec_value, 'geometry array')
    if integer(spec['components'], 'array.components') != components:
        raise ValueError(f"Unexpected component count: {spec}")
    if text(spec['dtype'], 'array.dtype') != dtype_name:
        raise ValueError(f"Unexpected geometry dtype: {spec}")
    type_map = {'float32': '<f4', 'int32': '<i4', 'uint32': '<u4'}
    dtype = type_map.get(dtype_name)
    if dtype is None:
        raise ValueError(f"Unsupported geometry dtype: {dtype_name}")
    offset = integer(spec['offset'], 'array.offset')
    count = integer(spec['count'], 'array.count')
    end = offset + count * components * 4
    if offset < 0 or count < 0 or end > len(payload):
        raise ValueError(f"Geometry array is out of payload bounds: {spec}")
    return np.frombuffer(payload, dtype=dtype, count=count * components, offset=offset).reshape(count, components).copy()


def source_node_matrices(
    document: dict[str, object],
) -> tuple[dict[str, Matrix], dict[str, Matrix], dict[str, str | None]]:
    node_rows = tuple(fields(value, 'node') for value in rows(document['nodes'], 'nodes'))
    lookup: dict[str, dict[str, object]] = {}
    locals_by_path: dict[str, Matrix] = {}
    parents: dict[str, str | None] = {}
    for row in node_rows:
        path = text(row['path'], 'node.path')
        if path in lookup:
            raise ValueError(f"Duplicate source node path: {path}")
        lookup[path] = row
        rotation = numbers(row['rotation'], 4, path + '.rotation')
        quaternion = Quaternion((rotation[3], rotation[0], rotation[1], rotation[2]))
        if abs(quaternion.magnitude - 1.0) > 1e-4:
            raise ValueError(f"Source node rotation is not unit length: {path}")
        locals_by_path[path] = Matrix.LocRotScale(
            Vector(numbers(row['position'], 3, path + '.position')),
            quaternion,
            Vector(numbers(row['scale'], 3, path + '.scale')),
        )
        parent_value = row['parent_path']
        parents[path] = None if parent_value is None else text(parent_value, path + '.parent_path')
    worlds: dict[str, Matrix] = {}
    active: set[str] = set()

    def solve(path: str) -> Matrix:
        if path not in lookup or path in active:
            raise ValueError(f"Missing or cyclic source node: {path}")
        if path not in worlds:
            active.add(path)
            parent_path = parents[path]
            worlds[path] = (locals_by_path[path] if parent_path is None
                            else solve(parent_path) @ locals_by_path[path])
            active.remove(path)
        return worlds[path]

    for path in lookup:
        solve(path)
    return locals_by_path, worlds, parents


def clear_scene_objects() -> None:
    for obj in tuple(bpy.data.objects):
        bpy.data.objects.remove(obj, do_unlink=True)
    bpy.context.view_layer.update()


def local_vertices(obj: bpy.types.Object) -> np.ndarray:
    values = np.empty(len(obj.data.vertices) * 3, dtype=np.float32)
    obj.data.vertices.foreach_get('co', values)
    return values.reshape(len(obj.data.vertices), 3)


def world_vertices(obj: bpy.types.Object) -> np.ndarray:
    return np.asarray([tuple(obj.matrix_world @ vertex.co) for vertex in obj.data.vertices], dtype=np.float64)


def validate_uv_layers(obj: bpy.types.Object, row: dict[str, object], payload: bytes, label: str) -> int:
    uv_rows = tuple(fields(value, 'uv') for value in rows(row['uvs'], 'mesh.uvs'))
    require(len(uv_rows) > 0, f"Source mesh has no UV layers: {label}")
    require(len(obj.data.uv_layers) == len(uv_rows), f"UV layer count changed: {label}")
    loop_vertices = np.asarray([loop.vertex_index for loop in obj.data.loops], dtype=np.int64)
    for uv_row in uv_rows:
        channel = integer(uv_row['channel'], 'uv.channel')
        layer = obj.data.uv_layers.get(f'UV{channel}')
        require(layer is not None, f"Imported UV channel is missing: {label}/UV{channel}")
        source = source_array(uv_row['values'], payload, 2, 'float32')
        require(len(source) == len(obj.data.vertices), f"Source UV vertex count differs: {label}/UV{channel}")
        expected = source[loop_vertices]
        actual_values = np.empty(len(layer.data) * 2, dtype=np.float32)
        layer.data.foreach_get('uv', actual_values)
        actual = actual_values.reshape(len(layer.data), 2)
        require(actual.shape == expected.shape, f"UV corner count changed: {label}/UV{channel}")
        require(np.allclose(actual, expected, atol=1e-6, rtol=0.0), f"UV coordinates changed: {label}/UV{channel}")
    return len(uv_rows)


def validate_root_follow(root: bpy.types.Object, meshes: tuple[bpy.types.Object, ...], label: str) -> tuple[float, float]:
    original_root = root.matrix_world.copy()
    before = {obj.name: world_vertices(obj) for obj in meshes}
    delta = Matrix.Translation(Vector((0.73, -0.41, 1.17))) @ Matrix.Rotation(0.37, 4, 'Y')
    try:
        root.matrix_world = delta @ original_root
        bpy.context.view_layer.update()
        follow_errors = []
        for obj in meshes:
            expected = np.asarray([tuple(delta @ Vector((*point, 1.0)))[:3] for point in before[obj.name]], dtype=np.float64)
            actual = world_vertices(obj)
            require(actual.shape == expected.shape, f"Root move changed mesh coordinates: {label}/{obj.name}")
            error = np.linalg.norm(actual - expected, axis=1)
            follow_errors.append(float(error.max()))
            require(np.allclose(actual, expected, atol=1e-5, rtol=0.0), f"Mesh did not follow root transform: {label}/{obj.name}")
    finally:
        root.matrix_world = original_root
        bpy.context.view_layer.update()
    restore_errors = []
    for obj in meshes:
        restored = world_vertices(obj)
        error = np.linalg.norm(restored - before[obj.name], axis=1)
        restore_errors.append(float(error.max()))
        require(np.allclose(restored, before[obj.name], atol=1e-5, rtol=0.0), f"Root restoration changed mesh coordinates: {label}/{obj.name}")
    return max(follow_errors), max(restore_errors)


def validate_intermediate_follow(
    transform: bpy.types.Object,
    descendants: tuple[bpy.types.Object, ...],
    label: str,
) -> tuple[float, float]:
    original_transform = transform.matrix_world.copy()
    before = {obj.name: world_vertices(obj) for obj in descendants}
    delta = Matrix.Translation(Vector((-0.29, 0.61, 0.83))) @ Matrix.Rotation(-0.23, 4, 'Z')
    try:
        transform.matrix_world = delta @ original_transform
        bpy.context.view_layer.update()
        follow_errors = []
        for obj in descendants:
            expected = np.asarray([tuple(delta @ Vector((*point, 1.0)))[:3] for point in before[obj.name]], dtype=np.float64)
            actual = world_vertices(obj)
            require(actual.shape == expected.shape, f"Intermediate move changed mesh coordinates: {label}/{obj.name}")
            error = np.linalg.norm(actual - expected, axis=1)
            follow_errors.append(float(error.max()))
            require(np.allclose(actual, expected, atol=1e-5, rtol=0.0),
                    f"Mesh did not follow intermediate transform: {label}/{obj.name}")
    finally:
        transform.matrix_world = original_transform
        bpy.context.view_layer.update()
    restore_errors = []
    for obj in descendants:
        restored = world_vertices(obj)
        error = np.linalg.norm(restored - before[obj.name], axis=1)
        restore_errors.append(float(error.max()))
        require(np.allclose(restored, before[obj.name], atol=1e-5, rtol=0.0),
                f"Intermediate restoration changed mesh coordinates: {label}/{obj.name}")
    return max(follow_errors), max(restore_errors)


def validate_static_weapon(geometry_path: Path, label: str) -> dict[str, object]:
    document, payload = read_geometry_payload(geometry_path)
    source_meshes = tuple(fields(value, 'mesh') for value in rows(document['meshes'], 'meshes'))
    roots = tuple(fields(value, 'node') for value in rows(document['nodes'], 'nodes')
                  if fields(value, 'node')['parent_path'] is None)
    require(len(roots) == 1, f"Static weapon geometry must have one source root: {label}")
    source_root = roots[0]
    locals_by_path, worlds, parents = source_node_matrices(document)
    source_nodes = tuple(fields(value, 'node') for value in rows(document['nodes'], 'nodes'))
    node_paths = tuple(text(node['path'], 'node.path') for node in source_nodes)
    mesh_paths = tuple(text(mesh['path'], 'mesh.path') for mesh in source_meshes)
    require(len(set(node_paths)) == len(node_paths), f"Source transform paths are not unique: {label}")
    require(set(mesh_paths).issubset(set(node_paths)), f"Source mesh has no transform node: {label}")
    root_matrix = Matrix.Rotation(math.pi / 2, 4, 'X') @ locals_by_path[text(source_root['path'], 'root.path')]

    clear_scene_objects()
    imported = import_static_geometry(geometry_path)
    require(len(imported) == len(source_nodes), f"Imported transform object count changed: {label}")
    transforms = {text(obj.get('source_transform_path'), 'object.source_transform_path'): obj for obj in imported}
    require(len(transforms) == len(imported), f"Imported source transform paths are not unique: {label}")
    require(set(transforms) == set(node_paths), f"Imported transform path set changed: {label}")
    root_path = text(source_root['path'], 'root.path')
    root = transforms[root_path]
    meshes = tuple(obj for obj in imported if obj.type == 'MESH')
    require(root.parent is None, f"Static source root is invalid: {label}")
    require(root.type == ('MESH' if root_path in set(mesh_paths) else 'EMPTY'),
            f"Static source root object type changed: {label}")
    require(len(meshes) == len(source_meshes), f"Imported mesh count changed: {label}")
    require(not any(obj.type == 'ARMATURE' for obj in bpy.data.objects), f"Static import fabricated an armature: {label}")
    for source_node in source_nodes:
        path = text(source_node['path'], 'node.path')
        obj = transforms[path]
        require(obj.name == text(source_node['name'], 'node.name'), f"Imported transform name changed: {label}/{path}")
        parent_path = parents[path]
        expected_parent = None if parent_path is None else transforms[parent_path]
        require(obj.parent == expected_parent, f"Imported transform parent changed: {label}/{path}")
        expected_basis = (root_matrix if parent_path is None else locals_by_path[path])
        require(np.allclose(np.asarray(obj.matrix_basis), np.asarray(expected_basis), atol=1e-6, rtol=0.0),
                f"Imported local transform changed: {label}/{path}")
        expected_world = Matrix.Rotation(math.pi / 2, 4, 'X') @ worlds[path]
        require(np.allclose(np.asarray(obj.matrix_world), np.asarray(expected_world), atol=1e-5, rtol=0.0),
                f"Imported world transform changed: {label}/{path}")

    by_path = {text(obj.get('source_geometry_path'), 'mesh.source_geometry_path'): obj for obj in meshes}
    require(len(by_path) == len(meshes), f"Imported source mesh paths are not unique: {label}")
    maximum_coordinate_error = 0.0
    uv_layer_count = 0
    for source_mesh in source_meshes:
        mesh_path = text(source_mesh['path'], 'mesh.path')
        obj = by_path.get(mesh_path)
        require(obj is not None, f"Imported source mesh is missing: {label}/{mesh_path}")
        count = integer(source_mesh['vertex_count'], 'mesh.vertex_count')
        expected_local = source_array(source_mesh['positions'], payload, 3, 'float32')
        actual_local = local_vertices(obj)
        require(len(actual_local) == count, f"Imported vertex count changed: {label}/{mesh_path}")
        require(np.allclose(actual_local, expected_local, atol=1e-7, rtol=0.0),
                f"Imported local coordinates changed: {label}/{mesh_path}")
        local_error = np.linalg.norm(actual_local.astype(np.float64) - expected_local, axis=1)
        maximum_coordinate_error = max(maximum_coordinate_error, float(local_error.max()))
        uv_layer_count += validate_uv_layers(obj, source_mesh, payload, label + '/' + mesh_path)
        expected_world = np.asarray([
            tuple(root_matrix @ worlds[mesh_path] @ Vector((*position, 1.0)))[:3]
            for position in expected_local
        ], dtype=np.float64)
        actual_world = world_vertices(obj)
        require(actual_world.shape == expected_world.shape, f"Imported world vertex count changed: {label}/{mesh_path}")
        require(np.allclose(actual_world, expected_world, atol=1e-5, rtol=0.0),
                f"Imported world coordinates differ from source hierarchy: {label}/{mesh_path}")
        require(not rows(source_mesh['bones'], 'mesh.bones') and source_mesh['skin'] is None,
                f"Test source unexpectedly contains skin: {label}/{mesh_path}")

    mesh_descendants: dict[str, tuple[bpy.types.Object, ...]] = {}
    for path in node_paths:
        descendant_paths = tuple(
            mesh_path for mesh_path in mesh_paths
            if mesh_path == path or mesh_path.startswith(path.rstrip('/') + '/')
        )
        mesh_descendants[path] = tuple(transforms[mesh_path] for mesh_path in descendant_paths)
    intermediate_paths = tuple(
        path for path in node_paths
        if path != root_path and path not in set(mesh_paths) and mesh_descendants[path]
    )
    require(bool(intermediate_paths), f"Weapon source hierarchy has no movable intermediate transform: {label}")
    intermediate_path = intermediate_paths[0]
    intermediate_follow_error, intermediate_restore_error = validate_intermediate_follow(
        transforms[intermediate_path], mesh_descendants[intermediate_path], label,
    )
    follow_error, restore_error = validate_root_follow(root, meshes, label)
    return {
        'name': label,
        'status': 'passed',
        'source_root': text(source_root['path'], 'root.path'),
        'source_transforms': len(source_nodes),
        'source_meshes': len(source_meshes),
        'vertices': sum(integer(row['vertex_count'], 'mesh.vertex_count') for row in source_meshes),
        'uv_layers': uv_layer_count,
        'parentless_roots': 1,
        'animators': 0,
        'armatures': 0,
        'maximum_local_coordinate_error': maximum_coordinate_error,
        'maximum_root_follow_error': follow_error,
        'maximum_restore_error': restore_error,
        'intermediate_transform': intermediate_path,
        'intermediate_descendant_meshes': len(mesh_descendants[intermediate_path]),
        'maximum_intermediate_follow_error': intermediate_follow_error,
        'maximum_intermediate_restore_error': intermediate_restore_error,
    }


def copy_geometry_case(source_path: Path, case_root: Path) -> tuple[dict[str, object], Path, Path]:
    document, _ = read_geometry_payload(source_path)
    binary_relative = Path(text(document['binary_path'], 'binary_path'))
    source_binary = (source_path.parent / binary_relative).resolve(strict=True)
    relative = source_binary.relative_to(source_path.parent.resolve(strict=True))
    destination_binary = case_root / relative
    destination_binary.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(source_binary, destination_binary)
    destination_json = case_root / source_path.name
    document['binary_path'] = str(relative)
    destination_json.write_text(json.dumps(document, indent=2), encoding='utf-8')
    return document, destination_json, destination_binary


def expect_value_error(action: Callable[[], object], expected_message: str, label: str) -> str:
    try:
        action()
    except ValueError as error:
        message = str(error)
        require(expected_message in message, f"{label} failed for the wrong reason: {message}")
        return message
    raise AssertionError(f"{label} did not fail")


def validate_corrupted_payloads(geometry_path: Path, temporary_root: Path) -> dict[str, object]:
    hash_document, hash_path, binary_path = copy_geometry_case(geometry_path, temporary_root / 'binary_hash')
    content = bytearray(binary_path.read_bytes())
    require(len(content) > 0, 'Geometry binary is unexpectedly empty')
    content[0] ^= 1
    binary_path.write_bytes(content)
    hash_message = expect_value_error(
        lambda: read_geometry_payload(hash_path),
        'Geometry binary identity mismatch',
        'Corrupted geometry binary',
    )

    skin_document, skin_path, _ = copy_geometry_case(geometry_path, temporary_root / 'skin_reference')
    mesh_rows = rows(skin_document['meshes'], 'meshes')
    first_mesh = fields(mesh_rows[0], 'mesh')
    first_mesh['skin'] = {'format': 'EndfieldSparseSkin/1', 'offsets': {'offset': 0, 'count': 0, 'components': 1, 'dtype': 'uint32'}}
    skin_path.write_text(json.dumps(skin_document, indent=2), encoding='utf-8')
    clear_scene_objects()
    skin_message = expect_value_error(
        lambda: import_static_geometry(skin_path),
        'Static geometry importer cannot discard source skin bindings',
        'Injected source skin reference',
    )
    return {
        'binary_hash_mismatch': {'status': 'passed', 'message': hash_message},
        'skin_reference_rejection': {'status': 'passed', 'message': skin_message},
        'source_was_copied': True,
    }


def validate_character_import(geometry_path: Path) -> dict[str, object]:
    document, payload = read_geometry_payload(geometry_path)
    source_meshes = tuple(fields(value, 'mesh') for value in rows(document['meshes'], 'meshes'))
    source_roots = tuple(fields(value, 'node') for value in rows(document['nodes'], 'nodes')
                         if fields(value, 'node')['parent_path'] is None)
    require(len(source_roots) == 1, 'Character geometry must have one explicit source root')
    require(any(rows(row['bones'], 'mesh.bones') and row['skin'] is not None for row in source_meshes),
            'Character regression input must contain skinned geometry')

    clear_scene_objects()
    imported = import_character_geometry(geometry_path)
    rig_rows = tuple(obj for obj in imported if obj.type == 'ARMATURE')
    meshes = tuple(obj for obj in imported if obj.type == 'MESH')
    require(len(rig_rows) == 1, 'Character import did not create exactly one armature')
    require(len(meshes) == len(source_meshes), 'Character import mesh count changed')
    rig = rig_rows[0]
    require(rig.parent is None, 'Character root armature unexpectedly has a parent')
    require(rig.get('source_transform_path') == text(source_roots[0]['path'], 'character.root.path'),
            'Character root identity changed')
    by_path = {text(obj.get('source_geometry_path'), 'mesh.source_geometry_path'): obj for obj in meshes}
    require(len(by_path) == len(meshes), 'Character source mesh paths are not unique')
    uv_layer_count = 0
    total_vertices = 0
    for source_mesh in source_meshes:
        mesh_path = text(source_mesh['path'], 'mesh.path')
        obj = by_path.get(mesh_path)
        require(obj is not None and obj.parent == rig, f"Character mesh hierarchy changed: {mesh_path}")
        expected_local = source_array(source_mesh['positions'], payload, 3, 'float32')
        actual_local = local_vertices(obj)
        require(len(actual_local) == integer(source_mesh['vertex_count'], 'mesh.vertex_count'),
                f"Character vertex count changed: {mesh_path}")
        require(np.allclose(actual_local, expected_local, atol=1e-7, rtol=0.0),
                f"Character local coordinates changed: {mesh_path}")
        expected_bones = tuple(fields(value, 'bone') for value in rows(source_mesh['bones'], 'mesh.bones'))
        require(len(obj.vertex_groups) == len(expected_bones), f"Character vertex group count changed: {mesh_path}")
        require(any(modifier.type == 'ARMATURE' and modifier.object == rig for modifier in obj.modifiers),
                f"Character armature modifier is missing: {mesh_path}")
        uv_layer_count += validate_uv_layers(obj, source_mesh, payload, 'character/' + mesh_path)
        total_vertices += len(actual_local)
    return {
        'status': 'passed',
        'source_geometry': str(geometry_path),
        'source_meshes': len(source_meshes),
        'vertices': total_vertices,
        'uv_layers': uv_layer_count,
        'source_skin_meshes': sum(1 for row in source_meshes if row['skin'] is not None),
        'armatures': len(rig_rows),
    }


def main(argv: Sequence[str]) -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--research-root', type=Path, required=True)
    parser.add_argument('--character-geometry', type=Path, required=True)
    parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args(list(argv))
    research_root = args.research_root.resolve(strict=True)
    character_geometry = args.character_geometry.resolve(strict=True)
    output = args.output.resolve()

    weapon_results = []
    for name in ('claymore', 'claymore_refined', 'sword', 'sword_refined'):
        geometry_path = (research_root / name / 'static_probe' / 'model.geometry.json').resolve(strict=True)
        weapon_results.append(validate_static_weapon(geometry_path, name))

    with tempfile.TemporaryDirectory(prefix='weapon_geometry_regression_', dir=str(research_root.parent)) as temporary:
        corrupted = validate_corrupted_payloads(
            (research_root / 'claymore' / 'static_probe' / 'model.geometry.json').resolve(strict=True),
            Path(temporary),
        )

    character_result = validate_character_import(character_geometry)
    report = {
        'format': 'EndfieldWeaponGeometryRegression/1',
        'status': 'passed',
        'blender_version': bpy.app.version_string,
        'weapons': weapon_results,
        'corrupted_payloads': corrupted,
        'character': character_result,
    }
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(report, indent=2), encoding='utf-8')
    print(json.dumps({'status': report['status'], 'output': str(output),
                      'weapons': len(weapon_results), 'character_meshes': character_result['source_meshes']}))


if __name__ == '__main__':
    if '--' not in sys.argv:
        raise SystemExit('Expected Blender arguments after --')
    main(sys.argv[sys.argv.index('--') + 1:])
