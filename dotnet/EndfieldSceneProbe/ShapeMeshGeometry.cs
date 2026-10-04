using System.Text;
using System.Text.Json;
using AnimeStudio;

internal static partial class Program
{
    private static HashSet<Mesh> SelectShapeMeshes(ParticleRecords particles, IReadOnlyList<Mesh> allMeshes)
    {
        var byKey = allMeshes.ToDictionary(mesh => (mesh.assetsFile.fileName, mesh.m_PathID));
        var selected = new HashSet<Mesh>();
        foreach (var system in particles.Systems)
        {
            var shape = system.Parameters.Shape;
            if (!shape.Enabled || shape.Type != 6 || !shape.References.Mesh.IsResolved)
                continue;
            var reference = shape.References.Mesh.Target;
            if (!byKey.TryGetValue((reference.Source, reference.PathId), out var mesh))
                throw new InvalidDataException(
                    $"SHAPE_MESH_RESOLVED_TARGET_MISSING systemSource={system.Asset.Source} " +
                    $"systemPathId={system.Asset.PathId} meshSource={reference.Source} meshPathId={reference.PathId}");
            selected.Add(mesh);
        }
        return selected;
    }

    private static string WriteShapeMeshGeometry(Mesh mesh, string meshDir, string output)
    {
        var identity = $"source={mesh.assetsFile.fileName} pathId={mesh.m_PathID}";
        var expectedVectorFloats = (long)mesh.m_VertexCount * 3;
        if (mesh.m_VertexCount <= 0 || expectedVectorFloats > int.MaxValue ||
            mesh.m_Vertices is not { Length: > 0 } || mesh.m_Vertices.Length != expectedVectorFloats)
            throw new InvalidDataException(
                $"SHAPE_MESH_VERTICES_INVALID {identity} vertices={mesh.m_VertexCount} " +
                $"floats={mesh.m_Vertices?.Length ?? 0} expected={expectedVectorFloats}");
        RequireShapeMeshFinite(mesh.m_Vertices, "m_Vertices", identity);
        var normals = mesh.m_Normals is { Length: > 0 } ? mesh.m_Normals.ToArray() : [];
        if (normals.Length != 0 && normals.Length != expectedVectorFloats)
            throw new InvalidDataException(
                $"SHAPE_MESH_NORMALS_INVALID {identity} vertices={mesh.m_VertexCount} floats={normals.Length}");
        RequireShapeMeshFinite(normals, "m_Normals", identity);
        var colors = mesh.m_Colors is { Length: > 0 } ? mesh.m_Colors.ToArray() : [];
        if (colors.Length != 0 && colors.Length % mesh.m_VertexCount != 0)
            throw new InvalidDataException(
                $"SHAPE_MESH_COLORS_INVALID {identity} vertices={mesh.m_VertexCount} floats={colors.Length}");
        RequireShapeMeshFinite(colors, "m_Colors", identity);

        var subMeshes = mesh.m_SubMeshes.Select((subMesh, index) => new ShapeMeshSubMesh(
            index, subMesh.firstByte, subMesh.indexCount, (int)subMesh.topology,
            subMesh.topology.ToString(), subMesh.baseVertex, subMesh.firstVertex,
            subMesh.vertexCount)).ToArray();
        var geometry = new ShapeMeshGeometryRecord(
            "EndfieldShapeMeshGeometry/1", ToRef(mesh), "unity_mesh_local_xyz",
            mesh.m_VertexCount, mesh.m_Vertices.ToArray(),
            normals.Length == 0 ? 0 : 3, normals,
            colors.Length == 0 ? 0 : colors.Length / mesh.m_VertexCount, colors,
            "not_exported", "anime_studio_parser_postprocessing", subMeshes,
            mesh.m_Indices.ToArray());
        var path = Path.Combine(meshDir, UniqueFileName(mesh, ".shape.json"));
        File.WriteAllText(path, JsonSerializer.Serialize(geometry, JsonOptions), new UTF8Encoding(false));
        return Path.GetRelativePath(output, path);
    }

    private static void RequireShapeMeshFinite(float[] values, string field, string identity)
    {
        for (var index = 0; index < values.Length; index++)
            if (!float.IsFinite(values[index]))
                throw new InvalidDataException(
                    $"SHAPE_MESH_FLOAT_NONFINITE {identity} field={field} index={index} value={values[index]}");
    }
}

internal sealed record ShapeMeshGeometryRecord(
    string Format, AssetRef Asset, string CoordinateSpace,
    int VertexCount, float[] Vertices,
    int NormalComponents, float[] Normals,
    int ColorComponents, float[] Colors,
    string RawTopologyStatus, string SubMeshIndexCountSource, ShapeMeshSubMesh[] SubMeshes,
    uint[] ProcessedTriangleIndices);
internal sealed record ShapeMeshSubMesh(
    int Slot, uint FirstByte, uint IndexCount, int Topology, string TopologyName,
    uint BaseVertex, uint FirstVertex, uint VertexCount);
