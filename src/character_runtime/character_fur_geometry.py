"""Source-derived shell guides with an explicit clip-to-world approximation.

The compiled vertex shader adds its expansion to each original shell vertex.
Therefore the small source shell displacement is retained, not used as a length
multiplier. FurDirMap RG changes fragment noise coordinates, not geometry.
The inspected native DXBC reads shell UV1 as TEXCOORD1 and passes it unchanged
to the fragment TEXCOORD4 varying. Clip W and the shading varyings remain
unexpanded; physical Curves do not reproduce that rasterization behavior.
"""

from __future__ import annotations

import array
from dataclasses import dataclass
import hashlib
import json
import math
from types import ModuleType

import bpy
from mathutils import Matrix
import numpy as np


@dataclass(frozen=True, slots=True)
class ShellSeries:
    key: str
    parameters: tuple[float, ...]
    indices: tuple[tuple[int, ...], ...]
    faces_per_layer: int


@dataclass(frozen=True, slots=True)
class FurGeometryParameters:
    length_intensity: float
    gravity_strength: float
    disable_rain_effect_on_material: float
    source_world_up: tuple[float, float, float]
    radius_meters: float
    root_stride: int
    artist_length_multiplier: float


@dataclass(frozen=True, slots=True)
class FurCurveResult:
    object_name: str
    series_count: int
    roots_per_series: tuple[int, ...]
    curve_count: int
    point_count: int
    source_track_mean_meters: float
    expanded_track_mean_meters: float
    expansion_max_meters: float
    direction_alpha_range: tuple[float, float]


@dataclass(frozen=True, slots=True)
class FurInterpolationResult:
    object_name: str
    source_guides: int
    interpolated_guides: int
    point_count: int


@dataclass(frozen=True, slots=True)
class FurSkinBindingResult:
    proxy_object: str
    modifier_name: str
    armature: str
    bind_points: int
    driven_points: int


@dataclass(frozen=True, slots=True)
class FurRadiusProfileResult:
    object_name: str
    modifier_name: str
    curve_count: int
    point_count: int
    source_radius_range: tuple[float, float]
    candidate_radius_range: tuple[float, float]
    tip_radius_ratio: float
    exponent: float


def _hash(value: object) -> str:
    return hashlib.sha256(json.dumps(value, separators=(",", ":")).encode()).hexdigest()


def vertex_uvs(mesh: bpy.types.Mesh, layer_name: str, tolerance: float) -> np.ndarray:
    """Reject UV seams instead of averaging multiple texture coordinates."""
    layer = mesh.uv_layers.get(layer_name)
    if layer is None:
        raise ValueError(f"Required source UV layer missing: {mesh.name}/{layer_name}")
    values: list[tuple[float, float] | None] = [None] * len(mesh.vertices)
    for loop in mesh.loops:
        uv = tuple(float(v) for v in layer.data[loop.index].uv)
        previous = values[loop.vertex_index]
        if previous is not None and max(abs(uv[i] - previous[i]) for i in range(2)) > tolerance:
            raise ValueError(f"Ambiguous source root UV: {mesh.name}, vertex={loop.vertex_index}")
        values[loop.vertex_index] = uv
    if any(value is None for value in values):
        raise ValueError(f"Source vertices without UV loops: {mesh.name}")
    result = np.asarray(values, dtype=np.float64)
    if not np.isfinite(result).all():
        raise ValueError(f"Non-finite source UV: {mesh.name}/{layer_name}")
    return result


def vertex_authored_normals(mesh: bpy.types.Mesh, tolerance: float) -> np.ndarray:
    """Read imported source normals without averaging split corner directions.

    MeshVertex.normal is recomputed from the mesh topology and can differ from
    the authored normals used by the native vertex expansion. A vertex guide
    is valid only when every incident source corner has the same direction.
    """
    if not math.isfinite(tolerance) or tolerance <= 0:
        raise ValueError("Authored-normal tolerance must be finite and positive")
    if not mesh.has_custom_normals:
        raise ValueError(f"Imported authored normals are required for source fur: {mesh.name}")
    if len(mesh.corner_normals) != len(mesh.loops):
        raise ValueError(f"Authored corner-normal count differs from source loops: {mesh.name}")
    values: list[tuple[float, float, float] | None] = [None] * len(mesh.vertices)
    for loop, corner in zip(mesh.loops, mesh.corner_normals):
        normal = tuple(float(value) for value in corner.vector)
        previous = values[loop.vertex_index]
        if previous is not None and max(abs(normal[index]-previous[index]) for index in range(3)) > tolerance:
            raise ValueError(f"Split authored normals require corner-specific fur guides: mesh={mesh.name}, vertex={loop.vertex_index}, loop={loop.index}")
        if previous is None:
            values[loop.vertex_index] = normal
    if any(value is None for value in values):
        raise ValueError(f"Source vertices without authored normal corners: {mesh.name}")
    result = np.asarray(values, dtype=np.float64)
    if not np.isfinite(result).all() or np.any(np.linalg.norm(result, axis=1) <= 1e-12):
        raise ValueError(f"Non-finite or zero authored source normal: {mesh.name}")
    return result


def detect_shell_series(
    mesh: bpy.types.Mesh,
    layer_uv: np.ndarray,
    root_uv_name: str,
    tolerance: float,
    helpers: ModuleType,
) -> tuple[ShellSeries, ...]:
    """Reuse connected-island/topology/UV correspondence from the shell study."""
    if layer_uv.shape != (len(mesh.vertices), 2) or not np.isfinite(layer_uv).all():
        raise ValueError(f"Invalid source shell-channel shape: {layer_uv.shape}")
    if not math.isfinite(tolerance) or tolerance <= 0:
        raise ValueError("Shell tolerance must be finite and positive")
    if np.max(np.abs(layer_uv[:, 0] - layer_uv[:, 1])) > tolerance:
        raise ValueError("Candidate shell channel components do not agree")
    uv0 = vertex_uvs(mesh, root_uv_name, tolerance)
    components = tuple(tuple(sorted(row)) for row in helpers.read_mesh_components(mesh))
    component_for_vertex = np.empty(len(mesh.vertices), dtype=np.int32)
    local_maps: list[dict[int, int]] = []
    faces: list[list[tuple[tuple[int, float, float], ...]]] = [[] for _ in components]
    for index, component in enumerate(components):
        component_for_vertex[list(component)] = index
        local_maps.append({vertex: local for local, vertex in enumerate(component)})
    for polygon in mesh.polygons:
        index = int(component_for_vertex[polygon.vertices[0]])
        if any(component_for_vertex[v] != index for v in polygon.vertices):
            raise ValueError(f"Polygon crosses shell components: {polygon.index}")
        corners = tuple((local_maps[index][v], round(float(uv0[v, 0]), 7), round(float(uv0[v, 1]), 7)) for v in polygon.vertices)
        faces[index].append(min(corners[i:] + corners[:i] for i in range(len(corners))))
    groups: dict[str, list[tuple[float, tuple[int, ...], int]]] = {}
    for index, component in enumerate(components):
        values = layer_uv[list(component), 0]
        if np.ptp(values) > tolerance or not faces[index]:
            raise ValueError(f"Connected shell island has mixed layer values or no faces: {index}")
        parameter = float(values.mean())
        if parameter < -tolerance or parameter > 1 + tolerance:
            raise ValueError(f"Shell parameter outside [0,1]: {parameter}")
        key = _hash((len(component), sorted(faces[index])))
        groups.setdefault(key, []).append((parameter, component, len(faces[index])))
    result: list[ShellSeries] = []
    common: tuple[float, ...] | None = None
    for key, rows in sorted(groups.items()):
        ordered = sorted(rows)
        parameters = tuple(row[0] for row in ordered)
        if len(parameters) < 2 or abs(parameters[0]) > tolerance or abs(parameters[-1] - 1) > tolerance:
            raise ValueError(f"Shell topology series lacks complete root/tip layers: {key}")
        if any(b - a <= tolerance for a, b in zip(parameters, parameters[1:])):
            raise ValueError(f"Ambiguous repeated island in the same shell layer: {key}")
        if common is not None and (len(common) != len(parameters) or max(abs(a-b) for a,b in zip(common, parameters)) > tolerance):
            raise ValueError(f"Shell topology series disagree on layer values: {key}")
        common = parameters
        result.append(ShellSeries(key, parameters, tuple(row[1] for row in ordered), ordered[0][2]))
    return tuple(result)


def expanded_shell_paths(
    source_paths: np.ndarray,
    surface_normals: np.ndarray,
    layers: np.ndarray,
    direction_alpha: np.ndarray,
    parameters: FurGeometryParameters,
) -> np.ndarray:
    """Approximate the verified vertex expansion in metric world space.

    Native code uses projected clip coordinates. Here the same scalar and
    normal/gravity mixing are interpreted as world meters, without a hidden
    artist scale. Weather-driven gravity is excluded: this is the dry branch.
    """
    roots, points, dimensions = source_paths.shape
    if dimensions != 3 or surface_normals.shape != source_paths.shape or layers.shape != (points,) or direction_alpha.shape != (roots, points):
        raise ValueError("Fur source paths, normal, layer and alpha dimensions disagree")
    if not all(np.isfinite(value).all() for value in (source_paths, surface_normals, layers, direction_alpha)):
        raise ValueError("Non-finite fur geometry inputs")
    if np.any(direction_alpha < 0) or np.any(direction_alpha > 1):
        raise ValueError("FurDirMap alpha must lie in [0,1]")
    source_scalars = (parameters.length_intensity, parameters.gravity_strength, parameters.disable_rain_effect_on_material, parameters.artist_length_multiplier)
    if not all(math.isfinite(value) for value in source_scalars):
        raise ValueError("Non-finite source fur scalar")
    if parameters.length_intensity <= 0 or not 0 <= parameters.gravity_strength <= 1 or not 0 <= parameters.disable_rain_effect_on_material <= 1 or parameters.artist_length_multiplier <= 0:
        raise ValueError("Invalid source fur length/gravity or artist multiplier")
    up = np.asarray(parameters.source_world_up, dtype=np.float64)
    if up.shape != (3,) or not np.isfinite(up).all() or abs(np.linalg.norm(up)-1) > 1e-6:
        raise ValueError("Source world up must be an explicit normalized world-space vector")
    magnitudes = np.linalg.norm(surface_normals, axis=2, keepdims=True)
    if np.any(magnitudes <= 1e-12):
        raise ValueError("Zero surface normal in a shell track")
    normal = surface_normals / magnitudes
    layer = layers[None, :, None]
    bend = layer * (0.5 - np.sum(normal * up, axis=2, keepdims=True) * 0.5)
    # DisableRain gates the weather lerp factor, not the dry gravity endpoint.
    dry_gravity = parameters.gravity_strength
    gravity_normal = normal * (1-dry_gravity) - up * dry_gravity
    direction = normal * (1-bend) + gravity_normal * bend
    length = parameters.length_intensity * layer * .01 * direction_alpha[:, :, None] * parameters.artist_length_multiplier
    return source_paths + direction * length


def build_source_fur_curves(
    source_object: bpy.types.Object,
    series: tuple[ShellSeries, ...],
    direction_image: bpy.types.Image,
    color_image: bpy.types.Image,
    root_uv_name: str,
    color_uv_scale: tuple[float, float],
    color_uv_offset: tuple[float, float],
    color_tint: tuple[float, float, float, float],
    parameters: FurGeometryParameters,
    collection: bpy.types.Collection,
    helpers: ModuleType,
) -> FurCurveResult:
    """Create editable native Curves while preserving the complete source mesh.

    Object parenting follows the source object's root transform. Per-vertex
    skin deformation is not implemented; original shell skinning is retained.
    Radius and root stride are artist controls, not recovered strand density.
    """
    if source_object.type != 'MESH' or not series:
        raise ValueError("Fur geometry needs a mesh with verified shell series")
    if isinstance(parameters.root_stride, bool) or not isinstance(parameters.root_stride, int) or parameters.root_stride < 1 or parameters.radius_meters <= 0:
        raise ValueError("Artist root stride/radius must be positive")
    scalars = (parameters.radius_meters, parameters.length_intensity, parameters.gravity_strength, parameters.disable_rain_effect_on_material, parameters.artist_length_multiplier, *color_uv_scale, *color_uv_offset, *color_tint)
    if not all(math.isfinite(value) for value in scalars):
        raise ValueError("Fur parameters and BaseMap UV/tint must be finite")
    if len(color_uv_scale) != 2 or len(color_uv_offset) != 2 or len(color_tint) != 4:
        raise ValueError("Invalid BaseMap UV transform or color tint dimensions")
    for image in (direction_image, color_image):
        # Accessing pixels materializes Blender's lazily loaded packed image.
        pixel_count = len(image.pixels)
        if image.packed_file is None or image.channels != 4 or pixel_count != image.size[0] * image.size[1] * 4 or pixel_count == 0:
            raise ValueError(f"Required packed RGBA source image missing: {image.name}")
    if direction_image.colorspace_settings.name != 'Non-Color':
        raise ValueError("FurDirMap must be sampled as Non-Color")
    mesh = source_object.data
    uv = vertex_uvs(mesh, root_uv_name, 1e-6)
    matrix = np.asarray(source_object.matrix_world, dtype=np.float64)
    if np.max(np.abs(np.linalg.svd(matrix[:3, :3], compute_uv=False)-1)) > 1e-5:
        raise ValueError("Fur source object must have unit world scale; metric curve radius otherwise ambiguous")
    local = np.asarray([tuple(v.co) for v in mesh.vertices], dtype=np.float64)
    world = local @ matrix[:3, :3].T + matrix[:3, 3]
    normals = vertex_authored_normals(mesh, 1e-6) @ np.linalg.inv(matrix[:3, :3])
    indices = np.concatenate([np.asarray(row.indices, dtype=np.int32)[:, ::parameters.root_stride] for row in series], axis=1).T
    layers = np.asarray(series[0].parameters, dtype=np.float64)
    paths = world[indices]
    direction_samples = helpers.sample_image_pixels(helpers.image_pixel_array(direction_image), uv[indices].reshape(-1,2)).reshape((*indices.shape,4))
    color_uvs = uv[indices[:,0]] * np.asarray(color_uv_scale) + np.asarray(color_uv_offset)
    colors = helpers.sample_image_pixels(helpers.image_pixel_array(color_image), color_uvs) * np.asarray(color_tint)
    expanded = expanded_shell_paths(paths, normals[indices], layers, direction_samples[:,:,3], parameters)
    inverse = np.linalg.inv(matrix)
    positions = expanded @ inverse[:3,:3].T + inverse[:3,3]
    data = bpy.data.hair_curves.new(source_object.name + ' Source-derived Fur Curves')
    data.add_curves([len(layers)] * len(indices))
    attributes = (
        ('position','FLOAT_VECTOR','vector',positions.reshape(-1,3)),
        ('radius','FLOAT','value',np.full(indices.size,parameters.radius_meters)),
        ('UVMap','FLOAT2','vector',np.repeat(uv[indices[:,0]],len(layers),axis=0)),
        ('hair_source_color','FLOAT_COLOR','color',np.repeat(colors,len(layers),axis=0)),
        ('hair_source_alpha','FLOAT','value',np.ones(indices.size)),
        ('source_vertex_index','INT','value',indices.reshape(-1)),
        ('source_shell_layer','FLOAT','value',np.tile(layers,len(indices))),
    )
    for name, kind, field, values in attributes:
        attribute = data.attributes.get(name) or data.attributes.new(name, kind, 'POINT')
        attribute.data.foreach_set(field, array.array('i' if kind=='INT' else 'f',values.reshape(-1)))
    material = helpers.make_sampled_curve_material(source_object.name+' Fur source color preview')
    data.materials.append(material)
    obj = bpy.data.objects.new(data.name,data)
    collection.objects.link(obj)
    obj.parent = source_object
    obj.matrix_parent_inverse = Matrix.Identity(4)
    obj.matrix_basis = Matrix.Identity(4)
    obj['approximation'] = 'Source scalar/normal/gravity expansion interpreted in world meters; native shader offsets clip XYZ while retaining clip W and unexpanded shading varyings. Native DXBC shell input TEXCOORD1 is forwarded to fragment TEXCOORD4.'
    obj['skin_deformation'] = 'Root/object transform follows source parent. Fine skin deformation not connected; original skinned shell retained.'
    obj['source_object'] = source_object.name
    obj['artist_radius_meters'] = parameters.radius_meters
    obj['artist_root_stride'] = parameters.root_stride
    obj['artist_length_multiplier'] = parameters.artist_length_multiplier
    return FurCurveResult(obj.name,len(series),tuple(len(row.indices[0][::parameters.root_stride]) for row in series),len(indices),indices.size,float(np.linalg.norm(np.diff(paths,axis=1),axis=2).sum(axis=1).mean()),float(np.linalg.norm(np.diff(expanded,axis=1),axis=2).sum(axis=1).mean()),float(np.linalg.norm(expanded-paths,axis=2).max()),(float(direction_samples[:,:,3].min()),float(direction_samples[:,:,3].max())))


def _point_values(data: bpy.types.Curves, name: str, kind: str, field: str, dimensions: int) -> np.ndarray:
    attribute = data.attributes.get(name)
    if attribute is None or attribute.domain != 'POINT' or attribute.data_type != kind:
        raise ValueError(f"Required curve point attribute missing or incompatible: {data.name}/{name}/{kind}")
    values = np.empty(len(data.points) * dimensions, dtype=np.int32 if kind == 'INT' else np.float32)
    attribute.data.foreach_get(field, values)
    if not np.isfinite(values).all():
        raise ValueError(f"Non-finite curve attribute: {data.name}/{name}")
    return values.reshape(len(data.points), dimensions)


def copy_fur_radius_profile(source_curves: bpy.types.Object,
                            tip_radius_ratio: float,
                            profile_exponent: float,
                            collection: bpy.types.Collection) -> FurRadiusProfileResult:
    """Copy Curves with an explicit artist radius taper; preserve skin and paths.

    The source shader attenuates shell coverage by layer and sampled masks, but
    coverage is not a native strand radius. This host translation uses the
    verified source track coordinate only to parameterize an editable taper.
    It does not claim a recovered radius law, alter source-derived lengths, or
    add strands. A ratio of one preserves the input radius at every point.
    """
    if source_curves.type != 'CURVES' or not source_curves.data.curves:
        raise ValueError('Radius profiling requires nonempty native Curves')
    if not math.isfinite(tip_radius_ratio) or not 0 <= tip_radius_ratio <= 1:
        raise ValueError('Artist tip radius ratio must be finite and within [0,1]')
    if not math.isfinite(profile_exponent) or profile_exponent <= 0:
        raise ValueError('Artist radius profile exponent must be finite and positive')
    source_radius = _point_values(source_curves.data, 'radius', 'FLOAT', 'value', 1).reshape(-1)
    layers = _point_values(source_curves.data, 'source_shell_layer', 'FLOAT', 'value', 1).reshape(-1)
    if np.any(source_radius <= 0):
        raise ValueError('Source radius profiling requires positive input radii')
    profile = np.empty(len(layers), dtype=np.float32)
    coordinates = np.empty(len(layers), dtype=np.float32)
    for curve in source_curves.data.curves:
        start, count = curve.first_point_index, curve.points_length
        values = layers[start:start+count]
        if count < 2 or np.any(np.diff(values) < 0) or values[-1] <= values[0]:
            raise ValueError('Source shell track coordinate must be increasing with a nonzero root-to-tip span')
        coordinate = (values-values[0])/(values[-1]-values[0])
        coordinates[start:start+count] = coordinate
        profile[start:start+count] = tip_radius_ratio + (1-tip_radius_ratio)*np.power(1-coordinate, profile_exponent)
    result_radius = source_radius*profile
    candidate = source_curves.copy()
    candidate.data = source_curves.data.copy()
    candidate.name = source_curves.name + ' Artist Radius Taper'
    collection.objects.link(candidate)
    candidate.data.attributes['radius'].data.foreach_set('value', result_radius)
    for name, values in (('artist_fur_radius_coordinate', coordinates), ('artist_fur_radius_source', source_radius)):
        if candidate.data.attributes.get(name) is not None:
            raise ValueError(f'Radius profiling cannot overwrite an existing source field: {name}')
        attribute = candidate.data.attributes.new(name, 'FLOAT', 'POINT')
        attribute.data.foreach_set('value', values)
    tree = bpy.data.node_groups.new(candidate.name + ' Editable Artist Radius', 'GeometryNodeTree')
    tree.interface.new_socket(name='Geometry', in_out='INPUT', socket_type='NodeSocketGeometry')
    tree.interface.new_socket(name='Geometry', in_out='OUTPUT', socket_type='NodeSocketGeometry')
    tip_input = tree.interface.new_socket(name='Tip Radius Ratio', in_out='INPUT', socket_type='NodeSocketFloat')
    tip_input.default_value, tip_input.min_value, tip_input.max_value = tip_radius_ratio, 0., 1.
    tip_input.description = 'Explicit artist tip/root radius ratio; not native source shell coverage.'
    exponent_input = tree.interface.new_socket(name='Profile Exponent', in_out='INPUT', socket_type='NodeSocketFloat')
    exponent_input.default_value, exponent_input.min_value, exponent_input.max_value = profile_exponent, 0.001, 10.
    exponent_input.description = 'Explicit host taper shape along the source track coordinate.'
    group_input = tree.nodes.new('NodeGroupInput')
    group_input.location = (-900, 400)
    output = tree.nodes.new('NodeGroupOutput')
    output.location = (900, 400)
    coordinate_node = tree.nodes.new('GeometryNodeInputNamedAttribute')
    coordinate_node.data_type = 'FLOAT'
    coordinate_node.inputs['Name'].default_value = 'artist_fur_radius_coordinate'
    coordinate_node.location = (-900, 100)
    radius_node = tree.nodes.new('GeometryNodeInputNamedAttribute')
    radius_node.data_type = 'FLOAT'
    radius_node.inputs['Name'].default_value = 'artist_fur_radius_source'
    radius_node.location = (-900, -100)

    def radius_math(operation: str, left: float | bpy.types.NodeSocket,
                    right: float | bpy.types.NodeSocket, label: str) -> bpy.types.NodeSocket:
        node = tree.nodes.new('ShaderNodeMath')
        node.operation, node.label = operation, label
        node.location = (-600 + (len(tree.nodes)-4)*170, 100)
        for index, value in enumerate((left, right)):
            if isinstance(value, bpy.types.NodeSocket):
                tree.links.new(value, node.inputs[index])
            else:
                node.inputs[index].default_value = value
        return node.outputs[0]

    remaining = radius_math('SUBTRACT', 1., coordinate_node.outputs['Attribute'], 'Root-to-tip remaining span')
    shape = radius_math('POWER', remaining, group_input.outputs['Profile Exponent'], 'Artist taper shape')
    amplitude = radius_math('SUBTRACT', 1., group_input.outputs['Tip Radius Ratio'], 'Artist taper amplitude')
    tapered = radius_math('MULTIPLY', shape, amplitude, 'Taper amplitude times shape')
    ratio = radius_math('ADD', tapered, group_input.outputs['Tip Radius Ratio'], 'Artist radius ratio')
    radius = radius_math('MULTIPLY', radius_node.outputs['Attribute'], ratio, 'Preserved source radius times artist ratio')
    set_radius = tree.nodes.new('GeometryNodeSetCurveRadius')
    set_radius.location = (650, 400)
    tree.links.new(group_input.outputs['Geometry'], set_radius.inputs['Curve'])
    tree.links.new(radius, set_radius.inputs['Radius'])
    tree.links.new(set_radius.outputs['Curve'], output.inputs['Geometry'])
    modifier = candidate.modifiers.new('Editable Artist Fur Radius - Not Native Coverage', 'NODES')
    modifier.node_group = tree
    modifier[tip_input.identifier] = tip_radius_ratio
    modifier[exponent_input.identifier] = profile_exponent
    candidate['artist_radius_profile_source'] = source_curves.name
    candidate['artist_tip_radius_ratio'] = tip_radius_ratio
    candidate['artist_radius_profile_exponent'] = profile_exponent
    candidate['artist_radius_profile_scope'] = 'Host radius translation along source track coordinates; not native fragment shell coverage or recovered strand radius.'
    return FurRadiusProfileResult(candidate.name, modifier.name, len(candidate.data.curves), len(candidate.data.points),
                                  (float(source_radius.min()), float(source_radius.max())),
                                  (float(result_radius.min()), float(result_radius.max())),
                                  tip_radius_ratio, profile_exponent)


def interpolate_source_fur_guides(
    source_object: bpy.types.Object,
    source_guides: bpy.types.Object,
    root_material_slot: int,
    collection: bpy.types.Collection,
) -> FurInterpolationResult:
    """Add one explicit artist guide per source root triangle, with no noise.

    Positions, radius, UV and source colors are barycentric interpolations of
    complete existing source-derived guides. This is a host density control,
    not reconstructed native strand count or fragment FurMap coverage.
    """
    if source_object.type != 'MESH' or source_guides.type != 'CURVES' or source_guides.parent != source_object:
        raise ValueError('Source-surface interpolation requires native Curves parented to the verified source mesh')
    if source_guides.modifiers:
        raise ValueError('Interpolation requires undeformed bind guides; modifiers must be attached afterward')
    if isinstance(root_material_slot, bool) or not isinstance(root_material_slot, int) or not 0 <= root_material_slot < len(source_object.data.materials):
        raise ValueError('Root material slot is outside the source mesh slots')
    data = source_guides.data
    lengths = tuple(curve.points_length for curve in data.curves)
    if not lengths or len(set(lengths)) != 1 or lengths[0] < 2:
        raise ValueError('Source interpolation requires equal, nonempty shell guide point counts')
    points_per_guide = lengths[0]
    source_indices = _point_values(data, 'source_vertex_index', 'INT', 'value', 1).reshape(len(lengths), points_per_guide)
    root_lookup = {int(row[0]):index for index, row in enumerate(source_indices)}
    if len(root_lookup) != len(lengths):
        raise ValueError('Multiple source guides share one root vertex')
    faces = tuple(p for p in source_object.data.polygons if p.material_index == root_material_slot)
    if not faces or any(len(p.vertices) != 3 for p in faces):
        raise ValueError('Source-surface guide interpolation requires a nonempty triangulated root slot')
    if any(index not in root_lookup for p in faces for index in p.vertices):
        raise ValueError('Source guide roots do not cover every root-slot triangle vertex')
    triangle_guides = np.asarray([[root_lookup[index] for index in p.vertices] for p in faces], dtype=np.int32)
    base_point_indices = np.arange(len(data.points), dtype=np.int32).reshape(len(lengths), points_per_guide)
    triangle_points = base_point_indices[triangle_guides].transpose(0, 2, 1).reshape(-1, 3)
    originals = np.repeat(np.arange(len(data.points), dtype=np.int32)[:, None], 3, axis=1)
    sample_indices = np.concatenate((originals, triangle_points))
    sample_weights = np.concatenate((np.tile((1., 0., 0.), (len(data.points), 1)), np.full(triangle_points.shape, 1./3)))
    result_data = bpy.data.hair_curves.new(source_guides.name + ' Surface Interpolated')
    result_data.add_curves([points_per_guide] * (len(lengths) + len(faces)))
    attributes = (
        ('position', 'FLOAT_VECTOR', 'vector', 3),
        ('radius', 'FLOAT', 'value', 1),
        ('UVMap', 'FLOAT2', 'vector', 2),
        ('hair_source_color', 'FLOAT_COLOR', 'color', 4),
        ('hair_source_alpha', 'FLOAT', 'value', 1),
        ('source_shell_layer', 'FLOAT', 'value', 1),
    )
    for name, kind, field, dimensions in attributes:
        values = _point_values(data, name, kind, field, dimensions)
        interpolated = values[triangle_points].mean(axis=1)
        combined = np.concatenate((values, interpolated))
        attribute = result_data.attributes.get(name) or result_data.attributes.new(name, kind, 'POINT')
        attribute.data.foreach_set(field, combined.astype(np.float32).reshape(-1))
    for name, values in (('fur_skin_sample_indices', sample_indices), ('fur_skin_sample_weights', sample_weights)):
        attribute = result_data.attributes.new(name, 'FLOAT_VECTOR', 'POINT')
        attribute.data.foreach_set('vector', values.astype(np.float32).reshape(-1))
    for material in data.materials:
        result_data.materials.append(material)
    result = bpy.data.objects.new(result_data.name, result_data)
    collection.objects.link(result)
    result.parent = source_guides.parent
    result.matrix_parent_inverse = source_guides.matrix_parent_inverse.copy()
    result.matrix_basis = source_guides.matrix_basis.copy()
    result['source_guides'] = source_guides.name
    result['artist_density'] = 'Original guides plus one barycentric guide per verified source root triangle; not native strand density.'
    result['source_object'] = source_object.name
    for key in ('approximation', 'artist_radius_meters', 'artist_root_stride', 'artist_length_multiplier'):
        if key not in source_guides:
            raise ValueError(f'Source guide provenance field missing: {key}')
        result[key] = source_guides[key]
    return FurInterpolationResult(result.name, len(lengths), len(faces), len(result_data.points))


def attach_source_fur_skin_deformation(
    source_object: bpy.types.Object,
    bind_guides: bpy.types.Object,
    driven_curves: bpy.types.Object,
    collection: bpy.types.Collection,
) -> FurSkinBindingResult:
    """Use Blender's source Armature to deform full guide positions and offsets.

    The hidden proxy contains actual expanded curve bind points, not source
    surface points. Corresponding original vertex weights are copied exactly.
    Interpolated guide points sample three already-skinned guide positions,
    which preserves their barycentric relation during bone rotation. Geometry
    Nodes require no Python callbacks or scripted driver namespace on reopen.
    """
    if source_object.type != 'MESH' or bind_guides.type != 'CURVES' or driven_curves.type != 'CURVES':
        raise ValueError('Fur skin deformation requires source Mesh and native Curves')
    if bind_guides.parent != source_object or driven_curves.parent != source_object or driven_curves.modifiers:
        raise ValueError('Fur bind/driven curves require the same source parent and no existing driven modifiers')
    if any(abs(bind_guides.matrix_world[i][j]-driven_curves.matrix_world[i][j]) > 1e-6 for i in range(4) for j in range(4)):
        raise ValueError('Bind guides and driven Curves have different coordinate frames')
    armatures = tuple(mod for mod in source_object.modifiers if mod.type == 'ARMATURE' and mod.show_viewport and mod.show_render)
    if len(armatures) != 1 or any(mod.type != 'ARMATURE' and (mod.show_viewport or mod.show_render) for mod in source_object.modifiers):
        raise ValueError('Fur skin binding requires one enabled source Armature and no other source deformation modifiers')
    original = armatures[0]
    if original.object is None or original.object.type != 'ARMATURE' or not original.use_vertex_groups or original.use_bone_envelopes or original.use_multi_modifier:
        raise ValueError('Fur skin binding requires an explicit vertex-group Armature; envelopes/multi-modifier are unsupported')
    indices = _point_values(bind_guides.data, 'source_vertex_index', 'INT', 'value', 1).reshape(-1)
    if np.any(indices < 0) or np.any(indices >= len(source_object.data.vertices)):
        raise ValueError('Fur bind points reference source vertices outside the mesh')
    bind_positions = _point_values(bind_guides.data, 'position', 'FLOAT_VECTOR', 'vector', 3)
    samples = _point_values(driven_curves.data, 'fur_skin_sample_indices', 'FLOAT_VECTOR', 'vector', 3)
    weights = _point_values(driven_curves.data, 'fur_skin_sample_weights', 'FLOAT_VECTOR', 'vector', 3)
    if np.any(samples != np.floor(samples)) or np.any(samples < 0) or np.any(samples >= len(bind_positions)) or np.any(weights < 0) or np.max(np.abs(weights.sum(axis=1)-1)) > 1e-6:
        raise ValueError('Invalid fur skin sample indices or barycentric weights')
    source_weights = tuple(tuple((group.group, float(group.weight)) for group in vertex.groups if group.weight > 0) for vertex in source_object.data.vertices)
    bone_names = set(original.object.data.bones.keys())
    group_names = tuple(group.name for group in source_object.vertex_groups)
    for index in set(int(value) for value in indices):
        row = source_weights[index]
        if not row or any(group_names[group] not in bone_names for group, _ in row):
            raise ValueError(f'Fur source vertex has missing/unresolved positive bone weights: {source_object.name}/{index}')
    proxy_data = bpy.data.meshes.new(driven_curves.name + ' Skin Bind Points')
    proxy_data.from_pydata(bind_positions.tolist(), [], [])
    proxy = bpy.data.objects.new(proxy_data.name, proxy_data)
    collection.objects.link(proxy)
    proxy.parent = source_object
    proxy.matrix_parent_inverse = bind_guides.matrix_parent_inverse.copy()
    proxy.matrix_basis = bind_guides.matrix_basis.copy()
    for name in group_names:
        proxy.vertex_groups.new(name=name)
    for point, source_index in enumerate(indices):
        for group, weight in source_weights[int(source_index)]:
            proxy.vertex_groups[group].add((point,), weight, 'REPLACE')
    modifier = proxy.modifiers.new('Source Armature - Full Expanded Bind Points', 'ARMATURE')
    modifier.object = original.object
    for field in ('use_vertex_groups', 'use_bone_envelopes', 'use_deform_preserve_volume', 'vertex_group', 'invert_vertex_group', 'use_multi_modifier'):
        setattr(modifier, field, getattr(original, field))
    proxy.hide_render = True
    proxy.hide_set(True)
    proxy['source_object'] = source_object.name
    proxy['binding_scope'] = 'Original source vertex weights acting on complete source-derived curve bind points, including off-surface expansion.'
    tree = bpy.data.node_groups.new(driven_curves.name + ' Source Skin Transport', 'GeometryNodeTree')
    tree.interface.new_socket(name='Geometry', in_out='INPUT', socket_type='NodeSocketGeometry')
    tree.interface.new_socket(name='Geometry', in_out='OUTPUT', socket_type='NodeSocketGeometry')
    group_input = tree.nodes.new('NodeGroupInput')
    group_input.location = (-700, 380)
    output = tree.nodes.new('NodeGroupOutput')
    output.location = (700, 380)
    source = tree.nodes.new('GeometryNodeObjectInfo')
    source.transform_space = 'RELATIVE'
    source.inputs['Object'].default_value = proxy
    source.location = (-900, 80)
    position = tree.nodes.new('GeometryNodeInputPosition')
    position.location = (-900, -120)
    components = []
    for label, attribute_name in (('Indices', 'fur_skin_sample_indices'), ('Weights', 'fur_skin_sample_weights')):
        attribute = tree.nodes.new('GeometryNodeInputNamedAttribute')
        attribute.data_type = 'FLOAT_VECTOR'
        attribute.inputs['Name'].default_value = attribute_name
        attribute.location = (-900, -340-len(components)*260)
        separate = tree.nodes.new('ShaderNodeSeparateXYZ')
        separate.location = (-670, -340-len(components)*260)
        tree.links.new(attribute.outputs['Attribute'], separate.inputs['Vector'])
        components.append(separate)
    values = []
    for index in range(3):
        sample = tree.nodes.new('GeometryNodeSampleIndex')
        sample.data_type = 'FLOAT_VECTOR'
        sample.domain = 'POINT'
        sample.clamp = False
        sample.location = (-400, 160-index*240)
        tree.links.new(source.outputs['Geometry'], sample.inputs['Geometry'])
        tree.links.new(position.outputs['Position'], sample.inputs['Value'])
        tree.links.new(components[0].outputs[index], sample.inputs['Index'])
        scale = tree.nodes.new('ShaderNodeVectorMath')
        scale.operation = 'SCALE'
        scale.location = (-130, 160-index*240)
        tree.links.new(sample.outputs['Value'], scale.inputs[0])
        tree.links.new(components[1].outputs[index], scale.inputs['Scale'])
        values.append(scale.outputs['Vector'])
    total = values[0]
    for index, value in enumerate(values[1:]):
        add = tree.nodes.new('ShaderNodeVectorMath')
        add.operation = 'ADD'
        add.location = (100+index*200, 160)
        tree.links.new(total, add.inputs[0])
        tree.links.new(value, add.inputs[1])
        total = add.outputs['Vector']
    set_position = tree.nodes.new('GeometryNodeSetPosition')
    set_position.location = (480, 380)
    tree.links.new(group_input.outputs['Geometry'], set_position.inputs['Geometry'])
    tree.links.new(total, set_position.inputs['Position'])
    tree.links.new(set_position.outputs['Geometry'], output.inputs['Geometry'])
    driven_modifier = driven_curves.modifiers.new('Source Fine Bone Deformation', 'NODES')
    driven_modifier.node_group = tree
    driven_curves['skin_deformation'] = 'Native Geometry Nodes sample the source Armature-deformed expanded guide points; interpolated guides use barycentric posed-source interpolation.'
    driven_curves['fur_skin_proxy'] = proxy.name
    return FurSkinBindingResult(proxy.name, driven_modifier.name, original.object.name, len(bind_positions), len(driven_curves.data.points))
