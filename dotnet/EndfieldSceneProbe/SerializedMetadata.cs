using System.Text;
using AnimeStudio;
using Object = AnimeStudio.Object;

internal static partial class Program
{
    private static List<ExportRecord> ExportSerializedMetadata(IEnumerable<Object> objects, string output)
    {
        var directory = Path.Combine(output, "metadata");
        Directory.CreateDirectory(directory);
        var result = new List<ExportRecord>();
        foreach (var obj in objects.Where(value => value.type is ClassIDType.MonoBehaviour or
                     ClassIDType.TextAsset or ClassIDType.Shader or ClassIDType.ComputeShader)
                     .OrderBy(value => value.assetsFile.fileName).ThenBy(value => value.m_PathID))
        {
            var text = obj.Dump();
            if (string.IsNullOrWhiteSpace(text))
                throw new InvalidDataException($"SERIALIZED_METADATA_MISSING source={obj.assetsFile.fileName} pathId={obj.m_PathID} type={obj.type}");
            var path = Path.Combine(directory, UniqueFileName(obj, ".txt"));
            File.WriteAllText(path, text, new UTF8Encoding(false));
            result.Add(new ExportRecord(ToRef(obj), Path.GetRelativePath(output, path), null));
        }
        return result;
    }
}
