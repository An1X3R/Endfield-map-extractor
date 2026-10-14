"""Validate authoring additions without treating intentional new shapes as corruption."""
from __future__ import annotations

import hashlib
import struct
from collections.abc import Mapping
from typing import TypedDict

import bpy

from audit_character_materials import source_geometry_without_shapes


class SignaturePart(TypedDict):
    field: str
    payload: str


class SignatureTree(TypedDict):
    owner: str
    tree_name: str
    users: int
    fake_user: bool
    parts: list[SignaturePart]
    sha256: str


class MaterialSignatureDetails(TypedDict):
    format: str
    scope: str
    sha256: str
    trees: list[SignatureTree]
    excluded_node_groups: list[SignatureTree]
    excluded_materials: list[SignatureTree]


def _signature_parts(tree: bpy.types.NodeTree) -> list[SignaturePart]:
    """Retain the exact legacy byte stream and label every contribution."""
    parts: list[SignaturePart] = [{'field': 'tree_name', 'payload': tree.name + '\0'}]
    driver_paths = frozenset(curve.data_path for curve in tree.animation_data.drivers) if tree.animation_data else frozenset()
    if tree.animation_data:
        for index, curve in enumerate(tree.animation_data.drivers):
            prefix = f'driver[{index}]'
            parts.append({'field': prefix, 'payload': repr((curve.data_path, curve.array_index, curve.driver.type, curve.driver.expression))})
            for variable_index, variable in enumerate(curve.driver.variables):
                parts.append({'field': f'{prefix}.variable[{variable_index}]', 'payload': repr((variable.name, variable.type))})
                for target_index, target in enumerate(variable.targets):
                    parts.append({'field': f'{prefix}.variable[{variable_index}].target[{target_index}]',
                                  'payload': repr((target.id.name_full if target.id else None, target.data_path, target.bone_target, target.transform_type, target.transform_space))})
    for node in sorted(tree.nodes, key=lambda node: node.name):
        prefix = f'node[{node.name}]'
        parts.append({'field': prefix, 'payload': node.name + '|' + node.bl_idname + '|' + node.label + '\0'})
        if node.type == 'GROUP':
            parts.append({'field': prefix + '.node_tree', 'payload': node.node_tree.name})
        if node.type == 'TEX_IMAGE' and node.image:
            parts.append({'field': prefix + '.image', 'payload': node.image.name})
        for index, socket in enumerate(node.inputs):
            if not hasattr(socket, 'default_value'):
                continue
            field = f'{prefix}.input[{index}:{socket.identifier}].default_value'
            path = socket.path_from_id('default_value')
            if path in driver_paths:
                payload = 'DRIVEN:' + path
            else:
                value = socket.default_value
                if isinstance(value, (float, int, bool, str)):
                    payload = repr(value)
                elif isinstance(value, bpy.types.ID):
                    payload = value.name_full
                else:
                    payload = repr(tuple(value))
            parts.append({'field': field, 'payload': payload})
        for property_name in ('operation', 'blend_type', 'vector_type', 'convert_from', 'convert_to'):
            if hasattr(node, property_name):
                parts.append({'field': prefix + '.' + property_name, 'payload': str(getattr(node, property_name))})
    for index, link in enumerate(sorted(tree.links, key=lambda link: (link.from_node.name, link.from_socket.identifier, link.to_node.name, link.to_socket.identifier))):
        parts.append({'field': f'link[{index}]', 'payload': repr((link.from_node.name, link.from_socket.identifier, link.to_node.name, link.to_socket.identifier))})
    return parts


def _has_persistent_reference(datablock: bpy.types.ID, users_by_id: Mapping[bpy.types.ID, set[bpy.types.ID]]) -> bool:
    """Follow Blender ID users to a saved scene or an explicit fake user."""
    pending = [datablock]
    visited: set[int] = set()
    while pending:
        owner = pending.pop()
        pointer = owner.as_pointer()
        if pointer in visited:
            continue
        visited.add(pointer)
        if owner.bl_rna.identifier == 'Scene' or owner.use_fake_user:
            return True
        pending.extend(users_by_id.get(owner, ()))
    return False


def material_signature_details() -> MaterialSignatureDetails:
    """Hash persistent trees; retain diagnostics for unsaved standalone orphans."""
    users_by_id = bpy.data.user_map()
    owners = tuple(('node_group:' + tree.name, tree, _has_persistent_reference(tree, users_by_id)) for tree in bpy.data.node_groups) + tuple(
        ('material:' + material.name, material.node_tree, _has_persistent_reference(material, users_by_id))
        for material in bpy.data.materials if material.use_nodes)
    trees: list[SignatureTree] = []
    excluded: list[SignatureTree] = []
    excluded_materials: list[SignatureTree] = []
    digest = hashlib.sha256()
    for owner, tree, persistent in sorted(owners, key=lambda item: item[1].name):
        parts = _signature_parts(tree)
        payload = ''.join(part['payload'] for part in parts).encode()
        entry: SignatureTree = {'owner': owner, 'tree_name': tree.name, 'users': tree.users,
                                'fake_user': tree.use_fake_user, 'parts': parts, 'sha256': hashlib.sha256(payload).hexdigest()}
        # Embedded trees persist according to their owning material's users.
        # Unreferenced datablocks without a fake user are omitted by blend saving.
        if not persistent:
            if owner.startswith('node_group:'):
                excluded.append(entry)
            else:
                excluded_materials.append(entry)
        else:
            digest.update(payload)
            trees.append(entry)
    return {'format': 'EndfieldMaterialSignatureDetails/1',
            'scope': 'Used or fake-user standalone node groups and materials. Embedded trees follow material ownership. Hash payloads retain the legacy byte encoding.',
            'sha256': digest.hexdigest(), 'trees': trees, 'excluded_node_groups': excluded,
            'excluded_materials': excluded_materials}


def material_signature() -> str:
    """Hash graph/static values and driver definitions, excluding evaluated driver outputs."""
    return material_signature_details()['sha256']


def material_animation_signature() -> str:
    digest = hashlib.sha256()
    owners = tuple(bpy.data.node_groups) + tuple(material.node_tree for material in bpy.data.materials if material.use_nodes)
    for tree in sorted(owners, key=lambda tree: tree.name):
        if not tree.animation_data or not tree.animation_data.action:
            continue
        action = tree.animation_data.action
        digest.update((tree.name + '|' + action.name).encode())
        for curve in action.fcurves:
            digest.update(repr((curve.data_path, curve.array_index)).encode())
            for point in curve.keyframe_points:
                digest.update(struct.pack('<2f', *point.co))
                digest.update(point.interpolation.encode())
    return digest.hexdigest()


def shape_signatures(names: tuple[str, ...]) -> dict[str, dict[str, str]]:
    result: dict[str, dict[str, str]] = {}
    for name in names:
        obj = bpy.data.objects[name]
        keys = obj.data.shape_keys
        result[name] = {}
        if keys is None:
            continue
        for block in keys.key_blocks:
            digest = hashlib.sha256()
            digest.update(repr((block.relative_key.name, block.vertex_group, block.interpolation)).encode())
            for point in block.data:
                digest.update(struct.pack('<3f', *point.co))
            result[name][block.name] = digest.hexdigest()
    return result


def character_invariants(names: tuple[str, ...]) -> dict[str, object]:
    images = {image.name: hashlib.sha256(image.packed_file.data).hexdigest()
              for image in bpy.data.images if image.packed_file is not None}
    rigs = {}
    for obj in bpy.data.objects:
        if obj.type != 'ARMATURE':
            continue
        digest = hashlib.sha256()
        for bone in obj.data.bones:
            digest.update((bone.name + '|' + (bone.parent.name if bone.parent else '')).encode())
            for row in bone.matrix_local:
                digest.update(struct.pack('<4f', *row))
        rigs[obj.name] = digest.hexdigest()
    return {'geometry_uv_skin_modifiers_visibility': source_geometry_without_shapes(names),
            'material_graph_static_inputs_and_drivers': material_signature(),
            'material_animation': material_animation_signature(),
            'packed_images': images, 'rig_bones_and_rest': rigs}


def verify_preserved_shapes(original: dict[str, dict[str, str]], names: tuple[str, ...]) -> None:
    current = shape_signatures(names)
    for obj, shapes in original.items():
        for name, digest in shapes.items():
            if current[obj].get(name) != digest:
                raise AssertionError(f'Original shape changed or disappeared: object={obj}, shape={name}')


def verify_invariants(expected: dict[str, object], names: tuple[str, ...]) -> None:
    current = character_invariants(names)
    changed = tuple(key for key in expected if current[key] != expected[key])
    if changed:
        raise AssertionError(f'Authoring changed immutable source data: fields={changed}')

