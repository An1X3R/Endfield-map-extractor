using System.Globalization;
using System.Security.Cryptography;
using System.Text;
using System.Text.Json;
using System.Text.Json.Serialization;
using AnimeStudio;
using EndfieldCharacterModel;
using AssetObject = AnimeStudio.Object;

namespace NpcAssemblyProbe;

internal sealed record BundleSpec(string Path, long Bytes, string Sha256);
internal sealed record SourceIdentity(string Source, string PathId, string Type, string Name, string RawDataSha256);
internal sealed record ResolvedMaterial(Material? Value, string Status, string FbxName, string PathHash, bool ConfiguredDisabled, string? ResourcePath);
internal sealed record ResolvedRenderer(int SlotIndex, int LodIndex, int RendererIndex, string DescriptorName, string ExportName, string SlotName, Avatar RigAvatar, bool UsesSelfAvatar, string? ParentBoneTransformName, Mesh Mesh, string MeshProviderCab, string MeshProviderLogicalName, string MeshResourcePath, string MeshPathHash, string[] GuidWords, int RootBoneId, int ConfiguredRootBoneId, string RootBoneName, int ConfiguredIsActive, bool ConfiguredRendererDisabled, int ConfiguredRealtimeShadowCaster, int ConfiguredPlatformAvailability, ResolvedMaterial[] Materials);
internal sealed record SourceConfig(string SourceJsonPath, string SourceJsonSha256, string Cab, long PathId, string Name, string Container, string RawDataSha256, string TypeTreeSource, string ConfigurationRole);
internal sealed record PoseCandidateDelta(int BoneIndex, uint BoneNameHash, string AvatarPath, double TranslationResidual, double BasisResidual, float[] MatrixColumnMajor);
internal sealed record PoseConsensusAudit(string PoseSource, string ExportName, int WeightedBoneCount, double MaximumTranslationResidual, double MaximumBasisResidual, PoseCandidateDelta[] Candidates);

internal static class EntryPoint
{
    private static readonly JsonSerializerOptions JsonOptions = new()
    {
        PropertyNamingPolicy = JsonNamingPolicy.SnakeCaseLower,
        WriteIndented = true,
        RespectRequiredConstructorParameters = true,
        RespectNullableAnnotations = true
    };

    private static int Main(string[] args)
    {
        try
        {
            if (args.Length != 4 || args[0] != "--input" || args[2] != "--output")
                throw new ArgumentException("Expected --input assembly_input.json --output new_directory.");
            Run(Path.GetFullPath(args[1]), Path.GetFullPath(args[3]));
            return 0;
        }
        catch (Exception error)
        {
            Console.Error.WriteLine(JsonSerializer.Serialize(new
            {
                error_type = error.GetType().FullName,
                message = error.Message,
                stack = error.StackTrace
            }, JsonOptions));
            return 1;
        }
    }

    private static void Run(string inputPath, string outputPath)
    {
        if (Directory.Exists(outputPath) || File.Exists(outputPath))
            throw new IOException($"Output already exists: {outputPath}");

        using var inputDocument = JsonDocument.Parse(File.ReadAllBytes(inputPath));
        var input = inputDocument.RootElement;
        RequireString(input, "format", "input", "EndfieldNpcAssemblyInput/1");
        var unityVersion = RequireString(input, "unity_version", "input");
        var selection = RequireObject(input, "selection", "input");
        var bundles = ReadBundles(input, Path.GetDirectoryName(inputPath)!);
        var avatarSpec = RequireObject(input, "avatar", "input");
        var avatarCab = RequireString(avatarSpec, "cab", "avatar");
        var avatarPathId = ParseInt64(RequireString(avatarSpec, "path_id", "avatar"), "avatar.path_id");
        var avatarName = RequireString(avatarSpec, "name", "avatar");
        var avatarSha = RequireSha256(RequireString(avatarSpec, "raw_data_sha256", "avatar"), "avatar.raw_data_sha256");
        var avatarMeshConfig = ReadConfigSpec(RequireObject(input, "avatar_mesh_configuration", "input"), "avatar_mesh_configuration");
        var avatarTempletConfig = input.TryGetProperty("avatar_templet_configuration", out var templetElement) && templetElement.ValueKind == JsonValueKind.Object
            ? ReadConfigSpec(templetElement, "avatar_templet_configuration")
            : null;
        var placementMode = RequireString(input, "placement_mode", "input");
        if (placementMode != "bindpose_consensus")
            throw new InvalidDataException($"Unsupported placement mode: {placementMode}.");
        var tolerance = RequireObject(input, "placement_tolerance", "input");
        var translationTolerance = RequireFiniteDouble(tolerance, "translation_m", "placement_tolerance");
        var basisTolerance = RequireFiniteDouble(tolerance, "basis_max_abs", "placement_tolerance");
        if (translationTolerance <= 0 || basisTolerance <= 0)
            throw new InvalidDataException("Placement tolerances must be positive.");

        Directory.CreateDirectory(outputPath);
        Directory.CreateDirectory(Path.Combine(outputPath, "source_textures"));
        var logger = new ReaderLogger();
        Logger.Default = logger;
        Logger.Flags = LoggerEvent.Warning | LoggerEvent.Error;
        Logger.Silent = false;
        TypeFlags.SetTypes(new[]
        {
            ClassIDType.AssetBundle,
            ClassIDType.ResourceManager,
            ClassIDType.MonoBehaviour,
            ClassIDType.Avatar,
            ClassIDType.Mesh,
            ClassIDType.Material,
            ClassIDType.Texture2D
        }.ToDictionary(type => type, type => (true, false)));

        var manager = new AssetsManager
        {
            Game = GameManager.GetGameByType(GameType.ArknightsEndfield),
            ResolveDependencies = false,
            SpecifyUnityVersion = unityVersion,
            Silent = false,
            SkipProcess = false
        };
        try
        {
            manager.LoadPreparedFiles(bundles.Select(bundle => bundle.Path).ToArray());
            WriteJson(outputPath, "reader_logs.json", logger.Snapshot());
            logger.RequireNoErrors();

            var avatarFile = SourceFile(manager, avatarCab);
            var avatar = ObjectAt<Avatar>(avatarFile, avatarPathId, avatarName, "Avatar");
            VerifyObjectHash(avatar, avatarSha, "avatar");
            var config = ResolveConfig(manager, avatarMeshConfig);
            var templet = avatarTempletConfig == null ? null : ResolveConfig(manager, avatarTempletConfig);
            VerifyAvatarTempletPointer(templet, avatar);
            var rendererSpecs = RequireArray(input, "renderers", "input").EnumerateArray().ToArray();
            if (rendererSpecs.Length == 0)
                throw new InvalidDataException("AvatarMesh configuration has no requested LOD renderer rows.");
            var slotName = RequireString(selection, "avatar_mesh_slot", "selection");
            var slotNames = selection.TryGetProperty("part_names", out var partNamesElement)
                ? ReadStringArray(partNamesElement, "selection.part_names")
                : new[] { slotName };
            if (slotNames.Length == 0 || slotNames.Distinct(StringComparer.Ordinal).Count() != slotNames.Length)
                throw new InvalidDataException("NPC slot selection must contain unique exact source part names.");
            var lodIndex = RequireInt32(selection, "static_lod", "selection");
            var configTree = ReadConfigJson(avatarMeshConfig);
            var slotRows = RequireArray(configTree, "avatarSlotMeshDatas", avatarMeshConfig.Container).EnumerateArray().ToArray();
            var selectedSlots = slotNames.Select(name =>
            {
                var matches = slotRows.Select((value, index) => (Value: value, Index: index))
                    .Where(row => string.Equals(RequireString(row.Value, "name", "avatarSlotMeshDatas row"), name, StringComparison.Ordinal))
                    .ToArray();
                if (matches.Length != 1)
                    throw new InvalidDataException($"Expected exactly one AvatarMesh slot by exact name: name={name}, matches={matches.Length}.");
                return (Name: name, Value: matches[0].Value, Index: matches[0].Index);
            }).ToArray();
            var selectedSlotMetadata = input.TryGetProperty("avatar_mesh_slots", out var slotMetadataElement)
                ? ReadArray(slotMetadataElement, "avatar_mesh_slots")
                : Array.Empty<JsonElement>();
            if (selectedSlotMetadata.Length != 0 && selectedSlotMetadata.Length != selectedSlots.Length)
                throw new InvalidDataException("NPC selected-slot evidence must match the exact source part list.");
            var sourceLodRowsBySlot = selectedSlots.Select(slot => RequireArray(slot.Value, $"partSubMeshsLOD{lodIndex}",
                $"AvatarMesh slot {slot.Name}").EnumerateArray().ToArray()).ToArray();
            if (sourceLodRowsBySlot.Sum(rows => rows.Length) != rendererSpecs.Length)
                throw new InvalidDataException($"Renderer manifest does not cover every selected LOD exactly: slots={selectedSlots.Length}, lod={lodIndex}, config_rows={sourceLodRowsBySlot.Sum(rows => rows.Length)}, input_rows={rendererSpecs.Length}.");

            var resolvedRenderers = new List<ResolvedRenderer>(rendererSpecs.Length);
            var materialObjects = new Dictionary<(string Source, long PathId), Material>();
            var meshAudits = new List<MeshAudit>();
            var materialAudits = new Dictionary<(string Source, long PathId), MaterialAudit>();
            var materialTypeTrees = new Dictionary<(string Source, long PathId), TypeTreeAudit>();
            var textureObjects = new Dictionary<(string Source, long PathId), Texture2D>();

            var rendererOffset = 0;
            for (var selectedIndex = 0; selectedIndex < selectedSlots.Length; selectedIndex++)
            {
                var selectedSlot = selectedSlots[selectedIndex];
                var sourceRows = sourceLodRowsBySlot[selectedIndex];
                var sourceSlotIndex = selectedSlot.Index;
                var sourceUseSelfAvatar = RequireInt32(selectedSlot.Value, "bUseSelfAvatar", $"AvatarMesh slot {selectedSlot.Name}");
                if (sourceUseSelfAvatar is not 0 and not 1)
                    throw new InvalidDataException($"NPC slot self-avatar flag must be 0 or 1: slot={selectedSlot.Name}, value={sourceUseSelfAvatar}.");
                if (selectedSlotMetadata.Length != 0)
                    VerifySelectedSlotMetadata(selectedSlotMetadata[selectedIndex], selectedSlot.Name, sourceSlotIndex,
                        lodIndex, selectedSlot.Value);
                for (var rendererIndex = 0; rendererIndex < sourceRows.Length; rendererIndex++)
                {
                    var rendererSpec = rendererSpecs[rendererOffset++];
                    var sourceConfigRow = sourceRows[rendererIndex];
                    VerifyRendererSlotOrder(rendererSpec, selectedSlot.Name, sourceSlotIndex, lodIndex, rendererIndex);
                    var rigAvatar = ResolveRendererAvatar(manager, avatar, selectedSlot.Value, rendererSpec, sourceConfigRow);
                    var parentPath = sourceUseSelfAvatar == 1
                        ? RequireString(selectedSlot.Value, "parentBoneTransformName", $"AvatarMesh slot {selectedSlot.Name}")
                        : null;
                    var renderer = ResolveRenderer(manager, rigAvatar, rendererSpec, sourceConfigRow,
                        sourceSlotIndex, lodIndex, rendererIndex, sourceUseSelfAvatar == 1, parentPath);
                    resolvedRenderers.Add(renderer);
                    meshAudits.Add(Program.AuditMesh(renderer.Mesh));
                    foreach (var resolvedMaterial in renderer.Materials)
                    {
                        if (resolvedMaterial.Value == null)
                            continue;
                        var material = resolvedMaterial.Value;
                        var identity = (material.assetsFile.fileName, material.m_PathID);
                        materialObjects.TryAdd(identity, material);
                        materialAudits.TryAdd(identity, Program.AuditMaterial(material));
                        materialTypeTrees.TryAdd(identity, Program.AuditTypeTree(material));
                        foreach (var textureEnv in material.m_SavedProperties.m_TexEnvs)
                        {
                            if (textureEnv.Value.m_Texture.m_PathID == 0)
                                continue;
                            var texture = Program.RequireTexture(textureEnv.Value.m_Texture, textureEnv.Key);
                            textureObjects.TryAdd((texture.assetsFile.fileName, texture.m_PathID), texture);
                        }
                    }
                }
            }

            var materialRows = materialAudits.Values.OrderBy(row => row.Object.Source, StringComparer.OrdinalIgnoreCase).ThenBy(row => row.Object.PathId).ToArray();
            var materialTreeRows = materialTypeTrees.Values.OrderBy(row => row.Object.Source, StringComparer.OrdinalIgnoreCase).ThenBy(row => row.Object.PathId).ToArray();
            var textureRows = textureObjects.Values
                .OrderBy(texture => texture.assetsFile.fileName, StringComparer.OrdinalIgnoreCase)
                .ThenBy(texture => texture.m_PathID)
                .Select(texture => Program.ExportSourceTexture(texture, outputPath))
                .ToArray();
            WriteJson(outputPath, "mesh_audits.json", meshAudits);
            WriteJson(outputPath, "avatar_native.json", Program.AuditTypeTree(avatar));
            WriteJson(outputPath, "rig_avatars.json", resolvedRenderers.Select(renderer => renderer.RigAvatar)
                .DistinctBy(value => (value.assetsFile.fileName, value.m_PathID))
                .Select(value => new { source = Identity(value), native = Program.AuditTypeTree(value) }).ToArray());
            WriteJson(outputPath, "bone_path_map.json", resolvedRenderers.Select(renderer => new
            {
                renderer.ExportName,
                mesh = Identity(renderer.Mesh),
                rig_avatar = Identity(renderer.RigAvatar),
                renderer.ParentBoneTransformName,
                bone_name_hashes = renderer.Mesh.m_BoneNameHashes.Select(hash => new
                {
                    hash,
                    avatar_path = renderer.RigAvatar.FindBonePath(hash)
                }).ToArray()
            }).ToArray());
            WriteJson(outputPath, "pose_bindpose_diagnostic.json", new
            {
                avatar_poses = resolvedRenderers.GroupBy(renderer => (renderer.RigAvatar.assetsFile.fileName, renderer.RigAvatar.m_PathID))
                    .Select(group => new
                    {
                        avatar = Identity(group.First().RigAvatar),
                        default_pose = AuditPoseConsensus(group.First().RigAvatar, group.ToArray(), "m_DefaultPose", group.First().RigAvatar.m_Avatar.m_DefaultPose.m_X),
                        avatar_skeleton_pose = AuditPoseConsensus(group.First().RigAvatar, group.ToArray(), "m_AvatarSkeletonPose", group.First().RigAvatar.m_Avatar.m_AvatarSkeletonPose.m_X)
                    }).ToArray()
            });
            WriteJson(outputPath, "materials.json", materialRows);
            WriteJson(outputPath, "material_sources.json", materialTreeRows);
            WriteJson(outputPath, "source_texture_artifacts.json", textureRows);

            var resolvedMeshes = resolvedRenderers.Select(renderer => new NpcResolvedMesh(
                renderer.Mesh,
                renderer.ExportName,
                renderer.Materials.Select(material => material.FbxName).ToArray(),
                renderer.RigAvatar,
                renderer.RootBoneId,
                renderer.RootBoneName,
                renderer.ConfiguredRendererDisabled,
                renderer.ParentBoneTransformName,
                renderer.SlotIndex,
                renderer.LodIndex,
                renderer.RendererIndex)).ToArray();
            var geometry = NpcGeometry.Build(avatar, resolvedMeshes, Math.Min(translationTolerance, basisTolerance));
            PortableGeometry.Write(outputPath, geometry.Model);

            WriteJson(outputPath, "placement_audit.json", geometry.PlacementAudit);
            WriteJson(outputPath, "skeleton_audit.json", geometry.SkeletonAudit);
            var assembly = BuildAssembly(
                input,
                avatar,
                avatarMeshConfig,
                resolvedRenderers,
                geometry);
            WriteJson(outputPath, "assembly.json", assembly);
            WriteJson(outputPath, "reader_logs.json", logger.Snapshot());
            logger.RequireNoErrors();
            WriteJson(outputPath, "receipt.json", new
            {
                format = "EndfieldNpcAssemblyReceipt/1",
                status = "source_geometry_exported",
                input = inputPath,
                input_sha256 = HashFile(inputPath),
                character_id = RequireString(selection, "character_id", "selection"),
                avatar = Identity(avatar),
                avatar_mesh_configuration = ConfigIdentity(config, avatarMeshConfig),
                avatar_templet_configuration = templet == null || avatarTempletConfig == null ? null : ConfigIdentity(templet, avatarTempletConfig),
                configured_mesh_instances = resolvedRenderers.Count,
                resolved_material_objects = materialRows.Length,
                source_texture_artifacts = textureRows.Length,
                geometry_format = "EndfieldCharacterGeometry/1",
                geometry_path = "model.geometry.json",
                geometry_sha256 = HashFile(Path.Combine(outputPath, "model.geometry.json")),
                limitations = new[]
                {
                    "This is an Avatar plus AvatarMesh LOD configuration assembly, not a runtime prefab or Unity GameObject/Renderer reconstruction.",
                    "Configured isActive and shadow-caster flags are preserved as source fields; their runtime semantics are not inferred.",
                    "FBX materials are identity placeholders. Source Material TypeTrees and source texture artifacts are emitted separately; shader equivalence is not claimed."
                }
            });
            Console.WriteLine(JsonSerializer.Serialize(new
            {
                status = "source_geometry_exported",
                output = outputPath,
                renderers = resolvedRenderers.Count,
                materials = materialRows.Length,
                textures = textureRows.Length
            }, JsonOptions));
        }
        finally
        {
            WriteJson(outputPath, "reader_logs.json", logger.Snapshot());
            manager.Clear();
        }
    }

    private static BundleSpec[] ReadBundles(JsonElement input, string basePath)
    {
        var rows = RequireArray(input, "bundles", "input").EnumerateArray().ToArray();
        if (rows.Length == 0)
            throw new InvalidDataException("Input bundle closure is empty.");
        var result = new BundleSpec[rows.Length];
        for (var index = 0; index < rows.Length; index++)
        {
            var row = rows[index];
            var declaredPath = RequireString(row, "path", $"bundles[{index}]");
            var path = Path.GetFullPath(declaredPath, basePath);
            var bytes = RequireInt64(row, "bytes", $"bundles[{index}]");
            var sha = RequireSha256(RequireString(row, "sha256", $"bundles[{index}]"), $"bundles[{index}].sha256");
            var info = new FileInfo(path);
            if (!info.Exists)
                throw new FileNotFoundException($"Prepared input bundle is missing: {path}", path);
            if (info.Length != bytes || !string.Equals(HashFile(path), sha, StringComparison.OrdinalIgnoreCase))
                throw new InvalidDataException($"Prepared input bundle identity mismatch: path={path}, expected_bytes={bytes}, actual_bytes={info.Length}, expected_sha256={sha}.");
            result[index] = new BundleSpec(path, bytes, sha);
        }
        if (result.Select(bundle => bundle.Path).Distinct(StringComparer.OrdinalIgnoreCase).Count() != result.Length)
            throw new InvalidDataException("Input bundle closure contains duplicate physical paths.");
        return result;
    }

    private static PoseConsensusAudit[] AuditPoseConsensus(Avatar avatar, IReadOnlyList<ResolvedRenderer> renderers, string poseSource, XForm[] pose)
    {
        var skeleton = avatar.m_Avatar.m_AvatarSkeleton;
        var ids = skeleton.m_ID;
        if (pose.Length != ids.Length || ids.Length != skeleton.m_Node.Count)
            throw new InvalidDataException($"Pose diagnostic source arrays disagree: source={poseSource}, pose={pose.Length}, ids={ids.Length}, nodes={skeleton.m_Node.Count}.");
        var paths = ids.Select(id => avatar.FindBonePath(id) ?? throw new InvalidDataException($"Pose diagnostic cannot resolve Avatar bone path: {id}")).ToArray();
        var indexByPath = paths.Select((path, index) => (path, index)).ToDictionary(row => row.path, row => row.index, StringComparer.Ordinal);
        var world = new Matrix4x4[ids.Length];
        for (var index = 0; index < ids.Length; index++)
        {
            var parent = skeleton.m_Node[index].m_ParentId;
            if ((index == 0 && parent != -1) || (index > 0 && (parent < 0 || parent >= index)))
                throw new InvalidDataException($"Pose diagnostic found invalid Avatar preorder: source={poseSource}, node={index}, parent={parent}.");
            var local = Matrix4x4.Translate(pose[index].t) * Matrix4x4.Rotate(pose[index].q) * Matrix4x4.Scale(pose[index].s);
            world[index] = index == 0 ? local : world[parent] * local;
        }

        var reports = new List<PoseConsensusAudit>(renderers.Count);
        foreach (var renderer in renderers)
        {
            var bonePaths = renderer.Mesh.m_BoneNameHashes.Select(hash => avatar.FindBonePath(hash)
                ?? throw new InvalidDataException($"Pose diagnostic cannot resolve mesh bone hash: mesh={renderer.ExportName}, hash={hash}.")).ToArray();
            var weightedIndices = new HashSet<int>();
            foreach (var skin in renderer.Mesh.m_Skin)
            {
                if (skin.weight.Length != skin.boneIndex.Length)
                    throw new InvalidDataException($"Pose diagnostic found mismatched skin arrays: mesh={renderer.ExportName}.");
                for (var influence = 0; influence < skin.weight.Length; influence++)
                    if (skin.weight[influence] > 0)
                        weightedIndices.Add(skin.boneIndex[influence]);
            }
            var candidates = weightedIndices.Order().Select(boneIndex =>
            {
                if (boneIndex < 0 || boneIndex >= bonePaths.Length || !indexByPath.TryGetValue(bonePaths[boneIndex], out var skeletonIndex))
                    throw new InvalidDataException($"Pose diagnostic found unresolved weighted bone: mesh={renderer.ExportName}, bone_index={boneIndex}.");
                return (BoneIndex: boneIndex, BoneNameHash: renderer.Mesh.m_BoneNameHashes[boneIndex], AvatarPath: bonePaths[boneIndex], Matrix: world[skeletonIndex] * renderer.Mesh.m_BindPose[boneIndex]);
            }).ToArray();
            if (candidates.Length == 0)
                throw new InvalidDataException($"Pose diagnostic found no weighted bones: mesh={renderer.ExportName}.");
            var reference = candidates[0].Matrix;
            var deltas = candidates.Select(candidate => new PoseCandidateDelta(
                candidate.BoneIndex,
                candidate.BoneNameHash,
                candidate.AvatarPath,
                Math.Sqrt(Enumerable.Range(0, 3).Sum(index => Math.Pow(candidate.Matrix[index, 3] - reference[index, 3], 2))),
                Enumerable.Range(0, 16).Where(index => index is not (12 or 13 or 14)).Max(index => Math.Abs((double)candidate.Matrix[index] - reference[index])),
                Enumerable.Range(0, 16).Select(index => candidate.Matrix[index]).ToArray())).ToArray();
            reports.Add(new PoseConsensusAudit(
                poseSource,
                renderer.ExportName,
                candidates.Length,
                deltas.Max(row => row.TranslationResidual),
                deltas.Max(row => row.BasisResidual),
                deltas));
        }
        return reports.ToArray();
    }

    private static SourceConfig ReadConfigSpec(JsonElement row, string role)
    {
        var objectRow = RequireObject(row, "object", role);
        return new SourceConfig(
            RequireString(row, "source_json_path", role),
            RequireSha256(RequireString(row, "source_json_sha256", role), $"{role}.source_json_sha256"),
            RequireString(objectRow, "cab", $"{role}.object"),
            ParseInt64(RequireString(objectRow, "path_id", $"{role}.object"), $"{role}.object.path_id"),
            RequireString(objectRow, "name", $"{role}.object"),
            RequireString(objectRow, "container", $"{role}.object"),
            RequireSha256(RequireString(objectRow, "raw_data_sha256", $"{role}.object"), $"{role}.object.raw_data_sha256"),
            RequireString(objectRow, "type_tree_source", $"{role}.object"),
            RequireString(row, "configuration_role", role));
    }

    private static MonoBehaviour ResolveConfig(AssetsManager manager, SourceConfig config)
    {
        var file = SourceFile(manager, config.Cab);
        var value = ObjectAt<MonoBehaviour>(file, config.PathId, config.Name, config.ConfigurationRole);
        VerifyObjectHash(value, config.RawDataSha256, config.ConfigurationRole);
        var sidecar = ReadConfigJson(config);
        var meta = RequireObject(sidecar, "$animestudio", config.SourceJsonPath);
        if (RequireInt64(meta, "pathId", "$animestudio") != config.PathId ||
            !string.Equals(RequireString(meta, "sourceFile", "$animestudio"), config.Cab, StringComparison.OrdinalIgnoreCase) ||
            !string.Equals(RequireString(meta, "name", "$animestudio"), config.Name, StringComparison.Ordinal) ||
            !string.Equals(RequireSha256(RequireString(meta, "rawDataSha256", "$animestudio"), "$animestudio.rawDataSha256"), config.RawDataSha256, StringComparison.OrdinalIgnoreCase) ||
            !string.Equals(RequireString(sidecar, "m_Name", config.SourceJsonPath), config.Name, StringComparison.Ordinal))
            throw new InvalidDataException($"Configuration sidecar identity does not match its exact source object: role={config.ConfigurationRole}, cab={config.Cab}, path_id={config.PathId}.");
        var embeddedTree = value.ToType();
        if (embeddedTree == null)
            throw new InvalidDataException($"Pinned reader cannot parse the selected {config.ConfigurationRole} source TypeTree: cab={config.Cab}, path_id={config.PathId}.");
        return value;
    }

    private static JsonElement ReadConfigJson(SourceConfig config)
    {
        if (!File.Exists(config.SourceJsonPath))
            throw new FileNotFoundException($"Configuration TypeTree sidecar is missing: {config.SourceJsonPath}", config.SourceJsonPath);
        if (!string.Equals(HashFile(config.SourceJsonPath), config.SourceJsonSha256, StringComparison.OrdinalIgnoreCase))
            throw new InvalidDataException($"Configuration TypeTree sidecar hash changed: {config.SourceJsonPath}.");
        using var document = JsonDocument.Parse(File.ReadAllBytes(config.SourceJsonPath));
        return document.RootElement.Clone();
    }

    private static void VerifyAvatarTempletPointer(MonoBehaviour? templet, Avatar avatar)
    {
        if (templet == null)
            return;
        var tree = templet.ToType() ?? throw new InvalidDataException($"AvatarTemplet TypeTree is unavailable: {templet.assetsFile.fileName}/{templet.m_PathID}.");
        if (!tree.Contains("sizeAvatar"))
            throw new InvalidDataException($"Selected AvatarTemplet source has no sizeAvatar field: {templet.assetsFile.fileName}/{templet.m_PathID}.");
        var pointer = tree["sizeAvatar"];
        var json = JsonDocument.Parse(Newtonsoft.Json.JsonConvert.SerializeObject(pointer)).RootElement;
        if (!json.TryGetProperty("m_PathID", out var pathValue) || !long.TryParse(pathValue.ToString(), NumberStyles.Integer, CultureInfo.InvariantCulture, out var pathId) || pathId != avatar.m_PathID)
            throw new InvalidDataException($"AvatarTemplet.sizeAvatar does not point to selected Avatar: templet={templet.m_PathID}, avatar={avatar.m_PathID}.");
        if (!json.TryGetProperty("m_FileID", out var fileValue) || !int.TryParse(fileValue.ToString(), NumberStyles.Integer, CultureInfo.InvariantCulture, out var fileId) || fileId <= 0)
            throw new InvalidDataException($"AvatarTemplet.sizeAvatar has no external-file pointer: templet={templet.m_PathID}.");
    }

    private static Avatar ResolveRendererAvatar(AssetsManager manager, Avatar hostAvatar, JsonElement slot,
        JsonElement rendererInput, JsonElement sourceRow)
    {
        var slotName = RequireString(slot, "name", "AvatarMesh slot");
        var useSelfAvatarValue = RequireInt32(slot, "bUseSelfAvatar", $"AvatarMesh slot {slotName}");
        if (useSelfAvatarValue is not 0 and not 1)
            throw new InvalidDataException($"NPC slot self-avatar flag must be 0 or 1: slot={slotName}, value={useSelfAvatarValue}.");
        var usesSelfAvatar = useSelfAvatarValue == 1;
        var parentName = SourceOptionalString(slot, "parentBoneName", $"AvatarMesh slot {slotName}");
        var parentPath = SourceOptionalString(slot, "parentBoneTransformName", $"AvatarMesh slot {slotName}");
        var selfAvatarGuid = ReadGuid(RequireObject(slot, "selfAvatarGuid", $"AvatarMesh slot {slotName}"),
            $"AvatarMesh slot {slotName}.selfAvatarGuid");
        var sourceMeshGuid = ReadGuid(RequireObject(sourceRow, "meshGuid", "AvatarMesh renderer"), "AvatarMesh renderer.meshGuid");
        if (rendererInput.TryGetProperty("slot_use_self_avatar", out var useSelfElement))
        {
            if (useSelfElement.ValueKind is not (JsonValueKind.True or JsonValueKind.False) || useSelfElement.GetBoolean() != usesSelfAvatar)
                throw new InvalidDataException($"Selected slot self-avatar flag differs from source: slot={slotName}.");
            var inputGuid = ReadStringArray(RequireArray(rendererInput, "slot_self_avatar_guid", "renderer input"), "renderer.slot_self_avatar_guid")
                .Select(value => uint.TryParse(value, NumberStyles.None, CultureInfo.InvariantCulture, out var word)
                    ? word : throw new InvalidDataException($"Invalid renderer self-avatar GUID word: slot={slotName}, value={value}."))
                .ToArray();
            if (!inputGuid.SequenceEqual(selfAvatarGuid))
                throw new InvalidDataException($"Selected slot self-avatar GUID differs from source: slot={slotName}.");
            var inputParentName = OptionalString(rendererInput, "slot_parent_bone_name", "renderer input");
            var inputParentPath = OptionalString(rendererInput, "slot_parent_bone_transform_name", "renderer input");
            if (inputParentName != parentName || inputParentPath != parentPath)
                throw new InvalidDataException($"Selected slot parent attachment differs from source: slot={slotName}.");
        }
        else if (usesSelfAvatar)
        {
            throw new InvalidDataException($"Self-avatar source slot requires grouped selection evidence: slot={slotName}.");
        }

        if (!usesSelfAvatar)
        {
            if (parentName != null || parentPath != null || selfAvatarGuid.Any(word => word != 0))
                throw new InvalidDataException($"Non-self-avatar slot contains attachment metadata: slot={slotName}.");
            return hostAvatar;
        }

        if (parentName == null || parentPath == null || !parentPath.StartsWith("Root/", StringComparison.Ordinal) ||
            parentPath.Split('/').Any(component => component is "" or "." or "..") ||
            !string.Equals(parentPath.Split('/').Last(), parentName, StringComparison.Ordinal) ||
            selfAvatarGuid.All(word => word == 0) || !sourceMeshGuid.SequenceEqual(selfAvatarGuid))
            throw new InvalidDataException($"Self-avatar slot has invalid parent path or GUID binding: slot={slotName}.");
        var hostPaths = hostAvatar.m_Avatar.m_AvatarSkeleton.m_ID
            .Select(id => hostAvatar.FindBonePath(id) ?? throw new InvalidDataException($"Host Avatar path is missing: id={id}."))
            .ToHashSet(StringComparer.Ordinal);
        if (!hostPaths.Contains(parentPath))
            throw new InvalidDataException($"Self-avatar parent transform is absent from the source host Avatar: slot={slotName}, parent={parentPath}.");
        var mesh = RequireObject(rendererInput, "mesh", "renderer input");
        var meshCab = RequireString(mesh, "provider_cab", "renderer mesh");
        var meshFile = SourceFile(manager, meshCab);
        var avatars = meshFile.Objects.OfType<Avatar>().ToArray();
        if (avatars.Length != 1)
            throw new InvalidDataException($"Self-avatar mesh provider must contain exactly one Avatar: slot={slotName}, cab={meshCab}, avatars={avatars.Length}.");
        return avatars[0];
    }

    private static void VerifySelectedSlotMetadata(JsonElement metadata, string slotName, int slotIndex,
        int lodIndex, JsonElement sourceSlot)
    {
        var metadataName = RequireString(metadata, "slot_name", "avatar_mesh_slots entry");
        if (metadataName != slotName || RequireInt32(metadata, "slot_index", "avatar_mesh_slots entry") != slotIndex ||
            RequireInt32(metadata, "lod_index", "avatar_mesh_slots entry") != lodIndex)
            throw new InvalidDataException($"Selected slot identity differs from exact AvatarMesh source row: slot={slotName}.");
        var usesSelfAvatar = RequireInt32(sourceSlot, "bUseSelfAvatar", $"AvatarMesh slot {slotName}") == 1;
        var metadataUseSelfAvatar = RequireBoolean(metadata, "use_self_avatar", "avatar_mesh_slots entry");
        var guid = ReadStringArray(RequireArray(metadata, "self_avatar_guid", "avatar_mesh_slots entry"),
            "avatar_mesh_slots.self_avatar_guid").Select(value => uint.TryParse(value, NumberStyles.None,
                CultureInfo.InvariantCulture, out var word) ? word : throw new InvalidDataException("Invalid selected-slot GUID word.")).ToArray();
        var sourceGuid = ReadGuid(RequireObject(sourceSlot, "selfAvatarGuid", $"AvatarMesh slot {slotName}"), "selfAvatarGuid");
        if (metadataUseSelfAvatar != usesSelfAvatar || !guid.SequenceEqual(sourceGuid) ||
            OptionalString(metadata, "parent_bone_name", "avatar_mesh_slots entry") != SourceOptionalString(sourceSlot, "parentBoneName", $"AvatarMesh slot {slotName}") ||
            OptionalString(metadata, "parent_bone_transform_name", "avatar_mesh_slots entry") != SourceOptionalString(sourceSlot, "parentBoneTransformName", $"AvatarMesh slot {slotName}"))
            throw new InvalidDataException($"Selected slot attachment metadata differs from exact AvatarMesh source row: slot={slotName}.");
    }

    private static void VerifyRendererSlotOrder(JsonElement renderer, string slotName, int slotIndex,
        int lodIndex, int rendererIndex)
    {
        if (RequireString(renderer, "slot_name", "renderer input") != slotName ||
            RequireInt32(renderer, "lod", "renderer input") != lodIndex ||
            RequireInt32(renderer, "renderer_index", "renderer input") != rendererIndex)
            throw new InvalidDataException($"Renderer rows are not in exact selected-slot/LOD order: slot={slotName}, index={rendererIndex}.");
        if (renderer.TryGetProperty("slot_index", out var value) &&
            (!value.TryGetInt32(out var actualIndex) || actualIndex != slotIndex))
            throw new InvalidDataException($"Renderer source slot index differs from its configuration row: slot={slotName}.");
    }

    private static ResolvedRenderer ResolveRenderer(
        AssetsManager manager,
        Avatar avatar,
        JsonElement input,
        JsonElement sourceRow,
        int slotIndex,
        int lodIndex,
        int expectedIndex,
        bool usesSelfAvatar,
        string? parentBoneTransformName)
    {
        var index = RequireInt32(input, "renderer_index", "renderer input");
        if (index != expectedIndex || RequireInt32(input, "lod", "renderer input") != lodIndex)
            throw new InvalidDataException($"Renderer rows are not in exact selected-LOD order: expected_index={expectedIndex}, actual_index={index}, expected_lod={lodIndex}.");
        var slotName = RequireString(input, "slot_name", "renderer input");
        var descriptorName = RequireString(input, "descriptor_name", "renderer input");
        var exportName = RequireString(input, "export_name", "renderer input");
        if (Encoding.UTF8.GetByteCount(exportName) > 63)
            throw new InvalidDataException($"FBX mesh object name exceeds Blender's 63-byte limit: {exportName}.");
        if (!string.Equals(RequireString(sourceRow, "meshName", "AvatarMesh source row"), descriptorName, StringComparison.Ordinal) ||
            !string.Equals(RequireString(sourceRow, "meshName", "AvatarMesh source row"), RequireString(RequireObject(input, "mesh", "renderer input"), "name", "renderer mesh"), StringComparison.Ordinal))
            throw new InvalidDataException($"AvatarMesh source mesh name does not match input row: renderer={index}.");

        var meshInput = RequireObject(input, "mesh", "renderer input");
        var meshCab = RequireString(meshInput, "provider_cab", "renderer mesh");
        var meshProvider = RequireString(meshInput, "provider_logical_name", "renderer mesh");
        var meshResource = RequireString(meshInput, "resource_path", "renderer mesh");
        var meshName = RequireString(meshInput, "name", "renderer mesh");
        var meshPathHash = RequireString(meshInput, "mesh_path_hash", "renderer mesh");
        var guidWords = RequireArray(meshInput, "guid_words", "renderer mesh").EnumerateArray()
            .Select(value => value.ValueKind == JsonValueKind.String ? value.GetString()! : value.GetRawText())
            .ToArray();
        if (guidWords.Length != 4 || guidWords.Any(word => !uint.TryParse(word, NumberStyles.None, CultureInfo.InvariantCulture, out _)))
            throw new InvalidDataException($"Mesh GUID must contain exactly four uint32 words: {meshName}.");
        VerifyConfigMeshRow(sourceRow, meshName, meshPathHash, guidWords, input);

        var meshFile = SourceFile(manager, meshCab);
        var meshMatches = meshFile.Objects.OfType<Mesh>().Where(value => string.Equals(value.Name, meshName, StringComparison.Ordinal)).ToArray();
        if (meshMatches.Length != 1)
            throw new InvalidDataException($"Mesh selector did not resolve to exactly one serialized Mesh in the specified CAB: cab={meshCab}, name={meshName}, matches={meshMatches.Length}, resource={meshResource}.");
        var mesh = meshMatches[0];
        ValidateMesh(mesh, avatar, meshName);

        var rootBone = RequireObject(input, "root_bone", "renderer input");
        var configuredRootBoneId = RequireInt32(rootBone, "id", "renderer root_bone");
        var rootBoneName = RequireString(rootBone, "name", "renderer root_bone");
        var avatarIds = avatar.m_Avatar.m_AvatarSkeleton.m_ID;
        var rootBoneMatches = avatarIds.Select((id, index) => (Index: index, Path: avatar.FindBonePath(id)))
            .Where(row => string.Equals(row.Path?.Split('/').Last(), rootBoneName, StringComparison.Ordinal))
            .Select(row => row.Index).ToArray();
        int rootBoneId;
        if (usesSelfAvatar)
        {
            if (rootBoneMatches.Length != 1)
                throw new InvalidDataException($"Self-avatar rootBoneName must resolve to one bone in its source Avatar: renderer={meshName}, name={rootBoneName}, matches={rootBoneMatches.Length}.");
            rootBoneId = rootBoneMatches[0];
        }
        else
        {
            rootBoneId = configuredRootBoneId;
            if (rootBoneId < 0 || rootBoneId >= avatarIds.Length ||
                !string.Equals(avatar.FindBonePath(avatarIds[rootBoneId])?.Split('/').Last(), rootBoneName, StringComparison.Ordinal))
                throw new InvalidDataException($"AvatarMesh rootBoneID/name does not match the host Avatar bone table: renderer={meshName}, index={rootBoneId}, name={rootBoneName}.");
        }

        var configuredDisabledValue = RequireInt32(input, "configured_renderer_disabled", "renderer input");
        var configuredActive = RequireInt32(input, "configured_is_active", "renderer input");
        var configuredRealtime = RequireInt32(input, "configured_real_time_shadow_caster", "renderer input");
        var configuredPlatform = RequireInt32(input, "configured_platform_availability", "renderer input");
        if (RequireInt32(sourceRow, "isRendererDisabled", "AvatarMesh source row") != configuredDisabledValue ||
            RequireInt32(sourceRow, "isActive", "AvatarMesh source row") != configuredActive ||
            RequireInt32(sourceRow, "realTimeShadowCaster", "AvatarMesh source row") != configuredRealtime ||
            RequireInt32(sourceRow, "platformAvailability", "AvatarMesh source row") != configuredPlatform ||
            RequireInt32(sourceRow, "rootBoneID", "AvatarMesh source row") != configuredRootBoneId ||
            !string.Equals(RequireString(sourceRow, "rootBoneName", "AvatarMesh source row"), rootBoneName, StringComparison.Ordinal))
            throw new InvalidDataException($"Raw AvatarMesh configuration flags or root bone differ from the supplied row: renderer={meshName}.");
        if (configuredDisabledValue is not 0 and not 1)
            throw new InvalidDataException($"AvatarMesh renderer-disabled flag must be 0 or 1: renderer={meshName}, value={configuredDisabledValue}.");

        var materialHashes = RequireArray(input, "material_path_hashes", "renderer input").EnumerateArray().Select(value => value.ValueKind == JsonValueKind.String ? value.GetString()! : value.GetRawText()).ToArray();
        var configMaterialHashes = RequireArray(sourceRow, "materialPathHashes", "AvatarMesh source row").EnumerateArray().Select(value => value.GetRawText()).ToArray();
        if (!materialHashes.SequenceEqual(configMaterialHashes, StringComparer.Ordinal))
            throw new InvalidDataException($"AvatarMesh material hash order differs from input: renderer={meshName}.");
        var materialInputs = RequireArray(input, "materials", "renderer input").EnumerateArray().ToArray();
        if (materialInputs.Length != materialHashes.Length || materialInputs.Length < mesh.m_SubMeshes.Count)
            throw new InvalidDataException($"Material binding count must match source hashes and cover every Mesh submesh: renderer={meshName}, bindings={materialInputs.Length}, hashes={materialHashes.Length}, submeshes={mesh.m_SubMeshes.Count}.");
        var materials = materialInputs.Select((value, materialIndex) => ResolveMaterial(manager, value, materialHashes[materialIndex], configuredDisabledValue == 1, meshName, materialIndex)).ToArray();
        return new ResolvedRenderer(
            slotIndex,
            lodIndex,
            index,
            descriptorName,
            exportName,
            slotName,
            avatar,
            usesSelfAvatar,
            parentBoneTransformName,
            mesh,
            meshCab,
            meshProvider,
            meshResource,
            meshPathHash,
            guidWords,
            rootBoneId,
            configuredRootBoneId,
            rootBoneName,
            configuredActive,
            configuredDisabledValue == 1,
            configuredRealtime,
            configuredPlatform,
            materials);
    }

    private static void VerifyConfigMeshRow(JsonElement sourceRow, string meshName, string meshPathHash, string[] guidWords, JsonElement rendererInput)
    {
        var actualHash = RequireInt64(sourceRow, "meshPathHash", "AvatarMesh source row").ToString(CultureInfo.InvariantCulture);
        if (!string.Equals(actualHash, meshPathHash, StringComparison.Ordinal))
            throw new InvalidDataException($"AvatarMesh meshPathHash differs from its input selector: mesh={meshName}, config={actualHash}, input={meshPathHash}.");
        var configGuid = RequireObject(sourceRow, "meshGuid", "AvatarMesh source row");
        var configWords = new[] { "val0", "val1", "val2", "val3" }.Select(name => RequireInt64(configGuid, name, "AvatarMesh meshGuid").ToString(CultureInfo.InvariantCulture)).ToArray();
        if (!configWords.SequenceEqual(guidWords, StringComparer.Ordinal))
            throw new InvalidDataException($"AvatarMesh mesh GUID differs from its input selector: mesh={meshName}.");
        var expectedResourceName = RequireString(RequireObject(rendererInput, "mesh", "renderer input"), "name", "renderer mesh");
        if (!string.Equals(expectedResourceName, meshName, StringComparison.Ordinal))
            throw new InvalidDataException($"Mesh descriptor name is inconsistent: expected={expectedResourceName}, actual={meshName}.");
    }

    private static void ValidateMesh(Mesh mesh, Avatar avatar, string label)
    {
        if (mesh.m_VertexCount <= 0 || mesh.m_Vertices == null || mesh.m_Vertices.Length < mesh.m_VertexCount * 3)
            throw new InvalidDataException($"Source Mesh has no complete vertex buffer: {label}, vertices={mesh.m_VertexCount}.");
        if (mesh.m_SubMeshes.Count == 0 || mesh.m_Indices.Count == 0)
            throw new InvalidDataException($"Source Mesh has no triangle data: {label}.");
        if (mesh.m_BindPose == null || mesh.m_BoneNameHashes == null || mesh.m_BindPose.Length == 0 || mesh.m_BindPose.Length != mesh.m_BoneNameHashes.Length)
            throw new InvalidDataException($"Source Mesh bindpose/name-hash arrays are missing or differ in length: {label}, bindposes={mesh.m_BindPose?.Length ?? 0}, bone_hashes={mesh.m_BoneNameHashes?.Length ?? 0}.");
        if (mesh.m_Skin == null || mesh.m_Skin.Count != mesh.m_VertexCount)
            throw new InvalidDataException($"Source Mesh has no complete four-influence skin array: {label}, weights={mesh.m_Skin?.Count ?? 0}, vertices={mesh.m_VertexCount}.");
        var avatarIds = avatar.m_Avatar.m_AvatarSkeleton.m_ID.ToHashSet();
        foreach (var hash in mesh.m_BoneNameHashes)
        {
            if (!avatarIds.Contains(hash) || string.IsNullOrWhiteSpace(avatar.FindBonePath(hash)))
                throw new InvalidDataException($"Mesh bone name hash does not resolve through the selected Avatar TOS: mesh={label}, hash={hash}.");
        }
        if (mesh.m_Skin.Any(row => row.weight.Length != 4 || row.boneIndex.Length != 4))
            throw new InvalidDataException($"Source Mesh contains an invalid influence width: {label}.");
    }

    private static ResolvedMaterial ResolveMaterial(AssetsManager manager, JsonElement input, string expectedHash, bool rendererDisabled, string meshName, int materialIndex)
    {
        var hash = RequireString(input, "hash", $"renderer {meshName} material[{materialIndex}]");
        if (!string.Equals(hash, expectedHash, StringComparison.Ordinal))
            throw new InvalidDataException($"Material hash order differs from AvatarMesh config: renderer={meshName}, index={materialIndex}.");
        var status = RequireString(input, "status", $"renderer {meshName} material[{materialIndex}]");
        if (status == "unresolved_source_hash")
        {
            if (!rendererDisabled)
                throw new InvalidDataException($"Unresolved material is present on a renderer not explicitly disabled by the AvatarMesh config: renderer={meshName}, hash={hash}.");
            return new ResolvedMaterial(null, "unresolved", MakeUnresolvedMaterialName(meshName, materialIndex, hash), hash, true, null);
        }
        if (status != "provider_resolved_name_requires_probe_confirmation")
            throw new InvalidDataException($"Unsupported input material resolution status: renderer={meshName}, index={materialIndex}, status={status}.");
        var cab = RequireString(input, "provider_cab", $"renderer {meshName} material[{materialIndex}]");
        var provider = RequireString(input, "provider_logical_name", $"renderer {meshName} material[{materialIndex}]");
        var resourcePath = RequireString(input, "resource_path", $"renderer {meshName} material[{materialIndex}]");
        var expectedName = RequireString(input, "name", $"renderer {meshName} material[{materialIndex}]");
        var file = SourceFile(manager, cab);
        var matches = file.Objects.OfType<Material>().Where(value => string.Equals(value.Name, expectedName, StringComparison.Ordinal)).ToArray();
        if (matches.Length != 1)
            throw new InvalidDataException($"Material selector did not resolve uniquely by exact CAB and serialized Object.Name: cab={cab}, name={expectedName}, provider={provider}, resource={resourcePath}, matches={matches.Length}.");
        return new ResolvedMaterial(matches[0], "resolved", MakeMaterialName(matches[0]), hash, false, resourcePath);
    }

    private static string MakeMaterialName(Material material)
    {
        var cabId = material.assetsFile.fileName.Replace("CAB-", string.Empty, StringComparison.OrdinalIgnoreCase);
        var pathId = material.m_PathID.ToString("x16", CultureInfo.InvariantCulture);
        var result = $"npcmat_{cabId}_{pathId}";
        if (Encoding.ASCII.GetByteCount(result) > 63)
            throw new InvalidDataException($"Identity-derived FBX material name exceeds Blender's 63-byte limit: {material.assetsFile.fileName}/{material.m_PathID}.");
        return result;
    }

    private static string MakeUnresolvedMaterialName(string meshName, int materialIndex, string hash)
    {
        var seed = string.Join('\0', meshName, materialIndex.ToString(CultureInfo.InvariantCulture), hash);
        var suffix = Convert.ToHexString(SHA256.HashData(Encoding.UTF8.GetBytes(seed)))[..24].ToLowerInvariant();
        return $"npcmat_u_{suffix}";
    }

    private static object BuildAssembly(
        JsonElement input,
        Avatar avatar,
        SourceConfig config,
        IReadOnlyList<ResolvedRenderer> renderers,
        NpcGeometryResult geometry)
    {
        var avatarSpec = RequireObject(input, "avatar", "input");
        var configIdentity = new
        {
            source = config.Cab,
            path_id = config.PathId.ToString(CultureInfo.InvariantCulture),
            type = "MonoBehaviour",
            name = config.Name,
            raw_data_sha256 = config.RawDataSha256,
            container = config.Container
        };
        return new
        {
            format = "EndfieldNpcAssembly/1",
            input_format = RequireString(input, "format", "input"),
            unity_version = RequireString(input, "unity_version", "input"),
            selection = input.GetProperty("selection").Clone(),
            source_scope = "Source Avatar plus the exact simultaneous PrefabInfo AvatarMesh slots. Self-avatar meshes use their source FBX Avatar and attach only at the configured parent transform. No GameObject hierarchy, Unity Renderer, runtime active state, or runtime LOD selection is reconstructed.",
            avatar = new
            {
                source = avatar.assetsFile.fileName,
                path_id = avatar.m_PathID.ToString(CultureInfo.InvariantCulture),
                type = avatar.type.ToString(),
                name = avatar.Name,
                raw_data_sha256 = HashObject(avatar),
                expected_cab = RequireString(avatarSpec, "cab", "avatar"),
                source_fbx = RequireObject(avatarSpec, "source_fbx", "avatar").Clone(),
                skeleton_bone_count = avatar.m_Avatar.m_AvatarSkeleton.m_ID.Length
            },
            avatar_mesh_configuration = new
            {
                @object = configIdentity,
                source_json_path = config.SourceJsonPath,
                source_json_sha256 = config.SourceJsonSha256,
                configuration_role = config.ConfigurationRole,
                slot_index = renderers[0].SlotIndex,
                slot_name = renderers[0].SlotName,
                lod_index = renderers[0].LodIndex,
                selected_slots = input.TryGetProperty("avatar_mesh_slots", out var selectedSlots)
                    ? selectedSlots.Clone()
                    : JsonDocument.Parse("[]").RootElement.Clone()
            },
            placement_mode = "bindpose_consensus",
            placement_validation = geometry.PlacementAudit,
            skeleton_validation = geometry.SkeletonAudit,
            renderers = renderers.Select(renderer => new
            {
                source_identity_kind = "npc_config_mesh_instance",
                slot_index = renderer.SlotIndex,
                lod_index = renderer.LodIndex,
                renderer_index = renderer.RendererIndex,
                descriptor_name = renderer.DescriptorName,
                export_name = renderer.ExportName,
                slot_name = renderer.SlotName,
                source_renderer_enabled = (bool?)null,
                source_renderer_enabled_status = "not_available_without_GameObject_Renderer",
                cast_shadows = (bool?)null,
                cast_shadows_status = "not_available_without_Unity_Renderer",
                configured_is_active = renderer.ConfiguredIsActive,
                configured_renderer_disabled = renderer.ConfiguredRendererDisabled,
                configured_real_time_shadow_caster = renderer.ConfiguredRealtimeShadowCaster,
                configured_platform_availability = renderer.ConfiguredPlatformAvailability,
                rig_avatar = Identity(renderer.RigAvatar),
                uses_self_avatar = renderer.UsesSelfAvatar,
                parent_bone_transform_name = renderer.ParentBoneTransformName,
                root_bone = new
                {
                    id = renderer.ConfiguredRootBoneId,
                    name = renderer.RootBoneName,
                    avatar_bone_index = renderer.RootBoneId,
                    path = renderer.RigAvatar.FindBonePath(renderer.RigAvatar.m_Avatar.m_AvatarSkeleton.m_ID[renderer.RootBoneId])
                },
                mesh = new
                {
                    source = renderer.Mesh.assetsFile.fileName,
                    path_id = renderer.Mesh.m_PathID.ToString(CultureInfo.InvariantCulture),
                    type = renderer.Mesh.type.ToString(),
                    name = renderer.Mesh.Name,
                    raw_data_sha256 = HashObject(renderer.Mesh),
                    provider_cab = renderer.MeshProviderCab,
                    provider_logical_name = renderer.MeshProviderLogicalName,
                    resource_path = renderer.MeshResourcePath,
                    mesh_path_hash = renderer.MeshPathHash,
                    guid_words = renderer.GuidWords,
                    vertex_count = renderer.Mesh.m_VertexCount,
                    submesh_count = renderer.Mesh.m_SubMeshes.Count,
                    bindpose_count = renderer.Mesh.m_BindPose.Length,
                    bone_name_hash_count = renderer.Mesh.m_BoneNameHashes.Length
                },
                submesh_count = renderer.Mesh.m_SubMeshes.Count,
                material_bindings = renderer.Materials.Select((material, materialIndex) => new
                {
                    material_index = materialIndex,
                    hash = material.PathHash,
                    status = material.Status,
                    configured_disabled = material.ConfiguredDisabled,
                    fbx_material_name = material.FbxName,
                    resource_path = material.ResourcePath,
                    @object = material.Value == null ? null : Identity(material.Value)
                }).ToArray()
            }).ToArray(),
            artifact_scope = new
            {
                geometry = "model.geometry.json and model.geometry.bin",
                material_audits = "materials.json and material_sources.json",
                source_texture_artifacts = "source_texture_artifacts.json",
                geometry_mapping = "renderers[].export_name names the exact imported mesh object; each material binding retains its AvatarMesh slot index and source object identity."
            }
        };
    }

    private static SerializedFile SourceFile(AssetsManager manager, string cab)
    {
        var matches = manager.assetsFileList.Where(file => string.Equals(file.fileName, cab, StringComparison.OrdinalIgnoreCase)).ToArray();
        if (matches.Length != 1)
            throw new InvalidDataException($"Expected exactly one prepared source CAB: cab={cab}, matches={matches.Length}.");
        return matches[0];
    }

    private static T ObjectAt<T>(SerializedFile file, long pathId, string expectedName, string role) where T : AssetObject
    {
        var matches = file.Objects.OfType<T>().Where(value => value.m_PathID == pathId).ToArray();
        if (matches.Length != 1)
            throw new InvalidDataException($"Expected exactly one {role} at source identity: cab={file.fileName}, path_id={pathId}, type={typeof(T).Name}, matches={matches.Length}.");
        if (!string.Equals(matches[0].Name, expectedName, StringComparison.Ordinal))
            throw new InvalidDataException($"Source object name mismatch: role={role}, cab={file.fileName}, path_id={pathId}, expected={expectedName}, actual={matches[0].Name}.");
        return matches[0];
    }

    private static void VerifyObjectHash(AssetObject value, string expectedSha, string role)
    {
        var actualSha = HashObject(value);
        if (!string.Equals(actualSha, expectedSha, StringComparison.OrdinalIgnoreCase))
            throw new InvalidDataException($"Source object raw SHA256 mismatch: role={role}, source={value.assetsFile.fileName}, path_id={value.m_PathID}, expected={expectedSha}, actual={actualSha}.");
    }

    private static object Identity(AssetObject value) => new
    {
        source = value.assetsFile.fileName,
        path_id = value.m_PathID.ToString(CultureInfo.InvariantCulture),
        type = value.type.ToString(),
        name = value.Name,
        raw_data_sha256 = HashObject(value)
    };

    private static object ConfigIdentity(MonoBehaviour value, SourceConfig config) => new
    {
        source = value.assetsFile.fileName,
        path_id = value.m_PathID.ToString(CultureInfo.InvariantCulture),
        type = value.type.ToString(),
        name = value.Name,
        raw_data_sha256 = HashObject(value),
        container = config.Container,
        configuration_role = config.ConfigurationRole
    };

    private static string HashObject(AssetObject value) => Convert.ToHexString(SHA256.HashData(value.GetRawData())).ToLowerInvariant();

    private static string HashFile(string path)
    {
        using var stream = new FileStream(path, FileMode.Open, FileAccess.Read, FileShare.Read);
        return Convert.ToHexString(SHA256.HashData(stream)).ToLowerInvariant();
    }

    private static string RequireString(JsonElement parent, string name, string context)
    {
        if (!parent.TryGetProperty(name, out var value) || value.ValueKind != JsonValueKind.String || string.IsNullOrWhiteSpace(value.GetString()))
            throw new InvalidDataException($"Expected nonempty string field: {context}.{name}.");
        return value.GetString()!;
    }

    private static string RequireString(JsonElement parent, string name, string context, string expected)
    {
        var actual = RequireString(parent, name, context);
        if (!string.Equals(actual, expected, StringComparison.Ordinal))
            throw new InvalidDataException($"Unexpected format value: {context}.{name}, expected={expected}, actual={actual}.");
        return actual;
    }

    private static JsonElement RequireObject(JsonElement parent, string name, string context)
    {
        if (!parent.TryGetProperty(name, out var value) || value.ValueKind != JsonValueKind.Object)
            throw new InvalidDataException($"Expected object field: {context}.{name}.");
        return value;
    }

    private static JsonElement RequireArray(JsonElement parent, string name, string context)
    {
        if (!parent.TryGetProperty(name, out var value) || value.ValueKind != JsonValueKind.Array)
            throw new InvalidDataException($"Expected array field: {context}.{name}.");
        return value;
    }

    private static int RequireInt32(JsonElement parent, string name, string context)
    {
        if (!parent.TryGetProperty(name, out var value) || !value.TryGetInt32(out var result))
            throw new InvalidDataException($"Expected signed 32-bit integer field: {context}.{name}.");
        return result;
    }

    private static long RequireInt64(JsonElement parent, string name, string context)
    {
        if (!parent.TryGetProperty(name, out var value) || !value.TryGetInt64(out var result))
            throw new InvalidDataException($"Expected signed 64-bit integer field: {context}.{name}.");
        return result;
    }

    private static double RequireFiniteDouble(JsonElement parent, string name, string context)
    {
        if (!parent.TryGetProperty(name, out var value) || !value.TryGetDouble(out var result) || !double.IsFinite(result))
            throw new InvalidDataException($"Expected finite number field: {context}.{name}.");
        return result;
    }

    private static bool RequireBoolean(JsonElement parent, string name, string context)
    {
        if (!parent.TryGetProperty(name, out var value) || value.ValueKind is not (JsonValueKind.True or JsonValueKind.False))
            throw new InvalidDataException($"Expected boolean field: {context}.{name}.");
        return value.GetBoolean();
    }

    private static JsonElement[] ReadArray(JsonElement value, string context)
    {
        if (value.ValueKind != JsonValueKind.Array)
            throw new InvalidDataException($"Expected JSON array: {context}.");
        return value.EnumerateArray().ToArray();
    }

    private static string[] ReadStringArray(JsonElement value, string context)
    {
        var rows = ReadArray(value, context);
        if (rows.Any(row => row.ValueKind != JsonValueKind.String || string.IsNullOrWhiteSpace(row.GetString())))
            throw new InvalidDataException($"Expected nonempty strings in array: {context}.");
        return rows.Select(row => row.GetString()!).ToArray();
    }

    private static uint[] ReadGuid(JsonElement value, string context)
    {
        if (value.ValueKind != JsonValueKind.Object)
            throw new InvalidDataException($"Expected GUID object: {context}.");
        var words = new uint[4];
        for (var index = 0; index < words.Length; index++)
        {
            var field = "val" + index.ToString(CultureInfo.InvariantCulture);
            if (!value.TryGetProperty(field, out var word) || !word.TryGetUInt32(out words[index]))
                throw new InvalidDataException($"Expected uint32 GUID word: {context}.{field}.");
        }
        return words;
    }

    private static string? SourceOptionalString(JsonElement parent, string name, string context)
    {
        if (!parent.TryGetProperty(name, out var value) || value.ValueKind != JsonValueKind.String)
            throw new InvalidDataException($"Expected source string field: {context}.{name}.");
        var text = value.GetString();
        return string.IsNullOrEmpty(text) ? null : text;
    }

    private static string? OptionalString(JsonElement parent, string name, string context)
    {
        if (!parent.TryGetProperty(name, out var value) || value.ValueKind == JsonValueKind.Null)
            return null;
        if (value.ValueKind != JsonValueKind.String)
            throw new InvalidDataException($"Expected optional string field: {context}.{name}.");
        var text = value.GetString();
        return string.IsNullOrEmpty(text) ? null : text;
    }

    private static long ParseInt64(string value, string context)
    {
        if (!long.TryParse(value, NumberStyles.Integer, CultureInfo.InvariantCulture, out var result))
            throw new InvalidDataException($"Expected decimal Int64 identity: {context}={value}.");
        return result;
    }

    private static string RequireSha256(string value, string context)
    {
        if (value.Length != 64 || !value.All(Uri.IsHexDigit))
            throw new InvalidDataException($"Expected 64-digit SHA256: {context}.");
        return value.ToLowerInvariant();
    }

    private static void WriteJson(string directory, string fileName, object value)
    {
        var path = Path.Combine(directory, fileName);
        var bytes = JsonSerializer.SerializeToUtf8Bytes(value, JsonOptions);
        using var stream = new FileStream(path, File.Exists(path) ? FileMode.Create : FileMode.CreateNew, FileAccess.Write, FileShare.None);
        stream.Write(bytes);
        stream.WriteByte((byte)'\n');
    }
}
