using System.Globalization;
using System.Text;
using System.Text.Json;
using System.Text.Json.Serialization;
using AnimeStudio;
using Object = AnimeStudio.Object;

internal static class Program
{
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
        var input = Path.GetFullPath(options.Input);
        var output = Path.GetFullPath(options.Output);
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
            ClassIDType.Mesh, ClassIDType.Material, ClassIDType.Texture2D, ClassIDType.Shader,
        })
            TypeFlags.SetType(type, true, false);

        Logger.Default = new ConsoleLogger();
        Logger.Flags = LoggerEvent.Error | LoggerEvent.Warning | LoggerEvent.Info;
        var manager = new AssetsManager
        {
            Game = GameManager.GetGameByType(GameType.ArknightsEndfield),
            ResolveDependencies = false,
            Silent = false,
        };
        manager.LoadFiles(files);

        var allObjects = manager.assetsFileList.SelectMany(f => f.Objects).ToList();
        var gameObjects = allObjects.OfType<GameObject>().ToList();
        var allMeshes = allObjects.OfType<Mesh>().ToList();
        var allMaterials = allObjects.OfType<Material>().ToList();
        var allTextures = allObjects.OfType<Texture2D>().ToList();

        var meshRefs = new HashSet<Mesh>();
        var materialRefs = new HashSet<Material>();
        var textureRefs = new HashSet<Texture2D>();
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
                IsRoot = transform == null || transform.m_Father.IsNull,
                TransformPathId = transform?.m_PathID,
                Parent = transform == null ? null : ToRef(transform.m_Father, transform.assetsFile),
                Children = transform?.m_Children.Select(p => ToRef(p, transform.assetsFile)).ToList(),
                LocalPosition = transform == null ? null : new[] { transform.m_LocalPosition.X, transform.m_LocalPosition.Y, transform.m_LocalPosition.Z },
                LocalRotation = transform == null ? null : new[] { transform.m_LocalRotation.X, transform.m_LocalRotation.Y, transform.m_LocalRotation.Z, transform.m_LocalRotation.W },
                LocalScale = transform == null ? null : new[] { transform.m_LocalScale.X, transform.m_LocalScale.Y, transform.m_LocalScale.Z },
                RendererType = renderer?.type.ToString(),
                Mesh = mesh == null ? null : ToRef(mesh),
                Materials = materials.Count == 0 ? null : materials,
            });
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

            materialRecords.Add(new MaterialRecord
            {
                Name = material.m_Name,
                Source = material.assetsFile.fileName,
                PathId = material.m_PathID,
                Shader = ToRef(material.m_Shader, material.assetsFile),
                Textures = texEnvs,
                Ints = material.m_SavedProperties.m_Ints?.ToDictionary(x => x.Key, x => x.Value),
                Floats = material.m_SavedProperties.m_Floats.ToDictionary(x => x.Key, x => x.Value),
                Colors = material.m_SavedProperties.m_Colors.ToDictionary(
                    x => x.Key, x => new[] { x.Value.R, x.Value.G, x.Value.B, x.Value.A }),
            });
        }

        var meshRecords = new List<ExportRecord>();
        foreach (var mesh in options.ManifestOnly
                     ? Enumerable.Empty<Mesh>()
                     : meshRefs.OrderBy(x => x.assetsFile.fileName).ThenBy(x => x.m_PathID))
        {
            var fileName = UniqueFileName(mesh, ".obj");
            var path = Path.Combine(meshDir, fileName);
            var ok = WriteObj(mesh, path);
            meshRecords.Add(new ExportRecord(ToRef(mesh), ok ? Path.GetRelativePath(output, path) : null,
                ok ? null : "Mesh has no exportable triangle/vertex data"));
        }

        var textureRecords = new List<ExportRecord>();
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
            textureRecords.Add(new ExportRecord(ToRef(texture), error == null ? Path.GetRelativePath(output, path) : null, error));
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
            Materials = materialRecords,
            MeshExports = meshRecords,
            TextureExports = textureRecords,
            Containers = containers,
            Summary = new Dictionary<string, int>
            {
                ["serializedFiles"] = manager.assetsFileList.Count,
                ["gameObjects"] = gameObjects.Count,
                ["roots"] = nodes.Count(n => n.IsRoot),
                ["allMeshes"] = allMeshes.Count,
                ["selectedMeshes"] = meshRefs.Count,
                ["allMaterials"] = allMaterials.Count,
                ["selectedMaterials"] = materialRefs.Count,
                ["allTextures"] = allTextures.Count,
                ["selectedTextures"] = textureRefs.Count,
                ["containers"] = containers.Count,
            },
        };

        var reportPath = Path.Combine(output, "scene_manifest.json");
        File.WriteAllText(reportPath, JsonSerializer.Serialize(report, JsonOptions), new UTF8Encoding(false));
        Console.WriteLine($"Scene manifest: {reportPath}");
        Console.WriteLine(string.Join(", ", report.Summary.Select(x => $"{x.Key}={x.Value}")));
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

    private sealed record Options(string Input, string Output, bool IncludeUnreferenced, bool ManifestOnly)
    {
        public static Options Parse(string[] args)
        {
            if (args.Length < 2)
                throw new ArgumentException("Usage: EndfieldSceneProbe <input-file-or-folder> <new-output-folder> [--include-unreferenced] [--manifest-only]");
            var flags = args.Skip(2).ToHashSet(StringComparer.OrdinalIgnoreCase);
            return new Options(args[0], args[1], flags.Contains("--include-unreferenced"), flags.Contains("--manifest-only"));
        }
    }
}

internal sealed class SceneReport
{
    public required string Format { get; init; }
    public required string[] InputFiles { get; init; }
    public required List<SerializedFileRecord> SerializedFiles { get; init; }
    public required List<NodeRecord> Nodes { get; init; }
    public required List<MaterialRecord> Materials { get; init; }
    public required List<ExportRecord> MeshExports { get; init; }
    public required List<ExportRecord> TextureExports { get; init; }
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
    public required bool IsRoot { get; init; }
    public long? TransformPathId { get; init; }
    public AssetRef? Parent { get; init; }
    public List<AssetRef>? Children { get; init; }
    public float[]? LocalPosition { get; init; }
    public float[]? LocalRotation { get; init; }
    public float[]? LocalScale { get; init; }
    public string? RendererType { get; init; }
    public AssetRef? Mesh { get; init; }
    public List<AssetRef>? Materials { get; init; }
}

internal sealed class MaterialRecord
{
    public required string Name { get; init; }
    public required string Source { get; init; }
    public required long PathId { get; init; }
    public required AssetRef Shader { get; init; }
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

internal sealed record AssetRef(string Source, long PathId, string Type, string? Name);
internal sealed record ExportRecord(AssetRef Asset, string? File, string? Error);
internal sealed record ContainerRecord(string Container, AssetRef Asset, string DeclaredBy);


