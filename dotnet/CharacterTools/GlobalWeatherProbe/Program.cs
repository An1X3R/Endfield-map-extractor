using System.Diagnostics;
using System.Security.Cryptography;
using System.Text.Json;
using AnimeStudio;

/// <summary>Read complete embedded configuration TypeTrees without following external references.</summary>
internal static class GlobalWeatherProgram
{
    private sealed record TextureTarget(string Slot, string Source, long PathId);
    private static int Main(string[] args)
    {
        try
        {
            if (args.Length == 5 && args[0] == "--configuration-only" && args[1] == "--input" && args[3] == "--output")
                Read(Path.GetFullPath(args[2]), [], Path.GetFullPath(args[4]), null);
            else if (args.Length == 6 && args[0] == "--configuration-resource" && args[2] == "--input" && args[4] == "--output")
                Read(Path.GetFullPath(args[3]), [], Path.GetFullPath(args[5]), args[1]);
            else if (args.Length == 6 && args[0] == "--input" && args[2] == "--textures" && args[4] == "--output")
                Read(Path.GetFullPath(args[1]), ReadTargets(Path.GetFullPath(args[3])), Path.GetFullPath(args[5]), null);
            else throw new ArgumentException("Expected --configuration-only --input manifest.json --output new_directory; --configuration-resource exact_path --input manifest.json --output new_directory; or --input manifest.json --textures exact_targets.json --output new_directory.");
            return 0;
        }
        catch (Exception error)
        {
            Console.Error.WriteLine(JsonSerializer.Serialize(new { error_type = error.GetType().FullName, error.Message, error.StackTrace }, Program.JsonOptions()));
            return 1;
        }
    }

    private static TextureTarget[] ReadTargets(string targetPath)
    {
        var targets = JsonSerializer.Deserialize<TextureTarget[]>(File.ReadAllText(targetPath), Program.JsonOptions()) ?? throw new InvalidDataException("Null texture targets.");
        if (targets.Length == 0 || targets.Any(target => string.IsNullOrWhiteSpace(target.Slot) || string.IsNullOrWhiteSpace(target.Source) || target.PathId == 0) || targets.Select(target => target.Slot).Distinct().Count() != targets.Length) throw new InvalidDataException("Texture targets require unique nonempty slots and non-null source identities.");
        return targets;
    }

    private static void Read(string inputPath, TextureTarget[] targets, string output, string? resourcePath)
    {
        var watch = Stopwatch.StartNew();
        if (Directory.Exists(output) || File.Exists(output)) throw new IOException($"Refusing existing output: {output}");
        var input = JsonSerializer.Deserialize<CharacterInput>(File.ReadAllText(inputPath), Program.JsonOptions()) ?? throw new InvalidDataException("Null input manifest.");
        if (input.Format != "EndfieldSingleCharacterInput/1" || input.Bundles.Length == 0 || string.IsNullOrWhiteSpace(input.UnityVersion) || (resourcePath is null && string.IsNullOrWhiteSpace(input.Root.Source))) throw new InvalidDataException("Invalid input format or missing bundle/root fields.");
        if (resourcePath is not null && string.IsNullOrWhiteSpace(resourcePath)) throw new InvalidDataException("Configuration resource path must be nonempty.");
        var paths = input.Bundles.Select(bundle => Program.VerifyBundle(bundle, Path.GetDirectoryName(inputPath)!)).ToArray();
        if (paths.Distinct(StringComparer.OrdinalIgnoreCase).Count() != paths.Length) throw new InvalidDataException("Duplicate input paths.");
        var logs = new ReaderLogger();
        Logger.Default = logs;
        Logger.Flags = LoggerEvent.Warning | LoggerEvent.Error;
        Logger.Silent = false;
        TypeFlags.SetTypes(new Dictionary<ClassIDType, (bool, bool)> { [ClassIDType.Texture2D] = (true, false), [ClassIDType.AssetBundle] = (true, false) });
        var manager = new AssetsManager { Game = GameManager.GetGameByType(GameType.ArknightsEndfield), ResolveDependencies = false, SpecifyUnityVersion = input.UnityVersion, Silent = false, SkipProcess = false };
        try
        {
            manager.LoadPreparedFiles(paths);
            logs.RequireNoErrors();
            var selectedSource = input.Root.Source;
            if (resourcePath is not null)
            {
                var matches = manager.assetsFileList.SelectMany(file => file.Objects.OfType<AssetBundle>()).SelectMany(bundle => bundle.m_Container).Where(entry => string.Equals(entry.Key.Replace('\\', '/'), resourcePath.Replace('\\', '/'), StringComparison.OrdinalIgnoreCase)).ToArray();
                if (matches.Length != 1) throw new InvalidDataException($"Configuration resource must match exactly one container entry: {resourcePath}, count={matches.Length}.");
                selectedSource = Program.Require(matches[0].Value.asset, "configuration container asset").assetsFile.fileName;
            }
            var sources = manager.assetsFileList.Where(file => string.Equals(file.fileName, selectedSource, StringComparison.OrdinalIgnoreCase)).ToArray();
            if (sources.Length != 1) throw new InvalidDataException($"Root CAB must occur exactly once; count={sources.Length}, source={input.Root.Source}.");
            var source = sources[0];
            var wanted = source.Objects.Where(value => value.type is ClassIDType.MonoBehaviour or ClassIDType.MonoScript or ClassIDType.AssetBundle).ToArray();
            if (wanted.Length == 0) throw new InvalidDataException("Root CAB contains no configuration objects.");
            var trees = wanted.Select(value =>
            {
                var audit = Program.AuditTypeTree(value);
                if (!audit.Available || value.reader.Position - value.reader.byteStart != value.byteSize) throw new InvalidDataException($"Complete embedded TypeTree unavailable: {source.fileName}/{value.m_PathID}, type={value.type}, reason={audit.Reason}.");
                return audit;
            }).ToArray();
            logs.RequireNoErrors();
            Directory.CreateDirectory(output);
            var textureArtifacts = targets.Select(target =>
            {
                var matching = manager.assetsFileList.Where(file => string.Equals(file.fileName, target.Source, StringComparison.OrdinalIgnoreCase)).ToArray();
                if (matching.Length != 1 || !matching[0].ObjectsDic.TryGetValue(target.PathId, out var value) || value is not Texture2D texture) throw new InvalidDataException($"Texture must resolve exactly once with type Texture2D: {target.Source}/{target.PathId}, slot={target.Slot}.");
                var fileName = $"{target.PathId:X16}.png";
                using var png = texture.ConvertToStream(ImageFormat.Png, true) ?? throw new InvalidDataException($"Texture PNG decode failed: {target.Source}/{target.PathId}.");
                if (png.Length == 0) throw new InvalidDataException("Texture decoder produced an empty PNG.");
                png.Position = 0;
                using (var file = new FileStream(Path.Combine(output, fileName), FileMode.CreateNew, FileAccess.Write, FileShare.None)) png.CopyTo(file);
                var bytes = File.ReadAllBytes(Path.Combine(output, fileName));
                return new { target.Slot, source = Program.Ref(texture), width = texture.m_Width, height = texture.m_Height, texture_format = texture.m_TextureFormat.ToString(), color_space = texture.m_ColorSpace, vertical_flip = true, artifact = fileName, bytes = bytes.Length, sha256 = Convert.ToHexString(SHA256.HashData(bytes)) };
            }).ToArray();
            File.WriteAllText(Path.Combine(output, "weather_texture_artifacts.json"), JsonSerializer.Serialize(textureArtifacts, Program.JsonOptions()));
            var externals = source.m_Externals.Select((external, index) => new { file_id = index + 1, external.fileName, external.pathName }).ToArray();
            File.WriteAllText(Path.Combine(output, "configuration_typetrees.json"), JsonSerializer.Serialize(trees, Program.JsonOptions()));
            File.WriteAllText(Path.Combine(output, "receipt.json"), JsonSerializer.Serialize(new { format = "EndfieldGlobalConfigurationProbe/1", source_commit = "5bb936ca1d3662fe9b956ec669ab7ebea08bfd07", input = inputPath, input_sha256 = Convert.ToHexString(SHA256.HashData(File.ReadAllBytes(inputPath))), source = source.fileName, resource_path = resourcePath ?? input.Root.ResourcePath, texture_count = textureArtifacts.Length, object_count = source.Objects.Count, configuration_objects = trees.Length, externals, logs = logs.Snapshot(), elapsed_seconds = watch.Elapsed.TotalSeconds, scope = "Complete embedded configuration TypeTrees and exact requested texture decoding; no recursive closure export, mesh conversion or game write." }, Program.JsonOptions()));
            Console.WriteLine(JsonSerializer.Serialize(new { status = "completed", configuration_objects = trees.Length, elapsed_seconds = watch.Elapsed.TotalSeconds, output }, Program.JsonOptions()));
        }
        finally { manager.Clear(); }
    }
}
