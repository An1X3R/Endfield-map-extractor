using System.Text.Json;

/// <summary>Applies exact qvvf bindings without reconstructing a Human pose for root-only scalar clips.</summary>
internal static class NpcQvvfProgram
{
    private static readonly string[] RootAttributes = ["RootT.x", "RootT.y", "RootT.z", "RootQ.x", "RootQ.y", "RootQ.z", "RootQ.w"];
    private static readonly float[] IdentityRoot = [0f, 0f, 0f, 0f, 0f, 0f, 1f];
    private const float IdentityTolerance = 1e-6f;

    internal static bool IsRootOnlyLayout(ScalarBinding[] bindings) => bindings.Length == 7 && bindings.Select((binding, index) => binding.Track == index && binding.CustomType == 8 && binding.Attribute == index + 7 && binding.AttributeName == RootAttributes[index]).All(value => value);

    internal static bool SupportsLayout(ScalarBinding[] bindings, int rootTracks) => rootTracks == 14 && IsRootOnlyLayout(bindings);

    internal static void ValidateStationaryRoot(ScalarBinding[] bindings, FloatSample[] scalars, FloatSample[] roots)
    {
        if (!IsRootOnlyLayout(bindings) || roots.Length == 0 || scalars.Length == 0)
            throw new InvalidDataException("Independent NPC root buffer supports only the exact seven RootT/RootQ scalar bindings and nonempty samples; moving or other root layouts are not implemented.");
        ValidateIdentitySamples("float_RootT_RootQ", scalars, 1);
        ValidateIdentitySamples("independent_root_motion", roots, 2);
    }

    private static void ValidateIdentitySamples(string buffer, FloatSample[] frames, int identityCopies)
    {
        for (int frame = 0; frame < frames.Length; frame++)
        {
            float[] values = frames[frame].Values;
            if (values.Length != identityCopies * IdentityRoot.Length)
                throw new InvalidDataException($"Unsupported stationary NPC root dimensions: buffer={buffer}, frame={frame}, tracks={values.Length}, expected={identityCopies * IdentityRoot.Length}.");
            for (int track = 0; track < values.Length; track++)
            {
                float value = values[track];
                if (!float.IsFinite(value) || MathF.Abs(value - IdentityRoot[track % 7]) > IdentityTolerance || value != frames[0].Values[track])
                    throw new InvalidDataException($"NPC qvvf-only root must be source-constant identity: buffer={buffer}, frame={frame}, track={track}, value={value}, initial={frames[0].Values[track]}, identity_tolerance={IdentityTolerance}; moving root semantics are not implemented.");
            }
        }
    }

    internal static void Export(AnimationRequest request, JsonSerializerOptions options)
    {
        string native = Path.Combine(request.Output, "native");
        ScalarBinding[] scalarBindings = Read<ScalarBinding[]>(Path.Combine(native, "scalar_bindings.json"), options);
        FloatSample[] scalars = AnimationFrameJson.Read<FloatSample[]>(Path.Combine(native, "float_samples.json"), options);
        FloatSample[] roots = AnimationFrameJson.Read<FloatSample[]>(Path.Combine(native, "root_motion_samples.json"), options);
        ValidateStationaryRoot(scalarBindings, scalars, roots);
        TransformSample[] qvvf = AnimationFrameJson.Read<TransformSample[]>(Path.Combine(native, "transform_samples.json"), options);
        BoundTrack[] bindings = Read<BoundTrack[]>(Path.Combine(native, "bound_tracks.json"), options);
        CommonFrameDomain domain = Read<CommonFrameDomain>(Path.Combine(native, "frame_domain_verification.json"), options);
        StaticAudit source = Read<StaticAudit>(request.SourceAudit, options);
        if (domain.CommonFrameCount <= 0 || qvvf.Length != domain.Transform.OriginalFrameCount || scalars.Length != domain.Float.OriginalFrameCount || roots.Length != domain.RootMotion.OriginalFrameCount || bindings.Length == 0)
            throw new InvalidDataException("NPC qvvf-only inputs disagree with the verified complete frame domain or contain no mapped transform tracks.");
        var sourceIndices = source.Transforms.Select((row, index) => (row.Path, Index: index)).ToDictionary(row => row.Path, row => row.Index, StringComparer.Ordinal);
        if (!sourceIndices.TryGetValue(request.AnimatorRootPath, out int rootIndex)) throw new InvalidDataException("Exact NPC Animator root is absent from source hierarchy.");
        StaticTransform sourceRoot = source.Transforms[rootIndex];
        FloatSample rootPose = new(0f, sourceRoot.Position.Concat(sourceRoot.Rotation).ToArray());
        ValidateIdentitySamples("selected_Avatar_source_root", [rootPose], 1);
        if (sourceRoot.Scale.Length != 3 || sourceRoot.Scale.Any(value => value != 1f)) throw new InvalidDataException("Stationary NPC qvvf-only source root must have unit scale.");
        var mapped = bindings.ToDictionary(binding => binding.SourceTransformPath, StringComparer.Ordinal);
        if (mapped.ContainsKey(request.AnimatorRootPath)) throw new InvalidDataException("Stationary NPC qvvf-only source root is also transform-bound; combined root ownership is not implemented.");
        SourceHierarchyRow[] hierarchy = source.Transforms.Select((row, index) =>
        {
            int slash = row.Path.LastIndexOf('/');
            int parent = slash < 0 ? -1 : sourceIndices[row.Path[..slash]];
            if (parent >= index) throw new InvalidDataException($"NPC source hierarchy must be parent-first: path={row.Path}, parent={parent}, index={index}.");
            bool bound = mapped.TryGetValue(row.Path, out BoundTrack? track);
            return new SourceHierarchyRow(index, row.Path, parent, new SourceLocalValue(row.Position, row.Rotation, row.Scale), bound ? "acl_qvvf" : "source_static", bound ? "acl_qvvf" : "source_static", bound && track!.HasScale ? "acl_qvvf" : "source_static_no_scale_bindings");
        }).ToArray();
        var frames = new List<SourceLocalFrame>(domain.CommonFrameCount);
        for (int frame = 0; frame < domain.CommonFrameCount; frame++)
        {
            SourceLocalValue[] locals = source.Transforms.Select(row =>
            {
                if (!mapped.TryGetValue(row.Path, out BoundTrack? binding)) return new SourceLocalValue(row.Position.ToArray(), row.Rotation.ToArray(), row.Scale.ToArray());
                TransformValue value = qvvf[frame].Tracks[binding.Track];
                if (value.Track != binding.Track) throw new InvalidDataException($"NPC qvvf track index disagrees: frame={frame}, expected={binding.Track}, actual={value.Track}.");
                return new SourceLocalValue(value.Translation.ToArray(), value.Rotation.ToArray(), binding.HasScale ? value.Scale.ToArray() : row.Scale.ToArray());
            }).ToArray();
            frames.Add(new SourceLocalFrame(qvvf[frame].Time, locals));
        }
        string output = Path.Combine(request.Output, "humanoid", "merged");
        if (Directory.Exists(output)) throw new IOException($"NPC merged output already exists: {output}.");
        Directory.CreateDirectory(output);
        Write(output, "source_hierarchy.json", hierarchy, options);
        AnimationFrameJson.Write(output, "source_local_frames.json", frames);
        Write(output, "frame_domain_verification.json", domain, options);
        Write(output, "unapplied_scalar_channels.json", scalarBindings.Select(binding => new UnappliedScalar(binding.Track, binding.AttributeName, "Source-constant identity root validated; selected Avatar root rest preserved without inferring root-motion channel mapping.", scalars.Min(frame => frame.Values[binding.Track]), scalars.Max(frame => frame.Values[binding.Track]))).ToArray(), options);
        File.Copy(Path.Combine(native, "scalar_bindings.json"), Path.Combine(output, "preserved_scalar_bindings.json"));
        File.Copy(Path.Combine(native, "float_samples.json"), Path.Combine(output, "preserved_scalar_frames.json"));
        var receipt = new { status = "exact_selected_avatar_qvvf_only_stationary_identity_root", frames = domain.CommonFrameCount, hierarchy_nodes = hierarchy.Length, qvvf_nodes = bindings.Length, humanoid_rotation_nodes = 0, humanoid_reconstruction_applied = false, source_audit = Path.GetFullPath(request.SourceAudit), motion_owner_path = request.AnimatorRootPath, motion_root_semantics = "Only source-constant identity RootT/RootQ and two identity seven-channel independent root-buffer groups are supported. Selected Avatar object-root rest is preserved. No root-buffer mapping, extraction, delta application, moving-root solve or Humanoid body reconstruction is inferred.", identity_tolerance = IdentityTolerance, raw_root_buffer_preserved = true, time_domain = domain, space = "Absolute parent-local source Unity transforms in meters; quaternion xyzw; exact qvvf local channels and source rest for unbound nodes.", validation_boundary = "Source binding and numerical merge only; no rendered, controller, runtime IK or full Generic root-motion validation." };
        Write(output, "receipt.json", receipt, options);
        Write(Path.GetDirectoryName(output)!, "receipt.json", receipt, options);
    }

    private static T Read<T>(string path, JsonSerializerOptions options) => JsonSerializer.Deserialize<T>(File.ReadAllText(path), options) ?? throw new InvalidDataException($"Null NPC qvvf input: {path}.");
    private static void Write<T>(string output, string name, T value, JsonSerializerOptions options) => File.WriteAllText(Path.Combine(output, name), JsonSerializer.Serialize(value, options));
}
