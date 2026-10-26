"""Build an editable standalone weapon from exact portable source evidence."""
from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys
from typing import Sequence

import bpy
from mathutils import Vector

sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from audit_character_fbx import clear_scene_objects, require_supported_blender
from audit_character_materials import source_geometry_digest, source_geometry_digest_with_modifiers
from character_vfx_vertex_offset import (
    install_vertex_offsets, source_modifiers_without_offsets, verify_vertex_offsets,
    verify_offset_surface_bindings,
)
from blender_scene_materials import layout_shader_node_tree
from character_material_authoring import package_material_authoring, verify_material_authoring
from character_animation_binding import fields, integer, rows, text
from character_geometry import import_character_geometry, import_static_geometry, read_geometry_payload
from character_npr_nodes import ArtistLighting
from character_windows_paths import extended_path, io_path
from preview_character import (
    configure_preview, map_renderers_to_fbx_objects, read_json_object, read_json_value,
    read_renderer_export_names, read_renderer_visibility,
    set_mesh_visibility, source_hierarchy_activity, source_lod_membership,
    source_renderer_rows, verify_renderer_visibility_coverage,
)
from rebuild_character_materials import (
    build_loaded_materials, configure_render_view, file_hash, read_packet,
)


def configure_weapon_preview(visible: Sequence[bpy.types.Object], center: Vector,
                             extent: float, width: int, height: int) -> None:
    """Fit actual projected source bounds, including wide and rotated weapons."""
    configure_render_view('BLENDER_EEVEE_NEXT', Vector((0.3, -1.0, 0.1)),
                          center, extent, width, height, 32)
    scene = bpy.context.scene
    camera = scene.camera
    rotation = camera.rotation_euler.to_quaternion().inverted()
    points = [rotation @ (obj.matrix_world @ Vector(corner) - center)
              for obj in visible for corner in obj.bound_box]
    camera.data.ortho_scale = 1.0
    frame = camera.data.view_frame(scene=scene)
    frame_width = max(abs(point.x) for point in frame)
    frame_height = max(abs(point.y) for point in frame)
    camera.data.ortho_scale = 1.18 * max(max(abs(point.x) for point in points) / frame_width,
                                       max(abs(point.y) for point in points) / frame_height)


def select_weapon_renderers(source: Path, visibility_path: Path, output: Path) -> Path:
    """Resolve all native LOD groups or explicit renderer state when none exist."""
    audit = read_json_object(source / 'source_audit.json')
    if audit['format'] != 'EndfieldCharacterSourceAudit/1':
        raise ValueError(f'Unsupported portable source audit: {source}')
    visibility = read_renderer_visibility(visibility_path, audit)
    root = fields(audit['actual_root'], 'actual_root')
    source_id = text(root['source'], 'actual_root.source')
    root_name = text(root['name'], 'actual_root.name')
    if root['type'] != 'GameObject':
        raise ValueError('Weapon selection requires an actual GameObject source root')
    hierarchy = source_hierarchy_activity(audit)
    transforms: dict[tuple[str, int], dict[str, object]] = {}
    for raw in rows(audit['transforms'], 'source transforms'):
        transform = fields(raw, 'source transform')
        owner = fields(transform['game_object'], 'source transform.game_object')
        key = (text(owner['source'], 'game_object.source'), integer(owner['path_id'], 'game_object.path_id'))
        if key in transforms:
            raise ValueError(f'Duplicate weapon source GameObject transform: identity={key}')
        transforms[key] = transform
    membership: dict[tuple[str, int], int] = {}
    owners: dict[tuple[str, int], str] = {}
    groups: list[dict[str, object]] = []
    group_identities: set[str] = set()
    lod_path = source / 'lod_sources.json'
    for raw in rows(read_json_value(lod_path), 'lod_sources'):
        lod = fields(raw, 'LODGroup source')
        group, group_source, enabled, levels, members = source_lod_membership(lod)
        if group in group_identities or group_source != source_id:
            raise ValueError(f'Duplicate or external weapon LODGroup: identity={group}')
        if not enabled:
            raise ValueError(f'Disabled weapon LODGroup has no verified current LOD selection: identity={group}')
        group_identities.add(group)
        data = fields(lod['data'], 'LODGroup.data')
        owner = fields(data['m_GameObject'], 'LODGroup.m_GameObject')
        owner_key = (group_source, integer(owner['m_PathID'], 'LODGroup.m_GameObject.m_PathID'))
        if integer(owner['m_FileID'], 'LODGroup.m_GameObject.m_FileID') != 0 or owner_key not in transforms:
            raise ValueError(f'Weapon LODGroup owner is external or absent from the source hierarchy: identity={group}, owner={owner_key}')
        transform = transforms[owner_key]
        owner_path = text(transform['path'], 'LODGroup.owner.path')
        if owner_path != root_name and not owner_path.startswith(root_name + '/'):
            raise ValueError(f'Weapon LODGroup owner is outside the selected source root: identity={group}, path={owner_path}')
        overlap = set(membership).intersection(members)
        if overlap:
            raise ValueError(f'Weapon renderer belongs to multiple LODGroups: group={group}, renderers={sorted(overlap)}')
        membership.update(members)
        owners.update({key: group for key in members})
        groups.append({'identity': group, 'owner_transform': transform,
                       'enabled': enabled, 'levels': levels, 'source_document': lod})
    renderer_rows = source_renderer_rows(audit, source_id, membership)
    verify_renderer_visibility_coverage(visibility, renderer_rows)
    names = read_renderer_export_names(source / 'renderer_export_map.json', audit, renderer_rows)
    matches = map_renderers_to_fbx_objects(renderer_rows, visibility, names)
    decisions, visible = set_mesh_visibility(matches, hierarchy)
    detailed_decisions: list[dict[str, object]] = []
    for decision in decisions:
        renderer_source, path_id = decision['renderer_identity'].rsplit(':', 1)
        detailed_decisions.append({**decision, 'lod_group_identity': owners.get((renderer_source, int(path_id)))})
    configure_preview(visible, output / 'preview.png')
    document = {
        'format': 'EndfieldWeaponRendererSelection/1', 'source_root_identity': root,
        'source_audit_sha256': file_hash(source / 'source_audit.json'),
        'lod_sources_sha256': file_hash(lod_path),
        'renderer_visibility_sha256': file_hash(visibility_path),
        'selection_mode': 'native_lod0_per_group' if groups else 'native_renderer_state_without_lod',
        'lod_groups': groups, 'renderer_decisions': detailed_decisions,
    }
    destination = output / 'selection_report.json'
    io_path(destination).write_text(json.dumps(document, indent=2), encoding='utf-8')
    return destination


def build_weapon(source: Path, shader: Path, visibility: Path, output: Path) -> None:
    """Preserve source hierarchy and bind packed material inputs to their exact renderers."""
    if io_path(output).exists():
        raise FileExistsError(f'Weapon build output already exists: {output}')
    io_path(output).mkdir(parents=True)
    require_supported_blender()
    clear_scene_objects()
    geometry = io_path(source / 'model.geometry.json')
    document, _ = read_geometry_payload(geometry)
    skinned = any(fields(mesh, 'mesh')['bones'] for mesh in rows(document['meshes'], 'meshes'))
    objects = import_character_geometry(geometry) if skinned else import_static_geometry(geometry)
    meshes = tuple(obj.name for obj in objects if obj.type == 'MESH')
    if not meshes:
        raise ValueError(f'Portable weapon import must have source meshes: {source}')
    selection = select_weapon_renderers(source, visibility, output)
    before = source_geometry_digest(meshes)
    packet = read_packet(source, shader, selection, visibility)
    vertex_offsets = install_vertex_offsets(packet, bpy.context.scene, frozenset())
    verify_vertex_offsets(vertex_offsets, bpy.context.scene)
    if source_geometry_digest_with_modifiers(meshes, source_modifiers_without_offsets) != before:
        raise ValueError('Weapon vertex offset installation changed immutable source geometry')
    lighting = ArtistLighting((0.4, -0.7, 0.6), 1.0, 0.35, 0.0, 0.2, 0.0)
    built = build_loaded_materials(packet, lighting, layout_shader_node_tree, {}, {})
    parallax_previews = [json.loads(group['source_parallax_preview']) for group in built.groups.values()
                         if 'source_parallax_preview' in group]
    materials = tuple(sorted({slot.material for obj in built.bound_objects
                              for slot in obj.material_slots if slot.material is not None},
                             key=lambda material: material.name))
    authoring = package_material_authoring(materials, layout_shader_node_tree)
    verify_material_authoring(authoring)
    verify_offset_surface_bindings(vertex_offsets, bpy.context.scene)
    offset_validation = verify_vertex_offsets(vertex_offsets, bpy.context.scene)
    if source_geometry_digest_with_modifiers(meshes, source_modifiers_without_offsets) != before:
        raise ValueError('Weapon material binding changed source geometry, UVs or hierarchy')
    visible = tuple(obj for obj in built.bound_objects if not obj.hide_render)
    configure_weapon_preview(visible, built.center, built.extent, 768, 768)
    for light in bpy.context.scene.objects:
        if light.type == 'LIGHT' and light.name.startswith('PREVIEW_ONLY_'):
            light.data.energy *= (built.extent / 2.0) ** 2
    for obj in objects:
        if obj.type == 'EMPTY' and obj.parent is not None:
            obj.hide_set(True)
    scene = bpy.context.scene
    scene['endfield_weapon_source_root'] = json.dumps(read_json_object(source / 'source_audit.json')['actual_root'])
    scene['endfield_weapon_variant_semantics'] = 'Exact selected source prefab; runtime unlock condition unverified'
    scene.render.image_settings.color_mode = 'RGBA'
    for screen in bpy.data.screens:
        for area in screen.areas:
            if area.type == 'VIEW_3D':
                area.spaces.active.overlay.show_relationship_lines = False
                area.spaces.active.region_3d.view_distance = built.extent * 2.5
                area.spaces.active.region_3d.view_location = built.center
                area.spaces.active.shading.type = 'MATERIAL'
    images = tuple(sorted({image.name for image in built.image_cache.values()} |
                          {str(row['texture']) for row in vertex_offsets}))
    for name in images:
        if not bpy.data.images[name].packed_file:
            raise ValueError(f'Weapon source texture is not packed: {name}')
    blend = output / 'weapon.blend'
    bpy.ops.wm.save_as_mainfile(filepath=extended_path(blend))
    report = {
        'format': 'EndfieldWeaponBuild/1', 'status': 'built_pending_reopen',
        'source_root': read_json_object(source / 'source_audit.json')['actual_root'],
        'input_hashes': dict(packet.input_hashes), 'geometry_digests': before,
        'geometry_manifest_sha256': file_hash(source / 'model.geometry.json'),
        'renderers': list(built.renderer_rows), 'materials': authoring,
        'vertex_offsets': vertex_offsets,
        'vertex_offset_validation': offset_validation,
        'parallax_previews': parallax_previews,
        'packed_images': list(images), 'output_blend': str(blend),
        'output_sha256': file_hash(blend), 'output_bytes': io_path(blend).stat().st_size,
        'limitations': [
            'Host NPR/PBR material approximation; native HGRP light loop and parallax are not reproduced.',
            'Inactive source renderers and lower LODs are retained but hidden.',
            'Attachment-only leaf transforms on skinned weapons remain source evidence, not separate Blender controls.',
            'Variant is selected by exact source resource; current runtime unlock condition is unverified.',
        ],
    }
    if vertex_offsets:
        report['limitations'].append(offset_validation['scope'])
        report['limitations'].append('Only source vertex-offset geometry is time-dependent; the VFX surface material remains an editable fixed snapshot at source time zero.')
    if parallax_previews:
        from character_parallax_preview import SCOPE
        report['limitations'].append(SCOPE)
    io_path(output / 'build_receipt.json').write_text(json.dumps(report, indent=2), encoding='utf-8')
    print(json.dumps({'status': report['status'], 'blend': str(blend), 'source_meshes': len(meshes)}))


def audit_weapon(output: Path) -> None:
    """Reopen the saved delivery and verify geometry, packed images and editable inputs."""
    report = read_json_object(output / 'build_receipt.json')
    if report['format'] != 'EndfieldWeaponBuild/1':
        raise ValueError(f'Unsupported weapon build receipt: {output}')
    blend = output / 'weapon.blend'
    if file_hash(blend) != report['output_sha256']:
        raise ValueError(f'Weapon blend differs from its build receipt: {blend}')
    bpy.ops.wm.open_mainfile(filepath=extended_path(blend))
    expected = report['geometry_digests']
    vertex_offsets = report.get('vertex_offsets', [])
    offset_audit = verify_vertex_offsets(vertex_offsets, bpy.context.scene)
    verify_offset_surface_bindings(vertex_offsets, bpy.context.scene)
    if 'vertex_offset_validation' in report and offset_audit != report['vertex_offset_validation']:
        raise ValueError('Reopened weapon vertex-offset evaluated samples differ from the saved build evidence')
    if source_geometry_digest_with_modifiers(tuple(expected), source_modifiers_without_offsets) != expected:
        raise ValueError('Reopened weapon geometry, UVs, source hierarchy or visibility changed')
    for name in report['packed_images']:
        image = bpy.data.images.get(name)
        if image is None or not image.packed_file or min(image.size) < 1:
            raise ValueError(f'Reopened weapon texture is missing or unpacked: {name}')
    materials = verify_material_authoring(report['materials'])
    result = {'format': 'EndfieldWeaponReopen/1', 'status': 'passed',
              'output_sha256': report['output_sha256'], 'materials': materials,
              'source_meshes': len(expected), 'packed_images': len(report['packed_images'])}
    result['vertex_offsets'] = offset_audit
    io_path(output / 'reopen_audit.json').write_text(json.dumps(result, indent=2), encoding='utf-8')
    print(json.dumps(result))


def main(argv: Sequence[str]) -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    actions = parser.add_subparsers(dest='action', required=True)
    build = actions.add_parser('build')
    for name in ('source-dir', 'shader-identities', 'renderer-visibility', 'output-dir'):
        build.add_argument('--' + name, type=Path, required=True)
    audit = actions.add_parser('audit')
    audit.add_argument('--output-dir', type=Path, required=True)
    args = parser.parse_args(list(argv))
    if args.action == 'build':
        build_weapon(args.source_dir, args.shader_identities, args.renderer_visibility, args.output_dir)
    else:
        audit_weapon(args.output_dir)


if __name__ == '__main__':
    main(sys.argv[sys.argv.index('--') + 1:])
