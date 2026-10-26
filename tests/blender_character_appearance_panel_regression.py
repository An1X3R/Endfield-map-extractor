"""Exercise appearance controls against real character files and an installed extension."""
import argparse
from array import array
import hashlib
import importlib
import json
import os
from pathlib import Path
import sys
import zipfile

import bpy


def _digest(value: object) -> str:
    return hashlib.sha256(repr(value).encode("utf-8")).hexdigest()


def _socket_value(value: object) -> object:
    if isinstance(value, (bool, int, float, str)) or value is None:
        return value
    try:
        return tuple(float(item) for item in value)  # type: ignore[union-attr]
    except (TypeError, ValueError):
        return repr(value)


def geometry_signature(source_objects: tuple[bpy.types.Object, ...]) -> str:
    """Hash mesh topology and positions without touching the source meshes."""
    digest = hashlib.sha256()
    for obj in sorted(source_objects, key=lambda item: item.name):
        if obj.type != "MESH":
            raise AssertionError(f"Expected a source mesh, got {obj.name}: {obj.type}")
        mesh = obj.data
        digest.update(repr((obj.name, len(mesh.vertices), len(mesh.edges), len(mesh.loops), len(mesh.polygons))).encode())
        coordinates = array("f", [0.0]) * (len(mesh.vertices) * 3)
        mesh.vertices.foreach_get("co", coordinates)
        digest.update(memoryview(coordinates))
        edges = array("i", [0]) * (len(mesh.edges) * 2)
        mesh.edges.foreach_get("vertices", edges)
        digest.update(memoryview(edges))
        loops = array("i", [0]) * len(mesh.loops)
        mesh.loops.foreach_get("vertex_index", loops)
        digest.update(memoryview(loops))
        polygons = tuple((polygon.loop_start, polygon.loop_total) for polygon in mesh.polygons)
        digest.update(repr(polygons).encode())
    return digest.hexdigest()


def material_signature(source_objects: tuple[bpy.types.Object, ...]) -> str:
    """Hash source material structure and slot assignments for one real character."""
    rows: list[object] = []
    materials = {slot.material for obj in source_objects for slot in obj.material_slots if slot.material is not None}
    for material in sorted(materials, key=lambda item: item.name):
        tree = material.node_tree
        nodes: tuple[object, ...] = ()
        links: tuple[object, ...] = ()
        if tree is not None:
            nodes = tuple(
                (node.name, node.bl_idname,
                 tuple((socket.identifier, _socket_value(getattr(socket, "default_value", None)))
                       for socket in node.inputs if hasattr(socket, "default_value")),
                 node.image.name if node.type == "TEX_IMAGE" and node.image is not None else None)
                for node in tree.nodes
            )
            links = tuple(sorted(
                (link.from_node.name, link.from_socket.identifier, link.to_node.name, link.to_socket.identifier)
                for link in tree.links
            ))
        rows.append((material.name, material.use_nodes, tuple(material.diffuse_color), material.metallic,
                     material.roughness, getattr(material, "surface_render_method", None), nodes, links))
    assignments = tuple(
        (obj.name, tuple(slot.material.name if slot.material is not None else None for slot in obj.material_slots))
        for obj in sorted(source_objects, key=lambda item: item.name)
    )
    return _digest((tuple(rows), assignments))


def render_signature() -> str:
    """Track output and color-management settings used by the real files."""
    rows: list[object] = []
    for scene in sorted(bpy.data.scenes, key=lambda item: item.name):
        render = scene.render
        image = render.image_settings
        view = scene.view_settings
        rows.append((scene.name, render.engine, render.filepath, render.resolution_x, render.resolution_y,
                     render.resolution_percentage, render.film_transparent, render.use_file_extension,
                     image.file_format, image.color_mode, image.color_depth, image.compression,
                     view.view_transform, view.look, view.exposure, view.gamma,
                     scene.camera.name if scene.camera is not None else None))
    return _digest(tuple(rows))


def authored_signature(source_objects: tuple[bpy.types.Object, ...]) -> tuple[str, str, str]:
    """Capture the authored scene areas registration and quick-view controls must preserve."""
    return geometry_signature(source_objects), material_signature(source_objects), render_signature()


def assert_registered(module: object) -> None:
    """Require every shared and appearance class plus one persistent load callback."""
    classes = module.SHARED_CLASSES + module.ADDON_CLASSES
    for cls in classes:
        if not cls.is_registered:
            raise AssertionError(f"Extension class is not registered: {cls.__name__}")
    if bpy.app.handlers.load_post.count(module.restore_panels_after_load) != 1:
        raise AssertionError("Extension load callback is missing or duplicated")


def assert_unregistered(module: object) -> None:
    """Require extension teardown to remove only its registered UI and callback."""
    for cls in module.SHARED_CLASSES + module.ADDON_CLASSES:
        if cls.is_registered:
            raise AssertionError(f"Extension class leaked after disable: {cls.__name__}")
    if module.restore_panels_after_load in bpy.app.handlers.load_post:
        raise AssertionError("Extension load callback leaked after disable")


def select_rig(rig: bpy.types.Object) -> None:
    """Set one actual character rig as the explicit active selection."""
    for obj in tuple(bpy.context.selected_objects):
        obj.select_set(False)
    rig.select_set(True)
    bpy.context.view_layer.objects.active = rig


def resolve_fixture(module: object, rig_name: str) -> tuple[bpy.types.Object, tuple[bpy.types.Object, ...]]:
    """Resolve the requested sample character and its source-backed geometry."""
    scene = bpy.context.scene
    rig = scene.objects.get(rig_name)
    if rig is None or rig.type != "ARMATURE":
        raise AssertionError(f"Real sample armature is missing: {rig_name}")
    select_rig(rig)
    resolved = module.character_appearance_panel.active_rig(bpy.context)
    if resolved != rig:
        raise AssertionError(f"Active appearance panel resolved the wrong rig: {resolved.name}")
    sources = module.character_appearance_panel.source_meshes(scene, rig)
    if not sources:
        raise AssertionError(f"Real sample has no supported source geometry: {rig.name}")
    return rig, sources


def _managed_drivers(shell: bpy.types.Object, rig: bpy.types.Object,
                     appearance: object) -> tuple[tuple[object, ...], ...]:
    """Return only the width and visibility drivers owned by this rig."""
    animation = shell.animation_data
    if animation is None:
        return ()
    owned_paths = {f'["{appearance.ENABLED}"]', f'["{appearance.WIDTH}"]'}
    rows = []
    for curve in animation.drivers:
        targets = tuple(target for variable in curve.driver.variables for target in variable.targets)
        if any(target.id == rig and target.data_path in owned_paths for target in targets):
            rows.append((curve.data_path, curve.array_index, curve.driver.expression,
                         tuple((variable.name, variable.targets[0].id.name if variable.targets[0].id else None,
                                variable.targets[0].data_path) for variable in curve.driver.variables)))
    return tuple(sorted(rows))


def _update_drivers(*changed_objects: bpy.types.Object) -> None:
    """Evaluate native Blender drivers after an ID property change."""
    for obj in changed_objects:
        obj.update_tag()
    bpy.context.view_layer.update()
    bpy.context.evaluated_depsgraph_get().update()


def _width_value(shell: bpy.types.Object, appearance: object) -> tuple[float, float]:
    modifier, identifier = appearance._width_input(shell)
    return float(modifier[identifier]), float(shell[appearance.BASE_WIDTH])


def _assert_outline_state(shells: tuple[bpy.types.Object, ...], sources: dict[str, bpy.types.Object],
                          enabled: bool) -> None:
    for shell in shells:
        source = sources[str(shell["outline_candidate_source"])]
        if shell.hide_viewport != (not enabled or source.hide_viewport):
            raise AssertionError(f"Viewport visibility driver is wrong: {shell.name}")
        if shell.hide_render != (not enabled or source.hide_render):
            raise AssertionError(f"Render visibility driver is wrong: {shell.name}")


def _exercise_potential_drivers(owner: bpy.types.Object) -> tuple[int, int]:
    """Toggle the existing sample boolean and verify its original driver targets react."""
    before_value = bool(owner["potential_appearance"])
    before = _driver_values_for(owner)
    if not before:
        raise AssertionError(f"Existing potential boolean has no native object drivers: {owner.name}")
    owner["potential_appearance"] = not before_value
    _update_drivers(owner)
    changed = _driver_values_for(owner)
    if changed.keys() != before.keys():
        raise AssertionError(f"Potential toggle changed the existing driver target set: {owner.name}")
    changed_count = sum(before[key] != changed[key] for key in before)
    if changed_count == 0:
        raise AssertionError(f"Potential boolean did not affect its existing driver targets: {owner.name}")
    owner["potential_appearance"] = before_value
    _update_drivers(owner)
    restored = _driver_values_for(owner)
    if restored != before:
        raise AssertionError(f"Potential boolean did not restore its original state: {owner.name}")
    return len(before), changed_count


def _driver_values_for(owner: bpy.types.Object) -> dict[str, object]:
    """Read every existing driven value whose variables target one appearance owner."""
    rows: dict[str, object] = {}
    for collection_name in ("objects", "materials", "node_groups", "scenes", "worlds", "collections", "meshes"):
        for id_block in getattr(bpy.data, collection_name):
            animation = getattr(id_block, "animation_data", None)
            if animation is None:
                continue
            for curve in animation.drivers:
                if not any(target.id == owner for variable in curve.driver.variables for target in variable.targets):
                    continue
                key = f"{collection_name}:{id_block.name}:{curve.data_path}:{curve.array_index}"
                value = id_block.path_resolve(curve.data_path)
                if hasattr(value, "__len__") and not isinstance(value, (str, bytes)):
                    value = value[curve.array_index]
                rows[key] = _socket_value(value)
    return rows


def bind_existing_outline_controls(module: object, rig: bpy.types.Object) -> dict[str, object]:
    """Adopt sample hulls, exercise their drivers, and prove repeated setup is idempotent."""
    appearance = module.character_appearance_panel
    scene = bpy.context.scene
    sources = appearance.source_meshes(scene, rig)
    source_by_name = {obj.name: obj for obj in sources}
    shells = appearance.owned_outlines(scene, sources)
    if not shells:
        raise AssertionError(f"Sample must provide real outline hulls to adopt: {rig.name}")
    initial_shell_names = tuple(sorted(shell.name for shell in shells))
    scene_before = authored_signature(sources)

    select_rig(rig)
    result = bpy.ops.endfield.prepare_outline_controls()
    if result != {"FINISHED"}:
        raise AssertionError(f"Installed setup operator failed for {rig.name}: {result}")
    if rig.get(appearance.CONTROL_MARKER) != appearance.CONTROL_FORMAT:
        raise AssertionError(f"Rig did not receive the native control marker: {rig.name}")
    if tuple(sorted(shell.name for shell in appearance.owned_outlines(scene, sources))) != initial_shell_names:
        raise AssertionError(f"Setup duplicated or replaced existing hulls: {rig.name}")
    shells = appearance.owned_outlines(scene, sources)
    if bool(rig[appearance.ENABLED]) is not True or float(rig[appearance.WIDTH]) != 1.0:
        raise AssertionError(f"New outline controls have incorrect initial values: {rig.name}")
    for shell in shells:
        managed = _managed_drivers(shell, rig, appearance)
        if len(managed) != 3:
            raise AssertionError(f"Expected one width and two visibility drivers: {shell.name}; {managed}")
        value, base = _width_value(shell, appearance)
        if abs(value - base) > 1e-6:
            raise AssertionError(f"Initial outline width differs from its authored base: {shell.name}: {value}/{base}")

    baseline_drivers = {shell.name: _managed_drivers(shell, rig, appearance) for shell in shells}
    rig[appearance.WIDTH] = 1.5
    _update_drivers(rig)
    for shell in shells:
        value, base = _width_value(shell, appearance)
        if abs(value - base * 1.5) > max(1e-6, abs(base) * 1e-5):
            width_curve = next((curve for curve in shell.animation_data.drivers
                                if curve.driver.expression.endswith("* width")), None)
            modifier, identifier = appearance._width_input(shell)
            evaluated_shell = shell.evaluated_get(bpy.context.evaluated_depsgraph_get())
            evaluated_modifier = evaluated_shell.modifiers.get(modifier.name)
            evaluated_value = evaluated_modifier[identifier] if evaluated_modifier is not None else None
            raise AssertionError(
                f"Width multiplier did not reach the hull modifier: {shell.name}: {value}/{base}; "
                f"rig={rig[appearance.WIDTH]}, driver={width_curve.driver.expression if width_curve else None}, "
                f"driver_valid={width_curve.driver.is_valid if width_curve else None}, evaluated={evaluated_value}"
            )
    rig[appearance.WIDTH] = 1.0
    _update_drivers(rig)
    for shell in shells:
        value, base = _width_value(shell, appearance)
        if abs(value - base) > 1e-6:
            raise AssertionError(f"Width reset did not restore its authored base: {shell.name}")

    source = source_by_name[str(shells[0]["outline_candidate_source"])]
    source_visibility = (source.hide_viewport, source.hide_render)
    source.hide_viewport = True
    source.hide_render = True
    _update_drivers(source)
    if not shells[0].hide_viewport or not shells[0].hide_render:
        raise AssertionError(f"Outline did not respect its source visibility: {source.name}")
    source.hide_viewport, source.hide_render = source_visibility
    _update_drivers(source)

    rig[appearance.ENABLED] = False
    _update_drivers(rig)
    _assert_outline_state(shells, source_by_name, False)
    rig[appearance.ENABLED] = True
    _update_drivers(rig)
    _assert_outline_state(shells, source_by_name, True)

    select_rig(rig)
    result = bpy.ops.endfield.prepare_outline_controls()
    if result != {"FINISHED"}:
        raise AssertionError(f"Repeated setup failed for {rig.name}: {result}")
    if tuple(sorted(shell.name for shell in appearance.owned_outlines(scene, sources))) != initial_shell_names:
        raise AssertionError(f"Repeated setup duplicated hulls: {rig.name}")
    for shell in shells:
        if _managed_drivers(shell, rig, appearance) != baseline_drivers[shell.name]:
            raise AssertionError(f"Repeated setup duplicated or changed owned drivers: {shell.name}")
    if authored_signature(sources) != scene_before:
        raise AssertionError(f"Outline setup changed sample geometry or materials: {rig.name}")
    return {"rig": rig.name, "source_meshes": len(sources), "adopted_hulls": len(shells),
            "idempotent_setup": True, "width_driver": True, "source_visibility": True}


def _view_areas() -> tuple[tuple[bpy.types.Screen, bpy.types.Area], ...]:
    """Find actual saved VIEW_3D areas available to the background context."""
    return tuple((screen, area) for screen in bpy.data.screens for area in screen.areas if area.type == "VIEW_3D")


def exercise_quick_views(source_objects: tuple[bpy.types.Object, ...]) -> bool:
    """Run quick view operators only when Blender exposes a real VIEW_3D area."""
    areas = _view_areas()
    if not areas:
        return False
    before = authored_signature(source_objects)
    screen, area = areas[0]
    region = next((item for item in area.regions if item.type == "WINDOW"), None)
    override = {"screen": screen, "area": area}
    if region is not None:
        override["region"] = region
    with bpy.context.temp_override(**override):
        if bpy.ops.endfield.texture_edit_view() != {"FINISHED"}:
            raise AssertionError("Quick texture view operator did not finish in VIEW_3D")
        if area.spaces.active.shading.type != "SOLID" or area.spaces.active.shading.color_type != "TEXTURE":
            raise AssertionError("Quick texture view did not select solid textured shading")
        if bpy.ops.endfield.material_check_view() != {"FINISHED"}:
            raise AssertionError("Material preview operator did not finish in VIEW_3D")
        if area.spaces.active.shading.type != "MATERIAL":
            raise AssertionError("Material preview operator did not restore material shading")
    if authored_signature(source_objects) != before:
        raise AssertionError("Quick view operators changed geometry, materials or render settings")
    return True


def append_second_character(sample: Path, module: object, rig_name: str) -> tuple[bpy.types.Object, tuple[bpy.types.Object, ...]]:
    """Append only one second real character and link its native objects into this scene."""
    owner_name = "EF Character Appearance"
    prefixes = ("S_actor_laevat_", "EF Outline Approx S_actor_laevat_")
    with bpy.data.libraries.load(str(sample.resolve()), link=False) as (data_from, data_to):
        available = tuple(data_from.objects)
        selected = tuple(name for name in available
                         if name == rig_name or name == owner_name or name.startswith(prefixes))
        if rig_name not in selected or owner_name not in selected:
            raise AssertionError("Second sample does not expose the expected rig and appearance owner")
        data_to.objects = list(selected)
    appended = tuple(obj for obj in data_to.objects if obj is not None)
    for obj in appended:
        if obj.name not in bpy.context.scene.objects:
            bpy.context.scene.collection.objects.link(obj)
    rig = bpy.context.scene.objects.get(rig_name)
    if rig is None or rig.type != "ARMATURE":
        raise AssertionError(f"Appended second character rig is missing: {rig_name}")
    select_rig(rig)
    appearance = module.character_appearance_panel
    resolved = appearance.active_rig(bpy.context)
    if resolved != rig:
        raise AssertionError(f"Appended character resolved incorrectly: {resolved.name}")
    sources = appearance.source_meshes(bpy.context.scene, rig)
    if len(sources) < 40:
        raise AssertionError(f"Appended character source geometry is incomplete: {len(sources)}")
    return rig, sources


def exercise_two_character_isolation(module: object, first_rig: bpy.types.Object,
                                     second_rig: bpy.types.Object) -> dict[str, object]:
    """Prove both appearance controls stay within the selected character ownership."""
    appearance = module.character_appearance_panel
    scene = bpy.context.scene
    first_sources = appearance.source_meshes(scene, first_rig)
    second_sources = appearance.source_meshes(scene, second_rig)
    first_shells = appearance.owned_outlines(scene, first_sources)
    second_shells = appearance.owned_outlines(scene, second_sources)
    if not first_shells or not second_shells:
        raise AssertionError("Both real characters must retain their own existing hulls")
    select_rig(first_rig)
    if bpy.ops.endfield.prepare_outline_controls() != {"FINISHED"}:
        raise AssertionError("First real character setup failed after append")
    select_rig(second_rig)
    if bpy.ops.endfield.prepare_outline_controls() != {"FINISHED"}:
        raise AssertionError("Second real character setup failed after append")
    first_shells = appearance.owned_outlines(scene, first_sources)
    second_shells = appearance.owned_outlines(scene, second_sources)
    for shell in first_shells:
        if shell.get(appearance.OWNER) != first_rig.name:
            raise AssertionError(f"First hull was assigned to the wrong rig: {shell.name}")
    for shell in second_shells:
        if shell.get(appearance.OWNER) != second_rig.name:
            raise AssertionError(f"Second hull was assigned to the wrong rig: {shell.name}")

    first_state = {shell.name: (shell.hide_viewport, shell.hide_render) for shell in first_shells}
    second_state = {shell.name: (shell.hide_viewport, shell.hide_render) for shell in second_shells}
    first_rig[appearance.ENABLED] = False
    _update_drivers(first_rig)
    _assert_outline_state(first_shells, {obj.name: obj for obj in first_sources}, False)
    if {shell.name: (shell.hide_viewport, shell.hide_render) for shell in second_shells} != second_state:
        raise AssertionError("First character outline toggle changed the second character")
    first_rig[appearance.ENABLED] = True
    _update_drivers(first_rig)
    if {shell.name: (shell.hide_viewport, shell.hide_render) for shell in first_shells} != first_state:
        raise AssertionError("First character outline state did not restore")

    second_rig[appearance.ENABLED] = False
    _update_drivers(second_rig)
    _assert_outline_state(second_shells, {obj.name: obj for obj in second_sources}, False)
    if {shell.name: (shell.hide_viewport, shell.hide_render) for shell in first_shells} != first_state:
        raise AssertionError("Second character outline toggle changed the first character")
    second_rig[appearance.ENABLED] = True
    _update_drivers(second_rig)

    first_owners = appearance.potential_owners(scene, first_rig)
    second_owners = appearance.potential_owners(scene, second_rig)
    if len(first_owners) != 1 or len(second_owners) != 1 or first_owners[0] == second_owners[0]:
        raise AssertionError("Potential controls did not resolve to one distinct existing owner per character")
    first_before = _driver_values_for(first_owners[0])
    second_before = _driver_values_for(second_owners[0])
    first_driver_count, first_changed_count = _exercise_potential_drivers(first_owners[0])
    if _driver_values_for(second_owners[0]) != second_before:
        raise AssertionError("First potential toggle changed the second character's existing drivers")
    if _driver_values_for(first_owners[0]) != first_before:
        raise AssertionError("First potential toggle did not restore its original driver state")
    return {"first_rig": first_rig.name, "second_rig": second_rig.name,
            "first_hulls": len(first_shells), "second_hulls": len(second_shells),
            "outline_toggles_isolated": True,
            "first_potential_drivers": first_driver_count, "first_potential_changed": first_changed_count,
            "second_potential_drivers_untouched": len(second_before), "potential_controls_isolated": True}


def _assert_archive(archive_path: Path) -> None:
    """Check the tested archive includes the exact appearance control closure."""
    with zipfile.ZipFile(archive_path) as archive:
        if archive.testzip() is not None:
            raise AssertionError("Addon ZIP failed its CRC check")
        names = frozenset(archive.namelist())
        for required in ("character_appearance_panel.py", "character_outline.py"):
            if required not in names:
                raise AssertionError(f"Built addon is missing {required}")


def main(arguments: list[str]) -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--archive", type=Path, required=True)
    parser.add_argument("--first-sample", type=Path, required=True)
    parser.add_argument("--second-sample", type=Path, required=True)
    parser.add_argument("--output-dir", type=Path, required=True)
    args = parser.parse_args(arguments)
    output = args.output_dir.resolve()
    output.mkdir(parents=True, exist_ok=True)
    bpy.context.preferences.filepaths.save_version = 0
    bpy.context.preferences.filepaths.use_scripts_auto_execute = False
    _assert_archive(args.archive)

    bpy.ops.wm.open_mainfile(filepath=str(args.first_sample.resolve()), use_scripts=False)
    first_baseline_sources = tuple(obj for obj in bpy.context.scene.objects
                                   if obj.type == "MESH" and obj.get("source_renderer_pipeline_json") is not None
                                   and obj.get("outline_candidate_contract") is None)
    first_baseline = authored_signature(first_baseline_sources)
    repository = output / f"extensions_{os.getpid()}"
    repository.mkdir(exist_ok=True)
    repo = bpy.context.preferences.extensions.repos.new(
        name="Endfield appearance integration test", module="endfield_controls_test",
        custom_directory=str(repository), remote_url="")
    module_name = "bl_ext.endfield_controls_test.endfield_character_tools"
    installed = bpy.ops.extensions.package_install_files(
        repo=repo.module, filepath=str(args.archive.resolve()), enable_on_install=True)
    if installed != {"FINISHED"}:
        raise AssertionError(f"Offline Blender extension installation failed: {installed}")
    module = importlib.import_module(module_name)
    assert_registered(module)
    if authored_signature(first_baseline_sources) != first_baseline:
        raise AssertionError("Extension registration changed real sample geometry, materials or render settings")

    first_rig, first_sources = resolve_fixture(module, "chr_0036_jsspsi_postmodel")
    first_result = bind_existing_outline_controls(module, first_rig)
    quick_views_available = exercise_quick_views(first_sources)
    appearance = module.character_appearance_panel
    old_hulls = appearance.owned_outlines(bpy.context.scene, first_sources)
    expected_sources = {shell['outline_candidate_source'] for shell in old_hulls}
    for shell in old_hulls:
        bpy.data.objects.remove(shell, do_unlink=True)
    bpy.context.view_layer.objects.active = first_rig
    if bpy.ops.endfield.prepare_outline_controls() != {'FINISHED'}:
        raise AssertionError('Outline creation failed on the real sample without existing hulls')
    generated_hulls = appearance.owned_outlines(bpy.context.scene, first_sources)
    if {shell['outline_candidate_source'] for shell in generated_hulls} != expected_sources:
        raise AssertionError('New hull creation changed the source-supported renderer selection')
    generated_hull_count = len(generated_hulls)
    first_potential_owner = module.character_appearance_panel.potential_owners(
        bpy.context.scene, first_rig)
    if len(first_potential_owner) != 1:
        raise AssertionError("First sample's existing potential control owner was not resolved")
    first_potential = _exercise_potential_drivers(first_potential_owner[0])

    bpy.ops.wm.open_mainfile(filepath=str(args.second_sample.resolve()), use_scripts=False)
    assert_registered(module)
    second_rig, second_sources = resolve_fixture(module, "chr_0016_laevat_postmodel")
    second_result = bind_existing_outline_controls(module, second_rig)
    second_owner = module.character_appearance_panel.potential_owners(bpy.context.scene, second_rig)
    if len(second_owner) != 1:
        raise AssertionError("Second sample's existing potential control owner was not resolved")
    second_potential = _exercise_potential_drivers(second_owner[0])

    bpy.ops.wm.open_mainfile(filepath=str(args.first_sample.resolve()), use_scripts=False)
    assert_registered(module)
    first_rig, first_sources = resolve_fixture(module, "chr_0036_jsspsi_postmodel")
    first_result_after_reload = bind_existing_outline_controls(module, first_rig)
    second_rig, appended_sources = append_second_character(
        args.second_sample, module, "chr_0016_laevat_postmodel")
    isolation = exercise_two_character_isolation(module, first_rig, second_rig)

    copy_path = output / "appearance_controls_native_drivers.blend"
    bpy.ops.wm.save_as_mainfile(filepath=str(copy_path), compress=True)
    bpy.ops.preferences.addon_disable(module=module_name)
    assert_unregistered(module)
    bpy.ops.wm.open_mainfile(filepath=str(copy_path), use_scripts=False)
    assert_unregistered(module)
    if bpy.context.preferences.filepaths.use_scripts_auto_execute:
        raise AssertionError("Opening the saved test copy enabled automatic script execution")
    native_first, native_first_sources = resolve_fixture(module, "chr_0036_jsspsi_postmodel")
    appearance = module.character_appearance_panel
    if native_first.get(appearance.CONTROL_MARKER) != appearance.CONTROL_FORMAT:
        raise AssertionError("Saved test copy lost its native outline controls")
    native_shells = appearance.owned_outlines(bpy.context.scene, native_first_sources)
    native_shell = native_shells[0]
    native_modifier, native_identifier = appearance._width_input(native_shell)
    base_width = float(native_shell[appearance.BASE_WIDTH])
    native_first[appearance.WIDTH] = 1.5
    _update_drivers(native_first)
    if abs(float(native_modifier[native_identifier]) - base_width * 1.5) > max(1e-6, base_width * 1e-5):
        raise AssertionError("Saved native width driver stopped working without the addon")
    native_first[appearance.WIDTH] = 1.0
    native_first[appearance.ENABLED] = False
    _update_drivers(native_first)
    if not all(shell.hide_viewport and shell.hide_render for shell in native_shells):
        raise AssertionError("Saved native visibility drivers stopped working without the addon")
    native_first[appearance.ENABLED] = True
    _update_drivers(native_first)
    native_owners = appearance.potential_owners(bpy.context.scene, native_first)
    if len(native_owners) != 1 or not _driver_values_for(native_owners[0]):
        raise AssertionError("Saved sample lost its pre-existing potential appearance drivers")
    native_potential = _exercise_potential_drivers(native_owners[0])

    report = {
        "status": "passed",
        "blender": bpy.app.version_string,
        "archive": str(args.archive.resolve()),
        "real_offline_install": True,
        "registration_preserved_scene": True,
        "fresh_outline_generation": {"passed": True, "hulls": generated_hull_count},
        "first_sample": first_result,
        "second_sample": second_result,
        "first_after_file_switch": first_result_after_reload,
        "quick_view_operators": "passed" if quick_views_available else "unavailable_background_VIEW_3D",
        "first_potential_drivers": {"targets": first_potential[0], "changed": first_potential[1]},
        "second_potential_drivers": {"targets": second_potential[0], "changed": second_potential[1]},
        "two_character_isolation": isolation,
        "native_drivers_without_addon": {"width": True, "outline_visibility": True,
                                         "potential_targets": native_potential[0],
                                         "potential_changed": native_potential[1]},
        "copy": str(copy_path),
        "automatic_scripts_disabled": True,
    }
    (output / "verification.json").write_text(
        json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(report, ensure_ascii=False))


if __name__ == "__main__":
    main(sys.argv[sys.argv.index("--") + 1:])
