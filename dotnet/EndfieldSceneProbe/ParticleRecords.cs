using System.Collections.Specialized;
using AnimeStudio;
using Object = AnimeStudio.Object;

internal static partial class Program
{
    private static (ParticleRecords Records, Mesh[] Meshes, Material[] Materials, Texture2D[] Textures)
        ReadParticleRecords(IReadOnlyList<Object> objects, IReadOnlyList<GameObject> gameObjects)
    {
        var systemExtractions = objects.Where(value => value.type == ClassIDType.ParticleSystem)
            .Select(ReadParticleSystem).OrderBy(value => value.Record.Asset.Source, StringComparer.Ordinal)
            .ThenBy(value => value.Record.Asset.PathId).ToArray();
        var systems = systemExtractions.Select(value => value.Record).ToArray();
        var rendererExtractions = objects.Where(value => value.type == ClassIDType.ParticleSystemRenderer)
            .Select(ReadParticleRenderer).OrderBy(value => value.Record.Asset.Source, StringComparer.Ordinal)
            .ThenBy(value => value.Record.Asset.PathId).ToArray();
        var renderers = rendererExtractions.Select(value => value.Record).ToArray();
        var gameObjectsByKey = gameObjects.ToDictionary(
            value => (value.assetsFile.fileName, value.m_PathID));
        var linkedComponents = new HashSet<(string Source, long PathId)>();
        var diagnostics = new List<ParticleComponentDiagnostic>();
        var pairs = new List<ParticleComponentPair>();

        foreach (var component in systems.Select(value => (value.Asset, value.GameObject))
                     .Concat(renderers.Select(value => (value.Asset, value.GameObject))))
        {
            if (!component.GameObject.IsResolved)
            {
                diagnostics.Add(new ParticleComponentDiagnostic(
                    "owner_unresolved", component.GameObject.Target, component.Asset,
                    component.GameObject.ResolutionStatus));
                continue;
            }
            var key = (component.GameObject.Target.Source, component.GameObject.Target.PathId);
            var gameObject = gameObjectsByKey[key];
            var listed = gameObject.m_Components.Any(pointer =>
                ParticlePointerSource(pointer.m_FileID, gameObject) == component.Asset.Source &&
                pointer.m_PathID == component.Asset.PathId);
            if (!listed)
                diagnostics.Add(new ParticleComponentDiagnostic(
                    "component_not_listed_by_game_object", component.GameObject.Target,
                    component.Asset, "owner_m_Component_missing"));
            else
                linkedComponents.Add((component.Asset.Source, component.Asset.PathId));
        }

        var systemsByOwner = systems.ToLookup(value =>
            (value.GameObject.Target.Source, value.GameObject.Target.PathId));
        var renderersByOwner = renderers.ToLookup(value =>
            (value.GameObject.Target.Source, value.GameObject.Target.PathId));
        var owners = systems.Select(value => value.GameObject.Target)
            .Concat(renderers.Select(value => value.GameObject.Target))
            .Where(value => value.PathId != 0)
            .DistinctBy(value => (value.Source, value.PathId))
            .OrderBy(value => value.Source, StringComparer.Ordinal)
            .ThenBy(value => value.PathId);
        foreach (var owner in owners)
        {
            var ownerSystems = systemsByOwner[(owner.Source, owner.PathId)].ToArray();
            var ownerRenderers = renderersByOwner[(owner.Source, owner.PathId)].ToArray();
            if (ownerSystems.Length == 1 && ownerRenderers.Length == 1 &&
                linkedComponents.Contains((ownerSystems[0].Asset.Source, ownerSystems[0].Asset.PathId)) &&
                linkedComponents.Contains((ownerRenderers[0].Asset.Source, ownerRenderers[0].Asset.PathId)))
                pairs.Add(new ParticleComponentPair(owner, ownerSystems[0].Asset, ownerRenderers[0].Asset));
            else if (ownerSystems.Length == 1 && ownerRenderers.Length == 1)
                diagnostics.Add(new ParticleComponentDiagnostic(
                    "owner_link_invalid", owner, null, "component_owner_unresolved_or_not_listed"));
            else
                diagnostics.Add(new ParticleComponentDiagnostic(
                    "component_multiplicity", owner, null,
                    $"systems={ownerSystems.Length} renderers={ownerRenderers.Length}"));
        }

        return (new ParticleRecords(
                "component_parameters_no_simulation", "initial_emission_shape_size_color_trail_custom_data_uv_parameters_other_modules_enabled_only",
                systems, renderers, pairs.ToArray(), diagnostics.ToArray()),
            systemExtractions.SelectMany(value => value.Meshes)
                .Concat(rendererExtractions.SelectMany(value => value.Meshes)).Distinct().ToArray(),
            rendererExtractions.SelectMany(value => value.Materials).Distinct().ToArray(),
            systemExtractions.SelectMany(value => value.Textures)
                .Concat(rendererExtractions.SelectMany(value => value.Textures)).Distinct().ToArray());
    }

    private static (ParticleSystemRecord Record, Mesh[] Meshes, Texture2D[] Textures)
        ReadParticleSystem(Object obj)
    {
        var fields = ParticleFields(obj);
        var (owner, _) = ParticlePointer<GameObject>(fields, "m_GameObject", obj);
        var modules = new List<ParticleModuleState>();
        foreach (System.Collections.DictionaryEntry entry in fields)
        {
            if (entry.Key is not string name || !name.EndsWith("Module", StringComparison.Ordinal))
                continue;
            if (entry.Value is not OrderedDictionary module)
                throw ParticleFieldError(obj, name, "OrderedDictionary", entry.Value);
            modules.Add(new ParticleModuleState(name, ParticleField<bool>(module, "enabled", obj)));
        }
        if (modules.Count == 0)
            throw new InvalidDataException(
                $"PARTICLE_MODULES_MISSING source={obj.assetsFile.fileName} pathId={obj.m_PathID}");
        var parameters = ReadParticleModuleParameters(fields, obj);
        return (new ParticleSystemRecord(
            ToRef(obj), owner,
            ParticleFiniteFloat(fields, "lengthInSec", obj),
            ParticleFiniteFloat(fields, "simulationSpeed", obj),
            ParticleField<bool>(fields, "looping", obj),
            ParticleField<bool>(fields, "prewarm", obj),
            ParticleField<bool>(fields, "playOnAwake", obj),
            ParticleField<bool>(fields, "useUnscaledTime", obj),
            ParticleField<bool>(fields, "autoRandomSeed", obj),
            ReadParticleMinMaxCurve(
                ParticleField<OrderedDictionary>(fields, "startDelay", obj), "ParticleSystem.startDelay", obj),
            ParticleField<int>(fields, "moveWithTransform", obj),
            ParticlePointer<Transform>(fields, "moveWithCustomTransform", obj).Reference,
            ParticleField<int>(fields, "scalingMode", obj),
            ParticleField<int>(fields, "randomSeed", obj),
            modules.ToArray(),
            parameters.Parameters),
            parameters.Meshes, parameters.Textures);
    }

    private static (ParticleRendererRecord Record, Mesh[] Meshes, Material[] Materials, Texture2D[] Textures)
        ReadParticleRenderer(Object obj)
    {
        var fields = ParticleFields(obj);
        var meshes = new List<Mesh>();
        var materials = new List<Material>();
        var textures = new List<Texture2D>();
        var (owner, _) = ParticlePointer<GameObject>(fields, "m_GameObject", obj);
        var materialRows = ParticleField<List<object>>(fields, "m_Materials", obj);
        var materialPointers = new ParticleAssetPointer[materialRows.Count];
        for (var index = 0; index < materialRows.Count; index++)
        {
            if (materialRows[index] is not OrderedDictionary pointer)
                throw ParticleFieldError(obj, $"m_Materials[{index}]", "OrderedDictionary", materialRows[index]);
            var (reference, material) = ParticlePointerValue<Material>(
                pointer, $"m_Materials[{index}]", obj);
            materialPointers[index] = reference;
            if (material is not null)
                materials.Add(material);
        }
        var meshSlots = new List<ParticleMeshSlot>();
        foreach (var name in new[] { "m_Mesh", "m_Mesh1", "m_Mesh2", "m_Mesh3" })
        {
            var (reference, mesh) = ParticlePointer<Mesh>(fields, name, obj);
            meshSlots.Add(new ParticleMeshSlot(name, reference));
            if (mesh is not null)
                meshes.Add(mesh);
        }
        var (cutout, cutoutTexture) = ParticlePointer<Texture2D>(fields, "m_CutoutTexture", obj);
        if (cutoutTexture is not null)
            textures.Add(cutoutTexture);
        var streamRows = ParticleField<List<object>>(fields, "m_VertexStreams", obj);
        var streams = streamRows.Select((value, index) => value is byte stream
            ? (int)stream
            : throw ParticleFieldError(obj, $"m_VertexStreams[{index}]", "Byte", value)).ToArray();
        return (new ParticleRendererRecord(
            ToRef(obj), owner, ParticleField<bool>(fields, "m_Enabled", obj),
            ParticleField<ushort>(fields, "m_RenderMode", obj),
            ParticleField<int>(fields, "m_RenderAlignment", obj),
            ParticleFiniteFloat(fields, "m_CameraVelocityScale", obj),
            ParticleFiniteFloat(fields, "m_VelocityScale", obj),
            ParticleFiniteFloat(fields, "m_LengthScale", obj),
            materialPointers, meshSlots.ToArray(), streams, cutout),
            meshes.ToArray(), materials.ToArray(), textures.ToArray());
    }

    private static OrderedDictionary ParticleFields(Object obj) =>
        obj.ToType() ?? throw new InvalidDataException(
            $"PARTICLE_TYPE_TREE_MISSING source={obj.assetsFile.fileName} pathId={obj.m_PathID} type={obj.type}");

    private static T ParticleField<T>(OrderedDictionary fields, string name, Object owner)
    {
        if (fields[name] is T value)
            return value;
        throw ParticleFieldError(owner, name, typeof(T).Name, fields[name]);
    }

    private static InvalidDataException ParticleFieldError(Object owner, string name, string expected, object? actual) =>
        new($"PARTICLE_FIELD_INVALID source={owner.assetsFile.fileName} pathId={owner.m_PathID} " +
            $"field={name} expected={expected} actual={actual?.GetType().Name ?? "missing"}");

    private static float ParticleFiniteFloat(OrderedDictionary fields, string name, Object owner)
    {
        var value = ParticleField<float>(fields, name, owner);
        if (!float.IsFinite(value))
            throw new InvalidDataException(
                $"PARTICLE_FLOAT_NONFINITE source={owner.assetsFile.fileName} pathId={owner.m_PathID} field={name} value={value}");
        return value;
    }

    private static (ParticleAssetPointer Reference, T? Resolved) ParticlePointer<T>(
        OrderedDictionary fields, string name, Object owner) where T : Object
    {
        var pointer = ParticleField<OrderedDictionary>(fields, name, owner);
        return ParticlePointerValue<T>(pointer, name, owner);
    }

    private static (ParticleAssetPointer Reference, T? Resolved) ParticlePointerValue<T>(
        OrderedDictionary fields, string name, Object owner) where T : Object
    {
        var fileId = ParticleField<int>(fields, "m_FileID", owner);
        var pathId = ParticleField<long>(fields, "m_PathID", owner);
        var source = ParticlePointerSource(fileId, owner);
        var unresolved = new AssetRef(source, pathId, typeof(T).Name, null);
        if (pathId == 0)
            return (new ParticleAssetPointer(fileId, unresolved, "null"), null);
        var sourceFile = owner.assetsFile.assetsManager.assetsFileList.FirstOrDefault(
            value => value.fileName.Equals(source, StringComparison.OrdinalIgnoreCase));
        if (sourceFile is null)
            return (new ParticleAssetPointer(fileId, unresolved, "source_not_loaded"), null);
        if (!sourceFile.ObjectsDic.TryGetValue(pathId, out var target))
            return (new ParticleAssetPointer(fileId, unresolved, "target_missing"), null);
        if (target is not T typed)
            throw new InvalidDataException(
                $"PARTICLE_POINTER_TYPE_INVALID source={owner.assetsFile.fileName} pathId={owner.m_PathID} " +
                $"field={name} targetSource={source} targetPathId={pathId} expected={typeof(T).Name} actual={target.type}");
        return (new ParticleAssetPointer(fileId, ToRef(typed), "resolved"), typed);
    }

    private static string ParticlePointerSource(int fileId, Object owner)
    {
        if (fileId == 0)
            return owner.assetsFile.fileName;
        if (fileId > 0 && fileId <= owner.assetsFile.m_Externals.Count)
            return owner.assetsFile.m_Externals[fileId - 1].fileName;
        throw new InvalidDataException(
            $"PARTICLE_POINTER_FILE_ID_INVALID source={owner.assetsFile.fileName} pathId={owner.m_PathID} " +
            $"fileId={fileId} externalCount={owner.assetsFile.m_Externals.Count}");
    }
}

internal sealed record ParticleRecords(
    string DataScope, string ModuleDataScope,
    ParticleSystemRecord[] Systems, ParticleRendererRecord[] Renderers,
    ParticleComponentPair[] Pairs, ParticleComponentDiagnostic[] Diagnostics);
internal sealed record ParticleSystemRecord(
    AssetRef Asset, ParticleAssetPointer GameObject, float LengthInSec, float SimulationSpeed,
    bool Looping, bool Prewarm, bool PlayOnAwake, bool UseUnscaledTime, bool AutoRandomSeed,
    ParticleMinMaxCurve StartDelay, int MoveWithTransform,
    ParticleAssetPointer MoveWithCustomTransform, int ScalingMode, int RandomSeed,
    ParticleModuleState[] ModuleStates,
    ParticleModuleParameters Parameters);
internal sealed record ParticleModuleState(string Name, bool Enabled);
internal sealed record ParticleRendererRecord(
    AssetRef Asset, ParticleAssetPointer GameObject, bool Enabled, ushort RenderMode,
    int RenderAlignment, float CameraVelocityScale, float VelocityScale, float LengthScale,
    ParticleAssetPointer[] Materials, ParticleMeshSlot[] MeshSlots, int[] VertexStreams,
    ParticleAssetPointer CutoutTexture);
internal sealed record ParticleMeshSlot(string Name, ParticleAssetPointer Mesh);
internal sealed record ParticleAssetPointer(int FileId, AssetRef Target, string ResolutionStatus)
{
    public bool IsResolved => ResolutionStatus == "resolved";
}
internal sealed record ParticleComponentPair(AssetRef GameObject, AssetRef System, AssetRef Renderer);
internal sealed record ParticleComponentDiagnostic(
    string Code, AssetRef GameObject, AssetRef? Component, string Detail);
