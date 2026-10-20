"""Compare complete source hierarchy import against an accepted skin-only rig."""
from __future__ import annotations

import argparse
from dataclasses import dataclass
from pathlib import Path
import sys
import json

import bpy
import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'src/character_runtime'))
from character_geometry import import_character_geometry, read_geometry_payload
from character_rig_authoring import _classify_source_bones


@dataclass(frozen=True)
class MeshState:
    coordinates: np.ndarray
    evaluated: np.ndarray
    weights: tuple[tuple[int, str, float], ...]


def mesh_states(names: tuple[str, ...]) -> dict[str, MeshState]:
    # Hidden LODs must participate in both dependency graphs for a skin comparison.
    for name in names:
        bpy.data.objects[name].hide_viewport = False
        bpy.data.objects[name].hide_set(False)
    bpy.context.view_layer.update()
    graph = bpy.context.evaluated_depsgraph_get()
    result: dict[str, MeshState] = {}
    for name in names:
        obj = bpy.data.objects[name]
        evaluated = obj.evaluated_get(graph)
        mesh = evaluated.to_mesh()
        try:
            result[name] = MeshState(np.array([tuple(vertex.co) for vertex in obj.data.vertices]),
                np.array([tuple(evaluated.matrix_world @ vertex.co) for vertex in mesh.vertices]),
                tuple((vertex.index, obj.vertex_groups[group.group].name, group.weight)
                      for vertex in obj.data.vertices for group in vertex.groups))
        finally:
            evaluated.to_mesh_clear()
    return result


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    for name in ('baseline', 'geometry', 'output'):
        parser.add_argument('--' + name, type=Path, required=True)
    args = parser.parse_args(sys.argv[sys.argv.index('--') + 1:])
    document, _ = read_geometry_payload(args.geometry)
    names = tuple(mesh['name'] for mesh in document['meshes'])
    bpy.ops.wm.open_mainfile(filepath=str(args.baseline), use_scripts=False, load_ui=False)
    rigs = tuple(obj for obj in bpy.context.scene.objects if obj.type == 'ARMATURE')
    if len(rigs) != 1:
        raise AssertionError('Baseline needs one selected source Armature')
    old_rig = rigs[0]
    old_bones = {bone.name: (np.array(bone.matrix_local), bone.parent.name if bone.parent else None, bone.use_deform)
                 for bone in old_rig.data.bones}
    baseline = mesh_states(names)
    for obj in tuple(bpy.data.objects):
        bpy.data.objects.remove(obj, do_unlink=True)
    imported = import_character_geometry(args.geometry)
    rig = next(obj for obj in imported if obj.type == 'ARMATURE')
    current = mesh_states(names)
    errors: list[float] = []
    for name, (matrix, parent, deform) in old_bones.items():
        bone = rig.data.bones[name]
        if (bone.parent.name if bone.parent else None) != parent or bone.use_deform != deform:
            raise AssertionError(f'Existing bone parent/deform changed: {name}')
        errors.append(float(np.max(np.abs(np.array(bone.matrix_local) - matrix))))
    vertex_errors: list[float] = []
    for name in names:
        old, new = baseline[name], current[name]
        if not np.array_equal(old.coordinates, new.coordinates) or old.weights != new.weights:
            raise AssertionError(f'Source mesh coordinates or skin weights changed: {name}')
        vertex_errors.append(float(np.max(np.abs(old.evaluated - new.evaluated))))
    paths = {bone.get('source_transform_path'): bone for bone in rig.data.bones}
    if set(paths) != {node['path'] for node in document['nodes']}:
        raise AssertionError('Imported rig does not retain every exact source Transform')
    for node in document['nodes']:
        bone = paths[node['path']]
        if (bone.parent.get('source_transform_path') if bone.parent else None) != node['parent_path']:
            raise AssertionError(f'Source hierarchy parent changed: {node["path"]}')
    controls = {row['bone']: row for row in _classify_source_bones(rig, {}, set())}
    added = tuple(bone for bone in rig.data.bones if bone.name not in old_bones)
    if not added or any(bone.use_deform or not bone.hide or controls[bone.name]['role'] != 'Source Details' for bone in added):
        raise AssertionError('New unskinned source nodes must remain hidden nondeforming Source Details')
    if max(errors) > 1e-7 or max(vertex_errors) > 1e-6:
        raise AssertionError(f'Existing source rig/geometry changed: bone={max(errors)}, mesh={max(vertex_errors)}')
    args.output.write_text(json.dumps({'status': 'passed', 'existing_bones': len(old_bones),
        'source_nodes': len(paths), 'added_hidden_nondeforming_nodes': len(added), 'source_meshes': len(names),
        'maximum_existing_bone_matrix_error': max(errors), 'maximum_evaluated_vertex_error_meters': max(vertex_errors),
        'source_coordinates_and_skin_identical': True,
        'evaluation_policy': 'All source meshes temporarily visible in memory, including normally hidden LODs and shadow proxies.'}, indent=2), encoding='utf-8')


if __name__ == '__main__':
    main()
