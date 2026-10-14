"""Validate that optional character authoring data belongs to the open scene."""
from __future__ import annotations

import hashlib
import json
from pathlib import Path

import bpy


AvatarIdentity = tuple[str, int]
RENDERER_REFERENCE_PROPERTIES = (
    'source_renderer_identity',
    'candidate_source_renderer_reference',
    'undercoat_source_renderer_reference',
)
CURVE_OBJECT_TYPES = ('CURVE', 'CURVES')


def _record(value: object, context: str) -> dict[str, object]:
    if not isinstance(value, dict) or any(not isinstance(key, str) for key in value):
        raise TypeError(f'Expected object: {context}')
    return value


def _array(value: object, context: str) -> list[object]:
    if not isinstance(value, list):
        raise TypeError(f'Expected array: {context}')
    return value


def _text(value: object, context: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise TypeError(f'Expected nonempty string: {context}')
    return value


def _integer(value: object, context: str) -> int:
    if isinstance(value, bool):
        raise TypeError(f'Expected exact integer: {context}')
    if isinstance(value, int):
        return value
    if isinstance(value, str):
        try:
            return int(value, 10)
        except ValueError as error:
            raise TypeError(f'Expected exact decimal integer: {context}') from error
    raise TypeError(f'Expected exact integer: {context}')


def _read_document(path: Path, context: str) -> tuple[dict[str, object], bytes, Path]:
    resolved = path.resolve(strict=True)
    if not resolved.is_file():
        raise FileNotFoundError(f'Expected {context} file: {resolved}')
    payload = resolved.read_bytes()
    document = _record(json.loads(payload.decode('utf-8-sig')), context)
    return document, payload, resolved


def _source_object_identity(value: object, context: str) -> tuple[AvatarIdentity, str]:
    row = _record(value, context)
    source = _text(row.get('source'), context + '.source')
    path_id = _integer(row.get('path_id'), context + '.path_id')
    return (source.casefold(), path_id), source.casefold() + ':' + str(path_id)


def _audit_avatar_identities(audit: dict[str, object]) -> tuple[list[dict[str, object]], set[AvatarIdentity]]:
    animator_rows = _array(audit.get('animators'), 'source audit.animators')
    records: list[dict[str, object]] = []
    identities: set[AvatarIdentity] = set()
    for index, value in enumerate(animator_rows):
        row = _record(value, f'source audit.animators[{index}]')
        animator_object = _record(row.get('object'), f'source audit.animators[{index}].object')
        if animator_object.get('type') != 'Animator':
            raise ValueError(f'Source audit animator row has an unexpected object type: index={index}')
        avatar = _record(row.get('avatar'), f'source audit.animators[{index}].avatar')
        resolved_value = avatar.get('resolved')
        if resolved_value is None:
            continue
        resolved = _record(resolved_value, f'source audit.animators[{index}].avatar.resolved')
        if resolved.get('type') != 'Avatar':
            raise ValueError(f'Animator Avatar pointer resolved to a non-Avatar object: index={index}, type={resolved.get("type")}')
        identity, canonical = _source_object_identity(resolved, f'source audit.animators[{index}].avatar.resolved')
        identities.add(identity)
        animator_source = _text(animator_object.get('source'), f'source audit.animators[{index}].object.source')
        animator_path_id = _integer(animator_object.get('path_id'), f'source audit.animators[{index}].object.path_id')
        records.append({
            'cab': identity[0],
            'path_id': str(identity[1]),
            'avatar_name': _text(resolved.get('name'), f'source audit.animators[{index}].avatar.resolved.name'),
            'animator_identity': animator_source.casefold() + ':' + str(animator_path_id),
            'animator_name': _text(animator_object.get('name'), f'source audit.animators[{index}].object.name'),
            'resolved_identity': canonical,
        })
    if not identities:
        raise ValueError('Source audit has no resolved Avatar on any Animator.')
    return records, identities


def _table_avatar_identity(document: dict[str, object], context: str) -> AvatarIdentity:
    schema = _text(document.get('schema'), context + '.schema')
    if schema == 'EndfieldCharacterExpressionSource/2':
        selected = _record(document.get('selected_animator_avatar'), context + '.selected_animator_avatar')
        if selected.get('class_name') != 'Avatar':
            raise ValueError(f'{context} selected object is not an Avatar.')
        cab = _text(selected.get('cab'), context + '.selected_animator_avatar.cab')
        path_id = _integer(selected.get('path_id'), context + '.selected_animator_avatar.path_id')
        return cab.casefold(), path_id
    if schema == 'EndfieldCharacterExpressionSource/1':
        raw = _record(document.get('raw'), context + '.raw')
        prefix = _record(raw.get('asset_prefix'), context + '.raw.asset_prefix')
        pointer = _record(prefix.get('avatar'), context + '.raw.asset_prefix.avatar')
        file_id = _integer(pointer.get('m_FileID'), context + '.raw.asset_prefix.avatar.m_FileID')
        path_id = _integer(pointer.get('m_PathID'), context + '.raw.asset_prefix.avatar.m_PathID')
        if path_id == 0:
            raise ValueError(f'{context} Avatar pointer is null.')
        source = _record(document.get('source'), context + '.source')
        serialized_files = _array(source.get('serialized_files'), context + '.source.serialized_files')
        if len(serialized_files) != 1:
            raise ValueError(f'{context} has an ambiguous serialized-file list: count={len(serialized_files)}.')
        serialized_file = _record(serialized_files[0], context + '.source.serialized_files[0]')
        own_cab = _text(serialized_file.get('name'), context + '.source.serialized_files[0].name')
        externals = [_text(item, f'{context}.source.serialized_files[0].externals[{index}]')
                     for index, item in enumerate(_array(serialized_file.get('externals'), context + '.source.serialized_files[0].externals'))]
        if file_id == 0:
            cab = own_cab
        elif 0 < file_id <= len(externals):
            cab = externals[file_id - 1]
        else:
            raise ValueError(f'{context} Avatar FileID is outside its exact serialized externals: file_id={file_id}, external_count={len(externals)}.')
        return cab.casefold(), path_id
    raise ValueError(f'Unsupported expression source schema: {context}.schema={schema}')


def _animation_avatar_identity(document: dict[str, object], context: str) -> AvatarIdentity:
    if document.get('format') != 'EndfieldAnimationExportReceipt/1':
        raise ValueError(f'Unsupported animation receipt format: {context}.format={document.get("format")}')
    if document.get('status') != 'decoded_bound_and_merged_candidate':
        raise ValueError(f'Animation receipt is not a successful bound candidate: {context}.status={document.get("status")}')
    cab = _text(document.get('avatar_cab'), context + '.avatar_cab')
    path_id = _integer(document.get('avatar_path_id'), context + '.avatar_path_id')
    return cab.casefold(), path_id


def _assert_avatar_match(identity: AvatarIdentity, audit_identities: set[AvatarIdentity], context: str) -> int:
    if identity not in audit_identities:
        expected = sorted((cab, str(path_id)) for cab, path_id in audit_identities)
        raise ValueError(f'{context} Avatar does not match any resolved source-audit Animator Avatar: got={identity[0]}/{identity[1]}, expected={expected}.')
    return sum(1 for candidate in audit_identities if candidate == identity)


def _audit_renderer_identities(audit: dict[str, object]) -> set[AvatarIdentity]:
    renderer_rows = _array(audit.get('renderers'), 'source audit.renderers')
    identities: set[AvatarIdentity] = set()
    for index, value in enumerate(renderer_rows):
        row = _record(value, f'source audit.renderers[{index}]')
        identity, _ = _source_object_identity(row.get('object'), f'source audit.renderers[{index}].object')
        if identity in identities:
            raise ValueError(f'Source audit contains duplicate renderer identity: {identity[0]}:{identity[1]}')
        identities.add(identity)
    if not identities:
        raise ValueError('Source audit has no renderer identities.')
    return identities


def _scene_renderer_identity(value: object, object_name: str, property_name: str) -> AvatarIdentity:
    raw = _text(value, f'{object_name}.{property_name}')
    pieces = raw.rsplit(':', maxsplit=1)
    if len(pieces) != 2:
        raise ValueError(f'Mesh renderer reference must be CAB:pathID: object={object_name}, property={property_name}, identity={raw}')
    source = _text(pieces[0], f'{object_name}.{property_name}.source')
    path_id = _integer(pieces[1], f'{object_name}.{property_name}.path_id')
    return source.casefold(), path_id


def _renderer_reference_values(obj: bpy.types.Object) -> tuple[tuple[str, object], ...]:
    return tuple((property_name, obj.get(property_name))
                 for property_name in RENDERER_REFERENCE_PROPERTIES if property_name in obj)


def _object_renderer_identity(obj: bpy.types.Object) -> AvatarIdentity | None:
    reference_values = _renderer_reference_values(obj)
    if not reference_values:
        return None
    identities = tuple(_scene_renderer_identity(value, obj.name, property_name)
                       for property_name, value in reference_values)
    if any(identity != identities[0] for identity in identities[1:]):
        references = [(property_name, value) for property_name, value in reference_values]
        raise ValueError(f'Mesh renderer reference properties disagree: object={obj.name}, references={references}')
    return identities[0]


def _scene_object_named(scene: bpy.types.Scene, name: str, context: str) -> bpy.types.Object:
    target = bpy.data.objects.get(name)
    scene_target = scene.objects.get(name)
    if target is None or scene_target is None or scene_target.as_pointer() != target.as_pointer():
        raise ValueError(f'Explicit scene object link does not resolve in the active scene: context={context}, target={name}')
    return target


def _resolve_renderer_identity(obj: bpy.types.Object, visited_names: tuple[str, ...]) -> AvatarIdentity | None:
    if obj.name in visited_names:
        chain = visited_names + (obj.name,)
        raise ValueError(f'Mesh source_object links contain a cycle: chain={chain}')
    direct_identity = _object_renderer_identity(obj)
    if direct_identity is not None:
        return direct_identity
    if 'source_object' not in obj:
        return None
    source_object_name = _text(obj.get('source_object'), f'{obj.name}.source_object')
    scene = bpy.context.scene
    if scene is None:
        raise RuntimeError('Blender has no active scene for renderer source_object resolution.')
    source_object = _scene_object_named(scene, source_object_name, f'{obj.name}.source_object')
    if source_object.type != 'MESH':
        raise TypeError(f'Mesh source_object link targets a non-mesh object: object={obj.name}, target={source_object.name}, type={source_object.type}')
    return _resolve_renderer_identity(source_object, visited_names + (obj.name,))


def _visible_fur_skin_proxy_links(scene: bpy.types.Scene) -> tuple[tuple[bpy.types.Object, bpy.types.Object], ...]:
    links: list[tuple[bpy.types.Object, bpy.types.Object]] = []
    for curve in scene.objects:
        if curve.type not in CURVE_OBJECT_TYPES or 'fur_skin_proxy' not in curve:
            continue
        if curve.hide_render or not curve.visible_get():
            continue
        proxy_name = _text(curve.get('fur_skin_proxy'), f'{curve.name}.fur_skin_proxy')
        proxy = _scene_object_named(scene, proxy_name, f'{curve.name}.fur_skin_proxy')
        if proxy.type != 'MESH':
            raise TypeError(f'Visible curve fur_skin_proxy link targets a non-mesh object: curve={curve.name}, target={proxy.name}, type={proxy.type}')
        identity = _resolve_renderer_identity(proxy, ())
        if identity is None:
            raise ValueError(f'Visible curve fur_skin_proxy target has no renderer identity through source_object: curve={curve.name}, target={proxy.name}')
        links.append((curve, proxy))
    return tuple(links)


def renderer_reference(obj: bpy.types.Object) -> str:
    """Return a mesh's canonical CAB:pathID through supported properties and exact source_object links."""
    if obj.type != 'MESH':
        raise TypeError(f'Renderer reference requires a mesh object: object={obj.name}, type={obj.type}')
    identity = _resolve_renderer_identity(obj, ())
    if identity is None:
        raise ValueError(f'Mesh has no supported renderer reference property: object={obj.name}')
    return identity[0] + ':' + str(identity[1])


def source_character_meshes() -> tuple[bpy.types.Object, ...]:
    """Return visible source meshes and exact hidden fur proxies referenced by visible curves."""
    scene = bpy.context.scene
    if scene is None:
        raise RuntimeError('Blender has no active scene for authoring identity validation.')
    meshes: list[bpy.types.Object] = []
    included_pointers: set[int] = set()
    for obj in scene.objects:
        if (obj.type == 'MESH' and _object_renderer_identity(obj) is not None
                and not obj.hide_render and obj.visible_get()):
            meshes.append(obj)
            included_pointers.add(obj.as_pointer())
    for _, proxy in _visible_fur_skin_proxy_links(scene):
        pointer = proxy.as_pointer()
        if pointer not in included_pointers:
            meshes.append(proxy)
            included_pointers.add(pointer)
    return tuple(meshes)


def _validate_scene_renderers(audit_identities: set[AvatarIdentity]) -> dict[str, object]:
    scene = bpy.context.scene
    if scene is None:
        raise RuntimeError('Blender has no active scene for authoring identity validation.')
    direct_meshes = tuple(obj for obj in scene.objects
                          if obj.type == 'MESH' and _renderer_reference_values(obj))
    proxy_links = _visible_fur_skin_proxy_links(scene)
    if not direct_meshes and not proxy_links:
        raise ValueError('Open Blender scene has no source-identified mesh objects.')
    scene_rows: list[dict[str, object]] = []
    scene_identities: set[AvatarIdentity] = set()
    reference_property_count = 0
    for obj in direct_meshes:
        identity = _object_renderer_identity(obj)
        if identity is None:
            raise RuntimeError(f'Mesh renderer reference disappeared during validation: object={obj.name}')
        scene_identities.add(identity)
        properties = tuple(property_name for property_name, _ in _renderer_reference_values(obj))
        reference_property_count += len(properties)
        scene_rows.append({
            'object_name': obj.name,
            'renderer_reference': identity[0] + ':' + str(identity[1]),
            'reference_properties': properties,
            'visible': not obj.hide_render and obj.visible_get(),
        })
        if identity not in audit_identities:
            raise ValueError(f'Open Blender source mesh is absent from the selected source audit: object={obj.name}, identity={identity[0]}:{identity[1]}')
    for curve, proxy in proxy_links:
        identity = _resolve_renderer_identity(proxy, ())
        if identity is None:
            raise RuntimeError(f'Fur skin proxy renderer identity disappeared during validation: curve={curve.name}, proxy={proxy.name}')
        scene_identities.add(identity)
        scene_rows.append({
            'object_name': proxy.name,
            'renderer_reference': identity[0] + ':' + str(identity[1]),
            'reference_properties': ('source_object',),
            'visible': False,
            'referenced_by_visible_curve': curve.name,
        })
        if identity not in audit_identities:
            raise ValueError(f'Visible curve fur skin proxy is absent from the selected source audit: curve={curve.name}, proxy={proxy.name}, identity={identity[0]}:{identity[1]}')
    visible_meshes = source_character_meshes()
    if not visible_meshes:
        raise ValueError('Open Blender scene has no visible source-identified mesh objects.')
    missing = audit_identities - scene_identities
    if missing:
        raise ValueError(f'Source audit renderer identities are absent from the open scene: missing_count={len(missing)}, sample={sorted((cab, str(path_id)) for cab, path_id in missing)[:5]}.')
    visible_rows = [
        {
            'object_name': obj.name,
            'renderer_reference': renderer_reference(obj),
            'reference_properties': tuple(property_name for property_name, _ in _renderer_reference_values(obj)),
            'resolved_via_source_object': not bool(_renderer_reference_values(obj)),
        }
        for obj in visible_meshes
    ]
    visible_identities = {
        _resolve_renderer_identity(obj, ())
        for obj in visible_meshes
    }
    scene_mesh_pointers = {obj.as_pointer() for obj in direct_meshes}
    scene_mesh_pointers.update(proxy.as_pointer() for _, proxy in proxy_links)
    return {
        'status': 'all_scene_renderer_references_exactly_match_source_audit_renderers',
        'audit_renderer_count': len(audit_identities),
        'scene_source_mesh_object_count': len(scene_mesh_pointers),
        'scene_renderer_reference_property_count': reference_property_count,
        'visible_fur_skin_proxy_link_count': len(proxy_links),
        'scene_referenced_renderer_count': len(scene_identities),
        'visible_source_mesh_count': len(visible_meshes),
        'visible_source_renderer_count': len(visible_identities),
        'scene_identity_set_exact_match': True,
        'scene_source_meshes': sorted(scene_rows, key=lambda row: (str(row['renderer_reference']), str(row['object_name']))),
        'visible_source_meshes': sorted(visible_rows, key=lambda row: str(row['renderer_reference'])),
    }


def validate_authoring_identity(
    source_audit_path: Path,
    original_table_path: Path | None,
    basic_table_path: Path | None,
    animation_receipts: tuple[Path, ...],
) -> dict[str, object]:
    """Bind optional authoring sources to the exact open character scene and its Animator Avatar."""
    audit, audit_payload, audit_resolved = _read_document(source_audit_path, 'source audit')
    if audit.get('format') != 'EndfieldCharacterSourceAudit/1':
        raise ValueError(f'Unsupported source audit format: {audit_resolved}')
    audit_avatars, avatar_identities = _audit_avatar_identities(audit)
    renderer_identities = _audit_renderer_identities(audit)
    scene_binding = _validate_scene_renderers(renderer_identities)
    source_table_reports: list[dict[str, object]] = []
    for kind, table_path in (('original_table', original_table_path), ('basic_table', basic_table_path)):
        if table_path is None:
            continue
        document, payload, resolved = _read_document(table_path, kind)
        identity = _table_avatar_identity(document, kind)
        matching_animators = _assert_avatar_match(identity, avatar_identities, kind)
        source_table_reports.append({
            'kind': kind,
            'file': resolved.name,
            'sha256': hashlib.sha256(payload).hexdigest(),
            'avatar_cab': identity[0],
            'avatar_path_id': str(identity[1]),
            'matching_animator_avatar_count': matching_animators,
            'status': 'exact_animator_avatar_match',
        })
    animation_reports: list[dict[str, object]] = []
    for index, receipt_path in enumerate(animation_receipts):
        document, payload, resolved = _read_document(receipt_path, f'animation receipt[{index}]')
        identity = _animation_avatar_identity(document, f'animation receipt[{index}]')
        matching_animators = _assert_avatar_match(identity, avatar_identities, f'animation receipt[{index}]')
        animation_reports.append({
            'file': resolved.name,
            'sha256': hashlib.sha256(payload).hexdigest(),
            'avatar_cab': identity[0],
            'avatar_path_id': str(identity[1]),
            'matching_animator_avatar_count': matching_animators,
            'status': 'exact_animator_avatar_match',
        })
    return {
        'format': 'EndfieldCharacterAuthoringIdentity/1',
        'status': 'exact_scene_renderer_and_animator_avatar_identity_verified',
        'source_audit_file': audit_resolved.name,
        'source_audit_sha256': hashlib.sha256(audit_payload).hexdigest(),
        'animator_avatars': audit_avatars,
        'scene_source_meshes': scene_binding,
        'source_tables': source_table_reports,
        'animation_receipts': animation_reports,
        'character_id_used_as_identity_evidence': False,
    }
