"""Optional artist microfibres over retained source shells, with native Curves skin transport."""
from __future__ import annotations

from dataclasses import asdict, dataclass
import json
import math

import bpy
import numpy as np

from audit_character_materials import source_geometry_digest
from character_fur_geometry import _point_values, attach_source_fur_skin_deformation


@dataclass(frozen=True, slots=True)
class FurMicrofibreProfile:
    root_material_slot: int
    barycentric_samples: tuple[tuple[float, float, float], ...]
    radius_meters: float
    tip_radius_ratio: float
    canopy_start: float
    tip_extension: float
    taper_exponent: float
    substrate_gain: float
    roughness: float


def triangle_fibre_samples(count: int) -> tuple[tuple[float, float, float], ...]:
    """Return deterministic interior triangle samples without a random seed or density cap."""
    if isinstance(count, bool) or not isinstance(count, int) or count < 1:
        raise ValueError('Microfibre triangle sample count must be a positive integer')
    golden_fraction = (math.sqrt(5) - 1) / 2
    result: list[tuple[float, float, float]] = []
    for index in range(count):
        radial = math.sqrt((index + .5) / count)
        angular = ((index + .5) * golden_fraction) % 1
        result.append((1 - radial, radial * (1 - angular), radial * angular))
    return tuple(result)


def _validate_profile(profile: FurMicrofibreProfile) -> np.ndarray:
    if isinstance(profile.root_material_slot, bool) or not isinstance(profile.root_material_slot, int) or profile.root_material_slot < 0:
        raise ValueError('Microfibre root material slot must be a nonnegative integer')
    scalars = (profile.radius_meters, profile.tip_radius_ratio, profile.canopy_start,
               profile.tip_extension, profile.taper_exponent, profile.substrate_gain, profile.roughness)
    if any(isinstance(value, bool) or not math.isfinite(value) for value in scalars):
        raise ValueError('Microfibre profile scalars must be finite numbers')
    if (profile.radius_meters <= 0 or not 0 < profile.tip_radius_ratio <= 1
            or not 0 <= profile.canopy_start < 1 or profile.tip_extension <= 0
            or profile.taper_exponent <= 0 or profile.substrate_gain < 0
            or not 0 <= profile.roughness <= 1):
        raise ValueError(f'Invalid explicit microfibre profile: {profile}')
    samples = np.asarray(profile.barycentric_samples, dtype=np.float64)
    if (samples.ndim != 2 or samples.shape[0] == 0 or samples.shape[1] != 3
            or not np.isfinite(samples).all() or np.any(samples <= 0)
            or np.max(np.abs(samples.sum(axis=1) - 1)) > 1e-7
            or len(np.unique(samples, axis=0)) != len(samples)):
        raise ValueError('Microfibre samples must be unique interior barycentric triples summing to one')
    return samples


def _source_name(record: dict[str, object], key: str) -> str:
    value = record.get(key)
    if not isinstance(value, str) or not value:
        raise ValueError(f'Active source-shell receipt requires a nonempty {key}')
    return value


def _source_objects(shell: bpy.types.Object) -> tuple[bpy.types.Object, bpy.types.Object]:
    if shell.type != 'MESH' or shell.hide_render or 'source_fur_shell' not in shell:
        raise ValueError('Microfibres require an explicit visible retained source-shell mesh')
    raw: object = json.loads(shell['source_fur_shell'])
    if not isinstance(raw, dict) or any(not isinstance(key, str) for key in raw):
        raise TypeError('Source-shell receipt must be an object with string keys')
    if raw.get('format') != 'EndfieldExpandedSourceFurShell/1' or raw.get('status') != 'active':
        raise ValueError('Microfibres require an active source-shell receipt')
    if _source_name(raw, 'shell_mesh') != shell.name:
        raise ValueError('Source-shell receipt identifies a different shell mesh')
    source = bpy.data.objects.get(_source_name(raw, 'source_mesh'))
    guides = bpy.data.objects.get(_source_name(raw, 'bind_guides'))
    if source is None or source.type != 'MESH' or guides is None or guides.type != 'CURVES':
        raise ValueError('Source-shell mesh or source-derived bind guides are missing')
    if guides.parent != source or guides.modifiers:
        raise ValueError('Microfibres require undeformed source-derived bind guides parented to the source mesh')
    return source, guides


def _samples(source: bpy.types.Object, guides: bpy.types.Object, slot: int,
             barycentric: np.ndarray) -> tuple[np.ndarray, np.ndarray, np.ndarray, np.ndarray, int]:
    counts = tuple(curve.points_length for curve in guides.data.curves)
    if not counts or len(set(counts)) != 1 or counts[0] < 2:
        raise ValueError('Microfibre source guides must share a nonempty point count of at least two')
    if slot >= len(source.data.materials):
        raise ValueError(f'Microfibre root material slot is outside source mesh slots: {slot}')
    points = counts[0]
    indices = _point_values(guides.data, 'source_vertex_index', 'INT', 'value', 1).reshape(-1, points)
    roots = {int(row[0]): index for index, row in enumerate(indices)}
    if len(roots) != len(counts):
        raise ValueError('Microfibre source guides repeat a root vertex')
    faces = tuple(face for face in source.data.polygons if face.material_index == slot)
    if not faces or any(len(face.vertices) != 3 for face in faces):
        raise ValueError('Microfibre roots require a nonempty triangulated source material slot')
    if any(index not in roots for face in faces for index in face.vertices):
        raise ValueError('Source bind guides do not cover every selected triangle root')
    triangle_guides = np.asarray([[roots[index] for index in face.vertices] for face in faces], dtype=np.int32)
    guide_points = np.arange(len(guides.data.points), dtype=np.int32).reshape(-1, points)
    triangle_points = guide_points[triangle_guides].transpose(0, 2, 1)
    extra_indices = np.repeat(triangle_points[:, None, :, :], len(barycentric), axis=1).reshape(-1, 3)
    extra_weights = np.broadcast_to(barycentric[None, :, None, :],
                                    (len(faces), len(barycentric), points, 3)).reshape(-1, 3)
    originals = np.repeat(np.arange(len(guides.data.points), dtype=np.int32)[:, None], 3, axis=1)
    sample_indices = np.concatenate((originals, extra_indices))
    sample_weights = np.concatenate((np.tile((1., 0., 0.), (len(originals), 1)), extra_weights))
    triangles = np.concatenate((np.full(len(counts), -1, dtype=np.int32),
                                np.repeat(np.asarray([face.index for face in faces], dtype=np.int32), len(barycentric))))
    sample_ids = np.concatenate((np.full(len(counts), -1, dtype=np.int32),
                                 np.tile(np.arange(len(barycentric), dtype=np.int32), len(faces))))
    return sample_indices, sample_weights, triangles, sample_ids, points


def _material(name: str, profile: FurMicrofibreProfile) -> bpy.types.Material:
    material = bpy.data.materials.new(name)
    material.use_nodes = True
    tree = material.node_tree
    if tree is None:
        raise RuntimeError('Microfibre material did not create a shader node tree')
    tree.nodes.clear()
    source = tree.nodes.new('ShaderNodeAttribute')
    source.attribute_name = 'hair_source_color'
    source.label = 'Sampled source color and dye / explicit artist gain'
    source.location = (-450, 120)
    gain = tree.nodes.new('ShaderNodeMixRGB')
    gain.blend_type = 'MULTIPLY'
    gain.inputs[0].default_value = 1.0
    gain.inputs[2].default_value = (profile.substrate_gain, profile.substrate_gain, profile.substrate_gain, 1.)
    gain.label = 'Artist charcoal fibre brightness'
    gain.location = (-220, 120)
    shader = tree.nodes.new('ShaderNodeBsdfPrincipled')
    shader.inputs['Roughness'].default_value = profile.roughness
    shader.inputs['Emission Strength'].default_value = 0.0
    shader.location = (20, 120)
    output = tree.nodes.new('ShaderNodeOutputMaterial')
    output.location = (330, 120)
    tree.links.new(source.outputs['Color'], gain.inputs[1])
    tree.links.new(gain.outputs[0], shader.inputs['Base Color'])
    tree.links.new(shader.outputs['BSDF'], output.inputs['Surface'])
    material['artist_microfibre_material'] = 'Sampled source color with explicit artist gain; no added emission or source-shell material replacement.'
    return material


def _verify_attribute_modifier(modifier: bpy.types.Modifier) -> None:
    """Admit only attribute stores and zero subdivision on the output geometry path."""
    if modifier.type != 'NODES' or modifier.node_group is None:
        raise ValueError(f'Microfibre skin cannot discard a source deformation modifier: {modifier.name}')
    tree = modifier.node_group
    outputs = tuple(node for node in tree.nodes if node.type == 'GROUP_OUTPUT' and node.is_active_output)
    if len(outputs) != 1 or 'Geometry' not in outputs[0].inputs:
        raise ValueError(f'Attribute-only modifier has no unambiguous Geometry output: {modifier.name}')
    socket = outputs[0].inputs['Geometry']
    visited: set[int] = set()
    while True:
        if len(socket.links) != 1:
            raise ValueError(f'Attribute-only geometry path is disconnected or ambiguous: {modifier.name}')
        origin = socket.links[0].from_node
        if origin.as_pointer() in visited:
            raise ValueError(f'Attribute-only geometry path contains a cycle: {modifier.name}')
        visited.add(origin.as_pointer())
        if origin.type == 'GROUP_INPUT':
            if socket.links[0].from_socket.name != 'Geometry':
                raise ValueError(f'Attribute-only modifier reads an unexpected geometry input: {modifier.name}')
            return
        if origin.bl_idname == 'GeometryNodeStoreNamedAttribute':
            if origin.inputs['Name'].is_linked or origin.inputs['Name'].default_value == 'position':
                raise ValueError(f'Microfibre skin cannot discard a dynamic or position attribute store: {modifier.name}')
            socket = origin.inputs['Geometry']
        elif (origin.bl_idname == 'GeometryNodeSubdivideMesh' and not origin.inputs['Level'].is_linked
              and origin.inputs['Level'].default_value == 0):
            socket = origin.inputs['Mesh']
        else:
            raise ValueError(f'Microfibre skin requires an unchanged source geometry path: {modifier.name}/{origin.bl_idname}')


def _skin_carrier(source: bpy.types.Object, collection: bpy.types.Collection) -> bpy.types.Object:
    """Copy source weights and Armature while excluding proven attribute-only geometry work."""
    for modifier in source.modifiers:
        if modifier.type != 'ARMATURE' and (modifier.show_viewport or modifier.show_render):
            _verify_attribute_modifier(modifier)
    carrier = source.copy()
    carrier.data = source.data.copy()
    carrier.name = 'EF Microfibre Skin Carrier ' + source.name
    collection.objects.link(carrier)
    for modifier in tuple(carrier.modifiers):
        if modifier.type != 'ARMATURE':
            carrier.modifiers.remove(modifier)
    carrier.hide_render = True
    carrier.hide_set(True)
    carrier['artist_microfibre_role'] = 'Owned source-weight carrier; only Armature deformation enters fibre transport.'
    return carrier


def attach_fur_microfibres(shell: bpy.types.Object, profile: FurMicrofibreProfile) -> dict[str, object]:
    """Add owned fine Curves without changing source geometry, shell materials or existing guides.

    Seven explicit samples per source triangle are a useful starting density;
    larger caller-selected sets use the identical barycentric skin transport.
    Zero radius hides the inner path while retaining its full source envelope.
    This is an artist adjunct, not recovered native strand geometry or shading.
    """
    barycentric = _validate_profile(profile)
    source, source_guides = _source_objects(shell)
    if any(obj.get('artist_microfibre_shell') == shell.name for obj in bpy.data.objects):
        raise ValueError(f'A microfibre adjunct is already attached to this shell: {shell.name}')
    samples, weights, triangles, sample_ids, points = _samples(source, source_guides, profile.root_material_slot, barycentric)
    before = source_geometry_digest((source.name, shell.name))
    source_positions = _point_values(source_guides.data, 'position', 'FLOAT_VECTOR', 'vector', 3).reshape(-1, points, 3)
    extended = source_positions[:, :1] + profile.tip_extension * (source_positions - source_positions[:, :1])
    layers = _point_values(source_guides.data, 'source_shell_layer', 'FLOAT', 'value', 1).reshape(-1, points)
    if np.any(np.diff(layers, axis=1) < 0) or np.any(layers[:, -1] <= layers[:, 0]):
        raise ValueError('Microfibre source tracks require increasing shell coordinates and a nonzero span')
    collection = bpy.data.collections.new('EF Artist Microfibres ' + source.name)
    bpy.context.scene.collection.children.link(collection)
    carrier = _skin_carrier(source, collection)
    bind = source_guides.copy()
    bind.data = source_guides.data.copy()
    bind.name = 'EF Microfibre Bind ' + source.name
    collection.objects.link(bind)
    bind.parent = carrier
    bind.data.attributes['position'].data.foreach_set('vector', extended.astype(np.float32).reshape(-1))
    bind.hide_render = True
    bind.hide_set(True)
    bind.data.materials.clear()
    bind['artist_microfibre_role'] = 'Owned extended bind tracks; original source guides remain unchanged.'
    data = bpy.data.hair_curves.new('EF Artist Microfibre Geometry ' + source.name)
    curve_count = len(samples) // points
    data.add_curves([points] * curve_count)
    attributes = (('position', 'FLOAT_VECTOR', 'vector', 3), ('UVMap', 'FLOAT2', 'vector', 2),
                  ('hair_source_color', 'FLOAT_COLOR', 'color', 4), ('hair_source_alpha', 'FLOAT', 'value', 1),
                  ('source_shell_layer', 'FLOAT', 'value', 1))
    for name, kind, field, dimensions in attributes:
        values = _point_values(bind.data, name, kind, field, dimensions)
        interpolated = np.sum(values[samples] * weights[:, :, None], axis=1).astype(np.float32)
        attribute = data.attributes.get(name) or data.attributes.new(name, kind, 'POINT')
        attribute.data.foreach_set(field, interpolated.reshape(-1))
    dense_layers = _point_values(data, 'source_shell_layer', 'FLOAT', 'value', 1).reshape(-1, points)
    coordinate = ((dense_layers - dense_layers[:, :1]) / (dense_layers[:, -1:] - dense_layers[:, :1])).reshape(-1)
    canopy = np.clip((coordinate - profile.canopy_start) / (1 - profile.canopy_start), 0., 1.)
    entrance = np.clip(canopy * 10., 0., 1.)
    entrance = entrance * entrance * (3 - 2 * entrance)
    radius = profile.radius_meters * entrance * (profile.tip_radius_ratio + (1 - profile.tip_radius_ratio)
                                                * np.power(1 - canopy, profile.taper_exponent))
    for name, values in (('radius', radius), ('artist_canopy_coordinate', coordinate)):
        attribute = data.attributes.new(name, 'FLOAT', 'POINT')
        attribute.data.foreach_set('value', values.astype(np.float32))
    for name, values in (('fur_skin_sample_indices', samples), ('fur_skin_sample_weights', weights)):
        attribute = data.attributes.new(name, 'FLOAT_VECTOR', 'POINT')
        attribute.data.foreach_set('vector', values.astype(np.float32).reshape(-1))
    for name, values in (('source_root_triangle', triangles), ('artist_density_sample', sample_ids)):
        attribute = data.attributes.new(name, 'INT', 'CURVE')
        attribute.data.foreach_set('value', values)
    material = _material('EF Artist Dark Microfibres ' + source.name, profile)
    data.materials.append(material)
    fibres = bpy.data.objects.new('EF Artist Fine Fibres ' + source.name, data)
    collection.objects.link(fibres)
    fibres.parent = carrier
    fibres.matrix_parent_inverse = source_guides.matrix_parent_inverse.copy()
    fibres.matrix_basis = source_guides.matrix_basis.copy()
    skin = attach_source_fur_skin_deformation(carrier, bind, fibres, collection)
    if source_geometry_digest((source.name, shell.name)) != before:
        raise AssertionError('Microfibre generation changed retained source or shell geometry/binding')
    fibres['artist_microfibre_shell'] = shell.name
    record: dict[str, object] = {
        'format': 'EndfieldArtistFurMicrofibres/1', 'object_name': fibres.name,
        'source_shell': shell.name, 'source_mesh': source.name, 'source_bind_guides': source_guides.name,
        'bind_guides': bind.name, 'skin_carrier': carrier.name, 'collection': collection.name, 'material': material.name,
        'source_guides': len(source_guides.data.curves), 'triangle_count': int(np.count_nonzero(sample_ids == 0)),
        'samples_per_triangle': len(barycentric), 'curve_count': curve_count, 'point_count': len(data.points),
        'profile': asdict(profile), 'skin_binding': asdict(skin), 'source_geometry_preserved': True,
        'source_shell_materials_retained': True, 'native_equivalence': False,
        'scope': 'Explicit artist canopy fibres over retained full shell; sampled source color, deterministic barycentric density and source Armature skin transport. No added emission.',
    }
    fibres['artist_fur_microfibres'] = json.dumps(record, sort_keys=True)
    return record
