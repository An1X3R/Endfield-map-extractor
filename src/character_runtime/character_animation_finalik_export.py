"""Replay explicit Animation0 FinalIK inputs after the selected source pose.

Component fields are authenticated snapshot evidence, not captured runtime
state. Frame targets, weights, invocation order and histories are explicit.
The replay never infers terrain, live scheduling or solver initiation.
"""
from __future__ import annotations

from dataclasses import asdict, dataclass
from pathlib import Path

from character_animation_goal_export import (
    array_value, compile_avatar_rig, encode_local, field_value, file_hash,
    integer_value, number_value, object_value, quaternion_xyzw, read_json,
    text_value, transform_from_json, vector3, write_frame_json, write_json,
)
from character_animation_goals import GoalRig, LimbSpec, LocalTransform, Vec3
from character_finalik_animation_limb import (
    FinalIKAnimationLimbRequest, FinalIKAnimationLimbState, solve_animation_limb,
)
from character_finalik_bend_modifier import FinalIKAnimationBendState
from character_finalik_bone_rotation import quaternion
from character_finalik_end_rotation import FinalIKEndRotationState
from character_finalik_pose import FinalIKDirectCalibration, calibrate_direct_limb
from character_finalik_trigonometric_inputs import vector
from character_runtime_ik_weights import _input_float32


@dataclass(frozen=True)
class FinalIKSourceIdentity:
    character_id: str
    avatar_cab: str
    avatar_path_id: int
    clip_source: str
    clip_path_id: int
    animator_root_path: str
    resource_path: str


@dataclass(frozen=True)
class FinalIKExportResult:
    source_local_frames: Path
    receipt_path: Path
    input_paths: tuple[Path, ...]
    output_paths: tuple[Path, ...]


@dataclass(frozen=True)
class ReplayLimb:
    name: str
    calibration: FinalIKDirectCalibration
    baseline: Vec3
    initial_state: FinalIKAnimationLimbState
    source_evidence: dict[str, object]


def boolean(value: object, context: str) -> bool:
    if type(value) is not bool:
        raise TypeError('Expected explicit boolean: ' + context)
    return value


def field(record: dict[str, object], name: str) -> object:
    return field_value(record, name, 'FinalIK replay')


def resolve_input(parent: Path, value: object, context: str) -> Path:
    path = (parent / text_value(value, context)).resolve(strict=True)
    if not path.is_file():
        raise ValueError('Expected an existing file: ' + str(path))
    return path


def validate_hashes(expected: dict[str, object], paths: dict[str, Path]) -> None:
    for name, path in paths.items():
        digest = text_value(field(expected, name), 'source_sha256.' + name)
        if len(digest) != 64 or any(c not in '0123456789abcdefABCDEF' for c in digest):
            raise ValueError('Expected SHA256 hex digest: ' + name)
        if file_hash(path) != digest.lower():
            raise ValueError('FinalIK source hash mismatch: field=' + name + ', path=' + str(path))


def validate_frames(value: object, hierarchy_count: int, domain: dict[str, object]) -> tuple[tuple[float, tuple[LocalTransform, ...]], ...]:
    rows = array_value(value, 'source frames')
    if not rows or len(rows) != integer_value(field(domain, 'common_frame_count'), 'common_frame_count'):
        raise ValueError('FinalIK source frame count differs from verified frame domain')
    frames: list[tuple[float, tuple[LocalTransform, ...]]] = []
    for index, raw in enumerate(rows):
        row = object_value(raw, 'source frame ' + str(index))
        time = number_value(field(row, 'time'), 'source frame time')
        locals_raw = array_value(field(row, 'locals'), 'source frame locals')
        if len(locals_raw) != hierarchy_count or (frames and time <= frames[-1][0]):
            raise ValueError('Invalid FinalIK hierarchy count or frame clock at index ' + str(index))
        locals_ = tuple(transform_from_json(local, 'source local') for local in locals_raw)
        frames.append((time, locals_))
    for key, actual in (('exported_start_time', frames[0][0]), ('exported_end_time', frames[-1][0])):
        if number_value(field(domain, key), key) != actual:
            raise ValueError('FinalIK frame clock disagrees with verified domain: ' + key)
    return tuple(frames)


def inventory_path_id(value: object) -> int:
    """The component inventory serializes Int64 identities as decimal text."""
    if type(value) is int:
        return value
    if isinstance(value, str) and value and value.removeprefix('-') and all(c in '0123456789' for c in value.removeprefix('-')):
        return int(value)
    raise TypeError('Expected exact decimal source component PathID')


def validate_source_avatar(audit: dict[str, object], identity: FinalIKSourceIdentity) -> None:
    animators = [object_value(item, 'source Animator') for item in array_value(field(audit, 'animators'), 'source Animators')
                 if isinstance(item, dict) and isinstance(item.get('object'), dict)
                 and item['object'].get('name') == identity.animator_root_path]
    if len(animators) != 1:
        raise ValueError('FinalIK source audit has no unique selected root Animator')
    avatar = object_value(field(object_value(field(animators[0], 'avatar'), 'Animator Avatar'), 'resolved'), 'resolved Avatar')
    if field(avatar, 'source') != identity.avatar_cab or integer_value(field(avatar, 'path_id'), 'Avatar PathID') != identity.avatar_path_id:
        raise ValueError('FinalIK source Animator Avatar differs from the freshly decoded selected Avatar')


def source_solver(inventory: dict[str, object], audit: dict[str, object], selector: dict[str, object], limb: LimbSpec,
                  hierarchy: list[dict[str, object]], root: str) -> dict[str, object]:
    if field(inventory, 'format') != 'EndfieldCharacterPrefabComponentInventory/2':
        raise ValueError('Unsupported FinalIK source component inventory format')
    requested = object_value(field(audit, 'requested_root'), 'source audit root')
    actual = object_value(field(audit, 'actual_root'), 'source audit actual root')
    for key, expected in (('root_cab', field(requested, 'source')),
                          ('root_resource_path', field(requested, 'resource_path')), ('root_name', field(actual, 'name'))):
        if field(inventory, key) != expected:
            raise ValueError('FinalIK component snapshot belongs to another selected source: ' + key)
    cab = text_value(field(selector, 'source_cab'), 'component source CAB')
    path_id = integer_value(field(selector, 'path_id'), 'component PathID')
    solver_field = text_value(field(selector, 'solver_field'), 'solver field')
    if path_id == 0:
        raise ValueError('FinalIK component PathID must be nonzero')
    candidates = [object_value(item, 'component') for item in array_value(field(inventory, 'components'), 'components')
                  if isinstance(item, dict) and item.get('component_source_cab') == cab and inventory_path_id(item.get('component_path_id')) == path_id]
    if len(candidates) != 1:
        raise ValueError('FinalIK component selector is missing or ambiguous')
    component = candidates[0]
    if (field(component, 'mono_script_class') != 'BipedIK' or field(component, 'mono_script_namespace') != 'RootMotion.FinalIK'
            or field(component, 'type_tree_status') != 'parsed' or field(component, 'game_object_path') != root):
        raise ValueError('FinalIK source component is not the selected root BipedIK with parsed fields')
    solvers = [object_value(field(object_value(item, 'relevant field'), 'value'), 'solvers')
               for item in array_value(field(component, 'type_tree_relevant_fields'), 'relevant fields')
               if isinstance(item, dict) and item.get('path') == '$.solvers']
    if len(solvers) != 1:
        raise ValueError('Missing or ambiguous serialized BipedIK solvers')
    solver = object_value(field(solvers[0], solver_field), 'selected source solver')
    if integer_value(field(solver, 'bendModifier'), 'source bendModifier') != 0:
        raise ValueError('FinalIK replay supports only source Animation0 bendModifier')
    transforms = [object_value(item, 'source Transform') for item in array_value(field(audit, 'transforms'), 'source transforms')]
    for bone, index in zip(('bone1', 'bone2', 'bone3'), (limb.upper, limb.middle, limb.end), strict=True):
        pointer = object_value(field(object_value(field(solver, bone), bone), 'transform'), bone + '.transform')
        if integer_value(field(pointer, 'm_FileID'), bone + '.fileID') != 0:
            raise ValueError('External FinalIK bone references require resolved CAB evidence: ' + bone)
        transform_id = integer_value(field(pointer, 'm_PathID'), bone + '.PathID')
        matches = [item for item in transforms if isinstance(item.get('object'), dict)
                   and item['object'].get('source') == cab and item['object'].get('path_id') == transform_id]
        if len(matches) != 1 or field(matches[0], 'path') != field(hierarchy[index], 'path'):
            raise ValueError('FinalIK serialized bone differs from exact selected Avatar limb: ' + bone + ', limb=' + limb.name)
    return solver


def parse_history(value: object) -> FinalIKAnimationLimbState:
    history = object_value(value, 'initial history')
    bend = object_value(field(history, 'bend'), 'bend history')
    end = object_value(field(history, 'end'), 'end history')
    return FinalIKAnimationLimbState(
        FinalIKAnimationBendState(boolean(field(bend, 'maintain_for_one_frame'), 'bend flag'),
                                  vector(vector3(field(bend, 'animation_normal'), 'animation normal'), 'animation normal')),
        FinalIKEndRotationState(boolean(field(end, 'maintain_for_one_frame'), 'end flag'),
                                quaternion(quaternion_xyzw(field(end, 'requested_maintain_rotation'), 'requested rotation'), 'requested rotation'),
                                quaternion(quaternion_xyzw(field(end, 'rotation_before_solve'), 'pre-solve rotation'), 'pre-solve rotation')),
        vector(vector3(field(history, 'weighted_position'), 'weighted position'), 'weighted position'))


def parse_limbs(scenario: dict[str, object], inventory: dict[str, object], audit: dict[str, object], rig: GoalRig,
                hierarchy: list[dict[str, object]], frames: tuple[tuple[float, tuple[LocalTransform, ...]], ...], root: str) -> tuple[ReplayLimb, ...]:
    result: list[ReplayLimb] = []
    for value in array_value(field(scenario, 'limbs'), 'selected limbs'):
        row = object_value(value, 'selected limb')
        name = text_value(field(row, 'name'), 'limb name')
        matching = [limb for limb in rig.limbs if limb.name == name]
        if len(matching) != 1 or any(item.name == name for item in result):
            raise ValueError('Missing, duplicate or unsupported selected Avatar limb: ' + name)
        solver = source_solver(inventory, audit, object_value(field(row, 'component'), 'component selector'), matching[0], hierarchy, root)
        serialized_weight = _input_float32(number_value(field(solver, 'bendModifierWeight'), 'source modifier weight'))
        if integer_value(field(row, 'serialized_bend_modifier'), 'serialized enum') != 0 or _input_float32(number_value(field(row, 'serialized_bend_modifier_weight'), 'serialized weight')) != serialized_weight:
            raise ValueError('FinalIK scenario serialized modifier fields differ from source snapshot: ' + name)
        initialization = object_value(field(row, 'initialization'), 'initialization')
        if integer_value(field(initialization, 'frame_index'), 'initialization frame index') != 0 or number_value(field(initialization, 'time'), 'initialization time') != frames[0][0]:
            raise ValueError('FinalIK initialization must explicitly select this export first frame')
        normal = vector(vector3(field(initialization, 'bend_normal'), 'initial bend normal'), 'initial bend normal')
        baseline = vector(vector3(field(row, 'baseline_bend_normal'), 'baseline normal'), 'baseline normal')
        calibration = calibrate_direct_limb(rig, frames[0][1], matching[0], normal)
        evidence = {'component': field(row, 'component'), 'solver': solver, 'initialization': initialization,
                    'baseline_bend_normal': baseline, 'static_source_semantics': 'serialized_snapshot_not_runtime_state'}
        result.append(ReplayLimb(name, calibration, baseline, parse_history(field(initialization, 'history')), evidence))
    if not result:
        raise ValueError('FinalIK replay requires at least one explicit limb')
    return tuple(result)


def parse_limb_request(value: object) -> FinalIKAnimationLimbRequest:
    row = object_value(value, 'frame limb')
    return FinalIKAnimationLimbRequest(
        _input_float32(number_value(field(row, 'position_weight'), 'position weight')),
        vector(vector3(field(row, 'target_position'), 'target position'), 'target position'),
        quaternion(quaternion_xyzw(field(row, 'target_rotation'), 'target rotation'), 'target rotation'),
        _input_float32(number_value(field(row, 'rotation_weight'), 'rotation weight')),
        _input_float32(number_value(field(row, 'maintain_rotation_weight'), 'maintain rotation weight')),
        _input_float32(number_value(field(row, 'bend_modifier_weight'), 'runtime modifier weight')),
        boolean(field(row, 'initiated'), 'initiated'),
        boolean(field(row, 'maintain_bend_requested'), 'maintain bend request'),
        boolean(field(row, 'maintain_rotation_requested'), 'maintain rotation request'))


def evaluate_frames(rig: GoalRig, limbs: tuple[ReplayLimb, ...], frames: tuple[tuple[float, tuple[LocalTransform, ...]], ...],
                    scenario_frames: object) -> tuple[list[dict[str, object]], list[dict[str, object]]]:
    rows = array_value(scenario_frames, 'scenario frames')
    if len(rows) != len(frames):
        raise ValueError('FinalIK scenario frame count must equal the fresh decoded frame count')
    names = tuple(limb.name for limb in limbs)
    states = tuple(limb.initial_state for limb in limbs)
    result_frames: list[dict[str, object]] = []
    diagnostics: list[dict[str, object]] = []
    for index, ((time, input_locals), raw) in enumerate(zip(frames, rows, strict=True)):
        row = object_value(raw, 'scenario frame')
        if number_value(field(row, 'time'), 'scenario time') != time:
            raise ValueError('FinalIK scenario time differs from fresh decoded clock: frame=' + str(index))
        requests = [object_value(item, 'frame limb') for item in array_value(field(row, 'limbs'), 'frame limbs')]
        if tuple(text_value(field(item, 'name'), 'limb name') for item in requests) != names:
            raise ValueError('FinalIK per-frame limb order differs from explicit selection: frame=' + str(index))
        locals_ = input_locals
        next_states: list[FinalIKAnimationLimbState] = []
        frame_diagnostics: list[dict[str, object]] = []
        for limb, state, request in zip(limbs, states, requests, strict=True):
            try:
                solved = solve_animation_limb(rig, locals_, limb.calibration, limb.baseline, parse_limb_request(request), state)
            except (ValueError, ArithmeticError) as error:
                raise ValueError('FinalIK replay failed: frame=' + str(index) + ', time=' + repr(time) + ', limb=' + limb.name + ', cause=' + str(error)) from error
            locals_ = solved.locals
            next_states.append(solved.state)
            frame_diagnostics.append({'name': limb.name, 'state': asdict(solved.state), 'modified_bend_normal': solved.modified_bend_normal,
                                      'position_stage_active': solved.position_stage_active, 'end_world_writes': solved.end_world_writes})
        if any(a.position != b.position or a.scale != b.scale for a, b in zip(input_locals, locals_, strict=True)):
            raise RuntimeError('FinalIK rotation writes changed source local translations or scales')
        states = tuple(next_states)
        result_frames.append({'time': time, 'locals': [encode_local(local) for local in locals_]})
        diagnostics.append({'frame_index': index, 'time': time, 'limbs': frame_diagnostics})
    return result_frames, diagnostics


def export_finalik_replay(config_path: Path, identity: FinalIKSourceIdentity, decoded_root: Path,
                          source_audit_path: Path, hierarchy_path: Path, input_frames_path: Path) -> FinalIKExportResult:
    config_hash = file_hash(config_path)
    request = object_value(read_json(config_path, 'FinalIK replay request'), 'FinalIK replay request')
    version = field(request, 'format')
    if version not in ('EndfieldFinalIKReplay/1', 'EndfieldFinalIKReplay/2'):
        raise ValueError('Unsupported FinalIK replay request format')
    provenance = text_value(field(request, 'provenance'), 'FinalIK replay provenance')
    expected_identity = object_value(field(request, 'source_identity'), 'FinalIK source identity')
    for name, actual in asdict(identity).items():
        expected = field(expected_identity, name)
        if type(expected) is not type(actual) or expected != actual:
            raise ValueError('FinalIK replay selected source identity mismatch: ' + name)
    scenario_path = resolve_input(config_path.parent, field(request, 'scenario'), 'scenario')
    inventory_path = resolve_input(config_path.parent, field(request, 'source_component_inventory'), 'source component inventory')
    paths = {'source_audit': source_audit_path, 'selected_avatar': decoded_root / 'avatar_parsed.json',
             'source_hierarchy': hierarchy_path, 'input_source_local_frames': input_frames_path,
             'frame_domain_verification': decoded_root / 'humanoid/merged/frame_domain_verification.json',
             'scenario': scenario_path, 'source_component_inventory': inventory_path}
    hashes = object_value(field(request, 'source_sha256'), 'source hashes')
    validate_hashes(hashes, paths)
    scenario = object_value(read_json(scenario_path, 'FinalIK scenario'), 'FinalIK scenario')
    scenario_format = 'EndfieldFinalIKScenario/1' if version == 'EndfieldFinalIKReplay/1' else 'EndfieldGrounderScenario/1'
    if field(scenario, 'format') != scenario_format:
        raise ValueError('Unsupported FinalIK scenario format')
    scenario_provenance = text_value(field(scenario, 'provenance'), 'scenario provenance')
    hierarchy = [object_value(item, 'hierarchy node') for item in array_value(read_json(hierarchy_path, 'source hierarchy'), 'source hierarchy')]
    domain = object_value(read_json(paths['frame_domain_verification'], 'frame domain'), 'frame domain')
    frames = validate_frames(read_json(input_frames_path, 'effective source frames'), len(hierarchy), domain)
    rig, _, _, _ = compile_avatar_rig(read_json(paths['selected_avatar'], 'selected Avatar'), identity.animator_root_path, hierarchy)
    audit = object_value(read_json(source_audit_path, 'source audit'), 'source audit')
    validate_source_avatar(audit, identity)
    inventory = object_value(read_json(inventory_path, 'component inventory'), 'component inventory')
    limbs = parse_limbs(scenario, inventory,
                        audit, rig, hierarchy, frames, identity.animator_root_path)
    grounder_evidence: dict[str, object] | None = None
    if version == 'EndfieldFinalIKReplay/2':
        from character_animation_grounder_export import parse_grounder_source, evaluate_grounder_frames
        grounder = parse_grounder_source(scenario, inventory, audit, rig, hierarchy, limbs, identity.animator_root_path)
        result_frames, diagnostics = evaluate_grounder_frames(rig, limbs, grounder, frames, field(scenario, 'frames'))
        grounder_evidence = grounder.evidence
    else:
        result_frames, diagnostics = evaluate_frames(rig, limbs, frames, field(scenario, 'frames'))
    validate_hashes(hashes, paths)
    if file_hash(config_path) != config_hash:
        raise ValueError('FinalIK replay configuration changed during execution')
    root = decoded_root / 'finalik_replay'
    root.mkdir(exist_ok=False)
    frames_path, diagnostics_path, receipt_path = (root / name for name in ('source_local_frames.json', 'frame_diagnostics.json', 'receipt.json'))
    write_frame_json(frames_path, result_frames)
    write_json(diagnostics_path, diagnostics)
    helpers = tuple(Path(__file__).with_name(name + '.py') for name in (
        'character_animation_finalik_export', 'character_finalik_animation_limb', 'character_finalik_pose',
        'character_finalik_bend_modifier', 'character_finalik_bone_rotation', 'character_finalik_bend_direction',
        'character_finalik_trigonometric_inputs', 'character_finalik_end_rotation', 'character_runtime_ik_weights',
        'character_finalik_weights', 'character_animation_goals', 'character_animation_goal_export'))
    if grounder_evidence is not None:
        helpers += tuple(Path(__file__).with_name(name + '.py') for name in (
            'character_animation_grounder_export', 'character_finalik_grounder_consumer',
            'character_finalik_grounder_pose', 'character_finalik_grounder_post', 'character_finalik_grounder'))
    input_paths = tuple(dict.fromkeys((config_path, *paths.values(), *helpers)))
    receipt: dict[str, object] = {'format': 'EndfieldFinalIKReplayReceipt/1', 'status': 'explicit_animation0_direct_limb_replay_applied',
        'source_identity': asdict(identity), 'source_component_evidence': 'authenticated_static_snapshot_not_fresh_runtime_capture',
        'provenance': provenance, 'scenario_provenance': scenario_provenance,
        'input_source_local_frames': str(input_frames_path), 'source_local_frames': str(frames_path),
        'frame_count': len(result_frames), 'limb_order': [limb.name for limb in limbs],
        'source_limbs': [limb.source_evidence for limb in limbs],
        'inputs': [{'path': str(path), 'sha256': file_hash(path)} for path in input_paths],
        'outputs': [{'path': str(path), 'sha256': file_hash(path)} for path in (frames_path, diagnostics_path)],
        'local_positions_scales_preserved': True, 'native_runtime_scheduler_reconstructed': False,
        'execution_order': 'fresh source-goal/optional ordinary pose -> explicit ordered FinalIK limb calls',
        'validation_boundary': 'Source Animation0/direct hierarchy arithmetic reuses native-verified kernels. Initialization pose, baseline normals, invocation order, per-frame world targets, weights and one-frame histories are caller scenarios. Terrain, gameplay state, initialization timing, indirect hierarchy, Arm modifier and native global scheduling are not reconstructed; scalar clip weights are not guessed.'}
    if grounder_evidence is not None:
        receipt.update({
            'status': 'explicit_grounder_two_leg_pose_replay_applied',
            'replay_request_format': version,
            'source_grounder': grounder_evidence,
            'local_positions_scales_preserved': False,
            'non_pelvis_local_positions_and_all_scales_preserved': True,
            'execution_order': 'fresh source-goal/optional ordinary pose -> pelvis history/pre offset -> source SetLegIK -> left/right Animation0 limbs -> left/right foot post clamp -> pelvis post capture',
            'validation_boundary': 'The bounded Grounder pelvis/two-leg/foot-post segment consumes native-verified arithmetic with exact snapshot Grounder/BipedIK/Transform PPtr identity. Ground queries, IKFootBone runtime observations/availability, offsets, theta, resolved weights, initialization and histories are explicit external inputs. Grounding terrain/smoothing, Grounder spine/body rotation, delegates and global Animator/BipedIK scheduling are not reconstructed. Only exact pelvis local translation may change; source scales remain preserved.',
        })
    write_json(receipt_path, receipt)
    return FinalIKExportResult(frames_path, receipt_path, input_paths, (frames_path, diagnostics_path, receipt_path))
