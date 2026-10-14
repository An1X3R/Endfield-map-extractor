using System.Buffers.Binary;
using System.Security.Cryptography;
using System.Text.Json;
using AnimeStudio;
using Ruri.ACL;

internal sealed record ReaderLog(string Severity, string Message);
internal sealed record Header(int Bytes, uint DeclaredBytes, uint Hash, string Magic, ushort Version, byte Algorithm, byte TrackType, uint Tracks, uint Samples, float SampleRate, uint Misc, string TrailingBytesHex);
internal sealed record TransformValue(int Track, float[] Rotation, float[] Translation, float[] Scale);
internal sealed record TransformSample(float Time, TransformValue[] Tracks);
internal sealed record FloatSample(float Time, float[] Values);

/// <summary>Preserves upstream warnings and rejects any reader error.</summary>
internal sealed class DiagnosticLogger : ILogger
{
    private readonly List<ReaderLog> entries = new();
    public void Log(LoggerEvent severity, string message)
    {
        if (entries.Count >= 1000) throw new InvalidDataException("Reader exceeded 1000 diagnostic records.");
        entries.Add(new ReaderLog(severity.ToString(), message));
    }
    public ReaderLog[] Snapshot() => entries.ToArray();
    public void RequireSuccess()
    {
        if (entries.Any(entry => entry.Severity == "Error" || entry.Message.Contains("Falling back", StringComparison.OrdinalIgnoreCase)))
            throw new InvalidDataException("Reader emitted an error or parser fallback; inspect reader_logs.json.");
    }
}

internal static class Program
{
    private static readonly JsonSerializerOptions JsonOptions = new() { PropertyNamingPolicy = JsonNamingPolicy.SnakeCaseLower, WriteIndented = true };
    private static Header ReadHeader(byte[] data)
    {
        if (data.Length < 32) throw new InvalidDataException($"ACL header shorter than 32 bytes: bytes={data.Length}.");
        uint declared = BinaryPrimitives.ReadUInt32LittleEndian(data.AsSpan(0, 4));
        uint magic = BinaryPrimitives.ReadUInt32LittleEndian(data.AsSpan(8, 4));
        ushort version = BinaryPrimitives.ReadUInt16LittleEndian(data.AsSpan(12, 2));
        byte type = data[15];
        uint tracks = BinaryPrimitives.ReadUInt32LittleEndian(data.AsSpan(16, 4));
        uint samples = BinaryPrimitives.ReadUInt32LittleEndian(data.AsSpan(20, 4));
        float rate = BitConverter.ToSingle(data, 24);
        if (declared > data.Length || declared < 32 || magic != 0xac11ac11 || version < 7 || version > 10 || data[14] != 0 || type is not (0 or 12) || tracks == 0 || samples == 0 || !float.IsFinite(rate) || rate <= 0)
            throw new InvalidDataException($"ACL header rejected: bytes={data.Length}, declared={declared}, magic={magic:x8}, version={version}, type={type}, tracks={tracks}, samples={samples}, rate={rate}.");
        return new Header(data.Length, declared, BinaryPrimitives.ReadUInt32LittleEndian(data.AsSpan(4, 4)), magic.ToString("x8"), version, data[14], type, tracks, samples, rate, BinaryPrimitives.ReadUInt32LittleEndian(data.AsSpan(28, 4)), Convert.ToHexString(data.AsSpan((int)declared)));
    }
    private static void WriteJson<T>(string output, string name, T value) => File.WriteAllText(Path.Combine(output, name), JsonSerializer.Serialize(value, JsonOptions));
    private static TransformValue ToValue(AclTransform transform, int index)
    {
        float[] rotation = [transform.Rotation.X, transform.Rotation.Y, transform.Rotation.Z, transform.Rotation.W];
        float[] translation = [transform.Translation.X, transform.Translation.Y, transform.Translation.Z];
        float[] scale = [transform.Scale.X, transform.Scale.Y, transform.Scale.Z];
        if (rotation.Concat(translation).Concat(scale).Any(value => !float.IsFinite(value))) throw new InvalidDataException($"Nonfinite decoded transform: track={index}.");
        if (Math.Abs(transform.Rotation.Length() - 1) > 0.001f) throw new InvalidDataException($"Nonunit decoded quaternion: track={index}, length={transform.Rotation.Length()}.");
        return new TransformValue(index, rotation, translation, scale);
    }
    private static void SampleTransforms(byte[] data, string output, string name)
    {
        if (data.Length == 0) { WriteJson(output, name + "_status.json", new { state = "source_buffer_empty" }); return; }
        Header header = ReadHeader(data);
        if (header.TrackType != 12) throw new InvalidDataException($"Expected qvvf transform buffer: name={name}, type={header.TrackType}.");
        WriteJson(output, name + "_header.json", header);
        File.WriteAllBytes(Path.Combine(output, name + ".acl"), data);
        var decoder = new AclDecompressor(data);
        if (!decoder.IsValid || decoder.NumTracks != header.Tracks) throw new InvalidDataException($"Ruri ACL transform decoder rejected: name={name}.");
        var samples = new List<TransformSample>();
        for (int frame = 0; frame < header.Samples; frame++)
        {
            float time = frame / header.SampleRate;
            if (!decoder.Sample(time, out var values) || values.Length != header.Tracks) throw new InvalidDataException($"Transform sampling failed: name={name}, frame={frame}, time={time}.");
            samples.Add(new TransformSample(time, values.Select(ToValue).ToArray()));
        }
        AnimationFrameJson.Write(output, name + "_samples.json", samples);
        WriteJson(output, name + "_status.json", new { state = "all_stored_frames_decoded", duration = decoder.Duration, frames = samples.Count, tracks = decoder.NumTracks });
    }
    private static void SampleFloats(byte[] data, string output, string name)
    {
        if (data.Length == 0) { WriteJson(output, name + "_status.json", new { state = "source_buffer_empty" }); return; }
        Header header = ReadHeader(data);
        if (header.TrackType != 0) throw new InvalidDataException($"Expected float1f buffer: type={header.TrackType}.");
        WriteJson(output, name + "_header.json", header);
        File.WriteAllBytes(Path.Combine(output, name + ".acl"), data);
        var decoder = new AclFloatDecompressor(data);
        if (!decoder.IsValid || decoder.NumTracks != header.Tracks) throw new InvalidDataException("Ruri ACL float decoder rejected source buffer.");
        var samples = new List<FloatSample>();
        for (int frame = 0; frame < header.Samples; frame++)
        {
            float time = frame / header.SampleRate;
            if (!decoder.Sample(time, out var values) || values.Length != header.Tracks || values.Any(value => !float.IsFinite(value))) throw new InvalidDataException($"Float sampling failed: frame={frame}, time={time}.");
            samples.Add(new FloatSample(time, values));
        }
        AnimationFrameJson.Write(output, name + "_samples.json", samples);
        WriteJson(output, name + "_status.json", new { state = "all_stored_frames_decoded", duration = decoder.Duration, frames = samples.Count, tracks = decoder.NumTracks });
    }
    internal static int Run(AnimationRequest request)
    {
        
        string output = Path.GetFullPath(Path.Combine(request.Output, "native"));
        if (Directory.Exists(output)) throw new IOException($"Output already exists: {output}.");
        Directory.CreateDirectory(output);
        var logger = new DiagnosticLogger();
        Logger.Default = logger;
        Logger.Flags = LoggerEvent.Warning | LoggerEvent.Error;
        TypeFlags.SetTypes(new[] { ClassIDType.AssetBundle, ClassIDType.AnimationClip }.ToDictionary(type => type, type => (true, false)));
        var manager = new AssetsManager { Game = GameManager.GetGameByType(GameType.ArknightsEndfield), ResolveDependencies = false, SpecifyUnityVersion = request.UnityVersion, Silent = true, SkipProcess = false };
        try
        {
            manager.LoadPreparedFiles([Path.GetFullPath(request.ClipBundle)]);
            WriteJson(output, "reader_logs.json", logger.Snapshot());
            logger.RequireSuccess();
            var containers = manager.assetsFileList.SelectMany(file => file.Objects.OfType<AssetBundle>()).SelectMany(bundle => bundle.m_Container).Where(entry => string.Equals(entry.Key, request.ResourcePath, StringComparison.OrdinalIgnoreCase)).ToArray();
            if (containers.Length != 1 || !containers[0].Value.asset.TryGet(out var asset) || asset is not AnimationClip clip) throw new InvalidDataException($"Expected one exact AnimationClip container: resource={request.ResourcePath}, matches={containers.Length}.");
            var buffer = clip.m_AclCompressedBuffer ?? throw new InvalidDataException("Clip has no new ACL compressed buffer.");
            var bindings = clip.m_ClipBindingConstant.genericBindings.Select((binding, index) => new { index, binding.path, binding.attribute, type = binding.typeID.ToString(), binding.customType, binding.isPPtrCurve, binding.isIntCurve, script_file_id = binding.script.m_FileID, script_path_id = binding.script.m_PathID, script_name = binding.script.TryGet(out var scriptAsset) && scriptAsset is NamedObject namedScript ? namedScript.m_Name : null }).ToArray();
            WriteJson(output, "clip_audit.json", new { source = clip.assetsFile.fileName, clip.m_Name, path_id = clip.m_PathID, resource_path = containers[0].Key, clip.m_SampleRate, clip.m_Legacy, clip.m_Compressed, clip.m_AnimationType, clip.m_HasGenericRootTransform, clip.m_HasMotionFloatCurves, start = clip.m_MuscleClip.m_StartTime, stop = clip.m_MuscleClip.m_StopTime, buffer.Version, buffer.OutputTrackCount, buffer.RootPosIndex, buffer.RootRotIndex, buffer.RootScaleIndex, buffer.RootTrackCount, buffer.FloatCurveCount, transform_buffer_bytes = buffer.TransformBufferData.Length, root_motion_buffer_bytes = buffer.RootMotionBufferData.Length, float_buffer_bytes = buffer.FloatBufferData.Length, transform_sub_track_masks = buffer.TransformSubTrackMasks.Select(value => (int)value).ToArray(), transform_sub_track_constant_masks = buffer.TransformSubTrackConstantMasks.Select(value => (int)value).ToArray(), default_indices = buffer.m_DefaultIndexs, constant_indices = buffer.m_ConstantIndexs, constant_values = buffer.m_ConstantValues, old_acl_is_set = clip.m_MuscleClip.m_Clip.m_ACLClip.IsSet, bindings, buffer_sha256 = Convert.ToHexString(SHA256.HashData(buffer.TransformBufferData)) });
            foreach (byte[] sourceBuffer in new[] { buffer.TransformBufferData, buffer.RootMotionBufferData, buffer.FloatBufferData }.Where(value => value.Length > 0))
                if (ReadHeader(sourceBuffer).Samples > request.MaximumStoredFrames) throw new InvalidDataException("Clip exceeds explicit maximum stored frame budget.");
            SampleTransforms(buffer.TransformBufferData, output, "transform");
            SampleFloats(buffer.RootMotionBufferData, output, "root_motion");
            SampleFloats(buffer.FloatBufferData, output, "float");
            BindingVerifier.Verify(clip, Path.GetFullPath(request.SourceAudit), output, JsonOptions);
            if (request.ControllerBundle is not null) AnimatorParameterVerifier.Verify(clip, Path.GetFullPath(request.ControllerBundle), output, JsonOptions);
            else WriteJson(output, "animator_parameter_verification.json", new { status = "controller_not_supplied_scalar_hashes_preserved_without_name_resolution" });
            WriteJson(output, "receipt.json", new { state = "source_clip_parsed_and_stored_frames_sampled", output, library = typeof(AclDecompressor).Assembly.FullName, reader = typeof(AnimationClip).Assembly.FullName });
            Console.WriteLine(JsonSerializer.Serialize(new { status = "sampled", output }, JsonOptions));
            return 0;
        }
        finally { manager.Clear(); }
    }
}





