"""Build an authenticated offline single-clip clothing-layer scenario."""
from __future__ import annotations

from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Literal

from character_animation_goal_export import object_value, read_json, write_json
from character_animation_ordinary_export import OrdinarySourceIdentity, sha256
from character_animation_parameter_trace import (TraceSourceIdentity,
    DecodedParameterSource, crc, read_decoded_parameter_source,
    required_parameter_names, source_curves_at_frame, source_input_paths,
    validate_parameter_source_identity)
from animator_ordinary_plan import (all_source_ordinary_bindings,
    assess_ordinary_layers, select_ordinary_routes)
from character_cloth_curve_parameters import (ClothRuntimeInputs,
    compute_cloth_parameters)


@dataclass(frozen=True)
class DefaultClothingResult:
    status: Literal['generated', 'unsupported']
    configuration_path: Path | None
    parameters_path: Path | None
    layer_binding_hashes: tuple[int, ...]
    reason: str | None


def _sampling_record(sampling: dict[str, object]) -> dict[str, object]:
    required_paths = ('repository_src', 'game_root', 'resource_database',
                      'cab_inventory', 'sampler', 'cache_root')
    for name in required_paths:
        value = sampling[name]
        if not isinstance(value, str) or not value:
            raise TypeError(f'Expected nonempty sampling path: field={name}')
    prepared = sampling['prepared_bundle_root']
    if prepared is not None and (not isinstance(prepared, str) or not prepared):
        raise TypeError('Expected nullable nonempty sampling path: field=prepared_bundle_root')
    return dict(sampling)


def _styled_body_add(source: DecodedParameterSource) -> tuple[bool, str]:
    declarations = source.declarations
    name = 'bUseStyledLoopBodyAdd'
    key = crc(name)
    matches = tuple(row for row in declarations if row.name == name or row.name_hash == key)
    if len(matches) > 1:
        raise ValueError(f'Duplicate controller declaration for {name}')
    if not matches:
        return False, 'controller declaration absent; explicit static nonstyled false'
    declaration = matches[0]
    if declaration.name is not None and declaration.name != name:
        raise ValueError(f'Controller parameter hash collides with {name}: resolved_name={declaration.name}')
    if declaration.name_hash != key or declaration.kind not in (4, 9) or type(declaration.default_value) is not bool:
        raise ValueError(f'Expected serialized controller boolean default: field={name}')
    return declaration.default_value, 'serialized controller default'


def build_default_clothing_replay(
    decoded_root: Path,
    source_audit: Path,
    source_local_frames: Path,
    identity: OrdinarySourceIdentity,
    sampling: dict[str, object],
) -> DefaultClothingResult:
    """Generate source/default frame parameters for every supported ordinary layer."""
    decoded_root = decoded_root.resolve(strict=True)
    source_audit = source_audit.resolve(strict=True)
    source_local_frames = source_local_frames.resolve(strict=True)
    sampling_record = _sampling_record(sampling)
    decoded_paths = source_input_paths(decoded_root, source_local_frames)
    decoded_hashes = {name: sha256(path.resolve(strict=True))
                      for name, path in decoded_paths.items()}
    type_tree_path = decoded_root / 'native/controller_type_tree.json'
    references_path = decoded_root / 'native/controller_source_references.json'
    tree = object_value(read_json(type_tree_path, 'controller type tree'), 'controller type tree')
    references = object_value(read_json(references_path, 'controller source references'),
                              'controller source references')
    reader = object_value(read_json(decoded_paths['reader_request'], 'reader request'), 'reader request')
    clip = object_value(read_json(decoded_paths['clip_audit'], 'clip audit'), 'clip audit')
    validate_parameter_source_identity(reader, clip, references, TraceSourceIdentity(**asdict(identity)))
    assessments = assess_ordinary_layers(tree, references)
    combine_layers = tuple(row for row in assessments if row.binding_hash == crc('ClothCombine'))
    if combine_layers:
        indices = tuple(row.index for row in combine_layers)
        return DefaultClothingResult('unsupported', None, None, (),
            'ClothCombine source layer is present; this scalar evaluator supports only the absent branch; '
            + f'layer_indices={indices}')
    candidate_hashes = tuple(row.binding_hash for row in assessments
                             if row.status == 'source_authored_ordinary_candidate')
    if not candidate_hashes:
        return DefaultClothingResult('unsupported', None, None, (),
            'No source-authored ordinary layer passed the existing structural assessment.')
    if clip['float_curve_count'] == 0:
        return DefaultClothingResult('unsupported', None, None, (),
            'The selected clip has no scalar curve stream; the current source parameter reader requires one.')
    source = read_decoded_parameter_source(decoded_root, source_local_frames,
        TraceSourceIdentity(**asdict(identity)), decoded_hashes)
    layer_hashes = all_source_ordinary_bindings(assessments)
    routes = select_ordinary_routes(assessments, layer_hashes)
    required = required_parameter_names(routes, source.declarations)
    declarations = {row.name_hash: row for row in source.declarations}
    channels = {row.name_hash: row for row in source.channels}
    styled_body_add, styled_body_add_source = _styled_body_add(source)
    frame_provenance = (
        'Offline single-clip clothing authoring scenario; values come from exact clip curves and '
        'serialized controller defaults; transition_offset=0; '
        f'bUseStyledLoopBodyAdd={styled_body_add} ({styled_body_add_source}); '
        'no live state-machine state, collision, or cloth simulation is reconstructed.'
    )
    frames: list[dict[str, object]] = []
    for index, time in enumerate(source.times):
        parameters = compute_cloth_parameters(
            source_curves_at_frame(source, index, True, {}),
            ClothRuntimeInputs(0.0, styled_body_add),
        )
        for name, key in required.items():
            if name not in parameters:
                parameters[name] = (channels[key].values[index] if key in channels
                                    else float(declarations[key].default_value))
        frames.append({'time': time, 'parameters': parameters,
                       'provenance': frame_provenance})
    parameters_path = decoded_root / 'default_clothing_parameters.json'
    configuration_path = decoded_root / 'default_clothing_replay.json'
    if parameters_path.exists() or configuration_path.exists():
        raise FileExistsError(f'Default clothing output already exists under {decoded_root}')
    source_paths = {
        'parameters': parameters_path,
        'source_audit': source_audit,
        'selected_avatar': decoded_root / 'avatar_parsed.json',
        'source_hierarchy': decoded_root / 'humanoid/merged/source_hierarchy.json',
        'source_local_frames': source_local_frames,
        'controller_type_tree': type_tree_path,
        'controller_source_references': references_path,
        'float_acl': decoded_root / 'native/float.acl',
    }
    for name, path in source_paths.items():
        if name != 'parameters':
            path.resolve(strict=True)
    parameter_record = {
        'format': 'EndfieldClothParameterTrace/1',
        'status': 'offline_single_clip_default_clothing_parameters',
        'source_identity': asdict(identity),
        'frames': frames,
        'scenario': {
            'type': 'single_clip_source_curves_and_controller_defaults',
            'transition_offset': 0.0,
            'bUseStyledLoopBodyAdd': styled_body_add,
            'bUseStyledLoopBodyAdd_source': styled_body_add_source,
        },
        'runtime_animator_reconstructed': False,
        'collision_or_cloth_simulation_reconstructed': False,
        'boundary': 'Offline single-clip source/default values only; not a live state-machine or collision result.',
        'source_layer_coverage': [asdict(row) for row in assessments],
        'decoded_input_sha256': {str(decoded_paths[name]): digest for name, digest in decoded_hashes.items()},
    }
    write_json(parameters_path, parameter_record)
    source_hashes = {name: sha256(path.resolve(strict=True))
                     for name, path in source_paths.items()}
    write_json(configuration_path, {
        'format': 'EndfieldOrdinaryLayerReplay/1',
        'source_identity': asdict(identity),
        'provenance': frame_provenance,
        'parameters': parameters_path.name,
        'layer_binding_hashes': list(layer_hashes),
        'sampling': sampling_record,
        'source_sha256': source_hashes,
    })
    changed = tuple(str(path) for name, path in source_paths.items()
                    if sha256(path) != source_hashes[name])
    if changed:
        raise ValueError(f'Default clothing source inputs changed during generation: paths={changed}')
    return DefaultClothingResult('generated', configuration_path, parameters_path,
                                 layer_hashes, None)
