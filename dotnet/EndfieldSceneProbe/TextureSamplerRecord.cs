using System.Collections.Specialized;
using AnimeStudio;

internal static partial class Program
{
    private static TextureSamplerRecord ReadTextureSampler(Texture2D texture, OrderedDictionary fields)
    {
        if (fields["m_TextureSettings"] is not OrderedDictionary settings)
            throw new InvalidDataException(
                $"TEXTURE_SAMPLER_SOURCE_FIELD_UNAVAILABLE source={texture.assetsFile.fileName} " +
                $"pathId={texture.m_PathID} field=m_TextureSettings");
        return new TextureSamplerRecord(
            SamplerInt(settings, "m_FilterMode", texture),
            SamplerInt(settings, "m_WrapU", texture),
            SamplerInt(settings, "m_WrapV", texture),
            SamplerInt(settings, "m_WrapW", texture),
            SamplerInt(settings, "m_Aniso", texture),
            SamplerFloat(settings, "m_MipBias", texture));
    }

    private static int SamplerInt(OrderedDictionary settings, string field, Texture2D texture)
    {
        if (settings[field] is not int value)
            throw new InvalidDataException(
                $"TEXTURE_SAMPLER_SOURCE_FIELD_UNAVAILABLE source={texture.assetsFile.fileName} " +
                $"pathId={texture.m_PathID} field=m_TextureSettings.{field} " +
                $"actualType={settings[field]?.GetType().Name ?? "missing"}");
        return value;
    }

    private static float SamplerFloat(OrderedDictionary settings, string field, Texture2D texture)
    {
        if (settings[field] is not float value)
            throw new InvalidDataException(
                $"TEXTURE_SAMPLER_SOURCE_FIELD_UNAVAILABLE source={texture.assetsFile.fileName} " +
                $"pathId={texture.m_PathID} field=m_TextureSettings.{field} " +
                $"actualType={settings[field]?.GetType().Name ?? "missing"}");
        if (!float.IsFinite(value))
            throw new InvalidDataException(
                $"TEXTURE_SAMPLER_VALUE_NONFINITE source={texture.assetsFile.fileName} " +
                $"pathId={texture.m_PathID} field=m_TextureSettings.{field} value={value}");
        return value;
    }
}

internal sealed record TextureSamplerRecord(
    int FilterMode,
    int WrapU,
    int WrapV,
    int WrapW,
    int Aniso,
    float MipBias);
