"""Install source overlays and mounted fixed display snapshots in character builds."""
from __future__ import annotations

from collections.abc import Mapping
import hashlib
import json
import math
from pathlib import Path, PureWindowsPath
from typing import Literal
from uuid import uuid4

import bpy
from mathutils import Matrix, Quaternion, Vector

from character_runtime.character_effect_attachment import attach_effect_snapshot, validate_effect_attachment
from character_runtime.character_potential_controls import PROPERTY, STATE, install_overlay_control, resolve_renderers
from character_runtime.character_potential_selection import potential_renderer_identities
from character_runtime.character_potential_source import integer, read_document, record, rows, text
from character_runtime.character_windows_paths import io_path


def _verified_manifest(path: Path) -> Mapping[str, object]:
    manifest_path = io_path(path).resolve()
    document = read_document(manifest_path)
    if (document.get('format') != 'EndfieldCharacterPotential/1'
            or document.get('status') != 'source_prepared_requires_blender'):
        raise ValueError(f'Potential build manifest format is unsupported: path={path}')
    for artifact in rows(document.get('artifacts'), 'artifacts'):
        artifact_path = io_path(text(artifact.get('path'), 'artifact.path')).resolve()
        if not artifact_path.is_relative_to(manifest_path.parent):
            raise ValueError(f'Potential artifact escapes preparation: path={artifact_path}')
        if not artifact_path.is_file() or artifact_path.stat().st_size != integer(artifact.get('bytes'), 'artifact.bytes'):
            raise ValueError(f'Potential artifact size changed: path={artifact_path}')
        with io_path(artifact_path).open('rb') as stream:
            digest = hashlib.file_digest(stream, 'sha256').hexdigest()
        if digest != artifact.get('sha256'):
            raise ValueError(f'Potential artifact digest changed: path={artifact_path}')
    return document


def _trs(node: Mapping[str, object]) -> Matrix:
    values: list[tuple[float, ...]] = []
    for field, count in (('position', 3), ('rotation', 4), ('scale', 3)):
        raw = node.get(field)
        if (not isinstance(raw, list) or len(raw) != count
                or any(type(value) not in (int, float) or not math.isfinite(value) for value in raw)):
            raise ValueError(f'Potential source TRS dimensions are invalid: field={field}')
        values.append(tuple(float(value) for value in raw))
    position, rotation, scale = values
    quaternion = Quaternion((rotation[3], *rotation[:3]))
    if abs(quaternion.magnitude - 1.) > 1e-4 or any(abs(value) < 1e-12 for value in scale):
        raise ValueError(f'Potential source transform is degenerate: node={node}')
    return Matrix.LocRotScale(Vector(position), quaternion, Vector(scale))


def _mount_matrix(rig: bpy.types.Object, chain: tuple[Mapping[str, object], ...], follow_scale: int) -> tuple[Matrix, str]:
    source_path = '/'.join(text(node.get('name'), 'ancestry.name') for node in chain[:-1])
    bones = tuple(bone for bone in rig.pose.bones if bone.bone.get('source_transform_path') == source_path)
    if len(bones) != 1:
        raise ValueError(f'Potential parent bone must resolve exactly once: source_path={source_path}, matches={len(bones)}')
    parent = Matrix.Identity(4)
    for node in chain[:-1]:
        parent = parent @ _trs(node)
    flip = Matrix.Diagonal((-1., 1., 1., 1.))
    source_parent = flip @ parent @ flip
    local_slot = flip @ _trs(chain[-1]) @ flip
    bone = bones[0]
    correction = source_parent.inverted() @ bone.bone.matrix_local
    slot_world = rig.matrix_world @ bone.matrix @ correction.inverted() @ local_slot
    if follow_scale == 0:
        location, rotation, scale = slot_world.decompose()
        slot_world = Matrix.LocRotScale(location, rotation, Vector((1., 1., 1.)))
    return slot_world @ flip, source_path


def _visibility(objects: tuple[bpy.types.Object, ...], owner: bpy.types.Object) -> None:
    for obj in objects:
        for field in ('hide_render', 'hide_viewport'):
            driver = obj.driver_add(field).driver
            variable = driver.variables.new()
            variable.name = 'enabled'
            variable.type = 'SINGLE_PROP'
            variable.targets[0].id = owner
            variable.targets[0].data_path = f'["{PROPERTY}"]'
            driver.expression = '1 - enabled'


def _scene_path(document: Mapping[str, object], path: Path) -> Path:
    relative = PureWindowsPath(text(document.get('scene_manifest'), 'scene_manifest'))
    if relative.drive or relative.root or '..' in relative.parts:
        raise ValueError(f'Potential scene manifest must use a safe relative path: path={relative}')
    resolved = io_path(path.parent.joinpath(*relative.parts)).resolve()
    if resolved not in {io_path(text(row.get('path'), 'artifact.path')).resolve()
                        for row in rows(document.get('artifacts'), 'artifacts')}:
        raise ValueError(f'Potential scene manifest has no verified artifact identity: path={resolved}')
    return resolved


def _build_effect(scene: bpy.types.Scene, rig: bpy.types.Object, owner: bpy.types.Object,
                  manifest_path: Path, binding: Mapping[str, object], index: int) -> dict[str, object]:
    from blender_build_map02_chunk_20260727 import import_obj
    from blender_scene_effects import build_scene_effects, _belongs_to_root, _blender_matrix, _owner_matrix_root_relative
    from blender_scene_effect_materials import make_effect_material, make_mesh_effect_material
    from character_npr_vfx import CHARACTER_VFX_SHADER, character_vfx_preview_record
    from endfield_scene_material_contract import load_catalog, reference_key

    effect = record(binding.get('effect'), 'effect')
    chain = rows(binding.get('ancestry'), 'ancestry')
    name = text(effect.get('name'), 'effect.name')
    rotation = integer(effect.get('follow_rotation'), 'follow_rotation')
    scale = integer(effect.get('follow_scale'), 'follow_scale')
    offset = effect.get('offset')
    if rotation not in (0, 1) or scale not in (0, 1):
        raise ValueError(f'Potential display follow flags must be source 0/1: effect={name}')
    if not isinstance(offset, list) or len(offset) != 3 or any(type(value) not in (int, float) for value in offset):
        raise ValueError(f'Potential display offset must contain three numbers: effect={name}')
    if rotation != 1 or any(value != 0 for value in offset):
        return {'effect': name, 'status': 'pending', 'reason': 'Source followRotation=0 or nonzero offset has no verified runtime transform mapping'}
    if scene.camera is None:
        return {'effect': name, 'status': 'pending', 'reason': 'Fixed display snapshots require the export camera'}
    source = read_document(io_path(manifest_path))
    node_rows = rows(source.get('Nodes'), 'Nodes')
    roots = tuple(node for node in node_rows if node.get('IsRoot') is True and node.get('Name') == name)
    if len(roots) != 1:
        raise ValueError(f'Potential configured prefab root must resolve once: effect={name}, matches={len(roots)}')
    root = roots[0]
    if (root.get('LocalPosition') != [0, 0, 0] or root.get('LocalRotation') != [0, 0, 0, 1]
            or root.get('LocalScale') != [1, 1, 1]):
        return {'effect': name, 'status': 'pending', 'reason': 'Nonidentity source prefab root requires explicit authored-root transform support'}
    root_key = (text(root.get('Source'), 'root.Source'), integer(root.get('PathId'), 'root.PathId'))
    nodes = {(text(node.get('Source'), 'Node.Source'), integer(node.get('PathId'), 'Node.PathId')): node for node in node_rows}
    raw_world, parent_path = _mount_matrix(rig, chain, scale)
    basis = Matrix(((1., 0., 0., 0.), (0., 0., -1., 0.), (0., 1., 0., 0.), (0., 0., 0., 1.)))
    post_build = raw_world @ basis.inverted()
    gravity = tuple(raw_world.to_3x3().inverted() @ Vector((0., 0., -9.81)))
    proxy = bpy.data.objects.new('Potential snapshot camera', scene.camera.data.copy())
    scene.collection.objects.link(proxy)
    proxy.matrix_world = post_build.inverted() @ scene.camera.matrix_world
    collection = bpy.data.collections.new('Potential display :: ' + name)
    scene.collection.children.link(collection)
    materials, textures = load_catalog((io_path(manifest_path),))
    age = 1.

    def factory(reference: Mapping[str, object], renderer: Mapping[str, object], system: Mapping[str, object]) -> bpy.types.Material:
        return make_effect_material(materials[reference_key(reference)], textures, age, tuple(renderer['VertexStreams']))

    particles = record(source.get('Particles'), 'Particles')
    unresolved = {reference_key(renderer['Asset']) for renderer in rows(particles.get('Renderers'), 'Particles.Renderers')
                  if renderer.get('RenderMode') == 4 and any(record(slot.get('Mesh'), 'MeshSlot.Mesh').get('ResolutionStatus') != 'resolved'
                      and record(record(slot.get('Mesh'), 'MeshSlot.Mesh').get('Target'), 'Mesh.Target').get('PathId') != 0
                      for slot in rows(renderer.get('MeshSlots'), 'MeshSlots'))}
    excluded = tuple(pair for pair in rows(particles.get('Pairs'), 'Particles.Pairs') if reference_key(pair['Renderer']) in unresolved)
    preview = io_path(manifest_path.parent / f'potential_resolved_{uuid4().hex}.json')
    preview.write_text(json.dumps({**source, 'Particles': {**particles, 'Pairs': [pair for pair in rows(particles.get('Pairs'), 'Pairs') if pair not in excluded]}}, indent=2), encoding='utf-8')
    instance = {'entity_id': index + 1, 'scene_manifest': str(preview.resolve()), 'matrix_space': 'dynamic_effect_anchor',
                'matrix': [1., 0., 0., 0., 0., 1., 0., 0., 0., 0., 1., 0., 0., 0., 0., 1.],
                'prefab_root': {'Source': root_key[0], 'PathId': root_key[1], 'Type': 'GameObject', 'Name': name}}
    try:
        particle_report = build_scene_effects((instance,), collection, factory, age, proxy, 1, 2000, gravity)
    finally:
        preview.unlink()
        camera_data = proxy.data
        bpy.data.objects.remove(proxy, do_unlink=True)
        bpy.data.cameras.remove(camera_data)
    pending: list[dict[str, object]] = []
    direct: list[str] = []
    for node_key, node in nodes.items():
        if node.get('RendererType') not in ('MeshRenderer', 'SkinnedMeshRenderer') or not _belongs_to_root(nodes, root_key, node_key):
            continue
        if node.get('RendererEnabled') is not True:
            continue
        if node.get('RendererType') == 'SkinnedMeshRenderer':
            pending.append({'renderer': node.get('RendererAsset'), 'reason': 'Display skinned renderer deformation is unsupported'})
            continue
        slots = node.get('Materials')
        exports = tuple(row for row in rows(source.get('MeshExports'), 'MeshExports') if reference_key(row['Asset']) == reference_key(node.get('Mesh')))
        if not isinstance(slots, list) or len(slots) != 1 or len(exports) != 1:
            pending.append({'renderer': node.get('RendererAsset'), 'reason': 'Direct display renderer requires one resolved mesh and material'})
            continue
        material_record = materials[reference_key(slots[0])]
        if material_record['Shader']['Name'] == CHARACTER_VFX_SHADER:
            if material_record['Floats']['_EnableNormalMap'] != 0.:
                pending.append({'renderer': node.get('RendererAsset'), 'reason': 'Character display normal requires source-packet binding'})
                continue
            material_record = character_vfx_preview_record(material_record)
        try:
            material = make_mesh_effect_material(material_record, textures, age, exports[0]['VertexColorComponents'])
        except NotImplementedError as error:
            pending.append({'renderer': node.get('RendererAsset'), 'reason': str(error)})
            continue
        limitations = material.get('endfield_fx_limitations')
        if limitations:
            pending.append({'renderer': node.get('RendererAsset'), 'material_limitations': json.loads(limitations)})
        relative = PureWindowsPath(text(exports[0].get('File'), 'MeshExport.File'))
        if relative.drive or relative.root or '..' in relative.parts:
            raise ValueError(f'Potential mesh export path is unsafe: path={relative}')
        imported = import_obj(io_path(manifest_path.parent.joinpath(*relative.parts)))
        for obj in imported:
            for current in tuple(obj.users_collection):
                current.objects.unlink(obj)
            collection.objects.link(obj)
            obj.data.materials.clear()
            obj.data.materials.append(material)
            obj.matrix_world = _blender_matrix(_owner_matrix_root_relative(nodes, root_key, node_key))
            direct.append(obj.name)
    objects = tuple(collection.objects)
    for obj in objects:
        obj.matrix_world = post_build @ obj.matrix_world
        obj['potential_mount_source'] = '/'.join(text(node.get('name'), 'name') for node in chain)
    if objects:
        _visibility(objects, owner)
        anchor = attach_effect_snapshot(rig, parent_path, rotation, scale, objects)
        anchor['source_slot_path'] = '/'.join(text(node.get('name'), 'name') for node in chain)
        anchor['scope'] = 'Fixed emitter snapshot follows source parent; moving emitter history and live slot-scale offsets are not replayed.'
        anchor.hide_set(True)
    return {'effect': name, 'status': 'snapshot_built' if objects else 'pending', 'objects': [obj.name for obj in objects],
            'particles': particle_report, 'direct_meshes': direct, 'pending': pending,
            'unresolved_mesh_pairs': [dict(pair) for pair in excluded if _belongs_to_root(nodes, root_key, reference_key(pair['GameObject']))],
            'snapshot_age_seconds': age}


def install_potential_appearance(scene: bpy.types.Scene, owner: bpy.types.Object, rig: bpy.types.Object,
                                 renderer_selection: object, manifest_path: Path | None,
                                 initial_state: Literal['base', 'maximum']) -> dict[str, object]:
    """Share one optional visibility control across source overlays and display snapshots."""
    if initial_state not in ('base', 'maximum'):
        raise ValueError(f'Potential initial state is unsupported: state={initial_state}')
    if scene.objects.get(owner.name) != owner or scene.objects.get(rig.name) != rig or rig.type != 'ARMATURE':
        raise ValueError('Potential installation requires the actual scene owner and character armature')
    document = _verified_manifest(manifest_path) if manifest_path is not None else None
    existing_images = frozenset(bpy.data.images.keys())
    if document is not None:
        source_root = record(document.get('source_root'), 'source_root')
        selection = record(renderer_selection, 'renderer_selection')
        if selection.get('source_root') != source_root.get('name'):
            raise ValueError('Potential source root differs from the character renderer selection')
        expected_source = text(source_root.get('source'), 'source_root.source').casefold() + ':'
        if any(not text(row.get('renderer_identity'), 'renderer_identity').casefold().startswith(expected_source)
               for row in rows(selection.get('renderer_decisions'), 'renderer_decisions')):
            raise ValueError('Potential source CAB differs from the character renderer selection')
    identities = potential_renderer_identities(renderer_selection)
    overlay_set = frozenset(resolve_renderers(scene, identities)) if identities else frozenset()
    source_overlay_visibility = {obj.name: [bool(obj.hide_render), bool(obj.hide_viewport), bool(obj.hide_get())]
                                 for obj in overlay_set}
    unrelated_visibility = {obj.name: [bool(obj.hide_render), bool(obj.hide_viewport), bool(obj.hide_get())]
                            for obj in scene.objects if obj not in overlay_set}
    if identities:
        overlays = install_overlay_control(scene, owner, identities)
    else:
        if PROPERTY in owner or STATE in owner:
            raise ValueError(f'Potential control is already installed: owner={owner.name}')
        owner[PROPERTY] = False
        owner[STATE] = json.dumps({'format': 'EndfieldPotentialOverlay/1', 'renderers': []})
        owner.id_properties_ui(PROPERTY).update(description='Source potential appearance overlay and fixed display snapshots')
        overlays = ()
    reports: list[dict[str, object]] = []
    if document is not None:
        effects = rows(document.get('effects'), 'effects')
        if effects:
            source = _scene_path(document, manifest_path)
            reports.extend(_build_effect(scene, rig, owner, source, binding, index) for index, binding in enumerate(effects))
    owner[PROPERTY] = initial_state == 'maximum'
    packed_images: list[str] = []
    for image in bpy.data.images:
        if image.name not in existing_images and image.source == 'FILE' and image.users and image.packed_file is None:
            image.pack()
            packed_images.append(image.name)
    owner.update_tag()
    scene.frame_set(scene.frame_current)
    bpy.context.view_layer.update()
    pending = [] if document is not None else ['Source display configuration has not been prepared']
    if document is not None and document.get('unresolved_external_sample'):
        pending.append({'reason': 'Source effect closure has unresolved external files',
                        'sources': document.get('unresolved_external_sample')})
    for report in reports:
        if (report['status'] == 'pending' or report.get('pending') or report.get('unresolved_mesh_pairs')
                or any(row.get('status') not in ('static_preview', 'renderer_disabled', 'no_alive_particles_at_snapshot') for row in report.get('particles', []))):
            pending.append(report)
    applicable = bool(identities or reports)
    result = {'requested': initial_state, 'discovered': {'overlays': len(identities), 'display_effects': len(reports)},
              'applied': {'overlay_objects': [obj.name for obj in overlays], 'display_effects': reports,
                          'control': PROPERTY, 'owner': owner.name, 'overlay_identities': list(identities),
                          'packed_display_images': packed_images},
              'unrelated_visibility': unrelated_visibility,
              'source_overlay_visibility': source_overlay_visibility,
              'pending': pending, 'status': 'partial' if pending else 'applied' if applicable else 'not_applicable',
              'scope': 'Source highest-LOD overlays and fixed emitter display snapshots; game runtime activation and particle history are not replayed.'}
    owner['endfield_potential_build_report'] = json.dumps(result, ensure_ascii=False)
    return result


def _authoring_owner_visibility(owner: bpy.types.Object, expected: object,
                                scene: bpy.types.Scene) -> object:
    """Recognize only the rig workspace's typed hidden-helper presentation contract."""
    from character_rig_authoring import FORMAT, OWNER_PROPERTY, RIG_MARKER_PROPERTY, ROLE_PROPERTY

    helpers = tuple(collection for collection in owner.users_collection
                    if collection.get(RIG_MARKER_PROPERTY) == FORMAT
                    and collection.get(ROLE_PROPERTY) == 'Technical Helpers')
    if not helpers:
        return expected
    if (len(helpers) != 1 or owner.type != 'EMPTY' or owner.get('source_geometry_path') is not None
            or owner.get('source_transform_path') is not None or owner.parent is None
            or owner.parent.type != 'ARMATURE' or scene.objects.get(owner.parent.name) != owner.parent
            or helpers[0].get(OWNER_PROPERTY) != owner.parent.name
            or not isinstance(expected, list) or len(expected) != 3):
        raise ValueError(f'Potential controller has an invalid authoring helper identity: owner={owner.name}')
    return [expected[0], expected[1], True]


def verify_potential_appearance(receipt: object, scene: bpy.types.Scene) -> dict[str, object]:
    """Verify saved drivers, exact mounts, and reversible toggles in the actual scene."""
    if scene != bpy.context.scene:
        raise ValueError('Potential verification requires the actual active saved scene')
    report = record(receipt, 'potential receipt')
    if report.get('status') not in ('applied', 'not_applicable', 'partial'):
        raise ValueError(f'Potential receipt status is unsupported: status={report.get("status")}')
    applied = record(report.get('applied'), 'potential.applied')
    owner_name = text(applied.get('owner'), 'potential.owner')
    owner = scene.objects.get(owner_name)
    if owner is None or PROPERTY not in owner or STATE not in owner:
        raise ValueError(f'Saved potential controller is absent from the actual scene: owner={owner_name}')
    if applied.get('control') != PROPERTY:
        raise ValueError('Saved potential controller property differs from the build receipt')
    names = applied.get('overlay_objects')
    identities = applied.get('overlay_identities')
    if (not isinstance(names, list) or any(type(name) is not str for name in names)
            or not isinstance(identities, list) or any(type(identity) is not str for identity in identities)):
        raise ValueError('Potential saved overlay names and identities must be string arrays')
    overlays = resolve_renderers(scene, tuple(identities)) if identities else ()
    if [obj.name for obj in overlays] != names:
        raise ValueError('Saved potential source overlays differ from the build receipt')
    state = record(json.loads(owner[STATE]), 'overlay state')
    if state.get('format') != 'EndfieldPotentialOverlay/1' or state.get('renderers') != identities:
        raise ValueError('Saved potential overlay source state differs from the receipt')
    effects = rows(applied.get('display_effects'), 'display_effects')
    display_names: list[str] = []
    for effect in effects:
        raw_names = effect.get('objects', [])
        if not isinstance(raw_names, list) or any(type(name) is not str for name in raw_names):
            raise ValueError('Potential display object names must be strings')
        display_names.extend(raw_names)
    all_names = [*names, *display_names]
    if len(all_names) != len(set(all_names)):
        raise ValueError('Saved potential object lists overlap or contain duplicates')
    objects: list[bpy.types.Object] = []
    for name in all_names:
        obj = scene.objects.get(name)
        if obj is None:
            raise ValueError(f'Saved potential object is absent from the actual scene: object={name}')
        objects.append(obj)
        drivers = tuple(obj.animation_data.drivers) if obj.animation_data else ()
        for field in ('hide_render', 'hide_viewport'):
            curves = tuple(curve for curve in drivers if curve.data_path == field)
            if len(curves) != 1:
                raise ValueError(f'Saved potential visibility driver must resolve once: object={name}, field={field}')
            driver = curves[0].driver
            variables = tuple(driver.variables)
            if (driver.type != 'SCRIPTED' or driver.expression != '1 - enabled' or len(variables) != 1
                    or variables[0].name != 'enabled' or variables[0].type != 'SINGLE_PROP'
                    or variables[0].targets[0].id != owner or variables[0].targets[0].data_path != f'["{PROPERTY}"]'):
                raise ValueError(f'Saved potential visibility driver binding differs: object={name}, field={field}')
    for name in display_names:
        obj = scene.objects[name]
        anchor = obj.parent
        if anchor is None or scene.objects.get(anchor.name) != anchor:
            raise ValueError(f'Saved potential display has no actual scene anchor: object={name}')
        rig_name = anchor.get('effect_attachment_rig')
        rig = scene.objects.get(rig_name) if isinstance(rig_name, str) else None
        if rig is None:
            raise ValueError(f'Saved potential display anchor has no actual scene rig: object={name}')
        snapshot_names = json.loads(anchor['effect_snapshot_objects'])
        if not isinstance(snapshot_names, list) or any(type(item) is not str or item not in scene.objects for item in snapshot_names):
            raise ValueError(f'Saved potential anchor references absent objects: anchor={anchor.name}')
        validate_effect_attachment(anchor, rig, tuple(scene.objects[item] for item in snapshot_names))
    unrelated = record(report.get('unrelated_visibility'), 'unrelated_visibility')
    original = owner[PROPERTY]
    if type(original) is not bool or original != (report.get('requested') == 'maximum'):
        raise ValueError('Saved potential initial state differs from the requested appearance')
    try:
        for enabled in (False, True, False, True, original):
            owner[PROPERTY] = enabled
            owner.update_tag()
            scene.frame_set(scene.frame_current)
            bpy.context.view_layer.update()
            if any(obj.hide_render != (not enabled) or obj.hide_viewport != (not enabled) for obj in objects):
                raise ValueError('Saved potential visibility does not follow the shared controller')
            for name, expected in unrelated.items():
                obj = scene.objects.get(name)
                if obj == owner:
                    expected = _authoring_owner_visibility(owner, expected, scene)
                if obj is None or [bool(obj.hide_render), bool(obj.hide_viewport), bool(obj.hide_get())] != expected:
                    raise ValueError(f'Potential toggle changed an unrelated source object visibility: object={name}')
    finally:
        owner[PROPERTY] = original
        owner.update_tag()
        scene.frame_set(scene.frame_current)
        bpy.context.view_layer.update()
    return {'status': 'verified', 'controller': owner_name, 'overlay_objects': len(overlays),
            'display_objects': len(display_names), 'initial_state_restored': True,
            'unrelated_visibility_verified': len(unrelated)}
