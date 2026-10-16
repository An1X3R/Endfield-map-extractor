using AnimeStudio;

internal static partial class Program
{
    private static SceneMeshBoundsRecord ReadSceneMeshBounds(Mesh mesh)
    {
        if (mesh.m_VertexCount <= 0 || mesh.m_Vertices is not { Length: > 0 }
            || mesh.m_Vertices.Length % mesh.m_VertexCount != 0 || mesh.m_Vertices.Length / mesh.m_VertexCount < 3)
            return new SceneMeshBoundsRecord(ToRef(mesh), null, null, "Source vertex positions are unavailable");
        var stride = mesh.m_Vertices.Length / mesh.m_VertexCount;
        var minimum = new[] { float.PositiveInfinity, float.PositiveInfinity, float.PositiveInfinity };
        var maximum = new[] { float.NegativeInfinity, float.NegativeInfinity, float.NegativeInfinity };
        for (var vertex = 0; vertex < mesh.m_VertexCount; vertex++)
            for (var axis = 0; axis < 3; axis++)
            {
                var value = mesh.m_Vertices[vertex * stride + axis];
                if (!float.IsFinite(value))
                    throw new InvalidDataException($"SCENE_MESH_BOUNDS_NONFINITE source={mesh.assetsFile.fileName} pathId={mesh.m_PathID} vertex={vertex} axis={axis}");
                minimum[axis] = Math.Min(minimum[axis], value);
                maximum[axis] = Math.Max(maximum[axis], value);
            }
        return new SceneMeshBoundsRecord(ToRef(mesh), minimum, maximum, null);
    }
}

internal sealed record SceneMeshBoundsRecord(AssetRef Asset, float[]? Minimum, float[]? Maximum, string? Error);
