using System.Collections.Specialized;
using System.Security.Cryptography;
using System.Text.Json;
using System.Text.Json.Serialization;
using AnimeStudio;

internal sealed record ReaderLog(string Severity, string Message);

internal sealed class ProbeLogger : ILogger
{
    private readonly List<ReaderLog> entries = new();

    public void Log(LoggerEvent severity, string message)
    {
        if (entries.Count >= 2000) throw new InvalidDataException("Reader exceeded 2000 diagnostic records.");
        entries.Add(new ReaderLog(severity.ToString(), message));
    }

    public ReaderLog[] Snapshot() => entries.ToArray();

    public void RequireSuccess()
    {
        ReaderLog[] failures = entries.Where(entry => entry.Severity == "Error" || entry.Message.Contains("Falling back", StringComparison.OrdinalIgnoreCase)).ToArray();
        if (failures.Length != 0) throw new InvalidDataException($"Reader emitted {failures.Length} errors or parser fallbacks.");
    }
}

internal static class Program
{
    private static readonly JsonSerializerOptions Options = new()
    {
        PropertyNamingPolicy = JsonNamingPolicy.SnakeCaseLower,
        WriteIndented = true,
        NumberHandling = JsonNumberHandling.Strict
    };

    private static int Main(string[] args)
    {
        try
        {
            if (args.Length != 4 || args[0] != "--input" || args[2] != "--output")
                throw new ArgumentException("Expected --input probe_manifest.json --output new_probe_directory.");
            RunByFormat(Path.GetFullPath(args[1]), Path.GetFullPath(args[3]));
            return 0;
        }
        catch (Exception error)
        {
            Console.Error.WriteLine(JsonSerializer.Serialize(new { error_type = error.GetType().FullName, message = error.Message }, Options));
            return 1;
        }
    }

    private static void RunByFormat(string inputPath, string outputPath)
    {
        using JsonDocument manifest = JsonDocument.Parse(File.ReadAllText(inputPath));
        string format = RequiredString(manifest.RootElement, "format");
        if (format == "EndfieldExpressionAssetProbeInput/1")
        {
            RunMorph(inputPath, outputPath);
            return;
        }
        if (format == "EndfieldAvatarNodeProbeInput/1")
        {
            RunAvatar(manifest.RootElement, outputPath);
            return;
        }
        throw new InvalidDataException($"Unsupported probe input format: {format}.");
    }

    private static void RunMorph(string inputPath, string outputPath)
    {
        if (Directory.Exists(outputPath) || File.Exists(outputPath)) throw new IOException($"Refusing to reuse probe output: {outputPath}.");
        using JsonDocument manifest = JsonDocument.Parse(File.ReadAllText(inputPath));
        JsonElement root = manifest.RootElement;
        if (RequiredString(root, "format") != "EndfieldExpressionAssetProbeInput/1") throw new InvalidDataException("Unsupported probe input format.");
        string bundlePath = Path.GetFullPath(RequiredString(root, "bundle_path"));
        string bundleSha = RequiredString(root, "bundle_sha256");
        string logicalName = RequiredString(root, "logical_name");
        string containerPath = NormalizeResourcePath(RequiredString(root, "container_path"));
        string expectedCab = RequiredString(root, "serialized_file");
        string avatarCab = RequiredString(root, "avatar_cab");
        long avatarPathId = RequiredInt64(root, "avatar_path_id");
        string unityVersion = RequiredString(root, "unity_version");
        string actualBundleSha = Convert.ToHexString(SHA256.HashData(File.ReadAllBytes(bundlePath))).ToLowerInvariant();
        if (!string.Equals(bundleSha, actualBundleSha, StringComparison.OrdinalIgnoreCase))
            throw new InvalidDataException($"Extracted bundle SHA-256 mismatch: expected={bundleSha}, actual={actualBundleSha}.");

        Directory.CreateDirectory(outputPath);
        ProbeLogger logger = new();
        Logger.Default = logger;
        Logger.Flags = LoggerEvent.Warning | LoggerEvent.Error;
        Logger.Silent = false;
        Progress.Silent = true;
        TypeFlags.SetTypes(new Dictionary<ClassIDType, (bool, bool)>
        {
            [ClassIDType.AssetBundle] = (true, false)
        });
        AssetsManager manager = new()
        {
            Game = GameManager.GetGameByType(GameType.ArknightsEndfield),
            ResolveDependencies = false,
            SpecifyUnityVersion = unityVersion,
            Silent = false,
            SkipProcess = false
        };

        try
        {
            manager.LoadPreparedFiles(bundlePath);
            logger.RequireSuccess();
            SerializedFile sourceFile = manager.assetsFileList.SingleOrDefault(file => string.Equals(file.fileName, expectedCab, StringComparison.OrdinalIgnoreCase))
                ?? throw new InvalidDataException($"Expected unique serialized file is absent from selected provider: {expectedCab}.");
            KeyValuePair<string, AssetInfo>[] containers = sourceFile.Objects.OfType<AssetBundle>()
                .SelectMany(bundle => bundle.m_Container)
                .Where(entry => string.Equals(NormalizeResourcePath(entry.Key), containerPath, StringComparison.OrdinalIgnoreCase))
                .ToArray();
            if (containers.Length != 1) throw new InvalidDataException($"Selected provider does not have one exact container path: path={containerPath}, matches={containers.Length}.");
            PPtr<AnimeStudio.Object> assetPointer = containers[0].Value.asset;
            if (assetPointer.m_FileID != 0) throw new InvalidDataException($"Selected morph container points outside its own serialized file: file_id={assetPointer.m_FileID}, path_id={assetPointer.m_PathID}.");
            ObjectInfo objectInfo = sourceFile.m_Objects.SingleOrDefault(info => info.m_PathID == assetPointer.m_PathID)
                ?? throw new InvalidDataException($"Container object PathID is absent: path={containerPath}, path_id={assetPointer.m_PathID}.");
            if (objectInfo.classID != (int)ClassIDType.MonoBehaviour)
                throw new InvalidDataException($"Selected container is not a MonoBehaviour: path={containerPath}, class_id={objectInfo.classID}.");

            AnimeStudio.Object asset = new(new ObjectReader(sourceFile.reader, sourceFile, objectInfo, manager.Game));
            if (!asset.TryReadTypePrefixBeforeField(asset.serializedType.m_Type, "references", "ManagedReferencesRegistry", out OrderedDictionary prefix, out long registryOffset, out Exception readError))
                throw new InvalidDataException("Exact ManagedReferencesRegistry prefix could not be decoded.", readError);
            if (prefix["avatar"] is not OrderedDictionary avatarPointerData)
                throw new InvalidDataException("Decoded morph prefix has no structured avatar pointer.");
            int avatarFileId = RequiredInt32(avatarPointerData, "m_FileID");
            long actualAvatarPathId = RequiredInt64(avatarPointerData, "m_PathID");
            string actualAvatarCab = ResolveExternalCab(sourceFile, avatarFileId);
            if (!string.Equals(actualAvatarCab, avatarCab, StringComparison.OrdinalIgnoreCase) || actualAvatarPathId != avatarPathId)
                throw new InvalidDataException($"Morph asset Avatar PPtr mismatch: expected={avatarCab}/{avatarPathId}, actual={actualAvatarCab}/{actualAvatarPathId}.");

            byte[] raw = asset.GetRawData();
            if (registryOffset < 0 || registryOffset > raw.Length) throw new InvalidDataException($"Registry prefix offset is outside the raw object: offset={registryOffset}, object_bytes={raw.Length}.");
            File.WriteAllBytes(Path.Combine(outputPath, "morph_asset.raw"), raw);
            File.WriteAllText(Path.Combine(outputPath, "prefix.json"), Newtonsoft.Json.JsonConvert.SerializeObject(prefix, Newtonsoft.Json.Formatting.Indented));
            object[] referenceTypes = sourceFile.m_RefTypes.Select(type => (object)new
            {
                class_id = type.classID,
                class_name = type.m_KlassName,
                namespace_name = type.m_NameSpace,
                assembly_name = type.m_AsmName,
                nodes = type.m_Type.m_Nodes.Select(node => new
                {
                    type = node.m_Type,
                    name = node.m_Name,
                    level = node.m_Level,
                    byte_size = node.m_ByteSize,
                    meta_flag = node.m_MetaFlag,
                    reference_type_hash = node.m_RefTypeHash
                }).ToArray()
            }).ToArray();
            File.WriteAllText(Path.Combine(outputPath, "ref_types.json"), JsonSerializer.Serialize(referenceTypes, Options));
            RegistryDecoder.Decode(raw, checked((int)registryOffset), sourceFile.m_RefTypes, outputPath, Options);
            logger.RequireSuccess();
            File.WriteAllText(Path.Combine(outputPath, "reader_logs.json"), JsonSerializer.Serialize(logger.Snapshot(), Options));
            File.WriteAllText(Path.Combine(outputPath, "probe_receipt.json"), JsonSerializer.Serialize(new
            {
                format = "EndfieldExpressionAssetProbeReceipt/1",
                status = "exact_avatar_pointer_and_entire_registry_decoded",
                logical_name = logicalName,
                container_path = containerPath,
                serialized_file = sourceFile.fileName,
                object_path_id = objectInfo.m_PathID,
                object_class_id = objectInfo.classID,
                object_bytes = raw.Length,
                registry_offset = registryOffset,
                bundle_sha256 = actualBundleSha,
                avatar_cab = actualAvatarCab,
                avatar_path_id = actualAvatarPathId,
                avatar_binding = "Exact source Morph MonoBehaviour avatar PPtr resolved through its serialized external table and matched the selected Animator Avatar CAB plus PathID.",
                unity_version = unityVersion
            }, Options));
            Console.WriteLine(JsonSerializer.Serialize(new
            {
                status = "exact_avatar_pointer_and_entire_registry_decoded",
                object_bytes = raw.Length,
                registry_offset = registryOffset,
                avatar_cab = actualAvatarCab,
                avatar_path_id = actualAvatarPathId
            }, Options));
        }
        finally
        {
            manager.Clear();
            Logger.Silent = false;
            Progress.Silent = false;
        }
    }

    private static void RunAvatar(JsonElement root, string outputPath)
    {
        if (Directory.Exists(outputPath) || File.Exists(outputPath)) throw new IOException($"Refusing to reuse Avatar probe output: {outputPath}.");
        string bundlePath = Path.GetFullPath(RequiredString(root, "bundle_path"));
        string bundleSha = RequiredString(root, "bundle_sha256");
        string logicalName = RequiredString(root, "logical_name");
        string expectedCab = RequiredString(root, "serialized_file");
        long expectedPathId = RequiredInt64(root, "avatar_path_id");
        string unityVersion = RequiredString(root, "unity_version");
        string actualBundleSha = Convert.ToHexString(SHA256.HashData(File.ReadAllBytes(bundlePath))).ToLowerInvariant();
        if (!string.Equals(bundleSha, actualBundleSha, StringComparison.OrdinalIgnoreCase))
            throw new InvalidDataException($"Avatar bundle SHA-256 mismatch: expected={bundleSha}, actual={actualBundleSha}.");

        Directory.CreateDirectory(outputPath);
        ProbeLogger logger = new();
        Logger.Default = logger;
        Logger.Flags = LoggerEvent.Warning | LoggerEvent.Error;
        Logger.Silent = false;
        Progress.Silent = true;
        TypeFlags.SetTypes(new Dictionary<ClassIDType, (bool, bool)>
        {
            [ClassIDType.AssetBundle] = (true, false),
            [ClassIDType.Avatar] = (true, false)
        });
        AssetsManager manager = new()
        {
            Game = GameManager.GetGameByType(GameType.ArknightsEndfield),
            ResolveDependencies = false,
            SpecifyUnityVersion = unityVersion,
            Silent = false,
            SkipProcess = false
        };

        try
        {
            manager.LoadPreparedFiles(bundlePath);
            logger.RequireSuccess();
            SerializedFile sourceFile = manager.assetsFileList.SingleOrDefault(file => string.Equals(file.fileName, expectedCab, StringComparison.OrdinalIgnoreCase))
                ?? throw new InvalidDataException($"Expected unique Avatar serialized file is absent from selected provider: {expectedCab}.");
            ObjectInfo info = sourceFile.m_Objects.SingleOrDefault(item => item.m_PathID == expectedPathId)
                ?? throw new InvalidDataException($"Exact Avatar PathID is absent: cab={expectedCab}, path_id={expectedPathId}.");
            if (info.classID != (int)ClassIDType.Avatar)
                throw new InvalidDataException($"Selected Avatar PathID has the wrong class: cab={expectedCab}, path_id={expectedPathId}, class_id={info.classID}.");
            Avatar avatar = new(new ObjectReader(sourceFile.reader, sourceFile, info, manager.Game));
            if (avatar.m_PathID != expectedPathId || !string.Equals(avatar.assetsFile.fileName, expectedCab, StringComparison.OrdinalIgnoreCase))
                throw new InvalidDataException("Parsed Avatar identity differs from the selected exact CAB and PathID.");
            long consumedBytes = avatar.reader.Position - avatar.reader.byteStart;
            if (consumedBytes != avatar.byteSize)
                throw new InvalidDataException($"Avatar TypeTree did not consume the exact serialized object: consumed={consumedBytes}, object_bytes={avatar.byteSize}.");

            var skeleton = avatar.m_Avatar.m_AvatarSkeleton;
            uint[] ids = skeleton.m_ID;
            if (ids.Length != skeleton.m_Node.Count || ids.Length != avatar.m_Avatar.m_SkeletonNameIDArray.Length)
                throw new InvalidDataException($"Avatar skeleton identity arrays disagree: ids={ids.Length}, nodes={skeleton.m_Node.Count}, name_ids={avatar.m_Avatar.m_SkeletonNameIDArray.Length}.");
            var nodes = ids.Select((id, index) =>
            {
                bool hasTosPath = avatar.m_TOS.TryGetValue(id, out string? resolvedPath) && !string.IsNullOrWhiteSpace(resolvedPath);
                if (!hasTosPath && id != 0)
                    throw new InvalidDataException($"Avatar.m_TOS has no exact path for skeleton ID: index={index}, id={id}.");
                string path = hasTosPath ? resolvedPath ?? throw new InvalidDataException($"Avatar.m_TOS path is null: index={index}, id={id}.") : string.Empty;
                string terminal = path.Length == 0 ? string.Empty : path[(path.LastIndexOf('/') + 1)..];
                return new
                {
                    index,
                    avatar_skeleton_id = id,
                    avatar_tos_path = path,
                    avatar_tos_path_status = hasTosPath ? "exact_path_present" : "zero_id_root_sentinel_without_tos_entry",
                    terminal_name = terminal,
                    skeleton_name_id = avatar.m_Avatar.m_SkeletonNameIDArray[index],
                    parent_id = skeleton.m_Node[index].m_ParentId,
                    axes_id = skeleton.m_Node[index].m_AxesId
                };
            }).ToArray();
            File.WriteAllText(Path.Combine(outputPath, "avatar_node_map.json"), JsonSerializer.Serialize(new
            {
                format = "EndfieldAvatarExactNodeMap/1",
                status = "exact_avatar_m_id_to_tos_paths_decoded",
                logical_name = logicalName,
                serialized_file = sourceFile.fileName,
                avatar_path_id = avatar.m_PathID,
                avatar_name = avatar.m_Name,
                avatar_object_bytes = avatar.byteSize,
                parser_consumed_bytes = consumedBytes,
                bundle_sha256 = actualBundleSha,
                node_count = nodes.Length,
                mapping_rule = "Morph basePoseConfig.boneID indexes Avatar.m_Avatar.m_AvatarSkeleton.m_ID; each nonzero uint skeleton ID resolves through Avatar.m_TOS to the exact full path. A zero ID without a TOS entry is preserved as the source root sentinel.",
                nodes
            }, Options));
            logger.RequireSuccess();
            File.WriteAllText(Path.Combine(outputPath, "reader_logs.json"), JsonSerializer.Serialize(logger.Snapshot(), Options));
            Console.WriteLine(JsonSerializer.Serialize(new
            {
                status = "exact_avatar_m_id_to_tos_paths_decoded",
                avatar_path_id = avatar.m_PathID,
                node_count = nodes.Length,
                parser_consumed_bytes = consumedBytes
            }, Options));
        }
        finally
        {
            manager.Clear();
            Logger.Silent = false;
            Progress.Silent = false;
        }
    }

    private static string ResolveExternalCab(SerializedFile sourceFile, int fileId)
    {
        if (fileId == 0) return sourceFile.fileName;
        if (fileId < 0 || fileId > sourceFile.m_Externals.Count)
            throw new InvalidDataException($"Avatar PPtr external FileID is out of range: file_id={fileId}, external_count={sourceFile.m_Externals.Count}.");
        return sourceFile.m_Externals[fileId - 1].fileName;
    }

    private static string NormalizeResourcePath(string value) => value.Replace('\\', '/').TrimStart('/');

    private static string RequiredString(JsonElement source, string name)
    {
        if (!source.TryGetProperty(name, out JsonElement value) || value.ValueKind != JsonValueKind.String || string.IsNullOrWhiteSpace(value.GetString()))
            throw new InvalidDataException($"Required nonempty probe input string is missing: {name}.");
        return value.GetString()!;
    }

    private static long RequiredInt64(JsonElement source, string name)
    {
        if (!source.TryGetProperty(name, out JsonElement value) || value.ValueKind != JsonValueKind.Number || !value.TryGetInt64(out long parsed))
            throw new InvalidDataException($"Required exact 64-bit probe input integer is missing: {name}.");
        return parsed;
    }

    private static int RequiredInt32(OrderedDictionary source, string name)
    {
        if (!source.Contains(name) || source[name] is null) throw new InvalidDataException($"Required decoded prefix integer is missing: {name}.");
        return Convert.ToInt32(source[name], System.Globalization.CultureInfo.InvariantCulture);
    }

    private static long RequiredInt64(OrderedDictionary source, string name)
    {
        if (!source.Contains(name) || source[name] is null) throw new InvalidDataException($"Required decoded prefix integer is missing: {name}.");
        return Convert.ToInt64(source[name], System.Globalization.CultureInfo.InvariantCulture);
    }
}
