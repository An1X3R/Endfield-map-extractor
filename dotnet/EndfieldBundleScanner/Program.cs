using System.Reflection;
using System.Diagnostics;
using System.Collections;
using System.Collections.Specialized;
using System.Text;
using System.Text.Json;
using AnimeStudio;
using Object = AnimeStudio.Object;

internal static class Program
{
    private static readonly MethodInfo LoadFileMethod = typeof(AssetsManager).GetMethod(
        "LoadFile", BindingFlags.Instance | BindingFlags.NonPublic, null,
        new[] { typeof(FileReader) }, null)
        ?? throw new MissingMethodException("AssetsManager.LoadFile(FileReader)");
    private static readonly MethodInfo ReadAssetsMethod = typeof(AssetsManager).GetMethod(
        "ReadAssets", BindingFlags.Instance | BindingFlags.NonPublic)
        ?? throw new MissingMethodException("AssetsManager.ReadAssets()");
    private static readonly JsonSerializerOptions JsonOptions = new() { WriteIndented = false };

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
            Console.Error.WriteLine($"Scanner stopped: {ex}");
            return 1;
        }
    }

    private static void Run(Options options)
    {
        var telemetry = new HelperProgress("EndfieldBundleScanner", options.PerformanceReport,
            "read timing wraps chunk ReadExactly; load_unpack timing wraps LoadFile, including nested reads; " +
            "object_parse timing wraps ReadAssets. Timings are exclusive measured call spans, include failed calls, " +
            "and exclude output serialization. Failed bundles remain below phase totals.");
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
        var cabOutput = options.CabOutput is null ? null : Path.GetFullPath(options.CabOutput);
        if (!File.Exists(input))
            throw new FileNotFoundException("Bundle index does not exist", input);
        if (File.Exists(output) || Directory.Exists(output))
            throw new IOException($"Refusing to overwrite existing output: {output}");
        if (cabOutput is not null && (cabOutput.Equals(output, StringComparison.OrdinalIgnoreCase) ||
                                      File.Exists(cabOutput) || Directory.Exists(cabOutput)))
            throw new IOException($"CAB output must be a new path distinct from provider output: {cabOutput}");
        if (options.PerformanceReport is not null &&
            (Path.GetFullPath(options.PerformanceReport).Equals(output, StringComparison.OrdinalIgnoreCase) ||
             Path.GetFullPath(options.PerformanceReport).Equals(cabOutput, StringComparison.OrdinalIgnoreCase)))
            throw new IOException("Performance report must differ from provider and CAB outputs");
        Directory.CreateDirectory(Path.GetDirectoryName(output)!);
        if (cabOutput is not null)
            Directory.CreateDirectory(Path.GetDirectoryName(cabOutput)!);

        TypeFlags.SetTypes(new Dictionary<ClassIDType, (bool, bool)>());
        TypeFlags.SetType(ClassIDType.AssetBundle, true, false);
        var logger = new QuietLogger();
        Logger.Default = logger;
        Logger.Flags = LoggerEvent.Error;

        var game = GameManager.GetGameByType(GameType.ArknightsEndfield);
        using var outputStream = new FileStream(output, FileMode.CreateNew, FileAccess.Write, FileShare.Read);
        using var writer = new StreamWriter(outputStream, new UTF8Encoding(false));
        using var cabWriter = cabOutput is null ? null : new StreamWriter(
            new FileStream(cabOutput, FileMode.CreateNew, FileAccess.Write, FileShare.Read), new UTF8Encoding(false));
        using var index = new StreamReader(input, Encoding.UTF8);
        var header = index.ReadLine();
        if (header != "logical_name\tchunk_path\toffset\tlength")
            throw new InvalidDataException("Unexpected scan-index header");
        var rows = File.ReadLines(input).LongCount() - 1;
        var total = options.Limit > 0 ? Math.Max(0, Math.Min(rows, options.Limit) - options.Skip)
                                      : Math.Max(0, rows - options.Skip);
        telemetry.Publish("read", 0, total);
        telemetry.Publish("load_unpack", 0, total);
        if (!options.CabOnly)
            telemetry.Publish("object_parse", 0, total);

        string? currentChunk = null;
        FileStream? chunk = null;
        var scanned = 0;
        var failed = 0;
        var matches = 0;
        var cabFailed = 0;
        var cabMatches = 0;
        long readCount = 0;
        long loadedCount = 0;
        long parsedCount = 0;
        try
        {
            string? line;
            while ((line = index.ReadLine()) != null)
            {
                if (options.Limit > 0 && scanned >= options.Limit)
                    break;
                if (scanned < options.Skip)
                {
                    scanned++;
                    continue;
                }
                var fields = line.Split('\t');
                if (fields.Length != 4)
                    throw new InvalidDataException($"Invalid TSV row at bundle {scanned + 1}");
                var logicalName = fields[0];
                var chunkPath = fields[1];
                var offset = long.Parse(fields[2]);
                var length = int.Parse(fields[3]);
                if (!StringComparer.OrdinalIgnoreCase.Equals(currentChunk, chunkPath))
                {
                    chunk?.Dispose();
                    chunk = new FileStream(chunkPath, FileMode.Open, FileAccess.Read, FileShare.ReadWrite,
                        1024 * 1024, FileOptions.SequentialScan);
                    currentChunk = chunkPath;
                }

                var manager = new AssetsManager
                {
                    Game = game,
                    ResolveDependencies = false,
                    Silent = true,
                    SkipProcess = true,
                };
                var cabLoaded = false;
                try
                {
                    logger.ErrorCount = 0;
                    var bytes = new byte[length];
                    chunk!.Position = offset;
                    var started = Stopwatch.GetTimestamp();
                    try
                    {
                        chunk.ReadExactly(bytes);
                        readCount++;
                    }
                    finally
                    {
                        telemetry.AddPhaseDuration("read", Stopwatch.GetElapsedTime(started));
                    }
                    var syntheticPath = Path.Combine(Path.GetDirectoryName(output)!,
                        logicalName.Replace('/', Path.DirectorySeparatorChar));
                    using var memory = new MemoryStream(bytes, writable: false);
                    var reader = new FileReader(syntheticPath, memory, leaveOpen: true).PreProcessing(game);
                    started = Stopwatch.GetTimestamp();
                    try
                    {
                        LoadFileMethod.Invoke(manager, new object[] { reader });
                        loadedCount++;
                    }
                    finally
                    {
                        telemetry.AddPhaseDuration("load_unpack", Stopwatch.GetElapsedTime(started));
                    }
                    var serialized = manager.assetsFileList.Select(file => new
                    {
                        name = file.fileName,
                        externals = file.m_Externals.Select(item => item.fileName).ToArray(),
                    }).ToArray();
                    var cabRecord = JsonSerializer.Serialize(new
                    {
                        kind = "cab", logical_name = logicalName, chunk_path = chunkPath,
                        offset, length, serialized,
                    }, JsonOptions);
                    if (options.CabOnly || cabWriter is not null)
                    {
                        (cabWriter ?? writer).WriteLine(cabRecord);
                        cabMatches++;
                    }
                    cabLoaded = true;
                    if (options.CabOnly)
                    {
                        matches++;
                        goto BundleFinished;
                    }

                    started = Stopwatch.GetTimestamp();
                    try
                    {
                        ReadAssetsMethod.Invoke(manager, null);
                        parsedCount++;
                    }
                    finally
                    {
                        telemetry.AddPhaseDuration("object_parse", Stopwatch.GetElapsedTime(started));
                    }
                    if (logger.ErrorCount != 0)
                        throw new InvalidDataException($"AnimeStudio logged {logger.ErrorCount} object errors while parsing {logicalName}");
                    if (options.AssetBundleTails || options.StreamedScenesOnly ||
                        options.HashContainerAudit)
                    {
                        foreach (var bundle in manager.assetsFileList
                                     .SelectMany(file => file.Objects.OfType<AssetBundle>()))
                        {
                            var unreadTail = ReadUnreadTail(bundle);
                            var typeTreeValues = ReadTypeTreeValues(bundle);
                            if (typeTreeValues == null)
                            {
                                writer.WriteLine(JsonSerializer.Serialize(new
                                {
                                    kind = "assetbundle_metadata_unavailable",
                                    logical_name = logicalName,
                                    chunk_path = chunkPath,
                                    offset,
                                    length,
                                    serialized_file = bundle.assetsFile.fileName,
                                    assetbundle_path_id = bundle.m_PathID,
                                    reason = "AssetBundle type tree is unavailable",
                                }, JsonOptions));
                                continue;
                            }
                            if (options.HashContainerAudit)
                            {
                                var stringEntries = bundle.m_Container
                                    .Select(item => new
                                    {
                                        path = item.Key,
                                        signature = AssetInfoSignature.From(item.Value),
                                    })
                                    .ToArray();
                                var hashEntries = ReadHashContainerEntries(typeTreeValues);
                                var stringSignatures = stringEntries
                                    .GroupBy(item => item.signature)
                                    .ToDictionary(group => group.Key, group => group.Count());
                                var hashSignatures = hashEntries
                                    .GroupBy(item => item.signature)
                                    .ToDictionary(group => group.Key, group => group.Count());
                                var mirrorsStringContainer =
                                    stringEntries.Length == hashEntries.Length &&
                                    MultisetsEqual(stringSignatures, hashSignatures);
                                if (!mirrorsStringContainer)
                                {
                                    writer.WriteLine(JsonSerializer.Serialize(new
                                    {
                                        kind = "hash_container_mismatch",
                                        logical_name = logicalName,
                                        chunk_path = chunkPath,
                                        offset,
                                        length,
                                        serialized_file = bundle.assetsFile.fileName,
                                        assetbundle_path_id = bundle.m_PathID,
                                        string_container_count = stringEntries.Length,
                                        hash_container_count = hashEntries.Length,
                                        string_entries = stringEntries,
                                        hash_entries = hashEntries,
                                    }, JsonOptions));
                                    matches++;
                                }
                                continue;
                            }

                            var isStreamedScene = GetValue(typeTreeValues,
                                "m_IsStreamedSceneAssetBundle") is true;
                            if (options.StreamedScenesOnly && !isStreamedScene)
                                continue;
                            writer.WriteLine(JsonSerializer.Serialize(new
                            {
                                kind = isStreamedScene ? "streamed_scene_assetbundle" : "assetbundle_tail",
                                logical_name = logicalName,
                                chunk_path = chunkPath,
                                offset,
                                length,
                                serialized_file = bundle.assetsFile.fileName,
                                assetbundle_path_id = bundle.m_PathID,
                                assetbundle_name = bundle.Name,
                                unity_version = bundle.version,
                                container_count = bundle.m_Container.Count,
                                preload_count = bundle.m_PreloadTable.Count,
                                bytes_left = unreadTail.Length,
                                tail_hex = Convert.ToHexString(unreadTail),
                                parsed = new
                                {
                                    hash_container = NormalizeTypeTreeValue(
                                        GetValue(typeTreeValues, "m_HashContainer")),
                                    main_asset = NormalizeTypeTreeValue(
                                        GetValue(typeTreeValues, "m_MainAsset")),
                                    runtime_compatibility = GetValue(typeTreeValues,
                                        "m_RuntimeCompatibility"),
                                    asset_bundle_name = GetValue(typeTreeValues,
                                        "m_AssetBundleName"),
                                    dependencies = NormalizeTypeTreeValue(
                                        GetValue(typeTreeValues, "m_Dependencies")),
                                    is_streamed_scene_asset_bundle = isStreamedScene,
                                    explicit_data_layout = GetValue(typeTreeValues,
                                        "m_ExplicitDataLayout"),
                                    scene_hashes = NormalizeTypeTreeValue(
                                        GetValue(typeTreeValues, "m_SceneHashes")),
                                    guid_path_map = NormalizeTypeTreeValue(
                                        GetValue(typeTreeValues, "m_GUIDPathMap")),
                                },
                                type_tree_fields = bundle.serializedType?.m_Type?.m_Nodes
                                    .Where(node => node.m_Level == 1)
                                    .Select(node => new { type = node.m_Type, name = node.m_Name })
                                    .ToArray(),
                            }, JsonOptions));
                            matches++;
                        }
                        goto BundleFinished;
                    }

                    var containers = manager.assetsFileList
                        .SelectMany(file => file.Objects.OfType<AssetBundle>())
                        .SelectMany(bundle => bundle.m_Container.Select(item => item.Key))
                        .Distinct(StringComparer.OrdinalIgnoreCase).ToArray();
                    var wanted = containers.Where(path => options.Contains.Length == 0
                        ? path.Contains("map01/lv001", StringComparison.OrdinalIgnoreCase) ||
                          path.Contains("map01_lv001", StringComparison.OrdinalIgnoreCase)
                        : options.Contains.Any(term =>
                            path.Contains(term, StringComparison.OrdinalIgnoreCase)))
                        .ToArray();
                    if (wanted.Length > 0)
                    {
                        writer.WriteLine(JsonSerializer.Serialize(new
                        {
                            kind = "match", logical_name = logicalName, chunk_path = chunkPath,
                            offset, length, serialized, containers = wanted,
                        }, JsonOptions));
                        matches++;
                    }
                BundleFinished:;
                }
                catch (Exception ex)
                {
                    failed++;
                    if (!cabLoaded && cabWriter is not null)
                    {
                        cabFailed++;
                        cabWriter.WriteLine(JsonSerializer.Serialize(new
                        {
                            kind = "error", logical_name = logicalName,
                            message = Unwrap(ex).Message,
                        }, JsonOptions));
                    }
                    writer.WriteLine(JsonSerializer.Serialize(new
                    {
                        kind = "error", logical_name = logicalName,
                        message = Unwrap(ex).Message,
                    }, JsonOptions));
                }
                finally
                {
                    manager.Clear();
                }

                scanned++;
                if (scanned % options.ProgressEvery == 0)
                {
                    writer.WriteLine(JsonSerializer.Serialize(new
                    {
                        kind = "progress", scanned, failed, matches,
                        utc = DateTime.UtcNow.ToString("O"),
                    }, JsonOptions));
                    if (cabWriter is not null)
                        cabWriter.WriteLine(JsonSerializer.Serialize(new
                        {
                            kind = "progress", scanned, failed = cabFailed, matches = cabMatches,
                            utc = DateTime.UtcNow.ToString("O"),
                        }, JsonOptions));
                    writer.Flush();
                    cabWriter?.Flush();
                    telemetry.Publish("read", readCount, total);
                    telemetry.Publish("load_unpack", loadedCount, total);
                    if (!options.CabOnly)
                        telemetry.Publish("object_parse", parsedCount, total);
                    Console.WriteLine($"scanned={scanned} failed={failed} matches={matches}");
                }
                if (options.Limit > 0 && scanned >= options.Limit)
                    break;
            }
            writer.WriteLine(JsonSerializer.Serialize(new
            {
                kind = "complete", scanned, failed, matches,
                utc = DateTime.UtcNow.ToString("O"),
            }, JsonOptions));
            if (cabWriter is not null)
                cabWriter.WriteLine(JsonSerializer.Serialize(new
                {
                    kind = "complete", scanned, failed = cabFailed, matches = cabMatches,
                    utc = DateTime.UtcNow.ToString("O"),
                }, JsonOptions));
            writer.Flush();
            cabWriter?.Flush();
            telemetry.Publish("read", readCount, total);
            telemetry.Publish("load_unpack", loadedCount, total);
            if (!options.CabOnly)
                telemetry.Publish("object_parse", parsedCount, total);
            telemetry.Publish("manifest_write", 1, 1);
            Console.WriteLine($"complete scanned={scanned} failed={failed} matches={matches}");
        }
        finally
        {
            chunk?.Dispose();
        }
    }

    private static (ulong key, AssetInfoSignature signature)[] ReadHashContainerEntries(
        OrderedDictionary values)
    {
        if (GetValue(values, "m_HashContainer") is not
            IEnumerable<KeyValuePair<object, object>> pairs)
            return Array.Empty<(ulong, AssetInfoSignature)>();
        return pairs.Select(item =>
            (Convert.ToUInt64(item.Key), AssetInfoSignature.From(item.Value))).ToArray();
    }

    private static bool MultisetsEqual<TKey>(
        Dictionary<TKey, int> left, Dictionary<TKey, int> right) where TKey : notnull
    {
        return left.Count == right.Count && left.All(item =>
            right.TryGetValue(item.Key, out var count) && count == item.Value);
    }

    private readonly record struct AssetInfoSignature(
        int PreloadIndex, int PreloadSize, int FileId, long PathId)
    {
        public static AssetInfoSignature From(AssetInfo value)
        {
            return new AssetInfoSignature(value.preloadIndex, value.preloadSize,
                value.asset.m_FileID, value.asset.m_PathID);
        }

        public static AssetInfoSignature From(object value)
        {
            if (value is not OrderedDictionary assetInfo ||
                GetValue(assetInfo, "asset") is not OrderedDictionary asset)
                throw new InvalidDataException("Unexpected AssetInfo value in m_HashContainer");
            return new AssetInfoSignature(
                Convert.ToInt32(GetValue(assetInfo, "preloadIndex")),
                Convert.ToInt32(GetValue(assetInfo, "preloadSize")),
                Convert.ToInt32(GetValue(asset, "m_FileID")),
                Convert.ToInt64(GetValue(asset, "m_PathID")));
        }
    }

    private static OrderedDictionary? ReadTypeTreeValues(AssetBundle bundle)
    {
        if (bundle.serializedType?.m_Type == null)
            return null;
        var reader = bundle.reader;
        var position = reader.Position;
        try
        {
            return bundle.ToType();
        }
        finally
        {
            reader.Position = position;
        }
    }

    private static object? GetValue(OrderedDictionary values, string name)
    {
        return values.Contains(name) ? values[name] : null;
    }

    private static object? NormalizeTypeTreeValue(object? value)
    {
        switch (value)
        {
            case null:
                return null;
            case byte[] bytes:
                return Convert.ToHexString(bytes);
            case OrderedDictionary dictionary:
            {
                var normalized = new Dictionary<string, object?>();
                foreach (DictionaryEntry item in dictionary)
                    normalized[Convert.ToString(item.Key) ?? string.Empty] =
                        NormalizeTypeTreeValue(item.Value);
                return normalized;
            }
            case IEnumerable<KeyValuePair<object, object>> pairs:
                return pairs.Select(item => new
                {
                    key = NormalizeTypeTreeValue(item.Key),
                    value = NormalizeTypeTreeValue(item.Value),
                }).ToArray();
            case IEnumerable sequence when value is not string:
                return sequence.Cast<object?>().Select(NormalizeTypeTreeValue).ToArray();
            default:
                return value;
        }
    }

    private static byte[] ReadUnreadTail(AssetBundle bundle)
    {
        var reader = bundle.reader;
        var position = reader.Position;
        try
        {
            _ = new AssetBundle(reader);
            return reader.ReadBytes(reader.BytesLeft());
        }
        finally
        {
            reader.Position = position;
        }
    }

    private static Exception Unwrap(Exception exception)
    {
        while (exception is TargetInvocationException { InnerException: not null })
            exception = exception.InnerException;
        return exception;
    }

    private sealed class QuietLogger : ILogger
    {
        public int ErrorCount { get; set; }
        public void Log(LoggerEvent loggerEvent, string message)
        {
            if (loggerEvent == LoggerEvent.Error)
                ErrorCount++;
        }
    }

    private sealed record Options(string Input, string Output, int Limit, int ProgressEvery,
        int Skip, bool CabOnly, bool AssetBundleTails, bool StreamedScenesOnly,
        bool HashContainerAudit, string[] Contains, string? CabOutput, string? PerformanceReport)
    {
        public static Options Parse(string[] args)
        {
            string? input = null;
            string? output = null;
            var limit = 0;
            var progressEvery = 1000;
            var skip = 0;
            var cabOnly = false;
            var assetBundleTails = false;
            var streamedScenesOnly = false;
            var hashContainerAudit = false;
            var contains = new List<string>();
            string? cabOutput = null;
            string? performanceReport = null;
            for (var i = 0; i < args.Length; i++)
            {
                switch (args[i])
                {
                    case "--input": input = args[++i]; break;
                    case "--output": output = args[++i]; break;
                    case "--limit": limit = int.Parse(args[++i]); break;
                    case "--progress-every": progressEvery = int.Parse(args[++i]); break;
                    case "--skip": skip = int.Parse(args[++i]); break;
                    case "--cab-only": cabOnly = true; break;
                    case "--assetbundle-tails": assetBundleTails = true; break;
                    case "--streamed-scenes-only": streamedScenesOnly = true; break;
                    case "--hash-container-audit": hashContainerAudit = true; break;
                    case "--contains": contains.Add(args[++i]); break;
                    case "--cab-output": cabOutput = args[++i]; break;
                    case "--performance-report": performanceReport = args[++i]; break;
                    default: throw new ArgumentException($"Unknown option: {args[i]}");
                }
            }
            if (string.IsNullOrWhiteSpace(input) || string.IsNullOrWhiteSpace(output))
                throw new ArgumentException("Usage: --input INDEX.tsv --output RESULTS.jsonl [--limit N]");
            if (limit < 0 || skip < 0 || progressEvery <= 0)
                throw new ArgumentException("Limit/skip must be >= 0 and progress interval must be > 0");
            var metadataModeCount = (assetBundleTails ? 1 : 0) +
                (streamedScenesOnly ? 1 : 0) + (hashContainerAudit ? 1 : 0);
            if (cabOnly && metadataModeCount > 0)
                throw new ArgumentException("--cab-only cannot be combined with AssetBundle metadata modes");
            if (cabOnly && cabOutput is not null)
                throw new ArgumentException("--cab-only writes CAB rows to --output; omit --cab-output");
            if (metadataModeCount > 1)
                throw new ArgumentException(
                    "AssetBundle metadata modes are mutually exclusive");
            return new Options(input, output, limit, progressEvery, skip, cabOnly, assetBundleTails,
                streamedScenesOnly, hashContainerAudit, contains.ToArray(), cabOutput, performanceReport);
        }
    }
}
