using System.Collections.Specialized;
using AnimeStudio;
using Object = AnimeStudio.Object;

internal static partial class Program
{
    private static AssetRef ReadShaderReference(Object shader)
    {
        // Endfield's current shader layout is described by its serialized type tree.
        var fields = shader.ToType();
        if (fields?["m_ParsedForm"] is not OrderedDictionary parsed ||
            parsed["m_Name"] is not string name || string.IsNullOrWhiteSpace(name))
            throw new InvalidDataException(
                $"SHADER_NAME_INVALID source={shader.assetsFile.fileName} pathId={shader.m_PathID}");
        if (parsed["m_SubShaders"] is not List<object> subShaders)
            throw new InvalidDataException($"SHADER_SUBSHADERS_INVALID source={shader.assetsFile.fileName} pathId={shader.m_PathID}");
        var passNames = new List<string>();
        foreach (var subShader in subShaders)
        {
            if (subShader is not OrderedDictionary sub || sub["m_Passes"] is not List<object> passes)
                throw new InvalidDataException($"SHADER_PASSES_INVALID source={shader.assetsFile.fileName} pathId={shader.m_PathID}");
            foreach (var pass in passes)
            {
                if (pass is not OrderedDictionary values || values["m_State"] is not OrderedDictionary state ||
                    state["m_Name"] is not string passName)
                    throw new InvalidDataException($"SHADER_PASS_NAME_INVALID source={shader.assetsFile.fileName} pathId={shader.m_PathID}");
                passNames.Add(passName);
            }
        }
        return new AssetRef(shader.assetsFile.fileName, shader.m_PathID, shader.type.ToString(), name)
        {
            PassNames = passNames.ToArray(),
            TextureDefaults = ReadShaderTextureDefaults(shader, parsed),
        };
    }

    private static ShaderTextureDefaultRecord[] ReadShaderTextureDefaults(Object shader, OrderedDictionary parsed)
    {
        if (parsed["m_PropInfo"] is not OrderedDictionary propertyInfo ||
            propertyInfo["m_Props"] is not List<object> properties)
            throw new InvalidDataException(
                $"SHADER_TEXTURE_PROPERTIES_INVALID source={shader.assetsFile.fileName} pathId={shader.m_PathID}");

        var defaults = new List<ShaderTextureDefaultRecord>();
        for (var index = 0; index < properties.Count; index++)
        {
            if (properties[index] is not OrderedDictionary property || property["m_Type"] is not int type)
                throw new InvalidDataException(
                    $"SHADER_PROPERTY_TYPE_INVALID source={shader.assetsFile.fileName} pathId={shader.m_PathID} index={index}");
            if (type != 4)
                continue;
            if (property["m_Name"] is not string propertyName || string.IsNullOrWhiteSpace(propertyName) ||
                property["m_DefTexture"] is not OrderedDictionary texture ||
                texture["m_DefaultName"] is not string defaultName ||
                texture["m_TexDim"] is not int dimension)
                throw new InvalidDataException(
                    $"SHADER_TEXTURE_DEFAULT_INVALID source={shader.assetsFile.fileName} pathId={shader.m_PathID} index={index}");
            defaults.Add(new ShaderTextureDefaultRecord(propertyName, defaultName, dimension));
        }
        return defaults.ToArray();
    }
}

internal sealed record ShaderTextureDefaultRecord(string Property, string Name, int Dimension);
