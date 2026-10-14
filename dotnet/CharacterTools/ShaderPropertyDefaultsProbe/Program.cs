using System.Collections.Specialized;
using System.Diagnostics;
using System.Security.Cryptography;
using System.Text.Json;
using AnimeStudio;

internal sealed record ShaderIdentityRow(SourceRef Shader, string? TopLevelName, string? ParsedFormName, string NameField, long ObjectBytes, long TypeTreeBytesRead, int TypeTreeNodeCount, string ObjectSha256, SourceRef[] Materials, object PropertyDefaults);

/// <summary>Reads exact material-referenced Shader identities through embedded source TypeTrees.</summary>
internal static class ShaderIdentityProgram
{
    private static int Main(string[] args)
    {
        try
        {
            if (args.Length != 6 || args[0] != "--input" || args[2] != "--source-audit" || args[4] != "--output") throw new ArgumentException("Expected --input manifest.json --source-audit source_audit.json --output new_directory.");
            Read(Path.GetFullPath(args[1]), Path.GetFullPath(args[3]), Path.GetFullPath(args[5]));
            return 0;
        }
        catch (Exception error)
        {
            Console.Error.WriteLine(JsonSerializer.Serialize(new { error_type = error.GetType().FullName, message = error.Message, stack = error.StackTrace }, Program.JsonOptions()));
            return 1;
        }
    }

    private static void Read(string inputPath, string auditPath, string outputPath)
    {
        var elapsed = Stopwatch.StartNew();
        if (File.Exists(outputPath) || Directory.Exists(outputPath)) throw new IOException($"Output already exists: {outputPath}");
        var input = JsonSerializer.Deserialize<CharacterInput>(File.ReadAllText(inputPath), Program.JsonOptions()) ?? throw new InvalidDataException("Input manifest is null.");
        var audit = JsonSerializer.Deserialize<SourceAudit>(File.ReadAllText(auditPath), Program.JsonOptions()) ?? throw new InvalidDataException("Source audit is null.");
        if (input.Format != "EndfieldSingleCharacterInput/1" || input.Bundles.Length == 0 || string.IsNullOrWhiteSpace(input.UnityVersion) || string.IsNullOrWhiteSpace(input.Root.Source) || string.IsNullOrWhiteSpace(input.Root.ResourcePath)) throw new InvalidDataException("Required input fields are empty or input format is unsupported.");
        if (audit.Format != "EndfieldCharacterSourceAudit/1" || audit.Commit != "5bb936ca1d3662fe9b956ec669ab7ebea08bfd07") throw new InvalidDataException("Source audit format or pinned reader commit is unsupported.");
        if (!string.Equals(audit.RequestedRoot.Source, input.Root.Source, StringComparison.OrdinalIgnoreCase) || !string.Equals(audit.RequestedRoot.ResourcePath.Replace('\\', '/'), input.Root.ResourcePath.Replace('\\', '/'), StringComparison.OrdinalIgnoreCase)) throw new InvalidDataException("Source audit root does not match the input manifest root.");
        if (audit.Materials.Length == 0) throw new InvalidDataException("Source audit contains no materials.");
        foreach (var material in audit.Materials)
        {
            if (material.Shader.Resolved is not SourceRef shader || shader.Type != "Shader" || shader.PathId == 0 || string.IsNullOrWhiteSpace(shader.Source)) throw new InvalidDataException($"Material has no resolved non-null Shader reference: {material.Object.Source}/{material.Object.PathId}.");
            if (shader.PathId != material.Shader.PathId) throw new InvalidDataException($"Resolved Shader PathID disagrees with source pointer: {material.Object.Source}/{material.Object.PathId}.");
        }
        var paths = input.Bundles.Select(bundle => Program.VerifyBundle(bundle, Path.GetDirectoryName(inputPath)!)).ToArray();
        if (paths.Distinct(StringComparer.OrdinalIgnoreCase).Count() != paths.Length) throw new InvalidDataException("Input bundle paths are duplicated.");
        if (paths.Any(path => path.StartsWith(outputPath + Path.DirectorySeparatorChar, StringComparison.OrdinalIgnoreCase))) throw new InvalidDataException("Output must not contain an input bundle.");
        var logs = new ReaderLogger();
        Logger.Default = logs;
        Logger.Flags = LoggerEvent.Warning | LoggerEvent.Error;
        Logger.Silent = false;
        TypeFlags.SetTypes(new Dictionary<ClassIDType, (bool, bool)>());
        var manager = new AssetsManager { Game = GameManager.GetGameByType(GameType.ArknightsEndfield), ResolveDependencies = false, SpecifyUnityVersion = input.UnityVersion, Silent = false, SkipProcess = false };
        try
        {
            manager.LoadPreparedFiles(paths);
            logs.RequireNoErrors();
            var rows = audit.Materials.GroupBy(material => (material.Shader.Resolved!.Source, material.Shader.Resolved.PathId)).Select(group =>
            {
                var sources = manager.assetsFileList.Where(file => string.Equals(file.fileName, group.Key.Source, StringComparison.OrdinalIgnoreCase)).ToArray();
                if (sources.Length != 1) throw new InvalidDataException($"Shader CAB must resolve exactly once; found {sources.Length}: {group.Key.Source}.");
                if (!sources[0].ObjectsDic.TryGetValue(group.Key.PathId, out var shader) || shader.type != ClassIDType.Shader) throw new InvalidDataException($"Shader target is absent or has the wrong type: {group.Key.Source}/{group.Key.PathId}.");
                if (shader.serializedType?.m_Type == null) throw new InvalidDataException($"Shader has no embedded TypeTree: {group.Key.Source}/{group.Key.PathId}.");
                var tree = shader.ToType() ?? throw new InvalidDataException($"Shader TypeTree returned no data: {group.Key.Source}/{group.Key.PathId}.");
                var bytesRead = shader.reader.Position - shader.reader.byteStart;
                if (bytesRead != shader.byteSize) throw new InvalidDataException($"Shader TypeTree did not consume the complete object: {group.Key.Source}/{group.Key.PathId}; read {bytesRead}, expected {shader.byteSize} bytes.");
                var topName = ReadName(tree, "m_Name");
                var parsedName = tree.Contains("m_ParsedForm") && tree["m_ParsedForm"] is OrderedDictionary parsed ? ReadName(parsed, "m_Name") : null;
                var name = !string.IsNullOrWhiteSpace(parsedName) ? parsedName : topName;
                if (string.IsNullOrWhiteSpace(name)) throw new InvalidDataException($"Shader TypeTree has no nonempty m_Name or m_ParsedForm.m_Name: {group.Key.Source}/{group.Key.PathId}.");
                var raw = shader.GetRawData();
                if (raw.LongLength != shader.byteSize) throw new InvalidDataException($"Shader raw byte count mismatch: {group.Key.Source}/{group.Key.PathId}.");
                return new ShaderIdentityRow(new SourceRef(sources[0].fileName, shader.m_PathID, shader.type.ToString(), name), topName, parsedName, !string.IsNullOrWhiteSpace(parsedName) ? "m_ParsedForm.m_Name" : "m_Name", shader.byteSize, bytesRead, shader.serializedType.m_Type.m_Nodes.Count, Convert.ToHexString(SHA256.HashData(raw)), group.Select(material => material.Object).ToArray(), ReadPropertyDefaults(tree));
            }).ToArray();
            logs.RequireNoErrors();
            Directory.CreateDirectory(outputPath);
            var evidence = new { format = "EndfieldCharacterShaderIdentity/1", source_commit = audit.Commit, input_manifest = inputPath, input_sha256 = HashFile(inputPath), source_audit = auditPath, source_audit_sha256 = HashFile(auditPath), requested_root = input.Root, input_bundle_count = paths.Length, material_count = audit.Materials.Length, shader_count = rows.Length, shaders = rows, reader_logs = logs.Snapshot(), elapsed_seconds = elapsed.Elapsed.TotalSeconds, method = "Pinned reader generic object ToType over embedded source TypeTree; complete object consumption required; Shader.Name precedence m_ParsedForm.m_Name then m_Name.", scope = "Identity only; no shader program decompression, mesh conversion, texture decoding, or game writes." };
            File.WriteAllText(Path.Combine(outputPath, "shader_identity.json"), JsonSerializer.Serialize(evidence, Program.JsonOptions()));
            Console.WriteLine(JsonSerializer.Serialize(new { status = "shader_identity_completed", shader_count = rows.Length, material_count = audit.Materials.Length, output = outputPath, elapsed_seconds = elapsed.Elapsed.TotalSeconds }, Program.JsonOptions()));
        }
        finally { manager.Clear(); }
    }

    private static object ReadPropertyDefaults(OrderedDictionary tree)
    {
        if (tree["m_ParsedForm"] is not OrderedDictionary parsed || parsed["m_PropInfo"] is not OrderedDictionary properties || properties["m_Props"] is not System.Collections.IEnumerable rows) throw new InvalidDataException("Required source shader property table is missing.");
        var result = new List<OrderedDictionary>();
        foreach (var item in rows)
        {
            if (item is not OrderedDictionary row || row["m_Name"] is not string) throw new InvalidDataException("Invalid source shader property row.");
            result.Add(row);
        }
        return result.ToArray();
    }

    private static string? ReadName(OrderedDictionary tree, string field)
    {
        if (!tree.Contains(field)) return null;
        return tree[field] is string value ? value : throw new InvalidDataException($"Embedded Shader TypeTree field is not a string: {field}.");
    }

    private static string HashFile(string path)
    {
        using var stream = File.OpenRead(path);
        return Convert.ToHexString(SHA256.HashData(stream));
    }
}
