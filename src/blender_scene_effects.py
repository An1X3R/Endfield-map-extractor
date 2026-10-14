"""Realize bounded SceneProbe particle snapshots in an authored Blender scene."""
from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
import json
import math
from pathlib import Path
import struct

import bpy
from mathutils import Matrix, Quaternion, Vector

from blender_build_map02_chunk_20260727 import import_obj, unity_matrix
from blender_scene_effect_vertex_offset import apply_material_vertex_offset, UnsupportedVertexOffset
from endfield_scene_material_contract import resolve_export_path
from extractor_core.particle_curves import UnsupportedParticleCurve, parse_minmax_curve
from extractor_core.particle_snapshot import (
    ParticleSnapshotError, SnapshotParticle, UnsupportedParticleSnapshot,
    generate_particle_snapshot,
)


MaterialFactory = Callable[[Mapping[str, object], Mapping[str, object], Mapping[str, object]], bpy.types.Material]


def _record(value: object, context: str) -> Mapping[str, object]:
    if not isinstance(value, dict) or any(type(key) is not str for key in value):
        raise ValueError(f"Expected object: field={context}")
    return value


def _field(row: Mapping[str, object], name: str, context: str) -> object:
    if name not in row:
        raise ValueError(f"Required field missing: field={context}.{name}")
    return row[name]


def _integer(value: object, context: str) -> int:
    if type(value) is not int:
        raise ValueError(f"Expected integer: field={context}, value={value!r}")
    return value


def _finite(value: object, context: str) -> float:
    if type(value) not in (int, float) or not math.isfinite(float(value)):
        raise ValueError(f"Expected finite number: field={context}, value={value!r}")
    return float(value)


def _source_key(value: object, context: str) -> tuple[str, int]:
    row = _record(value, context)
    source = _field(row, "Source", context)
    if type(source) is not str or not source:
        raise ValueError(f"Expected source name: field={context}.Source")
    return source, _integer(_field(row, "PathId", context), f"{context}.PathId")


def _typed_source_key(value: object, expected: str, context: str) -> tuple[str, int]:
    row = _record(value, context)
    actual = _field(row, "Type", context)
    if actual != expected:
        raise ValueError(f"Asset pointer type mismatch: field={context}, expected={expected}, actual={actual!r}")
    return _source_key(row, context)


def _number_triplet(value: object, context: str) -> tuple[float, float, float]:
    if not isinstance(value, list) or len(value) != 3 or any(type(item) not in (int, float) for item in value):
        raise ValueError(f"Expected numeric Vector3: field={context}")
    return tuple(_finite(item, f"{context}[{index}]") for index, item in enumerate(value))  # type: ignore[return-value]


def _node_trs(node: Mapping[str, object]) -> Matrix:
    position = _number_triplet(_field(node, "LocalPosition", "Node"), "Node.LocalPosition")
    scale = _number_triplet(_field(node, "LocalScale", "Node"), "Node.LocalScale")
    rotation = _field(node, "LocalRotation", "Node")
    if not isinstance(rotation, list) or len(rotation) != 4 or any(type(item) not in (int, float) for item in rotation):
        raise ValueError("Expected numeric quaternion: field=Node.LocalRotation")
    x, y, z, w = (float(item) for item in rotation)
    return Matrix.LocRotScale(Vector(position), Quaternion((w, x, y, z)), Vector(scale))


def _owner_nodes(nodes: Mapping[tuple[str, int], Mapping[str, object]], root: tuple[str, int],
                 owner: tuple[str, int]) -> tuple[Mapping[str, object], ...]:
    chain: list[Mapping[str, object]] = []
    seen: set[tuple[str, int]] = set()
    transform_to_node: dict[tuple[str, int], tuple[str, int]] = {}
    for key, node in nodes.items():
        transform = _integer(_field(node, "TransformPathId", "Node"), "Node.TransformPathId")
        transform_to_node[(key[0], transform)] = key
    current = owner
    while True:
        if current in seen:
            raise ValueError(f"Prefab transform cycle: owner={owner}, repeated={current}")
        seen.add(current)
        node = nodes.get(current)
        if node is None:
            raise ValueError(f"Prefab GameObject missing from Nodes: owner={owner}, node={current}")
        chain.append(node)
        if current == root:
            break
        parent = _source_key(_field(node, "Parent", "Node"), "Node.Parent")
        if parent not in transform_to_node:
            raise ValueError(f"Prefab parent transform missing: owner={owner}, parent={parent}")
        current = transform_to_node[parent]
    return tuple(reversed(chain))


def _owner_matrix(nodes: Mapping[tuple[str, int], Mapping[str, object]], root: tuple[str, int], owner: tuple[str, int]) -> Matrix:
    matrix = Matrix.Identity(4)
    for node in _owner_nodes(nodes, root, owner):
        matrix @= _node_trs(node)
    return matrix


def _owner_matrix_root_relative(nodes: Mapping[tuple[str, int], Mapping[str, object]],
                                root: tuple[str, int], owner: tuple[str, int]) -> Matrix:
    """Place a child under a DynamicStreaming anchor that replaces prefab root TRS."""
    matrix = Matrix.Identity(4)
    for node in _owner_nodes(nodes, root, owner)[1:]:
        matrix @= _node_trs(node)
    return matrix


def _anchor_matrix(value: object) -> Matrix:
    if not isinstance(value, list) or len(value) != 16 or any(type(item) not in (int, float) for item in value):
        raise ValueError("Effect anchor matrix must contain 16 numeric Unity column-major values")
    return Matrix(tuple(tuple(_finite(value[column * 4 + row], f"matrix[{column * 4 + row}]")
                              for column in range(4)) for row in range(4)))


def _blender_matrix(unity: Matrix) -> Matrix:
    values = [float(unity[row][column]) for column in range(4) for row in range(4)]
    return unity_matrix(struct.pack("<16f", *values))


def _vector(value: tuple[float, float, float]) -> Vector:
    return Vector((value[0], -value[2], value[1]))


def _quad_axes(position: Vector, velocity: Vector, matrix: Matrix, camera: bpy.types.Object, render_mode: int) -> tuple[Vector, Vector]:
    inverse = matrix.inverted()
    view = (inverse @ camera.matrix_world.translation - position).normalized()
    camera_up = inverse.to_3x3() @ (camera.matrix_world.to_3x3() @ Vector((0.0, 1.0, 0.0)))
    up = camera_up - view * camera_up.dot(view)
    if render_mode == 1:
        motion = velocity - view * velocity.dot(view)
        if motion.length_squared > 1e-12:
            up = motion
    if up.length_squared <= 1e-12:
        raise ValueError(f"Cannot orient particle quad toward snapshot camera: camera={camera.name}")
    up.normalize()
    right = up.cross(view).normalized()
    return right, up


def _particle_world_matrix(unity_world: Matrix, owner_node: Mapping[str, object], scaling_mode: int) -> Matrix:
    if scaling_mode == 0:
        return unity_world
    location, rotation, world_scale = unity_world.decompose()
    if any(abs(world_scale[index]) <= 1e-9 for index in range(3)):
        raise ValueError(f"Particle transform has zero scale: scale={tuple(world_scale)}")
    normalized = unity_world.to_3x3()
    axes = [normalized.col[index].normalized() for index in range(3)]
    if any(abs(axes[left].dot(axes[right])) > 1e-5 for left, right in ((0, 1), (0, 2), (1, 2))):
        raise UnsupportedParticleSnapshot("Particle scaling with a sheared owner transform is unsupported")
    if scaling_mode == 1:
        own_scale = _number_triplet(_field(owner_node, "LocalScale", "Node"), "Node.LocalScale")
        return Matrix.LocRotScale(location, rotation, Vector(own_scale))
    if scaling_mode == 2 and all(abs(value - 1.0) <= 1e-6 for value in world_scale):
        return Matrix.LocRotScale(location, rotation, Vector((1.0, 1.0, 1.0)))
    raise UnsupportedParticleSnapshot(f"Particle shape-only scaling with non-unit transform is unsupported: mode={scaling_mode}, scale={tuple(world_scale)}")


def _gravity_local(gravity_world_unity: tuple[float, float, float], unity_world: Matrix) -> tuple[float, float, float]:
    if len(gravity_world_unity) != 3:
        raise ValueError("Particle world gravity must contain three numbers")
    gravity = Vector(tuple(_finite(value, f"gravity_world_unity[{index}]")
                           for index, value in enumerate(gravity_world_unity)))
    if all(value == 0.0 for value in gravity):
        return 0.0, 0.0, 0.0
    try:
        local = unity_world.to_3x3().inverted() @ gravity
    except ValueError as error:
        raise UnsupportedParticleSnapshot(
            "Nonzero particle world gravity requires an invertible owner transform: "
            f"gravity={tuple(gravity)}, scale={tuple(unity_world.decompose()[2])}"
        ) from error
    return tuple(float(value) for value in local)


def _system_gravity_local(gravity_world_unity: tuple[float, float, float], unity_world: Matrix,
                          system: Mapping[str, object]) -> tuple[float, float, float]:
    gravity = _number_triplet(list(gravity_world_unity), "gravity_world_unity")
    parameters = _record(_field(system, "Parameters", "System"), "System.Parameters")
    initial = _record(_field(parameters, "Initial", "Parameters"), "Parameters.Initial")
    modifier = parse_minmax_curve(_field(initial, "GravityModifier", "Initial"), "Initial.GravityModifier")
    # Zero source acceleration needs no coordinate inverse, including flattened mesh emitters.
    if (modifier.mode == 0 and modifier.scalar == 0.0
            or modifier.mode == 3 and modifier.scalar == 0.0 and modifier.min_scalar == 0.0):
        return _gravity_local((0.0, 0.0, 0.0), unity_world)
    return _gravity_local(gravity, unity_world)


def _particle_custom_attributes(mesh: bpy.types.Mesh, particles: tuple[SnapshotParticle, ...]) -> None:
    names = ("custom1", "custom2")
    for name in names:
        present = [getattr(particle, name) is not None for particle in particles]
        if any(present) and not all(present):
            raise ParticleSnapshotError(f"Particle custom stream varies across particles: stream={name}")
        if any(present):
            mesh.color_attributes.new(name=f"EF_FX_{name.upper()}", type="FLOAT_COLOR", domain="CORNER")


def _write_color_stream(mesh: bpy.types.Mesh, name: str,
                         values: Sequence[tuple[float, float, float, float] | None]) -> None:
    attribute = mesh.color_attributes.get(name)
    if attribute is None:
        if any(value is not None for value in values):
            raise ParticleSnapshotError(f"Color stream attribute missing: mesh={mesh.name}, stream={name}")
        return
    if len(values) != len(mesh.loops) or any(value is None for value in values):
        raise ParticleSnapshotError(f"Color stream corner count or values invalid: mesh={mesh.name}, stream={name}")
    # Bulk writes preserve signed shader data; the RNA color setter clamps negatives.
    packed = [component for value in values for component in value]
    attribute.data.foreach_set("color", packed)


def _particle_mesh(name: str, particles: tuple[SnapshotParticle, ...], matrix: Matrix,
                   camera: bpy.types.Object, render_mode: int, velocity_scale: float,
                   length_scale: float) -> bpy.types.Mesh:
    return _particle_mesh_aligned(name, particles, matrix, camera, render_mode, 0,
                                  velocity_scale, length_scale)


def _particle_mesh_aligned(name: str, particles: tuple[SnapshotParticle, ...], matrix: Matrix,
                           camera: bpy.types.Object, render_mode: int, render_alignment: int,
                           velocity_scale: float, length_scale: float) -> bpy.types.Mesh:
    if render_alignment not in (0, 2) or (render_alignment == 2 and render_mode != 0):
        raise UnsupportedParticleSnapshot(
            f"Particle billboard alignment is unsupported: mode={render_mode}, alignment={render_alignment}")
    vertices: list[tuple[float, float, float]] = []
    faces: list[tuple[int, int, int, int]] = []
    for particle in particles:
        center = _vector(particle.position)
        velocity = _vector(particle.velocity)
        right, up = (_quad_axes(center, velocity, matrix, camera, render_mode)
                     if render_alignment == 0 else (Vector((1.0, 0.0, 0.0)), Vector((0.0, 0.0, 1.0))))
        width = particle.size[0]
        height = particle.size[1]
        if render_mode == 1:
            height = height * length_scale + velocity.length * velocity_scale
        if width < 0 or height < 0:
            raise ParticleSnapshotError(f"Negative particle dimension: width={width}, height={height}")
        angle = particle.rotation_radians
        right, up = (right * math.cos(angle) + up * math.sin(angle),
                     up * math.cos(angle) - right * math.sin(angle))
        right *= width * 0.5
        up *= height * 0.5
        base = len(vertices)
        vertices.extend(tuple(center + side * right + vertical * up)
                        for side, vertical in ((-1, -1), (1, -1), (1, 1), (-1, 1)))
        faces.append((base, base + 1, base + 2, base + 3))
    mesh = bpy.data.meshes.new(name)
    mesh.from_pydata(vertices, [], faces)
    mesh.update()
    uv = mesh.uv_layers.new(name="UVMap")
    colors = mesh.color_attributes.new(name="EF_FX_COLOR", type="FLOAT_COLOR", domain="CORNER")
    _particle_custom_attributes(mesh, particles)
    uv = mesh.uv_layers["UVMap"]
    colors = mesh.color_attributes["EF_FX_COLOR"]
    coordinates = ((0.0, 0.0), (1.0, 0.0), (1.0, 1.0), (0.0, 1.0))
    for index, polygon in enumerate(mesh.polygons):
        u0, v0, u1, v1 = particles[index].uv_rect
        for corner, loop_index in enumerate(polygon.loop_indices):
            source_u, source_v = coordinates[corner]
            uv.data[loop_index].uv = (u0 + (u1 - u0) * source_u,
                                      v0 + (v1 - v0) * source_v)
    _write_color_stream(mesh, "EF_FX_COLOR", [particle.color for particle in particles for _ in range(4)])
    _write_color_stream(mesh, "EF_FX_CUSTOM1", [particle.custom1 for particle in particles for _ in range(4)])
    _write_color_stream(mesh, "EF_FX_CUSTOM2", [particle.custom2 for particle in particles for _ in range(4)])
    return mesh


def _trail_mesh(name: str, particles: tuple[SnapshotParticle, ...], matrix: Matrix,
                camera: bpy.types.Object) -> bpy.types.Mesh | None:
    vertices: list[tuple[float, float, float]] = []
    faces: list[tuple[int, int, int, int]] = []
    face_uvs: list[tuple[tuple[float, float], ...]] = []
    face_colors: list[tuple[tuple[float, float, float, float], ...]] = []
    camera_local = matrix.inverted() @ camera.matrix_world.translation
    for particle in particles:
        points = particle.trail
        if len(points) < 2:
            continue
        base = len(vertices)
        for index, point in enumerate(points):
            center = _vector(point.position)
            previous = _vector(points[max(0, index - 1)].position)
            following = _vector(points[min(len(points) - 1, index + 1)].position)
            tangent = following - previous
            view = camera_local - center
            if tangent.length_squared <= 1e-12 or view.length_squared <= 1e-12:
                raise ParticleSnapshotError(f"Trail ribbon tangent or camera direction is degenerate: particleAge={particle.age_fraction}")
            side = tangent.cross(view)
            if side.length_squared <= 1e-12:
                raise ParticleSnapshotError("Trail ribbon is parallel to snapshot camera ray")
            side.normalize()
            half_width = point.width * 0.5
            vertices.extend((tuple(center - side * half_width), tuple(center + side * half_width)))
            if index:
                prior = points[index - 1]
                faces.append((base + 2 * index - 2, base + 2 * index - 1,
                              base + 2 * index + 1, base + 2 * index))
                face_uvs.append(((prior.u, 0.0), (prior.u, 1.0), (point.u, 1.0), (point.u, 0.0)))
                face_colors.append((prior.color, prior.color, point.color, point.color))
    if not faces:
        return None
    mesh = bpy.data.meshes.new(name)
    mesh.from_pydata(vertices, [], faces)
    mesh.update()
    uv = mesh.uv_layers.new(name="UVMap")
    colors = mesh.color_attributes.new(name="EF_FX_COLOR", type="FLOAT_COLOR", domain="CORNER")
    uv = mesh.uv_layers["UVMap"]
    for index, polygon in enumerate(mesh.polygons):
        for corner, loop_index in enumerate(polygon.loop_indices):
            uv.data[loop_index].uv = face_uvs[index][corner]
    _write_color_stream(mesh, "EF_FX_COLOR", [color for face in face_colors for color in face])
    return mesh


def _mesh_templates(manifest: Mapping[str, object], manifest_path: Path,
                    renderer: Mapping[str, object],
                    cache: dict[tuple[Path, tuple[str, int]], tuple[bpy.types.Mesh, ...]]) -> tuple[tuple[bpy.types.Mesh, ...], ...]:
    exports_value = _field(manifest, "MeshExports", "Manifest")
    if not isinstance(exports_value, list):
        raise ValueError("Manifest.MeshExports must be an array")
    exports = {_typed_source_key(_field(_record(item, "MeshExport"), "Asset", "MeshExport"), "Mesh", "MeshExport.Asset"):
               _record(item, "MeshExport") for item in exports_value}
    slot_values = _field(renderer, "MeshSlots", "Renderer")
    if not isinstance(slot_values, list):
        raise ValueError("Renderer.MeshSlots must be an array")
    slots: list[tuple[bpy.types.Mesh, ...]] = []
    for index, item in enumerate(slot_values):
        pointer = _record(_field(_record(item, f"MeshSlot[{index}]"), "Mesh", "MeshSlot"), "MeshSlot.Mesh")
        target = _record(_field(pointer, "Target", "MeshSlot.Mesh"), "MeshSlot.Target")
        key = _typed_source_key(target, "Mesh", "MeshSlot.Target")
        if key[1] == 0:
            continue
        status = _field(pointer, "ResolutionStatus", "MeshSlot.Mesh")
        if status != "resolved":
            raise ValueError(f"Particle mesh slot unresolved: slot={index}, ref={key}, status={status}")
        if key not in exports:
            raise FileNotFoundError(f"Particle mesh export missing: slot={index}, ref={key}")
        cache_key = (manifest_path, key)
        if cache_key not in cache:
            path_value = _field(exports[key], "File", "MeshExport")
            if type(path_value) is not str or not path_value:
                raise ValueError(f"Particle mesh export file invalid: ref={key}")
            path = resolve_export_path(manifest_path.parent / path_value)
            if not path.is_file():
                raise FileNotFoundError(f"Particle mesh OBJ missing: ref={key}, path={path}")
            imported = import_obj(path)
            if not imported:
                raise ValueError(f"Particle mesh OBJ contains no mesh objects: ref={key}, path={path}")
            cache[cache_key] = tuple(obj.data for obj in imported)
            for obj in imported:
                bpy.data.objects.remove(obj, do_unlink=True)
        slots.append(cache[cache_key])
    if not slots:
        raise ValueError("Particle mesh renderer has no resolved mesh slots")
    return tuple(slots)


def _particle_mesh_instances(name: str, particles: tuple[SnapshotParticle, ...],
                             templates: tuple[tuple[bpy.types.Mesh, ...], ...]) -> bpy.types.Mesh:
    vertices: list[tuple[float, float, float]] = []
    faces: list[tuple[int, ...]] = []
    face_uvs: list[tuple[tuple[float, float], ...]] = []
    face_colors: list[tuple[tuple[float, float, float, float], ...]] = []
    face_rects: list[tuple[float, float, float, float]] = []
    face_custom1: list[tuple[float, float, float, float] | None] = []
    face_custom2: list[tuple[float, float, float, float] | None] = []
    for index, particle in enumerate(particles):
        if any(size < 0 for size in particle.size):
            raise ParticleSnapshotError(f"Negative mesh particle size: size={particle.size}")
        center = _vector(particle.position)
        rotation = Quaternion(Vector((0.0, -1.0, 0.0)), particle.rotation_radians)
        for template in templates[particle.particle_id % len(templates)]:
            offset = len(vertices)
            for vertex in template.vertices:
                scaled = Vector((vertex.co.x * particle.size[0],
                                 vertex.co.y * particle.size[2],
                                 vertex.co.z * particle.size[1]))
                vertices.append(tuple(center + rotation @ scaled))
            uv_source = template.uv_layers.active
            if uv_source is None:
                raise ValueError(f"Particle source mesh has no UV layer: mesh={template.name}")
            for polygon in template.polygons:
                faces.append(tuple(offset + template.loops[loop_index].vertex_index
                                   for loop_index in polygon.loop_indices))
                face_uvs.append(tuple(tuple(uv_source.data[loop_index].uv)
                                      for loop_index in polygon.loop_indices))
                face_colors.append(tuple(particle.color for _ in polygon.loop_indices))
                face_rects.append(particle.uv_rect)
                face_custom1.append(particle.custom1)
                face_custom2.append(particle.custom2)
    mesh = bpy.data.meshes.new(name)
    mesh.from_pydata(vertices, [], faces)
    mesh.update()
    uv = mesh.uv_layers.new(name="UVMap")
    colors = mesh.color_attributes.new(name="EF_FX_COLOR", type="FLOAT_COLOR", domain="CORNER")
    _particle_custom_attributes(mesh, particles)
    uv = mesh.uv_layers["UVMap"]
    colors = mesh.color_attributes["EF_FX_COLOR"]
    for index, polygon in enumerate(mesh.polygons):
        u0, v0, u1, v1 = face_rects[index]
        for corner, loop_index in enumerate(polygon.loop_indices):
            source_u, source_v = face_uvs[index][corner]
            uv.data[loop_index].uv = (u0 + (u1 - u0) * source_u,
                                      v0 + (v1 - v0) * source_v)
    _write_color_stream(mesh, "EF_FX_COLOR", [color for face in face_colors for color in face])
    _write_color_stream(mesh, "EF_FX_CUSTOM1", [value for polygon, value in zip(mesh.polygons, face_custom1)
                                               for _ in polygon.loop_indices])
    _write_color_stream(mesh, "EF_FX_CUSTOM2", [value for polygon, value in zip(mesh.polygons, face_custom2)
                                               for _ in polygon.loop_indices])
    return mesh


def _belongs_to_root(nodes: Mapping[tuple[str, int], Mapping[str, object]],
                     root: tuple[str, int], owner: tuple[str, int]) -> bool:
    transform_to_node = {(key[0], _integer(_field(node, "TransformPathId", "Node"), "Node.TransformPathId")): key
                         for key, node in nodes.items()}
    current = owner
    seen: set[tuple[str, int]] = set()
    while current in nodes:
        if current == root:
            return True
        if current in seen:
            raise ValueError(f"Prefab transform cycle: owner={owner}, repeated={current}")
        seen.add(current)
        parent = _source_key(_field(nodes[current], "Parent", "Node"), "Node.Parent")
        if parent not in transform_to_node:
            return False
        current = transform_to_node[parent]
    return False


def _components(manifest: Mapping[str, object], root: tuple[str, int]) -> tuple[Mapping[tuple[str, int], Mapping[str, object]], tuple[tuple[Mapping[str, object], Mapping[str, object], tuple[str, int]], ...]]:
    node_values = _field(manifest, "Nodes", "Manifest")
    if not isinstance(node_values, list):
        raise ValueError("Manifest.Nodes must be an array")
    nodes = {_source_key(node, "Node"): _record(node, "Node") for node in node_values}
    if root not in nodes:
        raise ValueError(f"Prefab root not found: root={root}")
    particles = _record(_field(manifest, "Particles", "Manifest"), "Manifest.Particles")
    system_values = _field(particles, "Systems", "Particles")
    renderer_values = _field(particles, "Renderers", "Particles")
    pair_values = _field(particles, "Pairs", "Particles")
    if not all(isinstance(value, list) for value in (system_values, renderer_values, pair_values)):
        raise ValueError("Particle systems, renderers, and pairs must be arrays")
    systems = {_typed_source_key(_field(_record(item, "System"), "Asset", "System"), "ParticleSystem", "System.Asset"): _record(item, "System") for item in system_values}
    renderers = {_typed_source_key(_field(_record(item, "Renderer"), "Asset", "Renderer"), "ParticleSystemRenderer", "Renderer.Asset"): _record(item, "Renderer") for item in renderer_values}
    joined: list[tuple[Mapping[str, object], Mapping[str, object], tuple[str, int]]] = []
    for item in pair_values:
        pair = _record(item, "Pair")
        system_key = _typed_source_key(_field(pair, "System", "Pair"), "ParticleSystem", "Pair.System")
        renderer_key = _typed_source_key(_field(pair, "Renderer", "Pair"), "ParticleSystemRenderer", "Pair.Renderer")
        owner = _typed_source_key(_field(pair, "GameObject", "Pair"), "GameObject", "Pair.GameObject")
        if not _belongs_to_root(nodes, root, owner):
            continue
        if system_key not in systems or renderer_key not in renderers:
            raise ValueError(f"Particle pair targets missing: system={system_key}, renderer={renderer_key}")
        for component, kind in ((systems[system_key], "System"), (renderers[renderer_key], "Renderer")):
            pointer = _record(_field(component, "GameObject", kind), f"{kind}.GameObject")
            target = _typed_source_key(_field(pointer, "Target", f"{kind}.GameObject"), "GameObject", f"{kind}.GameObject.Target")
            if target != owner or _field(pointer, "ResolutionStatus", f"{kind}.GameObject") != "resolved":
                raise ValueError(f"Particle owner pointer does not match pair: kind={kind}, pairOwner={owner}, pointer={target}")
        joined.append((systems[system_key], renderers[renderer_key], owner))
    return nodes, tuple(joined)


def build_scene_effects(effect_instances: Sequence[Mapping[str, object]], collection: bpy.types.Collection,
                        material_factory: MaterialFactory, snapshot_age_seconds: float,
                        snapshot_camera: bpy.types.Object, max_effect_instances: int,
                        max_particle_objects: int,
                        gravity_world_unity: tuple[float, float, float]) -> list[dict[str, object]]:
    """Place static preview quads and return explicit per-component audit rows."""
    if max_effect_instances <= 0 or max_particle_objects <= 0:
        raise ValueError("Effect and particle limits must be positive")
    if snapshot_camera.type != "CAMERA":
        raise ValueError("A snapshot camera object is required for camera-facing particles")
    audit: list[dict[str, object]] = []
    manifests: dict[Path, Mapping[str, object]] = {}
    components: dict[tuple[Path, tuple[str, int]], tuple[Mapping[tuple[str, int], Mapping[str, object]],
                                                     tuple[tuple[Mapping[str, object], Mapping[str, object], tuple[str, int]], ...]]] = {}
    mesh_templates: dict[tuple[Path, tuple[str, int]], tuple[bpy.types.Mesh, ...]] = {}
    remaining = max_particle_objects
    for instance_index, value in enumerate(effect_instances):
        instance = _record(value, f"effect_instances[{instance_index}]")
        entity_id = _integer(_field(instance, "entity_id", "EffectInstance"), "EffectInstance.entity_id")
        if instance_index >= max_effect_instances:
            audit.append({"entity_id": entity_id, "status": "truncated_effect_limit", "limit": max_effect_instances})
            continue
        if _field(instance, "matrix_space", "EffectInstance") != "dynamic_effect_anchor":
            raise ValueError(f"Unsupported effect matrix space: entity_id={entity_id}")
        manifest_source = Path(str(_field(instance, "scene_manifest", "EffectInstance")))
        if not manifest_source.is_absolute():
            raise ValueError(f"Effect SceneProbe manifest must be absolute: entity_id={entity_id}, path={manifest_source}")
        manifest_path = resolve_export_path(manifest_source)
        if not manifest_path.is_file():
            raise FileNotFoundError(f"Effect SceneProbe manifest missing: entity_id={entity_id}, path={manifest_source}")
        if manifest_path not in manifests:
            manifests[manifest_path] = _record(json.loads(manifest_path.read_text(encoding="utf-8-sig")), "Manifest")
        manifest = manifests[manifest_path]
        root = _typed_source_key(_field(instance, "prefab_root", "EffectInstance"), "GameObject", "EffectInstance.prefab_root")
        component_key = (manifest_path, root)
        if component_key not in components:
            components[component_key] = _components(manifest, root)
        nodes, pairs = components[component_key]
        particle_section = _record(_field(manifest, "Particles", "Manifest"), "Manifest.Particles")
        component_diagnostics = _field(particle_section, "Diagnostics", "Particles")
        if not isinstance(component_diagnostics, list):
            raise ValueError("Particles.Diagnostics must be an array")
        for item in component_diagnostics:
            diagnostic = _record(item, "ParticleDiagnostic")
            owner = _typed_source_key(_field(diagnostic, "GameObject", "ParticleDiagnostic"),
                                      "GameObject", "ParticleDiagnostic.GameObject")
            if _belongs_to_root(nodes, root, owner):
                audit.append({"entity_id": entity_id, "status": "particle_component_diagnostic",
                              "owner_source": owner[0], "owner_path_id": owner[1],
                              "code": _field(diagnostic, "Code", "ParticleDiagnostic"),
                              "detail": _field(diagnostic, "Detail", "ParticleDiagnostic")})
        if not pairs:
            audit.append({"entity_id": entity_id, "status": "particle_pair_missing",
                          "prefab_root_source": root[0], "prefab_root_path_id": root[1]})
            continue
        anchor = _anchor_matrix(_field(instance, "matrix", "EffectInstance"))
        for system, renderer, owner in pairs:
            system_key = _typed_source_key(_field(system, "Asset", "System"), "ParticleSystem", "System.Asset")
            row: dict[str, object] = {"entity_id": entity_id, "system_source": system_key[0],
                                      "system_path_id": system_key[1], "owner_source": owner[0],
                                      "owner_path_id": owner[1]}
            if not _field(renderer, "Enabled", "Renderer"):
                audit.append(row | {"status": "renderer_disabled"})
                continue
            render_mode = _integer(_field(renderer, "RenderMode", "Renderer"), "Renderer.RenderMode")
            if render_mode not in (0, 1, 4):
                audit.append(row | {"status": "unsupported_renderer_mode", "render_mode": render_mode})
                continue
            render_alignment = _integer(_field(renderer, "RenderAlignment", "Renderer"), "Renderer.RenderAlignment")
            row["render_alignment"] = render_alignment
            if remaining == 0:
                audit.append(row | {"status": "truncated_particle_limit", "limit": max_particle_objects})
                continue
            if render_alignment not in (0, 2) or (render_alignment == 2 and render_mode == 1):
                audit.append(row | {"status": "unsupported_renderer_alignment"})
                continue
            if _finite(_field(renderer, "CameraVelocityScale", "Renderer"), "Renderer.CameraVelocityScale") != 0.0:
                audit.append(row | {"status": "unsupported_camera_velocity_scale"})
                continue
            try:
                unity_world = _particle_world_matrix(
                    anchor @ _owner_matrix_root_relative(nodes, root, owner), nodes[owner],
                    _integer(_field(system, "ScalingMode", "System"), "System.ScalingMode"))
                world = _blender_matrix(unity_world)
                local_gravity = _system_gravity_local(gravity_world_unity, unity_world, system)
                snapshot = generate_particle_snapshot(system, snapshot_age_seconds,
                                                     f"{entity_id}:{system_key[0]}:{system_key[1]}",
                                                     remaining, 100000, local_gravity)
            except (UnsupportedParticleSnapshot, UnsupportedParticleCurve) as error:
                audit.append(row | {"status": "unsupported_particle_parameters", "reason": str(error)})
                continue
            materials = _field(renderer, "Materials", "Renderer")
            if not isinstance(materials, list) or not materials:
                audit.append(row | {"status": "material_slot_missing"})
                continue
            material_refs = tuple(_record(_field(_record(item, f"Renderer.Materials[{index}]"), "Target", "MaterialSlot"), "MaterialRef")
                                  for index, item in enumerate(materials))
            identities = tuple(_typed_source_key(ref, "Material", "MaterialRef") for ref in material_refs)
            if len(set(identities)) != 1:
                audit.append(row | {"status": "unsupported_multiple_materials", "material_refs": [list(key) for key in identities]})
                continue
            material_ref = material_refs[0]
            if _integer(_field(material_ref, "PathId", "MaterialRef"), "MaterialRef.PathId") == 0:
                audit.append(row | {"status": "material_slot_null"})
                continue
            try:
                material = material_factory(material_ref, renderer, system)
            except NotImplementedError as error:
                audit.append(row | {"status": "unsupported_material", "reason": str(error)})
                continue
            if not isinstance(material, bpy.types.Material):
                raise TypeError(f"Material factory returned no Blender material: entity_id={entity_id}, system={system_key}")
            material_limitations_value = material.get("endfield_fx_limitations")
            material_limitations = (json.loads(material_limitations_value)
                                    if isinstance(material_limitations_value, str) else [])
            if not isinstance(material_limitations, list) or any(type(item) is not str for item in material_limitations):
                raise ValueError(f"FX material limitations must be a string array: material={material.name}")
            if not snapshot.particles:
                audit.append(row | {"status": "no_alive_particles_at_snapshot", "generated": snapshot.generated,
                                    "unsupported_modules": list(snapshot.unsupported_modules)})
                continue
            custom_channels_value = material.get("endfield_fx_custom1_channels")
            custom_channels = (json.loads(custom_channels_value)
                               if isinstance(custom_channels_value, str) else [])
            if not isinstance(custom_channels, list) or any(type(item) is not str for item in custom_channels):
                raise ValueError(f"FX Custom1 channels must be a string array: material={material.name}")
            if custom_channels and any(particle.custom1 is None for particle in snapshot.particles):
                audit.append(row | {"status": "unsupported_custom_vertex_stream",
                                    "required_channels": custom_channels,
                                    "reason": "Material reads Custom1 but the particle snapshot has no Custom1 data"})
                continue
            if render_mode == 4:
                templates = _mesh_templates(manifest, manifest_path, renderer, mesh_templates)
                mesh = _particle_mesh_instances(f"EF_FX_{entity_id}_{system_key[1]}",
                                                snapshot.particles, templates)
            else:
                mesh = _particle_mesh_aligned(f"EF_FX_{entity_id}_{system_key[1]}", snapshot.particles, world,
                                              snapshot_camera, render_mode, render_alignment,
                                              _finite(_field(renderer, "VelocityScale", "Renderer"), "Renderer.VelocityScale"),
                                              _finite(_field(renderer, "LengthScale", "Renderer"), "Renderer.LengthScale"))
            try:
                displaced = apply_material_vertex_offset(
                    material, mesh, world, snapshot_camera.matrix_world.translation, snapshot_age_seconds)
            except UnsupportedVertexOffset as error:
                bpy.data.meshes.remove(mesh)
                audit.append(row | {"status": "unsupported_vertex_offset", "reason": str(error)})
                continue
            if displaced is not mesh:
                bpy.data.meshes.remove(mesh)
                mesh = displaced
            mesh.materials.append(material)
            obj = bpy.data.objects.new(mesh.name, mesh)
            collection.objects.link(obj)
            obj.matrix_world = world
            obj["endfield_fx_preview_scope"] = snapshot.preview_scope
            obj["endfield_fx_unsupported_modules"] = ",".join(snapshot.unsupported_modules)
            obj["endfield_fx_system_source"] = system_key[0]
            obj["endfield_fx_system_path_id"] = str(system_key[1])
            obj["endfield_fx_entity_id"] = str(entity_id)
            if "endfield_fx_vertex_offset_scope" in mesh:
                obj["endfield_fx_vertex_offset_scope"] = mesh["endfield_fx_vertex_offset_scope"]
            has_trail_points = any(len(particle.trail) >= 2 for particle in snapshot.particles)
            trail_status = "no_trail_at_snapshot"
            trail_reason: str | None = None
            trail_mesh: bpy.types.Mesh | None = None
            if has_trail_points and custom_channels:
                trail_status = "unsupported_trail_custom_vertex_stream"
                trail_reason = "Trail geometry has no verified Custom1 stream mapping required by the material"
            elif has_trail_points:
                trail_mesh = _trail_mesh(f"EF_FX_TRAIL_{entity_id}_{system_key[1]}",
                                         snapshot.particles, world, snapshot_camera)
            trail_object_name: str | None = None
            if trail_mesh is not None:
                trail_mesh.materials.append(material)
                trail_obj = bpy.data.objects.new(trail_mesh.name, trail_mesh)
                collection.objects.link(trail_obj)
                trail_obj.matrix_world = world
                trail_obj["endfield_fx_preview_scope"] = snapshot.preview_scope
                trail_obj["endfield_fx_system_source"] = system_key[0]
                trail_obj["endfield_fx_system_path_id"] = str(system_key[1])
                trail_obj["endfield_fx_entity_id"] = str(entity_id)
                trail_object_name = trail_obj.name
                trail_status = "static_trail_preview"
            remaining -= len(snapshot.particles)
            audit.append(row | {"status": "static_preview_partial" if snapshot.truncated > 0 or snapshot.unsupported_modules or material_limitations or render_mode in (1, 4) or trail_status == "unsupported_trail_custom_vertex_stream" else "static_preview",
                                "object": obj.name, "particles": len(snapshot.particles),
                                "trail_object": trail_object_name,
                                "trail_status": trail_status,
                                "trail_reason": trail_reason,
                                "trail_segments": len(trail_mesh.polygons) if trail_mesh is not None else 0,
                                "truncated_particles": snapshot.truncated,
                                "unsupported_modules": list(snapshot.unsupported_modules),
                                "material_limitations": material_limitations,
                                "vertex_offset_scope": mesh.get("endfield_fx_vertex_offset_scope"),
                                "render_mode": render_mode,
                                "render_approximation": ("local_transform_static_quad" if render_alignment == 2 and render_mode == 0
                                                         else "local_transform_mesh_slot_static_geometry" if render_alignment == 2
                                                         else {0: "camera_facing_static_quad",
                                                               1: "camera_facing_stretched_static_quad",
                                                               4: "deterministic_mesh_slot_static_geometry"}[render_mode])})
    return audit
