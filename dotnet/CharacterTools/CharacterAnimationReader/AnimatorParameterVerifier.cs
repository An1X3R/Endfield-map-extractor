using System.Text.Json;
using AnimeStudio;

internal sealed record AnimatorParameter(int ScalarTrack, uint AttributeHash, string? Name, float FirstValue, float LastValue, bool Changes);

/// <summary>Resolves sampled Animator parameter hashes against one exact controller TOS.</summary>
internal static class AnimatorParameterVerifier
{
    internal static void Verify(AnimationClip clip, string controllerBundle, string output, JsonSerializerOptions options)
    {
        var logger = new DiagnosticLogger();
        Logger.Default = logger;
        TypeFlags.SetTypes(new[] { ClassIDType.AssetBundle, ClassIDType.AnimatorController }.ToDictionary(type => type, type => (true, false)));
        var manager = new AssetsManager { Game = GameManager.GetGameByType(GameType.ArknightsEndfield), ResolveDependencies = false, SpecifyUnityVersion = "2021.3.34f5", Silent = true, SkipProcess = false };
        try
        {
            manager.LoadPreparedFiles([controllerBundle]);
            File.WriteAllText(Path.Combine(output, "controller_reader_logs.json"), JsonSerializer.Serialize(logger.Snapshot(), options));
            logger.RequireSuccess();
            var controller = manager.assetsFileList.SelectMany(file => file.Objects).Single(value => value.type == ClassIDType.AnimatorController);
            var tree = controller.ToType() ?? throw new InvalidDataException("Endfield controller has no embedded TypeTree.");
            string treeJson = Newtonsoft.Json.JsonConvert.SerializeObject(tree, Newtonsoft.Json.Formatting.Indented);
            File.WriteAllText(Path.Combine(output, "controller_type_tree.json"), treeJson);
            File.WriteAllText(Path.Combine(output, "controller_source_references.json"), JsonSerializer.Serialize(new { source = controller.assetsFile.fileName, controller_path_id = controller.m_PathID, external_cabs = controller.assetsFile.m_Externals.Select(external => external.fileName).ToArray(), dependencies_loaded = false }, options));
            using var document = JsonDocument.Parse(treeJson);
            JsonElement source = document.RootElement;
            string name = source.GetProperty("m_Name").GetString() ?? throw new InvalidDataException("Controller source name is null.");
            string[] tosNames = source.GetProperty("m_TOSData").EnumerateArray().Select(value => value.GetString() ?? throw new InvalidDataException("Controller TOS string is null.")).Distinct().ToArray();
            var hashedNames = tosNames.GroupBy(BindingVerifier.HashPath).ToDictionary(group => group.Key, group => group.ToArray());
            if (hashedNames.Values.Any(names => names.Length != 1)) throw new InvalidDataException("Controller TOS string hashes have collisions.");
            var clipPointers = source.GetProperty("m_AnimationClips").EnumerateArray().Select(value => new { FileId = value.GetProperty("m_FileID").GetInt32(), PathId = value.GetProperty("m_PathID").GetInt64() }).Where(pointer => pointer.PathId == clip.m_PathID && pointer.FileId > 0 && pointer.FileId <= controller.assetsFile.m_Externals.Count && string.Equals(controller.assetsFile.m_Externals[pointer.FileId - 1].fileName, clip.assetsFile.fileName, StringComparison.OrdinalIgnoreCase)).ToArray();
            if (clipPointers.Length != 1) throw new InvalidDataException($"Expected exact clip pointer in source controller: controller={name}, clip={clip.m_Name}, references={clipPointers.Length}.");
            var samples = JsonSerializer.Deserialize<FloatSample[]>(File.ReadAllText(Path.Combine(output, "float_samples.json")), options) ?? throw new InvalidDataException("Scalar samples are null.");
            var scalarBindings = JsonSerializer.Deserialize<ScalarBinding[]>(File.ReadAllText(Path.Combine(output, "scalar_bindings.json")), options) ?? throw new InvalidDataException("Verified scalar bindings are null.");
            int scalarStart = clip.m_ClipBindingConstant.genericBindings.Count - clip.m_AclCompressedBuffer.FloatCurveCount;
            if (scalarBindings.Length != clip.m_AclCompressedBuffer.FloatCurveCount || samples.Length == 0 || samples.Any(sample => sample.Values.Length != scalarBindings.Length)) throw new InvalidDataException("Verified scalar binding and sample dimensions disagree with the source clip.");
            if (scalarBindings.Where((binding, index) => binding.Track != index || binding.Binding != scalarStart + index).Any()) throw new InvalidDataException("Scalar parameter indices disagree with the source binding layout.");
            var rows = scalarBindings.Select(row => new { Binding = clip.m_ClipBindingConstant.genericBindings[row.Binding], Track = row.Track }).Where(row => row.Binding.customType == 0 && row.Binding.typeID == ClassIDType.Animator).Select(row => new AnimatorParameter(row.Track, row.Binding.attribute, hashedNames.TryGetValue(row.Binding.attribute, out var names) ? names[0] : null, samples[0].Values[row.Track], samples[^1].Values[row.Track], samples.Skip(1).Any(sample => sample.Values[row.Track] != samples[0].Values[row.Track]))).ToArray();
            File.WriteAllText(Path.Combine(output, "animator_parameter_verification.json"), JsonSerializer.Serialize(new { status = "exact_controller_clip_reference_and_tos_verified", source = controller.assetsFile.fileName, controller = name, controller_path_id = controller.m_PathID, external_bundle_count = controller.assetsFile.m_Externals.Count, dependencies_loaded = false, source_format = "Endfield embedded TypeTree m_TOSData strings; CRC32 matched", clip_reference_file_id = clipPointers[0].FileId, clip_reference_path_id = clipPointers[0].PathId, parameter_count = rows.Length, resolved_parameters = rows.Count(row => row.Name != null), binding_type_id = (int)ClassIDType.Animator, binding_custom_type = 0, parameters = rows, script_binding_summary = new { binding_count = clip.m_ClipBindingConstant.genericBindings.Count, non_null_script_count = clip.m_ClipBindingConstant.genericBindings.Count(binding => binding.script.m_FileID != 0 || binding.script.m_PathID != 0) }, interpretation = "CustomType=0 is an Animator parameter binding, not a direct material or skeletal morph binding." }, options));
        }
        finally { manager.Clear(); }
    }
}


