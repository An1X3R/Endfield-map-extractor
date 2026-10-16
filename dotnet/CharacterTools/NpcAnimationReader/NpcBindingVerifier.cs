using System.Buffers.Binary;
using System.Security.Cryptography;
using System.Text;
using System.Text.Json;
using AnimeStudio;

internal sealed record BoundTrack(int Track, uint PathHash, string SourceTransformPath, int? PositionBinding, int? RotationBinding, int? ScaleBinding, bool HasPosition, bool HasRotation, bool HasScale);
internal sealed record RootSubTrackBinding(string Component, int ComponentOrdinal, int OutputTrack, int BindingIndex, uint PathHash, string SourceTransformPath);
internal sealed record RootTransformBindingAudit(string ExpectedSourceTransformPath, RootSubTrackBinding Position, RootSubTrackBinding Rotation, RootSubTrackBinding? Scale);
internal sealed record ScalarBinding(int Track, int Binding, uint Attribute, int CustomType, string? AttributeName);
internal sealed record ConstantComparison(int Binding, int ValueOffset, int Width, float MaximumAbsoluteDifference);
internal sealed record UnmappedTrack(int Track, uint PathHash, string NativeDisposition);
internal sealed record UnmappedConstantBinding(int Binding, uint PathHash, uint Attribute, int Width, string NativeDisposition);
internal sealed record BufferFrameDomain(int OriginalFrameCount, float SourceStartTime, float SourceEndTime, int ExcludedTailFrames, float? ExcludedTailStartTime, float? ExcludedTailEndTime);
internal sealed record RawAclBufferHashes(string Transform, string RootMotion, string Float);
internal sealed record CommonFrameDomain(string Status, int CommonFrameCount, float ExportedStartTime, float ExportedEndTime, float MaximumCommonTimestampDifference, float TimestampToleranceSeconds, BufferFrameDomain Transform, BufferFrameDomain RootMotion, BufferFrameDomain Float, RawAclBufferHashes RawAclBufferSha256, bool RawAclBuffersPreserved, string ExportPolicy);

/// <summary>Validates NPC ACL tracks while preserving native unmapped-Avatar behavior.</summary>
internal static class BindingVerifier
{
    private const float FrameTimeToleranceSeconds = 1e-6f;

    internal static void Verify(AnimationClip clip, string sourceAuditPath, string output, JsonSerializerOptions options)
    {
        var buffer = clip.m_AclCompressedBuffer;
        var bindings = clip.m_ClipBindingConstant.genericBindings;
        int count = buffer.OutputTrackCount;
        int stride = (count + 7) / 8;
        int payloadBytes = stride * 3;
        if (buffer.TransformSubTrackMasks.Length != ((payloadBytes + 7) / 8) * 8 || buffer.TransformSubTrackMasks.Skip(payloadBytes).Any(value => value != 0)) throw new InvalidDataException("Transform masks must contain three byte-aligned component bitmaps followed by zero padding to an eight-byte allocation.");
        if (count % 8 != 0 && Enumerable.Range(0, 3).Any(component => (buffer.TransformSubTrackMasks[component * stride + stride - 1] & ((1 << (8 - count % 8)) - 1)) != 0)) throw new InvalidDataException("Transform mask has nonzero low padding bits.");
        int[][] activeTracks = Enumerable.Range(0, 3).Select(component => Enumerable.Range(0, count)
            .Where(index => MaskBit(buffer.TransformSubTrackMasks, count, component, index)).ToArray()).ToArray();
        int positionCount = activeTracks[0].Length;
        int rotationCount = activeTracks[1].Length;
        int scaleCount = activeTracks[2].Length;
        int rotationStart = positionCount;
        int scaleStart = rotationStart + rotationCount;
        int scalarStart = scaleStart + scaleCount;
        if (bindings.Count != scalarStart + buffer.FloatCurveCount) throw new InvalidDataException($"Sparse transform/scalar binding blocks disagree with active mask bits: output_tracks={count}, position_mask_bits={positionCount}, rotation_mask_bits={rotationCount}, scale_mask_bits={scaleCount}, float_curves={buffer.FloatCurveCount}, bindings={bindings.Count}.");
        using var source = JsonDocument.Parse(File.ReadAllText(sourceAuditPath));
        string[] sourcePaths = source.RootElement.GetProperty("transforms").EnumerateArray().Select(row => row.GetProperty("path").GetString() ?? throw new InvalidDataException("Source transform path is null.")).ToArray();
        var pathHashes = sourcePaths.SelectMany(path => path.Split('/').Select((part, index) => new { Path = path, Hash = HashPath(string.Join('/', path.Split('/').Skip(index))) })).GroupBy(row => row.Hash).ToDictionary(group => group.Key, group => group.Select(row => row.Path).Distinct().ToArray());
        var pathHashByTrack = new uint?[count];
        var positionBindingByTrack = Enumerable.Repeat(-1, count).ToArray();
        var rotationBindingByTrack = Enumerable.Repeat(-1, count).ToArray();
        var scaleBindingByTrack = Enumerable.Repeat(-1, count).ToArray();
        var bindingTracks = new Dictionary<int, (int Track, uint Attribute)>();
        for (int ordinal = 0; ordinal < positionCount; ordinal++)
            AssignSubTrack(bindings[ordinal], ordinal, activeTracks[0][ordinal], 1, pathHashByTrack, positionBindingByTrack, bindingTracks);
        for (int ordinal = 0; ordinal < rotationCount; ordinal++)
        {
            int bindingIndex = rotationStart + ordinal;
            AssignSubTrack(bindings[bindingIndex], bindingIndex, activeTracks[1][ordinal], 2, pathHashByTrack, rotationBindingByTrack, bindingTracks);
        }
        for (int ordinal = 0; ordinal < scaleCount; ordinal++)
        {
            int bindingIndex = scaleStart + ordinal;
            AssignSubTrack(bindings[bindingIndex], bindingIndex, activeTracks[2][ordinal], 3, pathHashByTrack, scaleBindingByTrack, bindingTracks);
        }
        if (Enumerable.Range(0, count).Any(index => pathHashByTrack[index] is null))
            throw new InvalidDataException("QVVF output track has no active position, rotation or scale binding.");
        var tracks = new List<BoundTrack>(count);
        var unmappedTracks = new List<UnmappedTrack>();
        for (int index = 0; index < count; index++)
        {
            bool hasPosition = MaskBit(buffer.TransformSubTrackMasks, count, 0, index);
            bool hasRotation = MaskBit(buffer.TransformSubTrackMasks, count, 1, index);
            bool hasScale = MaskBit(buffer.TransformSubTrackMasks, count, 2, index);
            uint pathHash = pathHashByTrack[index]!.Value;
            if (!pathHashes.TryGetValue(pathHash, out var candidates))
            {
                unmappedTracks.Add(new UnmappedTrack(index, pathHash, "native selected-Avatar lookup misses and stores binding index -1; consumer skips it"));
                continue;
            }
            if (candidates.Length != 1) throw new InvalidDataException($"Transform CRC path is ambiguous: track={index}, hash={pathHash}, candidates={candidates.Length}.");
            tracks.Add(new BoundTrack(index, pathHash, candidates[0],
                positionBindingByTrack[index] < 0 ? null : positionBindingByTrack[index],
                rotationBindingByTrack[index] < 0 ? null : rotationBindingByTrack[index],
                scaleBindingByTrack[index] < 0 ? null : scaleBindingByTrack[index], hasPosition, hasRotation, hasScale));
        }
        if (tracks.Select(track => track.SourceTransformPath).Distinct().Count() != tracks.Count) throw new InvalidDataException("Multiple mapped ACL tracks resolve to one source transform.");
        var mappedScaleIndices = tracks.Where(track => track.HasScale).Select(track => track.ScaleBinding ?? throw new InvalidDataException($"Scale binding index is missing: track={track.Track}.")).ToArray();
        if (mappedScaleIndices.Distinct().Count() != mappedScaleIndices.Length) throw new InvalidDataException("Mapped ACL scale bindings are duplicated.");
        var scalarBindings = bindings.Skip(scalarStart).Select((binding, index) => new ScalarBinding(index, index + scalarStart, binding.attribute, binding.customType, binding.customType == 8 ? ((HumanoidMuscleType)binding.attribute).Update(binding.version).ToAttributeString() : null)).ToArray();
        if (scalarBindings.Length != buffer.FloatCurveCount || bindings.Skip(scalarStart).Any(binding => binding.typeID != ClassIDType.Animator)) throw new InvalidDataException("Float channels are not the observed Animator scalar layout.");
        var transforms = JsonSerializer.Deserialize<TransformSample[]>(File.ReadAllText(Path.Combine(output, "transform_samples.json")), options) ?? throw new InvalidDataException("Transform sample file is null.");
        var scalars = JsonSerializer.Deserialize<FloatSample[]>(File.ReadAllText(Path.Combine(output, "float_samples.json")), options) ?? throw new InvalidDataException("Float sample file is null.");
        var roots = JsonSerializer.Deserialize<FloatSample[]>(File.ReadAllText(Path.Combine(output, "root_motion_samples.json")), options) ?? throw new InvalidDataException("Root sample file is null.");
        CommonFrameDomain frameDomain = ValidateFrameDomain(transforms, roots, scalars, count, buffer.RootTrackCount, buffer.FloatCurveCount, output);
        File.WriteAllText(Path.Combine(output, "frame_domain_verification.json"), JsonSerializer.Serialize(frameDomain, options));
        NpcRootLayoutKind rootLayout = NpcQvvfProgram.IdentifyLayout(clip, scalarBindings);
        bool hasIndependentRootSignature = NpcQvvfProgram.HasIndependentRootSignature(clip, scalarBindings);
        if (hasIndependentRootSignature && rootLayout == NpcRootLayoutKind.Unsupported)
        {
            string attributes = string.Join(',', scalarBindings.Select(binding => binding.AttributeName ?? $"custom:{binding.Attribute}"));
            throw new InvalidDataException($"Independent NPC root channels have an unsupported source layout: legacy={clip.m_Legacy}, motion_float_curves={clip.m_HasMotionFloatCurves}, generic_root_transform={clip.m_HasGenericRootTransform}, root_tracks={buffer.RootTrackCount}, float_curves={scalarBindings.Length}, attributes={attributes}.");
        }
        bool independentRootLayout = rootLayout != NpcRootLayoutKind.Unsupported;
        if (independentRootLayout) NpcQvvfProgram.ValidateRootSamples(scalars, roots, buffer.RootTrackCount);
        RootTransformBindingAudit? rootTransformBinding = independentRootLayout
            ? ValidateRootTransformBinding(checked((int)buffer.RootPosIndex), checked((int)buffer.RootRotIndex), checked((int)buffer.RootScaleIndex), activeTracks, pathHashByTrack, pathHashes, positionBindingByTrack, rotationBindingByTrack, scaleBindingByTrack, sourcePaths)
            : null;
        RootFloatTrackComparison[] rootFloatComparisons = independentRootLayout
            ? Array.Empty<RootFloatTrackComparison>()
            : AclScalarPrefixVerifier.Compare(buffer.RootMotionBufferData, buffer.FloatBufferData, buffer.RootTrackCount, scalarBindings, roots, scalars, frameDomain.CommonFrameCount);
        RootFloatTrackComparison? failedRootFloatComparison = rootFloatComparisons.FirstOrDefault(row => !row.WithinEncodingBound);
        if (failedRootFloatComparison is not null) throw new InvalidDataException($"Root-motion/float prefix exceeds its per-track ACL encoding bound: track={failedRootFloatComparison.Track}, attribute={failedRootFloatComparison.AttributeName}, root_bits={failedRootFloatComparison.RootBits}, float_bits={failedRootFloatComparison.FloatBits}, maximum_difference={failedRootFloatComparison.MaximumAbsoluteDifference}, allowed_difference={failedRootFloatComparison.AllowedDifference}.");
        float rootError = independentRootLayout || rootFloatComparisons.Length == 0 ? 0f : rootFloatComparisons.Max(row => row.MaximumAbsoluteDifference);
        var rootFloatDiscrepancies = rootFloatComparisons.Where(row => row.MaximumAbsoluteDifference > 0f).ToArray();
        int offset = 0;
        var comparisons = new List<ConstantComparison>();
        var unmappedConstants = new List<UnmappedConstantBinding>();
        if (buffer.m_ConstantIndexs.Distinct().Count() != buffer.m_ConstantIndexs.Length) throw new InvalidDataException("Constant binding indices are duplicated.");
        foreach (ushort bindingIndex in buffer.m_ConstantIndexs)
        {
            if (bindingIndex >= bindings.Count) throw new InvalidDataException($"Constant binding index out of range: index={bindingIndex}.");
            GenericBinding binding = bindings[bindingIndex];
            int width = binding.typeID == ClassIDType.Transform ? binding.attribute switch { 1 => 3, 2 => 4, 3 => 3, _ => throw new InvalidDataException($"Unexpected constant transform attribute: {binding.attribute}.") } : 1;
            if (offset + width > buffer.m_ConstantValues.Length) throw new InvalidDataException("Constant value list is shorter than binding component widths.");
            string[]? candidates = binding.typeID == ClassIDType.Transform && pathHashes.TryGetValue(binding.path, out var resolved) ? resolved : null;
            if (binding.typeID == ClassIDType.Transform && candidates is { Length: > 1 }) throw new InvalidDataException($"Constant transform CRC path is ambiguous: binding={bindingIndex}, hash={binding.path}, candidates={candidates.Length}.");
            if (binding.typeID == ClassIDType.Transform && candidates is null)
            {
                unmappedConstants.Add(new UnmappedConstantBinding(bindingIndex, binding.path, binding.attribute, width, "native selected-Avatar lookup misses and consumer skips the binding"));
            }
            else
            {
                float maximum = Enumerable.Range(0, frameDomain.CommonFrameCount).SelectMany(frame => Enumerable.Range(0, width).Select(component => Math.Abs(buffer.m_ConstantValues[offset + component] - GetComponent(transforms[frame], scalars[frame], bindingIndex, scalarStart, bindingTracks, component)))).Max();
                if (maximum > 0.00001f) throw new InvalidDataException($"Decoded constant does not match source constant: binding={bindingIndex}, maximum_absolute_difference={maximum}.");
                comparisons.Add(new ConstantComparison(bindingIndex, offset, width, maximum));
            }
            offset += width;
        }
        if (offset != buffer.m_ConstantValues.Length) throw new InvalidDataException($"Unconsumed constant values: consumed={offset}, total={buffer.m_ConstantValues.Length}.");
        var scalarNames = scalarBindings.Take(buffer.RootTrackCount).ToArray();
        ScalarBinding[] rootScalarBindings = independentRootLayout ? NpcQvvfProgram.GetRootScalarBindings(scalarBindings) : scalarNames;
        ScalarBinding[] motionScalarBindings = rootLayout == NpcRootLayoutKind.MotionAndRoot ? NpcQvvfProgram.GetMotionScalarBindings(scalarBindings) : [];
        var preservedScalarBindings = scalarBindings.Where(binding => !rootScalarBindings.Contains(binding) && !motionScalarBindings.Contains(binding)).ToArray();
        File.WriteAllText(Path.Combine(output, "bound_tracks.json"), JsonSerializer.Serialize(tracks.ToArray(), options));
        File.WriteAllText(Path.Combine(output, "unmapped_tracks.json"), JsonSerializer.Serialize(unmappedTracks.ToArray(), options));
        File.WriteAllText(Path.Combine(output, "scalar_bindings.json"), JsonSerializer.Serialize(scalarBindings, options));
        File.WriteAllText(Path.Combine(output, "constant_comparisons.json"), JsonSerializer.Serialize(comparisons.ToArray(), options));
        File.WriteAllText(Path.Combine(output, "unmapped_constant_bindings.json"), JsonSerializer.Serialize(unmappedConstants.ToArray(), options));
        var changingTracks = tracks.Where(track => transforms.Skip(1).Any(row =>
            (track.HasPosition && !row.Tracks[track.Track].Translation.SequenceEqual(transforms[0].Tracks[track.Track].Translation)) ||
            (track.HasRotation && !row.Tracks[track.Track].Rotation.SequenceEqual(transforms[0].Tracks[track.Track].Rotation)) ||
            (track.HasScale && !row.Tracks[track.Track].Scale.SequenceEqual(transforms[0].Tracks[track.Track].Scale)))).Select(track => track.Track).ToArray();
        byte[] data = buffer.TransformBufferData;
        var body = new { segments = ReadUInt(data, 32), animated_rotation_tracks = ReadUInt(data, 40), animated_translation_tracks = ReadUInt(data, 44), animated_scale_tracks = ReadUInt(data, 48), constant_rotation_tracks = ReadUInt(data, 52), constant_translation_tracks = ReadUInt(data, 56), default_rotation_tracks = count - ReadUInt(data, 40) - ReadUInt(data, 52), default_translation_tracks = count - ReadUInt(data, 44) - ReadUInt(data, 56), misc_hex = ReadUInt(data, 28).ToString("x8") };
        if (rootTransformBinding is not null)
            File.WriteAllText(Path.Combine(output, "root_transform_binding.json"), JsonSerializer.Serialize(rootTransformBinding, options));
        File.WriteAllText(Path.Combine(output, "binding_verification.json"), JsonSerializer.Serialize(new { status = "mapped_selected_avatar_bindings_verified_native_unmapped_skipped", source_audit = sourceAuditPath, source_transform_count = sourcePaths.Length, binding_count = bindings.Count, mapped_transform_tracks = tracks.Count, unresolved_transform_tracks = unmappedTracks.Count, ambiguous_transform_tracks = 0, unmapped_tracks = unmappedTracks, transform_position_bindings = positionCount, transform_rotation_bindings = rotationCount, scale_bindings = scaleCount, mapped_scale_binding_indices = mappedScaleIndices, scalar_bindings = scalarBindings.Length, root_layout = rootLayout.ToString(), root_transform_binding = rootTransformBinding, root_scalar_bindings = rootScalarBindings, motion_scalar_bindings = motionScalarBindings, preserved_unclassified_scalar_bindings = preservedScalarBindings, constant_bindings_verified = comparisons.Count, unmapped_constant_bindings = unmappedConstants, constant_values_verified = offset, maximum_constant_difference = comparisons.Count == 0 ? 0f : comparisons.Max(row => row.MaximumAbsoluteDifference), default_binding_indices = buffer.m_DefaultIndexs, constant_subtrack_mask_bytes = buffer.TransformSubTrackConstantMasks.Length, mask_bytes = buffer.TransformSubTrackMasks.Length, position_rotation_scale_masks = "component-major active masks; each component binding ordinal maps to its ascending set-bit output track", animated_transform_tracks = changingTracks.Length, animated_track_indices = changingTracks, root_float_comparison_status = independentRootLayout ? "independent_root_buffer_preserved_without_transform_owner_or_second_application" : "per_track_acl_encoding_bound_verified", root_float_prefix_compared_tracks = independentRootLayout ? 0 : buffer.RootTrackCount, root_float_prefix_maximum_difference = independentRootLayout ? (float?)null : rootError, root_float_prefix_tolerance_basis = independentRootLayout ? "no_cross_buffer_mapping_asserted" : "sample_difference<=root_step+float_step+decoder_float32_margin; range_envelope_difference independently <= smaller_step+4_endpoint_float32_ULPs; constants require bit-identical values; observed range difference is never added to sample allowance", root_float_prefix_comparisons = rootFloatComparisons, root_float_buffer_comparison = new { comparison = independentRootLayout ? "Independent RootMotionBufferData and FloatBufferData are preserved as evidence without an authenticated transform owner or second application." : "Independent RootMotionBufferData and FloatBufferData decoded with the same ACL float decompressor; prefix comparison uses each source track's encoded bit rate and separately bounded range metadata rather than a fixed epsilon.", root_motion_sha256 = frameDomain.RawAclBufferSha256.RootMotion, float_buffer_sha256 = frameDomain.RawAclBufferSha256.Float, root_motion_frames = roots.Length, float_buffer_frames = scalars.Length, common_compared_frames = frameDomain.CommonFrameCount, discrepancies = rootFloatDiscrepancies }, root_indices = new[] { buffer.RootPosIndex, buffer.RootRotIndex, buffer.RootScaleIndex }, acl_transform_body = body, time_domain = frameDomain, native_unmapped_binding_contract = "Only selected-Avatar path misses are skipped. Ambiguous CRC hashes fail. Based on native AvatarClipBindingsAppend initial -1 map and ClipCacheBindingBuild conditional application; source metadata is recorded in the NPC integration receipt.", validation_boundary = "Every stored frame remains in the raw ACL outputs and was decoded. Timestamp alignment and constant/root comparisons cover the explicitly reported mapped and shared domains. Unmapped Avatar tracks are intentionally not applied. Unity runtime pose corrections, controller application, coordinate conversion and rendered playback are not validated." }, options));
    }

    private static void AssignSubTrack(GenericBinding binding, int bindingIndex, int track, uint expectedAttribute,
        uint?[] pathHashByTrack, int[] componentBindingByTrack, Dictionary<int, (int Track, uint Attribute)> bindingTracks)
    {
        if (binding.typeID != ClassIDType.Transform || binding.attribute != expectedAttribute)
            throw new InvalidDataException($"Sparse transform binding has the wrong type or component: binding={bindingIndex}, type={binding.typeID}, attribute={binding.attribute}, expected_attribute={expectedAttribute}.");
        if (componentBindingByTrack[track] >= 0)
            throw new InvalidDataException($"QVVF output track has duplicate component bindings: track={track}, attribute={expectedAttribute}.");
        if (pathHashByTrack[track] is uint priorPathHash && priorPathHash != binding.path)
            throw new InvalidDataException($"QVVF output track components target different transform paths: track={track}, first_hash={priorPathHash}, binding_hash={binding.path}.");
        if (!bindingTracks.TryAdd(bindingIndex, (track, binding.attribute)))
            throw new InvalidDataException($"Generic binding index was assigned more than once: binding={bindingIndex}.");
        pathHashByTrack[track] = binding.path;
        componentBindingByTrack[track] = bindingIndex;
    }

    private static RootTransformBindingAudit ValidateRootTransformBinding(int rootPositionOrdinal, int rootRotationOrdinal,
        int rootScaleOrdinal, int[][] activeTracks, uint?[] pathHashByTrack, Dictionary<uint, string[]> pathHashes, int[] positionBindingByTrack,
        int[] rotationBindingByTrack, int[] scaleBindingByTrack, string[] sourcePaths)
    {
        if (sourcePaths.Length == 0) throw new InvalidDataException("Root transform mapping requires a nonempty source hierarchy.");
        string expectedRootPath = sourcePaths[0] + "/Root";
        if (sourcePaths.Count(path => string.Equals(path, expectedRootPath, StringComparison.Ordinal)) != 1)
            throw new InvalidDataException($"Root transform path must resolve exactly once beneath the source Animator root: path={expectedRootPath}.");

        RootSubTrackBinding position = MapRootSubTrack("position", rootPositionOrdinal, 0, activeTracks, pathHashByTrack, pathHashes, positionBindingByTrack, expectedRootPath);
        RootSubTrackBinding rotation = MapRootSubTrack("rotation", rootRotationOrdinal, 1, activeTracks, pathHashByTrack, pathHashes, rotationBindingByTrack, expectedRootPath);
        RootSubTrackBinding? scale = rootScaleOrdinal == ushort.MaxValue
            ? null
            : MapRootSubTrack("scale", rootScaleOrdinal, 2, activeTracks, pathHashByTrack, pathHashes, scaleBindingByTrack, expectedRootPath);
        if (position.OutputTrack != rotation.OutputTrack || position.PathHash != rotation.PathHash || position.SourceTransformPath != rotation.SourceTransformPath ||
            (scale is not null && (scale.OutputTrack != position.OutputTrack || scale.PathHash != position.PathHash || scale.SourceTransformPath != position.SourceTransformPath)))
            throw new InvalidDataException($"Root position, rotation and optional scale indices must resolve to the same QVVF transform: position_track={position.OutputTrack}, rotation_track={rotation.OutputTrack}, scale_track={scale?.OutputTrack}, path={position.SourceTransformPath}.");
        return new RootTransformBindingAudit(expectedRootPath, position, rotation, scale);
    }

    private static RootSubTrackBinding MapRootSubTrack(string component, int componentOrdinal, int componentIndex,
        int[][] activeTracks, uint?[] pathHashByTrack, Dictionary<uint, string[]> pathHashes, int[] bindingByTrack, string expectedRootPath)
    {
        int[] componentTracks = activeTracks[componentIndex];
        if (componentOrdinal < 0 || componentOrdinal >= componentTracks.Length)
            throw new InvalidDataException($"Root {component} index is outside its active-mask component list: ordinal={componentOrdinal}, active_bindings={componentTracks.Length}.");
        int outputTrack = componentTracks[componentOrdinal];
        int bindingIndex = bindingByTrack[outputTrack];
        if (bindingIndex < 0 || pathHashByTrack[outputTrack] is not uint pathHash)
            throw new InvalidDataException($"Root {component} index resolves to an incomplete QVVF component mapping: ordinal={componentOrdinal}, output_track={outputTrack}, binding={bindingIndex}.");
        if (pathHash != HashPath("Root") || !pathHashes.TryGetValue(pathHash, out string[]? candidates) || candidates is null || candidates.Length != 1 || !string.Equals(candidates[0], expectedRootPath, StringComparison.Ordinal))
            throw new InvalidDataException($"Root {component} QVVF binding does not target the exact Root path: ordinal={componentOrdinal}, output_track={outputTrack}, path_hash={pathHash}, expected_path={expectedRootPath}.");
        return new RootSubTrackBinding(component, componentOrdinal, outputTrack, bindingIndex, pathHash, expectedRootPath);
    }

    private static CommonFrameDomain ValidateFrameDomain(TransformSample[] transforms, FloatSample[] roots, FloatSample[] scalars, int expectedTransformTracks, int expectedRootTracks, int expectedFloatTracks, string output)
    {
        if (transforms.Length == 0 || roots.Length == 0 || scalars.Length == 0) throw new InvalidDataException($"ACL frame buffers must be nonempty: transform={transforms.Length}, root_motion={roots.Length}, float={scalars.Length}.");
        if (transforms.Any(frame => frame.Tracks.Length != expectedTransformTracks) || roots.Any(frame => frame.Values.Length != expectedRootTracks) || scalars.Any(frame => frame.Values.Length != expectedFloatTracks)) throw new InvalidDataException($"ACL sample track dimensions disagree with headers: transform_expected={expectedTransformTracks}, root_expected={expectedRootTracks}, float_expected={expectedFloatTracks}.");
        float[] transformTimes = transforms.Select(frame => frame.Time).ToArray();
        float[] rootTimes = roots.Select(frame => frame.Time).ToArray();
        float[] scalarTimes = scalars.Select(frame => frame.Time).ToArray();
        ValidateTimes("transform", transformTimes);
        ValidateTimes("root_motion", rootTimes);
        ValidateTimes("float", scalarTimes);
        int commonFrameCount = Math.Min(transforms.Length, Math.Min(roots.Length, scalars.Length));
        float maximumDifference = 0f;
        for (int frame = 0; frame < commonFrameCount; frame++)
        {
            float transformRoot = MathF.Abs(transformTimes[frame] - rootTimes[frame]);
            float transformFloat = MathF.Abs(transformTimes[frame] - scalarTimes[frame]);
            float rootFloat = MathF.Abs(rootTimes[frame] - scalarTimes[frame]);
            maximumDifference = MathF.Max(maximumDifference, MathF.Max(transformRoot, MathF.Max(transformFloat, rootFloat)));
        }
        if (maximumDifference > FrameTimeToleranceSeconds) throw new InvalidDataException($"ACL frame timestamps diverge inside the common prefix: frames={commonFrameCount}, maximum_difference_seconds={maximumDifference}, tolerance_seconds={FrameTimeToleranceSeconds}.");
        string[] rawFiles = ["transform.acl", "root_motion.acl", "float.acl"];
        string[] rawHashes = rawFiles.Select(name => Convert.ToHexString(SHA256.HashData(File.ReadAllBytes(Path.Combine(output, name))))).ToArray();
        return new CommonFrameDomain("shared_timestamp_prefix_verified", commonFrameCount, transformTimes[0], transformTimes[commonFrameCount - 1], maximumDifference, FrameTimeToleranceSeconds,
            MakeBufferDomain(transformTimes, commonFrameCount), MakeBufferDomain(rootTimes, commonFrameCount), MakeBufferDomain(scalarTimes, commonFrameCount),
            new RawAclBufferHashes(rawHashes[0], rawHashes[1], rawHashes[2]), true,
            "Raw ACL buffers are never truncated or rewritten; only timestamps shared by all three decoded buffers enter merged source-local frames.");
    }

    private static BufferFrameDomain MakeBufferDomain(float[] times, int commonFrameCount)
    {
        int excluded = times.Length - commonFrameCount;
        float? excludedStart = excluded == 0 ? null : times[commonFrameCount];
        float? excludedEnd = excluded == 0 ? null : times[^1];
        return new BufferFrameDomain(times.Length, times[0], times[^1], excluded, excludedStart, excludedEnd);
    }

    private static void ValidateTimes(string buffer, float[] times)
    {
        for (int frame = 0; frame < times.Length; frame++)
        {
            if (!float.IsFinite(times[frame])) throw new InvalidDataException($"Nonfinite ACL timestamp: buffer={buffer}, frame={frame}, time={times[frame]}.");
            if (frame > 0 && times[frame] <= times[frame - 1]) throw new InvalidDataException($"ACL timestamps must be strictly increasing: buffer={buffer}, frame={frame}, previous={times[frame - 1]}, time={times[frame]}.");
        }
    }

    internal static uint HashPath(string path)
    {
        byte[] bytes = Encoding.UTF8.GetBytes(path);
        var crc = new SevenZip.CRC();
        crc.Update(bytes, 0, (uint)bytes.Length);
        return crc.GetDigest();
    }

    private static bool MaskBit(byte[] masks, int count, int component, int index)
    {
        int byteIndex = component * ((count + 7) / 8) + index / 8;
        if (byteIndex >= masks.Length) throw new InvalidDataException($"Transform mask is truncated: component={component}, track={index}, bytes={masks.Length}.");
        return (masks[byteIndex] & (1 << (7 - index % 8))) != 0;
    }

    private static uint ReadUInt(byte[] data, int offset) => BinaryPrimitives.ReadUInt32LittleEndian(data.AsSpan(offset, 4));
    private static uint OrderedFloatBits(float value)
    {
        uint bits = BitConverter.SingleToUInt32Bits(value);
        return (bits & 0x80000000) != 0 ? ~bits : bits | 0x80000000;
    }
    private static long UlpDistance(float left, float right) => Math.Abs((long)OrderedFloatBits(left) - OrderedFloatBits(right));
    private static float GetComponent(TransformSample transform, FloatSample scalar, int binding, int scalarStart,
        Dictionary<int, (int Track, uint Attribute)> bindingTracks, int component)
    {
        if (bindingTracks.TryGetValue(binding, out var track))
            return track.Attribute switch
            {
                1 => transform.Tracks[track.Track].Translation[component],
                2 => transform.Tracks[track.Track].Rotation[component],
                3 => transform.Tracks[track.Track].Scale[component],
                _ => throw new InvalidDataException($"Unsupported transform component in constant binding: binding={binding}, attribute={track.Attribute}.")
            };
        if (binding < scalarStart) throw new InvalidDataException($"Constant transform binding is absent from sparse component blocks: binding={binding}, scalar_start={scalarStart}.");
        return scalar.Values[binding - scalarStart];
    }
}
