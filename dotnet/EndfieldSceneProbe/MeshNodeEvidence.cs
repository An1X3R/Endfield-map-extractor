using AnimeStudio;
using Object = AnimeStudio.Object;

internal static partial class Program
{
    private static bool SceneNodeBoolean(Object obj, string field)
    {
        var fields = obj.ToType() ?? throw new InvalidDataException(
            $"SCENE_NODE_TYPE_TREE_MISSING source={obj.assetsFile.fileName} pathId={obj.m_PathID} type={obj.type}");
        return fields[field] switch
        {
            bool value => value,
            byte value => value != 0,
            _ => throw new InvalidDataException(
                $"SCENE_NODE_BOOLEAN_MISSING source={obj.assetsFile.fileName} pathId={obj.m_PathID} " +
                $"type={obj.type} field={field} actual={fields[field]?.GetType().Name ?? "missing"}"),
        };
    }

    private static int MeshVertexColorComponents(Mesh mesh)
    {
        if (mesh.m_Colors is not { Length: > 0 })
            return 0;
        if (mesh.m_VertexCount <= 0 || mesh.m_Colors.Length % mesh.m_VertexCount != 0)
            throw new InvalidDataException(
                $"MESH_VERTEX_COLOR_COUNT_INVALID source={mesh.assetsFile.fileName} pathId={mesh.m_PathID} " +
                $"vertices={mesh.m_VertexCount} colorFloats={mesh.m_Colors.Length}");
        return mesh.m_Colors.Length / mesh.m_VertexCount;
    }

    private static string? WriteMeshVertexColors(Mesh mesh, string meshDir, string output)
    {
        var components = MeshVertexColorComponents(mesh);
        if (components == 0)
            return null;
        var path = Path.Combine(meshDir, UniqueFileName(mesh, ".rgba32f"));
        using var stream = File.Create(path);
        using var writer = new BinaryWriter(stream);
        foreach (var component in mesh.m_Colors)
        {
            if (!float.IsFinite(component))
                throw new InvalidDataException(
                    $"MESH_VERTEX_COLOR_NONFINITE source={mesh.assetsFile.fileName} pathId={mesh.m_PathID}");
            writer.Write(component);
        }
        return Path.GetRelativePath(output, path);
    }

    private static int MeshUvComponents(Mesh mesh, float[]? values, string channel)
    {
        if (values is not { Length: > 0 })
            return 0;
        if (mesh.m_VertexCount <= 0 || values.Length % mesh.m_VertexCount != 0)
            throw new InvalidDataException(
                $"MESH_UV_COUNT_INVALID source={mesh.assetsFile.fileName} pathId={mesh.m_PathID} " +
                $"channel={channel} vertices={mesh.m_VertexCount} floats={values.Length}");
        return values.Length / mesh.m_VertexCount;
    }

    private static string? WriteMeshUv(Mesh mesh, float[]? values, string channel,
                                       string meshDir, string output)
    {
        if (MeshUvComponents(mesh, values, channel) == 0)
            return null;
        var path = Path.Combine(meshDir, UniqueFileName(mesh, $".{channel}f32"));
        using var stream = File.Create(path);
        using var writer = new BinaryWriter(stream);
        foreach (var component in values!)
        {
            if (!float.IsFinite(component))
                throw new InvalidDataException(
                    $"MESH_UV_NONFINITE source={mesh.assetsFile.fileName} pathId={mesh.m_PathID} channel={channel}");
            writer.Write(component);
        }
        return Path.GetRelativePath(output, path);
    }
}
