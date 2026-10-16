using System.Collections;
using System.Collections.Specialized;
using System.Text.Json;
using AnimeStudio;

internal static partial class Program
{
    private static SceneRendererRoleRecord ReadSceneRendererRole(Renderer renderer)
    {
        var fields = renderer.ToType() ?? throw new InvalidDataException(
            $"SCENE_RENDERER_ROLE_TYPE_TREE_MISSING source={renderer.assetsFile.fileName} pathId={renderer.m_PathID}");
        if (fields["m_CastShadows"] is not byte castShadows || castShadows > 4)
            throw new InvalidDataException($"SCENE_RENDERER_CAST_SHADOWS_INVALID source={renderer.assetsFile.fileName} " +
                $"pathId={renderer.m_PathID} actual={fields["m_CastShadows"]}");
        if (fields["m_PlatformSpecificCastShadows"] is not OrderedDictionary platform
            || platform["defaultParam"] is not uint platformCastShadows || platformCastShadows > 4)
            throw new InvalidDataException($"SCENE_RENDERER_PLATFORM_CAST_SHADOWS_INVALID source={renderer.assetsFile.fileName} " +
                $"pathId={renderer.m_PathID} expected=m_PlatformSpecificCastShadows.defaultParam in 0..4");
        var pipeline = new Dictionary<string, object>(StringComparer.Ordinal);
        foreach (DictionaryEntry entry in fields)
        {
            if (entry.Key is not string name || entry.Value is not (bool or byte or sbyte or short or ushort or int or uint or long or ulong or float))
                continue;
            pipeline.Add(name, entry.Value);
        }
        using var document = JsonDocument.Parse(Newtonsoft.Json.JsonConvert.SerializeObject(fields));
        return new SceneRendererRoleRecord(castShadows, platformCastShadows, pipeline, document.RootElement.Clone(), Convert.ToHexString(renderer.GetRawData()));
    }
}

internal sealed record SceneRendererRoleRecord(int CastShadows, uint PlatformCastShadows,
    Dictionary<string, object> PipelineFields, JsonElement SourceFields, string RawHex);
