"""Build a source-bound static character in one Blender process without rendering.

The mathematical kernels are shared with the frozen research reviews. This
entry saves one final blend; a separate process must verify saved-file fidelity.
"""
from __future__ import annotations

import argparse
from dataclasses import asdict, dataclass
import hashlib
import json
import math
from pathlib import Path
import sys
import time
from typing import Callable

import bpy
from mathutils import Vector

sys.dont_write_bytecode = True
sys.path.insert(0, str(Path(__file__).resolve().parent))

from character_windows_paths import extended_path, io_path
from audit_character_fbx import (
    require_supported_blender, enable_fbx_importer, clear_scene_objects,
    import_character_fbx, inspect_mesh, inspect_armature, test_weighted_joint_deformation, build_validation,
)
from audit_character_materials import source_geometry_digest
from preview_character import (
    read_json_object, read_json_value, read_renderer_visibility, select_root_lod_document,
    source_lod_membership, source_renderer_rows, verify_renderer_visibility_coverage,
    read_renderer_export_names, map_renderers_to_fbx_objects, set_mesh_visibility,
    source_hierarchy_activity, reset_static_bind_pose, configure_preview,
    pack_referenced_mesh_images, write_static_bind_pose_audit,
)
from rebuild_character_materials import (
    SourcePacket, read_packet, build_loaded_materials, configure_render_view, file_hash, SUPPORTED_SHADERS,
)
from character_npr_nodes import ArtistLighting
from review_character_cross_hgrp import build_loaded_hgrp, CharacterFrameSettings
from review_character_hgrp import visible_source_group_names
from review_character_skin_lights import refresh
from patch_character_hgrp import by_label, source_keywords
from attach_skin_shadow import attach_skin_shadow
from attach_skin_specular_light import SpecularLightParameters, attach_skin_specular_light
from character_skin_specular_light import build_skin_specular_light
from attach_skin_special_lights import RampedLightParameters, attach_skin_ramped_light
from character_skin_ramped_light import build_body_skin_ramped_light, build_face_skin_ramped_light
from attach_character_live_shadows import make_proxy_collection, attach_live_shadows
from character_shadow_proxy_alpha import read_shadow_alpha_evidence, attach_shadow_proxy_alpha
from review_character_live_shadows import shadow_fields, field_summary
from character_late_materials import attach_supported_late_materials
from character_wetfilm_profile import attach_profile_wetfilm
from character_standard_snow_profile import attach_profile_standard_snow
from character_hair_fur_snow_profile import attach_profile_hair_fur_snow
from character_fur_curves_profile import (
    attach_prepared_fur_curves, attach_profile_fur_curves,
    read_fur_curves_build_profile, read_fur_curves_preparation,
)
from character_material_profile import validate_profile_shader_identities

from character_skin_weather_profile import attach_profile_skin_rain
from character_weather_per_draw import WeatherPerDrawSource, make_weather_per_draw_source
from character_weather_nodes import WeatherOverride, WeatherUniforms, make_weather_controls, make_weather_controls_from_per_draw


WEATHER_PER_DRAW_FORMAT = 'EndfieldNativePerDrawWeatherInput/1'


@dataclass(frozen=True)
class WeatherPerDrawInput:
    path: Path
    byte_count: int
    sha256: str
    source: WeatherPerDrawSource


def read_weather_per_draw_input(path: Path | None) -> WeatherPerDrawInput | None:
    """Read and validate one caller-supplied raw native weather word."""
    if path is None:
        return None
    source_path = io_path(path).resolve(strict=True)
    if not source_path.is_file():
        raise ValueError(f'Native per-draw weather input must be a file: {source_path}')
    raw_bytes = source_path.read_bytes()
    digest = hashlib.sha256(raw_bytes).hexdigest()
    try:
        document = json.loads(raw_bytes.decode('utf-8'))
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise ValueError(f'Native per-draw weather input is invalid JSON: path={source_path}; error={error}') from error
    if not isinstance(document, dict) or any(not isinstance(key, str) for key in document):
        raise TypeError(f'Native per-draw weather input must be a JSON object: {source_path}')
    required = ('format', 'packed_word_bits', 'wet_effect_world_space_height', 'provenance')
    missing = tuple(name for name in required if name not in document)
    if missing:
        raise ValueError(f'Native per-draw weather input is missing required fields: path={source_path}; fields={missing}')
    if document['format'] != WEATHER_PER_DRAW_FORMAT:
        raise ValueError(f'Unsupported native per-draw weather input format: path={source_path}; format={document["format"]!r}')
    word = document['packed_word_bits']
    if type(word) is not int:
        raise TypeError(f'Native per-draw packed_word_bits must be a JSON uint32 integer: path={source_path}')
    height = document['wet_effect_world_space_height']
    if type(height) not in (int, float):
        raise ValueError(f'Native per-draw wet_effect_world_space_height must be a finite scalar: path={source_path}')
    try:
        height_value = float(height)
    except OverflowError as error:
        raise ValueError(f'Native per-draw wet_effect_world_space_height cannot be represented: path={source_path}') from error
    if not math.isfinite(height_value):
        raise ValueError(f'Native per-draw wet_effect_world_space_height must be finite: path={source_path}')
    provenance = document['provenance']
    if not isinstance(provenance, str) or not provenance.strip():
        raise ValueError(f'Native per-draw provenance must be a nonempty string: path={source_path}')
    try:
        source = make_weather_per_draw_source(word, height_value, provenance)
    except TypeError as error:
        raise TypeError(f'Native per-draw input types failed validation: path={source_path}; error={error}') from error
    except ValueError as error:
        raise ValueError(f'Native per-draw input values failed validation: path={source_path}; error={error}') from error
    return WeatherPerDrawInput(source_path, len(raw_bytes), digest, source)


def weather_per_draw_receipt(
    weather_input: WeatherPerDrawInput | None,
    family_inventory: list[dict[str, object]],
) -> dict[str, object]:
    """Describe the explicit source bytes, decoded values and routing scope."""
    if weather_input is None:
        return {'status': 'not_requested', 'family_inventory': []}
    source = weather_input.source
    channels = source.channels.normalized()
    return {
        'status': 'loaded_explicit_source_not_runtime_capture',
        'format': WEATHER_PER_DRAW_FORMAT,
        'path': str(weather_input.path),
        'bytes': weather_input.byte_count,
        'sha256': weather_input.sha256,
        'packed_word_bits': source.packed_word_bits,
        'packed_word_bits_hex': f'0x{source.packed_word_bits:08x}',
        'channels_u8': {
            'rain': source.channels.rain,
            'wet': source.channels.wet,
            'global_wet': source.channels.global_wet,
            'snow': source.channels.snow,
        },
        'channels_normalized': {
            'rain': channels[0],
            'wet': channels[1],
            'global_wet': channels[2],
            'snow': channels[3],
        },
        'wet_effect_world_space_height_float32': source.wet_effect_world_space_height,
        'wet_effect_world_space_height_float32_bits_hex': f'0x{source.wet_effect_world_space_height_bits:08x}',
        'provenance': source.provenance,
        'runtime_capture_verified': False,
        'family_inventory': family_inventory,
    }


def weather_family_inventory(
    packet: SourcePacket,
    wetfilm_receipt: dict[str, object],
    late_materials: list[dict[str, object]],
    skin_weather_receipt: dict[str, object],
    hair_fur_snow_receipt: dict[str, object],
) -> list[dict[str, object]]:
    """Inventory observed shader and keyword families against attached consumers."""
    wet_groups_value = wetfilm_receipt.get('groups', [])
    if not isinstance(wet_groups_value, list) or any(not isinstance(name, str) for name in wet_groups_value):
        raise TypeError('Standard wet-film family inventory requires a valid group list')
    wet_groups = frozenset(wet_groups_value)
    late_silk_groups = frozenset(
        row['group'] for row in late_materials
        if isinstance(row.get('group'), str) and row.get('status') == 'connected' and row.get('silk') is not None
    )
    skin_bindings = skin_weather_receipt.get('bindings', [])
    if not isinstance(skin_bindings, list):
        raise TypeError('Skin weather family inventory requires a valid binding list')
    skin_identities: set[tuple[str, int]] = set()
    for raw_binding in skin_bindings:
        if not isinstance(raw_binding, dict):
            raise TypeError('Skin weather binding inventory row must be an object')
        source_identity = raw_binding.get('source_identity')
        if (not isinstance(source_identity, list) or len(source_identity) != 2
                or not isinstance(source_identity[0], str) or type(source_identity[1]) is not int):
            raise ValueError('Skin weather binding inventory has an invalid source identity')
        skin_identities.add((source_identity[0].casefold(), source_identity[1]))
    hair_fur_bindings = hair_fur_snow_receipt.get('bindings', [])
    if not isinstance(hair_fur_bindings, list) or any(not isinstance(row, dict) or not isinstance(row.get('group'), str) for row in hair_fur_bindings):
        raise TypeError('Hair/Fur weather inventory requires valid binding groups')
    hair_fur_groups = frozenset(row['group'] for row in hair_fur_bindings)
    skin_source_kind = skin_weather_receipt.get('weather_source_kind', 'explicit_dry_artist_values')
    materials = packet.materials
    visible = packet.visible_materials
    from review_character_hgrp import visible_source_group_names

    visible_group_names = visible_source_group_names()
    groups_by_identity: dict[tuple[str, int], list[bpy.types.ShaderNodeTree]] = {}
    for group in bpy.data.node_groups:
        if group.name not in visible_group_names or not isinstance(group.get('source_identity'), str):
            continue
        identity = json.loads(group['source_identity'])
        if not isinstance(identity, list) or len(identity) != 2 or not isinstance(identity[0], str) or type(identity[1]) is not int:
            raise ValueError(f'Visible source group identity is malformed: {group.name}')
        key = (identity[0].casefold(), identity[1])
        groups_by_identity.setdefault(key, []).append(group)

    inventory: list[dict[str, object]] = []
    for material in materials:
        identity = material.identity
        key = (identity[0].casefold(), identity[1])
        groups = tuple(groups_by_identity.get(key, ()))
        group_names = tuple(sorted(group.name for group in groups))
        if key in visible and not groups:
            raise ValueError(f'Visible source material has no matching Blender source group: {identity}')
        keywords = tuple(material.keywords)
        for group in groups:
            if group.get('source_shader') != material.shader:
                raise ValueError(f'Source material and Blender shader family disagree: {identity}/{group.name}')
            group_keywords = source_keywords(group)
            if frozenset(group_keywords) != frozenset(keywords):
                raise ValueError(f'Source material and Blender keyword families disagree: {identity}')
        if groups:
            keywords = source_keywords(groups[0])
        consumers: list[str] = []
        if set(group_names) & wet_groups:
            consumers.append('standard_wetfilm')
        if set(group_names) & late_silk_groups:
            consumers.append('silk_surface')
        if key in skin_identities:
            consumers.append('skin_compiled_weather_surface')
        if set(group_names) & hair_fur_groups:
            consumers.append('hair_fur_compiled_snow_surface')
        if key not in visible:
            disposition, reason = 'excluded_inactive_material', 'Source material is not visible in the selected renderer set.'
        elif material.shader == 'HGRP/CharacterNPR' and '_CHARACTER_FUR' not in keywords:
            if consumers:
                disposition, reason = 'routed_to_non_fur_standard_consumers', 'Exact HGRP/CharacterNPR shader and non-Fur keyword family.'
            else:
                disposition, reason = 'eligible_family_without_attached_consumer', 'Exact non-Fur Standard family, but no configured weather consumer is attached.'
        elif material.shader == 'HGRP/CharacterNPR_Skin':
            if key in skin_identities and skin_source_kind == 'explicit_native_per_draw_word':
                disposition, reason = (
                    'routed_to_exact_skin_per_draw_consumer',
                    'Exact reflected non-SDF Skin keyword variant; input bytes are explicit and are not a runtime capture.',
                )
            elif key in skin_identities:
                disposition, reason = (
                    'routed_to_skin_artist_controls',
                    'Compiled non-SDF Skin surface retains its independent dry artist controls.',
                )
            else:
                disposition, reason = (
                    'excluded_skin_variant_or_consumer_unverified',
                    'Skin source variant is outside the exact reflected non-SDF per-draw consumer route.',
                )
        elif '_CHARACTER_FUR' in keywords or material.shader == 'HGRP/CharacterNPR_Hair':
            if set(group_names) & hair_fur_groups:
                disposition, reason = 'routed_to_exact_hair_fur_snow_consumer', 'Exact configured source keyword variant; explicit offline weather, not runtime capture.'
            else:
                disposition, reason = 'excluded_fur_or_hair_variant', 'No snow consumer for this exact Hair/Fur keyword variant.'
        else:
            disposition, reason = 'excluded_shader_family_unverified', 'No verified Standard per-draw consumer mapping exists for this shader family.'
        inventory.append({
            'source_identity': f'{identity[0]}:{identity[1]}',
            'source_name': material.name,
            'shader': material.shader,
            'keywords': list(keywords),
            'visible': key in visible,
            'source_groups': list(group_names),
            'consumers': consumers,
            'disposition': disposition,
            'reason': reason,
        })
    return inventory


def artifact(path: Path) -> dict[str, object]:
    """Describe an actual immutable input or output file."""
    return {'path': str(path.resolve(strict=True)), 'bytes': io_path(path).stat().st_size, 'sha256': file_hash(path)}


def import_source_geometry(fbx: Path) -> dict[str, object]:
    """Retain the existing importer repair and real reversible skin validation."""
    require_supported_blender()
    enable_fbx_importer()
    clear_scene_objects()
    objects, repairs = import_character_fbx(Path(extended_path(fbx)))
    meshes = tuple(obj for obj in objects if obj.type == 'MESH')
    rigs = tuple(obj for obj in objects if obj.type == 'ARMATURE')
    if not meshes or not rigs:
        raise ValueError('Character FBX must contain source meshes and an editable armature')
    records = []
    counts: dict[str, dict[str, int]] = {}
    for obj in meshes:
        record, group_counts = inspect_mesh(obj)
        records.append(record)
        counts[obj.name] = group_counts
    deformation = test_weighted_joint_deformation(rigs, meshes, counts)
    validation = build_validation(records, deformation, False)
    failed = tuple(name for name, valid in validation.items() if name != 'saved_blend_reopened' and not valid)
    if failed:
        raise ValueError('Imported source geometry/skin validation failed: ' + repr(failed))
    return {'mesh_count': len(meshes), 'meshes': records,
            'armatures': [inspect_armature(rig) for rig in rigs],
            'nested_source_skin_recoveries': list(repairs),
            'deformation_test': deformation, 'validation': validation, 'saved_blend_reopened': False}


def prepare_source_selection(args: argparse.Namespace) -> tuple[Path, Path]:
    """Use exact source LOD, hierarchy and renderer visibility on the imported scene."""
    source = args.source_dir / 'source_audit.json'
    export_map = args.source_dir / 'renderer_export_map.json'
    lod = args.source_dir / 'lod_sources.json'
    audit = read_json_object(source)
    if audit['format'] != 'EndfieldCharacterSourceAudit/1':
        raise ValueError('Unsupported source character audit format')
    visibility = read_renderer_visibility(args.renderer_visibility, audit)
    document = select_root_lod_document(read_json_value(lod), audit)
    group_identity, source_id, group_enabled, levels, membership = source_lod_membership(document)
    rows = source_renderer_rows(audit, source_id, membership)
    shadow_only = verify_renderer_visibility_coverage(visibility, rows)
    names = read_renderer_export_names(export_map, audit, rows)
    matches = map_renderers_to_fbx_objects(rows, visibility, names)
    decisions, visible = set_mesh_visibility(matches, source_hierarchy_activity(audit))
    pose_path = args.output_dir / 'static_bind_pose_audit.json'
    pose = reset_static_bind_pose(bpy.context.scene, args.fbx, visible)
    write_static_bind_pose_audit(pose_path, pose)
    # This prepares real camera/light fixtures but deliberately creates no PNG.
    fixtures, bounds = configure_preview(visible, args.output_dir / 'preview.png')
    packed = pack_referenced_mesh_images()
    selection = {'format': 'CharacterLodPreviewSelection/2', 'source_blend': str(args.fbx),
                 'source_audit': str(source), 'renderer_export_map': str(export_map),
                 'lod_sources': str(lod), 'renderer_visibility': str(args.renderer_visibility),
                 'source_root': audit['actual_root']['name'], 'static_bind_pose_audit': str(pose_path),
                 'lod_group_identity': group_identity, 'lod_group_enabled': group_enabled,
                 'lod_levels': levels, 'renderer_decisions': decisions,
                 'summary': {'source_renderer_count': len(decisions),
                             'visible_renderer_count': len(visible),
                             'hidden_renderer_count': len(decisions) - len(visible),
                             'matched_fbx_mesh_count': len(matches),
                             'native_shadows_only_renderer_count': shadow_only,
                             'preview_only_object_count': len(fixtures), 'packed_image_count': len(packed)},
                 'bounds': {'minimum': dict(zip(('x','y','z'), bounds[0])),
                            'maximum': dict(zip(('x','y','z'), bounds[1]))},
                 'packed_images': packed, 'preview_only_objects': fixtures,
                 'status': 'source_selected_not_rendered', 'rendered': False, 'saved_blend_reopened': False}
    path = args.output_dir / 'selection_report.json'
    path.write_text(json.dumps(selection, indent=2), encoding='utf-8')
    return path, pose_path


def attach_frozen_skin_modes(layout: Callable[[bpy.types.NodeTree], None]) -> dict[str, object]:
    """Connect the existing screen-shadow, SpecularOnly and Ramped factories."""
    visible = visible_source_group_names()
    skins = tuple(tree for tree in bpy.data.node_groups
                  if tree.name in visible and tree.get('source_shader') == 'HGRP/CharacterNPR_Skin')
    if not skins:
        return {'status': 'not_applicable', 'reason': 'No visible source Skin material family',
                'visible_skin_groups': [], 'shadows': [], 'specular_only': [], 'ramped': []}
    states = {by_label(tree, 'Explicit HGRP state and preview inputs').node_tree for tree in skins}
    if len(states) != 1:
        raise ValueError('Frozen Skin build requires one explicit shared HGRP state')
    state = next(iter(states))
    if any(by_label(state, label).outputs[0].default_value != 1.
           for label in ('Cast Shadow', 'Preview Atlas Visibility')):
        raise ValueError('Source screen-shadow consumers must start with raw R/G equal to one')
    point = bpy.data.objects['EF Fill Point']
    spot = bpy.data.objects['EF Fill Spot']
    plain = by_label(skins[0], 'Plain Skin response ' + point.name).node_tree
    spot_plain = by_label(skins[0], 'Plain Skin response ' + spot.name).node_tree
    if plain is None or spot_plain is None:
        raise ValueError('Existing source plain light kernels are missing')
    shadows = [attach_skin_shadow(tree, 1., 0.) for tree in skins]
    specular = build_skin_specular_light('EF Compiled Skin SpecularOnly Light', plain)
    specular_rows = [attach_skin_specular_light(tree, point, specular, SpecularLightParameters(1., .6, 0.)) for tree in skins]
    body = build_body_skin_ramped_light('EF Source Body ramped', spot_plain)
    face = build_face_skin_ramped_light('EF Source Face ramped', spot_plain)
    ramped = [attach_skin_ramped_light(tree, spot, face if '_SDFLIGHTMAP' in source_keywords(tree) else body,
                                      RampedLightParameters(.15, .35, .3, .5)) for tree in skins]
    for tree in bpy.data.node_groups:
        if tree.bl_idname == 'ShaderNodeTree':
            layout(tree)
    return {'shadows': shadows, 'specular_only': specular_rows, 'ramped': ramped}


def packed_image_records(images: tuple[bpy.types.Image, ...]) -> list[dict[str, object]]:
    rows = []
    for image in images:
        if image.packed_file is None or not image.has_data:
            raise ValueError('Source image is missing packed pixels: ' + image.name)
        rows.append({'name': image.name, 'sha256': hashlib.sha256(image.packed_file.data).hexdigest(),
                     'dimensions': list(image.size), 'color_space': image.colorspace_settings.name,
                     'alpha_mode': image.alpha_mode})
    return rows


def attach_source_shadows(args: argparse.Namespace) -> dict[str, object]:
    """Create source-identity-selected proxies and retain pre-modifier source digests."""
    from source_caster_manifest import build_source_caster_manifest, write_source_caster_manifest
    manifest = build_source_caster_manifest(args.source_dir / 'source_audit.json', args.renderer_visibility,
                                             args.shader_identities, args.source_dir / 'material_sources.json')
    objects = tuple(obj for obj in bpy.data.objects if obj.type == 'MESH' and 'source_renderer_identity' in obj)
    names = tuple(obj.name for obj in objects)
    transforms = {obj.name: [tuple(tuple(row) for row in obj.matrix_basis),
                            tuple(tuple(row) for row in obj.matrix_parent_inverse),
                            obj.parent.name if obj.parent else ''] for obj in objects}
    visibility = {obj.name: [obj.hide_render, obj.hide_viewport, obj.hide_get()] for obj in objects}
    missing = set(manifest.selection.accepted_identities) - {obj['source_renderer_identity'] for obj in objects}
    if missing:
        raise ValueError('Source-selected casters absent from imported scene: ' + repr(sorted(missing)))
    sun = bpy.data.objects['EF HGRP Main Sun']
    if sun.type != 'LIGHT' or sun.data.type != 'SUN':
        raise ValueError('Frozen source shadow bridge requires the actual bound Sun')
    casters = make_proxy_collection(objects, manifest.selection)
    evidence = read_shadow_alpha_evidence(args.shader_identities, args.alpha_audit)
    alpha = tuple(attach_shadow_proxy_alpha(obj, evidence, 2, 42) for obj in casters.objects)
    refresh()
    before = source_geometry_digest(names)
    collection = bpy.data.collections.new('EF Main Environment Casters')
    bpy.context.scene.collection.children.link(collection)
    points = tuple(obj.matrix_world @ Vector(corner) for obj in objects if not obj.hide_render for corner in obj.bound_box)
    low = Vector(tuple(min(point[axis] for point in points) for axis in range(3)))
    high = Vector(tuple(max(point[axis] for point in points) for axis in range(3)))
    extent = max(high - low)
    receivers = attach_live_shadows(objects, sun, collection, casters, .003, extent * 8., 0)
    refresh()
    fields = shadow_fields(receivers)
    path = args.output_dir / 'caster_selection.json'
    write_source_caster_manifest(path, manifest)
    return {'source_geometry_before_live_shadows': before, 'source_geometry_digest_basis': 'source_local_parent_chain/1', 'source_transforms': transforms,
            'source_visibility': visibility, 'receiver_names': receivers,
            'shadow_point_counts': field_summary(fields),
            'shadow_fields_sha256': hashlib.sha256(json.dumps(fields, sort_keys=True, separators=(',', ':')).encode()).hexdigest(),
            'selection': asdict(manifest.selection), 'alpha_casters': alpha, 'caster_selection': artifact(path),
            'receiver_subdivision': 0, 'bias_world_units': .003, 'center': list((low + high) * .5), 'extent': extent}


def main(args: argparse.Namespace) -> None:
    started = time.perf_counter()
    from character_material_variant import read_material_options, native_weather_required, attach_full_response_simple_weather, apply_material_variant
    material_options = read_material_options(args.material_options)
    variant_options = material_options if material_options is not None and material_options.mode == 'simplified' else None
    weather_input = read_weather_per_draw_input(args.weather_per_draw_input)
    fur_curves_profile = read_fur_curves_build_profile(args.fur_curves_profile)
    fur_curves_preparation = read_fur_curves_preparation(args.fur_curves_preparation)
    if io_path(args.output_dir).exists():
        raise FileExistsError('Build output already exists: ' + str(args.output_dir))
    for name in ('fbx', 'shader_identities', 'renderer_visibility', 'profile', 'alpha_audit'):
        path = getattr(args, name)
        if not io_path(path).is_file():
            raise FileNotFoundError('Required character build input absent: ' + name + '/' + str(path))
    validate_profile_shader_identities(args.profile, args.shader_identities)
    sys.path.insert(0, str(args.repository_src))
    from blender_scene_materials import layout_shader_node_tree
    io_path(args.output_dir).mkdir(parents=True)
    stage_times: dict[str, float] = {}
    mark = time.perf_counter()
    imported = import_source_geometry(args.fbx)
    stage_times['import_and_skin_validation'] = time.perf_counter() - mark
    mark = time.perf_counter()
    selection, pose = prepare_source_selection(args)
    stage_times['source_selection_and_bind_pose'] = time.perf_counter() - mark
    mark = time.perf_counter()
    packet = read_packet(args.source_dir, args.shader_identities, selection, args.renderer_visibility)
    materials = build_loaded_materials(packet, ArtistLighting((.7,-1.,.6),.9,.15,0.,.25,.75), layout_shader_node_tree)
    stage_times['source_materials'] = time.perf_counter() - mark
    mark = time.perf_counter()
    frames = CharacterFrameSettings(args.head_bone, tuple(args.face_right), tuple(args.face_up),
                                    tuple(args.face_forward), tuple(args.hair_x), tuple(args.hair_y), tuple(args.hair_z))
    hgrp = build_loaded_hgrp(args.profile, args.shader_identities, frames, layout_shader_node_tree)
    hgrp.point_light.hide_render = False
    hgrp.spot_light.hide_render = False
    refresh()
    stage_times['frozen_hgrp'] = time.perf_counter() - mark
    mark = time.perf_counter()
    modes = attach_frozen_skin_modes(layout_shader_node_tree)
    from character_emotion_atlas import attach_visible_emotion_atlases
    emotion_atlas = attach_visible_emotion_atlases(layout_shader_node_tree)
    stage_times['frozen_skin_modes'] = time.perf_counter() - mark
    authoring = None
    if args.authoring_request is not None:
        from character_authoring import apply_authoring, copy_original_table, read_authoring_request
        mark = time.perf_counter()
        authoring_request = read_authoring_request(args.authoring_request)
        authoring = apply_authoring(authoring_request, None)
        authoring['original_table'] = copy_original_table(authoring_request, args.output_dir)
        stage_times['optional_authoring'] = time.perf_counter() - mark
    refresh()
    for tree in bpy.data.node_groups:
        if tree.bl_idname == 'ShaderNodeTree':
            layout_shader_node_tree(tree)
    if weather_input is not None:
        weather_controls = make_weather_controls_from_per_draw(
            weather_input.source, WeatherOverride(0.0, (0.0, 0.0, 0.0, 0.0), 0.0), layout_shader_node_tree)
        weather_controls['source_per_draw_input_sha256'] = weather_input.sha256
    else:
        weather_controls = make_weather_controls(
            WeatherUniforms((0.0, 0.0, 0.0, 0.0), 0.0, 0.0, 0.0,
                            'Explicit shared artist weather controls; dry defaults, not captured native state'),
            WeatherOverride(1.0, (0.0, 0.0, 0.0, 0.0), 0.0), layout_shader_node_tree)
        weather_controls['source_input_kind'] = 'explicit_dry_artist_values'
        weather_controls['endfield_character_weather_controls'] = True
    late_materials = attach_supported_late_materials(layout_shader_node_tree, weather_controls, hgrp.extent)
    if native_weather_required(material_options):
        wetfilm = attach_profile_wetfilm(args.profile, layout_shader_node_tree, weather_controls)
        standard_snow = attach_profile_standard_snow(args.profile, layout_shader_node_tree, wetfilm)
        skin_rain = attach_profile_skin_rain(
            args.profile, layout_shader_node_tree,
            weather_input.source if weather_input is not None else None,
            weather_input.sha256 if weather_input is not None else None,
            standard_snow,
        )
        hair_fur_snow = attach_profile_hair_fur_snow(args.profile, weather_controls, layout_shader_node_tree)
    else:
        wetfilm = {'status': 'not_requested', 'groups': []}
        standard_snow = {'status': 'not_requested', 'groups': []}
        skin_rain = {'status': 'not_requested', 'bindings': []}
        hair_fur_snow = {'status': 'not_requested', 'groups': []}
    prepared_variant_weather = attach_full_response_simple_weather(bpy.context.scene, variant_options) if variant_options is not None else []
    mark = time.perf_counter()
    if fur_curves_preparation is not None:
        fur_curves_snow = attach_prepared_fur_curves(
            fur_curves_preparation, args.source_dir, args.profile, weather_controls, layout_shader_node_tree)
    else:
        fur_curves_snow = attach_profile_fur_curves(
            fur_curves_profile, args.source_dir, args.profile, weather_controls, layout_shader_node_tree)
    stage_times['optional_fur_curves_snow'] = time.perf_counter() - mark
    mark = time.perf_counter()
    shadows = attach_source_shadows(args)
    stage_times['source_live_shadow_bridge'] = time.perf_counter() - mark
    # Match the delivered final sample's camera and default engine settings.
    center = Vector(shadows['center'])
    extent = float(shadows['extent']) * 1.15
    configure_render_view('BLENDER_EEVEE_NEXT', Vector((0.,-1.,.02)), center, extent, 600, 800, 24)
    configure_render_view('CYCLES', Vector((0.,-1.,.02)), center, extent, 600, 800, 24)
    bpy.context.scene.render.engine = 'BLENDER_EEVEE_NEXT'
    refresh()
    rigs = tuple(obj for obj in bpy.data.objects if obj.type == 'ARMATURE')
    if len(rigs) != 1 or args.head_bone not in rigs[0].pose.bones or rigs[0].data.pose_position != 'POSE':
        raise ValueError('Final build requires the unambiguous actual armature/head in editable POSE mode')
    from character_authoring_workspace import prepare_authoring_workspace
    mark = time.perf_counter()
    workspace = prepare_authoring_workspace(
        args.source_dir, authoring_request.animations if authoring is not None else ())
    stage_times['authoring_workspace'] = time.perf_counter() - mark
    material_variant = apply_material_variant(bpy.context.scene, variant_options, prepared_variant_weather) if variant_options is not None else None
    if material_variant is not None:
        from character_weather_panel import attach_weather_panel
        workspace['weather_panel'] = attach_weather_panel(tuple(slot.material for obj in bpy.context.scene.objects if obj.type == 'MESH' for slot in obj.material_slots if slot.material is not None))
    output = args.output_dir / 'character.blend'
    if authoring is not None:
        from character_authoring_validation import character_invariants, material_signature_details
        authoring['preserved'] = character_invariants(tuple(authoring['source_meshes']))
        authoring['material_signature_details'] = material_signature_details()
    mark = time.perf_counter()
    bpy.ops.wm.save_as_mainfile(filepath=extended_path(output))
    if not io_path(output).is_file() or io_path(output).stat().st_size == 0:
        raise RuntimeError('Final save did not create a nonempty character blend: ' + str(output))
    stage_times['final_save'] = time.perf_counter() - mark
    source_files = tuple(args.source_dir / name for name in ('source_audit.json','material_sources.json','source_texture_artifacts.json','lod_sources.json','renderer_export_map.json'))
    humanoid_map = args.source_dir / 'humanoid_bone_map.json'
    if humanoid_map.is_file():
        source_files += (humanoid_map,)
    image_rows = packed_image_records(tuple(materials.image_cache.values()))
    family_inventory = weather_family_inventory(packet, wetfilm, late_materials, skin_rain, hair_fur_snow) if weather_input is not None else []
    weather_input_receipt = weather_per_draw_receipt(weather_input, family_inventory)
    source_artifact_refs = [artifact(path) for path in (*source_files, args.fbx, args.shader_identities,
                                     args.renderer_visibility, args.profile, args.alpha_audit)]
    source_hashes = {str(Path(__file__)): file_hash(Path(__file__)),
                     **{str(Path(__file__).parent/name): file_hash(Path(__file__).parent/name) for name in (
                         'character_windows_paths.py','rebuild_character_materials.py','review_character_cross_hgrp.py',
                         'audit_character_materials.py','character_late_materials.py','character_wetfilm_profile.py',
                         'character_standard_snow_profile.py','character_hair_fur_snow_profile.py','character_hair_fur_snow.py',
                         'character_fur_curves_profile.py','character_fur_curve_weather.py','character_fur_geometry.py',
                         'character_fur_texture_contract.py','character_mesh_effect.py','character_npr_vfx.py',
                         'character_hair_wet.py','character_hair_wet_profile.py','character_rain_droplets.py',
                         'character_fur_wet.py','character_fur_wet_profile.py',
                          'character_weather_nodes.py','character_weather_per_draw.py',
                         'attach_character_weather_surface.py','character_skin_weather_profile.py','character_skin_rain.py',
                         'character_material_profile.py','character_material_profile_template.json','source_caster_manifest.py',
                         'character_hair_additional_lights.py','character_additional_light_state.py')}}
    source_hashes.update({str(Path(__file__).parent/name): file_hash(Path(__file__).parent/name)
                         for name in ('character_emotion_atlas.py', 'character_expression_material_controls.py',
                                      'character_authoring_workspace.py', 'character_material_authoring.py',
                                      'character_rig_authoring.py', 'character_viewport_preview.py',
                                      'character_rig_panel.py', 'character_rig_ik.py', 'character_weather_panel.py')})
    if material_variant is not None:
        source_hashes.update({str(Path(__file__).parent/name): file_hash(Path(__file__).parent/name)
                             for name in ('character_material_options.py', 'character_material_variant.py',
                                          'character_simple_materials.py', 'character_simple_layout.py', 'character_simple_weather.py',
                                          'character_simple_full_weather.py')})
    if weather_input is not None:
        source_artifact_refs.append({'path': str(weather_input.path), 'bytes': weather_input.byte_count,
                                     'sha256': weather_input.sha256})
        source_hashes[str(weather_input.path)] = weather_input.sha256
    if fur_curves_profile is not None:
        curve_inputs = (fur_curves_profile.path, fur_curves_profile.sidecar, fur_curves_profile.helpers)
        source_artifact_refs.extend(artifact(path) for path in curve_inputs)
        source_hashes.update({str(path): file_hash(path) for path in curve_inputs})
    if fur_curves_preparation is not None:
        curve_inputs = (fur_curves_preparation.path, *(path for item in fur_curves_preparation.profiles
                        for path in (item.path, item.sidecar, item.helpers)))
        source_artifact_refs.extend(artifact(path) for path in dict.fromkeys(curve_inputs))
        source_hashes.update({str(path): file_hash(path) for path in curve_inputs})
    report = {'format': 'EndfieldCharacterBuild/1', 'status': 'built_not_reopened',
              'output': str(output.resolve()), 'sha256': file_hash(output), 'render_count': 0,
              'reopen_count': 0, 'save_count': 1, 'source_renderers': len(hgrp.names),
              'visible_renderers': sum(not obj.hide_render for obj in materials.bound_objects),
              'packed_image_roles': len(image_rows), 'packed_images': image_rows,
              'armature': rigs[0].name, 'head_bone': args.head_bone,
              'head_matrix_world': [list(row) for row in rigs[0].matrix_world @ rigs[0].pose.bones[args.head_bone].matrix],
              'source_import': imported, 'static_bind_pose_audit': str(pose), 'selection_report': artifact(selection),
              'source_artifact_refs': source_artifact_refs,
              'source_materials': len(packet.materials), 'actual_shader_families': sorted({source.shader for source in packet.materials}),
              'material_support': {'visible_supported': [{'identity': source.identity, 'shader': source.shader} for source in packet.materials if source.identity in packet.visible_materials],
                                   'inactive_unsupported': [{'identity': source.identity, 'shader': source.shader, 'status': 'archived_unsupported_shader'} for source in packet.materials if source.shader not in SUPPORTED_SHADERS]},
              'material_bindings': materials.renderer_rows, 'hgrp': dict(hgrp.details), 'special_lights': modes,
              'late_materials': late_materials,
              'emotion_atlas': emotion_atlas,
              'standard_wetfilm': wetfilm,
              'standard_snow': standard_snow,
              'hair_fur_snow': hair_fur_snow,
              'fur_curves_snow': fur_curves_snow,
              'skin_rain': skin_rain,
              'weather_per_draw_input': weather_input_receipt,
              **shadows, 'authoring': authoring, 'authoring_workspace': workspace,
              'stage_seconds': stage_times, 'elapsed_seconds': time.perf_counter() - started,
              'source_hashes': source_hashes,
              'limits': ['Build-only receipt does not establish successful saved-file reopen or rendered fidelity.',
                         'Frozen mathematical kernels, constructor state and explicit preview frames do not equal native game runtime captures.',
                         'GN vertex-ray shadows and face-domain alpha dither remain explicit host approximations.',
                         'Hair mode0 Point/Spot is wired only for supported source variants; other Hair modes/keywords, Eye/Fur, live cookie projection and native area-light descriptors remain unimplemented.']}
    if material_variant is not None:
        report['material_variant'] = material_variant
    io_path(args.output_dir / 'build_receipt.json').write_text(json.dumps(report, indent=2, allow_nan=False), encoding='utf-8')
    print(json.dumps({'status': report['status'], 'output': str(output), 'elapsed_seconds': report['elapsed_seconds']}))


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    for name in ('fbx','source-dir','shader-identities','renderer-visibility','profile','repository-src','alpha-audit','output-dir'):
        parser.add_argument('--'+name, type=Path, required=True)
    parser.add_argument('--head-bone', required=True)
    parser.add_argument('--authoring-request', type=Path)
    parser.add_argument('--weather-per-draw-input', type=Path)
    parser.add_argument('--material-options', type=Path)
    curves = parser.add_mutually_exclusive_group()
    curves.add_argument('--fur-curves-profile', type=Path)
    curves.add_argument('--fur-curves-preparation', type=Path)
    for name in ('face-right','face-up','face-forward','hair-x','hair-y','hair-z'):
        parser.add_argument('--'+name, nargs=3, type=float, required=True)
    main(parser.parse_args(sys.argv[sys.argv.index('--')+1:]))

