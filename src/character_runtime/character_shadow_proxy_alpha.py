"""Bind verified Standard ShadowCaster alpha to copied source caster geometry.

The exact reference Shader enables the audited alpha equations. Changed source
objects require an explicit property contract and use host approximations. Blend
coverage uses sqrt(alpha) with stable subdivided-face random sampling, not the
native SV_Position screen hash. Source materials/images/meshes are not edited.
"""
from __future__ import annotations

from dataclasses import dataclass
import hashlib
import json
from pathlib import Path
import struct

import bpy

from rebuild_character_materials import array_value, object_value, string_value, int_value, float_value, identity
from character_shadow_alpha_nodes import alpha_clip, alpha_dither, multiply_clip_mask
from character_material_profile import ShaderCompatibility, validate_shadow_alpha_compatibility


Identity = tuple[str, int]
MODIFIER = 'EF Source Caster Alpha'
OPAQUE_FAMILIES = frozenset(('HGRP/CharacterNPR', 'HGRP/CharacterNPR_Skin', 'HGRP/CharacterNPR_Hair', 'HGRP/CharacterNPR_Eye', 'HGRP/CharacterNPR_OverlayShadow'))


@dataclass(frozen=True, slots=True)
class ShadowAlphaEvidence:
    standard_materials: frozenset[Identity]
    resolved_materials: tuple[tuple[Identity, str], ...]
    shader_identity_sha256: str
    alpha_audit_sha256: str
    shader_object_sha256: str
    compatibility: ShaderCompatibility


def read_shadow_alpha_evidence(shader_identity_path: Path, alpha_audit_path: Path) -> ShadowAlphaEvidence:
    """Join exact source identities and retain the declared alpha evidence boundary."""
    shaders = object_value(json.loads(shader_identity_path.read_text(encoding='utf-8')), 'shader identity')
    audit = object_value(json.loads(alpha_audit_path.read_text(encoding='utf-8')), 'alpha audit')
    if shaders['format'] != 'EndfieldCharacterShaderIdentity/1' or audit['schema'] != 'EndfieldShadowCasterAlphaAudit/1':
        raise ValueError('Unsupported Shader identity/ShadowCaster alpha evidence format')
    target = object_value(audit['shaderIdentity'], 'audited Shader identity')
    if target['name'] != 'HGRP/CharacterNPR':
        raise ValueError('Alpha audit must describe the verified Standard CharacterNPR shader')
    standard: set[Identity] = set()
    resolved: dict[Identity, str] = {}
    matches = 0
    compatibility: ShaderCompatibility | None = None
    for value in array_value(shaders['shaders'], 'shaders'):
        row = object_value(value, 'shader row')
        shader = object_value(row['shader'], 'shader object')
        name = string_value(shader['name'], 'shader name')
        ids = tuple(identity(item, 'shader material') for item in array_value(row['materials'], 'shader materials'))
        for key in ids:
            if key in resolved:
                raise ValueError(f'Duplicate material Shader identity mapping: {key}')
            resolved[key] = name
        if name == target['name']:
            if string_value(shader['source'], 'shader CAB').casefold() != string_value(target['sourceCab'], 'audited CAB').casefold() or str(int_value(shader['path_id'], 'shader PathID')) != target['pathId'] or string_value(row['object_sha256'], 'Shader object SHA').casefold() != string_value(target['serializedObjectSha256'], 'audited Shader SHA').casefold():
                raise ValueError('Standard Shader object identity/hash differs from the actual alpha consumer audit')
            matches += 1
            compatibility = validate_shadow_alpha_compatibility(audit, row)
            standard.update(ids)
    if matches != 1 or not standard or compatibility is None:
        raise ValueError(f'Expected one exact audited Standard Shader with materials: matches={matches}')
    return ShadowAlphaEvidence(frozenset(standard), tuple(sorted(resolved.items())), hashlib.sha256(shader_identity_path.read_bytes()).hexdigest(), hashlib.sha256(alpha_audit_path.read_bytes()).hexdigest(), string_value(target['serializedObjectSha256'], 'audited Shader SHA'), compatibility)


def _group_identity(tree: bpy.types.ShaderNodeTree) -> Identity:
    value = array_value(json.loads(tree['source_identity']), 'source group identity')
    if len(value) != 2:
        raise ValueError(f'Source group CAB/PathID pair is invalid: {tree.name}')
    return string_value(value[0], 'source group CAB').casefold(), int_value(value[1], 'source group PathID')


def _native_material(tree: bpy.types.ShaderNodeTree, key: Identity) -> dict[str, object]:
    records = tuple(object_value(json.loads(text.as_string()), 'source material') for text in bpy.data.texts if text.name.startswith('SOURCE MATERIAL '))
    matches = tuple(row for row in records if identity(row['object'], 'source material identity') == key)
    if len(matches) != 1:
        raise ValueError(f'Exact embedded source material evidence is absent/ambiguous: {key}')
    name = string_value(object_value(matches[0]['object'], 'material object')['name'], 'material name')
    native = tuple(object_value(json.loads(text.as_string()), 'material TypeTree') for text in bpy.data.texts if text.name.startswith('SOURCE TYPETREE '))
    found = tuple(row for row in native if row['m_Name'] == name)
    if len(found) != 1:
        raise ValueError(f'Exact embedded source material TypeTree is absent/ambiguous: {key}/{name}')
    return found[0]


def _properties(native: dict[str, object], field: str) -> dict[str, object]:
    saved = object_value(native['m_SavedProperties'], 'saved material properties')
    result: dict[str, object] = {}
    for value in array_value(saved[field], field):
        row = object_value(value, field+' row')
        key = string_value(row['Key'], field+' key')
        if key in result:
            raise ValueError(f'Duplicate source property: {field}/{key}')
        result[key] = row['Value']
    return result


def _sampling(caster: bpy.types.Object, tree: bpy.types.ShaderNodeTree, native: dict[str, object], slot: str) -> tuple[bpy.types.Image, str, tuple[float, float], tuple[float, float]]:
    samples = tuple(n for n in tree.nodes if n.type == 'TEX_IMAGE' and n.get('source_slot') == slot)
    if len(samples) != 1 or samples[0].image is None or samples[0].image.packed_file is None:
        raise ValueError(f'Caster needs one actual packed source image: {caster.name}/{tree.name}/{slot}')
    sample = samples[0]
    if sample.image.alpha_mode != 'CHANNEL_PACKED' or min(sample.image.size) < 1 or sample.extension != 'REPEAT' or sample.interpolation != 'Linear':
        raise ValueError(f'Caster source texture has unsupported alpha interpretation/missing pixels: {slot}/{sample.image.name}')
    # Reopened packed images decode lazily; explicitly load their existing pixels.
    if len(sample.image.pixels) == 0 or not sample.image.has_data:
        raise ValueError(f'Packed caster source texture failed to decode pixel data: {slot}/{sample.image.name}')
    if len(sample.inputs['Vector'].links) != 1:
        raise ValueError(f'Caster source texture mapping is absent/ambiguous: {tree.name}/{slot}')
    mapping = sample.inputs['Vector'].links[0].from_node
    if mapping.type != 'MAPPING' or mapping.vector_type != 'POINT' or len(mapping.inputs['Vector'].links) != 1:
        raise ValueError(f'Caster source texture must retain explicit source POINT mapping: {tree.name}/{slot}')
    uv = mapping.inputs['Vector'].links[0].from_node
    if uv.type != 'UVMAP' or uv.uv_map != 'UV0' or uv.uv_map not in caster.data.uv_layers:
        raise ValueError(f'Caster canonical source texture requires actual source UV0: {caster.name}/{tree.name}/{slot}')
    tex = object_value(_properties(native, 'm_TexEnvs')[slot], 'source texture binding')
    if int_value(tex['m_UVSetIndex'], 'source texture UV index') != 0:
        raise ValueError(f'Unsupported canonical source caster texture UV set: {tree.name}/{slot}')
    st = tuple(tuple(float_value(object_value(tex[field], field)[axis], field+'.'+axis) for axis in ('x','y')) for field in ('m_Scale','m_Offset'))
    # RNA Mapping sockets store source JSON numbers as IEEE float32.
    stored_st = tuple(tuple(struct.unpack('f',struct.pack('f',value))[0] for value in pair) for pair in st)
    if tuple(mapping.inputs['Scale'].default_value) != (*stored_st[0],1.) or tuple(mapping.inputs['Location'].default_value) != (*stored_st[1],0.) or tuple(mapping.inputs['Rotation'].default_value) != (0.,0.,0.):
        raise ValueError(f'Caster mapping no longer matches source texture scale/offset: {tree.name}/{slot}')
    return sample.image, uv.uv_map, st[0], st[1]


def attach_shadow_proxy_alpha(caster: bpy.types.Object, evidence: ShadowAlphaEvidence, subdivision_level: int, dither_seed: int) -> dict[str, object]:
    """Append source alpha filtering after copied armature deformation, or keep opaque."""
    if caster.type != 'MESH' or 'hgrp_caster_source_identity' not in caster or MODIFIER in caster.modifiers:
        raise ValueError(f'Expected an unattached source-identity-tagged caster copy: {caster.name}')
    if any(mod.type != 'ARMATURE' for mod in caster.modifiers):
        raise ValueError(f'Caster alpha must follow only the copied Armature modifiers: {caster.name}')
    groups = tuple({n.node_tree for slot in caster.material_slots if slot.material and slot.material.use_nodes for n in slot.material.node_tree.nodes if n.type == 'GROUP' and n.node_tree and n.node_tree.get('source_shader')})
    if not groups:
        raise ValueError(f'Caster has no original source material groups: {caster.name}')
    resolved = dict(evidence.resolved_materials)
    rows: list[dict[str, object]] = []
    alpha_inputs: list[tuple[bpy.types.ShaderNodeTree, str, bpy.types.Image, str, tuple[float, float], tuple[float, float], float, float]] = []
    mask_inputs: dict[str, tuple[bpy.types.Image, str, tuple[float, float], tuple[float, float]]] = {}
    for tree in groups:
        key = _group_identity(tree)
        if key not in resolved or tree['source_shader'] != resolved[key] or resolved[key] not in OPAQUE_FAMILIES:
            raise ValueError(f'Caster source Shader identity is unresolved/unsupported: {caster.name}/{key}')
        native = _native_material(tree, key)
        keywords = tuple(string_value(x, 'material keyword') for x in array_value(native['m_ValidKeywords'], 'source keywords'))
        colors = _properties(native, 'm_Colors')
        floats = _properties(native, 'm_Floats')
        color_alpha = float_value(object_value(colors['_BaseColor'], 'BaseColor')['a'], 'BaseColor alpha')
        threshold = float_value(floats['_AlphaClipThreshold'], 'AlphaClipThreshold')
        surface = float_value(floats['_SurfaceType'], 'SurfaceType')
        modes = tuple(flag for flag in ('_ALPHATEST_ON', '_ALPHABLEND_ON') if flag in keywords)
        if len(modes) > 1 or '_DITHER_ON' in keywords or '_CHARACTER_FUR' in keywords:
            raise ValueError(f'Unsupported/ambiguous caster alpha variant: {key}/{keywords}')
        if modes:
            if key not in evidence.standard_materials:
                raise ValueError(f'Alpha caster Shader object/variant is outside exact Standard alpha evidence: {key}/{resolved[key]}')
            if (modes[0] == '_ALPHATEST_ON' and evidence.compatibility['status'] != 'verified_reference_object'
                    and float_value(floats['_EnableAlphaTest'], 'EnableAlphaTest') != 1.0):
                raise ValueError(f'Source alpha caster keyword requires its explicit enabled Shader toggle: {key}/_EnableAlphaTest={floats["_EnableAlphaTest"]}')
            image, uv_layer, scale, offset = _sampling(caster, tree, native, '_BaseMap')
            alpha_inputs.append((tree,modes[0],image,uv_layer,scale,offset,color_alpha,threshold))
            if '_UV2_COLOR' in keywords:
                proof = object_value(json.loads(tree.get('source_uv2_alpha_mask', '{}')), 'source UV2 alpha mask proof')
                if (modes[0] != '_ALPHATEST_ON' or float_value(floats['_UseUV2Color'], 'UseUV2Color') != 1.0
                        or proof.get('keyword') != '_UV2_COLOR' or proof.get('value') != 1.0
                        or proof.get('channel') != 'Red' or proof.get('uv_layer') != 'UV1'
                        or proof.get('native_shader_semantics_verified') is not False
                        or 'UV1' not in caster.data.uv_layers):
                    raise ValueError(f'Caster UV2 alpha mask has no matching source feature/UV1 contract: {key}')
                mask, _, mask_scale, mask_offset = _sampling(caster, tree, native, '_ExtraAlphaMask')
                if mask.colorspace_settings.name != 'Non-Color':
                    raise ValueError(f'Caster UV1 alpha mask requires linear source data: {mask.name}')
                mask_inputs[tree.name] = (mask, 'UV1', mask_scale, mask_offset)
        elif surface != 0.:
            raise ValueError(f'Transparent source caster has no verified alpha keyword: {key}/Surface={surface}')
        rows.append({'material_identity':key,'shader':resolved[key],'keywords':keywords,'mode':modes[0] if modes else 'opaque_preserved','base_map_image':image.name if modes else None,'packed_sha256':hashlib.sha256(image.packed_file.data).hexdigest() if modes else None,'uv_layer':uv_layer if modes else None,'scale':scale if modes else None,'offset':offset if modes else None,'base_color_alpha':color_alpha,'alpha_clip_threshold':threshold})
        if tree.name in mask_inputs:
            mask, mask_uv, mask_scale, mask_offset = mask_inputs[tree.name]
            rows[-1]['extra_alpha_mask'] = {'image': mask.name, 'packed_sha256': hashlib.sha256(mask.packed_file.data).hexdigest(),
                                          'uv_layer': mask_uv, 'scale': mask_scale, 'offset': mask_offset, 'channel': 'Red',
                                          'native_shader_semantics_verified': False}
    if alpha_inputs and (len(groups) != 1 or len(caster.material_slots) != 1):
        raise ValueError(f'Alpha proxy needs one verified source material; mixed material face routing is unimplemented: {caster.name}')
    if alpha_inputs:
        tree, mode, image, uv_layer, scale, offset, color_alpha, threshold = alpha_inputs[0]
        kernel = alpha_clip('EF Source Caster Alpha Clip',image,uv_layer,scale,offset,color_alpha,threshold,subdivision_level) if mode == '_ALPHATEST_ON' else alpha_dither('EF Source Caster Alpha Dither',image,uv_layer,scale,offset,color_alpha,dither_seed,subdivision_level)
        if tree.name in mask_inputs:
            multiply_clip_mask(kernel, *mask_inputs[tree.name])
        modifier = caster.modifiers.new(MODIFIER,'NODES')
        modifier.node_group = kernel
        caster['hgrp_alpha_scope'] = evidence.compatibility['provenance'] + ' Native SV_Position screen hash and mip-bias sampling are not reproduced.'
    return {'caster':caster.name,'source_identity':caster['hgrp_caster_source_identity'],'alpha_modified':bool(alpha_inputs),'modifier_order':[(mod.name,mod.type) for mod in caster.modifiers],'materials':rows,'compatibility':dict(evidence.compatibility),'evidence_sha256':{'shader_identity':evidence.shader_identity_sha256,'alpha_audit':evidence.alpha_audit_sha256},'scope':'Copied caster only; source materials/mesh unchanged. Hard clip or sqrt-alpha subdivided-face stochastic approximation; not native screen-space sampling.'}
