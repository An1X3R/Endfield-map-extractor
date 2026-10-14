using System.Buffers.Binary;
using System.Security.Cryptography;
using System.Text;
using System.Text.Json;
using AnimeStudio;

internal sealed record BoundTrack(int Track, uint PathHash, string SourceTransformPath, int PositionBinding, int RotationBinding, bool HasPosition, bool HasRotation, bool HasScale);
internal sealed record ScalarBinding(int Track, int Binding, uint Attribute, int CustomType, string? AttributeName);
internal sealed record ConstantComparison(int Binding, int ValueOffset, int Width, float MaximumAbsoluteDifference);
internal sealed record ScalarTrackEncoding(string Mode, int Bits, float? Minimum, float? Extent, float? ConstantValue, float QuantizationStep);
internal sealed record EncodingDifferenceBounds(float AllowedDifference, float RangeEnvelopeDifference, float AllowedRangeEnvelopeDifference);
internal sealed record RootFloatTrackComparison(int Track, string? AttributeName, int RootBits, string RootMode, float RootQuantizationStep, int FloatBits, string FloatMode, float FloatQuantizationStep, float RangeEnvelopeDifference, float AllowedRangeEnvelopeDifference, float MaximumAbsoluteDifference, float AllowedDifference, bool WithinEncodingBound);
internal sealed record UnmappedTrack(int Track, uint PathHash, string NativeDisposition);
internal sealed record UnmappedConstantBinding(int Binding, uint PathHash, uint Attribute, int Width, string NativeDisposition);
internal sealed record BufferFrameDomain(int OriginalFrameCount, float SourceStartTime, float SourceEndTime, int ExcludedTailFrames, float? ExcludedTailStartTime, float? ExcludedTailEndTime);
internal sealed record RawAclBufferHashes(string Transform, string RootMotion, string Float);
internal sealed record CommonFrameDomain(string Status, int CommonFrameCount, float ExportedStartTime, float ExportedEndTime, float MaximumCommonTimestampDifference, float TimestampToleranceSeconds, BufferFrameDomain Transform, BufferFrameDomain RootMotion, BufferFrameDomain Float, RawAclBufferHashes RawAclBufferSha256, bool RawAclBuffersPreserved, string ExportPolicy);

/// <summary>Validates NPC ACL tracks while preserving native unmapped-Avatar behavior.</summary>
internal static class BindingVerifier
{
    private const float FrameTimeToleranceSeconds = 1e-6f;
    private static readonly int[] ScalarBitRatesV0 = [0, 3, 4, 5, 6, 7, 8, 9, 10, 11, 12, 13, 14, 15, 16, 17, 18, 19, 32];
    private static readonly int[] ScalarBitRatesV1 = [0, 1, 2, 3, 4, 5, 6, 7, 8, 9, 10, 11, 12, 13, 14, 15, 16, 17, 18, 19, 20, 21, 22, 23, 32];

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
        var tracks = new List<BoundTrack>(count);
        var unmappedTracks = new List<UnmappedTrack>();
        for (int index = 0; index < count; index++)
        {
            GenericBinding position = bindings[index];
            GenericBinding rotation = bindings[index + count];
            if (position.typeID != ClassIDType.Transform || position.attribute != 1 || rotation.typeID != ClassIDType.Transform || rotation.attribute != 2 || position.path != rotation.path) throw new InvalidDataException($"Transform binding blocks do not align: track={index}.");
            bool hasPosition = MaskBit(buffer.TransformSubTrackMasks, count, 0, index);
            bool hasRotation = MaskBit(buffer.TransformSubTrackMasks, count, 1, index);
            bool hasScale = MaskBit(buffer.TransformSubTrackMasks, count, 2, index);
            if (!hasPosition || !hasRotation || hasScale != scaleIndices.ContainsKey(position.path)) throw new InvalidDataException($"Source subtrack mask disagrees with bindings: track={index}, has_position={hasPosition}, has_rotation={hasRotation}, has_scale={hasScale}.");
            if (!pathHashes.TryGetValue(position.path, out var candidates))
            {
                unmappedTracks.Add(new UnmappedTrack(index, position.path, "native selected-Avatar lookup misses and stores binding index -1; consumer skips it"));
                continue;
            }
            if (candidates.Length != 1) throw new InvalidDataException($"Transform CRC path is ambiguous: track={index}, hash={position.path}, candidates={candidates.Length}.");
            tracks.Add(new BoundTrack(index, position.path, candidates[0], index, index + count, hasPosition, hasRotation, hasScale));
        }
        if (tracks.Select(track => track.SourceTransformPath).Distinct().Count() != tracks.Count) throw new InvalidDataException("Multiple mapped ACL tracks resolve to one source transform.");
        var mappedScaleIndices = tracks.Where(track => track.HasScale).Select(track => scaleIndices[track.PathHash]).ToArray();
        if (mappedScaleIndices.Distinct().Count() != mappedScaleIndices.Length) throw new InvalidDataException("Mapped ACL scale bindings are duplicated.");
        var scaleTracksByBinding = tracks.Where(track => track.HasScale).ToDictionary(track => scaleIndices[track.PathHash], track => track.Track);
        var scalarBindings = bindings.Skip(scalarStart).Select((binding, index) => new ScalarBinding(index, index + scalarStart, binding.attribute, binding.customType, binding.customType == 8 ? ((HumanoidMuscleType)binding.attribute).Update(binding.version).ToAttributeString() : null)).ToArray();
        if (scalarBindings.Length != buffer.FloatCurveCount || bindings.Skip(scalarStart).Any(binding => binding.typeID != ClassIDType.Animator)) throw new InvalidDataException("Float channels are not the observed Animator scalar layout.");
        var transforms = JsonSerializer.Deserialize<TransformSample[]>(File.ReadAllText(Path.Combine(output, "transform_samples.json")), options) ?? throw new InvalidDataException("Transform sample file is null.");
        var scalars = JsonSerializer.Deserialize<FloatSample[]>(File.ReadAllText(Path.Combine(output, "float_samples.json")), options) ?? throw new InvalidDataException("Float sample file is null.");
        var roots = JsonSerializer.Deserialize<FloatSample[]>(File.ReadAllText(Path.Combine(output, "root_motion_samples.json")), options) ?? throw new InvalidDataException("Root sample file is null.");
        CommonFrameDomain frameDomain = ValidateFrameDomain(transforms, roots, scalars, count, buffer.RootTrackCount, buffer.FloatCurveCount, output);
        File.WriteAllText(Path.Combine(output, "frame_domain_verification.json"), JsonSerializer.Serialize(frameDomain, options));
        bool independentGenericRoot = buffer.RootTrackCount > buffer.FloatCurveCount;
        if (independentGenericRoot) NpcQvvfProgram.ValidateStationaryRoot(scalarBindings, scalars, roots);
        RootFloatTrackComparison[] rootFloatComparisons = independentGenericRoot
            ? Array.Empty<RootFloatTrackComparison>()
            : CompareRootFloatPrefix(buffer.RootMotionBufferData, buffer.FloatBufferData, buffer.RootTrackCount, scalarBindings, roots, scalars, frameDomain.CommonFrameCount);
        RootFloatTrackComparison? failedRootFloatComparison = rootFloatComparisons.FirstOrDefault(row => !row.WithinEncodingBound);
        if (failedRootFloatComparison is not null) throw new InvalidDataException($"Root-motion/float prefix exceeds its per-track ACL encoding bound: track={failedRootFloatComparison.Track}, attribute={failedRootFloatComparison.AttributeName}, root_bits={failedRootFloatComparison.RootBits}, float_bits={failedRootFloatComparison.FloatBits}, maximum_difference={failedRootFloatComparison.MaximumAbsoluteDifference}, allowed_difference={failedRootFloatComparison.AllowedDifference}.");
        float rootError = independentGenericRoot || rootFloatComparisons.Length == 0 ? 0f : rootFloatComparisons.Max(row => row.MaximumAbsoluteDifference);
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
                float maximum = Enumerable.Range(0, frameDomain.CommonFrameCount).SelectMany(frame => Enumerable.Range(0, width).Select(component => Math.Abs(buffer.m_ConstantValues[offset + component] - GetComponent(transforms[frame], scalars[frame], bindingIndex, count, scalarStart, scaleTracksByBinding, component)))).Max();
                if (maximum > 0.00001f) throw new InvalidDataException($"Decoded constant does not match source constant: binding={bindingIndex}, maximum_absolute_difference={maximum}.");
                comparisons.Add(new ConstantComparison(bindingIndex, offset, width, maximum));
            }
            offset += width;
        }
        if (offset != buffer.m_ConstantValues.Length) throw new InvalidDataException($"Unconsumed constant values: consumed={offset}, total={buffer.m_ConstantValues.Length}.");
        var scalarNames = scalarBindings.Take(buffer.RootTrackCount).ToArray();
        File.WriteAllText(Path.Combine(output, "bound_tracks.json"), JsonSerializer.Serialize(tracks.ToArray(), options));
        File.WriteAllText(Path.Combine(output, "unmapped_tracks.json"), JsonSerializer.Serialize(unmappedTracks.ToArray(), options));
        File.WriteAllText(Path.Combine(output, "scalar_bindings.json"), JsonSerializer.Serialize(scalarBindings, options));
        File.WriteAllText(Path.Combine(output, "constant_comparisons.json"), JsonSerializer.Serialize(comparisons.ToArray(), options));
        File.WriteAllText(Path.Combine(output, "unmapped_constant_bindings.json"), JsonSerializer.Serialize(unmappedConstants.ToArray(), options));
        var changingTracks = tracks.Where(track => transforms.Skip(1).Any(row => !row.Tracks[track.Track].Translation.SequenceEqual(transforms[0].Tracks[track.Track].Translation) || !row.Tracks[track.Track].Rotation.SequenceEqual(transforms[0].Tracks[track.Track].Rotation) || (track.HasScale && !row.Tracks[track.Track].Scale.SequenceEqual(transforms[0].Tracks[track.Track].Scale)))).Select(track => track.Track).ToArray();
        byte[] data = buffer.TransformBufferData;
        var body = new { segments = ReadUInt(data, 32), animated_rotation_tracks = ReadUInt(data, 40), animated_translation_tracks = ReadUInt(data, 44), animated_scale_tracks = ReadUInt(data, 48), constant_rotation_tracks = ReadUInt(data, 52), constant_translation_tracks = ReadUInt(data, 56), default_rotation_tracks = count - ReadUInt(data, 40) - ReadUInt(data, 52), default_translation_tracks = count - ReadUInt(data, 44) - ReadUInt(data, 56), misc_hex = ReadUInt(data, 28).ToString("x8") };
        File.WriteAllText(Path.Combine(output, "binding_verification.json"), JsonSerializer.Serialize(new { status = "mapped_selected_avatar_bindings_verified_native_unmapped_skipped", source_audit = sourceAuditPath, source_transform_count = sourcePaths.Length, binding_count = bindings.Count, mapped_transform_tracks = tracks.Count, unresolved_transform_tracks = unmappedTracks.Count, ambiguous_transform_tracks = 0, unmapped_tracks = unmappedTracks, transform_position_bindings = count, transform_rotation_bindings = count, scale_bindings = scaleCount, mapped_scale_binding_indices = mappedScaleIndices, scalar_bindings = scalarBindings.Length, constant_bindings_verified = comparisons.Count, unmapped_constant_bindings = unmappedConstants, constant_values_verified = offset, maximum_constant_difference = comparisons.Count == 0 ? 0f : comparisons.Max(row => row.MaximumAbsoluteDifference), default_binding_indices = buffer.m_DefaultIndexs, constant_subtrack_mask_bytes = buffer.TransformSubTrackConstantMasks.Length, mask_bytes = buffer.TransformSubTrackMasks.Length, position_rotation_scale_masks = "component-major; position and rotation set for all actual tracks; scale bits agree one-to-one with source scale bindings", animated_transform_tracks = changingTracks.Length, animated_track_indices = changingTracks, root_float_comparison_status = independentGenericRoot ? "independent_stationary_identity_root_buffer_preserved_without_scalar_binding_mapping" : "per_track_acl_encoding_bound_verified", root_float_prefix_compared_tracks = independentGenericRoot ? 0 : buffer.RootTrackCount, root_float_prefix_maximum_difference = independentGenericRoot ? (float?)null : rootError, root_float_prefix_tolerance_basis = independentGenericRoot ? "no_cross_buffer_mapping_asserted" : "sample_difference<=root_step+float_step+decoder_float32_margin; range_envelope_difference independently <= smaller_step+4_endpoint_float32_ULPs; constants require bit-identical values; observed range difference is never added to sample allowance", root_float_prefix_comparisons = rootFloatComparisons, root_float_buffer_comparison = new { comparison = independentGenericRoot ? "Independent root and float ACL buffers; stationary identity values checked separately. No cross-buffer semantic mapping asserted." : "Independent RootMotionBufferData and FloatBufferData decoded with the same ACL float decompressor; prefix comparison uses each source track's encoded bit rate and separately bounded range metadata rather than a fixed epsilon.", root_motion_sha256 = frameDomain.RawAclBufferSha256.RootMotion, float_buffer_sha256 = frameDomain.RawAclBufferSha256.Float, root_motion_frames = roots.Length, float_buffer_frames = scalars.Length, common_compared_frames = frameDomain.CommonFrameCount, discrepancies = rootFloatDiscrepancies }, root_scalar_bindings = independentGenericRoot ? Array.Empty<ScalarBinding>() : scalarNames, root_indices = new[] { buffer.RootPosIndex, buffer.RootRotIndex, buffer.RootScaleIndex }, acl_transform_body = body, time_domain = frameDomain, native_unmapped_binding_contract = "Only selected-Avatar path misses are skipped. Ambiguous CRC hashes fail. Based on native AvatarClipBindingsAppend initial -1 map and ClipCacheBindingBuild conditional application; source metadata is recorded in the NPC integration receipt.", validation_boundary = "Every stored frame remains in the raw ACL outputs and was decoded. Timestamp alignment and constant/root comparisons cover the explicitly reported mapped and shared domains. Unmapped Avatar tracks are intentionally not applied. Unity runtime pose corrections, controller application, coordinate conversion and rendered playback are not validated." }, options));
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

    private static RootFloatTrackComparison[] CompareRootFloatPrefix(byte[] rootData, byte[] floatData, int trackCount, ScalarBinding[] bindings, FloatSample[] roots, FloatSample[] scalars, int frameCount)
    {
        if (trackCount > bindings.Length) throw new InvalidDataException($"Root-motion prefix exceeds scalar binding count: root_tracks={trackCount}, scalar_bindings={bindings.Length}.");
        var results = new RootFloatTrackComparison[trackCount];
        for (int track = 0; track < trackCount; track++)
        {
            ScalarBinding binding = bindings[track];
            if (binding.Track != track || binding.CustomType != 8 || binding.Attribute != (uint)track)
                throw new InvalidDataException($"Root-motion prefix has no exact HumanoidMuscleType binding at the same scalar index: track={track}, binding_track={binding.Track}, custom_type={binding.CustomType}, attribute={binding.Attribute}.");

            ScalarTrackEncoding rootEncoding = ReadScalarTrackEncoding(rootData, track, "root_motion");
            ScalarTrackEncoding floatEncoding = ReadScalarTrackEncoding(floatData, track, "float");
            float maximumDifference = Enumerable.Range(0, frameCount).Max(frame => MathF.Abs(roots[frame].Values[track] - scalars[frame].Values[track]));
            float maximumMagnitude = Enumerable.Range(0, frameCount).Max(frame => MathF.Max(MathF.Abs(roots[frame].Values[track]), MathF.Abs(scalars[frame].Values[track])));
            float roundingMargin = 4f * FloatUlp(maximumMagnitude);
            EncodingDifferenceBounds bounds = AllowedEncodingDifference(rootEncoding, floatEncoding, roundingMargin, track);
            results[track] = new RootFloatTrackComparison(track, binding.AttributeName, rootEncoding.Bits, rootEncoding.Mode, rootEncoding.QuantizationStep, floatEncoding.Bits, floatEncoding.Mode, floatEncoding.QuantizationStep, bounds.RangeEnvelopeDifference, bounds.AllowedRangeEnvelopeDifference, maximumDifference, bounds.AllowedDifference, maximumDifference <= bounds.AllowedDifference);
        }
        return results;
    }

    private static EncodingDifferenceBounds AllowedEncodingDifference(ScalarTrackEncoding root, ScalarTrackEncoding scalar, float roundingMargin, int track)
    {
        if (root.Mode == "constant" && scalar.Mode == "constant")
        {
            if (BitConverter.SingleToInt32Bits(root.ConstantValue!.Value) != BitConverter.SingleToInt32Bits(scalar.ConstantValue!.Value))
                throw new InvalidDataException($"Root-motion and float constant encodings differ: track={track}, root={root.ConstantValue}, float={scalar.ConstantValue}.");
            return new EncodingDifferenceBounds(0f, 0f, 0f);
        }
        if (root.Mode == "raw32" && scalar.Mode == "raw32") return new EncodingDifferenceBounds(0f, 0f, 0f);
        if (root.Mode == "quantized" && scalar.Mode == "quantized")
        {
            float rootMaximum = root.Minimum!.Value + root.Extent!.Value;
            float scalarMaximum = scalar.Minimum!.Value + scalar.Extent!.Value;
            if (MathF.Max(root.Minimum.Value, scalar.Minimum.Value) > MathF.Min(rootMaximum, scalarMaximum))
                throw new InvalidDataException($"Root-motion and float quantization ranges do not overlap: track={track}, root_min={root.Minimum}, root_extent={root.Extent}, float_min={scalar.Minimum}, float_extent={scalar.Extent}.");
            float rangeEnvelopeDifference = MathF.Max(MathF.Abs(root.Minimum.Value - scalar.Minimum.Value), MathF.Abs(rootMaximum - scalarMaximum));
            float maximumEndpointUlp = new[] { root.Minimum.Value, rootMaximum, scalar.Minimum.Value, scalarMaximum }.Max(value => FloatUlp(MathF.Abs(value)));
            float allowedRangeEnvelopeDifference = MathF.Min(root.QuantizationStep, scalar.QuantizationStep) + 4f * maximumEndpointUlp;
            if (rangeEnvelopeDifference > allowedRangeEnvelopeDifference)
                throw new InvalidDataException($"Root-motion and float quantization range envelopes diverge beyond their independent ACL metadata bound: track={track}, range_envelope_difference={rangeEnvelopeDifference}, allowed_range_envelope_difference={allowedRangeEnvelopeDifference}, smaller_quantization_step={MathF.Min(root.QuantizationStep, scalar.QuantizationStep)}, endpoint_float32_margin={4f * maximumEndpointUlp}.");
            return new EncodingDifferenceBounds(root.QuantizationStep + scalar.QuantizationStep + roundingMargin, rangeEnvelopeDifference, allowedRangeEnvelopeDifference);
        }
        if (root.Mode == "constant" && scalar.Mode == "quantized")
        {
            RequireConstantInsideRange(root.ConstantValue!.Value, scalar, roundingMargin, track, "root_motion");
            return new EncodingDifferenceBounds(scalar.QuantizationStep + roundingMargin, 0f, 0f);
        }
        if (root.Mode == "quantized" && scalar.Mode == "constant")
        {
            RequireConstantInsideRange(scalar.ConstantValue!.Value, root, roundingMargin, track, "float");
            return new EncodingDifferenceBounds(root.QuantizationStep + roundingMargin, 0f, 0f);
        }
        if (root.Mode == "raw32" && scalar.Mode == "constant") return new EncodingDifferenceBounds(roundingMargin, 0f, 0f);
        if (root.Mode == "constant" && scalar.Mode == "raw32") return new EncodingDifferenceBounds(roundingMargin, 0f, 0f);
        if (root.Mode == "raw32") return new EncodingDifferenceBounds(scalar.QuantizationStep + roundingMargin, 0f, 0f);
        if (scalar.Mode == "raw32") return new EncodingDifferenceBounds(root.QuantizationStep + roundingMargin, 0f, 0f);
        throw new InvalidDataException($"Unsupported root-motion/float scalar encoding pair: track={track}, root_mode={root.Mode}, float_mode={scalar.Mode}.");
    }

    private static void RequireConstantInsideRange(float value, ScalarTrackEncoding range, float margin, int track, string constantBuffer)
    {
        float minimum = range.Minimum!.Value;
        float maximum = minimum + range.Extent!.Value;
        if (value < minimum - margin || value > maximum + margin)
            throw new InvalidDataException($"Constant scalar is outside the paired ACL quantization range: track={track}, constant_buffer={constantBuffer}, constant={value}, range_min={minimum}, range_max={maximum}, rounding_margin={margin}.");
    }

    private static ScalarTrackEncoding ReadScalarTrackEncoding(byte[] data, int track, string bufferName)
    {
        const int bodyOffset = 32;
        if (data.Length < bodyOffset + 20) throw new InvalidDataException($"ACL scalar buffer header is truncated: buffer={bufferName}, bytes={data.Length}.");
        uint declaredBytes = ReadUInt(data, 0);
        ushort version = BinaryPrimitives.ReadUInt16LittleEndian(data.AsSpan(12, 2));
        byte algorithm = data[14];
        byte trackType = data[15];
        uint trackCount = ReadUInt(data, 16);
        if (declaredBytes < bodyOffset + 20 || declaredBytes > data.Length || ReadUInt(data, 8) != 0xac11ac11 || version is < 7 or > 10 || algorithm != 0 || trackType != 0 || track < 0 || track >= trackCount)
            throw new InvalidDataException($"Unsupported ACL scalar encoding header: buffer={bufferName}, declared={declaredBytes}, bytes={data.Length}, version={version}, algorithm={algorithm}, track_type={trackType}, tracks={trackCount}, requested_track={track}.");

        int metaOffset = checked(bodyOffset + (int)ReadUInt(data, bodyOffset + 4));
        int constantOffset = checked(bodyOffset + (int)ReadUInt(data, bodyOffset + 8));
        int rangeOffset = checked(bodyOffset + (int)ReadUInt(data, bodyOffset + 12));
        EnsureSlice(metaOffset, (int)trackCount, (int)declaredBytes, bufferName, "scalar_meta");
        int[] rates = version == 7 ? ScalarBitRatesV0 : ScalarBitRatesV1;
        for (int index = 0; index <= track; index++)
        {
            byte code = data[metaOffset + index];
            if (code >= rates.Length) throw new InvalidDataException($"ACL scalar bit-rate code is invalid: buffer={bufferName}, track={index}, code={code}.");
            int bits = rates[code];
            if (bits == 0)
            {
                EnsureSlice(constantOffset, sizeof(float), (int)declaredBytes, bufferName, "scalar_constant");
                float value = BitConverter.Int32BitsToSingle((int)ReadUInt(data, constantOffset));
                if (!float.IsFinite(value)) throw new InvalidDataException($"ACL scalar constant is nonfinite: buffer={bufferName}, track={index}, value={value}.");
                if (index == track) return new ScalarTrackEncoding("constant", bits, value, 0f, value, 0f);
                constantOffset += sizeof(float);
                continue;
            }
            if (bits == 32)
            {
                if (index == track) return new ScalarTrackEncoding("raw32", bits, null, null, null, 0f);
                continue;
            }

            EnsureSlice(rangeOffset, sizeof(float) * 2, (int)declaredBytes, bufferName, "scalar_range");
            float minimum = BitConverter.Int32BitsToSingle((int)ReadUInt(data, rangeOffset));
            float extent = BitConverter.Int32BitsToSingle((int)ReadUInt(data, rangeOffset + sizeof(float)));
            if (!float.IsFinite(minimum) || !float.IsFinite(extent) || extent < 0f)
                throw new InvalidDataException($"ACL scalar range is invalid: buffer={bufferName}, track={index}, minimum={minimum}, extent={extent}.");
            if (index == track) return new ScalarTrackEncoding("quantized", bits, minimum, extent, null, extent / ((1 << bits) - 1));
            rangeOffset += sizeof(float) * 2;
        }
        throw new InvalidDataException($"ACL scalar track metadata was not resolved: buffer={bufferName}, track={track}.");
    }

    private static void EnsureSlice(int offset, int size, int length, string bufferName, string field)
    {
        if (offset < 0 || size < 0 || offset > length - size)
            throw new InvalidDataException($"ACL scalar metadata slice is outside declared bytes: buffer={bufferName}, field={field}, offset={offset}, size={size}, declared={length}.");
    }

    private static float FloatUlp(float magnitude)
    {
        if (!float.IsFinite(magnitude) || magnitude < 0f) throw new InvalidDataException($"Cannot calculate ACL decoder rounding margin for value {magnitude}.");
        float next = MathF.BitIncrement(magnitude);
        return float.IsFinite(next) ? next - magnitude : magnitude - MathF.BitDecrement(magnitude);
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
    private static float GetComponent(TransformSample transform, FloatSample scalar, int binding, int count, int scalarStart, Dictionary<int, int> scaleTracks, int component) => binding < count ? transform.Tracks[binding].Translation[component] : binding < count * 2 ? transform.Tracks[binding - count].Rotation[component] : binding < scalarStart ? transform.Tracks[scaleTracks[binding]].Scale[component] : scalar.Values[binding - scalarStart];
}
