"""Attach an optional source-shell Fur presentation to the normal character build."""

from __future__ import annotations

import json
import math
from pathlib import Path
from typing import TYPE_CHECKING, Callable, Literal, Mapping, Sequence, cast

import bpy

from character_fur_appearance import (
    FurAppearance,
    FurSurfaceDetail,
    apply_fur_appearance,
    apply_fur_surface_detail,
    verify_fur_appearance,
)
from character_fur_curves_profile import (
    FurCurvesPreparation,
    attach_prepared_fur_curves,
)
from character_fur_source_shell import (
    activate_source_fur_shell,
    build_source_fur_shell,
    verify_source_fur_shell,
)
from patch_character_hgrp import source_keywords

if TYPE_CHECKING:
    from rebuild_character_materials import MaterialSource, SourcePacket


FurPresentation = Literal["source_shell", "charcoal_embers"]
CHARCOAL_EMBER_APPEARANCE = FurAppearance(
    0.15, 0.3, (1.0, 0.3, 0.15, 1.0), 2.0, "ALPHA"
)
CHARCOAL_EMBER_DETAIL = FurSurfaceDetail(0.35, 0.18, 0.0005)


def _object(value: object, label: str) -> dict[str, object]:
    if not isinstance(value, dict) or any(not isinstance(key, str) for key in value):
        raise TypeError(f"Expected a JSON object: {label}")
    return cast(dict[str, object], value)


def _identity(value: object, label: str) -> tuple[str, int]:
    row = _object(value, label)
    source, path_id = row.get("source"), row.get("path_id")
    if not isinstance(source, str) or not source.strip():
        raise TypeError(f"Expected a nonempty source identity: {label}.source")
    if isinstance(path_id, bool) or not isinstance(path_id, (str, int)):
        raise TypeError(f"Expected a decimal source PathID: {label}.path_id")
    try:
        return source.casefold(), int(path_id, 10) if isinstance(
            path_id, str
        ) else path_id
    except ValueError as error:
        raise ValueError(
            f"Source PathID must be decimal: {label}.path_id={path_id!r}"
        ) from error


def _source_material(
    entry: Mapping[str, object], packet: SourcePacket
) -> MaterialSource:
    identity = _identity(entry.get("source_group_identity"), "source_group_identity")
    matches = tuple(
        material
        for material in packet.materials
        if (material.identity[0].casefold(), material.identity[1]) == identity
    )
    if len(matches) != 1:
        raise ValueError(
            f"Prepared Fur source identity must resolve to one material: {identity}; matches={len(matches)}"
        )
    return matches[0]


def _finite_source_value(values: Mapping[str, object], name: str) -> float | None:
    value = values.get(name)
    if (
        isinstance(value, bool)
        or not isinstance(value, (float, int))
        or not math.isfinite(float(value))
    ):
        return None
    return float(value)


def _artist_skip_reason(
    source: MaterialSource, receipt: Mapping[str, object]
) -> str | None:
    return _artist_skip_reason_from_fields(
        source.shader,
        source.keywords,
        source.floats,
        source.shader_property_types,
        {texture.name for texture in source.textures},
        receipt,
    )


def _artist_skip_reason_from_fields(
    shader: str,
    source_keywords_value: Sequence[str],
    source_values: Mapping[str, object],
    property_types: Mapping[str, object],
    texture_names: set[str],
    receipt: Mapping[str, object],
) -> str | None:
    keywords = frozenset(source_keywords_value)
    if shader != "HGRP/CharacterNPR":
        return "source_shader_not_character_npr"
    if "_CHARACTER_FUR" not in keywords:
        return "source_keyword_character_fur_missing"
    if "_CHARACTER_FUR_DYE" not in keywords:
        return "source_keyword_fur_dye_missing"
    if "_CHARACTER_VFX_SPECIAL" not in keywords:
        return "source_keyword_special_vfx_missing"

    expected_property_types = {
        "_FurTTIntensity": (2, 3),
        "_FurNoise": (2, 3),
        "_FurDyeEnable": (2,),
        "_FurDyeIntensity": (3,),
        "_FurDyeMap": (4,),
        "_EnableCharacterVFX": (2, 3),
        "_AlphaPremultiply": (2, 3),
    }
    for name, supported in expected_property_types.items():
        if property_types.get(name) not in supported:
            return f"source_shader_property_type_unsupported:{name}"

    required_values = (
        "_FurTTIntensity",
        "_FurNoise",
        "_FurDyeEnable",
        "_FurDyeIntensity",
        "_EnableCharacterVFX",
        "_AlphaPremultiply",
    )
    values = {
        name: _finite_source_value(source_values, name) for name in required_values
    }
    for name, value in values.items():
        if value is None:
            return f"source_parameter_missing_or_nonfinite:{name}"
    if values["_FurTTIntensity"] is None or not 0.0 <= values["_FurTTIntensity"] <= 1.0:
        return "source_fur_transmission_intensity_out_of_range"
    if values["_FurNoise"] is None or values["_FurNoise"] < 0.0:
        return "source_fur_noise_out_of_range"
    if (
        values["_FurDyeEnable"] != 1.0
        or values["_FurDyeIntensity"] is None
        or not 0.0 <= values["_FurDyeIntensity"] <= 1.0
    ):
        return "source_fur_dye_disabled_or_out_of_range"
    if values["_EnableCharacterVFX"] != 1.0:
        return "source_special_vfx_disabled"
    if values["_AlphaPremultiply"] != 0.0:
        return "source_special_vfx_alpha_premultiply_unsupported"

    if "_FurDyeMap" not in texture_names:
        return "source_fur_dye_map_missing"

    dye = _object(receipt.get("dye"), "source shell dye")
    transmission = _object(receipt.get("transmission"), "source shell transmission")
    special = _object(receipt.get("special_vfx"), "source shell special VFX")
    if dye.get("status") == "not_selected":
        return "source_fur_dye_branch_not_connected"
    if transmission.get("format") != "EndfieldFurTransmission/1":
        return "source_fur_transmission_not_connected"
    if special.get("format") != "EndfieldCharacterSpecialVfx/1":
        return "source_special_vfx_not_connected"
    source_values = _object(special.get("source_values"), "source special VFX values")
    if (
        source_values.get("_EnableCharacterVFX") != values["_EnableCharacterVFX"]
        or source_values.get("_AlphaPremultiply") != values["_AlphaPremultiply"]
    ):
        return "source_special_vfx_parameters_differ_from_material"
    transmission_values = _object(
        transmission.get("parameters"), "source transmission parameters"
    )
    if (
        transmission_values.get("_FurTTIntensity") != values["_FurTTIntensity"]
        or transmission_values.get("_FurNoise") != values["_FurNoise"]
    ):
        return "source_transmission_parameters_differ_from_material"
    return None


def _material_signature(
    obj: bpy.types.Object,
) -> tuple[tuple[int, int | None] | None, ...]:
    return tuple(
        None
        if slot.material is None
        else (
            slot.material.as_pointer(),
            slot.material.node_tree.as_pointer()
            if slot.material.use_nodes and slot.material.node_tree
            else None,
        )
        for slot in obj.material_slots
    )


def _deformation_signature(obj: bpy.types.Object) -> tuple[object, ...]:
    parent = obj.parent.as_pointer() if obj.parent is not None else None
    groups = tuple(group.name for group in obj.vertex_groups)
    modifiers = tuple(
        (
            modifier.type,
            getattr(modifier, "object", None).as_pointer()
            if getattr(modifier, "object", None) is not None
            else None,
            getattr(modifier, "use_vertex_groups", None),
            getattr(modifier, "vertex_group", None),
        )
        for modifier in obj.modifiers
    )
    return parent, groups, modifiers


def _verify_surface_detail(
    shell: bpy.types.Object, expected: FurSurfaceDetail
) -> dict[str, object]:
    record = _object(
        json.loads(shell["fur_surface_detail"]), "Fur surface detail receipt"
    )
    settings = _object(record.get("settings"), "Fur surface detail settings")
    if (
        record.get("format") != "EndfieldFurSurfaceDetail/1"
        or settings != expected._asdict()
    ):
        raise AssertionError(
            f"Fur surface detail settings differ from the selected presentation: {shell.name}"
        )
    appearance = _object(
        json.loads(shell["fur_artist_appearance"]), "Fur appearance receipt"
    )
    tree = bpy.data.node_groups[str(appearance["group"])]
    bump = tree.nodes[str(record["bump_node"])]
    (principled,) = (node for node in tree.nodes if node.type == "BSDF_PRINCIPLED")
    (height_link,) = bump.inputs["Height"].links
    (strength_link,) = bump.inputs["Strength"].links
    (distance_link,) = bump.inputs["Distance"].links
    (normal_link,) = principled.inputs["Normal"].links
    contrast_socket = tree.interface.items_tree.get("Fur clump contrast")
    strength_socket = tree.interface.items_tree.get("Fur relief strength")
    distance_socket = tree.interface.items_tree.get("Fur relief distance")
    color_node = tree.nodes[str(record["color_node"])]
    (contrast_link,) = color_node.inputs[2].links
    source = _object(json.loads(shell["source_fur_shell"]), "source shell receipt")
    transmission = _object(source.get("transmission"), "source transmission receipt")
    if (
        bump.type != "BUMP"
        or contrast_socket is None
        or not math.isclose(
            contrast_socket.default_value,
            expected.contrast,
            rel_tol=1e-7,
            abs_tol=1e-12,
        )
        or strength_socket is None
        or not math.isclose(
            strength_socket.default_value,
            expected.relief_strength,
            rel_tol=1e-7,
            abs_tol=1e-12,
        )
        or distance_socket is None
        or not math.isclose(
            distance_socket.default_value,
            expected.relief_distance,
            rel_tol=1e-7,
            abs_tol=1e-12,
        )
        or color_node.type != "MIX_RGB"
        or contrast_link.from_node.type != "MATH"
        or contrast_link.from_node.operation != "ADD"
        or height_link.from_node.name != transmission.get("fur_sample_node")
        or height_link.from_socket.name != "Alpha"
        or strength_link.from_node.type != "GROUP_INPUT"
        or strength_link.from_socket.name != "Fur relief strength"
        or distance_link.from_node.type != "GROUP_INPUT"
        or distance_link.from_socket.name != "Fur relief distance"
        or normal_link.from_node != bump
        or normal_link.from_socket.name != "Normal"
    ):
        raise AssertionError(
            f"Fur surface detail graph is disconnected from its source mask or shader: {tree.name}"
        )
    return {"status": "passed", "settings": dict(settings), "group": tree.name}


def _saved_artist_skip_reason(
    group: bpy.types.ShaderNodeTree, receipt: Mapping[str, object]
) -> str | None:
    shader = group.get("source_shader")
    if not isinstance(shader, str):
        return "source_shader_missing"
    source_values = _object(
        json.loads(group["source_floats"]), "saved source material values"
    )
    property_types = _object(
        json.loads(group["source_shader_property_types"]),
        "saved source Shader property types",
    )
    source_textures = {
        str(node.get("source_slot"))
        for node in group.nodes
        if node.type == "TEX_IMAGE" and node.get("source_slot")
    }
    return _artist_skip_reason_from_fields(
        shader,
        source_keywords(group),
        source_values,
        property_types,
        source_textures,
        receipt,
    )


def _apply_presentation(
    receipt: Mapping[str, object],
    source: MaterialSource,
    layout: Callable[[bpy.types.NodeTree], None],
    presentation: FurPresentation,
) -> dict[str, object]:
    shell = bpy.data.objects[str(receipt["shell_mesh"])]
    source_mesh = bpy.data.objects[str(receipt["source_mesh"])]
    source_materials_before = _material_signature(source_mesh)
    shell_binding_before = _deformation_signature(shell)
    geometry_before = tuple(
        str(value)
        for value in (
            receipt["retained_source_digest"],
            receipt["shell_geometry_digest"],
            receipt["shell_positions_sha256"],
        )
    )
    if presentation == "source_shell":
        record: dict[str, object] = {
            "format": "EndfieldFurShellPresentation/1",
            "mode": presentation,
            "status": "source_shell",
            "source_shader": source.shader,
            "source_keywords": list(source.keywords),
            "source_materials_preserved": True,
            "animation_binding_preserved": True,
        }
    elif presentation == "charcoal_embers":
        reason = _artist_skip_reason(source, receipt)
        if reason is not None:
            record = {
                "format": "EndfieldFurShellPresentation/1",
                "mode": presentation,
                "status": "skipped_source_variant",
                "reason": reason,
                "source_shader": source.shader,
                "source_keywords": list(source.keywords),
                "source_materials_preserved": True,
                "animation_binding_preserved": True,
            }
        else:
            appearance = json.loads(
                json.dumps(apply_fur_appearance(shell, CHARCOAL_EMBER_APPEARANCE))
            )
            detail = apply_fur_surface_detail(shell, CHARCOAL_EMBER_DETAIL)
            trees = (
                str(appearance["group"]),
                *(str(row["group"]) for row in appearance["base_materials"]),
            )
            for tree_name in trees:
                layout(bpy.data.node_groups[tree_name])
            verify_fur_appearance(shell)
            detail_audit = _verify_surface_detail(shell, CHARCOAL_EMBER_DETAIL)
            record = {
                "format": "EndfieldFurShellPresentation/1",
                "mode": presentation,
                "status": "applied",
                "source_shader": source.shader,
                "source_keywords": list(source.keywords),
                "appearance": appearance,
                "surface_detail": detail,
                "surface_detail_audit": detail_audit,
                "source_materials_preserved": True,
                "animation_binding_preserved": True,
            }
    else:
        raise ValueError(
            f"Unsupported Fur source-shell presentation mode: {presentation!r}"
        )

    active_receipt = {**dict(receipt), "presentation": record}
    shell["source_fur_shell"] = json.dumps(active_receipt, sort_keys=True)
    geometry_after = tuple(
        str(value)
        for value in (
            active_receipt["retained_source_digest"],
            active_receipt["shell_geometry_digest"],
            active_receipt["shell_positions_sha256"],
        )
    )
    if geometry_before != geometry_after:
        raise AssertionError(
            f"Fur presentation changed the source-shell geometry record: {shell.name}"
        )
    if (
        _material_signature(source_mesh) != source_materials_before
        or _deformation_signature(shell) != shell_binding_before
    ):
        raise AssertionError(
            f"Fur presentation changed the original source material or shell rig binding: {shell.name}"
        )
    verify_source_fur_shell(active_receipt)
    return active_receipt


def attach_backend_fur_shells(
    preparation: FurCurvesPreparation,
    source_dir: Path,
    weather_profile_path: Path,
    controls: bpy.types.ShaderNodeTree,
    layout: Callable[[bpy.types.NodeTree], None],
    packet: SourcePacket,
    presentation: FurPresentation,
) -> dict[str, object]:
    """Build source-indexed shells and apply only an explicitly requested compatible artist preset."""
    if presentation not in ("source_shell", "charcoal_embers"):
        raise ValueError(f"Unsupported Fur presentation mode: {presentation!r}")
    curves = attach_prepared_fur_curves(
        preparation, source_dir, weather_profile_path, controls, layout
    )
    entries = curves.get("entries")
    if not isinstance(entries, list):
        raise TypeError("Backend Fur shells require a completed Curves entry list")
    results: list[dict[str, object]] = []
    for value in entries:
        entry = _object(value, "prepared Fur entry")
        source = _source_material(entry, packet)
        built = build_source_fur_shell(entry, packet, layout)
        active = activate_source_fur_shell(built)
        results.append(_apply_presentation(active, source, layout, presentation))
    receipt: dict[str, object] = {
        "format": "EndfieldPreparedSourceFurShells/1",
        "status": "completed",
        "presentation_mode": presentation,
        "preparation": curves["preparation"],
        "entries": results,
        "skipped_hidden_sources": curves["skipped_hidden_sources"],
        "source_mesh_candidates": curves["source_mesh_candidates"],
        "retained_curves_preparation": curves,
        "native_shader_semantics_verified": False,
    }
    receipt["verification"] = verify_backend_fur_shells(receipt)
    return receipt


def verify_backend_fur_shells(receipt: Mapping[str, object]) -> dict[str, object]:
    """Verify source-shell preservation and the selected per-source artist eligibility result."""
    from character_fur_source_shell import verify_prepared_fur_shells

    if (
        receipt.get("format") != "EndfieldPreparedSourceFurShells/1"
        or receipt.get("status") != "completed"
    ):
        raise ValueError("Backend Fur shell receipt is incomplete")
    presentation = receipt.get("presentation_mode")
    if presentation not in ("source_shell", "charcoal_embers"):
        raise ValueError(
            "Backend Fur shell receipt has an unsupported presentation mode"
        )
    base_audit = verify_prepared_fur_shells(dict(receipt))
    entries = receipt.get("entries")
    if not isinstance(entries, list):
        raise TypeError("Backend Fur shell receipt requires an entry array")
    applied = 0
    skipped = 0
    for value in entries:
        entry = _object(value, "source shell entry")
        record = _object(entry.get("presentation"), "source shell presentation")
        shell = bpy.data.objects[str(entry["shell_mesh"])]
        group = bpy.data.node_groups[str(entry["source_group"])]
        current_keywords = source_keywords(group)
        if (
            record.get("format") != "EndfieldFurShellPresentation/1"
            or record.get("mode") != presentation
            or record.get("source_materials_preserved") is not True
            or record.get("animation_binding_preserved") is not True
            or record.get("source_shader") != group.get("source_shader")
            or set(record.get("source_keywords", [])) != set(current_keywords)
        ):
            raise AssertionError(
                f"Source shell presentation record differs from the request: {shell.name}"
            )
        if presentation == "source_shell":
            if (
                record.get("status") != "source_shell"
                or shell.get("fur_artist_appearance")
                or shell.get("fur_surface_detail")
            ):
                raise AssertionError(
                    f"Source-shell mode has an unexpected artist Fur override: {shell.name}"
                )
            continue
        if record.get("status") == "skipped_source_variant":
            if shell.get("fur_artist_appearance") or shell.get("fur_surface_detail"):
                raise AssertionError(
                    f"Skipped Fur source variant received artist overrides: {shell.name}"
                )
            reason = _saved_artist_skip_reason(group, entry)
            if reason is None or record.get("reason") != reason:
                raise AssertionError(
                    f"Skipped Fur source variant does not match its recorded eligibility reason: {shell.name}"
                )
            skipped += 1
            continue
        if record.get("status") != "applied":
            raise AssertionError(
                f"Unknown artist Fur presentation status: {shell.name}"
            )
        reason = _saved_artist_skip_reason(group, entry)
        if reason is not None:
            raise AssertionError(
                f"Artist Fur was applied to an incompatible source variant: {shell.name}; reason={reason}"
            )
        appearance = _object(record.get("appearance"), "artist appearance record")
        detail = _object(record.get("surface_detail"), "artist surface detail record")
        expected_appearance = {
            **CHARCOAL_EMBER_APPEARANCE._asdict(),
            "ember_tint": list(CHARCOAL_EMBER_APPEARANCE.ember_tint),
        }
        if (
            appearance.get("settings") != expected_appearance
            or detail.get("settings") != CHARCOAL_EMBER_DETAIL._asdict()
        ):
            raise AssertionError(
                f"Artist Fur values differ from the accepted charcoal-ember profile: {shell.name}"
            )
        verify_fur_appearance(shell)
        _verify_surface_detail(shell, CHARCOAL_EMBER_DETAIL)
        applied += 1
    return {
        "status": "passed",
        "presentation_mode": presentation,
        "source_shell_count": len(entries),
        "artist_applied_count": applied,
        "artist_skipped_count": skipped,
        "skipped_hidden_source_count": base_audit["skipped_hidden_source_count"],
    }
