"""Bake independently referenced SkeletalMorph tables as neutral shape-key controls."""
from __future__ import annotations

from dataclasses import dataclass
import hashlib
import json
import math
from pathlib import Path
import zlib

import bpy
from mathutils import Matrix

import basic_expression_shape_baker as basic_baker
from basic_expression_shape_baker import BasicExpressionDefinition, BakedExpression, _attach_driver, _decode_source, _extract_pose_target_batch, _mapping_for_control, _read_json, _selected_rig_meshes
from character_expression_shape_keys import (
    ExpressionDriverControl, FacialBoneTarget, evaluated_skin_batch, validate_expression_drivers,
)


@dataclass(frozen=True)
class TableManifest:
    character_id: str
    template_id: str
    family: str
    source_document_path: str
    source_identity: dict[str, object]
    endpoint_control_names: tuple[str, ...]
    empty_control_names: tuple[str, ...]
    unsupported_control_names: tuple[str, ...]


def _record(value: object, context: str) -> dict[str, object]:
    if not isinstance(value, dict) or any(not isinstance(key, str) for key in value):
        raise TypeError(f"Expected JSON object: {context}")
    return value


def _array(value: object, context: str) -> list[object]:
    if not isinstance(value, list):
        raise TypeError(f"Expected JSON array: {context}")
    return value


def _text(value: object, context: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise TypeError(f"Expected nonempty string: {context}")
    return value


def _integer(value: object, context: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise TypeError(f"Expected integer: {context}")
    return value


def _digest(value: object, context: str) -> str:
    digest = _text(value, context).lower()
    if len(digest) != 64 or any(character not in "0123456789abcdef" for character in digest):
        raise ValueError(f"Expected SHA-256 digest: {context}")
    return digest


def _manifest(path: Path) -> TableManifest:
    document = _record(json.loads(path.read_text(encoding="utf-8-sig")), "expression table bake manifest")
    if document.get("format") != "EndfieldExpressionTableBakeManifest/1":
        raise ValueError(f"Unsupported expression table bake manifest: {path}")
    family = _text(document.get("family"), "manifest.family")
    if family != "ear":
        raise ValueError(f"This authoring adapter accepts an ear table only: family={family}")
    identity = _record(document.get("source_identity"), "manifest.source_identity")
    endpoint_names = tuple(_text(value, f"endpoint_control_names[{index}]") for index, value in enumerate(
        _array(document.get("endpoint_control_names"), "manifest.endpoint_control_names")))
    empty_names = tuple(_text(value, f"empty_control_names[{index}]") for index, value in enumerate(
        _array(document.get("empty_control_names"), "manifest.empty_control_names")))
    unsupported_names = tuple(_text(value, f"unsupported_control_names[{index}]") for index, value in enumerate(
        _array(document.get("unsupported_control_names"), "manifest.unsupported_control_names")))
    all_names = endpoint_names + empty_names + unsupported_names
    if len(set(all_names)) != len(all_names):
        raise ValueError("Manifest expression control names must be unique and have one disposition each")
    if not endpoint_names:
        raise ValueError("Ear table manifest has no supported nonempty skeletal controls")
    return TableManifest(
        _text(document.get("character_id"), "manifest.character_id"),
        _text(document.get("template_id"), "manifest.template_id"),
        family,
        _text(document.get("source_document_path"), "manifest.source_document_path"),
        identity,
        endpoint_names,
        empty_names,
        unsupported_names,
    )


def _resource_path(value: str) -> str:
    return "/".join(part for part in value.replace("\\", "/").strip("/").split("/") if part).casefold()


def _verify_table_identity(manifest: TableManifest, table: dict[str, object], table_payload: bytes,
                           audit_payload: bytes) -> dict[str, object]:
    identity = manifest.source_identity
    source = _record(table.get("source"), "source")
    avatar = _record(table.get("selected_animator_avatar"), "selected_animator_avatar")
    provenance = _record(table.get("provenance"), "provenance")
    binding = _record(table.get("avatar_binding_provenance"), "avatar_binding_provenance")
    if table.get("schema") != "EndfieldCharacterExpressionSource/2" or table.get("status") != "verified_raw_source_extracted":
        raise ValueError("Expression table sidecar is not a verified raw source extraction")
    if _text(table.get("character_id"), "table.character_id") != manifest.character_id:
        raise ValueError("Expression table character differs from its manifest")
    identity_fields = (
        "table_identity", "character_id", "template_id", "reference_field", "reference_value",
        "resource_path", "provider_logical_name", "provider_kind", "serialized_file", "object_path_id",
        "object_class_id", "object_bytes", "bundle_sha256", "raw_object_sha256",
        "decoded_source_canonical_sha256", "avatar_cab", "avatar_path_id", "asset_name",
    )
    for field in identity_fields:
        if field not in identity:
            raise ValueError(f"Manifest source identity is missing required field: {field}")
    if _text(identity.get("character_id"), "identity.character_id") != manifest.character_id:
        raise ValueError("Manifest source identity belongs to a different character")
    if _text(identity.get("template_id"), "identity.template_id") != manifest.template_id:
        raise ValueError("Manifest source identity belongs to a different prefab template")
    if _text(identity.get("reference_field"), "identity.reference_field") != "earMorphAvatarName":
        raise ValueError("Ear table manifest does not bind the exact PrefabInfo ear reference")
    if _text(identity.get("provider_kind"), "identity.provider_kind") not in ("match", "exact"):
        raise ValueError("Ear table provider kind is not an accepted exact resource-index candidate")
    for field, actual in (
        ("resource_path", source.get("container_path")),
        ("provider_logical_name", source.get("provider_logical_name")),
        ("provider_kind", source.get("provider_kind")),
        ("serialized_file", source.get("serialized_file")),
        ("object_path_id", source.get("object_path_id")),
        ("bundle_sha256", source.get("bundle_sha256")),
        ("raw_object_sha256", source.get("raw_object_sha256")),
    ):
        expected = _text(identity.get(field), f"identity.{field}")
        if field == "resource_path":
            matches = _resource_path(expected) == _resource_path(_text(actual, f"source.{field}"))
        else:
            matches = expected.casefold() == _text(actual, f"source.{field}").casefold()
        if not matches:
            raise ValueError(f"Ear table source differs from manifest identity: field={field}")
    for field, actual in (("object_class_id", source.get("object_class_id")), ("object_bytes", source.get("object_bytes"))):
        if _integer(identity.get(field), f"identity.{field}") != _integer(actual, f"source.{field}"):
            raise ValueError(f"Ear table source differs from manifest identity: field={field}")
    if _text(avatar.get("cab"), "selected_animator_avatar.cab").casefold() != _text(identity.get("avatar_cab"), "identity.avatar_cab").casefold():
        raise ValueError("Selected Animator Avatar CAB differs from manifest identity")
    if _text(avatar.get("path_id"), "selected_animator_avatar.path_id") != _text(identity.get("avatar_path_id"), "identity.avatar_path_id"):
        raise ValueError("Selected Animator Avatar PathID differs from manifest identity")
    if avatar.get("class_name") != "Avatar" or binding.get("avatar_cab", "").casefold() != _text(identity.get("avatar_cab"), "identity.avatar_cab").casefold():
        raise ValueError("Expression sidecar exact Avatar binding differs from manifest identity")
    if _text(binding.get("avatar_path_id"), "avatar_binding_provenance.avatar_path_id") != _text(identity.get("avatar_path_id"), "identity.avatar_path_id"):
        raise ValueError("Expression sidecar exact Avatar binding PathID differs from manifest identity")
    if table.get("avatar_bone_bindings_status") != "exact_avatar_m_id_to_tos_paths_and_source_base_pose_verified":
        raise ValueError("Ear table does not have complete exact Avatar bone bindings")
    selection = _record(table.get("selection"), "selection")
    if selection.get("candidate_index_scan_complete") is not True or selection.get("selected_asset_avatar_pointer_verified") is not True:
        raise ValueError("Ear table provider scan or exact Avatar pointer was not verified")
    if _digest(provenance.get("source_audit_sha256"), "provenance.source_audit_sha256") != hashlib.sha256(audit_payload).hexdigest():
        raise ValueError("Ear table source-audit hash differs from the selected authoring audit")
    if _digest(identity.get("decoded_source_canonical_sha256"), "identity.decoded_source_canonical_sha256") != _digest(
        table.get("decoded_source_canonical_sha256"), "decoded_source_canonical_sha256"):
        raise ValueError("Ear table canonical source hash differs from the manifest")
    if _text(identity.get("asset_name"), "identity.asset_name") != _text(
        _record(_record(_record(table.get("raw"), "raw").get("asset_prefix"), "raw.asset_prefix"), "raw.asset_prefix").get("m_Name"),
        "raw.asset_prefix.m_Name"):
        raise ValueError("Ear table asset name differs from manifest identity")
    raw_source = _record(source, "source")
    raw_bytes = _text(raw_source.get("raw_object_bytes_base64"), "source.raw_object_bytes_base64")
    import base64
    import binascii
    try:
        decoded = base64.b64decode(raw_bytes, validate=True)
    except (binascii.Error, ValueError) as error:
        raise ValueError("Ear table serialized source object is not valid base64") from error
    raw_digest = hashlib.sha256(decoded).hexdigest()
    if raw_digest != _digest(identity.get("raw_object_sha256"), "identity.raw_object_sha256"):
        raise ValueError("Ear table serialized source object hash differs from manifest identity")
    if len(decoded) != _integer(identity.get("object_bytes"), "identity.object_bytes"):
        raise ValueError("Ear table serialized source object length differs from manifest identity")
    serialized_rows = _array(source.get("serialized_files"), "source.serialized_files")
    serialized_cabs = tuple(_text(_record(row, f"serialized_files[{index}]").get("cab"), f"serialized_files[{index}].cab")
                            for index, row in enumerate(serialized_rows))
    serialized_file = _text(source.get("serialized_file"), "source.serialized_file")
    if not any(cab.casefold() == serialized_file.casefold() for cab in serialized_cabs):
        raise ValueError("Ear table serialized-file identity is absent from indexed source files")
    identity_material = "|".join((
        manifest.template_id,
        _text(identity.get("reference_field"), "identity.reference_field"),
        _text(identity.get("reference_value"), "identity.reference_value"),
        _resource_path(_text(identity.get("resource_path"), "identity.resource_path")),
        _text(identity.get("provider_logical_name"), "identity.provider_logical_name").casefold(),
        f"{serialized_file}|{source.get('object_path_id')}|{source.get('object_class_id')}|{source.get('object_bytes')}",
        _text(identity.get("avatar_cab"), "identity.avatar_cab").casefold(),
        _text(identity.get("avatar_path_id"), "identity.avatar_path_id"),
        hashlib.sha256(audit_payload).hexdigest(),
        raw_digest,
    ))
    if hashlib.sha256(identity_material.encode("utf-8")).hexdigest() != _digest(identity.get("table_identity"), "identity.table_identity"):
        raise ValueError("Ear table manifest identity digest does not match its source evidence")
    if not table_payload:
        raise ValueError("Ear table sidecar is unexpectedly empty")
    return source


def _verify_control_partition(table: dict[str, object], manifest: TableManifest) -> None:
    raw = _record(table.get("raw"), "raw")
    asset = _record(raw.get("asset_prefix"), "raw.asset_prefix")
    data = _record(asset.get("data"), "raw.asset_prefix.data")
    names = tuple(_text(value, f"morphMappingNames[{index}]") for index, value in enumerate(
        _array(data.get("morphMappingNames"), "morphMappingNames")))
    configs = tuple(_record(value, f"morphMappingCfgs[{index}]") for index, value in enumerate(
        _array(data.get("morphMappingCfgs"), "morphMappingCfgs")))
    rows = tuple(_record(value, f"control_index[{index}]") for index, value in enumerate(
        _array(table.get("control_index"), "control_index")))
    references = tuple(_record(value, f"managed_references[{index}]") for index, value in enumerate(
        _array(raw.get("managed_references"), "managed_references")))
    if len(names) != len(configs) or len(names) != len(rows):
        raise ValueError("Ear table raw names, mapping configs, and control index have different lengths")
    by_rid: dict[str, dict[str, object]] = {}
    for index, record in enumerate(references):
        rid = str(record.get("rid"))
        if rid in by_rid:
            raise ValueError(f"Ear table has duplicate managed-reference RID: {rid}")
        by_rid[rid] = record
    partition = {
        "source_pose_endpoint_candidate": set(manifest.endpoint_control_names),
        "empty_mapping": set(manifest.empty_control_names),
        "unsupported_mapping_class": set(manifest.unsupported_control_names),
    }
    if set(names) != set().union(*partition.values()) or len(names) != len(set(names)):
        raise ValueError("Ear table manifest control dispositions do not cover every exact source control once")
    for index, (name, config, row) in enumerate(zip(names, configs, rows, strict=True)):
        if row.get("index") != index or row.get("name") != name:
            raise ValueError(f"Ear table control index/name differs from exact source arrays: index={index}")
        rid = str(config.get("rid"))
        if str(row.get("rid")) != rid:
            raise ValueError(f"Ear table control RID differs from raw mapping config: index={index}")
        record = by_rid.get(rid)
        if record is None or record.get("class") != row.get("class_name"):
            raise ValueError(f"Ear table control lacks its exact registered class: index={index}")
        raw_data = _record(record.get("data"), f"managed reference {rid}.data")
        if raw_data.get("id") != index:
            raise ValueError(f"Ear table mapping ID differs from control index: index={index}")
        class_name = _text(row.get("class_name"), f"control_index[{index}].class_name")
        bones = _array(raw_data.get("bones"), f"control {name}.bones") if class_name == "SkeletalMorphMappingData" else []
        if name in partition["source_pose_endpoint_candidate"]:
            if class_name != "SkeletalMorphMappingData" or not bones:
                raise ValueError(f"Manifest endpoint candidate has no skeletal bone mappings: {name}")
            mapping = _record(record.get("data"), f"managed reference {rid}.data")
            name_hash = _integer(mapping.get("nameHash"), f"managed reference {rid}.nameHash") & 0xFFFFFFFF
            if name_hash != (zlib.crc32(name.encode("utf-8")) & 0xFFFFFFFF):
                raise ValueError(f"Ear table mapping name hash differs from exact control name: {name}")
        elif name in partition["empty_mapping"]:
            if class_name != "SkeletalMorphMappingData" or bones:
                raise ValueError(f"Manifest empty control does not have an empty skeletal mapping: {name}")
        elif class_name == "SkeletalMorphMappingData":
            raise ValueError(f"Manifest unsupported control unexpectedly has a skeletal mapping: {name}")


def _identity_pose(armature: bpy.types.Object, tolerance: float) -> dict[str, float]:
    identity = Matrix.Identity(4)
    errors = {bone.name: max(abs(bone.matrix_basis[row][column] - identity[row][column])
                             for row in range(4) for column in range(4)) for bone in armature.pose.bones}
    if any(error > tolerance for error in errors.values()):
        raise ValueError(f"Independent expression baking requires the audited identity rig pose: residuals={list(errors.items())[:5]}")
    return errors


def _controller(name: str, labels: tuple[str, ...]) -> bpy.types.Object:
    if bpy.data.objects.get(name) is not None:
        raise FileExistsError(f"Independent expression controller already exists: {name}")
    controller = bpy.data.objects.new(name, None)
    bpy.context.collection.objects.link(controller)
    controller.empty_display_type = "PLAIN_AXES"
    controller.empty_display_size = 0.15
    controller["expression_controls_format"] = "EndfieldIndependentExpressionControls/1"
    for label in labels:
        controller[label] = 0.0
    return controller


def _bake_existing_key_data(armature: bpy.types.Object, meshes: tuple[bpy.types.Object, ...],
                            targets: tuple[FacialBoneTarget, ...], control_name: str, tolerance: float) -> tuple[BakedExpression, ...]:
    identity = Matrix.Identity(4)
    for obj in meshes:
        if obj.data.users != 1:
            raise ValueError(f"Independent expression baking requires single-user mesh data: {obj.name}, users={obj.data.users}")
        active_skin = tuple(modifier for modifier in obj.modifiers if modifier.show_viewport and modifier.type == "ARMATURE")
        if len(active_skin) != 1 or active_skin[0].object != armature:
            raise ValueError(f"Independent expression mesh is not bound to exactly the selected rig: {obj.name}")
        keys = obj.data.shape_keys
        if keys and (not keys.use_relative or control_name in keys.key_blocks):
            raise ValueError(f"Independent expression needs relative shape keys and a unique source control name: {obj.name}/{control_name}")
        if keys and any(abs(float(key.value)) > tolerance for key in keys.key_blocks):
            raise ValueError(f"Existing shape keys must be neutral before independent expression baking: {obj.name}")
    neutral = evaluated_skin_batch(meshes)
    try:
        for target in targets:
            armature.pose.bones[target.bone_name].matrix_basis = target.matrix_basis.copy()
        posed = evaluated_skin_batch(meshes)
    finally:
        for target in targets:
            armature.pose.bones[target.bone_name].matrix_basis = identity
        # The endpoint read below evaluates the neutral rig and new key together.
    baked: list[BakedExpression] = []
    created: list[tuple[bpy.types.Object, bpy.types.ShapeKey]] = []
    for obj in meshes:
        deltas = tuple(new - old for new, old in zip(posed[obj.name], neutral[obj.name], strict=True))
        maximum_delta = max((delta.length for delta in deltas), default=0.0)
        if maximum_delta <= tolerance:
            continue
        if obj.data.shape_keys is None:
            obj.shape_key_add(name="Basis", from_mix=False)
        basis = obj.data.shape_keys.reference_key
        key = obj.shape_key_add(name=control_name, from_mix=False)
        key.relative_key = basis
        for point, original, delta in zip(key.data, basis.data, deltas, strict=True):
            point.co = original.co + delta
        created.append((obj, key))
        baked.append(BakedExpression(obj.name, key.name, sum(delta.length > tolerance for delta in deltas), maximum_delta))
    try:
        for _obj, key in created:
            key.value = 1.0
        actual = evaluated_skin_batch(tuple(obj for obj, _key in created))
        for obj, key in created:
            error = max((value - expected).length for value, expected in zip(actual[obj.name], posed[obj.name], strict=True))
            if error > tolerance:
                raise ValueError(f"Independent shape endpoint differs from exact source pose: {obj.name}/{control_name}, error={error}, tolerance={tolerance}")
    finally:
        for _obj, key in created:
            key.value = 0.0
        bpy.context.view_layer.update()
    return tuple(baked)


def bake_expression_table_endpoints(manifest_path: Path, audit_path: Path, rig_name: str,
                                    controller_name: str, tolerance: float) -> dict[str, object]:
    """Bake all exact nonempty ear-table controls while preserving existing face drivers."""
    if not math.isfinite(tolerance) or tolerance <= 0.0:
        raise ValueError(f"Shape-key tolerance must be positive and finite: {tolerance}")
    manifest_resolved = manifest_path.resolve(strict=True)
    manifest = _manifest(manifest_resolved)
    table_path = (manifest_resolved.parent / manifest.source_document_path).resolve(strict=True)
    table, table_payload, table_resolved = basic_baker._read_json(table_path, "independent expression source table")
    audit, audit_payload, audit_resolved = _read_json(audit_path, "source audit")
    if audit.get("format") != "EndfieldCharacterSourceAudit/1":
        raise ValueError(f"Unsupported source audit format: {audit_resolved}")
    _verify_table_identity(manifest, table, table_payload, audit_payload)
    _verify_control_partition(table, manifest)
    armature = bpy.data.objects.get(rig_name)
    if armature is None or armature.type != "ARMATURE" or armature.data.pose_position != "POSE":
        raise ValueError(f"Explicit independent expression rig is missing or disabled: {rig_name}")
    if armature.animation_data and armature.animation_data.action:
        raise ValueError(f"Independent expression endpoints must be baked before body Actions are attached: {rig_name}")
    if armature.constraints or any(bone.constraints for bone in armature.pose.bones):
        raise ValueError(f"Independent expression rig has constraints: {rig_name}")
    basis_errors = _identity_pose(armature, tolerance)
    decoded = _decode_source(table, audit, table_payload, audit_payload, armature,
                             tuple(BasicExpressionDefinition(name, name, (), "ear_source_table", ())
                                   for name in manifest.endpoint_control_names))
    if decoded.character_id != manifest.character_id:
        raise ValueError("Decoded ear source belongs to a different character")
    avatar = decoded.rig_data.avatar_identity
    if avatar[0].casefold() != _text(manifest.source_identity.get("avatar_cab"), "identity.avatar_cab").casefold():
        raise ValueError("Decoded rig Avatar CAB differs from the ear-table manifest")
    if str(avatar[1]) != _text(manifest.source_identity.get("avatar_path_id"), "identity.avatar_path_id"):
        raise ValueError("Decoded rig Avatar PathID differs from the ear-table manifest")
    meshes = _selected_rig_meshes(armature)
    from character_authoring_validation import character_invariants, shape_signatures, verify_invariants, verify_preserved_shapes

    mesh_names = tuple(obj.name for obj in meshes)
    before_invariants = character_invariants(mesh_names)
    before_shapes = shape_signatures(mesh_names)
    face_controls: dict[str, dict[str, object]] = {}
    for obj in bpy.data.objects:
        if obj.get("expression_controls_format") != "EndfieldBasicExpressionControls/1":
            continue
        values = {key: float(value) for key, value in obj.items() if key != "expression_controls_format"}
        if any(abs(value) > tolerance for value in values.values()):
            raise ValueError(f"Existing basic facial controls must be neutral before ear baking: controller={obj.name}, values={values}")
        face_controls[obj.name] = values
    previous_keys = {obj.name: frozenset(key.name for key in obj.data.shape_keys.key_blocks)
                     if obj.data.shape_keys else frozenset() for obj in meshes}
    controller: bpy.types.Object | None = None
    try:
        baked_by_name: dict[str, tuple[BakedExpression, ...]] = {}
        mapping_rows_by_name: dict[str, tuple[dict[str, object], ...]] = {}
        mapping_info_by_name: dict[str, tuple[int, int, dict[str, object], list[dict[str, object]]]] = {}
        downstream = tuple(modifier for obj in meshes for modifier in obj.modifiers
                           if modifier.type != "ARMATURE" and modifier.show_viewport)
        try:
            # Match the facial baker: suspend shading geometry once for the whole batch.
            for modifier in downstream:
                modifier.show_viewport = False
            bpy.context.view_layer.update()
            for definition in decoded.definitions:
                mapping_info = _mapping_for_control(decoded, definition)
                if mapping_info is None:
                    raise ValueError(f"Manifest endpoint control is absent from decoded table: {definition.source_name}")
                mapping_info_by_name[definition.source_name] = mapping_info
            poses = _extract_pose_target_batch(armature, decoded,
                tuple((definition, mapping_info_by_name[definition.source_name][3])
                      for definition in decoded.definitions))
            for definition in decoded.definitions:
                targets, mapping_rows = poses[definition.source_name]
                baked_by_name[definition.source_name] = _bake_existing_key_data(
                    armature, meshes, targets, definition.source_name, tolerance)
                mapping_rows_by_name[definition.source_name] = mapping_rows
        finally:
            for modifier in downstream:
                modifier.show_viewport = True
            bpy.context.view_layer.update()
        controller = _controller(controller_name, manifest.endpoint_control_names)
        control_rows: list[dict[str, object]] = []
        for name in manifest.endpoint_control_names:
            definition = next(item for item in decoded.definitions if item.source_name == name)
            control_index, rid, mapping_data, mapping_bones = mapping_info_by_name[name]
            baked = baked_by_name[name]
            for item in baked:
                obj = bpy.data.objects[item.object_name]
                key = obj.data.shape_keys.key_blocks.get(item.shape_key_name)
                if key is None:
                    raise ValueError(f"Independent expression shape key disappeared: {item.object_name}/{item.shape_key_name}")
                _attach_driver(key, controller, name)
                key.value = 0.0
            meshes_report = [{"object_name": item.object_name, "shape_key_name": item.shape_key_name,
                              "moved_vertices": item.moved_vertices, "maximum_delta_meters": item.maximum_delta}
                             for item in baked]
            control_rows.append({
                "source_control": name,
                "label": name,
                "status": "baked_and_synchronized" if meshes_report else "source_pose_endpoint_has_no_visible_mesh_delta_above_tolerance",
                "source_control_index": control_index,
                "managed_reference_rid": str(rid),
                "source_class": "SkeletalMorphMappingData",
                "bone_count": len(mapping_bones),
                "candidate_runtime_semantics_verified": False,
                "bone_mappings_and_pose_basis": list(mapping_rows_by_name[name]),
                "baked_meshes": meshes_report,
                "master_property_path": f'["{name}"]',
                "driven_objects": [{"object_name": item["object_name"], "shape_key_name": item["shape_key_name"]}
                                   for item in meshes_report],
            })
        controls_by_name = {row["source_control"]: row for row in control_rows}
        control_rows.extend({"source_control": name, "label": name, "status": "empty_mapping",
                             "source_class": "SkeletalMorphMappingData", "bone_count": 0,
                             "driven_objects": []} for name in manifest.empty_control_names)
        control_rows.extend({"source_control": name, "label": name, "status": "unsupported_mapping_class",
                             "source_class": "source table managed-reference class", "bone_count": 0,
                             "driven_objects": []} for name in manifest.unsupported_control_names)
        for name in manifest.endpoint_control_names:
            row = controls_by_name[name]
            if row["status"] == "baked_and_synchronized" and not row["driven_objects"]:
                raise AssertionError(f"Baked ear control has no driven shape keys: {name}")
        bpy.context.view_layer.update()
        verify_invariants(before_invariants, mesh_names)
        verify_preserved_shapes(before_shapes, mesh_names)
        face_controls_after = {
            obj.name: {key: float(value) for key, value in obj.items() if key != "expression_controls_format"}
            for obj in bpy.data.objects if obj.get("expression_controls_format") == "EndfieldBasicExpressionControls/1"
        }
        if face_controls_after != face_controls:
            raise AssertionError("Existing ten basic facial controls changed while baking ear endpoints")
        return {
            "format": "EndfieldIndependentExpressionTableBake/1",
            "status": "source_table_ear_endpoints_baked_and_neutral",
            "character_id": decoded.character_id,
            "family": manifest.family,
            "source_identity": manifest.source_identity,
            "source_document": {"file": table_resolved.name, "sha256": hashlib.sha256(table_payload).hexdigest(),
                                "bytes": len(table_payload)},
            "source_audit": {"file": audit_resolved.name, "sha256": hashlib.sha256(audit_payload).hexdigest()},
            "avatar_identity": {"cab": avatar[0], "path_id": str(avatar[1])},
            "rig": {"object_name": armature.name, "bone_count": len(armature.data.bones),
                    "identity_pose_max_matrix_error": max(basis_errors.values(), default=0.0)},
            "mesh_objects": list(mesh_names),
            "controller": {"object_name": controller.name, "neutral_default": 0.0,
                           "properties": list(manifest.endpoint_control_names)},
            "controls": control_rows,
            "preservation": {"existing_basic_face_controller_values_unchanged": True,
                             "existing_shape_data_unchanged": True,
                             "bone_rest_and_material_invariants_unchanged": True,
                             "runtime_morph_mixing_verified": False},
            "semantics_boundary": {
                "source_mapping": "Exact Avatar base-pose and SkeletalMorphMappingData candidate endpoint.",
                "runtime_morph_mixing_verified": False,
                "default_values": "All newly created ear controls are neutral at zero.",
            },
        }
    except Exception:
        for pose_bone in armature.pose.bones:
            pose_bone.matrix_basis = Matrix.Identity(4)
        bpy.context.view_layer.update()
        for obj in meshes:
            keys = obj.data.shape_keys
            if keys is None:
                continue
            for key in tuple(keys.key_blocks)[1:]:
                if key.name not in previous_keys[obj.name]:
                    if keys.animation_data:
                        path = key.path_from_id("value")
                        if any(curve.data_path == path for curve in keys.animation_data.drivers):
                            key.driver_remove("value")
                    obj.shape_key_remove(key)
        if controller is not None and bpy.data.objects.get(controller.name) is controller:
            bpy.data.objects.remove(controller, do_unlink=True)
        bpy.context.view_layer.update()
        raise


def validate_expression_table_endpoints(receipt: dict[str, object], tolerance: float) -> dict[str, object]:
    """Validate saved ear-control drivers at zero, half, and full values."""
    if not math.isfinite(tolerance) or tolerance <= 0.0:
        raise ValueError(f"Validation tolerance must be positive and finite: {tolerance}")
    if receipt.get("format") != "EndfieldIndependentExpressionTableBake/1":
        raise ValueError("Unsupported independent expression table bake receipt")
    controller_data = _record(receipt.get("controller"), "receipt.controller")
    controller_name = _text(controller_data.get("object_name"), "receipt.controller.object_name")
    controller = bpy.data.objects.get(controller_name)
    if controller is None or controller.get("expression_controls_format") != "EndfieldIndependentExpressionControls/1":
        raise ValueError(f"Saved ear expression controller is missing or malformed: {controller_name}")
    active = tuple(_record(value, f"receipt.controls[{index}]") for index, value in enumerate(
        _array(receipt.get("controls"), "receipt.controls")) if value.get("status") == "baked_and_synchronized")
    original_values = {row["source_control"]: float(controller[row["source_control"]]) for row in active}
    if any(abs(value) > tolerance for value in original_values.values()):
        raise AssertionError(f"Saved ear controls do not open neutral: {original_values}")
    controls: list[ExpressionDriverControl] = []
    for row in active:
        label = _text(row.get("source_control"), "control.source_control")
        driven = tuple(_record(item, f"{label}.driven_objects[{index}]") for index, item in enumerate(
            _array(row.get("driven_objects"), f"{label}.driven_objects")))
        keys: list[bpy.types.ShapeKey] = []
        for target in driven:
            obj_name = _text(target.get("object_name"), f"{label}.object_name")
            key_name = _text(target.get("shape_key_name"), f"{label}.shape_key_name")
            obj = bpy.data.objects.get(obj_name)
            if obj is None or obj.type != 'MESH' or obj.data.shape_keys is None:
                raise ValueError(f"Saved ear shape-key data is missing: {obj_name}/{key_name}")
            key = obj.data.shape_keys.key_blocks.get(key_name)
            if key is None:
                raise AssertionError(f"Saved ear shape key is missing: {obj_name}/{key_name}")
            keys.append(key)
        controls.append(ExpressionDriverControl(label, tuple(keys)))
    checks = [{"control": check.label, "requested": check.requested,
               "driven_key_count": check.driven_key_count, "maximum_driver_error": check.maximum_driver_error}
              for check in validate_expression_drivers(controller, tuple(controls), tolerance)]
    return {"status": "all_independent_expression_drivers_verified_at_0_0_5_1_and_restored",
            "controller": controller_name, "checked_control_count": len(active), "checks": checks,
            "controls_restored": original_values}
