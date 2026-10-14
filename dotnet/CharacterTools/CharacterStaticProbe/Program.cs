using System.Security.Cryptography;
using System.Globalization;
using System.Reflection;
using System.Text;
using System.Text.Json;
using System.Text.Json.Serialization;
using AnimeStudio;
using EndfieldCharacterModel;
using AssetObject = AnimeStudio.Object;

internal sealed record BundleInput(string Path, long Bytes, string Sha256);
internal sealed record RootInput(string Source, string ResourcePath);
internal sealed record CharacterInput(string Format, string UnityVersion, BundleInput[] Bundles, RootInput Root);
internal sealed record SourceRef(string Source, long PathId, string Type, string Name);
internal sealed record VertexChannelAudit(int UnityChannelIndex, string Semantic, bool DescriptorPresent, byte? Stream, byte? Offset, byte? Format, byte? RawDimension, byte? Dimension, uint? StreamChannelMask, uint? StreamOffset, uint? StreamStride, bool? StreamMaskEnabled, bool DecodedArrayPresent, int DecodedComponentCount, int? DecodedComponentsPerVertex, int? ExpectedDecodedComponentCount, bool? DecodedCountMatchesDescriptor, bool? DecodedValuesFinite, string? DecodedValuesSha256, float[] DecodedValues);
internal sealed record VertexChannelDescriptorAudit(int UnityChannelIndex, byte Stream, byte Offset, byte Format, byte RawDimension, byte Dimension, uint? StreamChannelMask, uint? StreamOffset, uint? StreamStride, bool? StreamMaskEnabled);
internal sealed record VertexStreamAudit(int Index, uint ChannelMask, uint Offset, uint Stride, byte DividerOp, ushort Frequency);
internal sealed record MeshStreamDataAudit(bool HasExternalResource, string Path, long Offset, uint Size, bool ResourceSizeMatchesDecodedVertexData);
internal sealed record MeshStreamAudit(string Format, string InputManifestPath, string InputManifestSha256, string UnityVersion, SourceRef Mesh, int VertexCount, int? PositionComponentsPerVertex, int PositionComponentCount, bool PositionArrayPresent, bool? PositionValuesFinite, string? PositionValuesSha256, float[] SourcePositions, string VertexIndexContract, int VertexDataVertexCount, int VertexDataByteCount, string VertexDataSha256, VertexStreamAudit[] VertexStreams, MeshStreamDataAudit StreamData, VertexChannelDescriptorAudit[] VertexChannelDescriptors, VertexChannelAudit[] Channels);
internal sealed record PointerRef(int FileId, long PathId, SourceRef? Resolved);
internal sealed record TransformAudit(SourceRef Object, SourceRef GameObject, string Path, bool? GameObjectActive, string ActiveAvailability, PointerRef Parent, float[] Position, float[] Rotation, float[] Scale);
internal sealed record SkinAudit(int Vertices, int WeightedVertices, int PositiveInfluences, int ZeroSumVertices, int NonUnitVertices, double MinimumSum, double MaximumSum, double SumTolerance);
internal sealed record MeshAudit(SourceRef Object, int Vertices, int Indices, uint[] BoneNameHashes, float[][] BindposesColumnMajor, SkinAudit Skin);
internal sealed record TextureAudit(string Slot, PointerRef Reference, int? Width, int? Height, string? Format, int? ColorSpace, float[] Scale, float[] Offset);
internal sealed record FloatProperty(string Name, float Value);
internal sealed record IntProperty(string Name, int Value);
internal sealed record ColorProperty(string Name, float[] Rgba);
internal sealed record MaterialAudit(SourceRef Object, PointerRef Shader, TextureAudit[] Textures, FloatProperty[] Floats, IntProperty[] Ints, ColorProperty[] Colors);
internal sealed record RendererAudit(SourceRef Object, SourceRef GameObject, string Path, bool? Enabled, SourceRef Mesh, int SubmeshCount, int BindposeCount, int SourceBoneReferenceCount, int ResolvedSourceBoneCount, int PositiveUnresolvedBoneInfluences, PointerRef[] Bones, PointerRef[] Materials);
internal sealed record AnimatorAudit(SourceRef Object, PointerRef Avatar, PointerRef Controller, bool HasTransformHierarchy);
internal sealed record ContainerAudit(string Path, PointerRef Reference);
internal sealed record SourceAudit(string Format, string Commit, RootInput RequestedRoot, SourceRef ActualRoot, ContainerAudit[] SourceContainers, AnimatorAudit[] Animators, TransformAudit[] Transforms, RendererAudit[] Renderers, MeshAudit[] Meshes, MaterialAudit[] Materials);
internal sealed record ObjectIdentity(string Source, string PathId);
internal sealed record RendererExportRow(ObjectIdentity RendererIdentity, ObjectIdentity GameObjectIdentity, ObjectIdentity MeshIdentity, string SourcePath, string SourceName, string ExportName, string ExportPath);
internal sealed record RendererExportMap(string Format, string Commit, ObjectIdentity Root, RendererExportRow[] Renderers);
internal sealed record ConvertedBone(string? Path, float[] MatrixColumnMajor);
internal sealed record ConvertedMesh(string Path, int Vertices, int Triangles, int PositiveUnresolvedBoneInfluences, ConvertedBone[] Bones, SkinAudit Skin);
internal sealed record ConvertedTexture(string Name, long Bytes, string Sha256);
internal sealed record ConvertedFrame(string Path, float[] Position, float[] Rotation, float[] Scale);
internal sealed record ConvertedMaterial(string Name, string[] Textures, int[] TextureDestinations);
internal sealed record ConverterAudit(string Format, bool CollectAnimations, bool ExportAnimations, int AnimationCount, int MorphCount, ConvertedFrame[] Frames, ConvertedMesh[] Meshes, ConvertedMaterial[] Materials, ConvertedTexture[] Textures, string ShaderFidelity);
internal sealed record ReaderLog(string Severity, string Message);
internal sealed record Artifact(string Path, long Bytes, string Sha256);
internal sealed record TypeTreeAudit(SourceRef Object, bool Available, string Reason, JsonElement? Data);
internal sealed record SourceTextureArtifact(SourceRef Object, int Width, int Height, string SourceFormat, int ColorSpace, bool VerticalFlip, Artifact Png);
internal sealed record TexturePngReuseRow(SourceRef Texture, string ConverterTextureName, string ImageFormat, bool VerticalFlip, string SourcePngSha256, string ConverterPngSha256);
internal sealed record TexturePngReuseAudit(string Format, string IdentityBasis, int SourcePngCount, int ConverterTextureCount, int CacheHitCount, int NotConsumedByConverterCount, TexturePngReuseRow[] ReusedTextures, SourceRef[] NotConsumedByConverter);

/// <summary>Connects the upstream reader logger to bounded, structured evidence.</summary>
internal sealed class ReaderLogger : ILogger
{
    private readonly List<ReaderLog> rows = new();
    public void Log(LoggerEvent loggerEvent, string message)
    {
        var row = new ReaderLog(loggerEvent.ToString(), message);
        if (rows.Count >= 10000) throw new InvalidDataException("Reader log exceeded 10000 records.");
        rows.Add(row);
        Console.Error.WriteLine(JsonSerializer.Serialize(row, Program.JsonOptions()));
    }
    public ReaderLog[] Snapshot() => rows.ToArray();
    public void RequireNoErrors()
    {
        var failures = rows.Where(row => row.Severity is "Error" || row.Message.Contains("Falling back", StringComparison.OrdinalIgnoreCase)).ToArray();
        if (failures.Length != 0) throw new InvalidDataException($"Upstream reader emitted {failures.Length} errors or parser fallbacks; see reader_logs.json.");
    }
}

internal static class Program
{
    internal static GameObject SelectHierarchyRoot(IEnumerable<AssetObject> assets, string resourcePath)
    {
        var entries = assets.ToArray();
        var roots = entries.Select(value => value switch
        {
            GameObject gameObject => gameObject,
            Animator animator => Require(animator.m_GameObject, "root Animator GameObject"),
            _ => null
        }).Where(value => value != null).Cast<GameObject>().Distinct().ToArray();
        if (roots.Length != 1)
            throw new InvalidDataException($"Expected one hierarchy root among container subassets: resource={resourcePath}, roots={roots.Length}, entries={entries.Length}, types={string.Join(',', entries.Select(value => value.type).Distinct())}");
        return roots[0];
    }

    internal static JsonSerializerOptions JsonOptions() => new()
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
            if (args.SequenceEqual(new[] { "--help" }))
            {
                Console.WriteLine(JsonSerializer.Serialize(new { usage = new[] { "CharacterStaticProbe.exe --input manifest.json --output new_directory", "CharacterStaticProbe.exe --mesh-stream-audit --input manifest.json --source CAB --path-id signed_int64 --output new_json" }, geometry_format = "EndfieldCharacterGeometry/1", root_selection = "exact CAB and AssetBundle container resource_path", animations = "disabled" }, JsonOptions()));
                return 0;
            }
            if (args.Length == 9 && args[0] == "--mesh-stream-audit" && args[1] == "--input" && args[3] == "--source" && args[5] == "--path-id" && args[7] == "--output")
            {
                var pathId = long.Parse(args[6], NumberStyles.Integer, CultureInfo.InvariantCulture);
                RunMeshStreamAudit(Path.GetFullPath(args[2]), args[4], pathId, Path.GetFullPath(args[8]));
                return 0;
            }
            if (args.Length != 4 || args[0] != "--input" || args[2] != "--output") throw new ArgumentException("Expected --input manifest.json --output new_directory.");
            Run(Path.GetFullPath(args[1]), Path.GetFullPath(args[3]));
            return 0;
        }
        catch (Exception error)
        {
            Console.Error.WriteLine(JsonSerializer.Serialize(new { error_type = error.GetType().FullName, message = error.Message, stack = error.StackTrace }, JsonOptions()));
            return 1;
        }
    }

    private static void RunMeshStreamAudit(string inputPath, string source, long pathId, string outputPath)
    {
        if (string.IsNullOrWhiteSpace(source)) throw new ArgumentException("Source CAB must be nonempty.", nameof(source));
        if (pathId == 0) throw new ArgumentOutOfRangeException(nameof(pathId), "Mesh path ID must be nonzero.");
        if (File.Exists(outputPath) || Directory.Exists(outputPath)) throw new IOException($"Output already exists: {outputPath}");
        var input = JsonSerializer.Deserialize<CharacterInput>(File.ReadAllText(inputPath), JsonOptions()) ?? throw new InvalidDataException("Input manifest is null.");
        if (input.Format != "EndfieldSingleCharacterInput/1" || string.IsNullOrWhiteSpace(input.UnityVersion) || input.Bundles.Length == 0) throw new InvalidDataException("Input format, Unity version, and bundle list must be nonempty.");
        var paths = input.Bundles.Select(bundle => VerifyBundle(bundle, Path.GetDirectoryName(inputPath)!)).ToArray();
        if (paths.Distinct(StringComparer.OrdinalIgnoreCase).Count() != paths.Length) throw new InvalidDataException("Input bundle paths are duplicated.");
        if (paths.Contains(outputPath, StringComparer.OrdinalIgnoreCase)) throw new InvalidDataException("Output path must not overwrite an input bundle.");

        var log = new ReaderLogger();
        Logger.Default = log;
        Logger.Flags = LoggerEvent.Warning | LoggerEvent.Error;
        Logger.Silent = false;
        var parsedTypes = new[] { ClassIDType.AssetBundle, ClassIDType.ResourceManager, ClassIDType.GameObject, ClassIDType.Transform, ClassIDType.RectTransform, ClassIDType.Animator, ClassIDType.Avatar, ClassIDType.MeshFilter, ClassIDType.MeshRenderer, ClassIDType.SkinnedMeshRenderer, ClassIDType.Mesh };
        TypeFlags.SetTypes(parsedTypes.ToDictionary(type => type, type => (true, false)));
        var manager = new AssetsManager { Game = GameManager.GetGameByType(GameType.ArknightsEndfield), ResolveDependencies = false, SpecifyUnityVersion = input.UnityVersion, Silent = false, SkipProcess = false };
        try
        {
            manager.LoadPreparedFiles(paths);
            log.RequireNoErrors();
            var matches = manager.assetsFileList
                .Where(file => string.Equals(file.fileName, source, StringComparison.OrdinalIgnoreCase))
                .SelectMany(file => file.Objects.OfType<Mesh>())
                .Where(mesh => mesh.m_PathID == pathId)
                .ToArray();
            if (matches.Length != 1)
            {
                var loadedFiles = manager.assetsFileList.Select(file => file.fileName).Distinct(StringComparer.OrdinalIgnoreCase).OrderBy(value => value, StringComparer.OrdinalIgnoreCase).ToArray();
                throw new InvalidDataException($"Exact Mesh identity must resolve once: source={source}, path_id={pathId}, matches={matches.Length}, loaded_files={string.Join(',', loadedFiles)}.");
            }

            var audit = AuditMeshStreams(matches[0], inputPath, FileHash(inputPath), input.UnityVersion);
            var bytes = JsonSerializer.SerializeToUtf8Bytes(audit, JsonOptions());
            const int maximumOutputBytes = 10 * 1024 * 1024;
            if (bytes.Length > maximumOutputBytes) throw new InvalidDataException($"Mesh stream audit exceeds the 10 MiB output limit: bytes={bytes.Length}.");
            var outputDirectory = Path.GetDirectoryName(outputPath) ?? throw new InvalidDataException($"Output path has no parent directory: {outputPath}");
            Directory.CreateDirectory(outputDirectory);
            using (var stream = new FileStream(outputPath, FileMode.CreateNew, FileAccess.Write, FileShare.None)) stream.Write(bytes);
            Console.WriteLine(JsonSerializer.Serialize(new { status = "mesh_stream_audit_completed", output = outputPath, bytes = bytes.Length, sha256 = Hash(bytes), mesh = audit.Mesh, channels = audit.Channels.Select(channel => new { channel.UnityChannelIndex, channel.Dimension, channel.DecodedComponentCount, channel.DecodedComponentsPerVertex, channel.DecodedCountMatchesDescriptor, channel.DecodedValuesSha256 }).ToArray() }, JsonOptions()));
        }
        finally
        {
            manager.Clear();
        }
    }

    private static MeshStreamAudit AuditMeshStreams(Mesh mesh, string inputManifestPath, string inputManifestSha256, string unityVersion)
    {
        var vertexDataField = typeof(Mesh).GetField("m_VertexData", BindingFlags.Instance | BindingFlags.NonPublic) ?? throw new MissingFieldException(typeof(Mesh).FullName, "m_VertexData");
        var vertexData = vertexDataField.GetValue(mesh) as VertexData ?? throw new InvalidDataException($"Mesh has no decoded VertexData: {Ref(mesh).Source}/{mesh.m_PathID}.");
        var channels = vertexData.m_Channels ?? throw new InvalidDataException($"Mesh VertexData has no channel descriptors: {Ref(mesh).Source}/{mesh.m_PathID}.");
        var streams = vertexData.m_Streams ?? throw new InvalidDataException($"Mesh VertexData has no stream descriptors: {Ref(mesh).Source}/{mesh.m_PathID}.");
        var data = vertexData.m_DataSize ?? throw new InvalidDataException($"Mesh VertexData has no raw data buffer: {Ref(mesh).Source}/{mesh.m_PathID}.");
        if (vertexData.m_VertexCount != mesh.m_VertexCount) throw new InvalidDataException($"Mesh VertexData vertex count differs from parsed mesh: vertex_data={vertexData.m_VertexCount}, mesh={mesh.m_VertexCount}.");
        var streamDataField = typeof(Mesh).GetField("m_StreamData", BindingFlags.Instance | BindingFlags.NonPublic) ?? throw new MissingFieldException(typeof(Mesh).FullName, "m_StreamData");
        var streamData = streamDataField.GetValue(mesh) as StreamingInfo;
        var positions = mesh.m_Vertices ?? Array.Empty<float>();
        var positionComponents = InferComponentsPerVertex(positions.Length, mesh.m_VertexCount, "position");
        var channelRows = new[] { 9, 10, 11 }.Select(index => AuditVertexChannel(mesh, channels, streams, index)).ToArray();
        var channelDescriptors = channels.Select((channel, index) =>
        {
            var streamExists = channel.stream < streams.Count;
            var stream = streamExists ? streams[channel.stream] : null;
            bool? maskEnabled = stream == null ? null : (stream.channelMask & (1u << index)) != 0;
            return new VertexChannelDescriptorAudit(
                index,
                channel.stream,
                channel.offset,
                channel.format,
                channel.rawDimension,
                channel.dimension,
                stream?.channelMask,
                stream?.offset,
                stream?.stride,
                maskEnabled);
        }).ToArray();
        return new MeshStreamAudit(
            "EndfieldMeshStreamAudit/1",
            inputManifestPath,
            inputManifestSha256,
            unityVersion,
            Ref(mesh),
            mesh.m_VertexCount,
            positionComponents,
            positions.Length,
            mesh.m_Vertices != null,
            AllFinite(positions),
            positions.Length == 0 ? null : Hash(FloatBytes(positions)),
            positions,
            "Decoded arrays are vertex-major: source vertex v occupies [v * components, (v + 1) * components); source vertex identity is the original Unity mesh vertex index.",
            checked((int)vertexData.m_VertexCount),
            data.Length,
            Hash(data),
            streams.Select((stream, index) => new VertexStreamAudit(index, stream.channelMask, stream.offset, stream.stride, stream.dividerOp, stream.frequency)).ToArray(),
            new MeshStreamDataAudit(!string.IsNullOrEmpty(streamData?.path), streamData?.path ?? string.Empty, streamData?.offset ?? 0, streamData?.size ?? 0, !string.IsNullOrEmpty(streamData?.path) && streamData.size == data.Length),
            channelDescriptors,
            channelRows);
    }

    private static VertexChannelAudit AuditVertexChannel(Mesh mesh, IReadOnlyList<ChannelInfo> channels, IReadOnlyList<StreamInfo> streams, int unityChannelIndex)
    {
        var semantic = $"TEXCOORD{unityChannelIndex - 4}";
        if (unityChannelIndex >= channels.Count)
            return new VertexChannelAudit(unityChannelIndex, semantic, false, null, null, null, null, null, null, null, null, null, false, 0, null, null, null, null, null, Array.Empty<float>());

        var descriptor = channels[unityChannelIndex];
        var streamExists = descriptor.stream < streams.Count;
        if (descriptor.dimension > 0 && !streamExists)
            throw new InvalidDataException($"Mesh channel descriptor references a missing stream: mesh={mesh.assetsFile.fileName}/{mesh.m_PathID}, channel={unityChannelIndex}, stream={descriptor.stream}, stream_count={streams.Count}.");
        var stream = streamExists ? streams[descriptor.stream] : null;
        var maskEnabled = stream == null ? null : (bool?)((stream.channelMask & (1u << unityChannelIndex)) != 0);
        var decoded = mesh.GetUV(unityChannelIndex - 4) ?? Array.Empty<float>();
        var componentsPerVertex = InferComponentsPerVertex(decoded.Length, mesh.m_VertexCount, semantic);
        int? expectedComponents = descriptor.dimension > 0 && maskEnabled == true ? checked(mesh.m_VertexCount * descriptor.dimension) : null;
        bool? matchesDescriptor = expectedComponents.HasValue ? decoded.Length == expectedComponents.Value : null;
        return new VertexChannelAudit(
            unityChannelIndex,
            semantic,
            true,
            descriptor.stream,
            descriptor.offset,
            descriptor.format,
            descriptor.rawDimension,
            descriptor.dimension,
            stream?.channelMask,
            stream?.offset,
            stream?.stride,
            maskEnabled,
            decoded.Length != 0,
            decoded.Length,
            componentsPerVertex,
            expectedComponents,
            matchesDescriptor,
            AllFinite(decoded),
            decoded.Length == 0 ? null : Hash(FloatBytes(decoded)),
            decoded);
    }

    private static int? InferComponentsPerVertex(int componentCount, int vertexCount, string label)
    {
        if (componentCount == 0) return null;
        if (vertexCount <= 0 || componentCount % vertexCount != 0) throw new InvalidDataException($"Decoded {label} component count is not divisible by the source vertex count: components={componentCount}, vertices={vertexCount}.");
        return componentCount / vertexCount;
    }

    private static bool AllFinite(float[] values) => values.All(float.IsFinite);

    private static byte[] FloatBytes(float[] values)
    {
        if (!BitConverter.IsLittleEndian) throw new PlatformNotSupportedException("Mesh stream audit hashes require a little-endian host.");
        var bytes = new byte[checked(values.Length * sizeof(float))];
        Buffer.BlockCopy(values, 0, bytes, 0, bytes.Length);
        return bytes;
    }

    private static void Run(string inputPath, string outputPath)
    {
        var input = JsonSerializer.Deserialize<CharacterInput>(File.ReadAllText(inputPath), JsonOptions()) ?? throw new InvalidDataException("Input manifest is null.");
        if (input.Format != "EndfieldSingleCharacterInput/1" || string.IsNullOrWhiteSpace(input.UnityVersion) || input.Bundles.Length == 0 || string.IsNullOrWhiteSpace(input.Root.Source) || string.IsNullOrWhiteSpace(input.Root.ResourcePath)) throw new InvalidDataException("Input format, Unity version, bundle list, and root fields must be nonempty.");
        if (Directory.Exists(outputPath) || File.Exists(outputPath)) throw new IOException($"Output already exists: {outputPath}");
        var paths = input.Bundles.Select(bundle => VerifyBundle(bundle, Path.GetDirectoryName(inputPath)!)).ToArray();
        if (paths.Distinct(StringComparer.OrdinalIgnoreCase).Count() != paths.Length) throw new InvalidDataException("Input bundle paths are duplicated.");
        if (paths.Any(path => path.StartsWith(outputPath + Path.DirectorySeparatorChar, StringComparison.OrdinalIgnoreCase))) throw new InvalidDataException("Output must not contain an input bundle.");
        Directory.CreateDirectory(outputPath);
        var log = new ReaderLogger();
        Logger.Default = log;
        Logger.Flags = LoggerEvent.Warning | LoggerEvent.Error;
        Logger.Silent = false;
        var parsedTypes = new[] { ClassIDType.AssetBundle, ClassIDType.ResourceManager, ClassIDType.GameObject, ClassIDType.Transform, ClassIDType.RectTransform, ClassIDType.Animator, ClassIDType.Avatar, ClassIDType.MeshFilter, ClassIDType.MeshRenderer, ClassIDType.SkinnedMeshRenderer, ClassIDType.Mesh, ClassIDType.Material, ClassIDType.Texture2D };
        TypeFlags.SetTypes(parsedTypes.ToDictionary(type => type, type => (true, false)));
        var manager = new AssetsManager { Game = GameManager.GetGameByType(GameType.ArknightsEndfield), ResolveDependencies = false, SpecifyUnityVersion = input.UnityVersion, Silent = false, SkipProcess = false };
        try
        {
            manager.LoadPreparedFiles(paths);
            WriteJson(outputPath, "reader_logs.json", log.Snapshot());
            log.RequireNoErrors();
            var source = manager.assetsFileList.SingleOrDefault(file => string.Equals(file.fileName, input.Root.Source, StringComparison.OrdinalIgnoreCase)) ?? throw new InvalidDataException($"Expected unique serialized CAB not found: {input.Root.Source}");
            var containers = source.Objects.OfType<AssetBundle>().SelectMany(bundle => bundle.m_Container).ToArray();
            var matches = containers.Where(entry => string.Equals(entry.Key.Replace('\\', '/'), input.Root.ResourcePath.Replace('\\', '/'), StringComparison.OrdinalIgnoreCase)).ToArray();
            var root = SelectHierarchyRoot(matches.Select(entry => Require(entry.Value.asset, "root container subasset")), input.Root.ResourcePath);
            var transforms = WalkTransforms(root);
            var renderers = transforms.Select(transform => Require(transform.m_GameObject, "hierarchy GameObject")).SelectMany(Renderers).ToArray();
            if (renderers.Length == 0) throw new InvalidDataException("Selected hierarchy contains no mesh renderers.");
            var meshes = renderers.Select(RendererMesh).Distinct().ToArray();
            var materials = renderers.SelectMany(renderer => renderer.m_Materials).Where(pointer => pointer.m_PathID != 0).Select(pointer => Require(pointer, "renderer material")).Distinct().ToArray();
            var materialRows = materials.Select(AuditMaterial).ToArray();
            var sourceTextures = materials.SelectMany(material => material.m_SavedProperties.m_TexEnvs).Where(entry => entry.Value.m_Texture.m_PathID != 0).Select(entry => RequireTexture(entry.Value.m_Texture, entry.Key)).Distinct().ToArray();
            var textureCount = sourceTextures.Length;
            var sourceAudit = new SourceAudit("EndfieldCharacterSourceAudit/1", "5bb936ca1d3662fe9b956ec669ab7ebea08bfd07", input.Root, Ref(root), containers.Select(entry => new ContainerAudit(entry.Key, Pointer(entry.Value.asset))).ToArray(), transforms.Select(transform => Require(transform.m_GameObject, "hierarchy GameObject").m_Animator).Where(animator => animator != null).Distinct().Select(animator => new AnimatorAudit(Ref(animator), Pointer(animator.m_Avatar), Pointer(animator.m_Controller), animator.m_HasTransformHierarchy)).ToArray(), transforms.Select(AuditTransform).ToArray(), renderers.Select(AuditRenderer).ToArray(), meshes.Select(AuditMesh).ToArray(), materialRows);
            WriteJson(outputPath, "source_audit.json", sourceAudit);
            WriteJson(outputPath, "lod_sources.json", source.Objects.Where(value => value.type == ClassIDType.LODGroup).Select(AuditTypeTree).ToArray());
            WriteJson(outputPath, "material_sources.json", materials.Select(AuditTypeTree).ToArray());
            if (sourceAudit.Renderers.Any(renderer => renderer.PositiveUnresolvedBoneInfluences != 0)) throw new InvalidDataException("Source has positive weights referring to unresolved bone pointers; source_audit.json records counts. Static completeness is not established.");
            Directory.CreateDirectory(Path.Combine(outputPath, "source_textures"));
            var texturePngCache = new TexturePngCache();
            var sourceTextureArtifacts = sourceTextures.Select(texture => ExportSourceTexture(texture, outputPath, texturePngCache)).ToArray();
            WriteJson(outputPath, "source_texture_artifacts.json", sourceTextureArtifacts);
            var options = new ModelConverter.Options { imageFormat = ImageFormat.Png, game = manager.Game, collectAnimations = false, exportMaterials = true, materials = new HashSet<Material>(), uvs = Enumerable.Range(0, 8).ToDictionary(index => $"UV{index}", index => (true, index)), texs = new Dictionary<string, int>(), textureBytesProvider = texturePngCache.Require };
            var rendererExportNames = BuildRendererExportNames(renderers);
            var originalRendererNames = new Dictionary<GameObject, string>(ReferenceEqualityComparer.Instance);
            foreach (var gameObject in rendererExportNames.Keys) originalRendererNames.Add(gameObject, gameObject.m_Name);
            ModelConverter converted;
            try
            {
                foreach (var (gameObject, exportName) in rendererExportNames) gameObject.m_Name = exportName;
                converted = new ModelConverter(root, options, Array.Empty<AnimationClip>());
            }
            finally
            {
                foreach (var (gameObject, originalName) in originalRendererNames) gameObject.m_Name = originalName;
            }
            var rendererExportMap = BuildRendererExportMap(root, renderers, converted, rendererExportNames);
            WriteJson(outputPath, "renderer_export_map.json", rendererExportMap);
            var texturePngReuse = texturePngCache.Verify(converted.TextureList);
            WriteJson(outputPath, "texture_png_reuse_audit.json", texturePngReuse);
            var converterAudit = new ConverterAudit("EndfieldCharacterConverterAudit/1", false, false, converted.AnimationList.Count, converted.MorphList.Count, WalkFrames(converted.RootFrame).Select(frame => new ConvertedFrame(frame.Path, Vec(frame.LocalPosition), Quat(frame.LocalRotation), Vec(frame.LocalScale))).ToArray(), converted.MeshList.Select(AuditConvertedMesh).ToArray(), converted.MaterialList.Select(material => new ConvertedMaterial(material.Name, material.Textures.Select(texture => texture.Name).ToArray(), material.Textures.Select(texture => texture.Dest).ToArray())).ToArray(), converted.TextureList.Select(texture => new ConvertedTexture(texture.Name, texture.Data.LongLength, Hash(texture.Data))).ToArray(), "Upstream generic converted materials only; Endfield shader equivalence is not established.");
            WriteJson(outputPath, "converter_audit.json", converterAudit);
            if (converted.AnimationList.Count != 0 || converted.MeshList.Count != renderers.Length) throw new InvalidDataException($"Static geometry coverage mismatch: animations={converted.AnimationList.Count}, meshes={converted.MeshList.Count}/{renderers.Length}.");
            if (converterAudit.Meshes.Any(mesh => mesh.PositiveUnresolvedBoneInfluences != 0)) throw new InvalidDataException("Converted model has positive weights referring to unnamed bones; converter_audit.json records counts.");
            PortableGeometry.Write(outputPath, converted);
            WriteJson(outputPath, "reader_logs.json", log.Snapshot());
            log.RequireNoErrors();
            var complete = converted.TextureList.Count == textureCount && converted.MaterialList.Count == materials.Length;
            WriteJson(outputPath, "receipt.json", new { format = "EndfieldCharacterStaticReceipt/1", status = complete ? "static_export_completed" : "incomplete_material_coverage", complete, source_commit = sourceAudit.Commit, input_manifest = inputPath, input_sha256 = FileHash(inputPath), root = sourceAudit.ActualRoot, bundles = input.Bundles, animations_disabled = true, geometry_format = "EndfieldCharacterGeometry/1", geometry_path = "model.geometry.json", geometry_unit = "meters", source_coordinates_rescaled = false, source_materials = materials.Length, converted_materials = converted.MaterialList.Count, source_textures = textureCount, converted_textures = converted.TextureList.Count, lod_selection = "All source renderer objects retained; runtime LOD visibility is not reconstructed.", shader_parsing = "Disabled; unresolved Shader references do not establish missing source dependencies.", artifacts = Directory.GetFiles(outputPath, "*", SearchOption.AllDirectories).Select(path => new Artifact(Path.GetRelativePath(outputPath, path), new FileInfo(path).Length, FileHash(path))).ToArray() });
            if (!complete) throw new InvalidDataException($"Static export is incomplete: materials={converted.MaterialList.Count}/{materials.Length}, textures={converted.TextureList.Count}/{textureCount}. Portable geometry preserved for review; receipt.json marks incomplete.");
            Console.WriteLine(JsonSerializer.Serialize(new { status = "static_export_completed", output = outputPath, meshes = converted.MeshList.Count, textures = converted.TextureList.Count, animations = converted.AnimationList.Count }, JsonOptions()));
        }
        finally
        {
            WriteJson(outputPath, "reader_logs.json", log.Snapshot());
            manager.Clear();
        }
    }

    private static Dictionary<GameObject, string> BuildRendererExportNames(Renderer[] renderers)
    {
        var rendererGameObjects = renderers.Select(renderer => Require(renderer.m_GameObject, "renderer GameObject")).ToArray();
        var rendererCounts = new Dictionary<GameObject, int>(ReferenceEqualityComparer.Instance);
        foreach (var gameObject in rendererGameObjects)
        {
            rendererCounts[gameObject] = rendererCounts.GetValueOrDefault(gameObject) + 1;
        }
        var multiplyRenderedGameObjects = rendererCounts.Where(entry => entry.Value > 1).Select(entry => Ref(entry.Key)).ToArray();
        if (multiplyRenderedGameObjects.Length != 0)
        {
            throw new InvalidDataException($"A single GameObject has multiple renderer components, so a transform-based FBX path cannot encode renderer identity safely: {JsonSerializer.Serialize(multiplyRenderedGameObjects, JsonOptions())}.");
        }

        var boneProtectedTransforms = BoneProtectedTransforms(renderers);
        var reservedNames = rendererGameObjects.Select(gameObject => gameObject.m_Name).ToHashSet(StringComparer.Ordinal);
        var exportNames = new Dictionary<GameObject, string>(ReferenceEqualityComparer.Instance);
        var duplicateNameGroups = rendererGameObjects.Distinct<GameObject>(ReferenceEqualityComparer.Instance)
            .GroupBy(gameObject => gameObject.m_Name, StringComparer.Ordinal)
            .Where(group => group.Count() > 1);
        foreach (var group in duplicateNameGroups)
        {
            foreach (var gameObject in group)
            {
                var transform = gameObject.m_Transform ?? throw new InvalidDataException($"Renderer GameObject {Ref(gameObject).PathId} has no Transform.");
                if (boneProtectedTransforms.Contains(transform))
                {
                    throw new InvalidDataException($"Duplicate renderer GameObject name {gameObject.m_Name} belongs to a bone Transform or its ancestor; refusing to rename a rig path. GameObject={JsonSerializer.Serialize(Ref(gameObject), JsonOptions())}, path={TransformPath(transform)}.");
                }

                var identity = Ref(gameObject);
                var identityText = $"{identity.Source}\0{identity.PathId.ToString(CultureInfo.InvariantCulture)}";
                var identitySuffix = Convert.ToHexString(SHA256.HashData(Encoding.UTF8.GetBytes(identityText)))[..16].ToLowerInvariant();
                var exportName = $"{gameObject.m_Name}__endfield_{identitySuffix}";
                if (!reservedNames.Add(exportName))
                {
                    throw new InvalidDataException($"Generated renderer export name collides with another source GameObject name: {exportName}.");
                }
                exportNames.Add(gameObject, exportName);
            }
        }
        return exportNames;
    }

    private static HashSet<Transform> BoneProtectedTransforms(Renderer[] renderers)
    {
        var protectedTransforms = new HashSet<Transform>(ReferenceEqualityComparer.Instance);
        foreach (var skinnedRenderer in renderers.OfType<SkinnedMeshRenderer>())
        {
            AddBoneAndAncestors(skinnedRenderer.m_RootBone);
            foreach (var bone in skinnedRenderer.m_Bones) AddBoneAndAncestors(bone);
        }
        return protectedTransforms;

        void AddBoneAndAncestors(PPtr<Transform> pointer)
        {
            if (!pointer.TryGet(out var transform)) return;
            var current = transform;
            while (protectedTransforms.Add(current) && current.m_Father.TryGet(out var parent)) current = parent;
        }
    }

    private static RendererExportMap BuildRendererExportMap(GameObject root, Renderer[] renderers, ModelConverter converted, IReadOnlyDictionary<GameObject, string> exportNames)
    {
        if (converted.MeshList.Count != renderers.Length)
        {
            throw new InvalidDataException($"Renderer export identity count mismatch: source_renderers={renderers.Length}, converted_meshes={converted.MeshList.Count}.");
        }

        var rows = new RendererExportRow[renderers.Length];
        for (var index = 0; index < renderers.Length; index++)
        {
            var renderer = renderers[index];
            var gameObject = Require(renderer.m_GameObject, "renderer GameObject");
            var mesh = RendererMesh(renderer);
            var sourceName = gameObject.m_Name;
            var exportName = exportNames.GetValueOrDefault(gameObject, sourceName);
            var exportPath = converted.MeshList[index].Path;
            if (string.IsNullOrWhiteSpace(exportPath) || !string.Equals(exportPath[(exportPath.LastIndexOf('/') + 1)..], exportName, StringComparison.Ordinal))
            {
                throw new InvalidDataException($"Converted mesh path does not preserve its explicit renderer export name: index={index}, renderer={JsonSerializer.Serialize(Ref(renderer), JsonOptions())}, export_name={exportName}, export_path={exportPath}.");
            }
            rows[index] = new RendererExportRow(
                Identity(Ref(renderer)),
                Identity(Ref(gameObject)),
                Identity(Ref(mesh)),
                TransformPath(gameObject.m_Transform ?? throw new InvalidDataException($"Renderer GameObject {Ref(gameObject).PathId} has no Transform.")),
                sourceName,
                exportName,
                exportPath);
        }

        var duplicateExportNames = rows.GroupBy(row => row.ExportName, StringComparer.Ordinal).Where(group => group.Count() > 1).Select(group => group.Key).ToArray();
        var duplicateExportPaths = rows.GroupBy(row => row.ExportPath, StringComparer.Ordinal).Where(group => group.Count() > 1).Select(group => group.Key).ToArray();
        if (duplicateExportNames.Length != 0 || duplicateExportPaths.Length != 0)
        {
            throw new InvalidDataException($"Renderer export identity is not one-to-one: duplicate_names={JsonSerializer.Serialize(duplicateExportNames)}, duplicate_paths={JsonSerializer.Serialize(duplicateExportPaths)}.");
        }

        var rootIdentity = Identity(Ref(root));
        return new RendererExportMap("EndfieldCharacterRendererExportMap/1", "5bb936ca1d3662fe9b956ec669ab7ebea08bfd07", rootIdentity, rows);
    }

    private static ObjectIdentity Identity(SourceRef value) => new(value.Source, value.PathId.ToString(CultureInfo.InvariantCulture));

    internal static string VerifyBundle(BundleInput bundle, string basePath)
    {
        var path = Path.GetFullPath(bundle.Path, basePath);
        if (bundle.Bytes <= 0 || bundle.Sha256.Length != 64 || !bundle.Sha256.All(Uri.IsHexDigit)) throw new InvalidDataException($"Invalid bundle identity: {path}");
        if (new FileInfo(path).Length != bundle.Bytes || !string.Equals(FileHash(path), bundle.Sha256, StringComparison.OrdinalIgnoreCase)) throw new InvalidDataException($"Bundle identity mismatch: {path}");
        return path;
    }

    internal static Transform[] WalkTransforms(GameObject root)
    {
        var stack = new Stack<Transform>();
        stack.Push(root.m_Transform ?? throw new InvalidDataException("Root has no Transform."));
        var rows = new List<Transform>();
        var seen = new HashSet<Transform>();
        while (stack.TryPop(out var transform))
        {
            if (!seen.Add(transform)) throw new InvalidDataException($"Hierarchy repeats Transform {transform.m_PathID}.");
            rows.Add(transform);
            foreach (var child in transform.m_Children.AsEnumerable().Reverse()) stack.Push(Require(child, "Transform child"));
        }
        return rows.ToArray();
    }

    internal static IEnumerable<Renderer> Renderers(GameObject gameObject)
    {
        if (gameObject.m_MeshRenderer != null) yield return gameObject.m_MeshRenderer;
        if (gameObject.m_SkinnedMeshRenderer != null) yield return gameObject.m_SkinnedMeshRenderer;
    }

    private static Mesh RendererMesh(Renderer renderer) => renderer switch
    {
        SkinnedMeshRenderer skinned => Require(skinned.m_Mesh, "skinned mesh"),
        MeshRenderer => Require(Require(renderer.m_GameObject, "renderer GameObject").m_MeshFilter?.m_Mesh ?? throw new InvalidDataException("MeshRenderer has no MeshFilter."), "static mesh"),
        _ => throw new InvalidDataException($"Unsupported renderer type: {renderer.type}")
    };

    private static RendererAudit AuditRenderer(Renderer renderer)
    {
        var bones = renderer is SkinnedMeshRenderer skinned ? skinned.m_Bones.Select(Pointer).ToArray() : Array.Empty<PointerRef>();
        var mesh = RendererMesh(renderer);
        var unresolved = (mesh.m_Skin ?? new List<BoneWeights4>()).Sum(row => Enumerable.Range(0, row.weight.Length).Count(index => row.weight[index] > 0 && (row.boneIndex[index] < 0 || row.boneIndex[index] >= bones.Length || bones[row.boneIndex[index]].Resolved == null)));
        var gameObject = Require(renderer.m_GameObject, "renderer GameObject");
        return new RendererAudit(Ref(renderer), Ref(gameObject), TransformPath(gameObject.m_Transform), renderer.m_Enabled, Ref(mesh), mesh.m_SubMeshes.Count, mesh.m_BindPose?.Length ?? 0, bones.Length, bones.Count(bone => bone.Resolved != null), unresolved, bones, renderer.m_Materials.Select(Pointer).ToArray());
    }

    internal static TypeTreeAudit AuditTypeTree(AssetObject value)
    {
        var tree = value.ToType();
        if (tree == null) return new TypeTreeAudit(Ref(value), false, "Pinned reader has no dedicated parser and serialized source has no embedded TypeTree.", null);
        using var document = JsonDocument.Parse(Newtonsoft.Json.JsonConvert.SerializeObject(tree));
        return new TypeTreeAudit(Ref(value), true, "Decoded by pinned reader's embedded TypeTree API.", document.RootElement.Clone());
    }

    private static TransformAudit AuditTransform(Transform transform)
    {
        var gameObject = Require(transform.m_GameObject, "Transform GameObject");
        var tree = gameObject.ToType();
        bool? active = tree != null && tree.Contains("m_IsActive") ? Convert.ToBoolean(tree["m_IsActive"]) : null;
        return new TransformAudit(Ref(transform), Ref(gameObject), TransformPath(transform), active, active.HasValue ? "embedded_type_tree" : "Pinned GameObject parser does not expose active state; no embedded field available.", Pointer(transform.m_Father), Vec(transform.m_LocalPosition), Quat(transform.m_LocalRotation), Vec(transform.m_LocalScale));
    }

    internal static string TransformPath(Transform transform)
    {
        var parts = new List<string>();
        var seen = new HashSet<Transform>();
        var current = transform;
        while (current != null)
        {
            if (!seen.Add(current)) throw new InvalidDataException("Transform parent cycle detected.");
            parts.Add(Require(current.m_GameObject, "Transform GameObject").m_Name);
            if (current.m_Father.m_PathID == 0) break;
            current = Require(current.m_Father, "Transform parent");
        }
        parts.Reverse();
        return string.Join("/", parts);
    }

    private static ConvertedMesh AuditConvertedMesh(ImportedMesh mesh)
    {
        var bones = (mesh.BoneList ?? new List<ImportedBone>()).Select(bone => new ConvertedBone(bone.Path, Matrix(bone.Matrix))).ToArray();
        var weights = mesh.VertexList.Select(vertex => (vertex.Weights ?? Array.Empty<float>(), vertex.BoneIndices ?? Array.Empty<int>())).ToArray();
        var unresolved = weights.Sum(row => Enumerable.Range(0, row.Item1.Length).Count(index => row.Item1[index] > 0 && (row.Item2[index] < 0 || row.Item2[index] >= bones.Length || string.IsNullOrWhiteSpace(bones[row.Item2[index]].Path))));
        return new ConvertedMesh(mesh.Path, mesh.VertexList.Count, mesh.SubmeshList.Sum(submesh => submesh.FaceList.Count), unresolved, bones, AuditWeights(weights, bones.Length, mesh.VertexList.Count));
    }

    internal static MeshAudit AuditMesh(Mesh mesh) => new(Ref(mesh), mesh.m_VertexCount, mesh.m_Indices.Count, mesh.m_BoneNameHashes ?? Array.Empty<uint>(), (mesh.m_BindPose ?? Array.Empty<Matrix4x4>()).Select(Matrix).ToArray(), AuditWeights((mesh.m_Skin ?? new List<BoneWeights4>()).Select(weight => (weight.weight, weight.boneIndex)).ToArray(), mesh.m_BindPose?.Length ?? 0, mesh.m_VertexCount));

    internal static MaterialAudit AuditMaterial(Material material) => new(Ref(material), Pointer(material.m_Shader), material.m_SavedProperties.m_TexEnvs.Select(entry =>
    {
        var texture = entry.Value.m_Texture.m_PathID == 0 ? null : RequireTexture(entry.Value.m_Texture, entry.Key);
        return new TextureAudit(entry.Key, Pointer(entry.Value.m_Texture), texture?.m_Width, texture?.m_Height, texture?.m_TextureFormat.ToString(), texture?.m_ColorSpace, new[] { entry.Value.m_Scale.X, entry.Value.m_Scale.Y }, new[] { entry.Value.m_Offset.X, entry.Value.m_Offset.Y });
    }).ToArray(), material.m_SavedProperties.m_Floats.Select(entry => new FloatProperty(entry.Key, entry.Value)).ToArray(), (material.m_SavedProperties.m_Ints ?? new List<KeyValuePair<string, int>>()).Select(entry => new IntProperty(entry.Key, entry.Value)).ToArray(), material.m_SavedProperties.m_Colors.Select(entry => new ColorProperty(entry.Key, new[] { entry.Value.R, entry.Value.G, entry.Value.B, entry.Value.A })).ToArray());

    internal static SourceTextureArtifact ExportSourceTexture(Texture2D texture, string outputPath) => ExportSourceTextureCore(texture, outputPath, null);

    private static SourceTextureArtifact ExportSourceTexture(Texture2D texture, string outputPath, TexturePngCache cache) => ExportSourceTextureCore(texture, outputPath, cache);

    private static SourceTextureArtifact ExportSourceTextureCore(Texture2D texture, string outputPath, TexturePngCache? cache)
    {
        var sourceHash = Hash(Encoding.UTF8.GetBytes(texture.assetsFile.fileName));
        var relative = Path.Combine("source_textures", $"{sourceHash}_{texture.m_PathID:X16}.png");
        var path = Path.Combine(outputPath, relative);
        using var pixels = texture.ConvertToStream(ImageFormat.Png, true) ?? throw new InvalidDataException($"Texture conversion returned no PNG: {texture.assetsFile.fileName}/{texture.m_PathID}.");
        pixels.Position = 0;
        using var buffer = new MemoryStream();
        pixels.CopyTo(buffer);
        var pngBytes = buffer.ToArray();
        if (pngBytes.Length == 0) throw new InvalidDataException($"Texture conversion returned an empty PNG: {texture.assetsFile.fileName}/{texture.m_PathID}.");
        var pngHash = Hash(pngBytes);
        using (var file = new FileStream(path, FileMode.CreateNew, FileAccess.Write, FileShare.None)) file.Write(pngBytes);
        var fileHash = FileHash(path);
        if (!string.Equals(fileHash, pngHash, StringComparison.OrdinalIgnoreCase)) throw new InvalidDataException($"Exported source PNG changed while writing: {texture.assetsFile.fileName}/{texture.m_PathID}.");
        cache?.Add(texture, ImageFormat.Png, true, pngBytes, fileHash);
        return new SourceTextureArtifact(Ref(texture), texture.m_Width, texture.m_Height, texture.m_TextureFormat.ToString(), texture.m_ColorSpace, true, new Artifact(relative, new FileInfo(path).Length, fileHash));
    }

    private sealed class TexturePngCache
    {
        private sealed record Entry(SourceRef Source, byte[] Bytes, string Sha256);
        private sealed record Use(SourceRef Source, string ConverterTextureName, ImageFormat ImageFormat, bool VerticalFlip, string SourcePngSha256);

        private readonly Dictionary<Texture2D, Dictionary<(ImageFormat ImageFormat, bool VerticalFlip), Entry>> entries = new(ReferenceEqualityComparer.Instance);
        private readonly Dictionary<Texture2D, HashSet<(ImageFormat ImageFormat, bool VerticalFlip)>> usedKeys = new(ReferenceEqualityComparer.Instance);
        private readonly Dictionary<string, Use> usesByConverterName = new(StringComparer.Ordinal);

        public void Add(Texture2D texture, ImageFormat imageFormat, bool verticalFlip, byte[] pngBytes, string sha256)
        {
            if (pngBytes.Length == 0) throw new InvalidDataException($"Cannot cache an empty PNG: {texture.assetsFile.fileName}/{texture.m_PathID}.");
            if (sha256.Length != 64 || !sha256.All(Uri.IsHexDigit)) throw new InvalidDataException($"Invalid cached PNG SHA-256: {texture.assetsFile.fileName}/{texture.m_PathID}.");
            if (!entries.TryGetValue(texture, out var variants))
            {
                variants = new Dictionary<(ImageFormat ImageFormat, bool VerticalFlip), Entry>();
                entries.Add(texture, variants);
            }

            var key = (imageFormat, verticalFlip);
            if (!variants.TryAdd(key, new Entry(Ref(texture), pngBytes, sha256)))
                throw new InvalidDataException($"Duplicate PNG conversion cache key: {texture.assetsFile.fileName}/{texture.m_PathID}, format={imageFormat}, vertical_flip={verticalFlip}.");
        }

        public byte[] Require(Texture2D texture, string converterTextureName, ImageFormat imageFormat, bool verticalFlip)
        {
            var identity = $"{texture.assetsFile.fileName}/{texture.m_PathID}";
            if (!entries.TryGetValue(texture, out var variants) || !variants.TryGetValue((imageFormat, verticalFlip), out var entry))
                throw new InvalidDataException($"No source PNG cache entry for exact Texture2D object and conversion parameters: {identity}, format={imageFormat}, vertical_flip={verticalFlip}.");
            if (string.IsNullOrWhiteSpace(converterTextureName)) throw new InvalidDataException($"Converter supplied an empty output texture name for {identity}.");
            if (!usesByConverterName.TryAdd(converterTextureName, new Use(entry.Source, converterTextureName, imageFormat, verticalFlip, entry.Sha256)))
                throw new InvalidDataException($"Converter reused a cached PNG name for distinct conversion requests: {converterTextureName}.");
            if (!usedKeys.TryGetValue(texture, out var used))
            {
                used = new HashSet<(ImageFormat ImageFormat, bool VerticalFlip)>();
                usedKeys.Add(texture, used);
            }
            if (!used.Add((imageFormat, verticalFlip))) throw new InvalidDataException($"Exact Texture2D PNG cache key was consumed more than once: {identity}, format={imageFormat}, vertical_flip={verticalFlip}.");
            return entry.Bytes;
        }

        public TexturePngReuseAudit Verify(IReadOnlyCollection<ImportedTexture> convertedTextures)
        {
            var convertedByName = new Dictionary<string, ImportedTexture>(StringComparer.Ordinal);
            foreach (var texture in convertedTextures)
            {
                if (!convertedByName.TryAdd(texture.Name, texture)) throw new InvalidDataException($"Converted texture name is not unique: {texture.Name}.");
            }
            if (convertedByName.Count != usesByConverterName.Count)
                throw new InvalidDataException($"Cached source PNG conversion count differs from converter textures: cache_uses={usesByConverterName.Count}, converter_textures={convertedByName.Count}.");

            var rows = new List<TexturePngReuseRow>(usesByConverterName.Count);
            foreach (var use in usesByConverterName.Values.OrderBy(value => value.ConverterTextureName, StringComparer.Ordinal))
            {
                if (!convertedByName.TryGetValue(use.ConverterTextureName, out var converted))
                    throw new InvalidDataException($"Converted texture was not found for cached source PNG: {use.ConverterTextureName}.");
                var converterHash = Hash(converted.Data);
                if (!string.Equals(use.SourcePngSha256, converterHash, StringComparison.OrdinalIgnoreCase))
                    throw new InvalidDataException($"Source PNG and converter texture bytes differ: texture={use.Source.Source}/{use.Source.PathId}, name={use.ConverterTextureName}, source_sha256={use.SourcePngSha256}, converter_sha256={converterHash}.");
                rows.Add(new TexturePngReuseRow(use.Source, use.ConverterTextureName, use.ImageFormat.ToString(), use.VerticalFlip, use.SourcePngSha256, converterHash));
            }

            var notConsumed = entries.SelectMany(pair => pair.Value.Where(variant =>
                !usedKeys.TryGetValue(pair.Key, out var used) || !used.Contains(variant.Key)).Select(variant => variant.Value.Source)).ToArray();
            var sourcePngCount = entries.Sum(pair => pair.Value.Count);
            return new TexturePngReuseAudit("EndfieldCharacterTexturePngReuseAudit/1", "Loaded Texture2D reference equality plus image format and vertical-flip flag; CAB/pathID retained for audit.", sourcePngCount, convertedByName.Count, rows.Count, notConsumed.Length, rows.ToArray(), notConsumed);
        }
    }

    internal static Texture2D RequireTexture(PPtr<Texture> pointer, string slot) => pointer.TryGet<Texture2D>(out var texture) ? texture : throw new InvalidDataException($"Texture slot {slot} is unresolved or unsupported; file_id={pointer.m_FileID}, path_id={pointer.m_PathID}.");
    internal static T Require<T>(PPtr<T> pointer, string label) where T : AssetObject => pointer.TryGet(out var value) ? value : throw new InvalidDataException($"Unresolved {label}; file_id={pointer.m_FileID}, path_id={pointer.m_PathID}.");
    internal static PointerRef Pointer<T>(PPtr<T> pointer) where T : AssetObject => new(pointer.m_FileID, pointer.m_PathID, pointer.m_PathID != 0 && pointer.TryGet<AssetObject>(out var value) ? Ref(value) : null);
    internal static SourceRef Ref(AssetObject value) => new(value.assetsFile.fileName, value.m_PathID, value.type.ToString(), value.Name);
    private static float[] Vec(Vector3 value) => new[] { value.X, value.Y, value.Z };
    private static float[] Quat(Quaternion value) => new[] { value.X, value.Y, value.Z, value.W };
    private static float[] Matrix(Matrix4x4 value) => Enumerable.Range(0, 16).Select(index => value[index]).ToArray();

    private static SkinAudit AuditWeights((float[] Weights, int[] Indices)[] rows, int boneCount, int vertices)
    {
        const double tolerance = 0.0001;
        var sums = new List<double>();
        var influences = 0;
        foreach (var row in rows)
        {
            if (row.Weights.Length != row.Indices.Length) throw new InvalidDataException("Skin weight/index array lengths differ.");
            double sum = 0;
            for (var index = 0; index < row.Weights.Length; index++)
            {
                var weight = row.Weights[index];
                if (!float.IsFinite(weight) || weight < 0) throw new InvalidDataException("Skin has nonfinite or negative weight.");
                if (weight > 0)
                {
                    if (row.Indices[index] < 0 || row.Indices[index] >= boneCount) throw new InvalidDataException($"Positive weight has invalid bone index {row.Indices[index]}/{boneCount}.");
                    influences++;
                }
                sum += weight;
            }
            sums.Add(sum);
        }
        return new SkinAudit(vertices, sums.Count(sum => sum > 0), influences, sums.Count(sum => sum == 0), sums.Count(sum => sum > 0 && Math.Abs(sum - 1) > tolerance), sums.Count == 0 ? 0 : sums.Min(), sums.Count == 0 ? 0 : sums.Max(), tolerance);
    }

    private static ImportedFrame[] WalkFrames(ImportedFrame root)
    {
        var stack = new Stack<ImportedFrame>();
        stack.Push(root);
        var rows = new List<ImportedFrame>();
        while (stack.TryPop(out var frame))
        {
            rows.Add(frame);
            for (var index = frame.Count - 1; index >= 0; index--) stack.Push(frame[index]);
        }
        return rows.ToArray();
    }

    private static string Hash(byte[] bytes) => Convert.ToHexString(SHA256.HashData(bytes));
    private static string FileHash(string path) { using var stream = File.OpenRead(path); return Convert.ToHexString(SHA256.HashData(stream)); }
    private static void WriteJson<T>(string output, string name, T value) => File.WriteAllText(Path.Combine(output, name), JsonSerializer.Serialize(value, JsonOptions()));
}
