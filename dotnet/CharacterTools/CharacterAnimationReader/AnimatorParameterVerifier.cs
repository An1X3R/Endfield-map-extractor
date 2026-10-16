using System.Security.Cryptography;
using System.Text.Json;
using AnimeStudio;

internal sealed record AnimatorParameter(int ScalarTrack, uint AttributeHash, string? Name, float FirstValue, float LastValue, bool Changes);

/// <summary>Resolves sampled Animator parameter hashes against one exact controller TOS.</summary>
internal static class AnimatorParameterVerifier
{
    internal static void Verify(AnimationClip clip, AnimationRequest request, string output, JsonSerializerOptions options)
    {
        string controllerBundle = Path.GetFullPath(request.ControllerBundle ?? throw new InvalidDataException("Controller bundle is missing."));
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
            if (clipPointers.Length > 1) throw new InvalidDataException($"Ambiguous clip pointer in source controller: controller={name}, clip={clip.m_Name}, references={clipPointers.Length}.");
            string? selectionHash = request.ControllerSelectionReceipt is null ? null : VerifySelection(clip, request, controller.assetsFile.fileName, controller.m_PathID, clipPointers.Length);
            string? animatorOwnerHash = clipPointers.Length == 0 && selectionHash is null ? VerifyAnimatorOwner(request, controller.assetsFile.fileName, controller.m_PathID) : null;
            string status = clipPointers.Length == 1 ? "exact_controller_clip_reference_and_tos_verified" : "character_controller_dynamic_clip_and_tos_verified";
            var samples = JsonSerializer.Deserialize<FloatSample[]>(File.ReadAllText(Path.Combine(output, "float_samples.json")), options) ?? throw new InvalidDataException("Scalar samples are null.");
            var scalarBindings = JsonSerializer.Deserialize<ScalarBinding[]>(File.ReadAllText(Path.Combine(output, "scalar_bindings.json")), options) ?? throw new InvalidDataException("Verified scalar bindings are null.");
            int scalarStart = clip.m_ClipBindingConstant.genericBindings.Count - clip.m_AclCompressedBuffer.FloatCurveCount;
            if (scalarBindings.Length != clip.m_AclCompressedBuffer.FloatCurveCount || samples.Length == 0 || samples.Any(sample => sample.Values.Length != scalarBindings.Length)) throw new InvalidDataException("Verified scalar binding and sample dimensions disagree with the source clip.");
            if (scalarBindings.Where((binding, index) => binding.Track != index || binding.Binding != scalarStart + index).Any()) throw new InvalidDataException("Scalar parameter indices disagree with the source binding layout.");
            var rows = scalarBindings.Select(row => new { Binding = clip.m_ClipBindingConstant.genericBindings[row.Binding], Track = row.Track }).Where(row => row.Binding.customType == 0 && row.Binding.typeID == ClassIDType.Animator).Select(row => new AnimatorParameter(row.Track, row.Binding.attribute, hashedNames.TryGetValue(row.Binding.attribute, out var names) ? names[0] : null, samples[0].Values[row.Track], samples[^1].Values[row.Track], samples.Skip(1).Any(sample => sample.Values[row.Track] != samples[0].Values[row.Track]))).ToArray();
            File.WriteAllText(Path.Combine(output, "animator_parameter_verification.json"), JsonSerializer.Serialize(new { status, source = controller.assetsFile.fileName, controller = name, controller_path_id = controller.m_PathID, external_bundle_count = controller.assetsFile.m_Externals.Count, dependencies_loaded = false, source_format = "Endfield embedded TypeTree m_TOSData strings; CRC32 matched", clip_reference_file_id = clipPointers.Length == 1 ? (int?)clipPointers[0].FileId : null, clip_reference_path_id = clipPointers.Length == 1 ? (long?)clipPointers[0].PathId : null, selected_clip_source = clip.assetsFile.fileName, selected_clip_path_id = clip.m_PathID, controller_selection_receipt_sha256 = selectionHash, source_animator_controller_sha256 = animatorOwnerHash, parameter_count = rows.Length, resolved_parameters = rows.Count(row => row.Name != null), binding_type_id = (int)ClassIDType.Animator, binding_custom_type = 0, parameters = rows, script_binding_summary = new { binding_count = clip.m_ClipBindingConstant.genericBindings.Count, non_null_script_count = clip.m_ClipBindingConstant.genericBindings.Count(binding => binding.script.m_FileID != 0 || binding.script.m_PathID != 0) }, interpretation = "CustomType=0 is an Animator parameter binding, not a direct material or skeletal morph binding. Character controller ownership is independent of static clip membership; dynamic montage state is not reconstructed." }, options));
        }
        finally { manager.Clear(); }
    }

    private static string VerifySelection(AnimationClip clip, AnimationRequest request, string controllerSource, long controllerPathId, int staticReferences)
    {
        byte[] bytes = File.ReadAllBytes(request.ControllerSelectionReceipt!);
        using var document = JsonDocument.Parse(bytes);
        JsonElement selection = document.RootElement;
        string expectedStatus = staticReferences == 1 ? "source_normal_controller_selected_clip_verified" : "source_normal_controller_dynamic_clip_verified";
        if (selection.GetProperty("format").GetString() != "EndfieldCharacterControllerReceipt/1" || selection.GetProperty("status").GetString() != expectedStatus || selection.GetProperty("scope").GetString() != "source_normal_controller_not_runtime") throw new InvalidDataException("Controller selection receipt format, scope or static membership disagrees with the decoded controller.");
        if (selection.GetProperty("character_id").GetString() != request.CharacterId) throw new InvalidDataException("Controller selection belongs to a different character.");
        string selectedBundle = selection.GetProperty("reader_controller_bundle").GetString() ?? throw new InvalidDataException("Controller selection has no reader bundle.");
        if (!string.Equals(Path.GetFullPath(selectedBundle), Path.GetFullPath(request.ControllerBundle!), StringComparison.OrdinalIgnoreCase)) throw new InvalidDataException("Controller selection bundle path differs from the reader request.");
        string bundleHash = Convert.ToHexString(SHA256.HashData(File.ReadAllBytes(selectedBundle)));
        if (!string.Equals(bundleHash, selection.GetProperty("controller_bundle_sha256").GetString(), StringComparison.OrdinalIgnoreCase)) throw new InvalidDataException("Selected controller bundle bytes changed after source resolution.");
        JsonElement controller = selection.GetProperty("controller");
        if (!string.Equals(controller.GetProperty("cab").GetString(), controllerSource, StringComparison.OrdinalIgnoreCase) || controller.GetProperty("path_id").GetString() != controllerPathId.ToString(System.Globalization.CultureInfo.InvariantCulture)) throw new InvalidDataException("Decoded controller CAB/PathID differs from the character selection.");
        JsonElement selectedClip = selection.GetProperty("selected_clip_source");
        if (selectedClip.GetProperty("type").GetString() != "AnimationClip" || !string.Equals(selectedClip.GetProperty("sourceFile").GetString(), clip.assetsFile.fileName, StringComparison.OrdinalIgnoreCase) || selectedClip.GetProperty("pathId").GetInt64() != clip.m_PathID || !string.Equals(selectedClip.GetProperty("container").GetString(), request.ResourcePath, StringComparison.OrdinalIgnoreCase)) throw new InvalidDataException("Selected action resource/CAB/PathID differs from the decoded clip.");
        if ((selection.GetProperty("clip_reference").ValueKind == JsonValueKind.Null) != (staticReferences == 0)) throw new InvalidDataException("Controller selection misreports static clip membership.");
        return Convert.ToHexString(SHA256.HashData(bytes)).ToLowerInvariant();
    }

    private static string VerifyAnimatorOwner(AnimationRequest request, string controllerSource, long controllerPathId)
    {
        byte[] bytes = File.ReadAllBytes(request.SourceAudit);
        using var document = JsonDocument.Parse(bytes);
        JsonElement animator = document.RootElement.GetProperty("animators").EnumerateArray().Single(row => row.GetProperty("object").GetProperty("name").GetString() == request.AnimatorRootPath.Split('/').Last());
        JsonElement resolved = animator.GetProperty("controller").GetProperty("resolved");
        if (resolved.ValueKind != JsonValueKind.Object || resolved.GetProperty("type").GetString() != "AnimatorController" || !string.Equals(resolved.GetProperty("source").GetString(), controllerSource, StringComparison.OrdinalIgnoreCase) || resolved.GetProperty("path_id").GetInt64() != controllerPathId) throw new InvalidDataException("Dynamic action requires the exact prefab Animator controller or a verified character controller selection receipt.");
        return Convert.ToHexString(SHA256.HashData(bytes)).ToLowerInvariant();
    }
}


