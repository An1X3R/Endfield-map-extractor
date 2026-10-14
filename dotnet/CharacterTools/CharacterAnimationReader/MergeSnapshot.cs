using System.Numerics;
using Ruri.RipperHook.Humanoid;
using System.Text.Json;

internal sealed record StaticTransform(string Path, float[] Position, float[] Rotation, float[] Scale);
internal sealed record StaticAudit(StaticTransform[] Transforms);
internal sealed record SourceLocalValue(float[] Position, float[] Rotation, float[] Scale);
internal sealed record SourceLocalFrame(float Time, SourceLocalValue[] Locals);
internal sealed record SourceHierarchyRow(int Index, string Path, int ParentIndex, SourceLocalValue RestLocal, string PositionSource, string RotationSource, string ScaleSource);
internal sealed record UnappliedScalar(int Track, string? Name, string Reason, float Minimum, float Maximum);

internal static class MergeSnapshot
{
    internal static void Export(string staticAuditPath, string aclDirectory, BodyFrame[] bodyFrames, NodePose[][] bodyWorldFrames, BoneMetric[] bones, HumanoidScalarBinding[] scalarBindings, ScalarFrame[] scalarFrames, string output, string animatorRoot, RootChannelPlan root, JsonSerializerOptions options)
    {
        if (Directory.Exists(output)) throw new IOException($"Merged output already exists: {output}.");
        Directory.CreateDirectory(output);
        StaticAudit source = JsonSerializer.Deserialize<StaticAudit>(File.ReadAllText(staticAuditPath), options) ?? throw new InvalidDataException("Static source audit missing.");
        BoundTrack[] bindings = JsonSerializer.Deserialize<BoundTrack[]>(File.ReadAllText(Path.Combine(aclDirectory, "bound_tracks.json")), options) ?? throw new InvalidDataException("ACL bound tracks missing.");
        TransformSample[] qvvf = AnimationFrameJson.Read<TransformSample[]>(Path.Combine(aclDirectory, "transform_samples.json"), options);
        CommonFrameDomain frameDomain = JsonSerializer.Deserialize<CommonFrameDomain>(File.ReadAllText(Path.Combine(aclDirectory, "frame_domain_verification.json")), options) ?? throw new InvalidDataException("Common frame-domain verification missing.");
        if (source.Transforms.Length == 0 || frameDomain.CommonFrameCount <= 0 || bodyFrames.Length != frameDomain.CommonFrameCount || bodyWorldFrames.Length != bodyFrames.Length || scalarFrames.Length != frameDomain.CommonFrameCount || qvvf.Length != frameDomain.Transform.OriginalFrameCount) throw new InvalidDataException($"Merged candidate dimensions disagree with the verified common frame domain: body={bodyFrames.Length}, scalar={scalarFrames.Length}, transform={qvvf.Length}, common={frameDomain.CommonFrameCount}.");
        
        var sourceIndices = source.Transforms.Select((row, index) => (row.Path, Index: index)).ToDictionary(row => row.Path, row => row.Index, StringComparer.Ordinal);
        var qvvfMap = bindings.ToDictionary(binding => binding.SourceTransformPath, StringComparer.Ordinal);
        var muscleMap = bones.ToDictionary(bone => animatorRoot + "/" + bone.Path, StringComparer.Ordinal);
        string[] conflicts = qvvfMap.Keys.Intersect(muscleMap.Keys, StringComparer.Ordinal).ToArray();
        if (conflicts.Length != 0) throw new InvalidDataException($"Conflicting qvvf/Humanoid bone paths: {string.Join(',', conflicts)}.");
        if (qvvfMap.Keys.Concat(muscleMap.Keys).Any(path => !sourceIndices.ContainsKey(path))) throw new InvalidDataException("Decoded animation path does not resolve to static source.");
        string hipsPath = animatorRoot + "/" + bones.Single(bone => bone.Slot == 0).Path;
        SourceHierarchyRow[] hierarchy = source.Transforms.Select((transform, index) =>
        {
            int slash = transform.Path.LastIndexOf('/');
            int parent = slash < 0 ? -1 : sourceIndices[transform.Path[..slash]];
            return new SourceHierarchyRow(index, transform.Path, parent, new SourceLocalValue(transform.Position, transform.Rotation, transform.Scale), transform.Path == animatorRoot ? "motion_root" : transform.Path == hipsPath ? "humanoid_body_root" : qvvfMap.ContainsKey(transform.Path) ? "acl_qvvf" : "source_static", transform.Path == animatorRoot ? "motion_root" : muscleMap.ContainsKey(transform.Path) ? "humanoid_muscle" : qvvfMap.ContainsKey(transform.Path) ? "acl_qvvf" : "source_static", qvvfMap.TryGetValue(transform.Path, out BoundTrack? scaleTrack) && scaleTrack.HasScale ? "acl_qvvf" : "source_static_no_scale_bindings");
        }).ToArray();
        var merged = new List<SourceLocalFrame>();
        float maximumPreservedScaleError = 0f;
        float maximumBodyRestTranslationDifference = 0f;
        for (int frame = 0; frame < frameDomain.CommonFrameCount; frame++)
        {
            BodyFrame body = bodyFrames[frame];
            float qvvfDifference = MathF.Abs(body.Time - qvvf[frame].Time);
            float scalarDifference = MathF.Abs(body.Time - scalarFrames[frame].Time);
            if (qvvfDifference > frameDomain.TimestampToleranceSeconds || scalarDifference > frameDomain.TimestampToleranceSeconds) throw new InvalidDataException($"Merged candidate timestamps disagree inside common range: frame={frame}, body_qvvf_difference={qvvfDifference}, body_scalar_difference={scalarDifference}, tolerance={frameDomain.TimestampToleranceSeconds}.");
            var bodyByPath = body.Nodes.ToDictionary(node => animatorRoot + "/" + node.Path, StringComparer.Ordinal);
            var bodyWorldByPath = bodyWorldFrames[frame].ToDictionary(node => animatorRoot + "/" + node.Path, StringComparer.Ordinal);
            var worldMatrices = new Matrix4x4[source.Transforms.Length];
            var worldRotations = new Quaternion[source.Transforms.Length];
            int animatorIndex = sourceIndices[animatorRoot];
            SourceLocalValue[] locals = source.Transforms.Select((transform, index) =>
            {
                float[] position = transform.Position;
                float[] rotation = transform.Rotation;
                float[] scale = transform.Scale;
                if (qvvfMap.TryGetValue(transform.Path, out BoundTrack? binding))
                {
                    TransformValue track = qvvf[frame].Tracks.Single(track => track.Track == binding.Track);
                    position = track.Translation;
                    rotation = track.Rotation;
                    if (binding.HasScale) scale = track.Scale;
                    else maximumPreservedScaleError = MathF.Max(maximumPreservedScaleError, transform.Scale.Zip(track.Scale).Max(pair => MathF.Abs(pair.First - pair.Second)));
                }
                if (muscleMap.ContainsKey(transform.Path))
                {
                    NodePose node = bodyByPath[transform.Path];
                    NodePose desired = bodyWorldByPath[transform.Path];
                    int parent = hierarchy[index].ParentIndex;
                    if (parent >= index || animatorIndex >= index) throw new InvalidDataException($"Human mapping requires an evaluated source parent and Animator root: {transform.Path}.");
                    Quaternion animatorRotation = worldRotations[animatorIndex];
                    Quaternion desiredWorldRotation = Quaternion.Normalize(animatorRotation * new Quaternion(desired.Rotation[0], desired.Rotation[1], desired.Rotation[2], desired.Rotation[3]));
                    Quaternion parentRotation = parent < 0 ? Quaternion.Identity : worldRotations[parent];
                    Quaternion mapped = Quaternion.Normalize(Quaternion.Inverse(parentRotation) * desiredWorldRotation);
                    rotation = [mapped.X, mapped.Y, mapped.Z, mapped.W];
                    if (transform.Path == hipsPath)
                    {
                        Vector3 desiredWorldPosition = Vector3.Transform(new Vector3(desired.Position[0], desired.Position[1], desired.Position[2]), worldMatrices[animatorIndex]);
                        Matrix4x4 parentMatrix = parent < 0 ? Matrix4x4.Identity : worldMatrices[parent];
                        if (!Matrix4x4.Invert(parentMatrix, out Matrix4x4 inverseParent)) throw new InvalidDataException($"Singular Human parent transform: {transform.Path}.");
                        Vector3 mappedPosition = Vector3.Transform(desiredWorldPosition, inverseParent);
                        position = [mappedPosition.X, mappedPosition.Y, mappedPosition.Z];
                    }
                    else maximumBodyRestTranslationDifference = MathF.Max(maximumBodyRestTranslationDifference, transform.Position.Zip(node.Position).Max(pair => MathF.Abs(pair.First - pair.Second)));
                }
                if (transform.Path == animatorRoot)
                {
                    position = body.MotionPosition;
                    rotation = body.MotionRotation;
                }
                if (position.Length != 3 || rotation.Length != 4 || scale.Length != 3 || position.Concat(rotation).Concat(scale).Any(value => !float.IsFinite(value))) throw new InvalidDataException($"Invalid merged local at frame {frame}, path {transform.Path}.");
                int parentIndex = hierarchy[index].ParentIndex;
                if (parentIndex >= index) throw new InvalidDataException($"Source hierarchy is not parent-first: {transform.Path}.");
                Quaternion localRotation = new(rotation[0], rotation[1], rotation[2], rotation[3]);
                Matrix4x4 localMatrix = Matrix4x4.CreateScale(scale[0], scale[1], scale[2]) * Matrix4x4.CreateFromQuaternion(localRotation) * Matrix4x4.CreateTranslation(position[0], position[1], position[2]);
                worldMatrices[index] = parentIndex < 0 ? localMatrix : localMatrix * worldMatrices[parentIndex];
                worldRotations[index] = parentIndex < 0 ? localRotation : Quaternion.Normalize(worldRotations[parentIndex] * localRotation);
                return new SourceLocalValue(position.ToArray(), rotation.ToArray(), scale.ToArray());
            }).ToArray();
            merged.Add(new SourceLocalFrame(body.Time, locals));
        }
        var consumed = bones.SelectMany(bone => bone.Tracks).Where(track => track >= 0).Concat(new[] { root.RootTx, root.RootTy, root.RootTz, root.RootQx, root.RootQy, root.RootQz, root.RootQw, root.MotionTx, root.MotionTy, root.MotionTz, root.MotionQx, root.MotionQy, root.MotionQz, root.MotionQw }.Where(track => track >= 0)).ToHashSet();
        UnappliedScalar[] unapplied = scalarBindings.Where(binding => !consumed.Contains(binding.Track)).Select(binding => new UnappliedScalar(binding.Track, binding.AttributeName, binding.CustomType == 0 ? "Animator_parameter_runtime_consumer_not_applied" : "Humanoid_scalar_without_applied_solver_channel_IK_TDOF_or_missing_node", scalarFrames.Min(frame => frame.Values[binding.Track]), scalarFrames.Max(frame => frame.Values[binding.Track]))).ToArray();
        Write(output, "source_hierarchy.json", hierarchy, options);
        AnimationFrameJson.Write(output, "source_local_frames.json", merged);
        Write(output, "unapplied_scalar_channels.json", unapplied, options);
        Write(output, "frame_domain_verification.json", frameDomain, options);
        File.Copy(Path.Combine(aclDirectory, "float_samples.json"), Path.Combine(output, "preserved_scalar_frames.json"));
        File.Copy(Path.Combine(aclDirectory, "scalar_bindings.json"), Path.Combine(output, "preserved_scalar_bindings.json"));
        var receipt = new { status = "single_clip_complete_source_hierarchy_candidate_rendering_unverified", frames = bodyFrames.Length, time_domain = frameDomain, hierarchy_nodes = hierarchy.Length, qvvf_nodes = qvvfMap.Count, qvvf_scale_nodes = bindings.Count(binding => binding.HasScale), humanoid_rotation_nodes = muscleMap.Count, hips_translation_nodes = 1, motion_root_nodes = 1, rotation_conflicts = conflicts, animated_rotation_nodes = qvvfMap.Count + muscleMap.Count + 1, preserved_static_rotation_nodes = hierarchy.Count(row => row.RotationSource == "source_static"), consumed_scalar_channels = consumed.Count, unapplied_scalar_channels = unapplied.Length, maximum_preserved_source_scale_vs_unbound_acl_default_scale_difference = maximumPreservedScaleError, maximum_human_vs_source_static_nonhips_translation_difference = maximumBodyRestTranslationDifference, frame_schema = "Array of {time, locals:[{position:[x,y,z],rotation:[x,y,z,w],scale:[x,y,z]}]}; locals index equals source_hierarchy.index; hierarchy stores full unique source path and parent index.", space = "Absolute parent-local source Unity transforms in meters; quaternion xyzw; no export reflection applied; scales consume bound ACL scale tracks and preserve source static on all unbound nodes.", motion_owner_path = animatorRoot, root_policy = "Convert solved Human world rotations and Hips world translation into the actual full-hierarchy parent space; Motion channels to Animator object root. Do not also apply RootT/RootQ or duplicate RootMotion buffer; stationary/moving Motion follows this actual clip.", model_converter = "ModelConverter.cs SetFrame: t=(-x,y,z), q=(x,-y,-z,w), s unchanged. Matrix reflection S=diag(-1,1,1,1), M_export=S*M_source*S. FBX-to-Blender import and pose rest basis require measured binder calibration.", source_audit = Path.GetFullPath(staticAuditPath), validation_boundary = "Whole actual source hierarchy is populated, but limb IK/Animator parameters/Unity constraints are retained as unapplied data; this is not full runtime playback parity." };
        Write(output, "receipt.json", receipt, options);
    }
    private static void Write<T>(string output, string filename, T value, JsonSerializerOptions options) => File.WriteAllText(Path.Combine(output, filename), JsonSerializer.Serialize(value, options));
}
