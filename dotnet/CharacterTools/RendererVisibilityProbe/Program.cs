using System.Text.Json;
using AnimeStudio;

internal sealed record MeshFilterAudit(SourceRef Object, PointerRef GameObject, PointerRef Mesh, TypeTreeAudit TypeTree);
internal sealed record VisibilityRow(SourceRef Renderer, SourceRef GameObject, string Path, bool? Enabled, bool? ActiveSelf, int? GameObjectLayer, byte? CastShadows, uint? PlatformSpecificCastShadows, uint? RenderingLayerMask, int? RendererPriority, bool? IsShadowsOnly, string[] UnavailableFields, TypeTreeAudit RendererTypeTree, MeshFilterAudit? MeshFilter);

/// <summary>Reads source visibility metadata without mesh conversion or image export.</summary>
internal static class VisibilityProgram
{
    private static int Main(string[] args)
    {
        try
        {
            if (args.Length != 4 || args[0] != "--input" || args[2] != "--output") throw new ArgumentException("Expected --input manifest.json --output new_evidence.json.");
            Read(Path.GetFullPath(args[1]), Path.GetFullPath(args[3]));
            return 0;
        }
        catch (Exception error)
        {
            Console.Error.WriteLine(JsonSerializer.Serialize(new { error_type = error.GetType().FullName, message = error.Message, stack = error.StackTrace }, Program.JsonOptions()));
            return 1;
        }
    }

    private static void Read(string inputPath, string outputPath)
    {
        if (File.Exists(outputPath) || Directory.Exists(outputPath)) throw new IOException($"Output already exists: {outputPath}");
        var input = JsonSerializer.Deserialize<CharacterInput>(File.ReadAllText(inputPath), Program.JsonOptions()) ?? throw new InvalidDataException("Input manifest is null.");
        if (input.Format != "EndfieldSingleCharacterInput/1" || input.Bundles.Length == 0 || string.IsNullOrWhiteSpace(input.UnityVersion) || string.IsNullOrWhiteSpace(input.Root.Source) || string.IsNullOrWhiteSpace(input.Root.ResourcePath)) throw new InvalidDataException("Required source fields are empty or input format is unsupported.");
        var paths = input.Bundles.Select(bundle => Program.VerifyBundle(bundle, Path.GetDirectoryName(inputPath)!)).ToArray();
        if (paths.Distinct(StringComparer.OrdinalIgnoreCase).Count() != paths.Length) throw new InvalidDataException("Input bundle paths are duplicated.");
        var logs = new ReaderLogger();
        Logger.Default = logs;
        Logger.Flags = LoggerEvent.Warning | LoggerEvent.Error;
        Logger.Silent = false;
        var types = new[] { ClassIDType.AssetBundle, ClassIDType.GameObject, ClassIDType.Transform, ClassIDType.RectTransform, ClassIDType.MeshFilter, ClassIDType.Mesh, ClassIDType.MeshRenderer, ClassIDType.SkinnedMeshRenderer };
        TypeFlags.SetTypes(types.ToDictionary(type => type, type => (true, false)));
        var manager = new AssetsManager { Game = GameManager.GetGameByType(GameType.ArknightsEndfield), ResolveDependencies = false, SpecifyUnityVersion = input.UnityVersion, Silent = false, SkipProcess = false };
        try
        {
            manager.LoadPreparedFiles(paths);
            logs.RequireNoErrors();
            var sources = manager.assetsFileList.Where(file => string.Equals(file.fileName, input.Root.Source, StringComparison.OrdinalIgnoreCase)).ToArray();
            if (sources.Length != 1) throw new InvalidDataException($"Serialized root CAB count is {sources.Length}: {input.Root.Source}.");
            var matches = sources[0].Objects.OfType<AssetBundle>().SelectMany(bundle => bundle.m_Container).Where(entry => string.Equals(entry.Key.Replace('\\', '/'), input.Root.ResourcePath.Replace('\\', '/'), StringComparison.OrdinalIgnoreCase)).ToArray();
            var root = Program.SelectHierarchyRoot(matches.Select(entry => Program.Require(entry.Value.asset, "root container subasset")), input.Root.ResourcePath);
            var rows = Program.WalkTransforms(root).Select(transform => Program.Require(transform.m_GameObject, "hierarchy GameObject")).SelectMany(Program.Renderers).Select(Audit).ToArray();
            logs.RequireNoErrors();
            var evidence = new { format = "EndfieldCharacterRendererVisibility/1", input_manifest = inputPath, requested_root = input.Root, actual_root = Program.Ref(root), renderers = rows, shadows_only_count = rows.Count(row => row.IsShadowsOnly == true), unavailable_cast_shadows_count = rows.Count(row => row.CastShadows == null), reader_logs = logs.Snapshot(), rule = "Source m_CastShadows=3 means ShadowsOnly; exclude from the visible body pass while preserving shadow geometry. No name matching is used.", rule_scope = "Unity source flags only; camera culling, game layer overrides, runtime state, and shader pass behavior remain unverified.", unity_documentation = "https://docs.unity3d.com/2021.3/Documentation/ScriptReference/Rendering.ShadowCastingMode.ShadowsOnly.html", unity_enum_source = "https://github.com/Unity-Technologies/UnityCsReference/blob/master/Runtime/Export/Graphics/GraphicsEnums.cs" };
            File.WriteAllText(outputPath, JsonSerializer.Serialize(evidence, Program.JsonOptions()));
            Console.WriteLine(JsonSerializer.Serialize(new { status = "visibility_audit_completed", output = outputPath, renderer_count = rows.Length, shadows_only = rows.Count(row => row.IsShadowsOnly == true) }, Program.JsonOptions()));
        }
        finally { manager.Clear(); }
    }

    private static VisibilityRow Audit(Renderer renderer)
    {
        var gameObject = Program.Require(renderer.m_GameObject, "renderer GameObject");
        var gameTree = Program.AuditTypeTree(gameObject);
        var rendererTree = Program.AuditTypeTree(renderer);
        if (rendererTree.Data is JsonElement data && data.TryGetProperty("m_CastShadows", out var cast) && renderer.m_CastShadows.HasValue && cast.GetByte() != renderer.m_CastShadows.Value) throw new InvalidDataException($"Renderer parser and embedded TypeTree cast-shadows values disagree: {renderer.m_PathID}.");
        var unavailable = new List<string>();
        if (renderer.m_CastShadows == null) unavailable.Add("m_CastShadows: not retained by pinned parser");
        if (renderer.m_RenderingLayerMask == null) unavailable.Add("m_RenderingLayerMask: not retained by pinned parser");
        if (renderer.m_PlatformSpecificCastShadows == null) unavailable.Add("m_PlatformSpecificCastShadows: not retained by pinned parser");
        bool? active = gameTree.Data is JsonElement gameData && gameData.TryGetProperty("m_IsActive", out var activeValue) ? activeValue.GetBoolean() : null;
        int? layer = gameTree.Data is JsonElement layerData && layerData.TryGetProperty("m_Layer", out var layerValue) ? layerValue.GetInt32() : null;
        if (active == null) unavailable.Add("GameObject.m_IsActive: no embedded TypeTree field");
        if (layer == null) unavailable.Add("GameObject.m_Layer: no embedded TypeTree field");
        MeshFilterAudit? meshFilter = null;
        if (renderer is MeshRenderer)
        {
            var filter = gameObject.m_MeshFilter ?? throw new InvalidDataException($"MeshRenderer has no MeshFilter: {renderer.assetsFile.fileName}/{renderer.m_PathID}.");
            if (Program.Require(filter.m_GameObject, "MeshFilter GameObject") != gameObject) throw new InvalidDataException($"MeshFilter and MeshRenderer GameObjects disagree: {renderer.assetsFile.fileName}/{renderer.m_PathID}.");
            Program.Require(filter.m_Mesh, "MeshFilter mesh");
            meshFilter = new MeshFilterAudit(Program.Ref(filter), Program.Pointer(filter.m_GameObject), Program.Pointer(filter.m_Mesh), Program.AuditTypeTree(filter));
        }
        return new VisibilityRow(Program.Ref(renderer), Program.Ref(gameObject), Program.TransformPath(gameObject.m_Transform), renderer.m_Enabled, active, layer, renderer.m_CastShadows, renderer.m_PlatformSpecificCastShadows, renderer.m_RenderingLayerMask, renderer.m_RendererPriority, renderer.m_CastShadows.HasValue ? renderer.m_CastShadows.Value == 3 : null, unavailable.ToArray(), rendererTree, meshFilter);
    }
}
