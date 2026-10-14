"""Attach optional source expressions and source Actions to a loaded character."""
from __future__ import annotations

from dataclasses import dataclass
import hashlib
import json
import math
from pathlib import Path

import bpy

from character_authoring_native_expression import (AbsoluteExpressionReplay, AdditiveExpressionReplay,
    NativeExpressionReplay, bake_absolute_expression, bake_additive_expression,
    read_native_expression_replay, validate_loaded_replay_source, validate_native_expression)


@dataclass(frozen=True)
class BasicExpressionRequest:
    table: Path
    armature: str
    controller: str


@dataclass(frozen=True)
class IndependentExpressionTableRequest:
    manifest: Path
    armature: str
    controller: str


@dataclass(frozen=True)
class AnimationRequest:
    receipt: Path
    hierarchy: Path
    frames: Path
    armatures: tuple[str, ...]
    action_name: str
    sample_rate: float


@dataclass(frozen=True)
class AuthoringRequest:
    character_id: str
    source_audit: Path
    original_table: Path | None
    basic_expressions: BasicExpressionRequest | None
    independent_expression_tables: tuple[IndependentExpressionTableRequest, ...]
    animations: tuple[AnimationRequest, ...]
    native_expression_replays: tuple[NativeExpressionReplay, ...]


def record(value: object, context: str) -> dict[str, object]:
    if not isinstance(value, dict) or any(not isinstance(key, str) for key in value):
        raise TypeError(f'Expected object: {context}')
    return value


def text(value: object, context: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise TypeError(f'Expected nonempty string: {context}')
    return value


def read_authoring_request(path: Path) -> AuthoringRequest:
    data = record(json.loads(path.read_text(encoding='utf-8-sig')), 'authoring request')
    if data['format'] != 'EndfieldCharacterAuthoringRequest/1':
        raise ValueError('Unsupported character authoring request format')
    def source(value: object) -> Path:
        return (path.parent / text(value, 'source path')).resolve(strict=True)
    table = None if data['original_table'] is None else source(data['original_table'])
    raw_basic = data['basic_expressions']
    basic = None
    if raw_basic is not None:
        row = record(raw_basic, 'basic expressions')
        basic = BasicExpressionRequest(source(row['table']), text(row['armature'], 'expression armature'), text(row['controller'], 'expression controller'))
    raw_independent = data.get('independent_expression_tables', [])
    if not isinstance(raw_independent, list):
        raise TypeError('Independent expression tables must be an array')
    independent = []
    for index, raw in enumerate(raw_independent):
        row = record(raw, f'independent expression table[{index}]')
        independent.append(IndependentExpressionTableRequest(
            source(row['manifest']), text(row['armature'], 'independent expression armature'),
            text(row['controller'], 'independent expression controller')))
    raw_animations = data['animations']
    if not isinstance(raw_animations, list):
        raise TypeError('Animations must be an array')
    animations = []
    for raw in raw_animations:
        row = record(raw, 'animation')
        receipt_path = source(row['receipt'])
        receipt = record(json.loads(receipt_path.read_text(encoding='utf-8')), 'animation export receipt')
        if receipt['format'] != 'EndfieldAnimationExportReceipt/1' or receipt['status'] != 'decoded_bound_and_merged_candidate':
            raise ValueError('Animation requires a successfully decoded and merged source receipt')
        if receipt['character_id'] != data['character_id']:
            raise ValueError('Animation source belongs to a different character')
        hierarchy = (receipt_path.parent / text(receipt['source_hierarchy'], 'source hierarchy')).resolve(strict=True)
        frames = (receipt_path.parent / text(receipt['source_local_frames'], 'source frames')).resolve(strict=True)
        outputs = receipt['outputs']
        if not isinstance(outputs, list):
            raise TypeError('Animation export receipt requires output hash records')
        hashes = {(receipt_path.parent / text(record(item, 'output')['path'], 'output path')).resolve():
                  text(record(item, 'output')['sha256'], 'output hash') for item in outputs}
        for artifact in (hierarchy, frames):
            if hashes.get(artifact) != hashlib.sha256(artifact.read_bytes()).hexdigest():
                raise ValueError(f'Animation output is absent from its receipt or changed: {artifact}')
        rigs = row['armatures']
        if not isinstance(rigs, list) or not rigs:
            raise ValueError('Animation requires an explicit nonempty armature list')
        names = tuple(text(name, 'animation armature') for name in rigs)
        if len(set(names)) != len(names):
            raise ValueError('Animation armature names must be unique')
        rate = receipt['sample_rate']
        if type(rate) not in (int, float) or not math.isfinite(rate) or rate <= 0:
            raise ValueError('Animation sample rate must be positive and finite')
        animations.append(AnimationRequest(receipt_path, hierarchy, frames, names,
                                           text(row['action_name'], 'action name'), float(rate)))
    if len({animation.sample_rate for animation in animations}) > 1:
        raise ValueError('Actions attached in one scene must use the same sample rate')
    raw_replays = data.get('native_expression_replays', [])
    if not isinstance(raw_replays, list):
        raise TypeError('Native expression replays must be an array of manifest paths')
    replays = tuple(read_native_expression_replay(source(value)) for value in raw_replays)
    audit = source(data['source_audit'])
    for replay in replays:
        if replay.source.character_id != data['character_id'] or replay.source.source_audit.path != audit:
            raise ValueError('Native expression replay differs from the authoring character/source audit')
        if table is not None and replay.source.expression_table.path != table:
            raise ValueError('Native expression replay differs from the original authoring expression table')
    if len({replay.source.output for replay in replays}) != len(replays):
        raise ValueError('Native expression replay output directories must be unique')
    return AuthoringRequest(text(data['character_id'], 'character ID'), audit, table, basic,
                            tuple(independent), tuple(animations), replays)


def copy_original_table(request: AuthoringRequest, output: Path) -> dict[str, object] | None:
    if request.original_table is None:
        return None
    payload = request.original_table.read_bytes()
    data = record(json.loads(payload), 'original expression table')
    if data['character_id'] != request.character_id:
        raise ValueError('Optional original expression table belongs to a different character')
    target = output / 'expression_source.json'
    if target.exists():
        raise FileExistsError(target)
    target.write_bytes(payload)
    return {'file': target.name, 'sha256': hashlib.sha256(payload).hexdigest(),
            'bytes': len(payload), 'scope': 'Original source table; no native runtime mixing claim.'}


def rig_named(name: str) -> bpy.types.Object:
    obj = bpy.data.objects.get(name)
    if obj is None or obj.type != 'ARMATURE':
        raise ValueError(f'Explicit source armature is missing: {name}')
    return obj


def apply_authoring(request: AuthoringRequest, cache_root: Path | None) -> dict[str, object]:
    from character_authoring_validation import character_invariants, shape_signatures, verify_invariants, verify_preserved_shapes
    from character_authoring_identity import source_character_meshes, validate_authoring_identity

    for replay in request.native_expression_replays:
        validate_loaded_replay_source(replay.source)

    identity = validate_authoring_identity(request.source_audit, request.original_table,
                                          request.basic_expressions.table if request.basic_expressions else None,
                                          tuple(config.receipt for config in request.animations))

    leaf_restoration = None
    if request.basic_expressions is not None:
        from character_authoring_unweighted_leaf import restore_unweighted_avatar_leaf_bones
        leaf_restoration = restore_unweighted_avatar_leaf_bones(
            rig_named(request.basic_expressions.armature), request.basic_expressions.table, request.source_audit)

    names = tuple(obj.name for obj in source_character_meshes())
    if not names:
        raise ValueError('Authoring requires source-identified character meshes')
    material_controls = None
    material_table = request.original_table if request.original_table is not None else (
        request.basic_expressions.table if request.basic_expressions is not None else None)
    if material_table is not None:
        from character_expression_material_controls import attach_expression_material_controls
        from blender_scene_materials import layout_shader_node_tree
        material_controls = attach_expression_material_controls(material_table, request.source_audit, layout_shader_node_tree)
    else:
        from character_expression_material_controls import inherited_expression_material_controls
        material_controls = inherited_expression_material_controls()
        if (material_controls is not None
                and material_controls['source_audit_sha256'] != hashlib.sha256(request.source_audit.read_bytes()).hexdigest()):
            raise ValueError('Inherited material expression controls belong to a different source audit')
    preserved = character_invariants(names)
    original_shapes = shape_signatures(names)
    basic = None
    if request.basic_expressions is not None:
        config = request.basic_expressions
        table = record(json.loads(config.table.read_text(encoding='utf-8')), 'expression source')
        if table['character_id'] != request.character_id:
            raise ValueError('Basic expression source belongs to a different character')
        if cache_root is None:
            from basic_expression_shape_baker import apply_basic_expressions
            basic = apply_basic_expressions(config.table, request.source_audit, config.armature,
                                            config.controller, 2e-6)
        else:
            from character_expression_bake_adapter import apply_basic_expression_cache
            basic = apply_basic_expression_cache(cache_root, request.character_id,
                request.source_audit, rig_named(config.armature), config.table,
                config.controller, 2e-6)
    independent_tables = []
    for config in request.independent_expression_tables:
        if cache_root is None:
            from independent_expression_table_baker import bake_expression_table_endpoints
            independent_tables.append(bake_expression_table_endpoints(
                config.manifest, request.source_audit, config.armature, config.controller, 2e-6))
        else:
            from character_expression_bake_adapter import apply_independent_expression_cache
            independent_tables.append(apply_independent_expression_cache(
                cache_root, request.character_id, request.source_audit,
                rig_named(config.armature), config.manifest, config.controller, 2e-6))
    animations = []
    assigned: set[str] = set()
    for config in request.animations:
        from character_animation_binding import attach_animation
        for name in config.armatures:
            if name in assigned:
                raise ValueError(f'Authoring request attaches multiple active Actions to one rig: {name}')
            assigned.add(name)
            bound = attach_animation(rig_named(name), config.hierarchy, config.frames,
                                     config.action_name, config.sample_rate, 2e-4)
            animations.append({'armature': name, 'binding': bound})
    if animations:
        bpy.context.scene.frame_end = max(int(row['binding']['frames']) for row in animations)
        bpy.context.scene.frame_set(1)
    native_replays = []
    meshes = tuple(bpy.data.objects[name] for name in names)
    for replay in request.native_expression_replays:
        if isinstance(replay, AbsoluteExpressionReplay):
            native_replays.append(bake_absolute_expression(replay, meshes))
        elif isinstance(replay, AdditiveExpressionReplay):
            native_replays.append(bake_additive_expression(replay, meshes))
        else:
            raise TypeError('Unsupported typed native expression replay')
    verify_invariants(preserved, names)
    verify_preserved_shapes(original_shapes, names)
    return {'format': 'EndfieldCharacterAuthoring/1', 'character_id': request.character_id, 'identity': identity,
            'source_meshes': names, 'preserved': preserved, 'original_shapes': original_shapes,
            'all_shapes': shape_signatures(names), 'basic_expressions': basic,
            'independent_expression_tables': independent_tables,
            'restored_source_bones': leaf_restoration, 'animations': animations,
            'native_expression_replays': native_replays,
            'material_expression_controls': material_controls,
            'scope': 'Basic facial endpoints, independent source-table endpoints, body Actions and explicitly requested native facial writeback snapshots; no automatic expression tracker or full gameplay scheduling.'}


def verify_authoring(report: dict[str, object]) -> dict[str, object]:
    from character_authoring_validation import shape_signatures, verify_invariants, verify_preserved_shapes

    names = tuple(text(name, 'source mesh') for name in report['source_meshes'])
    verify_invariants(record(report['preserved'], 'invariants'), names)
    verify_preserved_shapes(report['original_shapes'], names)
    if shape_signatures(names) != report['all_shapes']:
        raise AssertionError('Baked shape data changed after saving')
    basic_checks = None
    if report['basic_expressions'] is not None:
        from basic_expression_shape_baker import validate_basic_expressions
        basic_checks = validate_basic_expressions(report['basic_expressions'], 2e-6)
    independent_checks = []
    for index, receipt in enumerate(report.get('independent_expression_tables', [])):
        from independent_expression_table_baker import validate_expression_table_endpoints
        independent_checks.append(validate_expression_table_endpoints(
            record(receipt, f'independent expression receipt[{index}]'), 2e-6))
    leaf_checks = None
    leaf_restoration = report.get('restored_source_bones')
    if leaf_restoration is not None:
        from character_authoring_unweighted_leaf import validate_restored_avatar_leaf_bones
        restoration = record(leaf_restoration, 'restored source bones')
        leaf_armature_name = text(restoration.get('armature_name'), 'restored source bone armature')
        leaf_checks = validate_restored_avatar_leaf_bones(rig_named(leaf_armature_name), restoration)
    checks = []
    for row in report['animations']:
        from character_animation_binding import validate_attached_animation
        binding = row['binding']
        armature = rig_named(row['armature'])
        # The binder owns its frame domain and validates source hashes again.
        start, end = 1, int(binding['frames'])
        frames = tuple(sorted({start, (start + end) // 2, end}))
        active_action = armature.animation_data.action
        armature.animation_data.action = bpy.data.actions[binding['action_name']]
        try:
            checks.append(validate_attached_animation(armature, binding, frames, 2e-4))
        finally:
            armature.animation_data.action = active_action
    native_checks = []
    material_checks = None
    if report.get('material_expression_controls') is not None:
        from character_expression_material_controls import verify_expression_material_controls
        material_checks = verify_expression_material_controls(report['material_expression_controls'])
    for receipt in report.get('native_expression_replays', []):
        native_checks.append({'replay': validate_native_expression(receipt),
                              'inherited_authoring': verify_authoring(receipt['inherited_authoring'])})
    bpy.context.scene.frame_set(bpy.context.scene.frame_start)
    verify_invariants(record(report['preserved'], 'invariants'), names)
    return {'status': 'passed', 'animations': checks, 'basic_expressions': basic_checks,
            'independent_expression_tables': independent_checks,
            'restored_source_bones': leaf_checks, 'all_shape_data_preserved': True,
            'native_expression_replays': native_checks,
            'material_expression_controls': material_checks,
            'original_geometry_skin_rest_materials_textures_preserved': True}
