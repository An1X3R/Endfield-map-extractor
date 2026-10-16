using System.Buffers.Binary;

internal sealed record ScalarTrackEncoding(string Mode, int Bits, float? Minimum, float? Extent, float? ConstantValue, float QuantizationStep);
internal sealed record EncodingDifferenceBounds(float AllowedDifference, float RangeEnvelopeDifference, float AllowedRangeEnvelopeDifference);
internal sealed record RootFloatTrackComparison(int Track, string? AttributeName, int RootBits, string RootMode, float RootQuantizationStep, int FloatBits, string FloatMode, float FloatQuantizationStep, float RangeEnvelopeDifference, float AllowedRangeEnvelopeDifference, float MaximumAbsoluteDifference, float AllowedDifference, bool WithinEncodingBound);

/// <summary>Checks independent ACL scalar encodings against their per-track quantization bounds.</summary>
internal static class AclScalarPrefixVerifier
{
    private static readonly int[] ScalarBitRatesV0 = [0, 3, 4, 5, 6, 7, 8, 9, 10, 11, 12, 13, 14, 15, 16, 17, 18, 19, 32];
    private static readonly int[] ScalarBitRatesV1 = [0, 1, 2, 3, 4, 5, 6, 7, 8, 9, 10, 11, 12, 13, 14, 15, 16, 17, 18, 19, 20, 21, 22, 23, 32];

    internal static RootFloatTrackComparison[] Compare(byte[] rootData, byte[] floatData, int trackCount, ScalarBinding[] bindings, FloatSample[] roots, FloatSample[] scalars, int frameCount)
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

    private static uint ReadUInt(byte[] data, int offset) => BinaryPrimitives.ReadUInt32LittleEndian(data.AsSpan(offset, 4));
}
