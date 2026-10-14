using System.Collections.Specialized;
using System.Diagnostics;
using System.Security.Cryptography;
using System.Text.Json;
using AnimeStudio;

internal sealed record NpcShaderInput(string Format, string UnityVersion, BundleInput[] Bundles);
internal sealed record ShaderIdentityRow(SourceRef Shader, string? TopLevelName, string? ParsedFormName, string NameField, long ObjectBytes, long TypeTreeBytesRead, int TypeTreeNodeCount, string ObjectSha256, SourceRef[] Materials, object PropertyDefaults);
internal sealed record ShaderIdentityEvidence(string Format, string InputManifest, string InputSha256, string Assembly, string AssemblySha256, string Materials, string MaterialsSha256, int InputBundleCount, int MaterialCount, int ShaderCount, ShaderIdentityRow[] Shaders, ReaderLog[] ReaderLogs, double ElapsedSeconds, string Method, string Scope);

/// <summary>Reads exact NPC material-referenced Shader objects from prepared source bundles.</summary>
internal static class NpcShaderIdentityProgram
{
    private static int Main(string[] args)
    {
        try
        {
            if (args.SequenceEqual(new[] { "--help" }))
            {
                Console.WriteLine(JsonSerializer.Serialize(new { usage = "NpcShaderIdentityProbe.exe --input input.json --assembly assembly.json --materials materials.json --output shader_identity.json" }, Program.JsonOptions()));
                return 0;
            }
            if (args.Length != 8 || args[0] != "--input" || args[2] != "--assembly" || args[4] != "--materials" || args[6] != "--output")
                throw new ArgumentException("Expected --input input.json --assembly assembly.json --materials materials.json --output shader_identity.json.");
            Read(Path.GetFullPath(args[1]), Path.GetFullPath(args[3]), Path.GetFullPath(args[5]), Path.GetFullPath(args[7]));
            return 0;
        }
        catch (Exception error)
        {
            Console.Error.WriteLine(JsonSerializer.Serialize(new { error_type = error.GetType().FullName, message = error.Message, stack = error.StackTrace }, Program.JsonOptions()));
            return 1;
        }
    }

    private static void Read(string inputPath, string assemblyPath, string materialsPath, string outputPath)
    {
        var elapsed = Stopwatch.StartNew();
        var outputParent = Path.GetDirectoryName(outputPath) ?? throw new InvalidDataException($"Output path has no parent directory: {outputPath}.");
        if (!Directory.Exists(outputParent)) throw new DirectoryNotFoundException($"Output parent directory does not exist: {outputParent}.");
        if (File.Exists(outputPath) || Directory.Exists(outputPath)) throw new IOException($"Output already exists: {outputPath}");
        if (PathsEqual(outputPath, inputPath) || PathsEqual(outputPath, assemblyPath) || PathsEqual(outputPath, materialsPath))
            throw new InvalidDataException("Output path must be distinct from every input document.");

        var inputBytes = File.ReadAllBytes(inputPath);
        var assemblyBytes = File.ReadAllBytes(assemblyPath);
        var materialBytes = File.ReadAllBytes(materialsPath);
        var input = JsonSerializer.Deserialize<NpcShaderInput>(inputBytes, Program.JsonOptions()) ?? throw new InvalidDataException("Input manifest is null.");
        var materials = JsonSerializer.Deserialize<MaterialAudit[]>(materialBytes, Program.JsonOptions()) ?? throw new InvalidDataException("Materials document is null.");
        using var assemblyDocument = JsonDocument.Parse(assemblyBytes);
        ValidateAssembly(assemblyDocument.RootElement, assemblyPath);
        ValidateInput(input, inputPath);
        ValidateMaterials(materials);

        var paths = input.Bundles.Select(bundle => Program.VerifyBundle(bundle, Path.GetDirectoryName(inputPath)!)).ToArray();
        if (paths.Distinct(StringComparer.OrdinalIgnoreCase).Count() != paths.Length) throw new InvalidDataException("Input bundle paths are duplicated.");
        if (paths.Any(path => PathsEqual(path, outputPath))) throw new InvalidDataException("Output path collides with an input bundle.");

        var logs = new ReaderLogger();
        Logger.Default = logs;
        Logger.Flags = LoggerEvent.Warning | LoggerEvent.Error;
        Logger.Silent = false;
        TypeFlags.SetTypes(new Dictionary<ClassIDType, (bool, bool)>());
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
            manager.LoadPreparedFiles(paths);
            logs.RequireNoErrors();
            ValidateActualMaterials(manager, materials);
            var rows = ReadShaders(manager, materials);
            logs.RequireNoErrors();
            var evidence = new ShaderIdentityEvidence(
                "EndfieldCharacterShaderIdentity/1",
                inputPath,
                HashBytes(inputBytes),
                assemblyPath,
                HashBytes(assemblyBytes),
                materialsPath,
                HashBytes(materialBytes),
                paths.Length,
                materials.Length,
                rows.Length,
                rows,
                logs.Snapshot(),
                elapsed.Elapsed.TotalSeconds,
                "AnimeStudio prepared bundles; exact CAB/PathID Shader resolution; embedded source TypeTree ToType with complete byte consumption and raw-object SHA256.",
                "Shader identity and parsed property defaults only; no shader-program decoding, mesh conversion, texture decoding, or game writes.");
            var json = JsonSerializer.Serialize(evidence, Program.JsonOptions());
            using var output = new FileStream(outputPath, FileMode.CreateNew, FileAccess.Write, FileShare.None);
            using var writer = new StreamWriter(output);
            writer.Write(json);
            writer.Flush();
            Console.WriteLine(JsonSerializer.Serialize(new { status = "shader_identity_completed", shader_count = rows.Length, material_count = materials.Length, output = outputPath, assembly_sha256 = evidence.AssemblySha256, materials_sha256 = evidence.MaterialsSha256, elapsed_seconds = elapsed.Elapsed.TotalSeconds }, Program.JsonOptions()));
        }
        finally
        {
            manager.Clear();
        }
    }

    private static void ValidateInput(NpcShaderInput input, string inputPath)
    {
        if (input.Format != "EndfieldNpcAssemblyInput/1" || string.IsNullOrWhiteSpace(input.UnityVersion) || input.Bundles.Length == 0)
            throw new InvalidDataException($"NPC input format, Unity version, or prepared bundle list is invalid: {inputPath}.");
    }

    private static void ValidateAssembly(JsonElement assembly, string assemblyPath)
    {
        if (assembly.ValueKind != JsonValueKind.Object || !assembly.TryGetProperty("format", out var format) || format.GetString() != "EndfieldNpcAssembly/1")
            throw new InvalidDataException($"Unsupported NPC assembly format: {assemblyPath}.");
        if (!assembly.TryGetProperty("renderers", out var renderers) || renderers.ValueKind != JsonValueKind.Array || renderers.GetArrayLength() == 0)
            throw new InvalidDataException($"NPC assembly contains no renderer configuration rows: {assemblyPath}.");
    }

    private static void ValidateMaterials(MaterialAudit[] materials)
    {
        if (materials.Length == 0) throw new InvalidDataException("Materials document contains no material records.");
        var identities = new HashSet<(string Source, long PathId)>();
        foreach (var material in materials)
        {
            if (material.Object.Type != "Material" || material.Object.PathId == 0 || string.IsNullOrWhiteSpace(material.Object.Source) || string.IsNullOrWhiteSpace(material.Object.Name))
                throw new InvalidDataException("Material record has an invalid source object identity.");
            if (!identities.Add((material.Object.Source.ToUpperInvariant(), material.Object.PathId)))
                throw new InvalidDataException($"Duplicate Material source identity: {material.Object.Source}/{material.Object.PathId}.");
            if (material.Shader.Resolved is not SourceRef shader || shader.Type != "Shader" || shader.PathId == 0 || string.IsNullOrWhiteSpace(shader.Source))
                throw new InvalidDataException($"Material has no resolved non-null Shader reference: {material.Object.Source}/{material.Object.PathId}.");
            if (shader.PathId != material.Shader.PathId)
                throw new InvalidDataException($"Resolved Shader PathID disagrees with source pointer: {material.Object.Source}/{material.Object.PathId}.");
        }
    }

    private static void ValidateActualMaterials(AssetsManager manager, MaterialAudit[] materials)
    {
        foreach (var material in materials)
        {
            var sourceFile = ResolveFile(manager, material.Object.Source, "Material");
            if (!sourceFile.ObjectsDic.TryGetValue(material.Object.PathId, out var sourceObject) || sourceObject.type != ClassIDType.Material)
                throw new InvalidDataException($"Material source object is absent or has the wrong type: {material.Object.Source}/{material.Object.PathId}.");
            if (sourceObject.serializedType?.m_Type == null)
                throw new InvalidDataException($"Material has no embedded TypeTree: {material.Object.Source}/{material.Object.PathId}.");
            var tree = sourceObject.ToType() ?? throw new InvalidDataException($"Material TypeTree returned no data: {material.Object.Source}/{material.Object.PathId}.");
            var bytesRead = sourceObject.reader.Position - sourceObject.reader.byteStart;
            if (bytesRead != sourceObject.byteSize)
                throw new InvalidDataException($"Material TypeTree did not consume the complete object: {material.Object.Source}/{material.Object.PathId}; read {bytesRead}, expected {sourceObject.byteSize} bytes.");
            var sourceName = ReadName(tree, "m_Name");
            if (!string.Equals(sourceName, material.Object.Name, StringComparison.Ordinal))
                throw new InvalidDataException($"Material name does not match its prepared source identity: {material.Object.Source}/{material.Object.PathId}; audit={material.Object.Name}, source={sourceName}.");
            if (tree["m_Shader"] is not OrderedDictionary sourceShader)
                throw new InvalidDataException($"Material TypeTree has no Shader pointer: {material.Object.Source}/{material.Object.PathId}.");
            var sourceFileId = ReadInteger(sourceShader["m_FileID"], "m_Shader.m_FileID");
            var sourcePathId = ReadInteger(sourceShader["m_PathID"], "m_Shader.m_PathID");
            if (sourceFileId != material.Shader.FileId || sourcePathId != material.Shader.PathId)
                throw new InvalidDataException($"Material TypeTree Shader pointer disagrees with materials.json: {material.Object.Source}/{material.Object.PathId}.");
            var resolved = material.Shader.Resolved ?? throw new InvalidDataException("Resolved Shader disappeared after materials validation.");
            var resolvedFile = ResolveFile(manager, resolved.Source, "Shader");
            if (!resolvedFile.ObjectsDic.TryGetValue(resolved.PathId, out var shaderObject) || shaderObject.type != ClassIDType.Shader)
                throw new InvalidDataException($"Material Shader target is absent or has the wrong type: {resolved.Source}/{resolved.PathId}.");
        }
    }

    private static ShaderIdentityRow[] ReadShaders(AssetsManager manager, MaterialAudit[] materials)
    {
        return materials
            .GroupBy(material => (Source: material.Shader.Resolved!.Source.ToUpperInvariant(), material.Shader.Resolved.PathId))
            .Select(group =>
            {
                var source = group.First().Shader.Resolved!.Source;
                var sourceFile = ResolveFile(manager, source, "Shader");
                var pathId = group.Key.PathId;
                if (!sourceFile.ObjectsDic.TryGetValue(pathId, out var shaderObject) || shaderObject.type != ClassIDType.Shader)
                    throw new InvalidDataException($"Shader target is absent or has the wrong type: {source}/{pathId}.");
                if (shaderObject.serializedType?.m_Type == null)
                    throw new InvalidDataException($"Shader has no embedded TypeTree: {source}/{pathId}.");
                var tree = shaderObject.ToType() ?? throw new InvalidDataException($"Shader TypeTree returned no data: {source}/{pathId}.");
                var bytesRead = shaderObject.reader.Position - shaderObject.reader.byteStart;
                if (bytesRead != shaderObject.byteSize)
                    throw new InvalidDataException($"Shader TypeTree did not consume the complete object: {source}/{pathId}; read {bytesRead}, expected {shaderObject.byteSize} bytes.");
                var topName = ReadName(tree, "m_Name");
                var parsedName = tree.Contains("m_ParsedForm") && tree["m_ParsedForm"] is OrderedDictionary parsed ? ReadName(parsed, "m_Name") : null;
                var name = !string.IsNullOrWhiteSpace(parsedName) ? parsedName : topName;
                if (string.IsNullOrWhiteSpace(name))
                    throw new InvalidDataException($"Shader TypeTree has no nonempty m_Name or m_ParsedForm.m_Name: {source}/{pathId}.");
                var raw = shaderObject.GetRawData();
                if (raw.LongLength != shaderObject.byteSize)
                    throw new InvalidDataException($"Shader raw byte count mismatch: {source}/{pathId}.");
                return new ShaderIdentityRow(
                    new SourceRef(sourceFile.fileName, shaderObject.m_PathID, shaderObject.type.ToString(), name),
                    topName,
                    parsedName,
                    !string.IsNullOrWhiteSpace(parsedName) ? "m_ParsedForm.m_Name" : "m_Name",
                    shaderObject.byteSize,
                    bytesRead,
                    shaderObject.serializedType.m_Type.m_Nodes.Count,
                    Convert.ToHexString(SHA256.HashData(raw)),
                    group.Select(material => material.Object).ToArray(),
                    ReadPropertyDefaults(tree));
            })
            .OrderBy(row => row.Shader.Source, StringComparer.OrdinalIgnoreCase)
            .ThenBy(row => row.Shader.PathId)
            .ToArray();
    }

    private static SerializedFile ResolveFile(AssetsManager manager, string source, string sourceType)
    {
        var files = manager.assetsFileList.Where(file => string.Equals(file.fileName, source, StringComparison.OrdinalIgnoreCase)).ToArray();
        if (files.Length != 1) throw new InvalidDataException($"{sourceType} CAB must resolve exactly once; found {files.Length}: {source}.");
        return files[0];
    }

    private static object ReadPropertyDefaults(OrderedDictionary tree)
    {
        if (tree["m_ParsedForm"] is not OrderedDictionary parsed || parsed["m_PropInfo"] is not OrderedDictionary properties || properties["m_Props"] is not System.Collections.IEnumerable rows)
            throw new InvalidDataException("Required source Shader property table is missing.");
        var result = new List<OrderedDictionary>();
        foreach (var item in rows)
        {
            if (item is not OrderedDictionary row || row["m_Name"] is not string)
                throw new InvalidDataException("Invalid source Shader property row.");
            result.Add(row);
        }
        return result.ToArray();
    }

    private static string? ReadName(OrderedDictionary tree, string field)
    {
        if (!tree.Contains(field)) return null;
        return tree[field] is string value ? value : throw new InvalidDataException($"Embedded Shader TypeTree field is not a string: {field}.");
    }

    private static long ReadInteger(object? value, string field) => value switch
    {
        byte number => number,
        sbyte number => number,
        short number => number,
        ushort number => number,
        int number => number,
        uint number => number,
        long number => number,
        ulong number when number <= long.MaxValue => (long)number,
        _ => throw new InvalidDataException($"Source TypeTree field is not an integral value: {field}.")
    };

    private static string HashBytes(byte[] bytes) => Convert.ToHexString(SHA256.HashData(bytes));

    private static bool PathsEqual(string first, string second) => string.Equals(Path.GetFullPath(first).TrimEnd(Path.DirectorySeparatorChar, Path.AltDirectorySeparatorChar), Path.GetFullPath(second).TrimEnd(Path.DirectorySeparatorChar, Path.AltDirectorySeparatorChar), StringComparison.OrdinalIgnoreCase);
}
