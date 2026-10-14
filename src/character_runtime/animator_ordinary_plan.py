"""Plan explicit authored ordinary layers from exact serialized controller sources.

The plan describes source-default-state scenarios, not the effective live
Animator state. Unsupported layers remain visible with concrete reasons.
"""
from __future__ import annotations

from dataclasses import asdict, dataclass
import hashlib
import json
from pathlib import Path
import sys

from animator_auxiliary_layers import number, object_row, read_json, rows
from animator_direct_parameters import read_direct_source
from character_animator_graph import _integer as integer, index_controller_graph


@dataclass(frozen=True)
class OrdinaryLayerAssessment:
    index: int
    binding_hash: int
    name: str | None
    status: str
    reasons: tuple[str, ...]
    route: dict[str, object] | None
    write_defaults_serialized: bool | None


def data(value: object, field: str) -> dict[str, object]:
    return object_row(object_row(value, field)['data'], field+'.data')


def assess_ordinary_layers(tree: dict[str, object], references: dict[str, object]) -> tuple[OrdinaryLayerAssessment, ...]:
    """Discover supported source shapes without character names or layer indices."""
    graph = index_controller_graph(tree, references)
    controller = object_row(tree['m_Controller'], 'controller')
    layer_rows = rows(controller['m_LayerArray'], 'layers')
    machine_rows = rows(controller['m_StateMachineArray'], 'machines')
    routes = tuple(object_row(json.loads(json.dumps(row)), 'route') for row in rows(graph['direct_routes'], 'routes'))
    result: list[OrdinaryLayerAssessment] = []
    for metadata_row in rows(graph['layers'], 'layer metadata'):
        metadata = object_row(metadata_row, 'layer metadata')
        index = integer(metadata['index'], 'layer.index')
        layer = data(layer_rows[index], 'layer')
        binding = object_row(metadata['name'], 'layer.name')
        binding_hash = integer(binding['hash'], 'layer.binding_hash')
        name = binding['name']
        if name is not None and not isinstance(name, str):
            raise ValueError('Layer binding name must be text or an unresolved source hash')
        machine = data(machine_rows[integer(layer['m_StateMachineIndex'], 'machine.index')], 'machine')
        state_rows = rows(machine['m_StateConstantArray'], 'states')
        reasons: list[str] = []
        if layer['(int&)m_LayerBlendingMode'] != 1:
            reasons.append('override_layer_unimplemented')
        if layer['m_MeshSpace'] is not False:
            reasons.append('mesh_space_unimplemented')
        if any(number(value, 'body_mask.word') != 0. for value in object_row(layer['m_BodyMask'], 'body_mask').values()):
            reasons.append('humanoid_mask_unimplemented')
        mask_rows = rows(data(layer['m_SkeletonMask'], 'skeleton_mask')['m_Data'], 'mask rows')
        mask = {integer(object_row(row, 'mask')['m_PathHash'], 'mask.hash'):
                number(object_row(row, 'mask')['m_Weight'], 'mask.weight') for row in mask_rows}
        if len(mask) != len(mask_rows) or any(value not in (0., 1.) for value in mask.values()):
            reasons.append('nonunique_or_fractional_skeleton_mask_unimplemented')
        if not any(mask.values()):
            reasons.append('no_enabled_ordinary_skeleton_channels')
        if number(layer['m_DefaultWeight'], 'layer.default_weight') < 0.:
            reasons.append('negative_layer_weight_unimplemented')
        for field in ('m_IKPass', 'm_SyncedLayerAffectsTiming', 'm_UseThreePoseBlender', 'm_ConvertSimpleBlend', 'm_PostProcessLayer', 'm_UseIdentifyValues'):
            if type(layer[field]) is not bool:
                raise ValueError(f'Expected serialized layer boolean: field={field}, layer={index}')
            if layer[field]:
                reasons.append(field+'_unimplemented')
        if len(state_rows) != 1 or metadata['state_transition_count'] != 0 or metadata['any_state_transition_count'] != 0:
            reasons.append('state_transitions_unimplemented')
        default_state = integer(machine['m_DefaultState'], 'machine.default_state')
        route: dict[str, object] | None = None
        write_defaults: bool | None = None
        if not 0 <= default_state < len(state_rows):
            reasons.append('default_state_unresolved')
        else:
            state = data(state_rows[default_state], 'state')
            write_defaults = state['m_WriteDefaultValues']
            if type(write_defaults) is not bool:
                raise ValueError(f'Expected state WriteDefaults boolean: layer={index}')
            if state['m_Mirror'] is not False or number(state['m_CycleOffset'], 'state.cycle_offset') != 0.:
                reasons.append('state_mirror_or_cycle_offset_unimplemented')
            if rows(state['m_StateParameterConstantArray'], 'state.parameter_constants'):
                reasons.append('state_parameter_overrides_unimplemented')
            trees = rows(state['m_BlendTreeConstantArray'], 'trees')
            if len(trees) != 1 or rows(state['m_BlendTreeConstantIndexArray'], 'tree index array') != [0]:
                reasons.append('multiple_state_blend_trees_unimplemented')
            else:
                nodes = rows(data(trees[0], 'tree')['m_NodeArray'], 'nodes')
                node = data(nodes[0], 'root node')
                if node['m_BlendType'] != 4 or node['m_ClipID'] != 0xffffffff:
                    reasons.append('root_is_not_direct')
                else:
                    direct_fields = data(node['m_BlendDirectData'], 'direct')
                    if type(direct_fields['m_UsePoseTimeValues']) is not bool:
                        raise ValueError(f'Expected Direct pose-time boolean: layer={index}')
                    if not direct_fields['m_UsePoseTimeValues']:
                        reasons.append('independent_child_clocks_unimplemented')
                    else:
                        read_direct_source(node)
                    children = tuple(integer(value, 'child.index') for value in rows(node['m_ChildIndices'], 'children'))
                    if any(not 0 <= child < len(nodes) for child in children):
                        raise ValueError(f'Direct child node index is out of range: layer={index}')
                    for child_index in children:
                        child = data(nodes[child_index], 'child')
                        if rows(child['m_ChildIndices'], 'child.children') or child['m_ClipID'] == 0xffffffff:
                            reasons.append('nested_or_nonclip_child_unimplemented')
                        if child['m_Mirror'] is not False or number(child['m_CycleOffset'], 'child.cycle_offset') != 0.:
                            reasons.append('child_mirror_or_cycle_offset_unimplemented')
                    matches = tuple(value for value in routes if value['layer'] == index and value['state'] == default_state and value['tree'] == 0 and value['node'] == 0)
                    if len(matches) != 1:
                        raise ValueError(f'Exact source root has no unique graph route: layer={index}')
                    route = matches[0]
                    if any(pointer is None or object_row(pointer, 'clip.pointer')['path_id'] == 0 for pointer in rows(route['child_clips'], 'clips')):
                        reasons.append('null_clip_pointer')
        result.append(OrdinaryLayerAssessment(index, binding_hash, name,
            'source_authored_ordinary_candidate' if not reasons else 'unsupported', tuple(dict.fromkeys(reasons)),
            route if not reasons else None, write_defaults))
    if len({entry.binding_hash for entry in result}) != len(result):
        raise ValueError('Controller layer binding hashes must be unique for explicit selection')
    return tuple(result)


def all_source_ordinary_bindings(assessments: tuple[OrdinaryLayerAssessment, ...]) -> tuple[int, ...]:
    """Include every supported source-default-state shape in serialized order.

    Candidate classification is structural; exact Avatar, reference, child clip
    settings and explicit scenario validation still run before pose publication.
    """
    if tuple(row.index for row in assessments) != tuple(sorted({row.index for row in assessments})):
        raise ValueError('Ordinary source assessments must have unique serialized layer order')
    selected = tuple(row.binding_hash for row in assessments
                     if row.status == 'source_authored_ordinary_candidate')
    if not selected:
        raise ValueError('Selected controller has no supported source-authored ordinary layers')
    select_ordinary_routes(assessments, selected)
    return selected


def select_ordinary_routes(assessments: tuple[OrdinaryLayerAssessment, ...], binding_hashes: tuple[int, ...]) -> list[object]:
    """Select exact source bindings while retaining serialized layer order."""
    if not binding_hashes or len(set(binding_hashes)) != len(binding_hashes):
        raise ValueError('Explicit ordinary layer binding hashes must be nonempty and unique')
    by_hash = {row.binding_hash: row for row in assessments}
    for binding_hash in binding_hashes:
        if binding_hash not in by_hash:
            raise ValueError(f'Ordinary layer binding is absent from selected controller: hash={binding_hash}')
        row = by_hash[binding_hash]
        if row.status != 'source_authored_ordinary_candidate' or row.route is None:
            raise ValueError(f'Ordinary source layer is unsupported: hash={binding_hash}, layer={row.index}, reasons={row.reasons}')
    selected = frozenset(binding_hashes)
    return [row.route for row in assessments if row.binding_hash in selected]


def validate_sampled_clip_pointers(routes: list[object], samples_root: Path) -> None:
    """Join each sampled object to the actual controller CAB/PathID pointer."""
    identities: dict[int, tuple[str, int]] = {}
    for raw_route in routes:
        for pointer_row in rows(object_row(raw_route, 'route')['child_clips'], 'child clips'):
            pointer = object_row(pointer_row, 'clip pointer')
            index = integer(pointer['index'], 'clip.index')
            cab = pointer['cab']
            if not isinstance(cab, str) or not cab:
                raise ValueError('Exact clip pointer requires a nonempty CAB')
            identity = (cab.casefold(), integer(pointer['path_id'], 'clip.path_id'))
            if index in identities and identities[index] != identity:
                raise ValueError(f'Controller clip index has conflicting identities: index={index}')
            identities[index] = identity
            sampled = object_row(read_json(samples_root/str(index)/'clip_source.json'), 'clip.source')
            sampled_cab = sampled['cab']
            if not isinstance(sampled_cab, str) or (sampled_cab.casefold(), integer(sampled['pathId'], 'sample.path_id')) != identity:
                raise ValueError(f'Sampled clip does not match selected controller pointer: index={index}, cab={cab}, path_id={identity[1]}')


def export_ordinary_plan(tree_path: Path, references_path: Path, output: Path) -> None:
    tree = object_row(read_json(tree_path), 'controller tree')
    references = object_row(read_json(references_path), 'controller references')
    assessments = assess_ordinary_layers(tree, references)
    record = {'format': 'EndfieldOrdinarySourceLayerPlan/1', 'status': 'source_default_state_candidates_explicit_runtime_boundary',
              'controller': tree['m_Name'], 'source': references['source'], 'controller_path_id': references['controller_path_id'],
              'layers': [asdict(row) for row in assessments],
              'input_sha256': {str(path): hashlib.sha256(path.read_bytes()).hexdigest() for path in (tree_path, references_path)},
              'pending': ['Candidates still require exact selected Avatar, clip objects/references/settings and explicit source parameter frames',
                          'WriteDefaults missing-channel writes, live layer enable/weights/clock, transitions and native scheduler are not evaluated']}
    with output.open('x', encoding='utf-8') as stream:
        json.dump(record, stream, indent=2)


if __name__ == '__main__':
    if len(sys.argv) != 4:
        raise ValueError('Expected controller_type_tree, controller_source_references and new plan output paths')
    export_ordinary_plan(Path(sys.argv[1]), Path(sys.argv[2]), Path(sys.argv[3]))
