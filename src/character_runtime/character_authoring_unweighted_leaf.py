"""Restore exact unweighted Avatar leaves omitted from an imported deform rig."""
from __future__ import annotations

import hashlib
import json
import struct
from pathlib import Path

import bpy
from mathutils import Matrix, Quaternion, Vector
from basic_expression_shape_baker import _array, _avatar_identity, _integer, _number, _record, _text, _vector3


REST_MATRIX_TOLERANCE = 2.0e-5
WEIGHT_EPSILON = 1.0e-8


def _read_document(path: Path, context: str) -> tuple[dict[str, object], bytes, Path]:
    resolved = path.resolve(strict=True)
    if not resolved.is_file():
        raise FileNotFoundError(f'Expected {context} file: {resolved}')
    payload = resolved.read_bytes()
    return _record(json.loads(payload.decode('utf-8-sig')), context), payload, resolved


def _source_identity(value: object, context: str) -> tuple[str, int]:
    row = _record(value, context)
    return (_text(row.get('source'), context + '.source').casefold(),
            _integer(row.get('path_id'), context + '.path_id'))


def _avatar_node_path(node: dict[str, object], index: int) -> str | None:
    value = node.get('avatar_tos_path')
    if value == '' and index == 0 and node.get('avatar_tos_path_status') == 'zero_id_root_sentinel_without_tos_entry':
        if (_integer(node.get('avatar_skeleton_id'), 'Avatar root sentinel ID') != 0 or
                _integer(node.get('parent_id'), 'Avatar root sentinel parent') != -1):
            raise ValueError('Avatar root sentinel fields are inconsistent')
        return None
    return _text(value, f'avatar_node_map.nodes[{index}].avatar_tos_path')


def _bone_path(bone: bpy.types.Bone) -> str:
    parts: list[str] = []
    current = bone
    while current is not None:
        parts.append(current.name)
        current = current.parent
    return '/'.join(reversed(parts))


def _matrix_values(matrix: Matrix) -> list[list[float]]:
    return [[float(matrix[row][column]) for column in range(4)] for row in range(4)]


def _matrix_error(left: Matrix, right: Matrix) -> float:
    return max(abs(left[row][column] - right[row][column]) for row in range(4) for column in range(4))


def _matrix_from_source_transform(row: dict[str, object], context: str) -> Matrix:
    position = _vector3(row.get('position'), context + '.position')
    scale = _vector3(row.get('scale'), context + '.scale')
    values = _array(row.get('rotation'), context + '.rotation')
    if len(values) != 4:
        raise ValueError(f'Expected four quaternion values at {context}.rotation')
    x, y, z, w = (_number(value, f'{context}.rotation[{index}]') for index, value in enumerate(values))
    return Matrix.LocRotScale(Vector((-position.x, position.y, position.z)),
                              Quaternion((w, x, -y, -z)).normalized(), scale)


def _transform_path(row: dict[str, object], context: str) -> str | None:
    path = _text(row.get('path'), context + '.path')
    pieces = path.split('/', maxsplit=1)
    if len(pieces) != 2:
        return None
    return pieces[1]


def _armature_snapshot(excluded: dict[str, set[str]]) -> tuple[str, int]:
    digest = hashlib.sha256()
    count = 0
    armatures = sorted((item for item in bpy.data.objects if item.type == 'ARMATURE'), key=lambda item: item.name)
    for obj in armatures:
        digest.update((obj.name + '\0' + obj.data.name + '\0').encode())
        omitted = excluded.get(obj.name, set())
        for bone in sorted(obj.data.bones, key=lambda item: item.name):
            if bone.name in omitted:
                continue
            digest.update((bone.name + '\0' + (bone.parent.name if bone.parent else '') + '\0').encode())
            digest.update(struct.pack('<?', bool(bone.use_deform)))
            digest.update(struct.pack('<16f', *(float(component) for row in bone.matrix_local for component in row)))
            count += 1
    return digest.hexdigest(), count


def _mesh_weight_snapshot() -> str:
    digest = hashlib.sha256()
    meshes = sorted((item for item in bpy.data.objects if item.type == 'MESH'), key=lambda item: item.name)
    for obj in meshes:
        digest.update((obj.name + '\0' + obj.data.name + '\0').encode())
        for group in sorted(obj.vertex_groups, key=lambda item: item.index):
            digest.update((group.name + '\0').encode())
            digest.update(struct.pack('<I', group.index))
        for vertex in obj.data.vertices:
            digest.update(struct.pack('<I', vertex.index))
            for membership in sorted(vertex.groups, key=lambda item: item.group):
                digest.update(struct.pack('<If', membership.group, membership.weight))
    return digest.hexdigest()


def _positive_vertex_weight_count(armature: bpy.types.Object, bone_name: str) -> int:
    positive = 0
    for obj in bpy.data.objects:
        if obj.type != 'MESH' or not any(modifier.type == 'ARMATURE' and modifier.object == armature for modifier in obj.modifiers):
            continue
        group_indices = {group.index for group in obj.vertex_groups if group.name == bone_name}
        for vertex in obj.data.vertices:
            positive += sum(1 for membership in vertex.groups
                            if membership.group in group_indices and membership.weight > WEIGHT_EPSILON)
    return positive


def _renderer_bone_reference_count(
    audit: dict[str, object], transform_by_identity: dict[tuple[str, int], dict[str, object]],
    target_identity: tuple[str, int],
) -> tuple[int, int]:
    renderers = _array(audit.get('renderers'), 'source audit.renderers')
    total = 0
    target = 0
    for renderer_index, value in enumerate(renderers):
        renderer = _record(value, f'source audit.renderers[{renderer_index}]')
        references = _array(renderer.get('bones'), f'source audit.renderers[{renderer_index}].bones')
        declared = _integer(renderer.get('source_bone_reference_count'), f'renderer[{renderer_index}].source_bone_reference_count')
        resolved_count = _integer(renderer.get('resolved_source_bone_count'), f'renderer[{renderer_index}].resolved_source_bone_count')
        unresolved = _integer(renderer.get('positive_unresolved_bone_influences'), f'renderer[{renderer_index}].positive_unresolved_bone_influences')
        if declared != len(references) or resolved_count != len(references) or unresolved != 0:
            raise ValueError(f'Renderer bone references are not fully resolved: renderer_index={renderer_index}, '
                             f'declared={declared}, listed={len(references)}, resolved={resolved_count}, positive_unresolved={unresolved}')
        for reference_index, value in enumerate(references):
            reference = _record(value, f'renderer[{renderer_index}].bones[{reference_index}]')
            resolved_value = reference.get('resolved')
            if resolved_value is None:
                raise ValueError(f'Renderer bone pointer is unresolved: renderer_index={renderer_index}, reference_index={reference_index}')
            resolved = _record(resolved_value, f'renderer[{renderer_index}].bones[{reference_index}].resolved')
            if resolved.get('type') != 'Transform':
                raise ValueError(f'Renderer bone pointer resolved to a non-Transform: renderer_index={renderer_index}, reference_index={reference_index}')
            identity = _source_identity(resolved, f'renderer[{renderer_index}].bones[{reference_index}].resolved')
            if identity not in transform_by_identity:
                raise ValueError(f'Renderer bone Transform is missing from source audit: identity={identity}')
            total += 1
            target += int(identity == target_identity)
    if not renderers:
        raise ValueError('Source audit has no renderers from which to prove an unweighted leaf')
    return total, target


def _path_prefix_from_existing_bindings(
    bindings: list[dict[str, object]], armature: bpy.types.Object,
) -> str:
    prefixes: set[str] = set()
    matched = 0
    for index, binding in enumerate(bindings):
        name = _text(binding.get('terminal_name'), f'avatar_bone_bindings[{index}].terminal_name')
        bone = armature.data.bones.get(name)
        if bone is None:
            continue
        avatar_path = _text(binding.get('avatar_tos_path'), f'avatar_bone_bindings[{index}].avatar_tos_path')
        rig_path = _bone_path(bone)
        if avatar_path == rig_path:
            prefix = ''
        elif avatar_path.endswith('/' + rig_path):
            prefix = avatar_path[:-len(rig_path)]
        else:
            raise ValueError(f'Existing Avatar-to-rig parent path is not exact: avatar={avatar_path}, rig={rig_path}')
        prefixes.add(prefix)
        matched += 1
    if matched == 0 or len(prefixes) != 1:
        raise ValueError(f'Existing Avatar bindings do not establish one exact rig-path prefix: matched={matched}, prefixes={sorted(prefixes)}')
    return next(iter(prefixes))


def _source_global_matrices(path_rows: dict[str, dict[str, object]]) -> dict[str, Matrix]:
    matrices: dict[str, Matrix] = {}
    for path in sorted(path_rows, key=lambda item: (item.count('/'), item)):
        row = path_rows[path]
        local = _matrix_from_source_transform(row, path)
        parent_path = path.rsplit('/', maxsplit=1)[0] if '/' in path else None
        parent = matrices.get(parent_path) if parent_path is not None else None
        matrices[path] = parent @ local if parent is not None else local
    return matrices


def _validated_missing_rows(
    armature: bpy.types.Object, table: dict[str, object], audit: dict[str, object],
) -> tuple[list[dict[str, object]], dict[str, dict[str, object]], dict[tuple[str, int], dict[str, object]], str]:
    if table.get('avatar_bone_bindings_status') != 'exact_avatar_m_id_to_tos_paths_and_source_base_pose_verified':
        raise ValueError('Expression table lacks exact Avatar skeleton-to-TOS bindings')
    node_map = _record(table.get('avatar_node_map'), 'expression table.avatar_node_map')
    if node_map.get('status') != 'exact_avatar_m_id_to_tos_paths_decoded':
        raise ValueError('Expression table Avatar node map is not exact and decoded')
    raw = _record(table.get('raw'), 'expression table.raw')
    prefix = _record(raw.get('asset_prefix'), 'expression table.raw.asset_prefix')
    data = _record(prefix.get('data'), 'expression table.raw.asset_prefix.data')
    base_pose = _array(data.get('basePoseConfig'), 'basePoseConfig')
    names = _array(data.get('allBoneNames'), 'allBoneNames')
    bindings = [_record(value, f'avatar_bone_bindings[{index}]')
                for index, value in enumerate(_array(table.get('avatar_bone_bindings'), 'avatar_bone_bindings'))]
    nodes = [_record(value, f'avatar_node_map.nodes[{index}]')
             for index, value in enumerate(_array(node_map.get('nodes'), 'avatar_node_map.nodes'))]
    if len(base_pose) != len(names) or len(base_pose) != len(bindings):
        raise ValueError(f'Expression base-pose arrays differ: base={len(base_pose)}, names={len(names)}, bindings={len(bindings)}')
    if len({str(value) for value in names}) != len(names):
        raise ValueError('Expression Avatar has duplicate terminal names; name-only rig binding is ambiguous')
    nodes_by_path: dict[str, list[dict[str, object]]] = {}
    for index, node in enumerate(nodes):
        path = _avatar_node_path(node, index)
        if path is None:
            continue
        nodes_by_path.setdefault(path, []).append(node)
    source_rows = [_record(value, f'source audit.transforms[{index}]')
                   for index, value in enumerate(_array(audit.get('transforms'), 'source audit.transforms'))]
    path_rows: dict[str, dict[str, object]] = {}
    transform_by_identity: dict[tuple[str, int], dict[str, object]] = {}
    for index, row in enumerate(source_rows):
        identity = _source_identity(row.get('object'), f'source audit.transforms[{index}].object')
        if identity in transform_by_identity:
            raise ValueError(f'Source audit has duplicate Transform identity: {identity}')
        transform_by_identity[identity] = row
        path = _transform_path(row, f'source audit.transforms[{index}]')
        if path is None:
            continue
        if path in path_rows:
            raise ValueError(f'Source audit has duplicate Transform path: {path}')
        path_rows[path] = row
    missing: list[dict[str, object]] = []
    for index, (base_value, name_value, binding) in enumerate(zip(base_pose, names, bindings, strict=True)):
        base = _record(base_value, f'basePoseConfig[{index}]')
        name = _text(name_value, f'allBoneNames[{index}]')
        bone_id = _integer(base.get('boneID'), f'basePoseConfig[{index}].boneID')
        binding_id = _integer(binding.get('bone_id'), f'avatar_bone_bindings[{index}].bone_id')
        bound_name = _text(binding.get('all_bone_name'), f'avatar_bone_bindings[{index}].all_bone_name')
        terminal = _text(binding.get('terminal_name'), f'avatar_bone_bindings[{index}].terminal_name')
        path = _text(binding.get('avatar_tos_path'), f'avatar_bone_bindings[{index}].avatar_tos_path')
        skeleton_id = _integer(binding.get('avatar_skeleton_id'), f'avatar_bone_bindings[{index}].avatar_skeleton_id')
        if bone_id != binding_id or name != bound_name or name != terminal or path.rsplit('/', maxsplit=1)[-1] != name:
            raise ValueError(f'Base-pose row differs from its exact Avatar binding: index={index}, bone_id={bone_id}, path={path}')
        node_matches = nodes_by_path.get(path, [])
        if len(node_matches) != 1:
            raise ValueError(f'Avatar node path is not unique: path={path}, matches={len(node_matches)}')
        node = node_matches[0]
        if (_integer(node.get('index'), f'Avatar node {path}.index') != bone_id or
                _integer(node.get('avatar_skeleton_id'), f'Avatar node {path}.avatar_skeleton_id') != skeleton_id):
            raise ValueError(f'Avatar node IDs differ from exact binding: bone_id={bone_id}, path={path}')
        if armature.data.bones.get(name) is None:
            missing.append({'base_pose': base, 'binding': binding, 'name': name, 'path': path,
                            'bone_id': bone_id, 'skeleton_id': skeleton_id})
    if not missing:
        return [], path_rows, transform_by_identity, ''
    for other in (item for item in bpy.data.objects if item.type == 'ARMATURE' and item != armature):
        if other.data == armature.data:
            raise ValueError(f'Target Armature data is shared with another rig: target={armature.name}, other={other.name}')
        elsewhere = sorted(str(row['name']) for row in missing if other.data.bones.get(str(row['name'])) is not None)
        if elsewhere:
            raise ValueError(f'Missing Avatar bones exist on another rig; refusing cross-rig reassignment: target={armature.name}, other={other.name}, bones={elsewhere}')
    path_prefix = _path_prefix_from_existing_bindings(bindings, armature)
    return missing, path_rows, transform_by_identity, path_prefix


def _prepare_candidate(
    missing: dict[str, object], path_rows: dict[str, dict[str, object]],
    transform_by_identity: dict[tuple[str, int], dict[str, object]],
    nodes_by_path: dict[str, list[dict[str, object]]], path_prefix: str,
    armature: bpy.types.Object, renderer_references: int,
) -> dict[str, object]:
    name = _text(missing.get('name'), 'missing Avatar bone.name')
    avatar_path = _text(missing.get('path'), 'missing Avatar bone.path')
    bone_id = _integer(missing.get('bone_id'), 'missing Avatar bone.bone_id')
    skeleton_id = _integer(missing.get('skeleton_id'), 'missing Avatar bone.avatar_skeleton_id')
    if name in armature.data.bones:
        raise ValueError(f'Candidate unexpectedly exists on target rig: {armature.name}/{name}')
    if any(name in obj.data.bones for obj in bpy.data.objects
           if obj.type == 'ARMATURE' and obj != armature):
        raise ValueError(f'Candidate exists on another Armature; refusing to guess ownership: {name}')
    avatar_descendants = sorted(path for path in nodes_by_path if path.startswith(avatar_path + '/'))
    if avatar_descendants:
        raise ValueError(f'Avatar candidate is not a leaf: path={avatar_path}, descendants={avatar_descendants[:8]}')
    transform_descendants = sorted(path for path in path_rows if path.startswith(avatar_path + '/'))
    if transform_descendants:
        raise ValueError(f'Source Transform candidate is not a leaf: path={avatar_path}, descendants={transform_descendants[:8]}')
    transform = path_rows.get(avatar_path)
    if transform is None:
        raise ValueError(f'Source audit has no exact Transform for Avatar leaf: {avatar_path}')
    game_object = _record(transform.get('game_object'), f'{avatar_path}.game_object')
    if _text(game_object.get('name'), f'{avatar_path}.game_object.name') != name:
        raise ValueError(f'Source Transform GameObject name differs from Avatar leaf: path={avatar_path}, name={name}')
    transform_identity = _source_identity(transform.get('object'), f'{avatar_path}.object')
    if transform_identity not in transform_by_identity:
        raise ValueError(f'Source Transform identity is absent from exact audit index: path={avatar_path}')
    if renderer_references != 0:
        raise ValueError(f'Avatar leaf is referenced by renderer m_Bones: path={avatar_path}, references={renderer_references}')
    parent_path = avatar_path.rsplit('/', maxsplit=1)[0] if '/' in avatar_path else ''
    if not parent_path or len(nodes_by_path.get(parent_path, [])) != 1:
        raise ValueError(f'Avatar leaf parent path is absent or ambiguous: leaf={avatar_path}, parent={parent_path}')
    parent_name = parent_path.rsplit('/', maxsplit=1)[-1]
    parent_bone = armature.data.bones.get(parent_name)
    if parent_bone is None:
        raise ValueError(f'Exact Avatar parent is missing from target rig: armature={armature.name}, parent={parent_name}')
    expected_parent = path_prefix + _bone_path(parent_bone)
    if expected_parent != parent_path:
        raise ValueError(f'Exact Avatar parent does not map to rig hierarchy: avatar={parent_path}, rig={expected_parent}')
    parent_ref = _record(transform.get('parent'), f'{avatar_path}.parent')
    resolved_value = parent_ref.get('resolved')
    if resolved_value is None:
        raise ValueError(f'Source leaf parent pointer is unresolved: {avatar_path}')
    resolved_parent = _record(resolved_value, f'{avatar_path}.parent.resolved')
    if resolved_parent.get('type') != 'Transform':
        raise ValueError(f'Source leaf parent is not a Transform: {avatar_path}')
    parent_identity = _source_identity(resolved_parent, f'{avatar_path}.parent.resolved')
    source_parent = transform_by_identity.get(parent_identity)
    if source_parent is None or _transform_path(source_parent, avatar_path + '.parent') != parent_path:
        raise ValueError(f'Source leaf parent does not resolve to exact Avatar parent: leaf={avatar_path}, parent={parent_path}')
    if _positive_vertex_weight_count(armature, name) != 0:
        raise ValueError(f'Missing Avatar leaf has positive vertex-group weights: {armature.name}/{name}')
    source_globals = _source_global_matrices(path_rows)
    source_matrix = source_globals.get(avatar_path)
    if source_matrix is None:
        raise ValueError(f'Could not construct source global rest transform: {avatar_path}')
    location, rotation, scale = source_matrix.decompose()
    if any(abs(float(component) - 1.0) > REST_MATRIX_TOLERANCE for component in scale):
        raise ValueError(f'Unweighted leaf rest has non-unit global scale unsupported by Blender Bone rest: path={avatar_path}, scale={tuple(scale)}')
    rest_matrix = Matrix.LocRotScale(location, rotation.normalized(), Vector((1.0, 1.0, 1.0)))
    matrix_error = _matrix_error(source_matrix, rest_matrix)
    if matrix_error > REST_MATRIX_TOLERANCE:
        raise ValueError(f'Unweighted leaf rest contains unsupported scale or shear: path={avatar_path}, error={matrix_error}')
    base = _record(missing.get('base_pose'), 'missing Avatar bone.base_pose')
    base_position = _vector3(base.get('position'), f'{avatar_path}.basePose.position')
    source_position = _vector3(transform.get('position'), f'{avatar_path}.position')
    position_error = float((base_position - source_position).length)
    if position_error > 0.006:
        raise ValueError(f'Avatar base-pose position differs from exact source Transform: path={avatar_path}, error={position_error}')
    return {
        'bone_name': name,
        'bone_id': bone_id,
        'avatar_skeleton_id': skeleton_id,
        'avatar_tos_path': avatar_path,
        'source_transform_path': _text(transform.get('path'), f'{avatar_path}.path'),
        'source_transform_identity': f'{transform_identity[0]}:{transform_identity[1]}',
        'source_transform_parent_identity': f'{parent_identity[0]}:{parent_identity[1]}',
        'parent_avatar_tos_path': parent_path,
        'parent_bone_name': parent_name,
        'renderer_bone_reference_count': renderer_references,
        'avatar_descendant_count': 0,
        'source_transform_descendant_count': 0,
        'positive_vertex_weight_count': 0,
        'base_pose_position_error_meters': position_error,
        'source_global_matrix': _matrix_values(source_matrix),
        'planned_rest_matrix': _matrix_values(rest_matrix),
        'bone_display_length_meters': 0.001,
    }


def restore_unweighted_avatar_leaf_bones(
    armature: bpy.types.Object, table_path: Path, source_audit_path: Path,
) -> dict[str, object]:
    if armature.type != 'ARMATURE':
        raise TypeError(f'Expression target is not an Armature: {armature.name}, type={armature.type}')
    table, table_payload, resolved_table = _read_document(table_path, 'expression source')
    audit, audit_payload, resolved_audit = _read_document(source_audit_path, 'source audit')
    avatar_identity = _avatar_identity(table, audit)
    missing, path_rows, transform_by_identity, path_prefix = _validated_missing_rows(armature, table, audit)
    if not missing:
        return {'format': 'EndfieldUnweightedAvatarLeafRestoration/1', 'status': 'no_missing_avatar_bones',
                'armature_name': armature.name, 'expression_source': str(resolved_table),
                'expression_source_sha256': hashlib.sha256(table_payload).hexdigest(),
                'source_audit': str(resolved_audit), 'source_audit_sha256': hashlib.sha256(audit_payload).hexdigest(),
                'avatar_identity': {'cab': avatar_identity[0], 'path_id': str(avatar_identity[1])}, 'restored_bones': []}
    node_map = _record(table.get('avatar_node_map'), 'expression table.avatar_node_map')
    nodes = [_record(value, f'avatar_node_map.nodes[{index}]')
             for index, value in enumerate(_array(node_map.get('nodes'), 'avatar_node_map.nodes'))]
    nodes_by_path: dict[str, list[dict[str, object]]] = {}
    for index, node in enumerate(nodes):
        path = _avatar_node_path(node, index)
        if path is None:
            continue
        nodes_by_path.setdefault(path, []).append(node)
    total_renderer_refs, _ = _renderer_bone_reference_count(audit, transform_by_identity, ('', 0))
    candidates: list[dict[str, object]] = []
    for row in missing:
        name = _text(row.get('name'), 'missing Avatar bone.name')
        path = _text(row.get('path'), 'missing Avatar bone.path')
        transform = path_rows.get(path)
        if transform is None:
            raise ValueError(f'Source audit has no exact Transform for missing Avatar bone: {name}')
        identity = _source_identity(transform.get('object'), f'{name}.object')
        _, references = _renderer_bone_reference_count(audit, transform_by_identity, identity)
        candidates.append(_prepare_candidate(row, path_rows, transform_by_identity, nodes_by_path,
                                             path_prefix, armature, references))
    original_bone_sha, original_bone_count = _armature_snapshot({})
    weight_sha = _mesh_weight_snapshot()
    original_target_names = tuple(sorted(bone.name for bone in armature.data.bones))
    bpy.context.view_layer.objects.active = armature
    armature.select_set(True)
    bpy.ops.object.mode_set(mode='EDIT')
    edit_bones = armature.data.edit_bones
    for candidate in candidates:
        bone_name = str(candidate['bone_name'])
        if edit_bones.get(bone_name) is not None:
            raise ValueError(f'Avatar leaf appeared on target rig during restoration: {bone_name}')
        parent = edit_bones.get(str(candidate['parent_bone_name']))
        if parent is None:
            raise ValueError(f'Exact parent disappeared before restoration: {candidate["parent_bone_name"]}')
        bone = edit_bones.new(bone_name)
        bone.parent = parent
        bone.use_connect = False
        bone.use_deform = False
        bone.head = parent.head.copy()
        bone.tail = bone.head + Vector((0.0, float(candidate['bone_display_length_meters']), 0.0))
        bone.matrix = Matrix(tuple(tuple(float(component) for component in row)
                                    for row in candidate['planned_rest_matrix']))
    bpy.ops.object.mode_set(mode='OBJECT')
    bpy.context.view_layer.update()
    restored: list[dict[str, object]] = []
    for candidate in candidates:
        name = str(candidate['bone_name'])
        bone = armature.data.bones.get(name)
        if bone is None or bone.parent is None or bone.parent.name != candidate['parent_bone_name']:
            raise AssertionError(f'Restored Bone or exact parent is missing: {armature.name}/{name}')
        if bone.use_deform:
            raise AssertionError(f'Restored leaf must not deform geometry: {armature.name}/{name}')
        planned = Matrix(tuple(tuple(float(component) for component in row)
                               for row in candidate['planned_rest_matrix']))
        rest_error = _matrix_error(bone.matrix_local, planned)
        if rest_error > REST_MATRIX_TOLERANCE:
            raise AssertionError(f'Restored leaf rest differs from exact source rest: bone={name}, error={rest_error}')
        source_path = str(candidate['source_transform_path'])
        bone['source_transform_path'] = source_path
        pose_bone = armature.pose.bones.get(name)
        if pose_bone is None:
            raise AssertionError(f'Restored leaf has no PoseBone: {armature.name}/{name}')
        pose_bone['source_transform_path'] = source_path
        candidate['rest_matrix_after'] = _matrix_values(bone.matrix_local)
        candidate['rest_matrix_max_error'] = rest_error
        restored.append(candidate)
    after_bone_sha, after_bone_count = _armature_snapshot(
        {armature.name: {str(candidate['bone_name']) for candidate in candidates}})
    if after_bone_sha != original_bone_sha or after_bone_count != original_bone_count:
        raise AssertionError(f'Restoration changed an original Armature Bone: before={original_bone_count}, after={after_bone_count}')
    after_weight_sha = _mesh_weight_snapshot()
    if after_weight_sha != weight_sha:
        raise AssertionError('Restoration changed mesh vertex-group weights')
    if tuple(sorted(bone.name for bone in armature.data.bones if bone.name in original_target_names)) != original_target_names:
        raise AssertionError('Restoration changed the original target-rig bone-name set')
    return {
        'format': 'EndfieldUnweightedAvatarLeafRestoration/1',
        'status': 'exact_unweighted_avatar_leaf_bones_restored',
        'armature_name': armature.name,
        'expression_source': str(resolved_table),
        'expression_source_sha256': hashlib.sha256(table_payload).hexdigest(),
        'source_audit': str(resolved_audit),
        'source_audit_sha256': hashlib.sha256(audit_payload).hexdigest(),
        'avatar_identity': {'cab': avatar_identity[0], 'path_id': str(avatar_identity[1])},
        'original_bone_count': original_bone_count,
        'final_bone_count': len(armature.data.bones),
        'original_bones_sha256': original_bone_sha,
        'mesh_vertex_group_weights_sha256_before': weight_sha,
        'mesh_vertex_group_weights_sha256_after': after_weight_sha,
        'mesh_vertex_group_weights_preserved': True,
        'source_renderer_count': len(_array(audit.get('renderers'), 'source audit.renderers')),
        'source_renderer_bone_reference_count': total_renderer_refs,
        'all_source_renderer_bone_references_resolved': True,
        'avatar_to_rig_path_prefix': path_prefix,
        'restored_bones': restored,
        'limits': 'Only exact source leaves with no Avatar or Transform descendants, no renderer m_Bones references, no positive vertex weights, and an exact target-rig parent are restored. No name alias or missing weight is inferred.',
    }


def validate_restored_avatar_leaf_bones(
    armature: bpy.types.Object, report_value: object,
) -> dict[str, object]:
    report = _record(report_value, 'bone restoration receipt')
    if report.get('format') != 'EndfieldUnweightedAvatarLeafRestoration/1':
        raise ValueError('Unsupported unweighted Avatar leaf restoration receipt')
    if report.get('status') == 'no_missing_avatar_bones':
        if report.get('restored_bones') != []:
            raise ValueError('No-op restoration receipt unexpectedly lists bones')
        return {'status': 'no_restored_bones_required', 'validated_bones': 0}
    if report.get('status') != 'exact_unweighted_avatar_leaf_bones_restored':
        raise ValueError(f'Unsuccessful leaf restoration receipt: status={report.get("status")}')
    expected_armature = _text(report.get('armature_name'), 'restoration armature_name')
    if armature.name != expected_armature:
        raise ValueError(f'Restored leaves belong to a different Armature: expected={expected_armature}, actual={armature.name}')
    table_path = Path(_text(report.get('expression_source'), 'restoration expression_source')).resolve(strict=True)
    audit_path = Path(_text(report.get('source_audit'), 'restoration source_audit')).resolve(strict=True)
    if hashlib.sha256(table_path.read_bytes()).hexdigest() != _text(report.get('expression_source_sha256'), 'restoration expression source hash'):
        raise ValueError('Expression source changed after leaf restoration')
    if hashlib.sha256(audit_path.read_bytes()).hexdigest() != _text(report.get('source_audit_sha256'), 'restoration source audit hash'):
        raise ValueError('Source audit changed after leaf restoration')
    rows = [_record(value, f'restored_bones[{index}]')
            for index, value in enumerate(_array(report.get('restored_bones'), 'restored_bones'))]
    if not rows:
        raise ValueError('Successful leaf restoration receipt has no restored bones')
    names = {_text(row.get('bone_name'), 'restored bone.name') for row in rows}
    if len(names) != len(rows):
        raise ValueError('Restoration receipt contains duplicate Bone names')
    original_sha, original_count = _armature_snapshot({armature.name: names})
    if original_sha != _text(report.get('original_bones_sha256'), 'restoration original bone hash'):
        raise AssertionError('An original Armature Bone or rest matrix changed after restoration')
    if original_count != _integer(report.get('original_bone_count'), 'restoration original bone count'):
        raise AssertionError('Original Armature Bone count changed after restoration')
    before = _text(report.get('mesh_vertex_group_weights_sha256_before'), 'restoration weights before')
    after = _text(report.get('mesh_vertex_group_weights_sha256_after'), 'restoration weights after')
    if before != after or _mesh_weight_snapshot() != before:
        raise AssertionError('Mesh vertex-group weights changed during or after restoration')
    validated: list[str] = []
    for index, row in enumerate(rows):
        name = _text(row.get('bone_name'), f'restored_bones[{index}].bone_name')
        bone = armature.data.bones.get(name)
        if bone is None or bone.parent is None or bone.parent.name != row.get('parent_bone_name'):
            raise AssertionError(f'Restored Bone or parent is missing after reopen: {armature.name}/{name}')
        if bone.use_deform:
            raise AssertionError(f'Restored Bone became deforming after reopen: {armature.name}/{name}')
        source_path = _text(row.get('source_transform_path'), f'restored_bones[{index}].source_transform_path')
        if bone.get('source_transform_path') != source_path:
            raise AssertionError(f'Data Bone lost source_transform_path after reopen: {armature.name}/{name}')
        pose_bone = armature.pose.bones.get(name)
        if pose_bone is None or pose_bone.get('source_transform_path') != source_path:
            raise AssertionError(f'PoseBone lost source_transform_path after reopen: {armature.name}/{name}')
        matrix_rows = _array(row.get('rest_matrix_after'), f'restored_bones[{index}].rest_matrix_after')
        expected = Matrix(tuple(tuple(_number(component, f'restored_bones[{index}].matrix') for component in _array(matrix_row, f'restored_bones[{index}].matrix_row')) for matrix_row in matrix_rows))
        error = _matrix_error(bone.matrix_local, expected)
        if error > REST_MATRIX_TOLERANCE:
            raise AssertionError(f'Restored Bone rest matrix changed after reopen: bone={name}, error={error}')
        validated.append(name)
    if len(armature.data.bones) != _integer(report.get('final_bone_count'), 'restoration final bone count'):
        raise AssertionError('Armature Bone count changed after restoration')
    return {'status': 'restored_source_leaves_reopened_and_verified', 'validated_bones': len(validated),
            'bone_names': validated, 'original_bone_snapshot_preserved': True,
            'mesh_vertex_group_weights_preserved': True}
