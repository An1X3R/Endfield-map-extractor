"""Apply audited instance repairs through the existing importer, instancer and decal projector."""
from __future__ import annotations

import argparse
import hashlib
import importlib.util
import itertools
import json
import struct
import sys
from collections import Counter, defaultdict
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

import bmesh
import bpy
import numpy as np
from mathutils import Euler, Matrix, Vector

sys.path.insert(0, str(Path(__file__).resolve().parent))
import blender_add_projected_decals as projector
from endfield_scene_slot_contract import static_submesh_slots
from endfield_scene_material_contract import resolve_export_path
from endfield_scene_slot_contract import SlotPlan
from blender_restore_scene_material_slots import matched_slot_indices


@dataclass(frozen=True)
class MaterialPair:
    identifier: str
    path: str


@dataclass(frozen=True)
class SupersededPoint:
    object_name: str
    index: int
    source_object: str
    preservation_property: str
    replacement_property: str


@dataclass(frozen=True)
class Repair:
    entity_id: int
    base: str
    matrix: tuple[float, ...]
    mesh_path: str
    mesh_name: str
    import_file: Path
    materials: tuple[MaterialPair, ...]
    placement_kind: Literal['existing_instance', 'unresolved_point', 'existing_direct']
    unresolved_markers: int
    superseded_point: SupersededPoint | None


def read_repairs(path: Path, bounds: tuple[float, ...]) -> tuple[Repair, ...]:
    payload = projector.require_object(json.loads(path.read_text(encoding='utf-8-sig')))
    if payload.get('format') != 'EndfieldGeometryRepairPlan/1':
        raise ValueError('Repair plan must use EndfieldGeometryRepairPlan/1')
    evidence = {}
    if 'missing_instance_contract' in payload:
        contract = projector.require_object(payload['missing_instance_contract'])
        audit_file = Path(projector.require_text(contract['source_audit']))
        audit_bytes = audit_file.read_bytes()
        if hashlib.sha256(audit_bytes).hexdigest() != contract['source_audit_sha256']:
            raise ValueError('Missing-instance source audit hash changed')
        audit = projector.require_object(json.loads(audit_bytes))
        if audit['format'] != 'EndfieldMissingSourceAudit/1':
            raise ValueError('Missing-instance evidence must use EndfieldMissingSourceAudit/1')
        for item in projector.require_list(audit['instances']):
            source = projector.require_object(item)
            if source['entity_id'] in evidence:
                raise ValueError('Missing-instance source audit repeats an entity ID')
            evidence[source['entity_id']] = source
    result = []
    for value in projector.require_list(payload['instances']):
        row = projector.require_object(value)
        entity_id = row['entity_id']
        if type(entity_id) is not int:
            raise TypeError('Repair entity ID must be an integer')
        matrix = projector.vector(row['matrix'], 16)
        if not bounds[0] <= matrix[12] < bounds[1] or not bounds[2] <= matrix[14] < bounds[3]:
            continue
        mesh = projector.require_object(row['cached_mesh'])
        asset = projector.require_object(mesh['asset'])
        pairs = []
        for value in projector.require_list(row['raw_material_pairs']):
            pair = projector.require_object(value)
            pairs.append(MaterialPair(projector.require_text(pair['material_id']), projector.require_text(pair['material_path'])))
        if not pairs:
            raise ValueError('Repair lacks material slots')
        placement = row.get('placement_kind', 'existing_instance')
        if placement not in ('existing_instance', 'unresolved_point', 'existing_direct'):
            raise ValueError(f'Unsupported repair placement kind: {placement}')
        markers = 1 if placement == 'unresolved_point' else 0
        superseded = None
        if placement == 'existing_direct':
            markers = row['unresolved_markers']
            if type(markers) is not int or markers not in (0, 1):
                raise ValueError('Existing direct repair requires zero or one audited unresolved marker')
        if placement in ('unresolved_point', 'existing_direct'):
            source = evidence.get(entity_id)
            if source is None or source['entity_base'] != row['entity_base'] or tuple(source['matrix']) != matrix:
                raise ValueError(f'Missing repair disagrees with authoritative entity evidence: entity_id={entity_id}')
            if placement == 'existing_direct' and source.get('unresolved_markers') != markers:
                raise ValueError(f'Direct repair marker count differs from source evidence: entity_id={entity_id}')
            if placement == 'existing_direct' and row.get('superseded_instance') != source.get('superseded_instance'):
                raise ValueError(f'Direct repair hidden backup contract differs from source evidence: entity_id={entity_id}')
            if placement == 'existing_direct' and 'superseded_instance' in row:
                backup = projector.require_object(row['superseded_instance'])
                if backup != source.get('superseded_instance') or type(backup['index']) is not int or backup['index'] < 0:
                    raise ValueError(f'Superseded point differs from source evidence: entity_id={entity_id}')
                superseded = SupersededPoint(projector.require_text(backup['object']), backup['index'],
                                             projector.require_text(backup['source_object']),
                                             projector.require_text(backup['preservation_property']),
                                             projector.require_text(backup['replacement_property']))
            source_pairs = tuple(MaterialPair(raw['material_id'], pair[1])
                                 for pair, raw in zip(source['pairs'], source['raw_ids'], strict=True)
                                 if pair[0] == row['current_primary_mesh_path'])
            if tuple(pairs) != source_pairs:
                raise ValueError(f'Missing repair material sequence disagrees with source renderer: entity_id={entity_id}')
        file = resolve_export_path(Path(projector.require_text(row['import_file'])))
        if not file.is_file():
            raise FileNotFoundError(file)
        result.append(Repair(entity_id, projector.require_text(row['entity_base']), matrix,
                             projector.require_text(row['current_primary_mesh_path']),
                             projector.require_text(asset['Name']), file, tuple(pairs), placement, markers, superseded))
    if not result or len({row.entity_id for row in result}) != len(result):
        raise ValueError('Repair selection is empty or contains duplicate entity IDs')
    return tuple(result)


def mesh_digest(obj: bpy.types.Object) -> str:
    mesh = obj.data
    digest = hashlib.sha256()
    coordinates = np.empty(len(mesh.vertices) * 3, dtype=np.float32)
    mesh.vertices.foreach_get('co', coordinates)
    digest.update(coordinates.tobytes())
    indices = np.empty(len(mesh.loops), dtype=np.int32)
    mesh.loops.foreach_get('vertex_index', indices)
    digest.update(indices.tobytes())
    for name in ('ef_rotation', 'ef_scale'):
        attribute = mesh.attributes.get(name)
        if attribute is not None:
            values = np.empty(len(attribute.data) * 3, dtype=np.float32)
            attribute.data.foreach_get('vector', values)
            digest.update(values.tobytes())
    digest.update(np.array(obj.matrix_world, dtype=np.float32).tobytes())
    return digest.hexdigest()


def unresolved_report_metadata() -> str:
    """Bind the legacy point-count metadata to the audited source scene."""
    unresolved = bpy.data.objects['Unresolved_Entity_Positions']
    return json.dumps({
        'point_count': len(unresolved.data.vertices),
        'object_count': unresolved.get('unresolved_count'),
        'base_counts': unresolved.get('unresolved_base_counts_json'),
        'scene_resolved': bpy.context.scene.get('resolved_instance_count'),
        'scene_unresolved': bpy.context.scene.get('unresolved_instance_count'),
        'base_resolved': bpy.context.scene.get('base_layer_resolved_instance_count'),
        'base_unresolved': bpy.context.scene.get('base_layer_unresolved_instance_count'),
    }, sort_keys=True)


def world_box(matrix: Matrix, corners: tuple[tuple[float, ...], ...]) -> tuple[np.ndarray, np.ndarray]:
    points = np.array([matrix @ Vector(corner) for corner in corners])
    return points.min(axis=0), points.max(axis=0)


def overlap(first: tuple[np.ndarray, np.ndarray], second: tuple[np.ndarray, np.ndarray]) -> bool:
    return bool(np.all(first[0] <= second[1]) and np.all(second[0] <= first[1]))


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    for name in ('blend', 'repairs', 'builder', 'decal-manifest', 'materials', 'prior-audit', 'output', 'audit'):
        parser.add_argument('--' + name, type=Path, required=True)
    parser.add_argument('--bounds', type=float, nargs=4, required=True)
    options = parser.parse_args(sys.argv[sys.argv.index('--') + 1:])
    if options.bounds[0] >= options.bounds[1] or options.bounds[2] >= options.bounds[3]:
        raise ValueError('Repair bounds must have increasing X and Z limits')
    if not options.builder.is_file():
        raise FileNotFoundError(options.builder)
    if options.output.exists() or options.audit.exists():
        raise FileExistsError('Repair output and audit must be new files')
    repairs = read_repairs(options.repairs, tuple(options.bounds))
    spec = importlib.util.spec_from_file_location('existing_stage65_builder', options.builder)
    if spec is None or spec.loader is None:
        raise ImportError('Existing scene builder cannot be loaded')
    builder = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = builder
    spec.loader.exec_module(builder)
    decals = tuple(r for r in projector.read_decals(options.decal_manifest)
                   if options.bounds[0] <= r.matrix[12] < options.bounds[1]
                   and options.bounds[2] <= r.matrix[14] < options.bounds[3])
    materials = projector.read_materials(options.materials)
    prior = json.loads(options.prior_audit.read_text())
    outcomes = {r['entity_id']:r for r in prior['outcomes']}
    if set(outcomes) != {r.entity_id for r in decals}:
        raise ValueError('Prior decal audit does not match the selected source manifest')
    evidenced_geometry: dict[str, str] | None = None
    evidenced_counts: str | None = None
    if any(row.placement_kind in ('unresolved_point', 'existing_direct') for row in repairs):
        plan = projector.require_object(json.loads(options.repairs.read_text(encoding='utf-8-sig')))
        contract = projector.require_object(plan['missing_instance_contract'])
        evidence = projector.require_object(json.loads(Path(contract['source_audit']).read_bytes()))
        source_blend = Path(projector.require_text(evidence['source_blend']))
        bpy.ops.wm.open_mainfile(filepath=str(source_blend))
        evidenced_geometry = {obj.name: mesh_digest(obj) for obj in bpy.data.objects
                              if obj.type == 'MESH' and 'projected_decal_entity_id' not in obj}
        evidenced_counts = unresolved_report_metadata()
    bpy.ops.wm.open_mainfile(filepath=str(options.blend))
    previous_repair_ids = {entity_id for obj in bpy.context.scene.objects if 'verified_entity_ids_json' in obj
                           for entity_id in json.loads(obj['verified_entity_ids_json'])}
    repeated = previous_repair_ids.intersection(row.entity_id for row in repairs)
    if repeated:
        raise ValueError(f'Repair plan repeats already repaired entities: entity_ids={sorted(repeated)}')
    terrain_before = projector.terrain_hash()
    original = {obj.name:mesh_digest(obj) for obj in bpy.data.objects
                if obj.type == 'MESH' and 'projected_decal_entity_id' not in obj}
    if evidenced_geometry is not None and original != evidenced_geometry:
        raise ValueError('Missing-instance input geometry differs from the audited source scene; refresh the missing-source audit')
    if evidenced_counts is not None and unresolved_report_metadata() != evidenced_counts:
        raise ValueError('Missing-instance input count metadata differs from the audited source scene')
    decal_objects = {int(obj['projected_decal_entity_id']):obj for obj in bpy.data.objects
                     if 'projected_decal_entity_id' in obj}
    if set(decal_objects) != {eid for eid,row in outcomes.items() if row['status'] == 'restored'}:
        raise ValueError('Input scene projected decal IDs disagree with the prior audit')
    bases = {row.base for row in repairs}
    point_lookup = defaultdict(list)
    for obj in bpy.data.objects:
        if obj.get('entity_base') not in bases or obj.type != 'MESH' or obj.data.attributes.get('ef_rotation') is None:
            continue
        if obj.matrix_world != Matrix.Identity(4):
            raise ValueError('Existing point instancer has a nonidentity object transform')
        source_objects = []
        for modifier in obj.modifiers:
            if modifier.type == 'NODES' and modifier.node_group is not None:
                source_objects.extend(node.inputs['Object'].default_value for node in modifier.node_group.nodes
                                      if node.bl_idname == 'GeometryNodeObjectInfo')
        if len(source_objects) != 1 or source_objects[0] is None:
            raise ValueError('Existing instancer does not have exactly one source mesh')
        for index, vertex in enumerate(obj.data.vertices):
            key = (obj['entity_base'], tuple(round(v, 4) for v in vertex.co))
            point_lookup[key].append((obj, index, source_objects[0]))
    basis = Matrix(((1,0,0,0),(0,0,-1,0),(0,1,0,0),(0,0,0,1)))
    removals = defaultdict(set)
    changed_boxes = []
    located = {}
    repair_matrices = {row.entity_id: builder.unity_matrix(struct.pack('<16f', *row.matrix)) for row in repairs}
    unresolved = bpy.data.objects['Unresolved_Entity_Positions']
    missing = tuple(row for row in repairs if row.placement_kind == 'unresolved_point')
    reported = tuple(row for row in repairs if row.unresolved_markers)
    direct_replacements = []
    missing_removals: set[int] = set()
    missing_points = defaultdict(list)
    if reported:
        if unresolved.matrix_world != Matrix.Identity(4):
            raise ValueError('Unresolved point object has a nonidentity transform')
        for index, vertex in enumerate(unresolved.data.vertices):
            missing_points[tuple(round(v, 4) for v in vertex.co)].append(index)
        direct_ids = {int(obj['entity_id']) for obj in bpy.data.objects if 'entity_id' in obj}
        if direct_ids.intersection(row.entity_id for row in missing):
            raise ValueError('Missing repair repeats an existing direct entity')
        required_markers = Counter(tuple(round(v, 4) for v in repair_matrices[row.entity_id].translation) for row in reported)
        if any(len(missing_points[point]) != count for point, count in required_markers.items()):
            raise ValueError('Audited repair multiset does not exactly match unresolved markers at its positions')
    for repair in repairs:
        matrix = repair_matrices[repair.entity_id]
        reconstructed = Matrix.LocRotScale(*matrix.decompose())
        if np.max(np.abs(np.array(matrix) - np.array(reconstructed))) > 0.0003:
            raise ValueError(f'Repair matrix contains unsupported shear: entity_id={repair.entity_id}')
    repairs_by_location = defaultdict(list)
    for row in repairs:
        repairs_by_location[(row.base, tuple(round(v, 4) for v in repair_matrices[row.entity_id].translation))].append(row)
    equivalent_point_groups = []
    for repair in repairs:
        matrix = repair_matrices[repair.entity_id]
        key = (repair.base, tuple(round(v,4) for v in matrix.translation))
        if repair.placement_kind == 'existing_direct':
            direct = [obj for obj in bpy.context.scene.objects if 'entity_id' in obj
                      and int(obj['entity_id']) == repair.entity_id]
            if len(direct) != 1:
                raise ValueError(f'Direct repair identity is not unique: entity_id={repair.entity_id}, direct_matches={len(direct)}')
            obj = direct[0]
            if (obj.type != 'MESH' or obj.get('entity_base') != repair.base or 'instance_count' in obj
                    or obj.hide_render or obj.hide_get() or obj.modifiers or obj.constraints
                    or np.max(np.abs(np.array(obj.matrix_world) - np.array(matrix))) > 0.0003):
                raise ValueError(f'Direct repair source is hidden, modified or has a different transform: entity_id={repair.entity_id}')
            backup = repair.superseded_point
            candidates = point_lookup[key]
            if backup is None and candidates:
                raise ValueError(f'Direct repair collides with an unaudited point instance: entity_id={repair.entity_id}')
            if backup is not None:
                if len(candidates) != 1:
                    raise ValueError(f'Superseded point is not unique: entity_id={repair.entity_id}')
                instancer, point_index, source = candidates[0]
                actual = Matrix.LocRotScale(instancer.data.vertices[point_index].co,
                                           Euler(instancer.data.attributes['ef_rotation'].data[point_index].vector, 'XYZ'),
                                           instancer.data.attributes['ef_scale'].data[point_index].vector)
                if ((instancer.name, point_index, source.name) != (backup.object_name, backup.index, backup.source_object)
                        or not instancer.hide_render or instancer.visible_get() or not source.hide_render or source.visible_get()
                        or instancer.get(backup.preservation_property) != True
                        or instancer.get(backup.replacement_property) != obj.name
                        or point_index in removals[instancer.name]
                        or np.max(np.abs(np.array(actual) - np.array(matrix))) > 0.0003):
                    raise ValueError(f'Superseded point does not match its hidden backup contract: entity_id={repair.entity_id}')
                removals[instancer.name].add(point_index)
            slot_plan = SlotPlan(repair.base, repair.mesh_name, repair.import_file,
                                 tuple(('ecs', int(pair.identifier, 16)) for pair in repair.materials),
                                 tuple(pair.path for pair in repair.materials), 'verified_current_per_instance_renderer')
            _, _, layout, comparison = matched_slot_indices(builder, slot_plan, obj.data, ())
            changed_boxes.append(world_box(obj.matrix_world, tuple(tuple(c) for c in obj.bound_box)))
            direct_replacements.append({'entity_id': repair.entity_id, 'object': obj.name,
                                        'prior_coordinate_layout': layout, 'source_contract_comparison': comparison,
                                        'unresolved_markers': repair.unresolved_markers,
                                        'superseded_instance': None if backup is None else
                                        {'object': backup.object_name, 'index': backup.index, 'source_object': backup.source_object}})
            located[repair.entity_id] = (obj.name, None, None)
            continue
        if repair.placement_kind == 'unresolved_point':
            indices = missing_points[key[1]]
            direct = [obj.name for obj in bpy.data.objects
                      if obj.type == 'MESH' and obj.get('entity_base') == repair.base
                      and obj.data.attributes.get('ef_rotation') is None
                      and tuple(round(v, 4) for v in obj.matrix_world.translation) == key[1]]
            if len(indices) != 1 or indices[0] in missing_removals or point_lookup[key] or direct:
                raise ValueError(f'Missing repair is not uniquely unplaced: entity_id={repair.entity_id}, unresolved_matches={len(indices)}')
            missing_removals.add(indices[0])
            located[repair.entity_id] = (unresolved.name, indices[0], None)
            continue
        candidates = []
        for obj, index, source in point_lookup[key]:
            if obj.name in removals and index in removals[obj.name]:
                continue
            rotation = obj.data.attributes['ef_rotation'].data[index].vector
            scale = obj.data.attributes['ef_scale'].data[index].vector
            actual = Matrix.LocRotScale(obj.data.vertices[index].co, Euler(rotation, 'XYZ'), scale)
            if np.max(np.abs(np.array(actual)-np.array(matrix))) <= 0.0003:
                candidates.append((obj, index, source, actual))
        if len(candidates) > 1:
            remaining = [row for row in repairs_by_location[key] if row.entity_id not in located
                         and np.max(np.abs(np.array(repair_matrices[row.entity_id])-np.array(matrix))) <= 0.0003]
            same_sources = len({source.as_pointer() for _, _, source, _ in candidates}) == 1
            same_repairs = len({(row.mesh_path, row.materials) for row in remaining}) == 1
            if same_sources and same_repairs and len(remaining) == len(candidates):
                # All indistinguishable source points are replaced by the same
                # authoritative mesh/material multiset with original transforms.
                equivalent_point_groups.append({'entity_ids': sorted(row.entity_id for row in remaining),
                                                'points': len(candidates), 'policy': 'equivalent_source_and_repair_multiset'})
                candidates = [min(candidates, key=lambda value: (
                    float(np.max(np.abs(np.array(value[3])-np.array(matrix)))), value[0].name, value[1]))]
        if len(candidates) != 1:
            raise ValueError(f'Repair transform is not unique: entity_id={repair.entity_id}, location_matches={len(point_lookup[key])}, full_transform_matches={len(candidates)}')
        obj, index, source, actual = candidates[0]
        removals[obj.name].add(index)
        located[repair.entity_id] = (obj.name, index, source.name)
        changed_boxes.append(world_box(actual, tuple(tuple(c) for c in source.bound_box)))
    print(json.dumps({'event':'repair_points_verified','count':len(located)}), flush=True)
    source_collection = bpy.data.collections.new('Verified_Geometry_Repair_Sources')
    bpy.context.scene.collection.children.link(source_collection)
    instance_collection = bpy.data.collections.new('Verified_Geometry_Repairs')
    bpy.context.scene.collection.children.link(instance_collection)
    instance_collection_name = instance_collection.name
    delivery_scene = bpy.context.scene
    import_scene = bpy.data.scenes.new('Endfield_Geometry_Repair_Import')
    bpy.context.window.scene = import_scene
    grouped = defaultdict(list)
    for repair in repairs:
        grouped[(repair.base,repair.mesh_path,repair.materials)].append(repair)
    templates = []
    for (base,mesh_path,pairs),rows in grouped.items():
        repair = rows[0]
        imported = builder.import_obj(repair.import_file)
        slots = static_submesh_slots(len(imported), tuple(pair.identifier for pair in pairs))
        mapped_pairs = tuple(pairs[index] for index in slots.indices)
        for index,(obj,pair) in enumerate(zip(imported,mapped_pairs,strict=True)):
            obj.data.materials.clear()
            material = bpy.data.materials.new(f'REPAIR_NEUTRAL_{base}_{index}_{pair.identifier}')
            material.use_nodes = True
            material.node_tree.nodes.get('Principled BSDF').inputs['Base Color'].default_value = (0.62,0.62,0.62,1)
            material.node_tree.nodes.get('Principled BSDF').inputs['Roughness'].default_value = 0.8
            material['ecs_material_id'] = pair.identifier
            material['ecs_material_path'] = pair.path
            material['material_status'] = 'exact_reference_neutral_preview_pending_material_phase'
            obj.data.materials.append(material)
        source = builder.join_submeshes(imported)
        # OBJ axis conversion changes mesh coordinates before Blender refreshes
        # the cached bounds used to select affected decal receivers.
        bpy.context.view_layer.update()
        source_corners = tuple(tuple(c) for c in source.bound_box)
        if len(source.data.materials) != len(mapped_pairs):
            raise ValueError('Joined mesh lost ordered material slots')
        for collection in tuple(source.users_collection):
            collection.objects.unlink(source)
        source_collection.objects.link(source)
        source.name = 'SRC_REPAIRED_' + builder.safe_name(base)
        source.hide_render = True
        source['source_asset'] = repair.mesh_name
        source['ecs_mesh_path'] = mesh_path
        source['ecs_material_ids'] = json.dumps([p.identifier for p in mapped_pairs])
        source['ecs_renderer_material_ids'] = json.dumps([p.identifier for p in pairs])
        source['static_material_slot_mapping'] = slots.evidence
        source['geometry_binding_method'] = 'verified_current_per_instance_renderer'
        source['source_coordinate_correction'] = 'existing_stage65_import_obj'
        source['material_binding_method'] = 'exact_reference_neutral_preview_pending_material_phase'
        for row in rows:
            matrix = builder.unity_matrix(struct.pack('<16f', *row.matrix))
            changed_boxes.append(world_box(matrix, source_corners))
        tuples = [(row.base,row.entity_id,struct.pack('<16f',*row.matrix)) for row in rows]
        instancer = builder.make_instancer(base, source, tuples, instance_collection)
        for index, row in enumerate(rows):
            actual = Matrix.LocRotScale(instancer.data.vertices[index].co,
                                       Euler(instancer.data.attributes['ef_rotation'].data[index].vector, 'XYZ'),
                                       instancer.data.attributes['ef_scale'].data[index].vector)
            if np.max(np.abs(np.array(actual) - np.array(repair_matrices[row.entity_id]))) > 0.0003:
                raise ValueError(f'Imported repair transform changed: entity_id={row.entity_id}')
        instancer['verified_entity_ids_json'] = json.dumps([row.entity_id for row in rows])
        instancer['repair_manifest'] = str(options.repairs)
        source.data.calc_loop_triangles()
        templates.append({'object':source.name,'mesh_path':mesh_path,'vertices':len(source.data.vertices),
                          'triangles':len(source.data.loop_triangles),'material_slots':len(mapped_pairs),
                          'renderer_material_slots':len(pairs),'static_slot_mapping':slots.evidence,
                          'renderer_slot_indices':slots.indices,
                          'uv_layers':[layer.name for layer in source.data.uv_layers]})
    bpy.context.window.scene = delivery_scene
    bpy.data.scenes.remove(import_scene)
    bpy.context.view_layer.update()
    for template in templates:
        bpy.data.objects[template['object']].hide_set(True)
    for row in direct_replacements:
        obj = bpy.data.objects[row['object']]
        mesh = obj.data
        bpy.data.objects.remove(obj, do_unlink=True)
        if mesh.users == 0:
            bpy.data.meshes.remove(mesh)
    for name,indices in removals.items():
        obj = bpy.data.objects[name]
        remaining = [(tuple(v.co),tuple(obj.data.attributes['ef_rotation'].data[i].vector),
                      tuple(obj.data.attributes['ef_scale'].data[i].vector))
                     for i,v in enumerate(obj.data.vertices) if i not in indices]
        copied = obj.data.copy()
        bm = bmesh.new()
        bm.from_mesh(copied)
        bm.verts.ensure_lookup_table()
        bmesh.ops.delete(bm, geom=[bm.verts[i] for i in indices], context='VERTS')
        bm.to_mesh(copied)
        bm.free()
        obj.data = copied
        obj['instance_count'] = len(copied.vertices)
        after = [(tuple(v.co),tuple(copied.attributes['ef_rotation'].data[i].vector),
                  tuple(copied.attributes['ef_scale'].data[i].vector)) for i,v in enumerate(copied.vertices)]
        if remaining != after:
            raise ValueError('Point removal changed untouched transforms or ordering')
    bpy.context.view_layer.update()
    changed = []
    corners = tuple(itertools.product((-0.5,0.5),repeat=3))
    for decal in decals:
        box = world_box(basis @ projector.shader_matrix(decal.matrix), corners)
        if any(overlap(box,other) for other in changed_boxes):
            changed.append(decal)
    print(json.dumps({'event':'affected_decals_selected','count':len(changed)}), flush=True)
    receivers,grid = projector.receiver_index()
    arrays = {}
    projected_materials = {}
    decal_collection = bpy.data.collections['Endfield_Projected_Decals']
    minimum_order = min(materials[r.material_path.casefold()].floats['_DrawOrder'] for r in decals)
    state_changes = []
    for index,decal in enumerate(sorted(changed,key=lambda r:(materials[r.material_path.casefold()].floats['_DrawOrder'],r.entity_id))):
        material = materials[decal.material_path.casefold()]
        if material.floats.get('_UseWaterFlow',0)>0 or material.floats.get('_UseWetness',0)>0:
            continue
        was_present = decal.entity_id in decal_objects
        if was_present:
            old = decal_objects[decal.entity_id]
            mesh = old.data
            bpy.data.objects.remove(old,do_unlink=True)
            if mesh.users == 0:
                bpy.data.meshes.remove(mesh)
        bias = prior['surface_offset']+(material.floats['_DrawOrder']-minimum_order)*0.00002
        polygons,hits = projector.project_decal(decal,material,receivers,grid,arrays,projected_materials,decal_collection,bias)
        present = polygons > 0
        outcomes[decal.entity_id] = {'entity_id':decal.entity_id,'entity_base':decal.entity_base,
                                   'status':'restored' if present else 'no_receiver_surface',
                                   'polygons':polygons,'receiver_triangles':hits}
        if present != was_present:
            state_changes.append((decal,was_present,present))
        if index%25 == 0:
            print(json.dumps({'event':'affected_decal_projection','done':index+1,'total':len(changed)}),flush=True)
    unresolved = bpy.data.objects['Unresolved_Entity_Positions']
    counts_available = 'unresolved_base_counts_json' in unresolved
    counts = Counter(json.loads(unresolved['unresolved_base_counts_json'])) if counts_available else Counter()
    remove = Counter()
    additions = []
    for repair in reported:
        remove[tuple(round(v, 4) for v in repair_matrices[repair.entity_id].translation)] += 1
        if counts_available:
            counts[repair.base] -= 1
    for decal,was_present,present in state_changes:
        position = (decal.matrix[12],-decal.matrix[14],decal.matrix[13])
        if present:
            remove[tuple(round(v,4) for v in position)] += 1
            if counts_available:
                counts[decal.entity_base] -= 1
        else:
            additions.append(position)
            if counts_available:
                counts[decal.entity_base] += 1
    if state_changes or reported:
        kept = []
        for vertex in unresolved.data.vertices:
            key = tuple(round(v,4) for v in vertex.co)
            if remove[key]:
                remove[key] -= 1
            else:
                kept.append(tuple(vertex.co))
        if any(remove.values()):
            raise ValueError('Restored decal report point missing')
        mesh = bpy.data.meshes.new('unresolved_after_verified_geometry_repairs')
        mesh.from_pydata(kept+additions,[],[])
        unresolved.data = mesh
        unresolved['unresolved_count'] = len(mesh.vertices)
        if counts_available:
            unresolved['unresolved_base_counts_json'] = json.dumps({k:v for k,v in counts.items() if v})
    if counts_available and (any(v<0 for v in counts.values()) or sum(counts.values())!=len(unresolved.data.vertices)):
        raise ValueError('Remaining entity report counts are inconsistent')
    for name,digest in original.items():
        if (name not in removals and name not in {row['object'] for row in direct_replacements}
                and not (name=='Unresolved_Entity_Positions' and (state_changes or reported))):
            if mesh_digest(bpy.data.objects[name]) != digest:
                raise ValueError(f'Untouched geometry changed: object={name}')
    if projector.terrain_hash()!=terrain_before:
        raise ValueError('Terrain geometry changed during instance repairs')
    scene = bpy.context.scene
    delta = len(reported) + sum(int(present)-int(was_present) for _,was_present,present in state_changes)
    scene['resolved_instance_count'] += delta
    if 'base_layer_resolved_instance_count' in scene:
        scene['base_layer_resolved_instance_count'] += delta
    scene['unresolved_instance_count'] = len(unresolved.data.vertices)
    if 'base_layer_unresolved_instance_count' in scene:
        scene['base_layer_unresolved_instance_count'] = len(unresolved.data.vertices)
    scene['projected_decal_count'] = sum(row['status']=='restored' for row in outcomes.values())
    scene['geometry_repairs_count'] = len(previous_repair_ids) + len(repairs)
    scene['geometry_repair_manifest'] = str(options.repairs)
    scene['instancer_count'] = sum(obj.type=='MESH' and obj.data.attributes.get('ef_rotation') is not None for obj in bpy.data.objects)
    expected = {obj.name:mesh_digest(obj) for obj in bpy.data.objects if obj.type=='MESH' and 'projected_decal_entity_id' not in obj}
    expected_decal_geometry = {int(obj['projected_decal_entity_id']):(len(obj.data.vertices),len(obj.data.polygons))
                               for obj in bpy.data.objects if 'projected_decal_entity_id' in obj}
    expected_repair_ids = sorted(row.entity_id for row in repairs)
    expected_unresolved = len(unresolved.data.vertices)
    options.output.parent.mkdir(parents=True,exist_ok=True)
    print(json.dumps({'event':'saving_repaired_scene','output':str(options.output)}),flush=True)
    bpy.ops.wm.save_as_mainfile(filepath=str(options.output),compress=True)
    bpy.ops.wm.open_mainfile(filepath=str(options.output))
    actual = {obj.name:mesh_digest(obj) for obj in bpy.data.objects if obj.type=='MESH' and 'projected_decal_entity_id' not in obj}
    if actual!=expected or projector.terrain_hash()!=terrain_before:
        raise ValueError('Reopened geometry does not match the validated repair')
    actual_decal_geometry = {int(obj['projected_decal_entity_id']):(len(obj.data.vertices),len(obj.data.polygons))
                            for obj in bpy.data.objects if 'projected_decal_entity_id' in obj}
    if actual_decal_geometry!=expected_decal_geometry:
        raise ValueError('Reopened decal geometry changed')
    actual_ids = sorted(eid for obj in bpy.data.collections[instance_collection_name].objects for eid in json.loads(obj['verified_entity_ids_json']))
    if actual_ids!=expected_repair_ids or len(bpy.data.objects['Unresolved_Entity_Positions'].data.vertices)!=expected_unresolved:
        raise ValueError('Reopened repair or unresolved entity IDs/counts changed')
    if len(bpy.data.libraries):
        raise ValueError('Repaired output has an external Blender library')
    result = {'format':'EndfieldVerifiedGeometryRepairAudit/1','input':str(options.blend),'output':str(options.output),
              'repaired_instance_count':len(repairs),'repaired_entity_ids':expected_repair_ids,
              'missing_instances_restored':len(missing),'unresolved_base_counts_available':counts_available,
              'existing_direct_replacements':direct_replacements,'resolved_report_markers':len(reported),
              'missing_source_scene_geometry_verified':evidenced_geometry is not None,
              'previous_repaired_instance_count':len(previous_repair_ids),'repair_collection':instance_collection_name,
              'equivalent_point_groups':equivalent_point_groups,
              'repaired_base_count':len(bases),'templates':templates,'affected_decal_count':len(changed),
              'decal_state_changes':[{'entity_id':r.entity_id,'before':old,'after':new} for r,old,new in state_changes],
              'restored':len(actual_decal_geometry),'unresolved':expected_unresolved,
              'status_counts':dict(Counter(row['status'] for row in outcomes.values())),
              'outcomes':list(outcomes.values()),'surface_offset':prior['surface_offset'],
              'terrain_unchanged':True,'untouched_geometry_unchanged':True,'reopen_verified':True,
              'material_status':'exact_references_and_slots_preserved_neutral_preview'}
    options.audit.write_text(json.dumps(result,indent=2),encoding='utf-8')
    print(json.dumps({'event':'geometry_repairs_reopened_verified','count':len(repairs),'output':str(options.output)}),flush=True)


if __name__ == '__main__':
    main()
