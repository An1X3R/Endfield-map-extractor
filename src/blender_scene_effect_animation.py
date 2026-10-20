"""Bake existing particle mesh samples into sibling Blender/Alembic deliveries."""
from __future__ import annotations

import shutil
from array import array
from collections.abc import Callable, Sequence
from pathlib import Path
from typing import NamedTuple
from uuid import uuid4

import bpy


class EffectMeshSampler(NamedTuple):
    object: bpy.types.Object
    sample: Callable[[float], bpy.types.Mesh]


_STREAMS = ("EF_FX_COLOR", "EF_FX_CUSTOM1", "EF_FX_CUSTOM2")
_ALIVE_UV = 'EF_FX_CACHE_ALIVE'


def _require_single_material(mesh: bpy.types.Mesh) -> None:
    """Changing particle topology carries one material, never static face sets."""
    if len(mesh.materials) > 1 or any(face.material_index != 0 for face in mesh.polygons):
        raise NotImplementedError(f"Effect cache requires one material per changing-topology mesh: mesh={mesh.name}")


def _encode_streams(mesh: bpy.types.Mesh, streams: tuple[str, ...]) -> None:
    """Use float UV pairs because Blender 4.4 Alembic exports byte colors only."""
    if len(mesh.uv_layers) + len(streams) * 2 + 1 > 8:
        raise ValueError(f"Animated effect exceeds Blender UV layer limit: mesh={mesh.name}")
    unexpected = [name for name in _STREAMS if name in mesh.color_attributes and name not in streams]
    if unexpected:
        raise ValueError(f"Animated effect added undeclared float streams: mesh={mesh.name}, streams={unexpected}")
    empty = len(mesh.loops) == 0
    if empty:
        if len(mesh.vertices):
            raise ValueError(f"Animated effect contains unsupported loose vertices: mesh={mesh.name}")
        # Alembic skips empty sideband samples. A removable zero-area face keeps time aligned.
        mesh.from_pydata([(0.0, 0.0, 0.0)] * 4, [], [(0, 1, 2, 3)])
    for name in streams:
        attribute = mesh.color_attributes.get(name)
        if attribute is None and not empty:
            raise ValueError(f"Animated effect stream disappeared: mesh={mesh.name}, stream={name}")
        values = array('f', [0.0]) * (len(mesh.loops) * 4)
        if attribute is not None:
            if attribute.data_type != 'FLOAT_COLOR' or attribute.domain != 'CORNER':
                raise ValueError(f"Animated effect stream type invalid: mesh={mesh.name}, stream={name}")
            attribute.data.foreach_get('color', values)
        for suffix, offset in (('_RG', 0), ('_BA', 2)):
            layer = mesh.uv_layers.new(name=name + suffix)
            packed = array('f', (values[index + component]
                                for index in range(0, len(values), 4)
                                for component in (offset, offset + 1)))
            layer.data.foreach_set('uv', packed)
    alive = mesh.uv_layers.new(name=_ALIVE_UV)
    alive.data.foreach_set('uv', [component for _ in mesh.loops
                                  for component in (0.0 if empty else 1.0, 0.0)])
    mesh.uv_layers.active_index = 0
    mesh.update()


def _restore_streams(obj: bpy.types.Object, streams: tuple[str, ...]) -> None:
    tree = bpy.data.node_groups.new('EF Animated Float Streams', 'GeometryNodeTree')
    tree.interface.new_socket(name='Geometry', in_out='INPUT', socket_type='NodeSocketGeometry')
    tree.interface.new_socket(name='Geometry', in_out='OUTPUT', socket_type='NodeSocketGeometry')
    input_node = tree.nodes.new('NodeGroupInput')
    input_node.location = (-780, 200)
    alive = tree.nodes.new('GeometryNodeInputNamedAttribute')
    alive.data_type = 'FLOAT_VECTOR'
    alive.inputs['Name'].default_value = _ALIVE_UV
    alive.location = (-780, -20)
    separate = tree.nodes.new('ShaderNodeSeparateXYZ')
    separate.location = (-570, -20)
    tree.links.new(alive.outputs['Attribute'], separate.inputs[0])
    missing = tree.nodes.new('ShaderNodeMath')
    missing.operation = 'LESS_THAN'
    missing.inputs[1].default_value = 0.5
    missing.location = (-370, -20)
    tree.links.new(separate.outputs['X'], missing.inputs[0])
    delete = tree.nodes.new('GeometryNodeDeleteGeometry')
    delete.domain = 'FACE'
    delete.mode = 'ALL'
    delete.label = 'Remove empty-frame cache carrier; retain zero visible geometry'
    delete.location = (-170, 200)
    tree.links.new(input_node.outputs['Geometry'], delete.inputs['Geometry'])
    tree.links.new(missing.outputs[0], delete.inputs['Selection'])
    previous = delete.outputs['Geometry']
    for index, name in enumerate(streams):
        frame = tree.nodes.new('NodeFrame')
        frame.label = name + ' | Lossless signed float cache channels'
        frame.location = (index * 800, 0)
        combine = tree.nodes.new('FunctionNodeCombineColor')
        combine.mode = 'RGB'
        combine.parent = frame
        combine.location = (390, 0)
        for pair, (suffix, offset) in enumerate((('_RG', 0), ('_BA', 2))):
            attribute = tree.nodes.new('GeometryNodeInputNamedAttribute')
            attribute.data_type = 'FLOAT_VECTOR'
            attribute.inputs['Name'].default_value = name + suffix
            attribute.parent = frame
            attribute.location = (0, -pair * 220)
            separate = tree.nodes.new('ShaderNodeSeparateXYZ')
            separate.parent = frame
            separate.location = (210, -pair * 220)
            tree.links.new(attribute.outputs['Attribute'], separate.inputs[0])
            tree.links.new(separate.outputs['X'], combine.inputs[offset])
            tree.links.new(separate.outputs['Y'], combine.inputs[offset + 1])
        store = tree.nodes.new('GeometryNodeStoreNamedAttribute')
        store.data_type = 'FLOAT_COLOR'
        store.domain = 'CORNER'
        store.inputs['Name'].default_value = name
        store.parent = frame
        store.location = (590, 0)
        tree.links.new(previous, store.inputs['Geometry'])
        tree.links.new(combine.outputs[0], store.inputs['Value'])
        previous = store.outputs['Geometry']
    output = tree.nodes.new('NodeGroupOutput')
    output.location = (len(streams) * 800, 200)
    tree.links.new(previous, output.inputs[0])
    modifier = obj.modifiers.new('EF Restore Animated Float Streams', 'NODES')
    modifier.node_group = tree


def _copy_sample_geometry(target: bpy.types.Mesh, source: bpy.types.Mesh) -> None:
    """Keep the mesh identity stable while Blender samples its dependency graph."""
    target.clear_geometry()
    for layer in list(target.uv_layers):
        target.uv_layers.remove(layer)
    for attribute in list(target.color_attributes):
        target.color_attributes.remove(attribute)
    target.from_pydata([tuple(vertex.co) for vertex in source.vertices], [],
                      [tuple(polygon.vertices) for polygon in source.polygons])
    for layer in source.uv_layers:
        result = target.uv_layers.new(name=layer.name)
        values = array('f', [0.0]) * (len(source.loops) * 2)
        layer.data.foreach_get('uv', values)
        result.data.foreach_set('uv', values)
    target.uv_layers.active_index = 0
    target.update()


def bake_effect_geometry_sequence(samplers: Sequence[EffectMeshSampler], cache_path: Path,
                                  frame_start: int, frame_end: int,
                                  max_vertices_per_sample: int) -> None:
    """Bake samples through disposable objects and attach a complete native cache."""
    if not samplers or frame_start < 1 or frame_end < frame_start or frame_end - frame_start >= 240:
        raise ValueError('Effect cache needs objects and a bounded interval of at most 240 frames')
    if max_vertices_per_sample <= 0 or not cache_path.is_absolute() or cache_path.exists():
        raise ValueError(f"Effect cache requires a positive vertex limit and unused absolute path: path={cache_path}")
    if not bpy.app.build_options.alembic:
        raise RuntimeError('This Blender build does not support Alembic effect playback')
    if len({sampler.object.as_pointer() for sampler in samplers}) != len(samplers):
        raise ValueError('Effect cache contains duplicate source objects')
    for index, sampler in enumerate(samplers):
        obj = sampler.object
        if obj.type != 'MESH' or obj.modifiers or obj.constraints:
            raise ValueError(f"Effect cache requires an unmodified source mesh: object={obj.name}")
        _require_single_material(obj.data)
        if bpy.data.objects.get(f'EFCACHE_{index:04d}') is not None:
            raise ValueError(f"Effect cache temporary object name is occupied: index={index}")
    scene = bpy.context.scene
    fps = scene.render.fps / scene.render.fps_base
    original_frame = scene.frame_current
    original_subframe = scene.frame_subframe
    selected = tuple(bpy.context.selected_objects)
    active = bpy.context.view_layer.objects.active
    stream_names = tuple(tuple(name for name in _STREAMS if name in sampler.object.data.color_attributes)
                         for sampler in samplers)
    errors: list[Exception] = []
    temporary: list[bpy.types.Object] = []
    imported: list[bpy.types.Object] = []
    original_actions = set(bpy.data.actions)
    original_groups = set(bpy.data.node_groups)
    original_caches = set(bpy.data.cache_files)
    property_names = ('endfield_fx_animation_scope', 'endfield_fx_animation_frames',
                      'endfield_fx_animation_fps', 'endfield_fx_animation_interpolation')
    original_properties = tuple({name: sampler.object[name] for name in property_names
                                 if name in sampler.object} for sampler in samplers)
    temporary_path = cache_path.with_name(f'.{cache_path.stem}.{uuid4().hex}.partial.abc')
    final_created = False
    completed = False

    def update(current_scene: bpy.types.Scene) -> None:
        if errors:
            return
        try:
            sample_frame = current_scene.frame_current + current_scene.frame_subframe
            for index, sampler in enumerate(samplers):
                mesh = sampler.sample(sample_frame / fps)
                if not isinstance(mesh, bpy.types.Mesh):
                    raise TypeError(f"Effect sampler did not return a Blender mesh: object={sampler.object.name}, frame={sample_frame}")
                if mesh == sampler.object.data or mesh == temporary[index].data:
                    raise ValueError(f"Effect sampler must return a new mesh: object={sampler.object.name}")
                try:
                    _require_single_material(mesh)
                    if len(mesh.vertices) > max_vertices_per_sample:
                        raise ValueError(f"Effect sample vertex limit exceeded: object={sampler.object.name}, frame={sample_frame}, vertices={len(mesh.vertices)}")
                    _encode_streams(mesh, stream_names[index])
                    _copy_sample_geometry(temporary[index].data, mesh)
                finally:
                    if mesh.users == 0:
                        bpy.data.meshes.remove(mesh)
        except Exception as error:
            # Blender swallows callback errors; re-raise the original after export.
            errors.append(error)

    cache_path.parent.mkdir(parents=True, exist_ok=True)
    try:
        bpy.ops.object.select_all(action='DESELECT')
        for index, sampler in enumerate(samplers):
            obj = sampler.object.copy()
            obj.data = sampler.object.data.copy()
            obj.animation_data_clear()
            obj.name = f'EFCACHE_{index:04d}'
            obj.data.name = f'EFCACHE_{index:04d}_Geometry'
            scene.collection.objects.link(obj)
            temporary.append(obj)
            obj.select_set(True)
            marker = obj.modifiers.new('EF Build Only Sampling Marker', 'DISPLACE')
            marker.strength = 0
            marker.keyframe_insert(data_path='strength', frame=frame_start)
            marker.keyframe_insert(data_path='strength', frame=frame_end)
        bpy.app.handlers.frame_change_pre.append(update)
        scene.frame_set(frame_start)
        if errors:
            raise errors[0]
        bpy.ops.wm.alembic_export(filepath=str(temporary_path), start=frame_start, end=frame_end,
                                 selected=True, vcolors=False, uvs=True, face_sets=False,
                                 export_particles=False, export_hair=False, as_background_job=False)
        if errors:
            raise errors[0]
        if update in bpy.app.handlers.frame_change_pre:
            bpy.app.handlers.frame_change_pre.remove(update)
        for obj in temporary:
            mesh = obj.data
            bpy.data.objects.remove(obj, do_unlink=True)
            if mesh.users == 0:
                bpy.data.meshes.remove(mesh)
        temporary.clear()
        bpy.ops.object.select_all(action='DESELECT')
        before_import = set(bpy.data.objects)
        try:
            bpy.ops.wm.alembic_import(filepath=str(temporary_path), as_background_job=False,
                                      set_frame_range=False, always_add_cache_reader=True)
        finally:
            imported = list(set(bpy.data.objects) - before_import)
        by_name = {obj.name: obj for obj in imported}
        sources: list[bpy.types.Modifier] = []
        for index in range(len(samplers)):
            source = by_name.get(f'EFCACHE_{index:04d}')
            if source is None:
                raise RuntimeError(f"Effect cache object missing after import: index={index}, path={temporary_path}")
            source_modifier = next((modifier for modifier in source.modifiers
                                    if modifier.type == 'MESH_SEQUENCE_CACHE'), None)
            if source_modifier is None:
                raise RuntimeError(f"Effect cache is not animated: object={source.name}, path={temporary_path}")
            sources.append(source_modifier)
        for index, sampler in enumerate(samplers):
            modifier = sampler.object.modifiers.new('EF Baked Particle Motion', 'MESH_SEQUENCE_CACHE')
            modifier.cache_file = sources[index].cache_file
            modifier.object_path = sources[index].object_path
            modifier.read_data = {'VERT', 'POLY', 'UV', 'COLOR'}
            # Equal vertex counts do not imply equal particle identities after births/deaths.
            modifier.use_vertex_interpolation = False
            _restore_streams(sampler.object, stream_names[index])
            sampler.object['endfield_fx_animation_scope'] = 'baked_source_subset_fixed_reference_camera'
            sampler.object['endfield_fx_animation_frames'] = [frame_start, frame_end]
            sampler.object['endfield_fx_animation_fps'] = fps
            sampler.object['endfield_fx_animation_interpolation'] = 'sample_hold_no_cross_particle_morph'
        if cache_path.exists():
            raise FileExistsError(f"Effect cache destination appeared during bake: path={cache_path}")
        with cache_path.open('xb') as destination:
            final_created = True
            with temporary_path.open('rb') as source:
                shutil.copyfileobj(source, destination)
        for modifier in sources:
            modifier.cache_file.filepath = str(cache_path)
        scene.frame_set(original_frame, subframe=original_subframe)
        completed = True
    except Exception:
        for sampler, properties in zip(samplers, original_properties):
            for modifier in list(sampler.object.modifiers):
                sampler.object.modifiers.remove(modifier)
            for name in property_names:
                if name in properties:
                    sampler.object[name] = properties[name]
                elif name in sampler.object:
                    del sampler.object[name]
        raise
    finally:
        if update in bpy.app.handlers.frame_change_pre:
            bpy.app.handlers.frame_change_pre.remove(update)
        for obj in (*temporary, *imported):
            mesh = obj.data
            bpy.data.objects.remove(obj, do_unlink=True)
            if isinstance(mesh, bpy.types.Mesh) and mesh.users == 0:
                bpy.data.meshes.remove(mesh)
        for group in set(bpy.data.node_groups) - original_groups:
            if group.users == 0:
                bpy.data.node_groups.remove(group)
        for cache in set(bpy.data.cache_files) - original_caches:
            if not completed:
                cache.filepath = ''
            if cache.users == 0:
                bpy.data.batch_remove(ids=[cache])
        for action in set(bpy.data.actions) - original_actions:
            if action.users == 0:
                bpy.data.actions.remove(action)
        bpy.context.view_layer.update()
        if temporary_path.exists():
            temporary_path.unlink()
        if final_created and not completed:
            cache_path.unlink()
        bpy.ops.object.select_all(action='DESELECT')
        for obj in selected:
            obj.select_set(True)
        bpy.context.view_layer.objects.active = active
        scene.frame_set(original_frame, subframe=original_subframe)


def save_effect_cache_delivery(cache_path: Path, blend_path: Path) -> None:
    """Save a portable sibling blend copy without changing the active scene."""
    if not cache_path.is_absolute() or not blend_path.is_absolute() or cache_path.parent != blend_path.parent:
        raise ValueError(f"Effect delivery requires absolute sibling paths: cache={cache_path}, blend={blend_path}")
    if not cache_path.is_file() or blend_path.exists():
        raise FileExistsError(f"Effect delivery needs an existing cache and unused blend path: cache={cache_path}, blend={blend_path}")
    unpacked = tuple(image.name for image in bpy.data.images
                     if image.source == 'FILE' and image.users and not image.packed_file)
    if unpacked:
        raise ValueError(f"Effect delivery requires packed source images: images={unpacked}")
    used = tuple(cache for cache in bpy.data.cache_files if cache.users > 0)
    matches = tuple(cache for cache in used
                    if Path(bpy.path.abspath(cache.filepath)).resolve() == cache_path.resolve())
    if len(used) != 1 or len(matches) != 1:
        raise ValueError(f"Effect delivery requires exactly one used cache, matching the requested path: cache={cache_path}, used={[cache.filepath for cache in used]}")
    cache = matches[0]
    original_path = cache.filepath
    try:
        cache.filepath = (bpy.path.relpath(str(cache_path), start=str(Path(bpy.data.filepath).parent))
                          if bpy.data.filepath else str(cache_path))
        bpy.ops.wm.save_as_mainfile(filepath=str(blend_path), relative_remap=True, copy=True)
        if not blend_path.is_file():
            raise FileNotFoundError(f"Effect delivery did not produce a blend file: blend={blend_path}")
    except Exception as error:
        if blend_path.exists():
            try:
                blend_path.unlink()
            except OSError as cleanup_error:
                error.add_note(f"Effect delivery could not remove failed output {blend_path}: {cleanup_error}")
        raise
    finally:
        cache.filepath = original_path


def save_effect_material_delivery(blend_path: Path) -> None:
    """Save material-only playback without introducing an empty geometry cache."""
    if not blend_path.is_absolute():
        raise ValueError(f"Material animation requires an absolute path: blend={blend_path}")
    if blend_path.exists():
        raise FileExistsError(f"Material animation output already exists: blend={blend_path}")
    unpacked = tuple(image.name for image in bpy.data.images
                     if image.source == 'FILE' and image.users and not image.packed_file)
    caches = tuple(cache.filepath for cache in bpy.data.cache_files if cache.users > 0)
    if unpacked or caches:
        raise ValueError(f"Material animation has external dependencies: images={unpacked}, caches={caches}")
    bpy.ops.wm.save_as_mainfile(filepath=str(blend_path), relative_remap=True, copy=True)
    if not blend_path.is_file():
        raise FileNotFoundError(f"Material animation did not produce a blend file: blend={blend_path}")


def animate_effect_material_time(material: bpy.types.Material, scene: bpy.types.Scene) -> None:
    """Drive verified VFX UV scroll with Blender seconds and explicit proxy scope."""
    if material.node_tree is None:
        raise ValueError(f"Effect material has no shader nodes: material={material.name}")
    node = material.node_tree.nodes.get('EF FX Source Time Seconds')
    if node is None:
        raise ValueError(f"Effect material must be rebuilt with its source time input: material={material.name}")
    driver = node.outputs[0].driver_add('default_value').driver
    driver.expression = 'frame * fps_base / fps'
    for variable in tuple(driver.variables):
        driver.variables.remove(variable)
    for name, path in (('fps', 'render.fps'), ('fps_base', 'render.fps_base')):
        variable = driver.variables.new()
        variable.name = name
        variable.targets[0].id_type = 'SCENE'
        variable.targets[0].id = scene
        variable.targets[0].data_path = path
    material['endfield_fx_time_scope'] = 'blender_seconds_game_global_phase_unavailable'
