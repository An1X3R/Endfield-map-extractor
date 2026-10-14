using System.Reflection;
using System.Runtime.ExceptionServices;
using System.Security.Cryptography;
using System.Text.Json;
using AnimeStudio;

// Reuses the existing compiled ACL sampler without changing its full-action validation gates.
internal static class AuxiliarySampler
{
    private static readonly JsonSerializerOptions Options = new() { WriteIndented = true };
    private static object? Invoke(Type sampler, string name, params object[] arguments)
    {
        var method = sampler.GetMethod(name, BindingFlags.Static | BindingFlags.NonPublic) ?? throw new MissingMethodException(sampler.FullName, name);
        try { return method.Invoke(null, arguments); }
        catch (TargetInvocationException failure) when (failure.InnerException is not null)
        { ExceptionDispatchInfo.Capture(failure.InnerException).Throw(); throw; }
    }
    private static void Write(string output, string name, object value) => File.WriteAllText(Path.Combine(output, name), JsonSerializer.Serialize(value, Options));
    public static int Main(string[] args)
    {
        if (args.Length != 1) throw new ArgumentException("Expected one exact auxiliary sampling request.");
        using var request = JsonDocument.Parse(File.ReadAllText(args[0]));
        var root = request.RootElement;
        if (root.GetProperty("format").GetString() != "EndfieldAuxiliaryClipSampling/1") throw new InvalidDataException("Unsupported auxiliary sampling format.");
        string output = Path.GetFullPath(root.GetProperty("output").GetString()!);
        if (Directory.Exists(output)) throw new IOException("Refusing to reuse auxiliary sampling output.");
        Directory.CreateDirectory(output);
        var assembly = Assembly.LoadFrom(Path.Combine(AppContext.BaseDirectory, "CharacterAnimationReader.dll"));
        var sampler = assembly.GetType("Program", true)!;
        var log = (ILogger)Activator.CreateInstance(assembly.GetType("DiagnosticLogger", true)!)!;
        Logger.Default = log;
        Logger.Flags = LoggerEvent.Warning | LoggerEvent.Error;
        TypeFlags.SetTypes(new[] { ClassIDType.AssetBundle, ClassIDType.AnimationClip }.ToDictionary(type => type, type => (true, false)));
        var manager = new AssetsManager { Game = GameManager.GetGameByType(GameType.ArknightsEndfield), ResolveDependencies = false, SpecifyUnityVersion = root.GetProperty("unity_version").GetString(), Silent = true, SkipProcess = false };
        try
        {
            manager.LoadPreparedFiles(root.GetProperty("targets").EnumerateArray().Select(target => target.GetProperty("bundle").GetString()!).Distinct().ToArray());
            Write(output, "reader_logs.json", log.GetType().GetMethod("Snapshot")!.Invoke(log, null)!);
            log.GetType().GetMethod("RequireSuccess")!.Invoke(log, null);
            foreach (var target in root.GetProperty("targets").EnumerateArray())
            {
                string cab = target.GetProperty("cab").GetString()!;
                long pathId = target.GetProperty("path_id").GetInt64();
                var clip = manager.assetsFileList.SelectMany(file => file.Objects.OfType<AnimationClip>()).Single(value => value.assetsFile.fileName.Equals(cab, StringComparison.OrdinalIgnoreCase) && value.m_PathID == pathId);
                var buffer = clip.m_AclCompressedBuffer ?? throw new InvalidDataException("Auxiliary clip has no supported ACL buffer.");
                string itemOutput = Path.Combine(output, target.GetProperty("index").GetInt32().ToString());
                Directory.CreateDirectory(itemOutput);
                var containers = manager.assetsFileList.SelectMany(file => file.Objects.OfType<AssetBundle>()).SelectMany(bundle => bundle.m_Container).Where(entry => entry.Value.asset.TryGet(out var value) && ReferenceEquals(value, clip)).Select(entry => entry.Key).ToArray();
                Write(itemOutput, "clip_source.json", new { cab, pathId, clip.m_Name, containers, rawDataSha256 = Convert.ToHexString(SHA256.HashData(clip.GetRawData())), clip.m_SampleRate, clip.m_AnimationType, clip.m_HasGenericRootTransform, start = clip.m_MuscleClip.m_StartTime, stop = clip.m_MuscleClip.m_StopTime, clip.m_MuscleClip.m_IndexArray, clip.m_MuscleClip.m_ValueArrayReferencePose, clip.m_MuscleClip.m_LoopTime, clip.m_MuscleClip.m_LoopBlend, clip.m_MuscleClip.m_LoopBlendOrientation, clip.m_MuscleClip.m_LoopBlendPositionY, clip.m_MuscleClip.m_LoopBlendPositionXZ, clip.m_MuscleClip.m_CycleOffset, buffer.OutputTrackCount, buffer.FloatCurveCount, buffer.RootTrackCount, masks = buffer.TransformSubTrackMasks, defaultIndices = buffer.m_DefaultIndexs, constantIndices = buffer.m_ConstantIndexs, constantValues = buffer.m_ConstantValues, bindings = clip.m_ClipBindingConstant.genericBindings.Select((binding, index) => new { index, binding.path, binding.attribute, type = binding.typeID.ToString(), binding.customType }) });
                File.WriteAllText(Path.Combine(itemOutput, "value_array_delta.json"), Newtonsoft.Json.JsonConvert.SerializeObject(clip.m_MuscleClip.m_ValueArrayDelta));
                foreach (byte[] source in new[] { buffer.TransformBufferData, buffer.RootMotionBufferData, buffer.FloatBufferData }.Where(value => value.Length > 0))
                {
                    var header = Invoke(sampler, "ReadHeader", source)!;
                    uint samples = (uint)header.GetType().GetProperty("Samples")!.GetValue(header)!;
                    if (samples > root.GetProperty("maximum_stored_frames").GetInt32()) throw new InvalidDataException("Auxiliary clip exceeds explicit stored frame budget.");
                }
                Invoke(sampler, "SampleTransforms", buffer.TransformBufferData, itemOutput, "transform");
                Invoke(sampler, "SampleFloats", buffer.RootMotionBufferData, itemOutput, "root_motion");
                Invoke(sampler, "SampleFloats", buffer.FloatBufferData, itemOutput, "float");
                try
                {
                    Invoke(assembly.GetType("BindingVerifier", true)!, "Verify", clip, root.GetProperty("source_audit").GetString()!, itemOutput, new JsonSerializerOptions { WriteIndented = true, PropertyNamingPolicy = JsonNamingPolicy.SnakeCaseLower });
                    Write(itemOutput, "verification_status.json", new { status = "existing_binding_verifier_passed" });
                }
                catch (InvalidDataException failure)
                { Write(itemOutput, "verification_status.json", new { status = "source_binding_validation_rejected", cause = failure.Message }); }
            }
            Write(output, "receipt.json", new { status = "exact_auxiliary_clip_pointers_sampled", samplerSha256 = Convert.ToHexString(SHA256.HashData(File.ReadAllBytes(assembly.Location))), runtime_state_reconstructed = false });
            return 0;
        }
        finally { manager.Clear(); }
    }
}
