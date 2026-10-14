using System.Security.Cryptography;
using System.Text;
using System.Text.Json;
using AnimeStudio;

internal sealed record AnimationRequest(string Format, string CharacterId, string AvatarBundle, string AvatarCab, long AvatarPathId, string ClipBundle, string ResourcePath, string SourceAudit, string AnimatorRootPath, string? ControllerBundle, string UnityVersion, int MaximumStoredFrames, string Output);

/// <summary>Validates configuration NPC source identities before reusing the established samplers.</summary>
internal static class NpcAnimationReader
{
    private static readonly JsonSerializerOptions Options = new() { PropertyNamingPolicy = JsonNamingPolicy.SnakeCaseLower, WriteIndented = true };

    private static int Main(string[] args)
    {
        try
        {
            if (args.Length != 1) throw new ArgumentException("Expected one animation request JSON path.");
            var request = JsonSerializer.Deserialize<AnimationRequest>(File.ReadAllText(args[0]), Options) ?? throw new InvalidDataException("Null animation request.");
            if (request.Format != "EndfieldAnimationExportRequest/1" || request.MaximumStoredFrames < 2 || request.ControllerBundle != null)
                throw new InvalidDataException("NPC CPU source reader requires an explicit frame budget and no inferred controller.");
            using var audit = JsonDocument.Parse(File.ReadAllBytes(request.SourceAudit));
            VerifySources(request, audit.RootElement);
            Program.Run(request);
            ScalarBinding[] scalarBindings = JsonSerializer.Deserialize<ScalarBinding[]>(File.ReadAllText(Path.Combine(request.Output, "native", "scalar_bindings.json")), Options) ?? throw new InvalidDataException("NPC scalar bindings are null.");
            using var rootHeader = JsonDocument.Parse(File.ReadAllText(Path.Combine(request.Output, "native", "root_motion_header.json")));
            if (NpcQvvfProgram.SupportsLayout(scalarBindings, rootHeader.RootElement.GetProperty("tracks").GetInt32()))
            {
                if (audit.RootElement.TryGetProperty("avatar_binding_status", out var bindingMode) && bindingMode.GetString() == "source_humanoid_to_selected_avatar")
                    throw new InvalidDataException("Cross-Avatar export requires actual Humanoid muscle channels; qvvf-only layout is unsupported.");
                NpcQvvfProgram.Export(request, Options);
            }
            else HumanoidProgram.Run(request);
            return 0;
        }
        catch (Exception error)
        {
            Console.Error.WriteLine(JsonSerializer.Serialize(new { status = "failed", error_type = error.GetType().FullName, message = error.Message, stack = error.ToString() }, Options));
            return 1;
        }
    }

    private static JsonDocument ReadEvidence(JsonElement audit, string field)
    {
        var row = audit.GetProperty(field);
        string path = row.GetProperty("path").GetString() ?? throw new InvalidDataException($"Null evidence path: {field}.");
        byte[] payload = File.ReadAllBytes(path);
        if (!Convert.ToHexString(SHA256.HashData(payload)).Equals(row.GetProperty("sha256").GetString(), StringComparison.OrdinalIgnoreCase))
            throw new InvalidDataException($"Source evidence changed: {field}.");
        return JsonDocument.Parse(payload);
    }

    private static void VerifySources(AnimationRequest request, JsonElement audit)
    {
        if (audit.GetProperty("format").GetString() != "EndfieldNpcAnimationSource/1" || audit.GetProperty("runtime_animator_reconstructed").GetBoolean())
            throw new InvalidDataException("NPC action requires explicit configuration source evidence, not a fabricated Animator.");
        using var input = ReadEvidence(audit, "assembly_input");
        using var cpu = ReadEvidence(audit, "cpu_source");
        using var avatarJson = ReadEvidence(audit, "avatar_source");
        using var clipJson = ReadEvidence(audit, "clip_source");
        bool retarget = audit.TryGetProperty("avatar_binding_status", out var mode) && mode.GetString() == "source_humanoid_to_selected_avatar";
        using var cpuAvatarJson = retarget ? ReadEvidence(audit, "cpu_avatar_source") : null;
        var selected = input.RootElement.GetProperty("selection");
        var avatarSpec = input.RootElement.GetProperty("avatar");
        if (input.RootElement.GetProperty("format").GetString() != "EndfieldNpcAssemblyInput/1" || selected.GetProperty("character_id").GetString() != request.CharacterId ||
            !request.AvatarCab.Equals(avatarSpec.GetProperty("cab").GetString(), StringComparison.OrdinalIgnoreCase) || request.AvatarPathId != long.Parse(avatarSpec.GetProperty("path_id").GetString()!))
            throw new InvalidDataException("Requested NPC or Avatar differs from authenticated assembly input.");
        string template = audit.GetProperty("cpu_animation_template").GetString() ?? throw new InvalidDataException("Null CPU template.");
        var crc = new SevenZip.CRC();
        byte[] tagBytes = Encoding.UTF8.GetBytes(template);
        crc.Update(tagBytes, 0, (uint)tagBytes.Length);
        if (unchecked((uint)cpu.RootElement.GetProperty("configTag").GetProperty("tagId").GetInt32()) != crc.GetDigest())
            throw new InvalidDataException("CPU GameplayTag disagrees with exact PrefabInfo template.");
        if (retarget)
        {
            var meta = cpuAvatarJson!.RootElement.GetProperty("$animestudio");
            VerifyPointer(cpu.RootElement, "$.avatar", cpu.RootElement.GetProperty("avatar"), meta.GetProperty("sourceFile").GetString()!, meta.GetProperty("pathId").GetInt64());
            int[] sourceSlots = cpuAvatarJson.RootElement.GetProperty("m_Avatar").GetProperty("m_Human").GetProperty("m_HumanBoneIndex").EnumerateArray().Select(value => value.GetInt32()).ToArray();
            int[] targetSlots = avatarJson.RootElement.GetProperty("m_Avatar").GetProperty("m_Human").GetProperty("m_HumanBoneIndex").EnumerateArray().Select(value => value.GetInt32()).ToArray();
            if (sourceSlots.Length != 25 || targetSlots.Length != 25 || sourceSlots[0] < 0 || targetSlots[0] < 0 || sourceSlots.Where((index, slot) => index >= 0 && targetSlots[slot] < 0).Any())
                throw new InvalidDataException("Target Human Avatar lacks required CPU Human slots.");
        }
        else VerifyPointer(cpu.RootElement, "$.avatar", cpu.RootElement.GetProperty("avatar"), request.AvatarCab, request.AvatarPathId);
        string stateName = audit.GetProperty("state_name").GetString()!;
        var states = cpu.RootElement.GetProperty("allBaseClips").EnumerateArray().Select((row, index) => (Row: row, Index: index)).Where(item => item.Row.GetProperty("stateName").GetString() == stateName).ToArray();
        if (states.Length != 1) throw new InvalidDataException("Selected CPU base state is absent or duplicated.");
        var clipMeta = clipJson.RootElement.GetProperty("$animestudio");
        string clipCab = clipMeta.GetProperty("sourceFile").GetString()!;
        long clipId = clipMeta.GetProperty("pathId").GetInt64();
        VerifyPointer(cpu.RootElement, $"$.allBaseClips[{states[0].Index}].clip", states[0].Row.GetProperty("clip"), clipCab, clipId);
        if (!request.ResourcePath.Equals(clipMeta.GetProperty("container").GetString(), StringComparison.OrdinalIgnoreCase))
            throw new InvalidDataException("Requested clip container differs from the exact CPU pointer target.");
        var logger = new DiagnosticLogger();
        Logger.Default = logger;
        Logger.Flags = LoggerEvent.Warning | LoggerEvent.Error;
        TypeFlags.SetTypes(new[] { ClassIDType.AssetBundle, ClassIDType.MonoBehaviour, ClassIDType.Avatar, ClassIDType.AnimationClip, ClassIDType.GameObject, ClassIDType.Transform }.ToDictionary(type => type, type => (true, false)));
        var manager = new AssetsManager { Game = GameManager.GetGameByType(GameType.ArknightsEndfield), ResolveDependencies = false, SpecifyUnityVersion = request.UnityVersion, Silent = true, SkipProcess = false };
        try
        {
            var sourceBundles = new List<string> { request.AvatarBundle, request.ClipBundle, audit.GetProperty("cpu_bundle").GetString()! };
            if (retarget) sourceBundles.Add(audit.GetProperty("cpu_avatar_bundle").GetString()!);
            manager.LoadPreparedFiles(sourceBundles.Distinct().ToArray());
            logger.RequireSuccess();
            File.WriteAllText(Path.Combine(request.Output, "npc_hierarchy_inventory.json"), JsonSerializer.Serialize(manager.assetsFileList.Select(file => new {
                source = file.fileName,
                parsed_objects = file.Objects.GroupBy(row => row.type.ToString()).Select(group => new { type = group.Key, count = group.Count() }),
                transforms = file.Objects.OfType<Transform>().Select(value => new { path_id = value.m_PathID, raw_data_sha256 = Hash(value),
                    game_object_file_id = value.m_GameObject.m_FileID, game_object_path_id = value.m_GameObject.m_PathID,
                    game_object_name = value.m_GameObject.TryGet(out var gameObject) ? gameObject.m_Name : null,
                    father_file_id = value.m_Father.m_FileID, father_path_id = value.m_Father.m_PathID,
                    position = new[] { value.m_LocalPosition.X, value.m_LocalPosition.Y, value.m_LocalPosition.Z },
                    rotation = new[] { value.m_LocalRotation.X, value.m_LocalRotation.Y, value.m_LocalRotation.Z, value.m_LocalRotation.W },
                    scale = new[] { value.m_LocalScale.X, value.m_LocalScale.Y, value.m_LocalScale.Z } })
            }), Options));
            Avatar avatar = manager.assetsFileList.SelectMany(file => file.Objects.OfType<Avatar>()).Single(value => value.m_PathID == request.AvatarPathId && value.assetsFile.fileName.Equals(request.AvatarCab, StringComparison.OrdinalIgnoreCase));
            VerifyObject(avatar, avatarJson.RootElement.GetProperty("$animestudio"));
            if (retarget)
            {
                var meta = cpuAvatarJson!.RootElement.GetProperty("$animestudio");
                Avatar cpuAvatar = manager.assetsFileList.SelectMany(file => file.Objects.OfType<Avatar>()).Single(value => value.m_PathID == meta.GetProperty("pathId").GetInt64() && value.assetsFile.fileName.Equals(meta.GetProperty("sourceFile").GetString(), StringComparison.OrdinalIgnoreCase));
                VerifyObject(cpuAvatar, meta);
            }
            if (!Hash(avatar).Equals(avatarSpec.GetProperty("raw_data_sha256").GetString(), StringComparison.OrdinalIgnoreCase))
                throw new InvalidDataException("Actual Avatar bytes differ from authenticated assembly source.");
            var cpuMeta = cpu.RootElement.GetProperty("$animestudio");
            var cpuObject = manager.assetsFileList.SelectMany(file => file.Objects.OfType<MonoBehaviour>()).Single(value => value.m_PathID == cpuMeta.GetProperty("pathId").GetInt64() && value.assetsFile.fileName.Equals(cpuMeta.GetProperty("sourceFile").GetString(), StringComparison.OrdinalIgnoreCase));
            VerifyObject(cpuObject, cpuMeta);
            var clip = manager.assetsFileList.SelectMany(file => file.Objects.OfType<AnimationClip>()).Single(value => value.m_PathID == clipId && value.assetsFile.fileName.Equals(clipCab, StringComparison.OrdinalIgnoreCase));
            VerifyObject(clip, clipMeta);
            var file = avatar.assetsFile;
            avatar = new Avatar(new ObjectReader(file.reader, file, file.m_Objects.Single(value => value.m_PathID == avatar.m_PathID), manager.Game));
            if (avatar.reader.Position - avatar.reader.byteStart != avatar.byteSize) throw new InvalidDataException("Avatar parser did not consume the complete source object.");
            VerifyHierarchy(avatar, audit, request.AnimatorRootPath);
            File.WriteAllText(Path.Combine(request.Output, "avatar_parsed.json"), Newtonsoft.Json.JsonConvert.SerializeObject(avatar, Newtonsoft.Json.Formatting.Indented));
            File.WriteAllText(Path.Combine(request.Output, "npc_source_verification.json"), JsonSerializer.Serialize(new { status = "exact_cpu_template_avatar_and_base_clip_verified", binding_mode = retarget ? "source_humanoid_to_selected_avatar" : "exact_avatar", cpu_template = template, state_name = stateName, avatar_cab = request.AvatarCab, avatar_path_id = request.AvatarPathId, cpu_avatar = retarget ? cpuAvatarJson!.RootElement.GetProperty("$animestudio") : avatarJson.RootElement.GetProperty("$animestudio"), clip_cab = clipCab, clip_path_id = clipId, runtime_animator_reconstructed = false, scope = retarget ? "Authenticated source CPU Avatar and base clip; target Human referential reconstructs the selected Avatar. No runtime controller state or complete Unity retargeting equivalence." : "Single source CPU base clip and exact Avatar; no runtime controller state or cross-Avatar retargeting." }, Options));
        }
        finally { manager.Clear(); }
    }

    private static string Hash(AnimeStudio.Object value) => Convert.ToHexString(SHA256.HashData(value.GetRawData()));

    private static void VerifyObject(AnimeStudio.Object value, JsonElement metadata)
    {
        if (metadata.GetProperty("byteSize").GetInt64() != value.byteSize || metadata.GetProperty("rawDataLength").GetInt64() != value.byteSize || !Hash(value).Equals(metadata.GetProperty("rawDataSha256").GetString(), StringComparison.OrdinalIgnoreCase))
            throw new InvalidDataException($"Source JSON does not authenticate actual object bytes: {value.assetsFile.fileName}/{value.m_PathID}.");
    }

    private static void VerifyPointer(JsonElement source, string path, JsonElement pointer, string cab, long pathId)
    {
        int fileId = pointer.GetProperty("m_FileID").GetInt32();
        if (fileId < 0 || pathId == 0 || pointer.GetProperty("m_PathID").GetInt64() != pathId) throw new InvalidDataException($"Invalid source pointer: {path}.");
        var metadata = source.GetProperty("$animestudio");
        var reference = metadata.GetProperty("pptrReferences").EnumerateArray().Single(row => row.GetProperty("path").GetString() == path && row.GetProperty("fileId").GetInt32() == fileId && row.GetProperty("pathId").GetInt64() == pathId);
        string actual = (fileId == 0 ? metadata.GetProperty("sourceFile") : reference.GetProperty("expectedTargetSourceFile")).GetString()!;
        if (!cab.Equals(actual, StringComparison.OrdinalIgnoreCase)) throw new InvalidDataException($"Source pointer target CAB disagrees: {path}.");
    }

    private static void VerifyHierarchy(Avatar avatar, JsonElement audit, string root)
    {
        var paths = avatar.m_Avatar.m_AvatarSkeleton.m_ID.Select(id => avatar.FindBonePath(id)).ToArray();
        var nodes = audit.GetProperty("transforms").EnumerateArray().ToArray();
        var poses = avatar.m_Avatar.m_DefaultPose.m_X;
        if (nodes.Length != paths.Length || poses.Length != paths.Length) throw new InvalidDataException("Source hierarchy differs from actual Avatar dimensions.");
        for (int index = 0; index < paths.Length; index++)
        {
            if (paths[index] == null || nodes[index].GetProperty("path").GetString() != root + (paths[index] == "" ? "" : "/" + paths[index])) throw new InvalidDataException($"NPC source hierarchy path differs from actual Avatar: {index}.");
            var pose = poses[index];
            var expected = new[] { new[] { pose.t.X, pose.t.Y, pose.t.Z }, new[] { pose.q.X, pose.q.Y, pose.q.Z, pose.q.W }, new[] { pose.s.X, pose.s.Y, pose.s.Z } };
            var fields = new[] { "position", "rotation", "scale" };
            for (int field = 0; field < fields.Length; field++)
                if (!nodes[index].GetProperty(fields[field]).EnumerateArray().Select(value => value.GetSingle()).SequenceEqual(expected[field])) throw new InvalidDataException($"NPC default local differs from actual Avatar: {index}/{fields[field]}.");
        }
    }
}
