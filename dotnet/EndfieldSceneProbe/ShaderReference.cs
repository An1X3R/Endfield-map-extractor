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
        };
    }
}
