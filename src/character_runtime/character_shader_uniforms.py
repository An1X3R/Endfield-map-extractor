"""Replace shader drivers with native attributes and receiver-owned matrix uniforms."""
from __future__ import annotations

import ast
from collections.abc import Sequence
import json
import math
import re
from typing import TypedDict, cast
from uuid import uuid4

import bpy


FORMAT = 'EndfieldShaderUniforms/1'
MARKER = 'endfield_shader_uniform'
INPUT_MARKER = 'endfield_shader_matrix_input'
SCENE_PATHS = {
    'fps': ('render.fps',), 'fps_base': ('render.fps_base',),
    'frame_start': ('frame_start',), 'rx': ('render.resolution_x',),
    'ry': ('render.resolution_y',), 'px': ('render.pixel_aspect_x',),
    'py': ('render.pixel_aspect_y',), 'scale': ('camera.data.ortho_scale',),
    'lens': ('camera.data.lens',),
    'sensor': ('camera.data.sensor_width', 'camera.data.sensor_height'),
}


class UniformVariable(TypedDict):
    name: str
    path: str
    kind: str
    key: str


class UniformEntry(TypedDict):
    tree: str
    node: str
    path: str
    expression: str
    result_node: str
    variables: list[UniformVariable]


class SkippedDriver(TypedDict):
    tree: str
    path: str
    reason: str


class ShaderUniformReport(TypedDict):
    format: str
    migrated: int
    bindings: int
    skipped: list[SkippedDriver]
    entries: list[UniformEntry]


class UniformPlan(TypedDict):
    tree: bpy.types.NodeTree
    node: bpy.types.Node
    curve: bpy.types.FCurve
    syntax: ast.Expression
    variables: list[UniformVariable]
    targets: dict[str, bpy.types.Object]


class MatrixInputPlan(TypedDict):
    tree: bpy.types.NodeTree
    socket: bpy.types.NodeSocket
    curve: bpy.types.FCurve
    target: bpy.types.Object
    variable: str
    source_path: str
    key: str


def _trees(material: bpy.types.Material) -> tuple[bpy.types.NodeTree, ...]:
    pending = [material.node_tree] if material.use_nodes and material.node_tree else []
    found: dict[int, bpy.types.NodeTree] = {}
    while pending:
        tree = pending.pop()
        if tree.as_pointer() in found:
            continue
        found[tree.as_pointer()] = tree
        pending.extend(node.node_tree for node in tree.nodes
                       if node.bl_idname == 'ShaderNodeGroup' and node.node_tree is not None)
    return tuple(found.values())


def _validate_expression(item: ast.AST, names: frozenset[str], depth: int) -> None:
    if depth > 16:
        raise ValueError('Shader uniform expression exceeds 16 levels')
    if isinstance(item, ast.Expression):
        _validate_expression(item.body, names, depth + 1)
    elif isinstance(item, ast.Constant) and type(item.value) in (int, float):
        if not math.isfinite(float(item.value)):
            raise ValueError('Shader uniform expression contains a non-finite constant')
    elif isinstance(item, ast.Name) and item.id in names:
        return
    elif isinstance(item, ast.BinOp) and isinstance(item.op, (ast.Add, ast.Sub, ast.Mult, ast.Div)):
        _validate_expression(item.left, names, depth + 1)
        _validate_expression(item.right, names, depth + 1)
    elif isinstance(item, ast.UnaryOp) and isinstance(item.op, (ast.UAdd, ast.USub)):
        _validate_expression(item.operand, names, depth + 1)
    elif (isinstance(item, ast.Call) and isinstance(item.func, ast.Name)
          and item.func.id in ('min', 'max') and 2 <= len(item.args) <= 4 and not item.keywords):
        for argument in item.args:
            _validate_expression(argument, names, depth + 1)
    else:
        raise ValueError(f'Unsupported shader uniform expression element: {type(item).__name__}')


def _scalar(scene: bpy.types.Scene, path: str) -> None:
    value = scene.path_resolve(path)
    if type(value) not in (int, float) or not math.isfinite(float(value)):
        raise ValueError(f'Shader uniform requires a finite scalar: scene={scene.name}, path={path}')


def _identity_modifiers(curve: bpy.types.FCurve) -> bool:
    return not curve.keyframe_points and not curve.sampled_points and all(modifier.mute or (
        modifier.type == 'GENERATOR' and modifier.mode == 'POLYNOMIAL'
        and modifier.poly_order == 1 and tuple(modifier.coefficients) == (0., 1.)
        and not modifier.use_additive and not modifier.use_influence
        and not modifier.use_restricted_range) for modifier in curve.modifiers)


def _plan(tree: bpy.types.NodeTree, curve: bpy.types.FCurve,
          scene: bpy.types.Scene) -> UniformPlan | str:
    driver = curve.driver
    if driver.type != 'SCRIPTED' or curve.mute or driver.use_self or not _identity_modifiers(curve):
        return 'Driver is outside the supported scripted scalar contract'
    nodes = tuple(node for node in tree.nodes if node.bl_idname == 'ShaderNodeValue'
                  and node.outputs[0].path_from_id('default_value') == curve.data_path)
    if len(nodes) != 1 or curve.array_index != 0:
        return 'Driver does not control one Value output scalar'
    variables: list[UniformVariable] = []
    targets: dict[str, bpy.types.Object] = {}
    for variable in driver.variables:
        if variable.type != 'SINGLE_PROP' or len(variable.targets) != 1:
            return 'Driver uses an unrelated variable type'
        target = variable.targets[0]
        if target.id == scene and target.data_path in SCENE_PATHS.get(variable.name, ()):
            _scalar(scene, target.data_path)
            variables.append({'name': variable.name, 'path': target.data_path,
                              'kind': 'VIEW_LAYER', 'key': ''})
        elif (variable.name == 'value' and target.data_path == 'hide_render'
              and isinstance(target.id, bpy.types.Object) and target.id.type == 'LIGHT'
              and driver.expression.replace(' ', '') == '1-value' and len(driver.variables) == 1):
            key = 'ef_hgrp_light_' + uuid4().hex[:20]
            targets[key] = target.id
            variables.append({'name': variable.name, 'path': '[' + json.dumps(key) + '].hide_render',
                              'kind': 'OBJECT', 'key': key})
        else:
            return 'Driver references an unrelated property'
    if not variables:
        return 'Driver has no supported external scalar inputs'
    syntax = ast.parse(driver.expression, mode='eval')
    if len(tuple(ast.walk(syntax))) > 128:
        raise ValueError(f'Shader uniform expression exceeds 128 elements: {tree.name}/{curve.data_path}')
    _validate_expression(syntax, frozenset(item['name'] for item in variables) | {'frame'}, 0)
    if not any(isinstance(item, ast.Name) and item.id in
               (frozenset(row['name'] for row in variables) | {'frame'}) for item in ast.walk(syntax)):
        return 'Driver expression does not consume its external scalar inputs'
    if any(isinstance(item, ast.Name) and item.id == 'frame' for item in ast.walk(syntax)):
        _scalar(scene, 'frame_current_final')
        variables.append({'name': 'frame', 'path': 'frame_current_final', 'kind': 'VIEW_LAYER', 'key': ''})
    return {'tree': tree, 'node': nodes[0], 'curve': curve, 'syntax': syntax,
            'variables': variables, 'targets': targets}


def _receipt(tree: bpy.types.NodeTree, node: bpy.types.Node) -> UniformEntry:
    raw = node.get(MARKER)
    if not isinstance(raw, str):
        raise ValueError(f'Shader uniform receipt is not JSON text: {tree.name}/{node.name}')
    document: object = json.loads(raw)
    if not isinstance(document, dict) or document.get('format') != FORMAT:
        raise ValueError(f'Unsupported shader uniform receipt: {tree.name}/{node.name}')
    fields = ('tree', 'node', 'path', 'expression', 'result_node')
    if not all(isinstance(document.get(key), str) for key in fields):
        raise ValueError(f'Invalid shader uniform receipt fields: {tree.name}/{node.name}')
    rows = document.get('variables')
    if not isinstance(rows, list) or not all(
        isinstance(row, dict) and all(isinstance(row.get(key), str) for key in ('name', 'path', 'kind', 'key'))
        and row.get('kind') in ('OBJECT', 'VIEW_LAYER') for row in rows
    ):
        raise ValueError(f'Invalid shader uniform receipt variables: {tree.name}/{node.name}')
    entry = cast(UniformEntry, {key: document[key] for key in fields} | {'variables': rows})
    if tree.nodes.get(entry['result_node']) is None:
        raise ValueError(f'Shader uniform result node is missing: {tree.name}/{entry["result_node"]}')
    return {**entry, 'tree': tree.name, 'node': node.name}


def _matrix_path(name: str, path: str) -> bool:
    # Keep explicit hair-frame constants until real-character render parity is established.
    match = re.fullmatch(r'matrix_world\[([0-3])\]\[([0-2])\]', path)
    return match is not None and (
        (name == 'origin_component' and match[1] == '3')
        or (name == 'basis_component' and match[1] != '3'))


def _matrix_plan(tree: bpy.types.NodeTree, curve: bpy.types.FCurve) -> MatrixInputPlan | None:
    driver = curve.driver
    if (driver.type != 'SCRIPTED' or curve.mute or driver.use_self
            or curve.array_index != 0 or not _identity_modifiers(curve) or len(driver.variables) != 1):
        return None
    variable = driver.variables[0]
    if variable.type != 'SINGLE_PROP' or driver.expression != variable.name:
        return None
    target = variable.targets[0]
    if (not isinstance(target.id, bpy.types.Object) or target.id.type != 'EMPTY'
            or target.use_fallback_value or not _matrix_path(variable.name, target.data_path)):
        return None
    sockets = tuple(socket for node in tree.nodes if node.bl_idname == 'ShaderNodeCombineXYZ'
                    for socket in node.inputs if socket.path_from_id('default_value') == curve.data_path)
    if len(sockets) != 1 or sockets[0].is_linked:
        return None
    value = target.id.path_resolve(target.data_path)
    if type(value) not in (int, float) or not math.isfinite(float(value)):
        raise ValueError(f'Matrix uniform source is not finite: {tree.name}/{target.data_path}')
    return {'tree': tree, 'socket': sockets[0], 'curve': curve, 'target': target.id,
            'variable': variable.name, 'source_path': target.data_path,
            'key': 'ef_matrix_' + uuid4().hex[:20]}


def _matrix_receipt(tree: bpy.types.NodeTree, node: bpy.types.Node) -> UniformEntry:
    raw = node.get(INPUT_MARKER)
    if not isinstance(raw, str):
        raise ValueError(f'Matrix uniform receipt is not JSON text: {tree.name}/{node.name}')
    document: object = json.loads(raw)
    fields = ('tree', 'node', 'path', 'expression', 'result_node')
    if (not isinstance(document, dict) or document.get('format') != FORMAT
            or not all(isinstance(document.get(key), str) for key in fields)):
        raise ValueError(f'Invalid matrix uniform receipt: {tree.name}/{node.name}')
    rows = document.get('variables')
    if (not isinstance(rows, list) or len(rows) != 1 or not isinstance(rows[0], dict)
            or not all(isinstance(rows[0].get(key), str) for key in ('name', 'path', 'kind', 'key'))):
        raise ValueError(f'Invalid matrix uniform variable: {tree.name}/{node.name}')
    variable = cast(UniformVariable, {key: rows[0][key] for key in ('name', 'path', 'kind', 'key')})
    if (variable['kind'] != 'RECEIVER_MATRIX' or not variable['key'].startswith('ef_matrix_')
            or not _matrix_path(variable['name'], variable['path'])
            or document['expression'] != variable['name']):
        raise ValueError(f'Matrix uniform source contract changed: {tree.name}/{node.name}')
    entry = cast(UniformEntry, {key: document[key] for key in fields} | {'variables': [variable]})
    if (node.bl_idname != 'ShaderNodeAttribute' or node.attribute_type != 'OBJECT'
            or node.attribute_name != variable['key']):
        raise ValueError(f'Matrix uniform attribute changed: {tree.name}/{node.name}')
    result = tree.nodes.get(entry['result_node'])
    if (result is None or result.bl_idname != 'ShaderNodeSeparateXYZ'
            or len(result.inputs[0].links) != 1
            or result.inputs[0].links[0].from_socket != node.outputs['Vector']):
        raise ValueError(f'Matrix uniform component extraction changed: {tree.name}/{node.name}')
    socket = tree.path_resolve(entry['path'].removesuffix('.default_value'))
    if (not isinstance(socket, bpy.types.NodeSocket) or socket.node.bl_idname != 'ShaderNodeCombineXYZ'
            or socket.is_output or socket.node.name != entry['node'] or len(socket.links) != 1
            or socket.links[0].from_socket != result.outputs['X']):
        raise ValueError(f'Matrix uniform input binding changed: {tree.name}/{entry["path"]}')
    if tree.animation_data and any(curve.data_path == entry['path'] for curve in tree.animation_data.drivers):
        raise ValueError(f'Matrix uniform still has a shader driver: {tree.name}/{entry["path"]}')
    return {**entry, 'tree': tree.name}


def _matrix_receiver_target(owner: bpy.types.Object, variable: UniformVariable) -> bpy.types.Object:
    key = variable['key']
    if key not in owner or type(owner[key]) not in (int, float) or not math.isfinite(float(owner[key])):
        raise ValueError(f'Matrix uniform receiver scalar is invalid: {owner.name}/{key}')
    path = '[' + json.dumps(key) + ']'
    curves = tuple(curve for curve in owner.animation_data.drivers if curve.data_path == path) if owner.animation_data else ()
    if len(curves) != 1:
        raise ValueError(f'Matrix uniform receiver driver is missing or ambiguous: {owner.name}/{key}')
    curve = curves[0]
    driver = curve.driver
    if (driver.type != 'SCRIPTED' or driver.expression != variable['name'] or driver.use_self
            or curve.mute or curve.array_index != 0 or not _identity_modifiers(curve)
            or len(driver.variables) != 1):
        raise ValueError(f'Matrix uniform receiver expression changed: {owner.name}/{key}')
    source = driver.variables[0]
    target = source.targets[0]
    if (source.type != 'SINGLE_PROP' or source.name != variable['name']
            or target.data_path != variable['path'] or target.use_fallback_value
            or not isinstance(target.id, bpy.types.Object) or target.id.type != 'EMPTY'):
        raise ValueError(f'Matrix uniform receiver target changed: {owner.name}/{key}')
    return target.id


def _matrix_binding_target(tree: bpy.types.NodeTree, entry: UniformEntry,
                           consumers: Sequence[bpy.types.Object]) -> bpy.types.Object:
    variable = entry['variables'][0]
    existing = tuple(obj for obj in consumers if variable['key'] in obj)
    if not existing:
        existing = tuple(obj for obj in bpy.data.objects if variable['key'] in obj)
    targets: dict[int, bpy.types.Object] = {}
    for obj in existing:
        target = _matrix_receiver_target(obj, variable)
        targets[target.as_pointer()] = target
    if len(targets) != 1:
        raise ValueError(f'Matrix uniform target is missing or ambiguous: {tree.name}/{variable["key"]}')
    return next(iter(targets.values()))


def _bind_matrix_receiver(owner: bpy.types.Object, variable: UniformVariable,
                          target: bpy.types.Object) -> None:
    """Keep matrix RNA evaluation outside shader trees without caching animated values."""
    key = variable['key']
    owner[key] = float(target.path_resolve(variable['path']))
    driver = owner.driver_add('[' + json.dumps(key) + ']').driver
    driver.type = 'SCRIPTED'
    driver.expression = variable['name']
    source = driver.variables.new()
    source.name = variable['name']
    source.type = 'SINGLE_PROP'
    source.targets[0].id = target
    source.targets[0].data_path = variable['path']


def _math(tree: bpy.types.NodeTree, operation: str,
          first: bpy.types.NodeSocket | float, second: bpy.types.NodeSocket | float) -> bpy.types.NodeSocket:
    node = tree.nodes.new('ShaderNodeMath')
    node.label = 'Native uniform / ' + operation
    node.operation = operation
    for socket, value in zip(node.inputs, (first, second)):
        if isinstance(value, bpy.types.NodeSocket):
            tree.links.new(value, socket)
        else:
            socket.default_value = value
    return node.outputs[0]


def _lower(tree: bpy.types.NodeTree, item: ast.expr,
           variables: dict[str, bpy.types.NodeSocket]) -> bpy.types.NodeSocket | float:
    if isinstance(item, ast.Constant):
        return float(cast(int | float, item.value))
    if isinstance(item, ast.Name):
        return variables[item.id]
    if isinstance(item, ast.UnaryOp):
        value = _lower(tree, item.operand, variables)
        return _math(tree, 'MULTIPLY', value, -1.) if isinstance(item.op, ast.USub) else value
    if isinstance(item, ast.BinOp):
        operation = {ast.Add: 'ADD', ast.Sub: 'SUBTRACT', ast.Mult: 'MULTIPLY', ast.Div: 'DIVIDE'}[type(item.op)]
        return _math(tree, operation, _lower(tree, item.left, variables), _lower(tree, item.right, variables))
    if isinstance(item, ast.Call) and isinstance(item.func, ast.Name):
        operation = 'MAXIMUM' if item.func.id == 'max' else 'MINIMUM'
        result = _lower(tree, item.args[0], variables)
        for argument in item.args[1:]:
            result = _math(tree, operation, result, _lower(tree, argument, variables))
        return result
    raise ValueError(f'Expression was not preflighted: {type(item).__name__}')


def _input_value(socket: bpy.types.NodeSocket) -> bpy.types.NodeSocket | float:
    if len(socket.links) > 1:
        raise ValueError(f'Shader uniform input has ambiguous links: {socket.node.name}/{socket.name}')
    return socket.links[0].from_socket if socket.links else float(socket.default_value)


def _verify_term(value: bpy.types.NodeSocket | float, item: ast.expr,
                 variables: dict[str, UniformVariable]) -> None:
    if isinstance(item, ast.UnaryOp) and isinstance(item.op, ast.UAdd):
        _verify_term(value, item.operand, variables)
        return
    if isinstance(item, ast.Constant):
        if not isinstance(value, float) or not math.isclose(
            value, float(cast(int | float, item.value)), rel_tol=1e-7, abs_tol=1e-7
        ):
            raise ValueError('Shader uniform numeric input differs from its source expression')
        return
    if not isinstance(value, bpy.types.NodeSocket):
        raise ValueError('Shader uniform expression is missing a linked native node')
    node = value.node
    if isinstance(item, ast.Name):
        variable = variables[item.id]
        if (node.bl_idname != 'ShaderNodeAttribute' or node.attribute_type != variable['kind']
                or node.attribute_name != variable['path'] or value != node.outputs['Fac']):
            raise ValueError(f'Shader uniform attribute binding changed: {node.name}/{item.id}')
        return
    if isinstance(item, ast.Call) and isinstance(item.func, ast.Name):
        operation = 'MAXIMUM' if item.func.id == 'max' else 'MINIMUM'
        left: ast.expr = item.args[0] if len(item.args) == 2 else ast.Call(
            func=item.func, args=item.args[:-1], keywords=[])
        right = item.args[-1]
    elif isinstance(item, ast.BinOp):
        operation = {ast.Add: 'ADD', ast.Sub: 'SUBTRACT', ast.Mult: 'MULTIPLY', ast.Div: 'DIVIDE'}[type(item.op)]
        left, right = item.left, item.right
    elif isinstance(item, ast.UnaryOp) and isinstance(item.op, ast.USub):
        operation, left, right = 'MULTIPLY', item.operand, ast.Constant(value=-1.)
    else:
        raise ValueError(f'Shader uniform receipt contains an unsupported expression: {type(item).__name__}')
    if (node.bl_idname != 'ShaderNodeMath' or node.operation != operation
            or node.use_clamp or value != node.outputs[0]):
        raise ValueError(f'Shader uniform arithmetic changed: {node.name}/{operation}')
    _verify_term(_input_value(node.inputs[0]), left, variables)
    _verify_term(_input_value(node.inputs[1]), right, variables)


def resolve_shader_uniform_socket(tree: bpy.types.NodeTree,
                                   source: bpy.types.NodeSocket) -> bpy.types.NodeSocket:
    """Resolve and validate an original Value output or its native replacement."""
    if source.id_data != tree:
        raise ValueError(f'Shader uniform socket belongs to another tree: {tree.name}')
    if MARKER not in source.node:
        return source
    if source.node.bl_idname != 'ShaderNodeValue' or source != source.node.outputs[0]:
        raise ValueError(f'Shader uniform receipt must belong to a Value output: {tree.name}')
    entry = _receipt(tree, source.node)
    variables = {item['name']: item for item in entry['variables']}
    if len(variables) != len(entry['variables']):
        raise ValueError(f'Shader uniform receipt repeats a variable: {tree.name}/{source.node.name}')
    for name, variable in variables.items():
        if variable['kind'] == 'VIEW_LAYER':
            allowed = ('frame_current_final',) if name == 'frame' else SCENE_PATHS.get(name, ())
            if variable['key'] or variable['path'] not in allowed:
                raise ValueError(f'Shader uniform scene property contract changed: {tree.name}/{name}')
        else:
            key = variable['key']
            if (name != 'value' or entry['expression'].replace(' ', '') != '1-value'
                    or not key.startswith('ef_hgrp_light_')
                    or variable['path'] != '[' + json.dumps(key) + '].hide_render'):
                raise ValueError(f'Shader uniform light property contract changed: {tree.name}/{name}')
    syntax = ast.parse(entry['expression'], mode='eval')
    _validate_expression(syntax, frozenset(variables), 0)
    result_node = tree.nodes[entry['result_node']]
    result = result_node.outputs['Fac'] if result_node.bl_idname == 'ShaderNodeAttribute' else result_node.outputs[0]
    if tree.animation_data and any(curve.data_path == source.path_from_id('default_value')
                                   for curve in tree.animation_data.drivers):
        raise ValueError(f'Shader uniform still has its broad driver: {tree.name}/{source.node.name}')
    if source.links:
        raise ValueError(f'Shader uniform evidence output was reconnected: {tree.name}/{source.node.name}')
    _verify_term(result, syntax.body, variables)
    return result


def verify_shader_uniforms(materials: Sequence[bpy.types.Material],
                           objects: Sequence[bpy.types.Object],
                           scene: bpy.types.Scene) -> dict[str, str | int]:
    """Validate replacement arithmetic, attributes, and every renderer pointer."""
    owners = _consumer_trees(materials, objects, scene)
    entries = 0
    for tree, consumers in owners.values():
        for node in tree.nodes:
            if INPUT_MARKER in node:
                entry = _matrix_receipt(tree, node)
                target = _matrix_binding_target(tree, entry, consumers)
                for consumer in consumers:
                    if _matrix_receiver_target(consumer, entry['variables'][0]) != target:
                        raise ValueError(f'Matrix uniform receiver target differs: {consumer.name}/{node.name}')
                entries += 1
            if MARKER not in node:
                continue
            resolve_shader_uniform_socket(tree, node.outputs[0])
            entry = _receipt(tree, node)
            entries += 1
            for variable in entry['variables']:
                if variable['kind'] == 'VIEW_LAYER':
                    _scalar(scene, variable['path'])
                else:
                    key = variable['key']
                    target = _binding_target(tree, key, consumers)
                    for consumer in consumers:
                        if consumer.get(key) != target:
                            raise ValueError(f'Shader uniform receiver pointer changed: {consumer.name}/{key}')
    return {'format': FORMAT, 'status': 'verified', 'entries': entries}


def _consumer_trees(materials: Sequence[bpy.types.Material],
                     objects: Sequence[bpy.types.Object], scene: bpy.types.Scene
                     ) -> dict[int, tuple[bpy.types.NodeTree, tuple[bpy.types.Object, ...]]]:
    selected = {tree.as_pointer(): tree for material in materials for tree in _trees(material)}
    renderers = {obj.as_pointer(): obj for obj in tuple(scene.objects) + tuple(objects)}
    used = {slot.material.as_pointer(): slot.material for obj in renderers.values()
            for slot in obj.material_slots if slot.material is not None}
    roots = {pointer: frozenset(tree.as_pointer() for tree in _trees(material))
             for pointer, material in used.items()}
    return {pointer: (tree, tuple(obj for obj in renderers.values() if any(
        slot.material is not None and pointer in roots[slot.material.as_pointer()]
        for slot in obj.material_slots))) for pointer, tree in selected.items()}


def _binding_target(tree: bpy.types.NodeTree, key: str,
                     consumers: Sequence[bpy.types.Object]) -> bpy.types.Object:
    existing = tuple(obj for obj in consumers if key in obj)
    if not existing:
        existing = tuple(obj for obj in bpy.data.objects if key in obj and any(
            slot.material is not None and tree in _trees(slot.material) for slot in obj.material_slots))
    targets: dict[int, bpy.types.Object] = {}
    for obj in existing:
        target = obj[key]
        if not isinstance(target, bpy.types.Object) or target.type != 'LIGHT':
            raise ValueError(f'Shader uniform receiver light pointer is invalid: {obj.name}/{key}')
        targets[target.as_pointer()] = target
    if len(targets) != 1:
        raise ValueError(f'Shader uniform light pointer is missing or ambiguous: tree={tree.name}, key={key}, targets={len(targets)}')
    return next(iter(targets.values()))


def migrate_shader_uniforms(materials: Sequence[bpy.types.Material],
                            objects: Sequence[bpy.types.Object],
                            scene: bpy.types.Scene) -> ShaderUniformReport:
    """Migrate recognized scalar drivers after validating every graph and binding."""
    owners = _consumer_trees(materials, objects, scene)
    plans: list[UniformPlan] = []
    matrix_plans: list[MatrixInputPlan] = []
    entries: list[UniformEntry] = []
    skipped: list[SkippedDriver] = []
    bindings: list[tuple[bpy.types.Object, str, bpy.types.Object]] = []
    matrix_bindings: list[tuple[bpy.types.Object, UniformVariable, bpy.types.Object]] = []
    for tree, consumers in owners.values():
        for node in tree.nodes:
            if INPUT_MARKER in node:
                entry = _matrix_receipt(tree, node)
                target = _matrix_binding_target(tree, entry, consumers)
                entries.append(entry)
                matrix_bindings.extend((obj, entry['variables'][0], target) for obj in consumers)
            if MARKER not in node:
                continue
            entry = _receipt(tree, node)
            resolve_shader_uniform_socket(tree, node.outputs[0])
            entries.append(entry)
            for variable in entry['variables']:
                if variable['kind'] != 'OBJECT':
                    continue
                key = variable['key']
                target = _binding_target(tree, key, consumers)
                bindings.extend((obj, key, target) for obj in consumers)
        for curve in tuple(tree.animation_data.drivers) if tree.animation_data else ():
            matrix_plan = _matrix_plan(tree, curve)
            if matrix_plan is not None:
                if tree.library is not None or not tree.is_editable:
                    raise ValueError(f'Matrix uniform tree is not editable: {tree.name}')
                if not consumers:
                    raise ValueError(f'Matrix uniform has no renderer consumers: {tree.name}')
                matrix_plans.append(matrix_plan)
                variable: UniformVariable = {'name': matrix_plan['variable'], 'path': matrix_plan['source_path'],
                                             'kind': 'RECEIVER_MATRIX', 'key': matrix_plan['key']}
                matrix_bindings.extend((obj, variable, matrix_plan['target']) for obj in consumers)
                continue
            plan = _plan(tree, curve, scene)
            if isinstance(plan, str):
                skipped.append({'tree': tree.name, 'path': curve.data_path, 'reason': plan})
                continue
            if tree.library is not None or not tree.is_editable:
                raise ValueError(f'Shader uniform tree is not editable: {tree.name}')
            plans.append(plan)
            for key, target in plan['targets'].items():
                if not consumers:
                    raise ValueError(f'Shader uniform has no supplied renderer consumers: {tree.name}')
                bindings.extend((obj, key, target) for obj in consumers)
    for owner, key, target in bindings:
        if key in owner and owner[key] != target:
            raise ValueError(f'Shader uniform pointer conflicts with authored data: {owner.name}/{key}')
        if key not in owner and (owner.library is not None or not owner.is_editable):
            raise ValueError(f'Shader uniform consumer is not editable: {owner.name}/{key}')
    for owner, variable, target in matrix_bindings:
        key = variable['key']
        if key in owner:
            if _matrix_receiver_target(owner, variable) != target:
                raise ValueError(f'Matrix uniform receiver target conflicts: {owner.name}/{key}')
        elif owner.library is not None or not owner.is_editable:
            raise ValueError(f'Matrix uniform consumer is not editable: {owner.name}/{key}')
        elif owner == target:
            raise ValueError(f'Matrix uniform receiver cannot drive itself: {owner.name}/{key}')
    bound = 0
    for owner, key, target in bindings:
        if key not in owner:
            owner[key] = target
            bound += 1
    for owner, variable, target in matrix_bindings:
        if variable['key'] not in owner:
            _bind_matrix_receiver(owner, variable, target)
            bound += 1
    for matrix_plan in matrix_plans:
        tree, socket, curve = matrix_plan['tree'], matrix_plan['socket'], matrix_plan['curve']
        attr = tree.nodes.new('ShaderNodeAttribute')
        attr.label = 'Native matrix uniform / ' + matrix_plan['variable']
        attr.attribute_type = 'OBJECT'
        attr.attribute_name = matrix_plan['key']
        attr.location = (socket.node.location.x - 520., socket.node.location.y)
        component = tree.nodes.new('ShaderNodeSeparateXYZ')
        component.label = 'Exact matrix scalar / X'
        component.location = (socket.node.location.x - 260., socket.node.location.y)
        tree.links.new(attr.outputs['Vector'], component.inputs[0])
        tree.links.new(component.outputs['X'], socket)
        entry: UniformEntry = {'tree': tree.name, 'node': socket.node.name, 'path': curve.data_path,
            'expression': curve.driver.expression, 'result_node': component.name,
            'variables': [{'name': matrix_plan['variable'], 'path': matrix_plan['source_path'],
                           'kind': 'RECEIVER_MATRIX', 'key': matrix_plan['key']}]}
        socket.driver_remove('default_value')
        attr[INPUT_MARKER] = json.dumps({'format': FORMAT, **entry}, sort_keys=True)
        entries.append(entry)
    for plan in plans:
        tree, source, curve = plan['tree'], plan['node'], plan['curve']
        inputs: dict[str, bpy.types.NodeSocket] = {}
        created_before = {node.as_pointer() for node in tree.nodes}
        for variable in plan['variables']:
            attr = tree.nodes.new('ShaderNodeAttribute')
            attr.label = 'Native uniform / ' + variable['name']
            attr.attribute_type = variable['kind']
            attr.attribute_name = variable['path']
            inputs[variable['name']] = attr.outputs['Fac']
        result = _lower(tree, plan['syntax'].body, inputs)
        if not isinstance(result, bpy.types.NodeSocket):
            raise ValueError(f'Shader uniform expression did not produce a socket: {tree.name}')
        targets = tuple(link.to_socket for link in source.outputs[0].links)
        for target in targets:
            tree.links.new(result, target)
        entry: UniformEntry = {'tree': tree.name, 'node': source.name, 'path': curve.data_path,
            'expression': curve.driver.expression, 'result_node': result.node.name, 'variables': plan['variables']}
        source.outputs[0].driver_remove('default_value')
        source[MARKER] = json.dumps({'format': FORMAT, **entry}, sort_keys=True)
        for index, node in enumerate(node for node in tree.nodes if node.as_pointer() not in created_before):
            node.location = (source.location.x - 260. * (1 + index % 3), source.location.y - 140. * (index // 3))
        entries.append(entry)
    return {'format': FORMAT, 'migrated': len(plans) + len(matrix_plans), 'bindings': bound,
            'skipped': skipped, 'entries': entries}
