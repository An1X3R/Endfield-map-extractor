"""Import source-bound character geometry without an Autodesk SDK dependency."""
from __future__ import annotations

from dataclasses import dataclass
import hashlib
import json
import math
from pathlib import Path

import bpy
from mathutils import Matrix, Quaternion, Vector
import numpy as np

from character_animation_binding import fields, rows, numbers, text, integer


@dataclass(frozen=True)
class GeometryNode:
    path: str
    name: str
    parent: str | None
    local: Matrix


def _array(value: object, payload: bytes, components: int, dtype: str) -> np.ndarray:
    spec = fields(value, 'geometry array')
    offset = integer(spec['offset'], 'array.offset')
    count = integer(spec['count'], 'array.count')
    if spec['components'] != components or spec['dtype'] != dtype:
        raise ValueError(f'Geometry array type mismatch: {spec}, expected={dtype}/{components}')
    types = {'float32': '<f4', 'int32': '<i4', 'uint32': '<u4'}
    if offset < 0 or count < 0 or offset + count * components * 4 > len(payload):
        raise ValueError(f'Geometry array exceeds payload bounds: {spec}')
    result = np.frombuffer(payload, dtype=types[dtype], count=count * components, offset=offset).reshape(count, components)
    if not np.isfinite(result).all():
        raise ValueError(f'Nonfinite geometry array: {spec}')
    return result


def _nodes(value: object) -> tuple[GeometryNode, ...]:
    result = []
    for item in rows(value, 'nodes'):
        row = fields(item, 'node')
        path = text(row['path'], 'node.path')
        parent = None if row['parent_path'] is None else text(row['parent_path'], 'node.parent_path')
        q = numbers(row['rotation'], 4, path + '.rotation')
        quaternion = Quaternion((q[3], q[0], q[1], q[2]))
        if abs(quaternion.magnitude - 1) > 1e-4:
            raise ValueError(f'Geometry node has non-unit rotation: {path}')
        local = Matrix.LocRotScale(Vector(numbers(row['position'], 3, path + '.position')),
                                  quaternion, Vector(numbers(row['scale'], 3, path + '.scale')))
        result.append(GeometryNode(path, text(row['name'], 'node.name'), parent, local))
    if not result or len({node.path for node in result}) != len(result):
        raise ValueError('Geometry requires nonempty, unique source node paths')
    return tuple(result)


def _worlds(nodes: tuple[GeometryNode, ...]) -> dict[str, Matrix]:
    lookup = {node.path: node for node in nodes}
    result: dict[str, Matrix] = {}
    active: set[str] = set()
    def solve(path: str) -> Matrix:
        if path not in lookup or path in active:
            raise ValueError(f'Missing or cyclic geometry parent: {path}')
        if path not in result:
            active.add(path)
            node = lookup[path]
            result[path] = node.local.copy() if node.parent is None else solve(node.parent) @ node.local
            active.remove(path)
        return result[path]
    for node in nodes:
        solve(node.path)
    return result


def _matrix(value: object) -> Matrix:
    elements = numbers(value, 16, 'inverse bind matrix')
    result = Matrix(tuple(tuple(elements[column * 4 + row] for column in range(4)) for row in range(4)))
    if abs(result.determinant()) < 1e-12:
        raise ValueError('Singular source inverse bind matrix')
    return result


def geometry_bind_worlds(worlds: dict[str, Matrix], meshes: tuple[dict[str, object], ...]) -> tuple[dict[str, Matrix], dict[str, Matrix]]:
    """Resolve the canonical source skin bind space used by geometry import."""
    binds: dict[str, Matrix] = {}
    mesh_worlds: dict[str, Matrix] = {}
    for mesh in meshes:
        mesh_path = text(mesh['path'], 'mesh.path')
        world = worlds[mesh_path]
        candidates: dict[str, Matrix] = {}
        for raw in rows(mesh['bones'], 'mesh.bones'):
            bone = fields(raw, 'bone')
            path = text(bone['path'], 'bone.path')
            candidates[path] = world @ _matrix(bone['inverse_bind_matrix_column_major']).inverted()
        shared = tuple(path for path in candidates if path in binds)
        # Shared joints establish a canonical bind space across render and shadow meshes.
        # Keep each mesh's original world transform; applying this correction to it rotates valid geometry.
        correction = candidates[shared[0]] @ binds[shared[0]].inverted() if shared else Matrix.Identity(4)
        inverse = correction.inverted()
        for path, candidate in candidates.items():
            bind = inverse @ candidate
            if path in binds:
                residual = max(abs(a - b) for r1, r2 in zip(bind, binds[path]) for a, b in zip(r1, r2))
                if residual > .0005:
                    raise ValueError(f'No common mesh bind-space correction: bone={path}, mesh={mesh["name"]}, residual={residual}')
            else:
                binds[path] = bind
        mesh_worlds[mesh_path] = world
    if not binds:
        raise ValueError('Character geometry contains no skin bones')
    return binds, mesh_worlds


def source_bone_name(node: GeometryNode) -> str:
    """Fit Blender's 63-byte UTF-8 name limit without splitting a source code point."""
    if len(node.name.encode('utf-8')) <= 63:
        return node.name
    suffix = '_' + hashlib.sha256(node.path.encode('utf-8')).hexdigest()[:12]
    prefix = ''
    for character in node.name:
        if len((prefix + character + suffix).encode('utf-8')) > 63:
            break
        prefix += character
    return prefix + suffix


def _rig(nodes: tuple[GeometryNode, ...], worlds: dict[str, Matrix], meshes: tuple[dict[str, object], ...]) -> tuple[bpy.types.Object, dict[str, str], dict[str, Matrix]]:
    roots = tuple(node for node in nodes if node.parent is None)
    if len(roots) != 1:
        raise ValueError(f'Geometry must have one explicit root: {len(roots)}')
    lookup = {node.path: node for node in nodes}
    binds, mesh_worlds = geometry_bind_worlds(worlds, meshes)
    paths = set(binds)
    for path in tuple(paths):
        while path is not None:
            if path not in lookup:
                raise ValueError(f'Skin bone is absent from source hierarchy: {path}')
            paths.add(path)
            path = lookup[path].parent
    skin_hierarchy = frozenset(paths)
    paths = set(lookup)
    data = bpy.data.armatures.new(roots[0].name + ' Skeleton')
    rig = bpy.data.objects.new(roots[0].name, data)
    rig['source_transform_path'] = roots[0].path
    bpy.context.collection.objects.link(rig)
    rig.matrix_world = Matrix.Rotation(math.pi / 2, 4, 'X')
    bpy.context.view_layer.objects.active = rig
    rig.select_set(True)
    names: dict[str, str] = {}
    bpy.ops.object.mode_set(mode='EDIT')
    try:
        for node in nodes:
            if node.path not in paths:
                continue
            bone = data.edit_bones.new(source_bone_name(node))
            bone.head = (0, 0, 0)
            bone.tail = (0, .05, 0)
            bone.matrix = binds.get(node.path, worlds[node.path])
            if node.path not in skin_hierarchy:
                bone.use_deform = False
            names[node.path] = bone.name
        for node in nodes:
            if node.path in paths and node.parent in paths:
                data.edit_bones[names[node.path]].parent = data.edit_bones[names[node.parent]]
    finally:
        bpy.ops.object.mode_set(mode='OBJECT')
    for path, name in names.items():
        data.bones[name]['source_transform_path'] = path
        rig.pose.bones[name]['source_transform_path'] = path
        if path not in skin_hierarchy:
            data.bones[name]['source_unskinned_transform'] = True
            data.bones[name].hide = True
    rig['source_unskinned_transform_count'] = len(paths - skin_hierarchy)
    return rig, names, mesh_worlds


def _mesh(row: dict[str, object], payload: bytes, world: Matrix, rig: bpy.types.Object,
          bone_names: dict[str, str]) -> bpy.types.Object:
    name = text(row['name'], 'mesh.name')
    positions = _array(row['positions'], payload, 3, 'float32')
    count = integer(row['vertex_count'], 'mesh.vertex_count')
    if len(positions) != count:
        raise ValueError(f'Geometry vertex count mismatch: {name}')
    sections = tuple(fields(value, 'submesh') for value in rows(row['submeshes'], 'submeshes'))
    triangles = tuple(_array(section['indices'], payload, 3, 'int32') for section in sections)
    faces = np.concatenate(triangles) if triangles else np.empty((0, 3), dtype=np.uint32)
    if faces.size and (int(faces.max()) >= count or int(faces.min()) < 0):
        raise ValueError(f'Geometry triangle index exceeds vertex count: {name}')
    data = bpy.data.meshes.new(name)
    data.from_pydata(positions.tolist(), [], faces.tolist())
    data.update()
    obj = bpy.data.objects.new(name, data)
    bpy.context.collection.objects.link(obj)
    obj.parent = rig
    obj.matrix_world = rig.matrix_world @ world
    obj['source_geometry_path'] = text(row['path'], 'mesh.path')
    offset = 0
    for section, indices in zip(sections, triangles, strict=True):
        material_name = text(section['material'], 'submesh.material')
        material = bpy.data.materials.get(material_name)
        if material is None:
            material = bpy.data.materials.new(material_name)
        slot = len(data.materials)
        data.materials.append(material)
        for polygon in data.polygons[offset:offset + len(indices)]:
            polygon.material_index = slot
            polygon.use_smooth = True
        offset += len(indices)
    loop_vertices = np.array([loop.vertex_index for loop in data.loops])
    for raw in rows(row['uvs'], 'uvs'):
        uv = fields(raw, 'uv')
        coordinates = _array(uv['values'], payload, 2, 'float32')
        if len(coordinates) != count:
            raise ValueError(f'Geometry UV count mismatch: {name}')
        layer = data.uv_layers.new(name=f"UV{integer(uv['channel'], 'uv.channel')}")
        layer.data.foreach_set('uv', coordinates[loop_vertices].reshape(-1))
    # Remove degenerate source faces while retaining corner attributes, as Blender's FBX importer does.
    data.validate(clean_customdata=False)
    if row['normals'] is not None:
        normals = _array(row['normals'], payload, 3, 'float32')
        if len(normals) != count:
            raise ValueError(f'Geometry normal count mismatch: {name}')
        lengths = np.linalg.norm(normals, axis=1, keepdims=True)
        # Blender expects unit custom normals; zero vectors explicitly request its geometric normal.
        normalized = np.divide(normals, lengths, out=np.zeros_like(normals), where=lengths > 0)
        data.normals_split_custom_set_from_vertices(normalized.tolist())
    if row['colors'] is not None:
        colors = _array(row['colors'], payload, 4, 'float32')
        if len(colors) != count:
            raise ValueError(f'Geometry color count mismatch: {name}')
        data.color_attributes.new(name='Color', type='FLOAT_COLOR', domain='POINT').data.foreach_set('color', colors.reshape(-1))
    if row['tangents'] is not None:
        tangents = _array(row['tangents'], payload, 4, 'float32')
        if len(tangents) != count:
            raise ValueError(f'Geometry tangent count mismatch: {name}')
        data.attributes.new(name='source_tangent', type='FLOAT_VECTOR', domain='POINT').data.foreach_set('vector', tangents[:, :3].reshape(-1))
        data.attributes.new(name='source_tangent_sign', type='FLOAT', domain='POINT').data.foreach_set('value', tangents[:, 3])
    bones = tuple(fields(item, 'bone') for item in rows(row['bones'], 'bones'))
    if bones:
        skin = fields(row['skin'], 'skin')
        offsets = _array(skin['offsets'], payload, 1, 'uint32').reshape(-1)
        indices = _array(skin['indices'], payload, 1, 'int32').reshape(-1)
        weights = _array(skin['weights'], payload, 1, 'float32').reshape(-1)
        if (len(offsets) != count + 1 or offsets[0] != 0 or offsets[-1] != len(indices)
                or len(indices) != len(weights) or np.any(offsets[1:] < offsets[:-1])
                or np.any(indices < 0) or np.any(indices >= len(bones)) or np.any(weights < 0)):
            raise ValueError(f'Invalid sparse geometry skin: {name}')
        groups = tuple(obj.vertex_groups.new(name=bone_names[text(bone['path'], 'bone.path')]) for bone in bones)
        for vertex in range(count):
            for i in range(int(offsets[vertex]), int(offsets[vertex + 1])):
                if weights[i] > 0:
                    groups[int(indices[i])].add([vertex], float(weights[i]), 'REPLACE')
        modifier = obj.modifiers.new('Source skin', 'ARMATURE')
        modifier.object = rig
    return obj


def _morphs(value: object, payload: bytes, objects: dict[str, bpy.types.Object]) -> None:
    for raw in rows(value, 'morphs'):
        morph = fields(raw, 'morph')
        path = text(morph['path'], 'morph.path')
        obj = objects[path]
        if obj.data.shape_keys is None:
            obj.shape_key_add(name='Basis')
        for item in rows(morph['channels'], 'morph.channels'):
            channel = fields(item, 'channel')
            name = text(channel['name'], 'channel.name')
            frames = rows(channel['keyframes'], 'channel.keyframes')
            for raw_frame in frames:
                frame = fields(raw_frame, 'morph frame')
                indices = _array(frame['indices'], payload, 1, 'uint32').reshape(-1)
                positions = _array(frame['positions'], payload, 3, 'float32')
                if len(indices) != len(positions) or (len(indices) and int(indices.max()) >= len(obj.data.vertices)):
                    raise ValueError(f'Morph vertex mapping is invalid: {path}/{name}')
                key = obj.shape_key_add(name=name if len(frames) == 1 else f"{name}@{frame['weight']}")
                for index, position in zip(indices, positions, strict=True):
                    key.data[int(index)].co = position


def read_geometry_payload(path: Path) -> tuple[dict[str, object], bytes]:
    """Verify the shared portable geometry and binary identities before import."""
    document = fields(json.loads(path.read_text(encoding='utf-8')), 'geometry')
    if document['format'] != 'EndfieldCharacterGeometry/1':
        raise ValueError(f'Unsupported character geometry format: {path}')
    binary_path = (path.parent / text(document['binary_path'], 'binary_path')).resolve()
    if not binary_path.is_relative_to(path.parent.resolve()):
        raise ValueError(f'Geometry binary escapes its source directory: {binary_path}')
    payload = binary_path.read_bytes()
    if len(payload) != document['binary_bytes'] or hashlib.sha256(payload).hexdigest() != document['binary_sha256']:
        raise ValueError(f'Geometry binary identity mismatch: {binary_path}')
    return document, payload


def import_static_geometry(path: Path) -> list[bpy.types.Object]:
    """Import unskinned source meshes under one movable source root."""
    document, payload = read_geometry_payload(path)
    nodes = _nodes(document['nodes'])
    worlds = _worlds(nodes)
    roots = tuple(node for node in nodes if node.parent is None)
    if len(roots) != 1:
        raise ValueError(f'Static geometry requires one explicit root: {len(roots)}')
    meshes = tuple(fields(value, 'mesh') for value in rows(document['meshes'], 'meshes'))
    paths = tuple(text(mesh['path'], 'mesh.path') for mesh in meshes)
    if not meshes or len(set(paths)) != len(paths):
        raise ValueError('Static geometry requires nonempty unique mesh paths')
    if any(rows(mesh['bones'], 'mesh.bones') or mesh['skin'] is not None for mesh in meshes):
        raise ValueError('Static geometry importer cannot discard source skin bindings')
    root = bpy.data.objects.new(roots[0].name, None)
    root['source_transform_path'] = roots[0].path
    root['source_static_hierarchy'] = json.dumps(document['nodes'])
    bpy.context.collection.objects.link(root)
    root.matrix_world = Matrix.Rotation(math.pi / 2, 4, 'X')
    objects = {text(mesh['path'], 'mesh.path'): _mesh(mesh, payload, worlds[text(mesh['path'], 'mesh.path')], root, {}) for mesh in meshes}
    transforms = {roots[0].path: root, **objects}
    if roots[0].path in objects:
        bpy.data.objects.remove(root, do_unlink=True)
        root = objects[roots[0].path]
    for node in nodes:
        if node.path not in transforms:
            obj = bpy.data.objects.new(node.name, None)
            bpy.context.collection.objects.link(obj)
            transforms[node.path] = obj
    for node in nodes:
        obj = transforms[node.path]
        obj['source_transform_path'] = node.path
        obj.parent = transforms[node.parent] if node.parent is not None else None
        obj.matrix_parent_inverse = Matrix.Identity(4)
        obj.matrix_basis = node.local if node.parent is not None else Matrix.Rotation(math.pi / 2, 4, 'X') @ node.local
    root['source_static_hierarchy'] = json.dumps(document['nodes'])
    _morphs(document['morphs'], payload, objects)
    bpy.context.view_layer.update()
    return list(transforms.values())


def import_character_geometry(path: Path) -> list[bpy.types.Object]:
    """Create editable geometry from the verified native reader payload."""
    document, payload = read_geometry_payload(path)
    nodes = _nodes(document['nodes'])
    worlds = _worlds(nodes)
    meshes = tuple(fields(value, 'mesh') for value in rows(document['meshes'], 'meshes'))
    if len({text(mesh['path'], 'mesh.path') for mesh in meshes}) != len(meshes):
        raise ValueError('Duplicate geometry mesh paths')
    rig, names, mesh_worlds = _rig(nodes, worlds, meshes)
    objects = {text(mesh['path'], 'mesh.path'): _mesh(mesh, payload, mesh_worlds[text(mesh['path'], 'mesh.path')], rig, names) for mesh in meshes}
    _morphs(document['morphs'], payload, objects)
    bpy.context.view_layer.update()
    return [rig, *objects.values()]


