using System.Buffers.Binary;
using System.Security.Cryptography;
using System.Text;
using System.Text.Json;
using AnimeStudio;

internal sealed record BoundTrack(int Track, uint PathHash, string SourceTransformPath, int PositionBinding, int RotationBinding, bool HasPosition, bool HasRotation, bool HasScale);
internal sealed record ScalarBinding(int Track, int Binding, uint Attribute, int CustomType, string? AttributeName);
internal sealed record ConstantComparison(int Binding, int ValueOffset, int Width, float MaximumAbsoluteDifference);
internal sealed record BufferFrameDomain(int OriginalFrameCount, float SourceStartTime, float SourceEndTime, int ExcludedTailFrames, float? ExcludedTailStartTime, float? ExcludedTailEndTime);
internal sealed record RawAclBufferHashes(string Transform, string RootMotion, string Float);
internal sealed record CommonFrameDomain(string Status, int CommonFrameCount, float ExportedStartTime, float ExportedEndTime, float MaximumCommonTimestampDifference, float TimestampToleranceSeconds, BufferFrameDomain Transform, BufferFrameDomain RootMotion, BufferFrameDomain Float, RawAclBufferHashes RawAclBufferSha256, bool RawAclBuffersPreserved, string ExportPolicy);

/// <summary>Validates the decoded sample against source bindings and constant values.</summary>
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
        int scaleCount = Enumerable.Range(0, count).Count(index => MaskBit(buffer.TransformSubTrackMasks, count, 2, index));
        int scalarStart = count * 2 + scaleCount;
        if (bindings.Count != scalarStart + buffer.FloatCurveCount) throw new InvalidDataException($"Unsupported transform/scalar binding layout: tracks={count}, scale_mask_bits={scaleCount}, float_curves={buffer.FloatCurveCount}, bindings={bindings.Count}.");
        var scaleBindings = bindings.Skip(count * 2).Take(scaleCount).Select((binding, index) => (Binding: binding, Index: count * 2 + index)).ToArray();
        if (scaleBindings.Any(row => row.Binding.typeID != ClassIDType.Transform || row.Binding.attribute != 3) || scaleBindings.Select(row => row.Binding.path).Distinct().Count() != scaleCount) throw new InvalidDataException("Scale binding block must contain unique Transform scale paths.");
        var scaleIndices = scaleBindings.ToDictionary(row => row.Binding.path, row => row.Index);
        using var source = JsonDocument.Parse(File.ReadAllText(sourceAuditPath));
        string[] sourcePaths = source.RootElement.GetProperty("transforms").EnumerateArray().Select(row => row.GetProperty("path").GetString() ?? throw new InvalidDataException("Source transform path is null.")).ToArray();
        var pathHashes = sourcePaths.SelectMany(path => path.Split('/').Select((part, index) => new { Path = path, Hash = HashPath(string.Join('/', path.Split('/').Skip(index))) })).GroupBy(row => row.Hash).ToDictionary(group => group.Key, group => group.Select(row => row.Path).Distinct().ToArray());
        var tracks = Enumerable.Range(0, count).Select(index => BindTrack(index, count, bindings, pathHashes, buffer.TransformSubTrackMasks, scaleIndices)).ToArray();
        if (tracks.Select(track => track.SourceTransformPath).Distinct().Count() != count) throw new InvalidDataException("Multiple ACL tracks resolve to one source transform.");
        if (tracks.Count(track => track.HasScale) != scaleCount) throw new InvalidDataException("Scale bindings are not exhausted by mapped qvvf tracks.");
        var scaleTracksByBinding = tracks.Where(track => track.HasScale).ToDictionary(track => scaleIndices[track.PathHash], track => track.Track);
        var scalarBindings = bindings.Skip(scalarStart).Select((binding, index) => new ScalarBinding(index, index + scalarStart, binding.attribute, binding.customType, binding.customType == 8 ? ((HumanoidMuscleType)binding.attribute).Update(binding.version).ToAttributeString() : null)).ToArray();
        if (scalarBindings.Length != buffer.FloatCurveCount || bindings.Skip(scalarStart).Any(binding => binding.typeID != ClassIDType.Animator)) throw new InvalidDataException("Float channels are not the observed Animator scalar layout.");
        var transforms = JsonSerializer.Deserialize<TransformSample[]>(File.ReadAllText(Path.Combine(output, "transform_samples.json")), options) ?? throw new InvalidDataException("Transform sample file is null.");
        var scalars = JsonSerializer.Deserialize<FloatSample[]>(File.ReadAllText(Path.Combine(output, "float_samples.json")), options) ?? throw new InvalidDataException("Float sample file is null.");
        var roots = JsonSerializer.Deserialize<FloatSample[]>(File.ReadAllText(Path.Combine(output, "root_motion_samples.json")), options) ?? throw new InvalidDataException("Root sample file is null.");
        CommonFrameDomain frameDomain = ValidateFrameDomain(transforms, roots, scalars, count, buffer.RootTrackCount, buffer.FloatCurveCount, output);
        File.WriteAllText(Path.Combine(output, "frame_domain_verification.json"), JsonSerializer.Serialize(frameDomain, options));
        RootFloatTrackComparison[] rootFloatComparisons = AclScalarPrefixVerifier.Compare(buffer.RootMotionBufferData, buffer.FloatBufferData, buffer.RootTrackCount, scalarBindings, roots, scalars, frameDomain.CommonFrameCount);
        File.WriteAllText(Path.Combine(output, "root_float_comparisons.json"), JsonSerializer.Serialize(rootFloatComparisons, options));
        RootFloatTrackComparison? failedRootFloatComparison = rootFloatComparisons.FirstOrDefault(row => !row.WithinEncodingBound);
        if (failedRootFloatComparison is not null) throw new InvalidDataException($"Root-motion/float prefix exceeds its per-track ACL encoding bound: track={failedRootFloatComparison.Track}, attribute={failedRootFloatComparison.AttributeName}, root_bits={failedRootFloatComparison.RootBits}, float_bits={failedRootFloatComparison.FloatBits}, maximum_difference={failedRootFloatComparison.MaximumAbsoluteDifference}, allowed_difference={failedRootFloatComparison.AllowedDifference}.");
        float rootError = rootFloatComparisons.Length == 0 ? 0f : rootFloatComparisons.Max(row => row.MaximumAbsoluteDifference);
        int offset = 0;
        var comparisons = new List<ConstantComparison>();
        if (buffer.m_ConstantIndexs.Distinct().Count() != buffer.m_ConstantIndexs.Length) throw new InvalidDataException("Constant binding indices are duplicated.");
        foreach (ushort bindingIndex in buffer.m_ConstantIndexs)
        {
            if (bindingIndex >= bindings.Count) throw new InvalidDataException($"Constant binding index out of range: index={bindingIndex}.");
            GenericBinding binding = bindings[bindingIndex];
            int width = binding.typeID == ClassIDType.Transform ? binding.attribute switch { 1 => 3, 2 => 4, 3 => 3, _ => throw new InvalidDataException($"Unexpected constant transform attribute: {binding.attribute}.") } : 1;
            if (offset + width > buffer.m_ConstantValues.Length) throw new InvalidDataException("Constant value list is shorter than binding component widths.");
            float maximum = Enumerable.Range(0, frameDomain.CommonFrameCount).SelectMany(frame => Enumerable.Range(0, width).Select(component => Math.Abs(buffer.m_ConstantValues[offset + component] - GetComponent(transforms[frame], scalars[frame], bindingIndex, count, scalarStart, scaleTracksByBinding, component)))).Max();
            if (maximum > 0.00001f) throw new InvalidDataException($"Decoded constant does not match source constant: binding={bindingIndex}, maximum_absolute_difference={maximum}.");
            comparisons.Add(new ConstantComparison(bindingIndex, offset, width, maximum));
            offset += width;
        }
        if (offset != buffer.m_ConstantValues.Length) throw new InvalidDataException($"Unconsumed constant values: consumed={offset}, total={buffer.m_ConstantValues.Length}.");
        var scalarNames = scalarBindings.Take(buffer.RootTrackCount).ToArray();
        File.WriteAllText(Path.Combine(output, "bound_tracks.json"), JsonSerializer.Serialize(tracks, options));
        File.WriteAllText(Path.Combine(output, "scalar_bindings.json"), JsonSerializer.Serialize(scalarBindings, options));
        File.WriteAllText(Path.Combine(output, "constant_comparisons.json"), JsonSerializer.Serialize(comparisons.ToArray(), options));
        var changingTracks = Enumerable.Range(0, count).Where(track => transforms.Skip(1).Any(row => !row.Tracks[track].Translation.SequenceEqual(transforms[0].Tracks[track].Translation) || !row.Tracks[track].Rotation.SequenceEqual(transforms[0].Tracks[track].Rotation) || (tracks[track].HasScale && !row.Tracks[track].Scale.SequenceEqual(transforms[0].Tracks[track].Scale)))).ToArray();
        byte[] data = buffer.TransformBufferData;
        var body = new { segments = ReadUInt(data, 32), animated_rotation_tracks = ReadUInt(data, 40), animated_translation_tracks = ReadUInt(data, 44), animated_scale_tracks = ReadUInt(data, 48), constant_rotation_tracks = ReadUInt(data, 52), constant_translation_tracks = ReadUInt(data, 56), default_rotation_tracks = count - ReadUInt(data, 40) - ReadUInt(data, 52), default_translation_tracks = count - ReadUInt(data, 44) - ReadUInt(data, 56), misc_hex = ReadUInt(data, 28).ToString("x8") };
        File.WriteAllText(Path.Combine(output, "binding_verification.json"), JsonSerializer.Serialize(new { status = "single_clip_decoded_and_source_bindings_verified", source_audit = sourceAuditPath, source_transform_count = sourcePaths.Length, binding_count = bindings.Count, mapped_transform_tracks = tracks.Length, unresolved_transform_tracks = 0, ambiguous_transform_tracks = 0, transform_position_bindings = count, transform_rotation_bindings = count, scale_bindings = scaleCount, scale_binding_tracks = scaleTracksByBinding, scalar_bindings = scalarBindings.Length, constant_bindings_verified = comparisons.Count, constant_values_verified = offset, maximum_constant_difference = comparisons.Count == 0 ? 0f : comparisons.Max(row => row.MaximumAbsoluteDifference), default_binding_indices = buffer.m_DefaultIndexs, constant_subtrack_mask_bytes = buffer.TransformSubTrackConstantMasks.Length, mask_bytes = buffer.TransformSubTrackMasks.Length, position_rotation_scale_masks = "component-major; position and rotation set for all actual tracks; scale bits agree one-to-one with source scale bindings", animated_transform_tracks = changingTracks.Length, animated_track_indices = changingTracks, root_float_comparison_status = "per_track_acl_encoding_bound_verified", root_float_prefix_compared_tracks = buffer.RootTrackCount, root_float_prefix_maximum_difference = rootError, root_float_prefix_tolerance_basis = "Independent ACL buffers: sample difference <= root quantization step + float quantization step + decoder float32 margin; range envelopes independently bounded by smaller step + 4 endpoint ULPs; constants remain bit-identical.", root_float_prefix_comparisons = rootFloatComparisons, root_scalar_bindings = scalarNames, root_indices = new[] { buffer.RootPosIndex, buffer.RootRotIndex, buffer.RootScaleIndex }, acl_transform_body = body, time_domain = frameDomain, validation_boundary = "Every stored frame remains in the raw ACL outputs and was decoded. Timestamp alignment and constant/root comparisons cover only the explicitly reported shared time domain exported to the candidate. Unity runtime pose corrections, controller application, coordinate conversion and rendered playback are not validated." }, options));
    }

    private static CommonFrameDomain ValidateFrameDomain(TransformSample[] transforms, FloatSample[] roots, FloatSample[] scalars, int expectedTransformTracks, int expectedRootTracks, int expectedFloatTracks, string output)
    {
        if (transforms.Length == 0 || roots.Length == 0 || scalars.Length == 0) throw new InvalidDataException($"ACL frame buffers must be nonempty: transform={transforms.Length}, root_motion={roots.Length}, float={scalars.Length}.");
        if (expectedRootTracks > expectedFloatTracks) throw new InvalidDataException($"Root track count exceeds float track count: root={expectedRootTracks}, float={expectedFloatTracks}.");
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
        return new CommonFrameDomain(
            "shared_timestamp_prefix_verified",
            commonFrameCount,
            transformTimes[0],
            transformTimes[commonFrameCount - 1],
            maximumDifference,
            FrameTimeToleranceSeconds,
            MakeBufferDomain(transformTimes, commonFrameCount),
            MakeBufferDomain(rootTimes, commonFrameCount),
            MakeBufferDomain(scalarTimes, commonFrameCount),
            new RawAclBufferHashes(rawHashes[0], rawHashes[1], rawHashes[2]),
            true,
            "Candidate frames are restricted to the prefix whose timestamps are present and aligned in all three buffers. No raw ACL buffer or full native sample JSON is truncated, padded, or rewritten.");
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
    private static uint ReadUInt(byte[] data, int offset) => BinaryPrimitives.ReadUInt32LittleEndian(data.AsSpan(offset, 4));
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
    private static BoundTrack BindTrack(int index, int count, List<GenericBinding> bindings, Dictionary<uint, string[]> paths, byte[] masks, Dictionary<uint, int> scales)
    {
        GenericBinding position = bindings[index];
        GenericBinding rotation = bindings[index + count];
        if (position.typeID != ClassIDType.Transform || position.attribute != 1 || rotation.typeID != ClassIDType.Transform || rotation.attribute != 2 || position.path != rotation.path) throw new InvalidDataException($"Transform binding blocks do not align: track={index}.");
        if (!paths.TryGetValue(position.path, out var candidates) || candidates.Length != 1) throw new InvalidDataException($"Transform CRC path is missing or ambiguous: track={index}, hash={position.path}.");
        bool hasPosition = MaskBit(masks, count, 0, index);
        bool hasRotation = MaskBit(masks, count, 1, index);
        bool hasScale = MaskBit(masks, count, 2, index);
        if (!hasPosition || !hasRotation || hasScale != scales.ContainsKey(position.path)) throw new InvalidDataException($"Source subtrack mask disagrees with bindings: track={index}, has_position={hasPosition}, has_rotation={hasRotation}, has_scale={hasScale}.");
        return new BoundTrack(index, position.path, candidates[0], index, index + count, hasPosition, hasRotation, hasScale);
    }
    private static float GetComponent(TransformSample transform, FloatSample scalar, int binding, int count, int scalarStart, Dictionary<int, int> scaleTracks, int component) => binding < count ? transform.Tracks[binding].Translation[component] : binding < count * 2 ? transform.Tracks[binding - count].Rotation[component] : binding < scalarStart ? transform.Tracks[scaleTracks[binding]].Scale[component] : scalar.Values[binding - scalarStart];
}

