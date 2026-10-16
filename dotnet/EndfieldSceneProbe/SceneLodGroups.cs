using System.Collections.Specialized;
using AnimeStudio;
using Object = AnimeStudio.Object;

internal static partial class Program
{
    private static T LodField<T>(OrderedDictionary fields, string name, Object owner)
    {
        if (fields[name] is T value)
            return value;
        throw new InvalidDataException($"SCENE_LOD_FIELD_INVALID source={owner.assetsFile.fileName} " +
            $"pathId={owner.m_PathID} field={name} expected={typeof(T).Name} actual={fields[name]?.GetType().Name ?? "missing"}");
    }

    private static SceneLodGroupRecord ReadSceneLodGroup(Object owner)
    {
        var fields = owner.ToType() ?? throw new InvalidDataException(
            $"SCENE_LOD_TYPE_TREE_MISSING source={owner.assetsFile.fileName} pathId={owner.m_PathID}");
        var gameObjectPointer = DecalPointer<GameObject>(fields, "m_GameObject", owner);
        if (!gameObjectPointer.TryGet(out var gameObject))
            throw new InvalidDataException($"SCENE_LOD_OWNER_MISSING source={owner.assetsFile.fileName} pathId={owner.m_PathID}");
        var levels = new List<SceneLodLevelRecord>();
        foreach (var value in LodField<List<object>>(fields, "m_LODs", owner))
        {
            if (value is not OrderedDictionary level)
                throw new InvalidDataException($"SCENE_LOD_LEVEL_INVALID source={owner.assetsFile.fileName} pathId={owner.m_PathID}");
            var renderers = new List<AssetRef>();
            foreach (var entry in LodField<List<object>>(level, "renderers", owner))
            {
                if (entry is not OrderedDictionary renderer)
                    throw new InvalidDataException($"SCENE_LOD_RENDERER_INVALID source={owner.assetsFile.fileName} pathId={owner.m_PathID}");
                var pointer = DecalPointer<Renderer>(renderer, "renderer", owner);
                if (!pointer.TryGet(out var resolved))
                    throw new InvalidDataException($"SCENE_LOD_RENDERER_MISSING source={owner.assetsFile.fileName} pathId={owner.m_PathID} rendererPathId={pointer.m_PathID}");
                renderers.Add(ToRef(resolved));
            }
            var height = LodField<float>(level, "screenRelativeHeight", owner);
            var fade = LodField<float>(level, "fadeTransitionWidth", owner);
            if (!float.IsFinite(height) || !float.IsFinite(fade))
                throw new InvalidDataException($"SCENE_LOD_LEVEL_NONFINITE source={owner.assetsFile.fileName} pathId={owner.m_PathID}");
            levels.Add(new SceneLodLevelRecord(height, fade, renderers));
        }
        return new SceneLodGroupRecord(ToRef(owner), ToRef(gameObject), SceneNodeBoolean(owner, "m_Enabled"), levels,
            Convert.ToHexString(owner.GetRawData()));
    }
}

internal sealed record SceneLodGroupRecord(AssetRef Asset, AssetRef GameObject, bool Enabled,
    List<SceneLodLevelRecord> Levels, string RawHex);
internal sealed record SceneLodLevelRecord(float ScreenRelativeHeight, float FadeTransitionWidth, List<AssetRef> Renderers);
