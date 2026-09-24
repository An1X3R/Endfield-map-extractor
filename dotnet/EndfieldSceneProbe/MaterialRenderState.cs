using System.Collections.Specialized;
using AnimeStudio;

internal static partial class Program
{
    private static MaterialRenderState ReadMaterialRenderState(Material material)
    {
        var fields = material.ToType() ?? throw new InvalidDataException(
            $"MATERIAL_TYPE_TREE_MISSING source={material.assetsFile.fileName} pathId={material.m_PathID}");
        return new MaterialRenderState(
            MaterialStrings(fields, "disabledShaderPasses", material),
            MaterialStrings(fields, "m_ValidKeywords", material),
            fields["m_CustomRenderQueue"] is int queue ? queue : throw new InvalidDataException(
                $"MATERIAL_QUEUE_INVALID source={material.assetsFile.fileName} pathId={material.m_PathID}"));
    }

    private static string[] MaterialStrings(OrderedDictionary fields, string field, Material material)
    {
        if (fields[field] is not List<object> values || values.Any(value => value is not string))
            throw new InvalidDataException(
                $"MATERIAL_STRING_ARRAY_INVALID source={material.assetsFile.fileName} pathId={material.m_PathID} field={field}");
        return values.Cast<string>().ToArray();
    }
}

internal sealed record MaterialRenderState(string[] DisabledShaderPasses, string[] ValidKeywords, int CustomRenderQueue);
