using System.Globalization;
using System.Security.Cryptography;
using System.Text.Json;
using AnimeStudio;

/// <summary>Authenticates one enemy controller clip and its selected Avatar before shared decoding.</summary>
internal static class EnemyAnimationSourceVerifier
{
    private sealed record PointerIdentity(string Cab, long PathId);

    internal static void Verify(AnimationRequest request, JsonElement audit, JsonSerializerOptions options)
    {
        if (audit.GetProperty("format").GetString() != "EndfieldEnemyAnimationSource/1" || audit.GetProperty("runtime_animator_reconstructed").GetBoolean())
            throw new InvalidDataException("Enemy animation requires exact source controller evidence and no reconstructed runtime Animator.");
        if (audit.GetProperty("character_id").GetString() != request.CharacterId || request.ControllerSelectionReceipt is not null)
            throw new InvalidDataException("Enemy animation request identity or controller-selection receipt is invalid.");

        using var selection = NpcAnimationReader.ReadEvidence(audit, "selection_source");
        using var staticAudit = NpcAnimationReader.ReadEvidence(audit, "static_source_audit");
        using var controllerSource = NpcAnimationReader.ReadEvidence(audit, "controller_source");
        using var avatarSource = NpcAnimationReader.ReadEvidence(audit, "avatar_source");
        using var clipSource = NpcAnimationReader.ReadEvidence(audit, "clip_source");
        JsonElement source = selection.RootElement;
        if (source.GetProperty("format").GetString() != "EndfieldEnemyAnimationSource/1" ||
            source.GetProperty("npc_id").GetString() != request.CharacterId ||
            source.GetProperty("source_identity").GetString() != audit.GetProperty("catalog_source_identity").GetString())
            throw new InvalidDataException("Enemy selection evidence does not match the requested character or catalog identity.");

        JsonElement actions = source.GetProperty("actions");
        if (actions.ValueKind != JsonValueKind.Array || actions.GetArrayLength() != 1)
            throw new InvalidDataException("Enemy animation audit must select exactly one controller action.");
        JsonElement action = actions[0];
        string clipKind = action.GetProperty("controller_clip_kind").GetString() ?? throw new InvalidDataException("Enemy action clip kind is null.");
        if (clipKind is not ("base" or "override") || audit.GetProperty("controller_clip_kind").GetString() != clipKind)
            throw new InvalidDataException("Enemy controller clip kind differs between the selected action and reader audit.");
        int clipIndex = action.GetProperty("controller_clip_index").GetInt32();
        if (clipIndex < 0 || audit.GetProperty("controller_clip_index").GetInt32() != clipIndex ||
            !request.ResourcePath.Equals(action.GetProperty("resource_path").GetString(), StringComparison.OrdinalIgnoreCase))
            throw new InvalidDataException("Enemy controller clip index or resource differs from the selected action.");
        VerifyOriginalClipIdentity(audit.GetProperty("original_clip"), action.GetProperty("original_clip"));

        JsonElement sourceController = source.GetProperty("controller");
        string sourceControllerType = RequiredString(sourceController, "type", "selection controller type");
        if (sourceControllerType is not ("AnimatorController" or "AnimatorOverrideController"))
            throw new InvalidDataException($"Unsupported enemy selection controller type: {sourceControllerType}.");
        bool hasBaseController = source.TryGetProperty("base_controller", out JsonElement baseController) && baseController.ValueKind != JsonValueKind.Null;
        if ((sourceControllerType == "AnimatorOverrideController") != hasBaseController)
            throw new InvalidDataException("Enemy override controller base source is absent or unexpected.");
        if (clipKind == "override" && sourceControllerType != "AnimatorOverrideController")
            throw new InvalidDataException("An override action requires an AnimatorOverrideController source.");

        JsonElement selectedController = sourceControllerType == "AnimatorOverrideController" && clipKind == "base" ? baseController : sourceController;
        string selectedControllerType = clipKind == "base" ? "AnimatorController" : sourceControllerType;
        if (RequiredString(selectedController, "type", "selected clip owner type") != selectedControllerType)
            throw new InvalidDataException("Enemy selected clip owner type disagrees with its controller clip kind.");
        VerifySourceJson(controllerSource.RootElement, audit.GetProperty("controller_source").GetProperty("sha256").GetString()!,
            selectedController, selectedControllerType, "controller_source");
        if (hasBaseController)
        {
            using var baseSource = NpcAnimationReader.ReadEvidence(audit, "base_controller_source");
            VerifySourceJson(baseSource.RootElement, audit.GetProperty("base_controller_source").GetProperty("sha256").GetString()!,
                baseController, "AnimatorController", "base_controller_source");
        }
        else if (audit.TryGetProperty("base_controller_source", out _))
        {
            throw new InvalidDataException("Enemy reader audit contains base-controller evidence for a non-override source.");
        }

        JsonElement selectedAvatar = source.GetProperty("selected_avatar");
        long avatarPathId = ParsePathId(selectedAvatar.GetProperty("path_id"), "selected Avatar path_id");
        string avatarCab = RequiredString(selectedAvatar, "cab", "selected Avatar CAB");
        if (avatarPathId != request.AvatarPathId || !avatarCab.Equals(request.AvatarCab, StringComparison.OrdinalIgnoreCase))
            throw new InvalidDataException("Enemy request Avatar differs from the authenticated selected Avatar.");
        VerifySourceJson(avatarSource.RootElement, audit.GetProperty("avatar_source").GetProperty("sha256").GetString()!,
            selectedAvatar, "Avatar", "avatar_source");
        VerifySelectedStaticAvatar(staticAudit.RootElement, source, selectedAvatar);
        VerifyClipSource(clipSource.RootElement, audit.GetProperty("clip_source").GetProperty("sha256").GetString()!, action, request.ResourcePath);

        string controllerBundle = RequiredString(audit, "controller_bundle", "controller bundle");
        if (!File.Exists(controllerBundle)) throw new FileNotFoundException("Exact enemy controller bundle is missing.", controllerBundle);
        var sourceBundles = new List<string> { request.AvatarBundle, request.ClipBundle, controllerBundle };
        string? baseControllerBundle = null;
        if (hasBaseController)
        {
            baseControllerBundle = RequiredString(audit, "base_controller_bundle", "base controller bundle");
            if (!File.Exists(baseControllerBundle)) throw new FileNotFoundException("Exact enemy base-controller bundle is missing.", baseControllerBundle);
            sourceBundles.Add(baseControllerBundle);
        }

        var logger = new DiagnosticLogger();
        Logger.Default = logger;
        Logger.Flags = LoggerEvent.Warning | LoggerEvent.Error;
        TypeFlags.SetTypes(new[]
        {
            ClassIDType.AssetBundle, ClassIDType.AnimatorController, ClassIDType.AnimatorOverrideController,
            ClassIDType.Avatar, ClassIDType.AnimationClip
        }.ToDictionary(type => type, type => (true, false)));
        var manager = new AssetsManager
        {
            Game = GameManager.GetGameByType(GameType.ArknightsEndfield), ResolveDependencies = false,
            SpecifyUnityVersion = request.UnityVersion, Silent = true, SkipProcess = false
        };
        try
        {
            manager.LoadPreparedFiles(sourceBundles.Select(Path.GetFullPath).Distinct(StringComparer.OrdinalIgnoreCase).ToArray());
            logger.RequireSuccess();
            Avatar avatar = FindObject<Avatar>(manager, avatarCab, avatarPathId, "selected enemy Avatar");
            NpcAnimationReader.VerifyObject(avatar, avatarSource.RootElement.GetProperty("$animestudio"));
            var avatarFile = avatar.assetsFile;
            Avatar parsedAvatar = new Avatar(new ObjectReader(avatarFile.reader, avatarFile,
                avatarFile.m_Objects.Single(value => value.m_PathID == avatar.m_PathID), manager.Game));
            if (parsedAvatar.reader.Position - parsedAvatar.reader.byteStart != parsedAvatar.byteSize)
                throw new InvalidDataException("Enemy Avatar parser did not consume the complete source object.");
            NpcAnimationReader.VerifyHierarchy(parsedAvatar, audit, request.AnimatorRootPath);
            File.WriteAllText(Path.Combine(request.Output, "avatar_parsed.json"),
                Newtonsoft.Json.JsonConvert.SerializeObject(parsedAvatar, Newtonsoft.Json.Formatting.Indented));
            AnimationClip clip = AnimationClipSelector.Select(request, manager.assetsFileList);
            AnimeStudio.Object selectedOwner = VerifyControllerClip(manager, audit, selectedController, sourceControllerType,
                clipKind, clipIndex, clip, hasBaseController, baseController, baseControllerBundle);
            File.WriteAllText(Path.Combine(request.Output, "enemy_source_verification.json"), JsonSerializer.Serialize(new
            {
                status = "exact_enemy_controller_clip_and_avatar_verified",
                controller_type = selectedOwner.type.ToString(), controller_source = selectedOwner.assetsFile.fileName,
                controller_path_id = selectedOwner.m_PathID, controller_clip_kind = clipKind, controller_clip_index = clipIndex,
                original_clip = new { cab = action.GetProperty("original_clip").GetProperty("cab").GetString(),
                    path_id = ParsePathId(action.GetProperty("original_clip").GetProperty("path_id"), "action original clip path_id") },
                avatar_cab = avatar.assetsFile.fileName, avatar_path_id = avatar.m_PathID,
                clip_cab = clip.assetsFile.fileName, clip_path_id = clip.m_PathID, runtime_animator_reconstructed = false
            }, options));
        }
        finally
        {
            manager.Clear();
        }
    }

    private static AnimeStudio.Object VerifyControllerClip(AssetsManager manager, JsonElement audit,
        JsonElement selectedController, string sourceControllerType, string clipKind, int clipIndex,
        AnimationClip clip, bool hasBaseController, JsonElement baseController, string? baseControllerBundle)
    {
        string controllerCab = RequiredString(selectedController, "cab", "selected controller CAB");
        long controllerPathId = ParsePathId(selectedController.GetProperty("path_id"), "selected controller path_id");
        ClassIDType expectedControllerType = clipKind == "base" ? ClassIDType.AnimatorController : ClassIDType.AnimatorOverrideController;
        AnimeStudio.Object[] controllers = manager.assetsFileList.SelectMany(file => file.Objects)
            .Where(value => value.type == expectedControllerType && value.m_PathID == controllerPathId && value.assetsFile.fileName.Equals(controllerCab, StringComparison.OrdinalIgnoreCase))
            .ToArray();
        if (controllers.Length != 1) throw new InvalidDataException($"Expected one exact enemy controller object: cab={controllerCab}, path_id={controllerPathId}, matches={controllers.Length}.");
        AnimeStudio.Object owner = controllers[0];
        string actualControllerType = owner.type switch
        {
            ClassIDType.AnimatorController => "AnimatorController",
            ClassIDType.AnimatorOverrideController => "AnimatorOverrideController",
            _ => throw new InvalidDataException($"Unsupported decoded enemy controller type: {owner.type}.")
        };
        if (actualControllerType != RequiredString(selectedController, "type", "selected controller type") ||
            actualControllerType != (clipKind == "base" ? "AnimatorController" : sourceControllerType))
            throw new InvalidDataException("Decoded enemy controller type differs from the authenticated action owner.");
        using var controllerSource = NpcAnimationReader.ReadEvidence(audit, "controller_source");
        NpcAnimationReader.VerifyObject(owner, controllerSource.RootElement.GetProperty("$animestudio"));
        using JsonDocument controllerTree = ReadControllerTypeTree(owner);
        JsonElement controllerFields = controllerTree.RootElement;

        JsonElement original = audit.GetProperty("original_clip");
        string originalCab = RequiredString(original, "cab", "original clip CAB");
        long originalPathId = ParsePathId(original.GetProperty("path_id"), "original clip path_id");
        if (clipKind == "base")
        {
            JsonElement baseClips = controllerFields.GetProperty("m_AnimationClips");
            if (baseClips.ValueKind != JsonValueKind.Array || clipIndex >= baseClips.GetArrayLength())
                throw new InvalidDataException($"Base controller clip index is outside m_AnimationClips: index={clipIndex}, count={baseClips.GetArrayLength()}.");
            JsonElement pointer = baseClips[clipIndex];
            VerifySerializedPointer(owner, pointer, originalCab, originalPathId, "base m_AnimationClips entry");
            VerifySerializedPointer(owner, pointer, clip.assetsFile.fileName, clip.m_PathID, "base effective clip");
            return owner;
        }

        if (!hasBaseController || baseControllerBundle is null) throw new InvalidDataException("Override enemy action lacks its exact base controller bundle.");
        string baseCab = RequiredString(baseController, "cab", "base controller CAB");
        long basePathId = ParsePathId(baseController.GetProperty("path_id"), "base controller path_id");
        AnimeStudio.Object[] baseOwners = manager.assetsFileList.SelectMany(file => file.Objects)
            .Where(value => value.type == ClassIDType.AnimatorController && value.m_PathID == basePathId && value.assetsFile.fileName.Equals(baseCab, StringComparison.OrdinalIgnoreCase))
            .ToArray();
        if (baseOwners.Length != 1) throw new InvalidDataException($"Expected one exact enemy base controller: cab={baseCab}, path_id={basePathId}, matches={baseOwners.Length}.");
        AnimeStudio.Object baseOwnerForOverride = baseOwners[0];
        using (JsonDocument baseSource = NpcAnimationReader.ReadEvidence(audit, "base_controller_source"))
            NpcAnimationReader.VerifyObject(baseOwnerForOverride, baseSource.RootElement.GetProperty("$animestudio"));
        VerifySerializedPointer(owner, controllerFields.GetProperty("m_Controller"), baseCab, basePathId, "override m_Controller");
        JsonElement overrides = controllerFields.GetProperty("m_Clips");
        if (overrides.ValueKind != JsonValueKind.Array || clipIndex >= overrides.GetArrayLength())
            throw new InvalidDataException($"Override controller clip index is outside m_Clips: index={clipIndex}, count={overrides.GetArrayLength()}.");
        JsonElement row = overrides[clipIndex];
        JsonElement originalPointer = row.GetProperty("m_OriginalClip");
        VerifySerializedPointer(owner, originalPointer, originalCab, originalPathId, "override m_OriginalClip");
        using JsonDocument baseTree = ReadControllerTypeTree(baseOwnerForOverride);
        JsonElement baseClipsForOverride = baseTree.RootElement.GetProperty("m_AnimationClips");
        int originalMatches = baseClipsForOverride.EnumerateArray().Count(pointer => PointerMatches(baseOwnerForOverride, pointer, originalCab, originalPathId));
        if (originalMatches < 1) throw new InvalidDataException($"Override original clip is absent from the exact base controller: cab={originalCab}, path_id={originalPathId}.");
        JsonElement overridePointer = row.GetProperty("m_OverrideClip");
        long overridePathId = overridePointer.GetProperty("m_PathID").GetInt64();
        int overrideFileId = overridePointer.GetProperty("m_FileID").GetInt32();
        if (overridePathId == 0)
        {
            if (overrideFileId != 0) throw new InvalidDataException("Null override clip has a nonzero file ID.");
            VerifySerializedPointer(owner, originalPointer, clip.assetsFile.fileName, clip.m_PathID, "inherited override clip");
        }
        else
        {
            VerifySerializedPointer(owner, overridePointer, clip.assetsFile.fileName, clip.m_PathID, "override m_OverrideClip");
        }
        return owner;
    }

    private static JsonDocument ReadControllerTypeTree(AnimeStudio.Object controller)
    {
        object tree = controller.ToType() ?? throw new InvalidDataException($"Enemy controller type tree is absent: {controller.assetsFile.fileName}/{controller.m_PathID}.");
        return JsonDocument.Parse(Newtonsoft.Json.JsonConvert.SerializeObject(tree, Newtonsoft.Json.Formatting.None));
    }

    private static void VerifySelectedStaticAvatar(JsonElement staticAudit, JsonElement selection, JsonElement avatar)
    {
        if (staticAudit.GetProperty("format").GetString() != "EndfieldCharacterSourceAudit/1")
            throw new InvalidDataException("Enemy static source audit format is unsupported.");
        JsonElement prefab = selection.GetProperty("prefab_source");
        JsonElement requestedRoot = staticAudit.GetProperty("requested_root");
        JsonElement actualRoot = staticAudit.GetProperty("actual_root");
        string resourcePath = RequiredString(selection, "model_resource_path", "enemy model resource path");
        if (!resourcePath.Equals(RequiredString(requestedRoot, "resource_path", "static requested root"), StringComparison.OrdinalIgnoreCase) ||
            !RequiredString(prefab, "cab", "prefab CAB").Equals(RequiredString(actualRoot, "source", "static actual root CAB"), StringComparison.OrdinalIgnoreCase) ||
            ParsePathId(prefab.GetProperty("root_path_id"), "prefab root path_id") != actualRoot.GetProperty("path_id").GetInt64())
            throw new InvalidDataException("Enemy static source root differs from the selected prefab identity.");
        long animatorPathId = ParsePathId(prefab.GetProperty("animator_path_id"), "prefab Animator path_id");
        JsonElement[] animators = staticAudit.GetProperty("animators").EnumerateArray()
            .Where(row => RequiredString(row.GetProperty("object"), "source", "static Animator CAB").Equals(RequiredString(prefab, "cab", "prefab CAB"), StringComparison.OrdinalIgnoreCase) &&
                row.GetProperty("object").GetProperty("path_id").GetInt64() == animatorPathId)
            .ToArray();
        if (animators.Length != 1) throw new InvalidDataException($"Expected one exact static enemy Animator source: path_id={animatorPathId}, matches={animators.Length}.");
        JsonElement avatarReference = animators[0].GetProperty("avatar").GetProperty("resolved");
        if (avatarReference.ValueKind != JsonValueKind.Object || avatarReference.GetProperty("type").GetString() != "Avatar" ||
            !RequiredString(avatarReference, "source", "static Animator Avatar CAB").Equals(RequiredString(avatar, "cab", "selected Avatar CAB"), StringComparison.OrdinalIgnoreCase) ||
            avatarReference.GetProperty("path_id").GetInt64() != ParsePathId(avatar.GetProperty("path_id"), "selected Avatar path_id"))
            throw new InvalidDataException("Static enemy Animator Avatar differs from the selected Avatar source.");
    }

    private static void VerifyClipSource(JsonElement clipSource, string evidenceHash, JsonElement action, string resourcePath)
    {
        JsonElement metadata = clipSource.GetProperty("$animestudio");
        if (metadata.GetProperty("type").GetString() != "AnimationClip" ||
            !RequiredString(metadata, "sourceFile", "clip source CAB").Equals(RequiredString(action, "clip_cab", "action clip CAB"), StringComparison.OrdinalIgnoreCase) ||
            metadata.GetProperty("pathId").GetInt64() != ParsePathId(action.GetProperty("clip_path_id"), "action clip path_id") ||
            !RequiredString(metadata, "container", "clip container").Equals(resourcePath, StringComparison.OrdinalIgnoreCase) ||
            !RequiredString(metadata, "rawDataSha256", "clip raw data hash").Equals(RequiredString(action, "clip_raw_data_sha256", "action clip raw data hash"), StringComparison.OrdinalIgnoreCase))
            throw new InvalidDataException("Enemy clip source metadata differs from the selected action.");
        if (!RequiredString(action, "source_json_sha256", "action clip source JSON hash").Equals(evidenceHash, StringComparison.OrdinalIgnoreCase))
            throw new InvalidDataException("Enemy clip evidence hash differs from the selected action record.");
    }

    private static void VerifySourceJson(JsonElement evidence, string evidenceHash, JsonElement selectedSource, string expectedType, string label)
    {
        JsonElement metadata = evidence.GetProperty("$animestudio");
        if (metadata.GetProperty("type").GetString() != expectedType ||
            !RequiredString(metadata, "sourceFile", label + " CAB").Equals(RequiredString(selectedSource, "cab", label + " selected CAB"), StringComparison.OrdinalIgnoreCase) ||
            metadata.GetProperty("pathId").GetInt64() != ParsePathId(selectedSource.GetProperty("path_id"), label + " selected path_id") ||
            !RequiredString(metadata, "rawDataSha256", label + " raw data hash").Equals(RequiredString(selectedSource, "raw_data_sha256", label + " selected raw data hash"), StringComparison.OrdinalIgnoreCase))
            throw new InvalidDataException($"{label} source JSON differs from the authenticated enemy selection.");
        string expectedJsonHash = RequiredString(selectedSource, "source_json_sha256", label + " source JSON hash");
        if (!expectedJsonHash.Equals(evidenceHash, StringComparison.OrdinalIgnoreCase))
            throw new InvalidDataException($"{label} evidence hash differs from the selected source record.");
    }

    private static void VerifyOriginalClipIdentity(JsonElement auditOriginal, JsonElement actionOriginal)
    {
        if (!RequiredString(auditOriginal, "cab", "audit original clip CAB").Equals(RequiredString(actionOriginal, "cab", "action original clip CAB"), StringComparison.OrdinalIgnoreCase) ||
            ParsePathId(auditOriginal.GetProperty("path_id"), "audit original clip path_id") != ParsePathId(actionOriginal.GetProperty("path_id"), "action original clip path_id"))
            throw new InvalidDataException("Enemy original clip pointer differs between the reader audit and selected action.");
    }

    private static T FindObject<T>(AssetsManager manager, string cab, long pathId, string label) where T : AnimeStudio.Object
    {
        T[] matches = manager.assetsFileList.SelectMany(file => file.Objects.OfType<T>())
            .Where(value => value.m_PathID == pathId && value.assetsFile.fileName.Equals(cab, StringComparison.OrdinalIgnoreCase))
            .ToArray();
        if (matches.Length != 1) throw new InvalidDataException($"Expected one exact {label}: cab={cab}, path_id={pathId}, matches={matches.Length}.");
        return matches[0];
    }

    private static void VerifyResolvedPointer<T>(AnimeStudio.Object owner, PPtr<T> pointer, string expectedCab, long expectedPathId, string label) where T : AnimeStudio.Object
    {
        VerifyPointerIdentity(owner, pointer, expectedCab, expectedPathId, label);
        if (!pointer.TryGet(out T target) || target.m_PathID != expectedPathId || !target.assetsFile.fileName.Equals(expectedCab, StringComparison.OrdinalIgnoreCase))
            throw new InvalidDataException($"{label} does not resolve to its exact loaded source object: cab={expectedCab}, path_id={expectedPathId}.");
    }

    private static void VerifyPointerIdentity<T>(AnimeStudio.Object owner, PPtr<T> pointer, string expectedCab, long expectedPathId, string label) where T : AnimeStudio.Object
    {
        PointerIdentity identity = PointerTarget(owner, pointer.m_FileID, pointer.m_PathID, label);
        if (identity.PathId != expectedPathId || !identity.Cab.Equals(expectedCab, StringComparison.OrdinalIgnoreCase))
            throw new InvalidDataException($"{label} target differs from its authenticated source: expected={expectedCab}/{expectedPathId}, actual={identity.Cab}/{identity.PathId}.");
    }

    private static bool PointerMatches<T>(AnimeStudio.Object owner, PPtr<T> pointer, string expectedCab, long expectedPathId) where T : AnimeStudio.Object
    {
        PointerIdentity identity = PointerTarget(owner, pointer.m_FileID, pointer.m_PathID, "base m_AnimationClips entry");
        return identity.PathId == expectedPathId && identity.Cab.Equals(expectedCab, StringComparison.OrdinalIgnoreCase);
    }

    private static bool PointerMatches(AnimeStudio.Object owner, JsonElement pointer, string expectedCab, long expectedPathId)
    {
        PointerIdentity identity = PointerTarget(owner, pointer.GetProperty("m_FileID").GetInt32(),
            pointer.GetProperty("m_PathID").GetInt64(), "base m_AnimationClips entry");
        return identity.PathId == expectedPathId && identity.Cab.Equals(expectedCab, StringComparison.OrdinalIgnoreCase);
    }

    private static void VerifySerializedPointer(AnimeStudio.Object owner, JsonElement pointer, string expectedCab, long expectedPathId, string label)
    {
        PointerIdentity identity = PointerTarget(owner, pointer.GetProperty("m_FileID").GetInt32(),
            pointer.GetProperty("m_PathID").GetInt64(), label);
        if (identity.PathId != expectedPathId || !identity.Cab.Equals(expectedCab, StringComparison.OrdinalIgnoreCase))
            throw new InvalidDataException($"{label} target differs from its authenticated source: expected={expectedCab}/{expectedPathId}, actual={identity.Cab}/{identity.PathId}.");
    }

    private static PointerIdentity PointerTarget(AnimeStudio.Object owner, int fileId, long pathId, string label)
    {
        if (pathId == 0 || fileId < 0 || fileId > owner.assetsFile.m_Externals.Count)
            throw new InvalidDataException($"{label} has an invalid PPtr: file_id={fileId}, path_id={pathId}.");
        string cab = fileId == 0 ? owner.assetsFile.fileName : owner.assetsFile.m_Externals[fileId - 1].fileName;
        return new PointerIdentity(cab, pathId);
    }

    private static string RequiredString(JsonElement value, string name, string label)
    {
        string? text = value.GetProperty(name).GetString();
        return !string.IsNullOrWhiteSpace(text) ? text : throw new InvalidDataException($"{label} is null or empty.");
    }

    private static long ParsePathId(JsonElement value, string label)
    {
        string text = value.ValueKind switch
        {
            JsonValueKind.String => value.GetString() ?? throw new InvalidDataException($"{label} is null."),
            JsonValueKind.Number => value.GetInt64().ToString(CultureInfo.InvariantCulture),
            _ => throw new InvalidDataException($"{label} has an invalid JSON type.")
        };
        if (!long.TryParse(text, NumberStyles.Integer, CultureInfo.InvariantCulture, out long pathId) || pathId == 0)
            throw new InvalidDataException($"{label} is not a nonzero signed integer: value={text}.");
        return pathId;
    }
}
