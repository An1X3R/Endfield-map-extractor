using System.Globalization;
using System.Reflection;
using System.Security.Cryptography;
using System.Text.Json;
using System.Text.Json.Serialization;
using AnimeStudio;
using AssetObject = AnimeStudio.Object;

internal sealed record BundleInput(string Path, long Bytes, string Sha256);
internal sealed record RootInput(string Source, string ResourcePath);
internal sealed record InputManifest(string Format, string UnityVersion, BundleInput[] Bundles, RootInput Root);
internal sealed record SourceRef(string Source, long PathId, string Type, string Name);
internal sealed record AuditPointer(int FileId, long PathId, SourceRef? Resolved);
internal sealed record AuditRenderer(SourceRef Object, SourceRef GameObject, string Path, SourceRef Mesh, AuditPointer[] Materials);
internal sealed record AuditMaterial(SourceRef Object, AuditPointer Shader);
internal sealed record SourceAudit(string Format, string Commit, RootInput RequestedRoot, SourceRef ActualRoot, AuditRenderer[] Renderers, AuditMaterial[] Materials);
internal sealed record ObjectIdentity(string Source, string PathId);
internal sealed record MeshRequest(ObjectIdentity Renderer, ObjectIdentity Mesh);
internal sealed record ChannelRequest(string Format, string SourceAuditSha256, ObjectIdentity Shader, int SubShaderIndex, int PassIndex, int VertexSubProgramIndex, uint VertexProgramBlobIndex, MeshRequest[] Meshes);
internal sealed record StreamRecord(int Index, uint ChannelMask, uint Offset, uint Stride, byte DividerOp, ushort Frequency);
internal sealed record ChannelRecord(int Index, string Semantic, byte Stream, byte Offset, byte Format, byte RawDimension, byte Dimension, uint StreamChannelMask, uint StreamOffset, uint StreamStride);
internal sealed record FloatDataRecord(string Semantic, int? UnityChannelIndex, int ComponentsPerVertex, int ValueCount, string File, long Bytes, string Sha256);
internal sealed record MeshChannelRecord(ObjectIdentity Identity, string Name, int VertexCount, StreamRecord[] Streams, ChannelRecord[] Channels, FloatDataRecord[] Data, string[] MissingSemantics);
internal sealed record ShaderChannelRecord(sbyte Source, sbyte Target);
internal sealed record ShaderProgramRecord(int SubProgramIndex, uint BlobIndex, string GpuProgramType, uint SourceMap, ushort[] KeywordIndices, string[] Keywords, ShaderChannelRecord[] Channels);
internal sealed record ShaderRecord(ObjectIdentity Identity, string Name, int SubShaderIndex, int PassIndex, uint ProgramMask, bool VertexProgramPresent, int ExpectedVertexSubProgramIndex, uint ExpectedVertexProgramBlobIndex, bool ExpectedIndexInRange, uint? ActualBlobAtExpectedIndex, int[] IndicesMatchingExpectedBlob, bool UseExternalBlobs, int EmbeddedSubShaderBlobCount, ShaderProgramRecord[] VertexSubPrograms, string BindingStatus);
internal sealed record ChannelSidecar(string Format, string ReaderCommit, string InputManifestSha256, string SourceAuditSha256, string RequestSha256, SourceRef Root, ObjectIdentity Shader, ShaderRecord ShaderBinding, MeshChannelRecord[] Meshes);
internal sealed record ReaderLog(string Severity, string Message);

internal sealed class ProbeLogger : ILogger
{
    private readonly List<ReaderLog> rows = new();

    public void Log(LoggerEvent loggerEvent, string message)
    {
        if (rows.Count >= 10000) throw new InvalidDataException("Reader log exceeded 10000 records.");
        rows.Add(new ReaderLog(loggerEvent.ToString(), message));
        if (loggerEvent is LoggerEvent.Error or LoggerEvent.Warning) Console.Error.WriteLine(JsonSerializer.Serialize(rows[^1], Program.JsonOptions));
    }

    public void RequireNoErrors()
    {
        var errors = rows.Where(row => row.Severity == "Error" || row.Message.Contains("Falling back", StringComparison.OrdinalIgnoreCase)).ToArray();
        if (errors.Length > 0) throw new InvalidDataException($"Pinned reader emitted {errors.Length} errors or parser fallbacks; see console output.");
    }

    public ReaderLog[] Snapshot() => rows.ToArray();
}

internal static class Program
{
    internal static readonly JsonSerializerOptions JsonOptions = new()
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
            if (args.Length == 3 && args[0] == "--decompile-dxbc")
            {
                DecompileDxbc(Path.GetFullPath(args[1]), Path.GetFullPath(args[2]));
                return 0;
            }
            if (args.Length != 8 || args[0] != "--input" || args[2] != "--source-audit" || args[4] != "--request" || args[6] != "--output")
                throw new ArgumentException("Expected --input manifest.json --source-audit source_audit.json --request request.json --output new_directory.");
            Run(Path.GetFullPath(args[1]), Path.GetFullPath(args[3]), Path.GetFullPath(args[5]), Path.GetFullPath(args[7]));
            return 0;
        }
        catch (Exception error)
        {
            Console.Error.WriteLine(JsonSerializer.Serialize(new { error_type = error.GetType().FullName, message = error.Message, stack = error.StackTrace }, JsonOptions));
            return 1;
        }
    }

    private static void DecompileDxbc(string inputPath, string outputPath)
    {
        var bytecode = File.ReadAllBytes(inputPath);
        if (bytecode.Length < 4 || !bytecode.AsSpan(0, 4).SequenceEqual("DXBC"u8)) throw new InvalidDataException($"Input is not a DXBC container: {inputPath}.");
        HLSLDecompiler.DecompileShader(bytecode, bytecode.Length, out var hlsl);
        File.WriteAllText(outputPath, hlsl);
        Console.WriteLine(JsonSerializer.Serialize(new { status = "dxbc_decompiled", input = inputPath, output = outputPath, bytes = bytecode.Length }, JsonOptions));
    }

    private static void Run(string inputPath, string auditPath, string requestPath, string outputPath)
    {
        if (Directory.Exists(outputPath) || File.Exists(outputPath)) throw new IOException($"Output must be a new path: {outputPath}");
        var input = JsonSerializer.Deserialize<InputManifest>(File.ReadAllText(inputPath), JsonOptions) ?? throw new InvalidDataException("Input manifest is null.");
        var audit = JsonSerializer.Deserialize<SourceAudit>(File.ReadAllText(auditPath), JsonOptions) ?? throw new InvalidDataException("Source audit is null.");
        var request = JsonSerializer.Deserialize<ChannelRequest>(File.ReadAllText(requestPath), JsonOptions) ?? throw new InvalidDataException("Channel request is null.");
        ValidateDocuments(input, audit, request, auditPath);
        var inputHash = FileHash(inputPath);
        var auditHash = FileHash(auditPath);
        var requestHash = FileHash(requestPath);
        var bundlePaths = input.Bundles.Select(bundle => VerifyBundle(bundle, Path.GetDirectoryName(inputPath)!)).ToArray();
        if (bundlePaths.Distinct(StringComparer.OrdinalIgnoreCase).Count() != bundlePaths.Length) throw new InvalidDataException("Input bundle paths are duplicated.");
        if (bundlePaths.Any(path => IsWithin(outputPath, path))) throw new InvalidDataException("Output path must not contain an input bundle.");

        var logger = new ProbeLogger();
        Logger.Default = logger;
        Logger.Flags = LoggerEvent.Warning | LoggerEvent.Error;
        Logger.Silent = false;
        var parsedTypes = new[] { ClassIDType.AssetBundle, ClassIDType.GameObject, ClassIDType.Transform, ClassIDType.MeshFilter, ClassIDType.MeshRenderer, ClassIDType.SkinnedMeshRenderer, ClassIDType.Mesh, ClassIDType.Material, ClassIDType.Shader };
        TypeFlags.SetTypes(parsedTypes.ToDictionary(type => type, type => (true, false)));
        var manager = new AssetsManager
        {
            Game = GameManager.GetGameByType(GameType.ArknightsEndfield),
            ResolveDependencies = false,
            SpecifyUnityVersion = input.UnityVersion,
            Silent = false,
            SkipProcess = false
        };
        try
        {
            manager.LoadPreparedFiles(bundlePaths);
            logger.RequireNoErrors();
            var root = LoadRoot(manager, input.Root);
            var renderers = WalkTransforms(root).Select(transform => Require(transform.m_GameObject, "hierarchy GameObject")).SelectMany(Renderers).ToArray();
            var requested = request.Meshes.Select(target => LoadRequestedMesh(renderers, target)).ToArray();
            ValidateShaderAssignments(requested, request.Shader);
            var shader = manager.assetsFileList.SelectMany(file => file.Objects.OfType<Shader>())
                .SingleOrDefault(value => Identity(value) == request.Shader)
                ?? throw new InvalidDataException($"Requested shader identity is not loaded: {request.Shader.Source}:{request.Shader.PathId}.");
            var shaderRecord = AuditShader(shader, request);

            Directory.CreateDirectory(outputPath);
            var meshRecords = requested.Select((entry, index) => WriteMesh(entry.Mesh, request.Meshes[index].Mesh, outputPath)).ToArray();
            logger.RequireNoErrors();
            var sidecar = new ChannelSidecar("EndfieldCharacterSnowProjection/1", audit.Commit, inputHash, auditHash, requestHash, audit.ActualRoot, request.Shader, shaderRecord, meshRecords);
            WriteJson(outputPath, "snow_projection_sidecar.json", sidecar);
            WriteJson(outputPath, "reader_logs.json", logger.Snapshot());
            Console.WriteLine(JsonSerializer.Serialize(new { status = "channel_sidecar_completed", output = outputPath, meshes = meshRecords.Length, reader_commit = audit.Commit }, JsonOptions));
        }
        finally
        {
            manager.Clear();
        }
    }

    private static void ValidateDocuments(InputManifest input, SourceAudit audit, ChannelRequest request, string auditPath)
    {
        if (input.Format != "EndfieldSingleCharacterInput/1" || string.IsNullOrWhiteSpace(input.UnityVersion) || input.Bundles.Length == 0) throw new InvalidDataException("Input manifest format, Unity version, and bundle list must be valid.");
        if (audit.Format != "EndfieldCharacterSourceAudit/1" || audit.Commit != "5bb936ca1d3662fe9b956ec669ab7ebea08bfd07") throw new InvalidDataException("Source audit format or pinned reader commit is unsupported.");
        if (request.Format != "EndfieldCharacterSnowChannelRequest/1" || request.Meshes.Length == 0) throw new InvalidDataException("Channel request format and mesh list must be valid.");
        if (!string.Equals(FileHash(auditPath), request.SourceAuditSha256, StringComparison.OrdinalIgnoreCase)) throw new InvalidDataException("Source audit SHA-256 does not match the request.");
        if (!string.Equals(input.Root.Source, audit.ActualRoot.Source, StringComparison.OrdinalIgnoreCase) || input.Root != audit.RequestedRoot) throw new InvalidDataException("Input manifest root does not match the source audit.");
        var requestedMeshKeys = request.Meshes.Select(item => Key(item.Mesh)).ToArray();
        if (requestedMeshKeys.Distinct(StringComparer.OrdinalIgnoreCase).Count() != requestedMeshKeys.Length) throw new InvalidDataException("Channel request contains duplicate mesh identities.");
        foreach (var target in request.Meshes)
        {
            var matches = audit.Renderers.Where(renderer => Identity(renderer.Object) == target.Renderer && Identity(renderer.Mesh) == target.Mesh).ToArray();
            if (matches.Length != 1) throw new InvalidDataException($"Requested renderer/mesh pair must occur once in source audit; found {matches.Length}: renderer={Key(target.Renderer)}, mesh={Key(target.Mesh)}.");
        }
        var shaderMatches = audit.Materials.Where(material => material.Shader.Resolved != null && Identity(material.Shader.Resolved) == request.Shader).ToArray();
        if (shaderMatches.Length == 0) throw new InvalidDataException($"Requested shader identity is not assigned to an audited material: {Key(request.Shader)}.");
    }

    private static GameObject LoadRoot(AssetsManager manager, RootInput rootInput)
    {
        var source = manager.assetsFileList.SingleOrDefault(file => string.Equals(file.fileName, rootInput.Source, StringComparison.OrdinalIgnoreCase))
            ?? throw new InvalidDataException($"Expected unique root CAB not loaded: {rootInput.Source}.");
        var containers = source.Objects.OfType<AssetBundle>().SelectMany(bundle => bundle.m_Container).ToArray();
        var matches = containers.Where(entry => string.Equals(entry.Key.Replace('\\', '/'), rootInput.ResourcePath.Replace('\\', '/'), StringComparison.OrdinalIgnoreCase)).ToArray();
        if (matches.Length != 1) throw new InvalidDataException($"Root container path must match once; found {matches.Length}: {rootInput.ResourcePath}.");
        var selected = Require(matches[0].Value.asset, "root container");
        return selected switch
        {
            GameObject gameObject => gameObject,
            Animator animator => Require(animator.m_GameObject, "root Animator GameObject"),
            _ => throw new InvalidDataException($"Root container type is unsupported: {selected.type}.")
        };
    }

    private static (Renderer Renderer, Mesh Mesh) LoadRequestedMesh(Renderer[] renderers, MeshRequest target)
    {
        var matches = renderers.Where(renderer => Identity(renderer) == target.Renderer).ToArray();
        if (matches.Length != 1) throw new InvalidDataException($"Requested renderer must occur once below selected root; found {matches.Length}: {Key(target.Renderer)}.");
        var renderer = matches[0];
        var mesh = RendererMesh(renderer);
        if (Identity(mesh) != target.Mesh) throw new InvalidDataException($"Requested mesh does not match the renderer's live mesh pointer: expected={Key(target.Mesh)}, actual={Key(Identity(mesh))}.");
        if (renderer.m_Materials.All(pointer => pointer.m_PathID == 0 || !pointer.TryGet<Material>(out var material) || !material.m_Shader.TryGet<Shader>(out _)))
            throw new InvalidDataException($"Renderer {Key(target.Renderer)} has no resolved shader material.");
        return (renderer, mesh);
    }

    private static void ValidateShaderAssignments((Renderer Renderer, Mesh Mesh)[] requested, ObjectIdentity shaderIdentity)
    {
        foreach (var entry in requested)
        {
            var shaderIdentities = entry.Renderer.m_Materials.Where(pointer => pointer.m_PathID != 0)
                .Select(pointer => Require(pointer, "renderer material"))
                .Select(material => Require(material.m_Shader, "material shader"))
                .Select(Identity).ToArray();
            if (!shaderIdentities.Contains(shaderIdentity)) throw new InvalidDataException($"Requested shader {Key(shaderIdentity)} is not assigned to renderer {Key(Identity(entry.Renderer))}.");
        }
    }

    private static ShaderRecord AuditShader(Shader shader, ChannelRequest request)
    {
        var parsed = shader.m_ParsedForm ?? throw new InvalidDataException($"Shader has no parsed form: {Key(Identity(shader))}.");
        if (request.SubShaderIndex < 0 || request.SubShaderIndex >= parsed.m_SubShaders.Count) throw new InvalidDataException($"Shader subshader index is out of range: {request.SubShaderIndex}/{parsed.m_SubShaders.Count}.");
        var passes = parsed.m_SubShaders[request.SubShaderIndex].m_Passes;
        if (request.PassIndex < 0 || request.PassIndex >= passes.Count) throw new InvalidDataException($"Shader pass index is out of range: {request.PassIndex}/{passes.Count}.");
        var pass = passes[request.PassIndex];
        var programs = pass.progVertex?.m_SubPrograms ?? new List<SerializedSubProgram>();
        var actualAtIndex = request.VertexSubProgramIndex >= 0 && request.VertexSubProgramIndex < programs.Count ? programs[request.VertexSubProgramIndex].m_BlobIndex : (uint?)null;
        var records = programs.Select((program, index) => new ShaderProgramRecord(
            index,
            program.m_BlobIndex,
            program.m_GpuProgramType.ToString(),
            program.m_Channels.m_SourceMap,
            program.m_KeywordIndices ?? Array.Empty<ushort>(),
            (program.m_KeywordIndices ?? Array.Empty<ushort>()).Select(keyword => keyword < parsed.m_KeywordNames.Length ? parsed.m_KeywordNames[keyword] : $"<keyword-index-{keyword}-out-of-range>").ToArray(),
            program.m_Channels.m_Channels.Select(channel => new ShaderChannelRecord(channel.source, channel.target)).ToArray()
        )).ToArray();
        var bindingStatus = programs.Count == 0
            ? "Parsed serialized pass exposes no vertex subprogram records; exact ShaderBindChannels mapping is unavailable through this reader view."
            : actualAtIndex == request.VertexProgramBlobIndex
                ? "Requested subprogram index resolves to the expected program blob."
                : "Requested shader sidecar subprogram/blob identity does not match the parsed serialized pass records.";
        return new ShaderRecord(Identity(shader), shader.Name, request.SubShaderIndex, request.PassIndex, pass.m_ProgramMask, pass.progVertex != null, request.VertexSubProgramIndex, request.VertexProgramBlobIndex, actualAtIndex.HasValue, actualAtIndex, programs.Select((program, index) => (program, index)).Where(pair => pair.program.m_BlobIndex == request.VertexProgramBlobIndex).Select(pair => pair.index).ToArray(), shader.m_UseExternalBlobs, shader.subShaderBlobs?.Count ?? 0, records, bindingStatus);
    }

    private static MeshChannelRecord WriteMesh(Mesh mesh, ObjectIdentity identity, string outputPath)
    {
        var vertexData = ReadVertexData(mesh);
        if (mesh.m_VertexCount <= 0 || vertexData.m_VertexCount != mesh.m_VertexCount) throw new InvalidDataException($"Mesh vertex count is inconsistent: {Key(identity)}.");
        var channels = vertexData.m_Channels.Select((channel, index) =>
        {
            if (channel.stream >= vertexData.m_Streams.Count) throw new InvalidDataException($"Mesh channel {index} references missing stream {channel.stream}: {Key(identity)}.");
            var stream = vertexData.m_Streams[channel.stream];
            return new ChannelRecord(index, Semantic(index), channel.stream, channel.offset, channel.format, channel.rawDimension, channel.dimension, stream.channelMask, stream.offset, stream.stride);
        }).ToArray();
        var streams = vertexData.m_Streams.Select((stream, index) => new StreamRecord(index, stream.channelMask, stream.offset, stream.stride, stream.dividerOp, stream.frequency)).ToArray();
        var arrays = new List<(string Semantic, int? ChannelIndex, float[]? Values)> { ("POSITION", 0, mesh.m_Vertices), ("NORMAL", 1, mesh.m_Normals), ("TANGENT", 2, mesh.m_Tangents), ("COLOR", 3, mesh.m_Colors) };
        arrays.AddRange(Enumerable.Range(0, 8).Select(index => ($"TEXCOORD{index}", (int?)(index + 4), (float[]?)mesh.GetUV(index))));
        var data = arrays.Where(item => item.Values != null).Select(item => WriteFloatData(item.Semantic, item.ChannelIndex, item.Values!, mesh.m_VertexCount, identity, outputPath)).ToArray();
        var required = new[] { "POSITION", "NORMAL", "TEXCOORD2", "TEXCOORD5", "TEXCOORD6" };
        var missing = required.Where(semantic => data.All(record => record.Semantic != semantic)).ToArray();
        return new MeshChannelRecord(identity, mesh.m_Name, mesh.m_VertexCount, streams, channels, data, missing);
    }

    private static FloatDataRecord WriteFloatData(string semantic, int? channelIndex, float[] values, int vertexCount, ObjectIdentity identity, string outputPath)
    {
        if (values.Length % vertexCount != 0) throw new InvalidDataException($"Channel {semantic} value count is not divisible by vertex count: {Key(identity)}.");
        var components = values.Length / vertexCount;
        if (components is < 1 or > 4) throw new InvalidDataException($"Channel {semantic} has unsupported dimension {components}: {Key(identity)}.");
        var fileName = $"{SafeFilePart(identity.Source)}_{identity.PathId}_{semantic.ToLowerInvariant()}.f32le";
        var filePath = Path.Combine(outputPath, fileName);
        using (var stream = File.Create(filePath))
        using (var writer = new BinaryWriter(stream)) foreach (var value in values) writer.Write(value);
        return new FloatDataRecord(semantic, channelIndex, components, values.Length, fileName, new FileInfo(filePath).Length, FileHash(filePath));
    }

    private static string Semantic(int channelIndex) => channelIndex switch
    {
        0 => "POSITION",
        1 => "NORMAL",
        2 => "TANGENT",
        3 => "COLOR",
        >= 4 and <= 11 => $"TEXCOORD{channelIndex - 4}",
        _ => $"CHANNEL{channelIndex}"
    };

    private static VertexData ReadVertexData(Mesh mesh)
    {
        var field = typeof(Mesh).GetField("m_VertexData", BindingFlags.Instance | BindingFlags.NonPublic)
            ?? throw new MissingFieldException(typeof(Mesh).FullName, "m_VertexData");
        return field.GetValue(mesh) as VertexData ?? throw new InvalidDataException($"Pinned reader did not expose parsed VertexData for mesh {mesh.m_Name}.");
    }

    private static Renderer[] Renderers(GameObject gameObject)
    {
        var result = new List<Renderer>(2);
        if (gameObject.m_MeshRenderer != null) result.Add(gameObject.m_MeshRenderer);
        if (gameObject.m_SkinnedMeshRenderer != null) result.Add(gameObject.m_SkinnedMeshRenderer);
        return result.ToArray();
    }

    private static Transform[] WalkTransforms(GameObject root)
    {
        var stack = new Stack<Transform>();
        stack.Push(root.m_Transform ?? throw new InvalidDataException("Selected root has no Transform."));
        var rows = new List<Transform>();
        var seen = new HashSet<Transform>();
        while (stack.TryPop(out var transform))
        {
            if (!seen.Add(transform)) throw new InvalidDataException($"Selected hierarchy repeats Transform {transform.m_PathID}.");
            rows.Add(transform);
            foreach (var child in transform.m_Children.AsEnumerable().Reverse()) stack.Push(Require(child, "Transform child"));
        }
        return rows.ToArray();
    }

    private static Mesh RendererMesh(Renderer renderer) => renderer switch
    {
        SkinnedMeshRenderer skinned => Require(skinned.m_Mesh, "skinned mesh"),
        MeshRenderer => Require(Require(renderer.m_GameObject, "renderer GameObject").m_MeshFilter?.m_Mesh ?? throw new InvalidDataException("MeshRenderer has no MeshFilter."), "static mesh"),
        _ => throw new InvalidDataException($"Unsupported renderer type: {renderer.type}.")
    };

    private static T Require<T>(PPtr<T> pointer, string label) where T : AssetObject => pointer.TryGet(out var value) ? value : throw new InvalidDataException($"Unresolved {label}; file_id={pointer.m_FileID}, path_id={pointer.m_PathID}.");
    private static ObjectIdentity Identity(AssetObject value) => new(value.assetsFile.fileName, value.m_PathID.ToString(CultureInfo.InvariantCulture));
    private static ObjectIdentity Identity(SourceRef value) => new(value.Source, value.PathId.ToString(CultureInfo.InvariantCulture));
    private static string Key(ObjectIdentity value) => $"{value.Source}:{value.PathId}";
    private static bool IsWithin(string directory, string file) => Path.GetFullPath(file).StartsWith(Path.GetFullPath(directory).TrimEnd(Path.DirectorySeparatorChar) + Path.DirectorySeparatorChar, StringComparison.OrdinalIgnoreCase);
    private static string SafeFilePart(string value) => string.Concat(value.Select(character => Path.GetInvalidFileNameChars().Contains(character) ? '_' : character));
    private static string FileHash(string path) { using var stream = File.OpenRead(path); return Convert.ToHexString(SHA256.HashData(stream)); }
    private static string VerifyBundle(BundleInput bundle, string basePath)
    {
        var path = Path.GetFullPath(bundle.Path, basePath);
        if (bundle.Bytes <= 0 || bundle.Sha256.Length != 64 || !bundle.Sha256.All(Uri.IsHexDigit)) throw new InvalidDataException($"Invalid bundle identity: {path}.");
        if (new FileInfo(path).Length != bundle.Bytes || !string.Equals(FileHash(path), bundle.Sha256, StringComparison.OrdinalIgnoreCase)) throw new InvalidDataException($"Bundle identity mismatch: {path}.");
        return path;
    }

    private static void WriteJson<T>(string output, string name, T value) => File.WriteAllText(Path.Combine(output, name), JsonSerializer.Serialize(value, JsonOptions));
}
