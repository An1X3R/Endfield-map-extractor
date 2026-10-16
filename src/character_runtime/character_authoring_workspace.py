"""Assemble editable rig presentation and root-level material inputs at export."""
from __future__ import annotations

from collections.abc import Sequence
from pathlib import Path

import bpy

from character_authoring import AnimationRequest
from character_material_authoring import package_material_authoring, verify_material_authoring
from character_viewport_preview import attach_viewport_preview, verify_viewport_preview
from character_weather_panel import attach_weather_panel, verify_weather_panel
from character_rig_authoring import (
    apply_rig_authoring, read_animation_human_slot_paths,
    read_humanoid_bone_map, verify_rig_authoring,
)


def prepare_authoring_workspace(source_directory: Path,
                                animations: Sequence[AnimationRequest]) -> dict[str, object]:
    """Use exact Avatar paths when present; preserve explicit legacy-map absence."""
    from blender_scene_materials import layout_shader_node_tree

    materials = tuple(sorted({slot.material for obj in bpy.context.scene.objects
                              if obj.type in {'MESH', 'CURVES'} for slot in obj.material_slots
                              if slot.material is not None and slot.material.use_nodes},
                             key=lambda material: material.name))
    material_report = package_material_authoring(materials, layout_shader_node_tree)
    material_check = verify_material_authoring(material_report)
    weather_panel = attach_weather_panel(materials)
    verify_weather_panel(weather_panel)
    rigs = tuple(obj for obj in bpy.context.scene.objects if obj.type == 'ARMATURE')
    if len(rigs) != 1:
        raise ValueError(f'Character workspace requires one source armature: found {len(rigs)}')
    rig_reports = []
    sidecar = source_directory / 'humanoid_bone_map.json'
    for rig in rigs:
        sources = tuple(item for item in animations if rig.name in item.armatures)
        paths = None
        if sources:
            paths = read_animation_human_slot_paths(sources[0].receipt)
            for source in sources[1:]:
                if read_animation_human_slot_paths(source.receipt) != paths:
                    raise ValueError(f'Animation Avatar mappings disagree for authoring rig: {rig.name}')
        elif sidecar.is_file():
            root_path = rig.get('source_transform_path')
            if not isinstance(root_path, str) or not root_path:
                raise ValueError(f'Avatar authoring requires an exact rig source path: {rig.name}')
            paths = read_humanoid_bone_map(sidecar, root_path)
        report = apply_rig_authoring(rig, paths)
        verify_rig_authoring(report)
        rig_reports.append(report)
    preview = attach_viewport_preview(rigs[0])
    verify_viewport_preview(preview)
    return {'format': 'EndfieldAuthoringWorkspace/1', 'materials': material_report,
            'material_check': material_check, 'rigs': rig_reports, 'viewport_preview': preview,
            'weather_panel': weather_panel}


def verify_authoring_workspace(report: dict[str, object]) -> dict[str, object]:
    """Check saved authoring entry points in the existing single reopen pass."""
    if report['format'] != 'EndfieldAuthoringWorkspace/1':
        raise ValueError('Unsupported character authoring workspace format')
    checks = {'status': 'passed', 'materials': verify_material_authoring(report['materials']),
              'rigs': [verify_rig_authoring(row) for row in report['rigs']]}
    if 'viewport_preview' in report:
        checks['viewport_preview'] = verify_viewport_preview(report['viewport_preview'])
    if 'weather_panel' in report:
        checks['weather_panel'] = verify_weather_panel(report['weather_panel'])
    return checks
