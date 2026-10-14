"""Bind source-selected shadow proxies and live Sun fields to HGRP consumers."""
from __future__ import annotations

from dataclasses import dataclass
import json
from pathlib import Path

import bpy

from character_live_shadow_nodes import (
    CHARACTER_SHADOW_ATTRIBUTE, MAIN_SHADOW_ATTRIBUTE, build_sun_shadow_nodes,
)
from character_node_graph import node
from patch_character_hgrp import by_label


SHADOW_MODIFIER = 'EF Live HGRP Shadow Fields'


@dataclass(frozen=True, slots=True)
class CasterSelection:
    accepted_identities: tuple[str, ...]
    alpha_aware_identities: tuple[str, ...]


def select_source_casters(inventory: Path, objects: tuple[bpy.types.Object, ...]) -> CasterSelection:
    """Join an audited source hierarchy by renderer identity, never by name."""
    document = json.loads(inventory.read_text(encoding='utf-8'))
    if document['format'] != 'EndfieldCharacterCasterInventory/1':
        raise ValueError(f'Unsupported source caster inventory: {inventory}')
    identities = {o['source_renderer_identity']: o for o in objects}
    candidates = []
    for character in document['characters']:
        rows = character['active_shadow_only_candidates']
        keys = tuple(row['renderer_identity']['source'] + ':' + row['renderer_identity']['path_id'] for row in rows)
        if set(keys) & identities.keys():
            if not set(keys) <= identities.keys():
                raise ValueError(f'Incomplete source proxy set: missing={sorted(set(keys) - identities.keys())}')
            candidates.extend(zip(keys, rows))
    if not candidates:
        raise ValueError('No source-identity-matched shadow proxy candidates')
    accepted = []
    alpha_aware = []
    for identity, row in candidates:
        source = json.loads(identities[identity]['source_renderer_pipeline_json'])
        if source['m_CastShadows'] != 3 or source['m_Enabled'] is not True or not row['active_in_hierarchy_from_source_transform_chain']:
            raise ValueError(f'Caster source state no longer matches its audit: {identity}')
        if source['m_PlatformSpecificCastShadows']['defaultParam'] != 3:
            raise ValueError(f'Caster platform default differs from audited ShadowsOnly: {identity}')
        if any(not m['resolved'] or m['shadow_caster_pass_listed_disabled'] for m in row['source_materials']):
            raise ValueError(f'Caster material is unresolved or ShadowCaster disabled: {identity}')
        if row['classification'] == 'shadow_only_opaque_geometry_candidate':
            accepted.append(identity)
        elif row['classification'] in ('shadow_only_proxy_requires_alpha_aware_occlusion', 'shadow_only_proxy_requires_alpha_test_mask'):
            accepted.append(identity)
            alpha_aware.append(identity)
        else:
            raise ValueError(f'Unsupported caster classification: {identity}/{row["classification"]}')
    return CasterSelection(tuple(accepted), tuple(alpha_aware))


def source_groups(obj: bpy.types.Object) -> tuple[bpy.types.ShaderNodeTree, ...]:
    """Read source material groups directly attached to one renderer."""
    return tuple({n.node_tree for slot in obj.material_slots if slot.material and slot.material.use_nodes
                  for n in slot.material.node_tree.nodes if n.type == 'GROUP' and n.node_tree and n.node_tree.get('source_shader')})


def make_proxy_collection(objects: tuple[bpy.types.Object, ...], selection: CasterSelection) -> bpy.types.Collection:
    """Copy native proxy objects while retaining shared mesh keys and armatures."""
    collection = bpy.data.collections.new('EF Source Shadow Casters')
    bpy.context.scene.collection.children.link(collection)
    identities = {o['source_renderer_identity']: o for o in objects}
    for identity in selection.accepted_identities:
        source = identities[identity]
        if any(m.type != 'ARMATURE' for m in source.modifiers):
            raise ValueError(f'Unsupported source proxy modifier: {identity}')
        clone = source.copy()
        clone.name = 'EF Caster ' + source.name
        collection.objects.link(clone)
        for key in tuple(clone.keys()):
            del clone[key]
        clone['hgrp_caster_source_identity'] = identity
        clone.hide_viewport = False
        clone.hide_render = True
        clone.hide_set(True)
        constraint = clone.constraints.new('COPY_TRANSFORMS')
        constraint.name = 'Live source proxy transform'
        constraint.target = source
        constraint.target_space = 'WORLD'
        constraint.owner_space = 'WORLD'
    return collection


def bind_shadow_attributes(tree: bpy.types.ShaderNodeTree) -> int:
    """Replace raw R/G producer edges without altering family consumer formulas."""
    shared = by_label(tree, 'Explicit HGRP state and preview inputs')
    total = 0
    for output_name, attribute_name in (('Cast Shadow', MAIN_SHADOW_ATTRIBUTE), ('Preview Atlas Visibility', CHARACTER_SHADOW_ATTRIBUTE)):
        targets = tuple(link.to_socket for link in shared.outputs[output_name].links)
        if not targets:
            raise ValueError(f'No verified raw shadow consumers: {tree.name}/{output_name}')
        field = node(tree, 'ShaderNodeAttribute', 'Live ' + attribute_name, 'Live Geometry Shadows')
        field.attribute_name = attribute_name
        field.attribute_type = 'GEOMETRY'
        for target in targets:
            tree.links.new(field.outputs['Fac'], target)
        total += len(targets)
    tree['shadow_producer'] = 'Live Blender Geometry Nodes Sun raycast; source-selected proxies; single ray per vertex'
    if tree.get('source_shader') == 'HGRP/CharacterNPR_Skin':
        by_label(tree, 'Compiled Skin screen-shadow consumption')['input_provenance'] = tree['shadow_producer']
    return total


def attach_live_shadows(
    objects: tuple[bpy.types.Object, ...], sun: bpy.types.Object,
    main_collection: bpy.types.Collection, character_collection: bpy.types.Collection,
    bias_meters: float, ray_length_meters: float, subdivision_level: int,
) -> tuple[str, ...]:
    """Attach fields only to visible renderers whose verified graphs consume R/G."""
    visible = tuple(o for o in objects if not o.hide_render)
    groups = {t for o in visible for t in source_groups(o)
              if any(n.label == 'Explicit HGRP state and preview inputs' for n in t.nodes)}
    if not groups:
        raise ValueError('No visible HGRP shadow receiver groups')
    for tree in groups:
        bind_shadow_attributes(tree)
    kernel = build_sun_shadow_nodes('EF Live Sun Shadow Fields', sun, main_collection, character_collection,
                                    bias_meters, ray_length_meters, subdivision_level)
    receivers = tuple(o for o in visible if groups.intersection(source_groups(o)))
    for obj in receivers:
        if obj.matrix_world.determinant() == 0. or SHADOW_MODIFIER in obj.modifiers:
            raise ValueError(f'Invalid or already-attached receiver: {obj.name}')
        modifier = obj.modifiers.new(SHADOW_MODIFIER, 'NODES')
        modifier.node_group = kernel
    return tuple(o.name for o in receivers)
