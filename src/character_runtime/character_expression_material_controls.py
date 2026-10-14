"""Bind exact source material-property RIDs independently of skeletal poses."""
from __future__ import annotations

from collections.abc import Callable
import hashlib
import json
from pathlib import Path

import bpy

from character_authoring_identity import validate_authoring_identity
from character_emotion_atlas import attach_visible_emotion_atlases, verify_emotion_atlases
from endfield_character_expression_tables import read_expression_material_properties


SOCKETS = {'_EmotionIndex': ('Emotion Index', 3.), '_EmotionBlend': ('Emotion Blend', 1.)}


def inherited_expression_material_controls() -> dict[str, object] | None:
    """Read and validate a previous authoring stage without rebinding it."""
    existing = tuple(obj for obj in bpy.data.objects if obj.get('expression_material_controls') is not None)
    if not existing:
        return None
    if len(existing) != 1:
        raise ValueError('Multiple source material expression controllers are present')
    previous = json.loads(existing[0]['expression_material_controls'])
    verify_expression_material_controls(previous)
    return previous


def attach_expression_material_controls(
    table: Path, source_audit: Path, layout: Callable[[bpy.types.NodeTree], None],
) -> dict[str, object]:
    """Attach artist controls only to source-enabled visible material instances."""
    payload = table.read_bytes()
    document = json.loads(payload.decode('utf-8-sig'))
    properties = read_expression_material_properties(document)
    if not properties:
        return {'status': 'not_applicable', 'bindings': [], 'reason': 'No supported source material-property mappings.'}
    identity = validate_authoring_identity(source_audit, table, None, ())
    if document['provenance']['source_audit_sha256'] != hashlib.sha256(source_audit.read_bytes()).hexdigest():
        raise ValueError('Expression material source audit hash does not match the selected table')
    if any(item.renderer_mask != -1 or item.blend_mode != 0 for item in properties):
        raise ValueError('Emotion material controls require source all-renderer mask and direct float blend mode')
    previous = inherited_expression_material_controls()
    if previous is not None:
        if (previous['table_sha256'] != hashlib.sha256(payload).hexdigest()
                or previous['source_audit_sha256'] != hashlib.sha256(source_audit.read_bytes()).hexdigest()):
            raise ValueError('Existing material expression controls belong to a different source table/audit')
        return previous
    if len({item.property_name for item in properties}) != len(properties):
        raise ValueError('Emotion material property has multiple source mappings')
    atlas = attach_visible_emotion_atlases(layout)
    groups = {row['group'] for row in atlas['bindings']}
    if not groups:
        return {'status': 'not_applicable', 'bindings': [], 'reason': 'Selected source has no visible emotion-enabled material.',
                'identity': identity, 'atlas': atlas}
    materials = {slot.material.name: slot.material for obj in bpy.data.objects
                 if obj.type == 'MESH' and not obj.hide_render and not obj.hide_get()
                 for slot in obj.material_slots if slot.material is not None and slot.material.use_nodes}
    pending = [('MATERIAL', material.name, material.node_tree) for material in materials.values()]
    owners = []
    visited: set[int] = set()
    while pending:
        kind, name, owner = pending.pop()
        if owner.as_pointer() in visited:
            continue
        visited.add(owner.as_pointer())
        owners.append((kind, name, owner))
        pending.extend(('GROUP', item.node_tree.name, item.node_tree) for item in owner.nodes
                       if item.type == 'GROUP' and item.node_tree is not None)
    consumers = tuple((kind, name, owner, node) for kind, name, owner in owners for node in owner.nodes
                      if node.type == 'GROUP' and node.node_tree is not None and node.node_tree.name in groups)
    if not consumers:
        raise ValueError('Source emotion atlas has no live material instances')
    controller = bpy.data.objects.new('EF Source Material Expression Controls', None)
    bpy.context.scene.collection.objects.link(controller)
    controller['source_table_sha256'] = hashlib.sha256(payload).hexdigest()
    controller['source_scope'] = 'Source material parameters; artist selection, not native expression scheduling.'
    bindings = []
    for prop in properties:
        label, maximum = SOCKETS[prop.property_name]
        if not 0. <= prop.default_value <= maximum:
            raise ValueError('Source material property default exceeds its verified range: ' + prop.property_name)
        controller[prop.property_name] = prop.default_value
        controller.id_properties_ui(prop.property_name).update(min=0., max=maximum,
            description=prop.control_name + '; source material property, independent of bone poses.')
        for kind, name, owner, instance in consumers:
            socket = instance.inputs[label]
            if socket.is_linked:
                raise ValueError('Emotion material input is already linked: ' + instance.name + '/' + label)
            path = socket.path_from_id('default_value')
            if owner.animation_data and any(curve.data_path == path for curve in owner.animation_data.drivers):
                raise ValueError('Emotion material input already has a driver: ' + instance.name + '/' + label)
            driver = socket.driver_add('default_value').driver
            driver.type = 'AVERAGE'
            variable = driver.variables.new()
            variable.name = 'source_material_parameter'
            variable.type = 'SINGLE_PROP'
            variable.targets[0].id = controller
            variable.targets[0].data_path = '[' + json.dumps(prop.property_name) + ']'
            bindings.append({'owner_kind': kind, 'owner': name, 'node': instance.name, 'socket': label,
                'driver_path': path, 'property': prop.property_name, 'mapping_rid': prop.mapping_rid,
                'parameter_rid': prop.parameter_rid, 'control_name': prop.control_name,
                'material_identity': instance.node_tree['source_identity']})
    report = {'status': 'attached', 'controller': controller.name, 'bindings': bindings,
              'table': str(table.resolve()), 'table_sha256': controller['source_table_sha256'],
              'source_audit_sha256': hashlib.sha256(source_audit.read_bytes()).hexdigest(), 'identity': identity,
              'atlas': atlas, 'scope': 'Typed source material controls; no skeletal pose, emotion tracker or runtime mixing.'}
    controller['expression_material_controls'] = json.dumps(report, sort_keys=True)
    verify_expression_material_controls(report)
    return report


def verify_expression_material_controls(report: dict[str, object]) -> dict[str, object]:
    """Verify the saved controller, exact driver route and active atlas consumers."""
    if report['status'] == 'not_applicable':
        if report['bindings']:
            raise AssertionError('Not-applicable material controls contain bindings')
        return {'status': 'not_applicable', 'bindings': 0}
    controller = bpy.data.objects.get(report['controller'])
    if controller is None or controller.get('source_table_sha256') != report['table_sha256']:
        raise AssertionError('Expression material controller source identity changed')
    verify_emotion_atlases(report['atlas'])
    for row in report['bindings']:
        if row['owner_kind'] == 'GROUP':
            owner = bpy.data.node_groups.get(row['owner'])
        elif row['owner_kind'] == 'MATERIAL':
            material = bpy.data.materials.get(row['owner'])
            owner = None if material is None else material.node_tree
        else:
            raise AssertionError('Unknown material control owner kind')
        instance = None if owner is None else owner.nodes.get(row['node'])
        if (instance is None or instance.node_tree is None
                or instance.node_tree.get('source_identity') != row['material_identity']):
            raise AssertionError('Expression material instance identity changed')
        socket = instance.inputs[row['socket']]
        curves = () if owner.animation_data is None else tuple(
            curve for curve in owner.animation_data.drivers if curve.data_path == row['driver_path'])
        if len(curves) != 1 or socket.is_linked or socket.path_from_id('default_value') != row['driver_path']:
            raise AssertionError('Expression material socket driver is missing or changed')
        driver = curves[0].driver
        if driver.type != 'AVERAGE' or len(driver.variables) != 1 or driver.variables[0].type != 'SINGLE_PROP':
            raise AssertionError('Expression material driver type changed')
        target = driver.variables[0].targets[0]
        if target.id != controller or target.data_path != '[' + json.dumps(row['property']) + ']':
            raise AssertionError('Expression material driver targets a different control')
    return {'status': 'passed', 'bindings': len(report['bindings']), 'controller': controller.name}
