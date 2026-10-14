"""Offline, same-camera SceneColor reconstruction of the verified VFXRefract branch.

The background is an explicitly rendered host image, never a texture default.
Refresh it after changing the camera, background, render settings or frame.
Scene exports can explicitly opt in with a camera-bound snapshot request.
Source and snapshot images are packed before rendering so Cycles uses Blender's
loaded image buffers rather than an external path it may not be able to read.
Refresh and render consecutive frames in the same process; reopening is not needed.
This module never reopens files implicitly.
"""
from __future__ import annotations

import copy
import hashlib
import json
import math
from pathlib import Path
from typing import TypedDict, cast

import bpy
from mathutils import Matrix

from blender_scene_effect_layers import _component, _math, _sample, _scale, _uv, _value, _vec, _xy
from blender_scene_materials import layout_shader_node_tree
from endfield_scene_material_contract import MaterialRecord
from endfield_scene_vfxrefract_contract import SceneColorSnapshotPlan, vfxrefract_snapshot_issues


def _source_issues(record: MaterialRecord, vertex_color_components: int) -> tuple[str, ...]:
    """Admit the sampled mask/RB branch without enabling unrelated shader features."""
    return vfxrefract_snapshot_issues(record, vertex_color_components)


class SceneColorSnapshotAudit(TypedDict):
    status: str
    camera: str
    receiver_objects: list[str]
    snapshot_metadata: str | None
    time_seconds: float
    boundary: str


def create_scene_color_camera(scene: bpy.types.Scene,
                              settings: SceneColorSnapshotPlan) -> bpy.types.Object:
    """Apply the caller's explicit perspective camera and bounded render settings."""
    camera = bpy.data.objects.new("Endfield_SceneColor_Camera", bpy.data.cameras.new("Endfield_SceneColor_Camera"))
    scene.collection.objects.link(camera)
    values = settings["camera_matrix_world"]
    camera.matrix_world = Matrix(tuple(tuple(values[row * 4 + column] for column in range(4)) for row in range(4)))
    camera.data.type = "PERSP"
    camera.data.lens = settings["lens_mm"]
    camera.data.clip_start = settings["clip_start"]
    camera.data.clip_end = settings["clip_end"]
    camera["endfield_fx_role"] = "Explicit offline SceneColor camera; refresh after camera, time, or scene changes"
    scene.camera = camera
    scene.render.engine = settings["engine"]
    scene.render.resolution_x, scene.render.resolution_y = settings["resolution"]
    scene.render.resolution_percentage = 100
    scene.cycles.samples = settings["samples"]
    scene.eevee.taa_render_samples = settings["samples"]
    frame = settings["time_seconds"] * scene.render.fps / scene.render.fps_base
    scene.frame_set(math.floor(frame), subframe=frame - math.floor(frame))
    return camera


def make_vfxrefract_capture_material(record: MaterialRecord,
                                     vertex_color_components: int) -> bpy.types.Material:
    """Keep validated receiver geometry separate until its background is captured."""
    issues = _source_issues(record, vertex_color_components)
    if issues:
        raise NotImplementedError(f"VFXRefract source branch unsupported: material={record['Name']}, issues={issues}")
    material = bpy.data.materials.new("EF_FX_REFRACT_CAPTURE_PENDING")
    material.use_nodes = True
    tree = material.node_tree
    tree.nodes.clear()
    transparent = tree.nodes.new("ShaderNodeBsdfTransparent")
    output = tree.nodes.new("ShaderNodeOutputMaterial")
    tree.links.new(transparent.outputs[0], output.inputs["Surface"])
    material["endfield_fx_source_material_json"] = json.dumps(record, sort_keys=True, allow_nan=False)
    material["endfield_fx_status"] = "awaiting_host_scene_color"
    material["endfield_fx_limitations"] = "[]"
    material["endfield_fx_source_color_components"] = vertex_color_components
    return material


def finalize_scene_vfxrefract(scene: bpy.types.Scene,
                             textures: dict[tuple[str, int], tuple[Path, str]],
                             settings: SceneColorSnapshotPlan,
                             output: Path) -> SceneColorSnapshotAudit:
    """Capture the completed scene once and replace all temporary receiver materials."""
    targets = tuple(obj for obj in scene.objects if any(
        slot.material is not None and slot.material.get("endfield_fx_status") == "awaiting_host_scene_color"
        for slot in obj.material_slots))
    audit: SceneColorSnapshotAudit = {
        "status": "not_applicable", "camera": scene.camera.name,
        "receiver_objects": [obj.name for obj in targets], "snapshot_metadata": None,
        "time_seconds": settings["time_seconds"],
        "boundary": "Offline same-camera background snapshot; refresh after camera, time, engine or background changes. Not native live SceneColor."
    }
    if not targets:
        return audit
    pending = {slot.material for obj in targets for slot in obj.material_slots}
    if any(material is None or material.get("endfield_fx_status") != "awaiting_host_scene_color" for material in pending):
        raise ValueError("SceneColor receiver geometry must exclusively use pending refraction materials")
    scene.cycles.samples = settings["samples"]
    scene.eevee.taa_render_samples = settings["samples"]
    image = render_host_scene_color(scene, scene.camera, targets, output)
    replacements: dict[bpy.types.Material, bpy.types.Material] = {}
    for material in pending:
        record = cast(MaterialRecord, json.loads(material["endfield_fx_source_material_json"]))
        replacements[material] = make_vfxrefract_snapshot_material(
            record, textures, settings["time_seconds"], material["endfield_fx_source_color_components"], image)
    for obj in targets:
        for slot in obj.material_slots:
            slot.material = replacements[slot.material]
    for material in pending:
        if material.users == 0:
            bpy.data.materials.remove(material)
    audit["status"] = "offline_camera_snapshot_refraction"
    audit["snapshot_metadata"] = image["endfield_host_scene_color"]
    scene["endfield_scene_color_snapshot"] = json.dumps(audit, sort_keys=True)
    return audit


def verify_scene_vfxrefract(scene: bpy.types.Scene, audit: SceneColorSnapshotAudit) -> None:
    """Verify the saved camera-bound receivers and both packed background samplers."""
    if any(material.get("endfield_fx_status") == "awaiting_host_scene_color"
           for obj in scene.objects if obj.type == "MESH"
           for material in obj.data.materials if material is not None):
        raise ValueError("Scene export still contains unfinished SceneColor receiver materials")
    if audit["status"] == "not_applicable":
        return
    if scene.camera is None or scene.camera.name != audit["camera"]:
        raise ValueError("SceneColor camera changed after snapshot creation")
    snapshot = json.loads(audit["snapshot_metadata"])
    actual_size = [int(scene.render.resolution_x * scene.render.resolution_percentage / 100),
                   int(scene.render.resolution_y * scene.render.resolution_percentage / 100)]
    if (scene.render.engine != snapshot["engine"] or scene.frame_current != snapshot["frame"]
            or scene.camera.data.type != snapshot["camera_type"] or actual_size != snapshot["image_size"]
            or abs(scene.frame_subframe - snapshot["subframe"]) > 1e-6
            or abs(scene.camera.data.lens - snapshot["lens"]) > 1e-5
            or max(abs(scene.camera.matrix_world[row][col] - snapshot["camera_world_matrix"][row][col])
                   for row in range(4) for col in range(4)) > 1e-5):
        raise ValueError("SceneColor camera, time or engine changed after snapshot creation")
    for name in audit["receiver_objects"]:
        obj = scene.objects.get(name)
        if obj is None or obj.type != "MESH":
            raise ValueError(f"SceneColor receiver missing after export: {name}")
        for slot in obj.material_slots:
            material = slot.material
            if material is None or material.get("endfield_host_scene_color") != audit["snapshot_metadata"]:
                raise ValueError(f"SceneColor receiver metadata changed: {name}")
            samplers = tuple(node for node in material.node_tree.nodes if node.get("endfield_host_scene_color_receiver"))
            if len(samplers) != 2 or any(node.image is None or not node.image.packed_file
                    or node.image.get("endfield_host_scene_color") != audit["snapshot_metadata"] for node in samplers):
                raise ValueError(f"SceneColor packed samplers changed: {name}")


def render_host_scene_color(scene: bpy.types.Scene, camera: bpy.types.Object,
                            excluded_draws: tuple[bpy.types.Object, ...],
                            output: Path) -> bpy.types.Image:
    """Render a linear EXR with every receiver hidden, then restore scene state."""
    if camera.type != "CAMERA" or camera not in tuple(scene.objects):
        raise ValueError("SceneColor camera must belong to the rendered scene")
    if not excluded_draws or any(obj not in tuple(scene.objects) for obj in excluded_draws):
        raise ValueError("SceneColor requires explicit receiver objects from the scene")
    if output.suffix.lower() != ".exr":
        raise ValueError("Host SceneColor must be a linear EXR file")
    output = output.resolve()
    output.parent.mkdir(parents=True, exist_ok=True)
    settings = scene.render.image_settings
    previous = (scene.camera, scene.render.filepath, settings.file_format,
                settings.color_mode, settings.color_depth)
    hidden = tuple((obj, obj.hide_render) for obj in excluded_draws)
    try:
        scene.camera = camera
        scene.render.filepath = str(output)
        settings.file_format, settings.color_mode, settings.color_depth = "OPEN_EXR", "RGBA", "32"
        for obj, _ in hidden:
            obj.hide_render = True
        bpy.ops.render.render(write_still=True, scene=scene.name)
    finally:
        scene.camera, scene.render.filepath, settings.file_format, settings.color_mode, settings.color_depth = previous
        for obj, value in hidden:
            obj.hide_render = value
    image = bpy.data.images.load(str(output), check_existing=False)
    image.colorspace_settings.name = "Non-Color"
    image.alpha_mode = "STRAIGHT"
    image.pack()
    image["endfield_host_scene_color"] = json.dumps({
        "format": "EndfieldHostSceneColorSnapshot/1", "camera": camera.name,
        "camera_world_matrix": [list(row) for row in camera.matrix_world],
        "camera_type": camera.data.type, "lens": camera.data.lens,
        "frame": scene.frame_current, "subframe": scene.frame_subframe, "engine": scene.render.engine,
        "image_size": list(image.size), "excluded_draws": [obj.name for obj in excluded_draws],
        "file": str(output), "sha256": hashlib.sha256(output.read_bytes()).hexdigest(),
        "scope": "Explicit host camera snapshot; refresh after scene/camera/frame changes",
    }, sort_keys=True)
    return image


def _background(tree: bpy.types.NodeTree, image: bpy.types.Image, label: str,
                uv: bpy.types.NodeSocket) -> bpy.types.NodeSocket:
    node = tree.nodes.new("ShaderNodeTexImage")
    node.name = node.label = label
    node.image, node.extension, node.interpolation = image, "EXTEND", "Linear"
    node["endfield_host_scene_color_receiver"] = True
    tree.links.new(uv, node.inputs["Vector"])
    return node.outputs["Color"]


def _pack_refraction_images(tree: bpy.types.NodeTree) -> None:
    """Keep this graph's loaded textures available to both render engines."""
    for node in tree.nodes:
        if node.type != "TEX_IMAGE":
            continue
        image = node.image
        if image is None:
            raise ValueError(f"Refraction texture node has no image: {node.name}")
        if image.source == "FILE" and image.packed_file is None:
            path = Path(bpy.path.abspath(image.filepath))
            if not path.is_file():
                raise FileNotFoundError(f"Refraction image file is missing: {image.name}: {path}")
        if not len(image.pixels) or not image.has_data or min(image.size) <= 0:
            raise ValueError(f"Refraction image has no loaded pixel buffer: {image.name}")
        if image.packed_file is None:
            image.pack()


def make_vfxrefract_snapshot_material(record: MaterialRecord,
        textures: dict[tuple[str, int], tuple[Path, str]], snapshot_time: float,
        vertex_color_components: int, scene_color: bpy.types.Image) -> bpy.types.Material:
    """Reconstruct source two-sample/max/coverage math against authenticated host RGB."""
    issues = _source_issues(record, vertex_color_components)
    if issues:
        raise NotImplementedError(f"VFXRefract source branch unsupported: material={record['Name']}, issues={issues}")
    snapshot = scene_color.get("endfield_host_scene_color")
    if not isinstance(snapshot, str) or scene_color.source != "FILE" or not scene_color.packed_file:
        raise ValueError("VFXRefract requires a packed, explicitly rendered host SceneColor snapshot")
    if type(snapshot_time) not in (int, float) or not math.isfinite(snapshot_time):
        raise ValueError("VFXRefract source time must be finite")
    source_json = json.dumps(record, sort_keys=True, allow_nan=False)
    name = "EF_FX_REFRACT_" + hashlib.sha256((source_json + snapshot).encode()).hexdigest()[:24]
    material = bpy.data.materials.new(name)
    material.use_nodes = True
    material.surface_render_method = "BLENDED"
    material.use_transparency_overlap = True
    material["endfield_fx_source_material_json"] = source_json
    material["endfield_fx_source_material"] = record["Name"]
    material["endfield_fx_source"] = record["Source"]
    material["endfield_fx_path_id"] = str(record["PathId"])
    material["endfield_fx_status"] = "offline_camera_snapshot_refraction"
    material["endfield_fx_branch"] = "vfxrefract_mask_rb_snapshot"
    material["endfield_host_scene_color"] = snapshot
    tree = material.node_tree
    tree.nodes.clear()
    uv = tree.nodes.new("ShaderNodeUVMap")
    uv.uv_map = "UVMap"
    _value(tree, "EF FX Source Time Seconds", snapshot_time)
    source = copy.deepcopy(record)
    source["Colors"]["_RefractTexUVSpeed"] = list(record["Colors"]["_RefractUVSpeed"])
    for prop in ("_RefractTex", "_MaskTex"):
        radians = math.radians(record["Floats"][prop + "UVRotate"])
        source["Colors"][prop + "UVRotateMat"] = [math.cos(radians), -math.sin(radians),
                                                 math.sin(radians), math.cos(radians)]
    zero = _vec(tree, "Source zero UV offset", "ADD", (0., 0., 0.), (0., 0., 0.))
    normal_uv = _uv(tree, source, "_RefractTex", uv.outputs[0], snapshot_time, zero, 0.0)
    sampled = _sample(tree, source, "_RefractTex", textures, normal_uv)
    color, alpha = sampled.rgba.rgb, sampled.rgba.alpha
    floats = record["Floats"]
    red = _component(tree, "Refract source red", color, "X")
    green = _component(tree, "Refract source green", color, "Y")
    red = _math(tree, "BiRefract subtract", "SUBTRACT",
                _math(tree, "BiRefract multiply", "MULTIPLY", red, 1 + floats["_Bi_Refract"]),
                floats["_Bi_Refract"])
    packed = _math(tree, "Packed normal Alpha x modified Red", "MULTIPLY", alpha, red)
    signed_x = _math(tree, "Packed normal X", "SUBTRACT",
                     _math(tree, "Packed normal X twice", "MULTIPLY", packed, 2.), 1.)
    signed_y = _math(tree, "Packed normal Y", "SUBTRACT",
                     _math(tree, "Packed normal Y twice", "MULTIPLY", green, 2.), 1.)
    normal_xy = _xy(tree, "Packed normal signed XY", signed_x, signed_y)
    if vertex_color_components == 4:
        vc = tree.nodes.new("ShaderNodeVertexColor")
        vc.layer_name = "EF_FX_COLOR"
        vertex_alpha = vc.outputs["Alpha"]
    else:
        vertex_alpha = _value(tree, "HOST_VERTEX_ALPHA (source COLOR absent)", 1.)
    factor = _math(tree, "Refract vertex alpha x intensity x TintAlpha", "MULTIPLY",
                   vertex_alpha, floats["_Intensity"] * floats["_TintColorAlpha"])
    instance_alpha = _value(tree, "HOST_INSTANCE_ALPHA", 1.)
    factor = _math(tree, "Explicit host instance alpha", "MULTIPLY", factor, instance_alpha)
    direction = tuple(record["Colors"]["_RefractDir"][:2]) + (0.,)
    vector = (_vec(tree, "Normal times source refract direction", "MULTIPLY", normal_xy, direction)
              if floats["_RefractIsNormal"] == 1 else _scale(
                  tree, "Scalar source refract direction", _vec(tree, "Source direction", "ADD", direction, (0.,0.,0.)), red))
    offset = _scale(tree, "Source screen UV offset", vector, factor)
    screen = tree.nodes.new("ShaderNodeTexCoord")
    screen.name = "Host current camera normalized screen UV"
    sample_uv = _vec(tree, "SceneColor primary UV", "ADD", screen.outputs["Window"], offset)
    primary = _background(tree, scene_color, "Host SceneColor primary", sample_uv)
    if floats["_RefractUseRBOffset"] == 1 and floats["_RefractIsNormal"] == 1:
        length = tree.nodes.new("ShaderNodeVectorMath")
        length.operation = "LENGTH"
        tree.links.new(offset, length.inputs[0])
        rb_factor = length.outputs["Value"]
    elif floats["_RefractUseRBOffset"] == 1:
        rb_factor = red
    else:
        rb_factor = _value(tree, "RB source unmodulated offset", 1.)
    rb_factor = _math(tree, "RB source intensity times vertex alpha", "MULTIPLY", rb_factor,
                      _math(tree, "RB vertex intensity", "MULTIPLY", vertex_alpha, floats["_Intensity"]))
    rb_direction = tuple(value * .01 for value in record["Colors"]["_RBOffset"][:2]) + (0.,)
    rb_uv = _vec(tree, "SceneColor RB UV", "ADD", sample_uv, _scale(
        tree, "RB source screen offset", _vec(tree, "RB direction", "ADD", rb_direction, (0.,0.,0.)), rb_factor))
    secondary = _background(tree, scene_color, "Host SceneColor RB", rb_uv)
    main_color = _vec(tree, "Source main RGB mask", "MULTIPLY", primary,
                      tuple(record["Colors"]["_RBMainColorMask"][:3]))
    offset_color = _vec(tree, "Source offset RGB mask", "MULTIPLY", secondary,
                        tuple(record["Colors"]["_RBOffsetColorMask"][:3]))
    maximum = _vec(tree, "Source per-channel max (not additive)", "MAXIMUM", main_color, offset_color)
    rgb = _vec(tree, "Source RBIntensity extrapolation", "ADD",
               _scale(tree, "Primary source weight", primary, 1 - floats["_RBIntensity"]),
               _scale(tree, "Masked max source weight", maximum, floats["_RBIntensity"]))
    mask_disturb = _scale(tree, "Source mask follows refraction", offset, floats["_MaskTexUseRefract"])
    mask_uv = _uv(tree, source, "_MaskTex", uv.outputs[0], snapshot_time, mask_disturb, 0.)
    mask = _sample(tree, source, "_MaskTex", textures, mask_uv)
    _pack_refraction_images(tree)
    rgb = _vec(tree, "Source mask RGB", "MULTIPLY", rgb, mask.rgba.rgb)
    coverage = mask.rgba.alpha
    if floats["_UseNearCameraFade"] == 1:
        geometry = tree.nodes.new("ShaderNodeNewGeometry")
        view = tree.nodes.new("ShaderNodeVectorTransform")
        view.vector_type, view.convert_from, view.convert_to = "POINT", "WORLD", "CAMERA"
        tree.links.new(geometry.outputs["Position"], view.inputs[0])
        depth = _math(tree, "Source absolute camera axial depth", "ABSOLUTE",
                      _component(tree, "Source camera Z", view.outputs[0], "Z"), 0.)
        for suffix in ("", "2"):
            start, end = floats["_NearCameraFadeDistanceStart"+suffix], floats["_NearCameraFadeDistanceEnd"+suffix]
            fade = _math(tree, "Source near camera fade " + suffix, "DIVIDE",
                         _math(tree, "Source near camera numerator " + suffix, "SUBTRACT", depth, start), end-start)
            fade.node.use_clamp = True
            coverage = _math(tree, "Source near camera coverage " + suffix, "MULTIPLY", coverage, fade)
    coverage = _math(tree, "Source coverage clamp", "MULTIPLY", coverage, 1.)
    coverage.node.use_clamp = True
    rgb = _vec(tree, "Source RGB minimum zero", "MAXIMUM", rgb, (0.,0.,0.))
    rgb = _vec(tree, "Source RGB maximum 1000", "MINIMUM", rgb, (1000.,1000.,1000.))
    emission = tree.nodes.new("ShaderNodeEmission")
    tree.links.new(rgb, emission.inputs["Color"])
    transparent = tree.nodes.new("ShaderNodeBsdfTransparent")
    mix = tree.nodes.new("ShaderNodeMixShader")
    tree.links.new(coverage, mix.inputs[0])
    tree.links.new(transparent.outputs[0], mix.inputs[1])
    tree.links.new(emission.outputs[0], mix.inputs[2])
    output = tree.nodes.new("ShaderNodeOutputMaterial")
    tree.links.new(mix.outputs[0], output.inputs["Surface"])
    material["endfield_fx_limitations"] = json.dumps([
        "Host background snapshot replaces native SceneColor buffer; camera/frame/background changes require refresh",
        "Source time is editable Blender seconds, not captured runtime global phase",
        "HOST_INSTANCE_ALPHA is explicit; native instance opacity unavailable",
        "Absent source COLOR uses editable HOST_VERTEX_ALPHA=1; no captured native missing-stream assertion",
        "Native motion-vector target, post exposure, distortion render queue and depth occlusion are not reconstructed",
        "Source and host snapshot images are packed for same-process rendering in both engines",
        *sampled.limitations, *mask.limitations])
    layout_shader_node_tree(tree)
    return material


def refresh_vfxrefract_scene_color(scene: bpy.types.Scene, camera: bpy.types.Object,
                                  target_draws: tuple[bpy.types.Object, ...],
                                  output: Path) -> bpy.types.Image:
    """Pack receiver textures and refresh the explicit same-camera background."""
    materials = {slot.material for obj in target_draws for slot in obj.material_slots}
    if any(material is None or material.get("endfield_fx_branch") != "vfxrefract_mask_rb_snapshot"
           for material in materials):
        raise ValueError("SceneColor refresh targets must exclusively use the snapshot refraction material")
    scene_audit = None
    if "endfield_scene_color_snapshot" in scene:
        scene_audit = cast(SceneColorSnapshotAudit, json.loads(scene["endfield_scene_color_snapshot"]))
        if set(scene_audit["receiver_objects"]) != {obj.name for obj in target_draws}:
            raise ValueError("Refresh must include every receiver in the exported SceneColor snapshot")
        times = {float(material.node_tree.nodes["EF FX Source Time Seconds"].outputs[0].default_value)
                 for material in materials}
        if len(times) != 1 or not all(math.isfinite(value) for value in times):
            raise ValueError("Exported SceneColor receivers require one finite source time for refresh")
        scene_audit = {**scene_audit, "camera": camera.name, "time_seconds": next(iter(times))}
    for material in materials:
        _pack_refraction_images(material.node_tree)
    image = render_host_scene_color(scene, camera, target_draws, output)
    for material in materials:
        nodes = [node for node in material.node_tree.nodes if node.get("endfield_host_scene_color_receiver")]
        if len(nodes) != 2:
            raise ValueError(f"Refraction snapshot must have two SceneColor samplers: material={material.name}")
        for node in nodes:
            node.image = image
        material["endfield_host_scene_color"] = image["endfield_host_scene_color"]
        legacy_limitations = {
            "Cycles same-process refreshed animation is not supported in the verified host; explicitly save/reopen before rendering",
            "Cycles refreshed animation requires explicit save/reopen in this verified host",
        }
        limitations = json.loads(material["endfield_fx_limitations"])
        material["endfield_fx_limitations"] = json.dumps(
            [value for value in limitations if value not in legacy_limitations])
    if scene_audit is not None:
        scene_audit["snapshot_metadata"] = image["endfield_host_scene_color"]
        scene["endfield_scene_color_snapshot"] = json.dumps(scene_audit, sort_keys=True)
    return image
