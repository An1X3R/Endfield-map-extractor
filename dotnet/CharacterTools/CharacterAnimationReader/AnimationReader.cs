using System.Text.Json;
using AnimeStudio;

internal sealed record AnimationRequest(string Format, string CharacterId, string AvatarBundle, string AvatarCab, long AvatarPathId, string ClipBundle, string ResourcePath, string SourceAudit, string AnimatorRootPath, string? ControllerBundle, string UnityVersion, int MaximumStoredFrames, string Output, string? ControllerSelectionReceipt);

internal static class AnimationReader
{
    private static int Main(string[] args)
    {
        try { return Run(args); }
        catch (Exception error)
        {
            Console.Error.WriteLine(JsonSerializer.Serialize(new { status = "failed", error_type = error.GetType().FullName, error.Message, stack = error.ToString() }));
            return 1;
        }
    }

    private static int Run(string[] args)
    {
        if (args.Length != 1) throw new ArgumentException("Expected one EndfieldAnimationExportRequest/1 JSON path.");
        var options = new JsonSerializerOptions { PropertyNamingPolicy = JsonNamingPolicy.SnakeCaseLower, WriteIndented = true };
        AnimationRequest request = JsonSerializer.Deserialize<AnimationRequest>(File.ReadAllText(args[0]), options) ?? throw new InvalidDataException("Animation request was null.");
        if (request.Format != "EndfieldAnimationExportRequest/1" || request.MaximumStoredFrames < 2 || request.AvatarPathId == 0 || string.IsNullOrEmpty(request.AvatarCab) || string.IsNullOrEmpty(request.AnimatorRootPath)) throw new InvalidDataException("Invalid request format, frame budget, Avatar key or Animator root path.");
        using var audit = JsonDocument.Parse(File.ReadAllText(request.SourceAudit));
        JsonElement animator = audit.RootElement.GetProperty("animators").EnumerateArray().Single(row => row.GetProperty("object").GetProperty("name").GetString() == request.AnimatorRootPath.Split('/').Last());
        JsonElement avatar = animator.GetProperty("avatar").GetProperty("resolved");
        if (!request.AvatarCab.Equals(avatar.GetProperty("source").GetString(), StringComparison.OrdinalIgnoreCase) || request.AvatarPathId != avatar.GetProperty("path_id").GetInt64() || avatar.GetProperty("type").GetString() != "Avatar") throw new InvalidDataException("Requested Avatar key does not match the exact static Animator reference.");
        ExportAvatar(request);
        Program.Run(request);
        HumanoidProgram.Run(request);
        return 0;
    }

    private static void ExportAvatar(AnimationRequest request)
    {
        var logger = new DiagnosticLogger();
        Logger.Default = logger;
        Logger.Flags = LoggerEvent.Warning | LoggerEvent.Error;
        TypeFlags.SetTypes(new[] { ClassIDType.Avatar, ClassIDType.AssetBundle }.ToDictionary(type => type, type => (true, false)));
        var manager = new AssetsManager { Game = GameManager.GetGameByType(GameType.ArknightsEndfield), ResolveDependencies = false, SpecifyUnityVersion = request.UnityVersion, Silent = true, SkipProcess = false };
        try
        {
            manager.LoadPreparedFiles([request.AvatarBundle]);
            logger.RequireSuccess();
            Avatar avatar = manager.assetsFileList.SelectMany(file => file.Objects.OfType<Avatar>()).Single(value => value.m_PathID == request.AvatarPathId && value.assetsFile.fileName.Equals(request.AvatarCab, StringComparison.OrdinalIgnoreCase));
            var file = avatar.assetsFile;
            var info = file.m_Objects.Single(value => value.m_PathID == avatar.m_PathID);
            avatar = new Avatar(new ObjectReader(file.reader, file, info, manager.Game));
            long consumed = avatar.reader.Position - avatar.reader.byteStart;
            if (consumed != avatar.byteSize) throw new InvalidDataException($"Exact Avatar parser consumed {consumed} of {avatar.byteSize} bytes.");
            File.WriteAllText(Path.Combine(request.Output, "avatar_parsed.json"), Newtonsoft.Json.JsonConvert.SerializeObject(avatar, Newtonsoft.Json.Formatting.Indented));
        }
        finally { manager.Clear(); }
    }
}
