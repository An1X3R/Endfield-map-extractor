"""Parse an authenticated Grounder snapshot and explicit offline query scenario."""
from __future__ import annotations

from dataclasses import asdict, dataclass

from character_animation_finalik_export import (
    ReplayLimb, boolean, field, inventory_path_id,
)
from character_animation_goal_export import (
    array_value, encode_local, integer_value, number_value, object_value,
    quaternion_xyzw, text_value, vector3,
)
from character_animation_goals import GoalRig, LocalTransform
from character_finalik_bone_rotation import quaternion
from character_finalik_grounder_consumer import (
    GrounderLegObservation, GrounderPoseRequest, GrounderPoseState, solve_grounder_pose,
)
from character_finalik_grounder_pose import GrounderPelvisState
from character_finalik_grounder_post import GrounderFootLimits
from character_finalik_trigonometric_inputs import vector
from character_finalik_weights import FinalIKWeightResult, FinalIKWeightState
from character_runtime_ik_weights import _input_float32


@dataclass(frozen=True)
class GrounderReplaySource:
    pelvis_index: int
    limits: GrounderFootLimits
    initial_pelvis_state: GrounderPelvisState
    ik_foot_reference_indices: tuple[int | None, int | None]
    evidence: dict[str, object]


def float_field(row: dict[str, object], name: str) -> float:
    return _input_float32(number_value(field(row, name), name))


def relevant_field(component: dict[str, object], path: str) -> dict[str, object]:
    values = [field(object_value(item, 'component field'), 'value')
              for item in array_value(field(component, 'type_tree_relevant_fields'), 'component fields')
              if isinstance(item, dict) and item.get('path') == path]
    if len(values) != 1:
        raise ValueError('Grounder snapshot requires one exact serialized field: ' + path)
    return object_value(values[0], path)


def selected_component(inventory: dict[str, object], cab: str, path_id: int,
                       class_name: str) -> dict[str, object]:
    candidates = [object_value(item, 'component')
                  for item in array_value(field(inventory, 'components'), 'components')
                  if isinstance(item, dict) and item.get('component_source_cab') == cab
                  and inventory_path_id(item.get('component_path_id')) == path_id]
    if len(candidates) != 1:
        raise ValueError('Grounder component selector is missing or ambiguous: ' + class_name)
    component = candidates[0]
    if (field(component, 'mono_script_class') != class_name
            or field(component, 'mono_script_namespace') != 'RootMotion.FinalIK'
            or field(component, 'type_tree_status') != 'parsed'):
        raise ValueError('Grounder snapshot has an unsupported component class or unparsed source: ' + class_name)
    return component


def local_pointer(value: object, context: str) -> int:
    pointer = object_value(value, context)
    if integer_value(field(pointer, 'm_FileID'), context + '.m_FileID') != 0:
        raise ValueError('Grounder external PPtr requires resolved CAB evidence: ' + context)
    path_id = integer_value(field(pointer, 'm_PathID'), context + '.m_PathID')
    if path_id == 0:
        raise ValueError('Grounder source PPtr must be nonzero: ' + context)
    return path_id


def reference_index(audit: dict[str, object], hierarchy: list[dict[str, object]],
                    cab: str, pointer: object, context: str) -> int:
    path_id = local_pointer(pointer, context)
    transforms = [object_value(item, 'source Transform')
                  for item in array_value(field(audit, 'transforms'), 'source transforms')
                  if isinstance(item, dict) and isinstance(item.get('object'), dict)
                  and item['object'].get('source') == cab and item['object'].get('path_id') == path_id]
    if len(transforms) != 1:
        raise ValueError('Grounder source Transform is missing or ambiguous: ' + context)
    matches = [index for index, node in enumerate(hierarchy) if field(node, 'path') == field(transforms[0], 'path')]
    if len(matches) != 1:
        raise ValueError('Grounder reference has no unique selected-Avatar hierarchy node: ' + context)
    return matches[0]


def parse_grounder_source(scenario: dict[str, object], inventory: dict[str, object],
                          audit: dict[str, object], rig: GoalRig, hierarchy: list[dict[str, object]],
                          limbs: tuple[ReplayLimb, ...], root: str) -> GrounderReplaySource:
    if len(limbs) != 2:
        raise ValueError('Grounder replay requires exactly the source left and right leg solvers')
    row = object_value(field(scenario, 'grounder'), 'Grounder selection')
    selector = object_value(field(row, 'component'), 'Grounder component selector')
    cab = text_value(field(selector, 'source_cab'), 'Grounder source CAB')
    path_id = integer_value(field(selector, 'path_id'), 'Grounder component PathID')
    if path_id == 0:
        raise ValueError('Grounder component PathID must be nonzero')
    grounder = selected_component(inventory, cab, path_id, 'GrounderBipedIK')
    game_object_path = text_value(field(grounder, 'game_object_path'), 'Grounder object path')
    if game_object_path != root and not game_object_path.startswith(root + '/'):
        raise ValueError('Grounder component belongs to another source root')
    biped_id = local_pointer(relevant_field(grounder, '$.ik'), 'Grounder.ik')
    biped = selected_component(inventory, cab, biped_id, 'BipedIK')
    if field(biped, 'game_object_path') != root:
        raise ValueError('Grounder.ik does not select the exact root BipedIK')
    references = relevant_field(biped, '$.references')
    indices = {name: reference_index(audit, hierarchy, cab, field(references, name), 'BipedReferences.' + name)
               for name in ('pelvis', 'leftThigh', 'leftCalf', 'leftFoot', 'rightThigh', 'rightCalf', 'rightFoot')}
    for replay_limb, side in zip(limbs, ('left', 'right'), strict=True):
        limb_selector = object_value(field(replay_limb.source_evidence, 'component'), 'limb component')
        if (field(limb_selector, 'source_cab') != cab or field(limb_selector, 'path_id') != biped_id
                or field(limb_selector, 'solver_field') != side + 'Foot'):
            raise ValueError('Grounder selected limb order/component differs from source BipedIK: ' + side)
        limb = replay_limb.calibration.limb
        if (limb.upper, limb.middle, limb.end) != tuple(indices[side + name] for name in ('Thigh', 'Calf', 'Foot')):
            raise ValueError('Grounder references differ from exact selected Avatar leg: ' + side)
        ancestor = rig.parents[limb.upper]
        while ancestor >= 0 and ancestor != indices['pelvis']:
            ancestor = rig.parents[ancestor]
        if ancestor != indices['pelvis']:
            raise ValueError('Grounder source pelvis must be an ancestor of both selected leg chains: ' + side)
    solver = relevant_field(grounder, '$.solver')
    ik_indices: list[int | None] = []
    for name in ('IKFootBoneL', 'IKFootBoneR'):
        pointer = object_value(field(solver, name), name)
        if integer_value(field(pointer, 'm_FileID'), name + '.m_FileID') != 0:
            raise ValueError('Grounder IKFootBone external PPtr requires resolved CAB evidence: ' + name)
        ik_indices.append(None if integer_value(field(pointer, 'm_PathID'), name + '.m_PathID') == 0
                          else reference_index(audit, hierarchy, cab, pointer, name))
    limits = GrounderFootLimits(*(float_field(solver, name) for name in (
        'maxFootRotationAngleFore', 'maxFootRotationAngleBack', 'maxFootRotationAngleIn', 'maxFootRotationAngleOut')))
    if any(value < 0. for value in asdict(limits).values()):
        raise ValueError('Grounder serialized foot limits must be nonnegative')
    history = object_value(field(row, 'initial_pelvis_history'), 'pelvis initial history')
    state = GrounderPelvisState(vector(vector3(field(history, 'animated_local_position'), 'animated pelvis'), 'animated pelvis'),
        vector(vector3(field(history, 'solved_local_position'), 'solved pelvis'), 'solved pelvis'),
        float_field(history, 'last_weight'), float_field(history, 'last_adsorb_weight'))
    return GrounderReplaySource(indices['pelvis'], limits, state, (ik_indices[0], ik_indices[1]),
        {'component': selector, 'ik': {'source_cab': cab, 'path_id': biped_id},
         'references': references, 'hierarchy_indices': indices, 'serialized_foot_limits': asdict(limits),
         'ik_foot_pointers': {name: field(solver, name) for name in ('IKFootBoneL', 'IKFootBoneR')},
         'ik_foot_hierarchy_indices': ik_indices,
         'source_semantics': 'authenticated_component_snapshot_not_runtime_state'})


def parse_resolved_weights(value: object) -> FinalIKWeightResult:
    row = object_value(value, 'resolved FinalIK weights')
    state = object_value(field(row, 'state'), 'weight state')
    return FinalIKWeightResult(FinalIKWeightState(float_field(state, 'pelvis_weight'),
        float_field(state, 'keeping_foot_weight'), float_field(state, 'keeping_foot_target')),
        float_field(row, 'foot_adsorb_weight'))


def parse_leg_observation(value: object) -> GrounderLegObservation:
    row = object_value(value, 'Grounder frame leg')
    ik_position = field(row, 'ik_foot_position')
    return GrounderLegObservation(vector(vector3(field(row, 'grounded_position'), 'grounded position'), 'grounded position'),
        None if ik_position is None else vector(vector3(ik_position, 'IKFootBone world position'), 'IKFootBone world position'),
        boolean(field(row, 'last_frame_invalid'), 'last frame invalid'),
        quaternion(quaternion_xyzw(field(row, 'rotation_offset'), 'ground rotation offset'), 'ground rotation offset'),
        float_field(row, 'rotation_weight'), float_field(row, 'maintain_rotation_weight'),
        float_field(row, 'bend_modifier_weight'),
        boolean(field(row, 'maintain_bend_requested'), 'maintain bend request'),
        boolean(field(row, 'maintain_rotation_requested'), 'maintain rotation request'))


def evaluate_grounder_frames(rig: GoalRig, limbs: tuple[ReplayLimb, ...], source: GrounderReplaySource,
                             frames: tuple[tuple[float, tuple[LocalTransform, ...]], ...],
                             scenario_frames: object) -> tuple[list[dict[str, object]], list[dict[str, object]]]:
    rows = array_value(scenario_frames, 'Grounder scenario frames')
    if len(rows) != len(frames):
        raise ValueError('Grounder scenario count must match fresh decoded frames')
    names = tuple(limb.name for limb in limbs)
    state = GrounderPoseState(source.initial_pelvis_state, (limbs[0].initial_state, limbs[1].initial_state))
    result: list[dict[str, object]] = []
    diagnostics: list[dict[str, object]] = []
    for index, ((time, locals_), raw) in enumerate(zip(frames, rows, strict=True)):
        row = object_value(raw, 'Grounder frame')
        if number_value(field(row, 'time'), 'Grounder frame time') != time:
            raise ValueError('Grounder scenario time differs from fresh decoded clock: frame=' + str(index))
        leg_rows = [object_value(item, 'Grounder leg') for item in array_value(field(row, 'legs'), 'Grounder frame legs')]
        if tuple(text_value(field(item, 'name'), 'leg name') for item in leg_rows) != names:
            raise ValueError('Grounder frame legs differ from exact source order: frame=' + str(index))
        observations = (parse_leg_observation(leg_rows[0]), parse_leg_observation(leg_rows[1]))
        present = tuple(item.ik_foot_position is not None for item in observations)
        if present[0] != present[1] or (any(present) and any(index is None for index in source.ik_foot_reference_indices)):
            raise ValueError('Grounder IKFootBone observations require both exact source references and paired runtime availability')
        request = GrounderPoseRequest(parse_resolved_weights(field(row, 'resolved_weights')),
            vector(vector3(field(row, 'pelvis_ik_offset'), 'pelvis offset'), 'pelvis offset'),
            float_field(row, 'floor_feet_theta_by_foot'), float_field(row, 'floor_feet_theta_by_logic'), source.limits,
            observations,
            boolean(field(row, 'enabled'), 'Grounder enabled'), boolean(field(row, 'initiated'), 'Grounder initiated'))
        try:
            solved = solve_grounder_pose(rig, locals_, source.pelvis_index,
                (limbs[0].calibration, limbs[1].calibration), (limbs[0].baseline, limbs[1].baseline), request, state)
        except (ValueError, ArithmeticError) as error:
            raise ValueError('Grounder replay failed: frame=' + str(index) + ', time=' + repr(time) + ', cause=' + str(error)) from error
        if any(a.scale != b.scale or (i != source.pelvis_index and a.position != b.position)
               for i, (a, b) in enumerate(zip(locals_, solved.locals, strict=True))):
            raise RuntimeError('Grounder writes changed a non-pelvis translation or source scale')
        state = solved.state
        result.append({'time': time, 'locals': [encode_local(local) for local in solved.locals]})
        diagnostics.append({'frame_index': index, 'time': time, 'state': asdict(state),
            'pelvis_pre': asdict(solved.pelvis_pre.preparation),
            'pelvis_world_position_write': solved.pelvis_pre.written_world_position,
            'limbs': [{'name': limb.name, 'modified_bend_normal': value.modified_bend_normal,
                'position_stage_active': value.position_stage_active, 'end_world_writes': value.end_world_writes}
                for limb, value in zip(limbs, solved.limbs, strict=False)],
            'foot_post': [asdict(value) for value in solved.foot_post]})
    return result, diagnostics
