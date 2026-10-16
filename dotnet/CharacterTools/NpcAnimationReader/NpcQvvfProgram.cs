using System.Text.Json;
using AnimeStudio;

internal enum NpcRootLayoutKind
{
    Unsupported,
    RootOnly,
    MotionAndRoot
}

internal sealed record PreservedScalarChannel(int Track, int Binding, uint Attribute, int CustomType, string? Name, string Reason, float Minimum, float Maximum);
internal sealed record MergedFrameDomain(string Status, int FrameCount, int ExportedFrameCount, float ExportedStartTime, float ExportedEndTime, float TimestampToleranceSeconds, BufferFrameDomain Transform, BufferFrameDomain RootMotion, BufferFrameDomain Float, CommonFrameDomain CommonBufferDomain, RawAclBufferHashes RawAclBufferSha256, bool RawAclBuffersPreserved, string ExportPolicy);

/// <summary>Applies exact qvvf bindings while retaining unsupported scalar channels as source evidence.</summary>
internal static class NpcQvvfProgram
{
    private static readonly string[] MotionAttributes = ["MotionT.x", "MotionT.y", "MotionT.z", "MotionQ.x", "MotionQ.y", "MotionQ.z", "MotionQ.w"];
    private static readonly string[] RootAttributes = ["RootT.x", "RootT.y", "RootT.z", "RootQ.x", "RootQ.y", "RootQ.z", "RootQ.w"];
    private static readonly float[] IdentityTransform = [0f, 0f, 0f, 0f, 0f, 0f, 1f];
    private const float IdentityTolerance = 1e-6f;

    internal static NpcRootLayoutKind IdentifyLayout(ScalarBinding[] bindings, JsonElement clipAudit) =>
        IdentifyLayout(bindings, clipAudit.GetProperty("root_track_count").GetInt32(), clipAudit.GetProperty("m_legacy").GetBoolean(),
            clipAudit.GetProperty("m_has_motion_float_curves").GetBoolean(), clipAudit.GetProperty("m_has_generic_root_transform").GetBoolean());

    internal static bool HasIndependentRootSignature(AnimationClip clip, ScalarBinding[] bindings)
        => HasIndependentRootSignature(bindings, clip.m_AclCompressedBuffer.RootTrackCount);

    internal static bool HasIndependentRootSignature(ScalarBinding[] bindings, int rootTrackCount)
    {
        if (rootTrackCount <= 0) return false;
        bool hasRootOrMotionChannels = bindings.Any(binding => binding.CustomType == 8 && (IsRootAttribute(binding.AttributeName) || IsMotionAttribute(binding.AttributeName)));
        return hasRootOrMotionChannels && !HasHumanoidMuscleChannels(bindings);
    }

    internal static bool HasHumanoidMuscleChannels(ScalarBinding[] bindings) =>
        bindings.Any(binding => binding.CustomType == 8 && !IsRootAttribute(binding.AttributeName) && !IsMotionAttribute(binding.AttributeName));

    internal static NpcRootLayoutKind IdentifyLayout(AnimationClip clip, ScalarBinding[] bindings) =>
        IdentifyLayout(bindings, clip.m_AclCompressedBuffer.RootTrackCount, clip.m_Legacy, clip.m_HasMotionFloatCurves, clip.m_HasGenericRootTransform);

    internal static void ValidateRootSamples(FloatSample[] scalars, FloatSample[] roots, int expectedRootTracks)
    {
        if (scalars.Length == 0 || roots.Length == 0)
            throw new InvalidDataException($"Independent NPC root buffers must contain samples: float_frames={scalars.Length}, root_motion_frames={roots.Length}.");
        if (expectedRootTracks <= 0 || roots.Any(frame => frame.Values.Length != expectedRootTracks))
            throw new InvalidDataException($"Independent root-motion frame widths differ from the authenticated header: expected_tracks={expectedRootTracks}, frame_widths={string.Join(',', roots.Select(frame => frame.Values.Length).Distinct())}.");
        for (int frame = 0; frame < roots.Length; frame++)
            for (int track = 0; track < roots[frame].Values.Length; track++)
                if (!float.IsFinite(roots[frame].Values[track]))
                    throw new InvalidDataException($"Independent root-motion sample is not finite: frame={frame}, track={track}, value={roots[frame].Values[track]}.");
        for (int frame = 0; frame < scalars.Length; frame++)
            for (int track = 0; track < scalars[frame].Values.Length; track++)
                if (!float.IsFinite(scalars[frame].Values[track]))
                    throw new InvalidDataException($"Independent float sample is not finite: frame={frame}, track={track}, value={scalars[frame].Values[track]}.");
    }

    internal static ScalarBinding[] GetMotionScalarBindings(ScalarBinding[] bindings) => RequireChannelGroup(bindings, MotionAttributes, 0);

    internal static ScalarBinding[] GetRootScalarBindings(ScalarBinding[] bindings) => RequireChannelGroup(bindings, RootAttributes, 7);

    private static NpcRootLayoutKind IdentifyLayout(ScalarBinding[] bindings, int rootTracks, bool legacy, bool hasMotionFloatCurves, bool hasGenericRootTransform)
    {
        if (legacy || hasGenericRootTransform) return NpcRootLayoutKind.Unsupported;
        ScalarBinding[]? rootBindings = FindChannelGroup(bindings, RootAttributes, 7);
        if (rootBindings is null) return NpcRootLayoutKind.Unsupported;

        ScalarBinding[]? motionBindings = FindChannelGroup(bindings, MotionAttributes, 0);
        if (rootTracks == 14 && !hasMotionFloatCurves && bindings.Length == RootAttributes.Length && motionBindings is null)
            return NpcRootLayoutKind.RootOnly;
        if (rootTracks == 21 && hasMotionFloatCurves && motionBindings is not null && HasOnlyUnclassifiedCustomScalars(bindings, rootBindings, motionBindings))
            return NpcRootLayoutKind.MotionAndRoot;
        return NpcRootLayoutKind.Unsupported;
    }

    private static bool HasOnlyUnclassifiedCustomScalars(ScalarBinding[] bindings, ScalarBinding[] rootBindings, ScalarBinding[] motionBindings)
    {
        var recognizedTracks = rootBindings.Concat(motionBindings).Select(binding => binding.Track).ToHashSet();
        return bindings.Where(binding => !recognizedTracks.Contains(binding.Track))
            .All(binding => binding.CustomType == 0 && binding.AttributeName is null);
    }

    private static bool IsRootAttribute(string? attributeName) =>
        attributeName is not null && (attributeName.StartsWith("RootT.", StringComparison.Ordinal) || attributeName.StartsWith("RootQ.", StringComparison.Ordinal));

    private static bool IsMotionAttribute(string? attributeName) =>
        attributeName is not null && (attributeName.StartsWith("MotionT.", StringComparison.Ordinal) || attributeName.StartsWith("MotionQ.", StringComparison.Ordinal));

    private static ScalarBinding[]? FindChannelGroup(ScalarBinding[] bindings, string[] names, uint firstAttribute)
    {
        ScalarBinding[][] rows = names.Select(name => bindings.Where(binding => binding.CustomType == 8 && binding.AttributeName == name).ToArray()).ToArray();
        if (rows.Any(group => group.Length != 1)) return null;
        ScalarBinding[] result = rows.Select(group => group[0]).ToArray();
        if (result.Select((binding, index) => binding.Attribute == firstAttribute + (uint)index).Any(valid => !valid) ||
            result.Select(binding => binding.Track).Distinct().Count() != result.Length)
            return null;
        return result;
    }

    private static ScalarBinding[] RequireChannelGroup(ScalarBinding[] bindings, string[] names, uint firstAttribute) =>
        FindChannelGroup(bindings, names, firstAttribute) ?? throw new InvalidDataException($"Expected one exact {names[0][..names[0].IndexOf('.')]} T/Q scalar group: attributes={string.Join(',', names)}.");

    private static void ValidateIdentityAnimatorRoot(StaticTransform root, string path)
    {
        if (root.Position.Length != 3 || root.Rotation.Length != 4 || root.Scale.Length != 3)
            throw new InvalidDataException($"NPC Animator root transform dimensions are invalid: path={path}, position={root.Position.Length}, rotation={root.Rotation.Length}, scale={root.Scale.Length}.");
        float[] pose = root.Position.Concat(root.Rotation).ToArray();
        for (int index = 0; index < pose.Length; index++)
            if (!float.IsFinite(pose[index]) || MathF.Abs(pose[index] - IdentityTransform[index]) > IdentityTolerance)
                throw new InvalidDataException($"NPC Animator object-root rest must remain identity: path={path}, component={index}, value={pose[index]}, tolerance={IdentityTolerance}.");
        if (root.Scale.Any(value => !float.IsFinite(value) || value != 1f))
            throw new InvalidDataException($"NPC Animator object-root rest must retain unit scale: path={path}.");
    }

    internal static void Export(AnimationRequest request, JsonSerializerOptions options)
    {
        string native = Path.Combine(request.Output, "native");
        ScalarBinding[] scalarBindings = Read<ScalarBinding[]>(Path.Combine(native, "scalar_bindings.json"), options);
        FloatSample[] scalars = AnimationFrameJson.Read<FloatSample[]>(Path.Combine(native, "float_samples.json"), options);
        FloatSample[] roots = AnimationFrameJson.Read<FloatSample[]>(Path.Combine(native, "root_motion_samples.json"), options);
        JsonDocument clipAudit = JsonDocument.Parse(File.ReadAllText(Path.Combine(native, "clip_audit.json")));
        bool legacy = clipAudit.RootElement.GetProperty("m_legacy").GetBoolean();
        bool hasMotionFloatCurves = clipAudit.RootElement.GetProperty("m_has_motion_float_curves").GetBoolean();
        bool hasGenericRootTransform = clipAudit.RootElement.GetProperty("m_has_generic_root_transform").GetBoolean();
        int rootTrackCount = clipAudit.RootElement.GetProperty("root_track_count").GetInt32();
        NpcRootLayoutKind layout = IdentifyLayout(scalarBindings, rootTrackCount, legacy, hasMotionFloatCurves, hasGenericRootTransform);
        if (layout == NpcRootLayoutKind.Unsupported)
            throw new InvalidDataException("NPC qvvf export received a scalar/root layout that was not source-validated.");
        ValidateRootSamples(scalars, roots, rootTrackCount);

        TransformSample[] qvvf = AnimationFrameJson.Read<TransformSample[]>(Path.Combine(native, "transform_samples.json"), options);
        BoundTrack[] bindings = Read<BoundTrack[]>(Path.Combine(native, "bound_tracks.json"), options);
        RootTransformBindingAudit rootTransformBinding = Read<RootTransformBindingAudit>(Path.Combine(native, "root_transform_binding.json"), options);
        CommonFrameDomain domain = Read<CommonFrameDomain>(Path.Combine(native, "frame_domain_verification.json"), options);
        ValidateRootTrackApplication(rootTransformBinding, bindings, request.AnimatorRootPath);
        StaticAudit source = Read<StaticAudit>(request.SourceAudit, options);
        if (domain.CommonFrameCount <= 0 || qvvf.Length != domain.Transform.OriginalFrameCount || scalars.Length != domain.Float.OriginalFrameCount || roots.Length != domain.RootMotion.OriginalFrameCount || bindings.Length == 0)
            throw new InvalidDataException("NPC qvvf-only inputs disagree with the verified complete frame domain or contain no mapped transform tracks.");
        var sourceIndices = source.Transforms.Select((row, index) => (row.Path, Index: index)).ToDictionary(row => row.Path, row => row.Index, StringComparer.Ordinal);
        if (!sourceIndices.TryGetValue(request.AnimatorRootPath, out int rootIndex)) throw new InvalidDataException("Exact NPC Animator root is absent from source hierarchy.");
        StaticTransform sourceRoot = source.Transforms[rootIndex];
        ValidateIdentityAnimatorRoot(sourceRoot, request.AnimatorRootPath);
        var mapped = bindings.ToDictionary(binding => binding.SourceTransformPath, StringComparer.Ordinal);
        if (mapped.ContainsKey(request.AnimatorRootPath)) throw new InvalidDataException("Stationary NPC qvvf-only source root is also transform-bound; combined root ownership is not implemented.");
        SourceHierarchyRow[] hierarchy = source.Transforms.Select((row, index) =>
        {
            int slash = row.Path.LastIndexOf('/');
            int parent = slash < 0 ? -1 : sourceIndices[row.Path[..slash]];
            if (parent >= index) throw new InvalidDataException($"NPC source hierarchy must be parent-first: path={row.Path}, parent={parent}, index={index}.");
            bool bound = mapped.TryGetValue(row.Path, out BoundTrack? track);
            return new SourceHierarchyRow(index, row.Path, parent, new SourceLocalValue(row.Position, row.Rotation, row.Scale), bound && track!.HasPosition ? "acl_qvvf" : "source_static", bound && track!.HasRotation ? "acl_qvvf" : "source_static", bound && track!.HasScale ? "acl_qvvf" : "source_static_no_scale_bindings");
        }).ToArray();
        var frames = new List<SourceLocalFrame>(qvvf.Length);
        for (int frame = 0; frame < qvvf.Length; frame++)
        {
            SourceLocalValue[] locals = source.Transforms.Select(row =>
            {
                if (!mapped.TryGetValue(row.Path, out BoundTrack? binding)) return new SourceLocalValue(row.Position.ToArray(), row.Rotation.ToArray(), row.Scale.ToArray());
                TransformValue value = qvvf[frame].Tracks[binding.Track];
                if (value.Track != binding.Track) throw new InvalidDataException($"NPC qvvf track index disagrees: frame={frame}, expected={binding.Track}, actual={value.Track}.");
                return new SourceLocalValue(binding.HasPosition ? value.Translation.ToArray() : row.Position.ToArray(), binding.HasRotation ? value.Rotation.ToArray() : row.Rotation.ToArray(), binding.HasScale ? value.Scale.ToArray() : row.Scale.ToArray());
            }).ToArray();
            frames.Add(new SourceLocalFrame(qvvf[frame].Time, locals));
        }
        string output = Path.Combine(request.Output, "humanoid", "merged");
        if (Directory.Exists(output)) throw new IOException($"NPC merged output already exists: {output}.");
        Directory.CreateDirectory(output);
        Write(output, "source_hierarchy.json", hierarchy, options);
        AnimationFrameJson.Write(output, "source_local_frames.json", frames);
        MergedFrameDomain mergedDomain = CreateMergedFrameDomain(domain, qvvf);
        Write(output, "frame_domain_verification.json", mergedDomain, options);
        Write(output, "unapplied_scalar_channels.json", scalarBindings.Select(binding => new PreservedScalarChannel(binding.Track, binding.Binding, binding.Attribute, binding.CustomType, binding.AttributeName,
            "Source scalar was retained as evidence without assigning transform or parameter semantics.", scalars.Min(frame => frame.Values[binding.Track]), scalars.Max(frame => frame.Values[binding.Track]))).ToArray(), options);
        File.Copy(Path.Combine(native, "scalar_bindings.json"), Path.Combine(output, "preserved_scalar_bindings.json"));
        File.Copy(Path.Combine(native, "float_samples.json"), Path.Combine(output, "preserved_scalar_frames.json"));
        var receipt = new { status = "exact_selected_avatar_qvvf_with_root_transform_binding_applied_once", frames = qvvf.Length, hierarchy_nodes = hierarchy.Length, qvvf_nodes = bindings.Length, humanoid_rotation_nodes = 0, humanoid_reconstruction_applied = false, source_audit = Path.GetFullPath(request.SourceAudit), motion_owner_path = request.AnimatorRootPath, root_layout = layout == NpcRootLayoutKind.MotionAndRoot ? "motion_and_root_scalar_groups_with_explicit_qvvf_root_mapping" : "root_scalar_group_with_explicit_qvvf_root_mapping", root_transform_binding = rootTransformBinding, root_transform_application_count = 1, root_buffer_semantics = "QVVF root position and rotation are applied once through their authenticated transform binding. RootMotion and float buffers have no authenticated transform owner in this export; they remain preserved as decoded source evidence and are not applied or used to synthesize a second transform.", scalar_channel_semantics = "All float scalar channels are retained with source attributes and ranges; no independent runtime semantics are assigned.", animator_root_identity_tolerance = IdentityTolerance, raw_root_buffer_preserved = true, time_domain = new { exported_transform_frames = qvvf.Length, shared_buffer_prefix_frames = domain.CommonFrameCount, transform_frames_after_shared_prefix = domain.Transform.ExcludedTailFrames, root_motion_frames_after_shared_prefix = domain.RootMotion.ExcludedTailFrames, float_frames_after_shared_prefix = domain.Float.ExcludedTailFrames, common_buffer_domain = domain }, space = "Absolute parent-local source Unity transforms in meters; quaternion xyzw; exact QVVF local channels and source rest for unbound components.", validation_boundary = "Authenticated source bindings and numerical QVVF merge only. This does not establish native Animator root extraction policy, rendered playback, controller state, runtime IK, coordinate conversion, or final Generic runtime equivalence." };
        Write(output, "receipt.json", receipt, options);
        Write(Path.GetDirectoryName(output)!, "receipt.json", receipt, options);
    }

    private static MergedFrameDomain CreateMergedFrameDomain(CommonFrameDomain commonDomain, TransformSample[] qvvf)
    {
        if (qvvf.Length == 0 || qvvf.Length != commonDomain.Transform.OriginalFrameCount)
            throw new InvalidDataException($"Merged QVVF timeline disagrees with its authenticated transform buffer: merged_frames={qvvf.Length}, source_frames={commonDomain.Transform.OriginalFrameCount}.");
        if (qvvf[0].Time != commonDomain.Transform.SourceStartTime || qvvf[^1].Time != commonDomain.Transform.SourceEndTime)
            throw new InvalidDataException($"Merged QVVF endpoints disagree with its authenticated transform buffer: merged_start={qvvf[0].Time}, source_start={commonDomain.Transform.SourceStartTime}, merged_end={qvvf[^1].Time}, source_end={commonDomain.Transform.SourceEndTime}.");
        return new MergedFrameDomain(
            "generic_full_qvvf_timestamp_domain_verified",
            qvvf.Length,
            qvvf.Length,
            qvvf[0].Time,
            qvvf[^1].Time,
            commonDomain.TimestampToleranceSeconds,
            FullBufferDomain(commonDomain.Transform),
            FullBufferDomain(commonDomain.RootMotion),
            FullBufferDomain(commonDomain.Float),
            commonDomain,
            commonDomain.RawAclBufferSha256,
            commonDomain.RawAclBuffersPreserved,
            "Every verified QVVF transform frame is exported on its complete timestamp domain. RootMotion and float buffers are preserved in full as independent diagnostics without an authenticated transform owner and are not applied. common_buffer_domain records only their shared timestamp prefix.");
    }

    private static BufferFrameDomain FullBufferDomain(BufferFrameDomain source) =>
        new(source.OriginalFrameCount, source.SourceStartTime, source.SourceEndTime, 0, null, null);

    private static void ValidateRootTrackApplication(RootTransformBindingAudit rootBinding, BoundTrack[] bindings, string animatorRootPath)
    {
        string expectedPath = animatorRootPath + "/Root";
        if (!string.Equals(rootBinding.ExpectedSourceTransformPath, expectedPath, StringComparison.Ordinal) ||
            rootBinding.Position.Component != "position" || rootBinding.Rotation.Component != "rotation" ||
            rootBinding.Position.OutputTrack != rootBinding.Rotation.OutputTrack ||
            rootBinding.Position.SourceTransformPath != expectedPath || rootBinding.Rotation.SourceTransformPath != expectedPath)
            throw new InvalidDataException($"Authenticated QVVF root binding does not match the requested Animator root child: expected={expectedPath}, audited={rootBinding.ExpectedSourceTransformPath}, position_track={rootBinding.Position.OutputTrack}, rotation_track={rootBinding.Rotation.OutputTrack}.");
        BoundTrack[] matches = bindings.Where(binding => binding.Track == rootBinding.Position.OutputTrack && binding.SourceTransformPath == expectedPath).ToArray();
        if (matches.Length != 1)
            throw new InvalidDataException($"Authenticated QVVF root track is absent or duplicated in mapped source tracks: path={expectedPath}, track={rootBinding.Position.OutputTrack}, matches={matches.Length}.");
        BoundTrack rootTrack = matches[0];
        if (!rootTrack.HasPosition || !rootTrack.HasRotation || rootTrack.PositionBinding != rootBinding.Position.BindingIndex || rootTrack.RotationBinding != rootBinding.Rotation.BindingIndex)
            throw new InvalidDataException($"Mapped QVVF root components disagree with the authenticated subtrack indices: path={expectedPath}, track={rootTrack.Track}, position_binding={rootTrack.PositionBinding}, audited_position={rootBinding.Position.BindingIndex}, rotation_binding={rootTrack.RotationBinding}, audited_rotation={rootBinding.Rotation.BindingIndex}.");
        if (rootBinding.Scale is not null && (rootBinding.Scale.Component != "scale" || !rootTrack.HasScale || rootTrack.ScaleBinding != rootBinding.Scale.BindingIndex))
            throw new InvalidDataException($"Mapped QVVF root scale disagrees with the authenticated subtrack index: path={expectedPath}, track={rootTrack.Track}, scale_binding={rootTrack.ScaleBinding}, audited_scale={rootBinding.Scale.BindingIndex}.");
    }

    private static T Read<T>(string path, JsonSerializerOptions options) => JsonSerializer.Deserialize<T>(File.ReadAllText(path), options) ?? throw new InvalidDataException($"Null NPC qvvf input: {path}.");
    private static void Write<T>(string output, string name, T value, JsonSerializerOptions options) => File.WriteAllText(Path.Combine(output, name), JsonSerializer.Serialize(value, options));
}
