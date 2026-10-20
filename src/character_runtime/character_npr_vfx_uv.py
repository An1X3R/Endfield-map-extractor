"""Retain signed mesh UV coefficients in the character VFX host adapter."""
from __future__ import annotations

import json
import math
from typing import TYPE_CHECKING

import bpy

if TYPE_CHECKING:
    from endfield_scene_material_contract import MaterialRecord


def _uv_names(record: MaterialRecord) -> tuple[str, ...]:
    return ('_MainTex', *(f'_SampleTex{index}' for index in range(6)
                          if record['Floats'].get(f'_UseSampleTex{index}', 0.0) != 0.0))


def character_vfx_mesh_uv_record(record: MaterialRecord, mesh_names: tuple[str, ...]) -> MaterialRecord:
    """Validate actual UV layers and delegate signed UV0 scaling to explicit nodes."""
    if record['Floats'].get('_InParticle', 0.0) != 0.0:
        return record
    colors = {key: list(value) for key, value in record['Colors'].items()}
    for name in _uv_names(record):
        weights = colors[name + 'UVWeights']
        if len(weights) != 4 or any(not math.isfinite(value) for value in weights):
            raise ValueError(f'Character VFX UV weights must be finite: material={record["Name"]}; property={name}; weights={weights}')
        if weights[1:] == [0.0, 0.0, 0.0] and weights[0] != 1.0:
            colors[name + 'UVWeights'] = [1.0, 0.0, 0.0, 0.0]
        layer = 'UV1' if weights == [0.0, 1.0, 0.0, 0.0] else 'UV0'
        for mesh_name in mesh_names:
            obj = bpy.data.objects.get(mesh_name)
            if obj is None or obj.type != 'MESH' or obj.data.uv_layers.get(layer) is None:
                raise ValueError(f'Character VFX requires its actual imported UV layer: material={record["Name"]}; property={name}; mesh={mesh_name}; layer={layer}')
            coordinates = obj.data.uv_layers[layer].data
            if len(coordinates) != len(obj.data.loops) or any(not math.isfinite(value) for item in coordinates for value in item.uv):
                raise ValueError(f'Character VFX imported UV layer has invalid corner data: material={record["Name"]}; mesh={mesh_name}; layer={layer}')
    return {**record, 'Colors': colors}


def apply_character_vfx_mesh_uv_weights(material: bpy.types.Material, record: MaterialRecord,
                                      mesh_names: tuple[str, ...]) -> None:
    """Scale source UV0 before the existing time, rotation, ST and disturbance nodes."""
    if record['Floats'].get('_InParticle', 0.0) != 0.0:
        return
    tree = material.node_tree
    if tree is None:
        raise ValueError(f'Character VFX factory material has no node tree: material={material.name}')
    evidence: dict[str, str] = {}
    if any(record['Colors'][name + 'UVWeights'] == [0.0, 1.0, 0.0, 0.0] for name in _uv_names(record)):
        for mesh_name in mesh_names:
            obj = bpy.data.objects[mesh_name]
            proof = obj.data.get('endfield_source_missing_uv1_reference')
            if proof is not None:
                if not isinstance(proof, str):
                    raise ValueError(f'Character VFX absent-source UV1 evidence is invalid: mesh={mesh_name}')
                row = json.loads(proof)
                if not isinstance(row, dict) or row.get('status') != 'absent_source_uv1_constant_zero_reference/1' or row.get('source_uv1', 1) is not None:
                    raise ValueError(f'Character VFX absent-source UV1 evidence has no zero-input contract: mesh={mesh_name}')
                if any(value != 0.0 for item in obj.data.uv_layers['UV1'].data for value in item.uv):
                    raise ValueError(f'Character VFX absent-source UV1 reference contains nonzero data: mesh={mesh_name}')
                evidence[mesh_name] = proof
    if evidence:
        material['endfield_character_vfx_missing_uv1_reference'] = json.dumps(evidence, sort_keys=True)
        limits = json.loads(material['endfield_fx_limitations'])
        if not isinstance(limits, list) or any(not isinstance(value, str) for value in limits):
            raise ValueError(f'Character VFX limitation metadata is invalid: material={material.name}')
        limits.append('Verified absent native UV1 uses an explicit constant-zero reference; UV0 is not substituted and native shader program parity remains unverified.')
        material['endfield_fx_limitations'] = json.dumps(limits)
    bindings: dict[str, list[float]] = {}
    for name in _uv_names(record):
        weights = record['Colors'][name + 'UVWeights']
        if weights[1:] != [0.0, 0.0, 0.0] or weights[0] == 1.0:
            continue
        target = tree.nodes.get(name + ' UV Speed')
        if target is None or target.type != 'VECT_MATH' or target.operation != 'ADD':
            raise ValueError(f'Character VFX factory UV consumer is absent: material={record["Name"]}; property={name}')
        links = tuple(target.inputs[0].links)
        if len(links) != 1 or links[0].from_node.type != 'UVMAP':
            raise ValueError(f'Character VFX factory UV0 binding is unexpected: material={record["Name"]}; property={name}')
        scale = tree.nodes.new('ShaderNodeVectorMath')
        scale.name = scale.label = name + ' Source UV0 Weight'
        scale.operation = 'SCALE'
        scale.inputs[3].default_value = weights[0]
        tree.links.new(links[0].from_socket, scale.inputs[0])
        tree.links.new(scale.outputs[0], target.inputs[0])
        bindings[name] = list(weights)
    if bindings:
        material['endfield_character_vfx_mesh_uv_weights'] = json.dumps({
            'bindings': bindings, 'order': 'source UV0 coefficient, time, rotation, ST, disturbance',
            'scope': 'Signed source UV coefficients interpreted in the existing character host preview; native CharacterNPR_VFX compiled program equivalence remains unverified.',
        }, sort_keys=True)
