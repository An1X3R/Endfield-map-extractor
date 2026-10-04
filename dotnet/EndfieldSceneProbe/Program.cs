using System.Globalization;
using System.Reflection;
using System.Runtime.ExceptionServices;
using System.Text;
using System.Text.Json;
using System.Text.Json.Serialization;
using AnimeStudio;
using Object = AnimeStudio.Object;

internal static partial class Program
{
    private static readonly MethodInfo ReadAssetsMethod = typeof(AssetsManager).GetMethod(
        "ReadAssets", BindingFlags.Instance | BindingFlags.NonPublic)
        ?? throw new MissingMethodException("AssetsManager.ReadAssets()");
    private static readonly MethodInfo ProcessAssetsMethod = typeof(AssetsManager).GetMethod(
        "ProcessAssets", BindingFlags.Instance | BindingFlags.NonPublic)
        ?? throw new MissingMethodException("AssetsManager.ProcessAssets()");
    private static readonly JsonSerializerOptions JsonOptions = new()
    {
        WriteIndented = true,
        DefaultIgnoreCondition = JsonIgnoreCondition.WhenWritingNull,
    };

    private static int Main(string[] args)
    {
        Console.OutputEncoding = Encoding.UTF8;
        try
        {
            if (args.Length == 1 && args[0] == "--initialize-runtime")
            {
                HelperRuntime.Initialize();
                return 0;
            }
            var options = Options.Parse(args);
            Run(options);
            return 0;
        }
        catch (Exception ex)
        {
            Console.Error.WriteLine($"Scene probe stopped: {ex.Message}");
            return 1;
        }
    }

    private static void Run(Options options)
    {
        var telemetry = new HelperProgress("EndfieldSceneProbe", options.PerformanceReport,
            "load_unpack includes input-file reading and bundle unpacking because AssetsManager.LoadFiles " +
            "does not expose separate timings; object_parse covers ReadAssets and ProcessAssets in original order.");
        try
        {
            RunCore(options, telemetry);
        }
        catch (Exception error)
        {
            try
            {
                telemetry.Finish("failed");
            }
            catch (Exception reportError)
            {
                error.Data["PerformanceReportError"] = reportError.ToString();
            }
            throw;
        }
        telemetry.Finish("complete");
    }

    private static void RunCore(Options options, HelperProgress telemetry)
    {
        var input = Path.GetFullPath(options.Input);
        var output = Path.GetFullPath(options.Output);
        if (options.PerformanceReport is not null && Path.GetFullPath(options.PerformanceReport)
            .Equals(Path.Combine(output, "scene_manifest.json"), StringComparison.OrdinalIgnoreCase))
            throw new IOException("Performance report cannot overwrite the scene manifest");
        if (!File.Exists(input) && !Directory.Exists(input))
            throw new FileNotFoundException("Input does not exist.", input);
        if (Directory.Exists(output) || File.Exists(output))
            throw new IOException($"Refusing to overwrite existing output: {output}");

        var files = File.Exists(input)
            ? new[] { input }
            : Directory.GetFiles(input, "*", SearchOption.AllDirectories)
                .OrderBy(x => x, StringComparer.OrdinalIgnoreCase).ToArray();
        if (files.Length == 0)
            throw new IOException("Input contains no files.");

        Directory.CreateDirectory(output);
        var meshDir = Path.Combine(output, "meshes");
        var textureDir = Path.Combine(output, "textures");
        Directory.CreateDirectory(meshDir);
        Directory.CreateDirectory(textureDir);

        // Keep the parser narrow: unknown/custom components remain raw Objects, while
        // the scene, render and material chain is parsed into typed objects.
        TypeFlags.SetTypes(new Dictionary<ClassIDType, (bool, bool)>());
        foreach (var type in new[]
        {
            ClassIDType.AssetBundle, ClassIDType.ResourceManager,
            ClassIDType.GameObject, ClassIDType.Transform, ClassIDType.RectTransform,
            ClassIDType.MeshFilter, ClassIDType.MeshRenderer, ClassIDType.SkinnedMeshRenderer,
            ClassIDType.Mesh, ClassIDType.Material, ClassIDType.Texture2D,
        })
            TypeFlags.SetType(type, true, false);

        Logger.Default = new ConsoleLogger();
        Logger.Flags = LoggerEvent.Error | LoggerEvent.Warning | LoggerEvent.Info;
        var manager = new AssetsManager
        {
            Game = GameManager.GetGameByType(GameType.ArknightsEndfield),
            ResolveDependencies = false,
            Silent = false,
            SkipProcess = true,
        };
        telemetry.Publish("load_unpack", 0, files.Length);
        manager.LoadFiles(files);
        telemetry.Publish("load_unpack", files.Length, files.Length);
        var objectCount = manager.assetsFileList.Sum(file => file.m_Objects.Count);
        telemetry.Publish("object_parse", 0, objectCount);
        InvokeManagerPhase(ReadAssetsMethod, manager);
        InvokeManagerPhase(ProcessAssetsMethod, manager);
        telemetry.Publish("object_parse", objectCount, objectCount);

        var allObjects = manager.assetsFileList.SelectMany(f => f.Objects).ToList();
        var gameObjects = allObjects.OfType<GameObject>().ToList();
        var allMeshes = allObjects.OfType<Mesh>().ToList();
        var allMaterials = allObjects.OfType<Material>().ToList();
        var allTextures = allObjects.OfType<Texture2D>().ToList();
        var particleExtraction = ReadParticleRecords(allObjects, gameObjects);
        var shapeMeshes = SelectShapeMeshes(particleExtraction.Records, allMeshes);
        var waterRecords = ReadWaterRecords(allObjects);
        var shaderReferences = allObjects.Where(obj => obj.type == ClassIDType.Shader)
            .ToDictionary(obj => (obj.assetsFile.fileName, obj.m_PathID), ReadShaderReference);

        var meshRefs = new HashSet<Mesh>();
        var materialRefs = new HashSet<Material>();
        var textureRefs = new HashSet<Texture2D>();
        meshRefs.UnionWith(particleExtraction.Meshes);
        materialRefs.UnionWith(particleExtraction.Materials);
        textureRefs.UnionWith(particleExtraction.Textures);
        var nodes = new List<NodeRecord>();

        foreach (var go in gameObjects)
        {
            Mesh? mesh = null;
            Renderer? renderer = go.m_SkinnedMeshRenderer ?? (Renderer?)go.m_MeshRenderer;
            if (go.m_SkinnedMeshRenderer?.m_Mesh.TryGet(out var skinMesh) == true)
                mesh = skinMesh;
            else if (go.m_MeshFilter?.m_Mesh.TryGet(out var staticMesh) == true)
                mesh = staticMesh;
            if (mesh != null)
                meshRefs.Add(mesh);

            var materials = new List<AssetRef>();
            if (renderer != null)
            {
                foreach (var pointer in renderer.m_Materials)
                {
                    materials.Add(ToRef(pointer, renderer.assetsFile));
                    if (pointer.TryGet(out var material))
                        materialRefs.Add(material);
                }
            }

            var transform = go.m_Transform;
            nodes.Add(new NodeRecord
            {
                Name = go.m_Name,
                Source = go.assetsFile.fileName,
                PathId = go.m_PathID,
                GameObjectActive = SceneNodeBoolean(go, "m_IsActive"),
                IsRoot = transform == null || transform.m_Father.IsNull,
                TransformPathId = transform?.m_PathID,
                Parent = transform == null ? null : ToRef(transform.m_Father, transform.assetsFile),
                Children = transform?.m_Children.Select(p => ToRef(p, transform.assetsFile)).ToList(),
                LocalPosition = transform == null ? null : new[] { transform.m_LocalPosition.X, transform.m_LocalPosition.Y, transform.m_LocalPosition.Z },
                LocalRotation = transform == null ? null : new[] { transform.m_LocalRotation.X, transform.m_LocalRotation.Y, transform.m_LocalRotation.Z, transform.m_LocalRotation.W },
                LocalScale = transform == null ? null : new[] { transform.m_LocalScale.X, transform.m_LocalScale.Y, transform.m_LocalScale.Z },
                RendererType = renderer?.type.ToString(),
                RendererAsset = renderer == null ? null : ToRef(renderer),
                RendererEnabled = renderer == null ? null : SceneNodeBoolean(renderer, "m_Enabled"),
                Mesh = mesh == null ? null : ToRef(mesh),
                Materials = materials.Count == 0 ? null : materials,
            });
        }

        var projectors = new List<DecalProjectorRecord>();
        foreach (var obj in allObjects.Where(obj => obj.type == ClassIDType.HGDecalProjector))
        {
            var (projector, material) = ReadDecalProjector(obj);
            projectors.Add(projector);
            materialRefs.Add(material);
        }

        if (options.IncludeUnreferenced)
        {
            meshRefs.UnionWith(allMeshes);
            materialRefs.UnionWith(allMaterials);
            textureRefs.UnionWith(allTextures);
        }

        var materialRecords = new List<MaterialRecord>();
        foreach (var material in materialRefs.OrderBy(x => x.assetsFile.fileName).ThenBy(x => x.m_PathID))
        {
            var texEnvs = new List<TextureSlotRecord>();
            foreach (var pair in material.m_SavedProperties.m_TexEnvs)
            {
                Texture2D? texture = null;
                pair.Value.m_Texture.TryGet<Texture2D>(out texture);
                if (texture != null)
                    textureRefs.Add(texture);
                texEnvs.Add(new TextureSlotRecord
                {
                    Property = pair.Key,
                    Texture = ToRef(pair.Value.m_Texture, material.assetsFile),
                    Scale = new[] { pair.Value.m_Scale.X, pair.Value.m_Scale.Y },
                    Offset = new[] { pair.Value.m_Offset.X, pair.Value.m_Offset.Y },
                });
            }

            var shaderReference = ToRef(material.m_Shader, material.assetsFile);
            materialRecords.Add(new MaterialRecord
            {
                Name = material.m_Name,
                Source = material.assetsFile.fileName,
                PathId = material.m_PathID,
                Shader = shaderReferences.GetValueOrDefault(
                    (shaderReference.Source, shaderReference.PathId), shaderReference),
                RenderState = ReadMaterialRenderState(material),
                Textures = texEnvs,
                Ints = material.m_SavedProperties.m_Ints?.ToDictionary(x => x.Key, x => x.Value),
                Floats = material.m_SavedProperties.m_Floats.ToDictionary(x => x.Key, x => x.Value),
                Colors = material.m_SavedProperties.m_Colors.ToDictionary(
                    x => x.Key, x => new[] { x.Value.R, x.Value.G, x.Value.B, x.Value.A }),
            });
        }

        var meshRecords = new List<ExportRecord>();
        var meshTotal = options.ManifestOnly ? 0 : meshRefs.Count;
        telemetry.Publish("mesh_export", 0, meshTotal);
        foreach (var mesh in options.ManifestOnly
                     ? Enumerable.Empty<Mesh>()
                     : meshRefs.OrderBy(x => x.assetsFile.fileName).ThenBy(x => x.m_PathID))
        {
            var fileName = UniqueFileName(mesh, ".obj");
            var path = Path.Combine(meshDir, fileName);
            var ok = WriteObj(mesh, path);
            string? shapeGeometryFile = null;
            string? shapeGeometryError = null;
            if (shapeMeshes.Contains(mesh))
            {
                try
                {
                    shapeGeometryFile = WriteShapeMeshGeometry(mesh, meshDir, output);
                }
                catch (InvalidDataException error)
                {
                    shapeGeometryError = error.Message;
                }
            }
            meshRecords.Add(new ExportRecord(ToRef(mesh), ok ? Path.GetRelativePath(output, path) : null,
                ok ? null : "Mesh has no exportable triangle/vertex data")
            {
                VertexCount = mesh.m_VertexCount,
                VertexColorComponents = MeshVertexColorComponents(mesh),
                VertexColorsFile = ok ? WriteMeshVertexColors(mesh, meshDir, output) : null,
                Uv0Components = MeshUvComponents(mesh, mesh.m_UV0, "uv0"),
                Uv1Components = MeshUvComponents(mesh, mesh.m_UV1, "uv1"),
                Uv0File = ok ? WriteMeshUv(mesh, mesh.m_UV0, "uv0", meshDir, output) : null,
                Uv1File = ok ? WriteMeshUv(mesh, mesh.m_UV1, "uv1", meshDir, output) : null,
                ShapeGeometryFile = shapeGeometryFile,
                ShapeGeometryError = shapeGeometryError,
            });
            if (meshRecords.Count % 100 == 0)
                telemetry.Publish("mesh_export", meshRecords.Count, meshTotal);
        }
        telemetry.Publish("mesh_export", meshRecords.Count, meshTotal);

        var textureRecords = new List<ExportRecord>();
        var textureTotal = options.ManifestOnly ? 0 : textureRefs.Count;
        telemetry.Publish("texture_export", 0, textureTotal);
        foreach (var texture in options.ManifestOnly
                     ? Enumerable.Empty<Texture2D>()
                     : textureRefs.OrderBy(x => x.assetsFile.fileName).ThenBy(x => x.m_PathID))
        {
            var fileName = UniqueFileName(texture, ".png");
            var path = Path.Combine(textureDir, fileName);
            string? error = null;
            try
            {
                using var stream = texture.ConvertToStream(ImageFormat.Png, true);
                if (stream == null)
                    error = "Texture decoder returned no image";
                else
                {
                    stream.Position = 0;
                    using var file = File.Create(path);
                    stream.CopyTo(file);
                }
            }
            catch (Exception ex)
            {
                error = ex.Message;
            }
            textureRecords.Add(new ExportRecord(TextureReference(texture), error == null ? Path.GetRelativePath(output, path) : null, error));
            if (textureRecords.Count % 100 == 0)
                telemetry.Publish("texture_export", textureRecords.Count, textureTotal);
        }
        telemetry.Publish("texture_export", textureRecords.Count, textureTotal);

        var waterArrayKeys = waterRecords.GlobalConfigs
            .Where(record => !record.NormalMapArray.IsNull)
            .Select(record => (record.NormalMapArray.Target.Source, record.NormalMapArray.Target.PathId))
            .ToHashSet();
        var arrayObjects = allObjects.Where(obj => obj.type == ClassIDType.Texture2DArray &&
            (options.IncludeUnreferenced || waterArrayKeys.Contains((obj.assetsFile.fileName, obj.m_PathID))))
            .OrderBy(obj => obj.assetsFile.fileName).ThenBy(obj => obj.m_PathID).ToArray();
        var arrayRecords = new List<TextureArrayExportBatch>();
        if (!options.ManifestOnly)
        {
            telemetry.Publish("texture_array_export", 0, arrayObjects.Length);
            foreach (var obj in arrayObjects)
            {
                arrayRecords.Add(TextureArrayExports.Export(obj, output, textureDir));
                telemetry.Publish("texture_array_export", arrayRecords.Count, arrayObjects.Length);
            }
        }

        var containers = BuildContainers(allObjects);
        var report = new SceneReport
        {
            Format = "EndfieldSceneProbe/1",
            InputFiles = files.Select(Path.GetFullPath).ToArray(),
            SerializedFiles = manager.assetsFileList.Select(f => new SerializedFileRecord
            {
                Name = f.fileName,
                OriginalPath = f.originalPath,
                UnityVersion = f.unityVersion,
                ExternalFiles = f.m_Externals.Select(e => e.fileName).ToArray(),
                ObjectCount = f.Objects.Count,
            }).ToList(),
            Nodes = nodes,
            Particles = particleExtraction.Records,
            DecalProjectors = projectors,
            Water = waterRecords,
            Materials = materialRecords,
            MeshExports = meshRecords,
            TextureExports = textureRecords,
            TextureArrayExports = arrayRecords,
            MetadataExports = options.DumpMetadata ? ExportSerializedMetadata(allObjects, output) : [],
            Containers = containers,
            Summary = new Dictionary<string, int>
            {
                ["serializedFiles"] = manager.assetsFileList.Count,
                ["gameObjects"] = gameObjects.Count,
                ["roots"] = nodes.Count(n => n.IsRoot),
                ["allMeshes"] = allMeshes.Count,
                ["selectedMeshes"] = meshRefs.Count,
                ["selectedShapeMeshes"] = shapeMeshes.Count,
                ["shapeMeshGeometries"] = meshRecords.Count(record => record.ShapeGeometryFile is not null),
                ["shapeMeshGeometryErrors"] = meshRecords.Count(record => record.ShapeGeometryError is not null),
                ["allMaterials"] = allMaterials.Count,
                ["selectedMaterials"] = materialRefs.Count,
                ["allTextures"] = allTextures.Count,
                ["selectedTextures"] = textureRefs.Count,
                ["containers"] = containers.Count,
                ["decalProjectors"] = projectors.Count,
                ["particleSystems"] = particleExtraction.Records.Systems.Length,
                ["particleRenderers"] = particleExtraction.Records.Renderers.Length,
                ["particlePairs"] = particleExtraction.Records.Pairs.Length,
                ["particleDiagnostics"] = particleExtraction.Records.Diagnostics.Length,
                ["waterConfigs"] = waterRecords.Configs.Length,
                ["waterGlobals"] = waterRecords.GlobalConfigs.Length,
                ["waterSectors"] = waterRecords.Sectors.Length,
                ["textureArrays"] = arrayRecords.Count,
            },
        };

        var reportPath = Path.Combine(output, "scene_manifest.json");
        telemetry.Publish("manifest_write", 0, 1);
        File.WriteAllText(reportPath, JsonSerializer.Serialize(report, JsonOptions), new UTF8Encoding(false));
        telemetry.Publish("manifest_write", 1, 1);
        Console.WriteLine($"Scene manifest: {reportPath}");
        Console.WriteLine(string.Join(", ", report.Summary.Select(x => $"{x.Key}={x.Value}")));
    }

    private static void InvokeManagerPhase(MethodInfo method, AssetsManager manager)
    {
        try
        {
            method.Invoke(manager, null);
        }
        catch (TargetInvocationException error) when (error.InnerException is not null)
        {
            ExceptionDispatchInfo.Capture(error.InnerException).Throw();
            throw;
        }
    }

    private static List<ContainerRecord> BuildContainers(IEnumerable<Object> objects)
    {
        var result = new List<ContainerRecord>();
        foreach (var bundle in objects.OfType<AssetBundle>())
        {
            foreach (var entry in bundle.m_Container)
                result.Add(new ContainerRecord(entry.Key, ToRef(entry.Value.asset, bundle.assetsFile), bundle.assetsFile.fileName));
        }
        foreach (var resource in objects.OfType<ResourceManager>())
        {
            foreach (var entry in resource.m_Container)
                result.Add(new ContainerRecord(entry.Key, ToRef(entry.Value, resource.assetsFile), resource.assetsFile.fileName));
        }
        return result;
    }

    private static AssetRef ToRef(Object obj) => new(obj.assetsFile.fileName, obj.m_PathID, obj.type.ToString(), obj.Name);

    private static AssetRef TextureReference(Texture2D texture)
    {
        var fields = texture.ToType() ?? throw new InvalidDataException(
            $"Texture2D serialized type tree unavailable: source={texture.assetsFile.fileName}, pathId={texture.m_PathID}");
        if (fields["m_ColorSpace"] is not int colorSpace || colorSpace is not (0 or 1))
            throw new InvalidDataException($"Texture2D requires serialized m_ColorSpace 0/1: source={texture.assetsFile.fileName}, pathId={texture.m_PathID}");
        return ToRef(texture) with
        {
            ColorSpace = colorSpace,
            TextureSampler = ReadTextureSampler(texture, fields),
        };
    }

    private static AssetRef ToRef<T>(PPtr<T> pointer, SerializedFile owner) where T : Object
    {
        if (pointer.TryGet(out var obj))
            return ToRef(obj);
        var source = pointer.m_FileID == 0
            ? owner.fileName
            : pointer.m_FileID > 0 && pointer.m_FileID <= owner.m_Externals.Count
                ? owner.m_Externals[pointer.m_FileID - 1].fileName
                : $"<fileID:{pointer.m_FileID}>";
        return new AssetRef(source, pointer.m_PathID, typeof(T).Name, null);
    }

    private static string UniqueFileName(Object obj, string extension)
    {
        var source = Path.GetFileNameWithoutExtension(obj.assetsFile.originalPath ?? obj.assetsFile.fileName);
        return $"{Sanitize(source)}_{obj.m_PathID}_{Sanitize(obj.Name)}{extension}";
    }

    private static string Sanitize(string? value)
    {
        if (string.IsNullOrWhiteSpace(value)) return "unnamed";
        var invalid = Path.GetInvalidFileNameChars().ToHashSet();
        var chars = value.Select(c => invalid.Contains(c) || char.IsControl(c) ? '_' : c).ToArray();
        var result = new string(chars).Trim().TrimEnd('.');
        return result.Length > 100 ? result[..100] : result;
    }

    private static bool WriteObj(Mesh mesh, string path)
    {
        if (mesh.m_VertexCount <= 0 || mesh.m_Vertices is not { Length: > 0 } || mesh.m_Indices.Count < 3)
            return false;
        var vertexStride = mesh.m_Vertices.Length / mesh.m_VertexCount;
        if (vertexStride < 3) return false;
        var uvStride = mesh.m_UV0 is { Length: > 0 } ? mesh.m_UV0.Length / mesh.m_VertexCount : 0;
        var normalStride = mesh.m_Normals is { Length: > 0 } ? mesh.m_Normals.Length / mesh.m_VertexCount : 0;

        using var writer = new StreamWriter(path, false, new UTF8Encoding(false));
        writer.WriteLine($"g {Sanitize(mesh.m_Name)}");
        for (var v = 0; v < mesh.m_VertexCount; v++)
            writer.WriteLine(FormattableString.Invariant($"v {-mesh.m_Vertices[v * vertexStride]} {mesh.m_Vertices[v * vertexStride + 1]} {mesh.m_Vertices[v * vertexStride + 2]}"));
        if (uvStride >= 2)
            for (var v = 0; v < mesh.m_VertexCount; v++)
                writer.WriteLine(FormattableString.Invariant($"vt {mesh.m_UV0![v * uvStride]} {mesh.m_UV0[v * uvStride + 1]}"));
        if (normalStride >= 3)
            for (var v = 0; v < mesh.m_VertexCount; v++)
                writer.WriteLine(FormattableString.Invariant($"vn {-mesh.m_Normals![v * normalStride]} {mesh.m_Normals[v * normalStride + 1]} {mesh.m_Normals[v * normalStride + 2]}"));

        var firstIndex = 0;
        for (var subMesh = 0; subMesh < mesh.m_SubMeshes.Count; subMesh++)
        {
            writer.WriteLine($"g {Sanitize(mesh.m_Name)}_{subMesh}");
            var count = Math.Min((int)mesh.m_SubMeshes[subMesh].indexCount, mesh.m_Indices.Count - firstIndex);
            for (var i = 0; i + 2 < count; i += 3)
            {
                var a = mesh.m_Indices[firstIndex + i + 2] + 1;
                var b = mesh.m_Indices[firstIndex + i + 1] + 1;
                var c = mesh.m_Indices[firstIndex + i] + 1;
                if (uvStride >= 2 && normalStride >= 3)
                    writer.WriteLine($"f {a}/{a}/{a} {b}/{b}/{b} {c}/{c}/{c}");
                else if (uvStride >= 2)
                    writer.WriteLine($"f {a}/{a} {b}/{b} {c}/{c}");
                else if (normalStride >= 3)
                    writer.WriteLine($"f {a}//{a} {b}//{b} {c}//{c}");
                else
                    writer.WriteLine($"f {a} {b} {c}");
            }
            firstIndex += count;
        }
        return true;
    }

    private sealed record Options(string Input, string Output, bool IncludeUnreferenced, bool ManifestOnly,
        bool DumpMetadata, string? PerformanceReport)
    {
        public static Options Parse(string[] args)
        {
            if (args.Length < 2)
                throw new ArgumentException("Usage: EndfieldSceneProbe <input-file-or-folder> <new-output-folder> [--include-unreferenced] [--manifest-only] [--dump-metadata] [--performance-report PATH]");
            string? report = null;
            var includeUnreferenced = false;
            var manifestOnly = false;
            var dumpMetadata = false;
            for (var index = 2; index < args.Length; index++)
            {
                switch (args[index])
                {
                    case "--include-unreferenced": includeUnreferenced = true; break;
                    case "--manifest-only": manifestOnly = true; break;
                    case "--dump-metadata": dumpMetadata = true; break;
                    case "--performance-report" when index + 1 < args.Length: report = args[++index]; break;
                    default: throw new ArgumentException($"Unknown SceneProbe option: {args[index]}");
                }
            }
            return new Options(args[0], args[1], includeUnreferenced, manifestOnly, dumpMetadata, report);
        }
    }
}

internal sealed class SceneReport
{
    public required string Format { get; init; }
    public required string[] InputFiles { get; init; }
    public required List<SerializedFileRecord> SerializedFiles { get; init; }
    public required List<NodeRecord> Nodes { get; init; }
    public required ParticleRecords Particles { get; init; }
    public required List<DecalProjectorRecord> DecalProjectors { get; init; }
    public required WaterRecords Water { get; init; }
    public required List<MaterialRecord> Materials { get; init; }
    public required List<ExportRecord> MeshExports { get; init; }
    public required List<ExportRecord> TextureExports { get; init; }
    public required List<TextureArrayExportBatch> TextureArrayExports { get; init; }
    public required List<ExportRecord> MetadataExports { get; init; }
    public required List<ContainerRecord> Containers { get; init; }
    public required Dictionary<string, int> Summary { get; init; }
}

internal sealed class SerializedFileRecord
{
    public required string Name { get; init; }
    public string? OriginalPath { get; init; }
    public required string UnityVersion { get; init; }
    public required string[] ExternalFiles { get; init; }
    public required int ObjectCount { get; init; }
}

internal sealed class NodeRecord
{
    public required string Name { get; init; }
    public required string Source { get; init; }
    public required long PathId { get; init; }
    public required bool GameObjectActive { get; init; }
    public required bool IsRoot { get; init; }
    public long? TransformPathId { get; init; }
    public AssetRef? Parent { get; init; }
    public List<AssetRef>? Children { get; init; }
    public float[]? LocalPosition { get; init; }
    public float[]? LocalRotation { get; init; }
    public float[]? LocalScale { get; init; }
    public string? RendererType { get; init; }
    public AssetRef? RendererAsset { get; init; }
    public bool? RendererEnabled { get; init; }
    public AssetRef? Mesh { get; init; }
    public List<AssetRef>? Materials { get; init; }
}

internal sealed class MaterialRecord
{
    public required string Name { get; init; }
    public required string Source { get; init; }
    public required long PathId { get; init; }
    public required AssetRef Shader { get; init; }
    public required MaterialRenderState RenderState { get; init; }
    public required List<TextureSlotRecord> Textures { get; init; }
    public Dictionary<string, int>? Ints { get; init; }
    public required Dictionary<string, float> Floats { get; init; }
    public required Dictionary<string, float[]> Colors { get; init; }
}

internal sealed class TextureSlotRecord
{
    public required string Property { get; init; }
    public required AssetRef Texture { get; init; }
    public required float[] Scale { get; init; }
    public required float[] Offset { get; init; }
}

internal sealed record AssetRef(string Source, long PathId, string Type, string? Name)
{
    public string[]? PassNames { get; init; }
    public ShaderTextureDefaultRecord[]? TextureDefaults { get; init; }
    public int? ColorSpace { get; init; }
    public TextureSamplerRecord? TextureSampler { get; init; }
}
internal sealed record ExportRecord(AssetRef Asset, string? File, string? Error)
{
    public int? VertexCount { get; init; }
    public int? VertexColorComponents { get; init; }
    public string? VertexColorsFile { get; init; }
    public int? Uv0Components { get; init; }
    public int? Uv1Components { get; init; }
    public string? Uv0File { get; init; }
    public string? Uv1File { get; init; }
    public string? ShapeGeometryFile { get; init; }
    public string? ShapeGeometryError { get; init; }
}
internal sealed record ContainerRecord(string Container, AssetRef Asset, string DeclaredBy);
