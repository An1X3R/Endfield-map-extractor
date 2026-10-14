using AnimeStudio;
using System.Security.Cryptography;
using System.Text.Json;

namespace EndfieldCharacterModel;

internal sealed record GeometryArray(long Offset, int Count, int Components, string Dtype);
internal sealed record GeometryNode(string Path, string Name, string? ParentPath, float[] Position, float[] Rotation, float[] Scale);
internal sealed record GeometryUv(int Channel, int UvType, GeometryArray Values);
internal sealed record GeometrySubmesh(string Material, GeometryArray Indices);
internal sealed record GeometryBone(string Path, float[] InverseBindMatrixColumnMajor);
internal sealed record GeometrySkin(GeometryArray Offsets, GeometryArray Indices, GeometryArray Weights);
internal sealed record GeometryMesh(string Path, string Name, int VertexCount, GeometryArray Positions,
    GeometryArray? Normals, GeometryArray? Tangents, GeometryArray? Colors, GeometryUv[] Uvs,
    GeometrySubmesh[] Submeshes, GeometryBone[] Bones, GeometrySkin? Skin);
internal sealed record GeometryMorphFrame(float Weight, GeometryArray Indices, GeometryArray Positions,
    GeometryArray? Normals, GeometryArray? Tangents);
internal sealed record GeometryMorphChannel(string Name, GeometryMorphFrame[] Keyframes);
internal sealed record GeometryMorph(string Path, GeometryMorphChannel[] Channels);
internal sealed record GeometryMaterialTexture(string Name, int Destination, float[] Offset, float[] Scale);
internal sealed record GeometryMaterial(string Name, float[] Diffuse, float[] Ambient, float[] Specular,
    float[] Emissive, float[] Reflection, float Shininess, float Transparency, GeometryMaterialTexture[] Textures);
internal sealed record GeometryTexture(string Name, string Path);
internal sealed record GeometryDocument(string Format, string BinaryPath, long BinaryBytes, string BinarySha256,
    string Space, string MatrixConvention, GeometryNode[] Nodes, GeometryMesh[] Meshes,
    GeometryMorph[] Morphs, GeometryMaterial[] Materials, GeometryTexture[] Textures);

/// <summary>Writes the existing converter model as lossless arrays and explicit hierarchy metadata.</summary>
public static class PortableGeometry
{
    public static void Write(string directory, IImported model)
    {
        if (model.AnimationList.Count != 0) throw new InvalidDataException("Portable static geometry cannot contain animation tracks.");
        var nodes = Walk(model.RootFrame).Select(frame => new GeometryNode(frame.Path, frame.Name, frame.Parent?.Path,
            Vector(frame.LocalPosition), Quaternion(frame.LocalRotation), Vector(frame.LocalScale))).ToArray();
        var paths = nodes.Select(node => node.Path).ToHashSet(StringComparer.Ordinal);
        if (paths.Count != nodes.Length) throw new InvalidDataException($"Portable geometry hierarchy contains duplicate paths: {string.Join(';', nodes.GroupBy(node => node.Path).Where(group => group.Count() > 1).Select(group => group.Key))}");
        var binaryPath = Path.Combine(directory, "model.geometry.bin");
        GeometryMesh[] meshes;
        GeometryMorph[] morphs;
        using (var stream = new FileStream(binaryPath, FileMode.CreateNew, FileAccess.Write))
        using (var writer = new BinaryWriter(stream))
        {
            meshes = model.MeshList.Select(mesh => Mesh(writer, mesh, paths)).ToArray();
            var meshesByPath = meshes.ToDictionary(mesh => mesh.Path, StringComparer.Ordinal);
            morphs = model.MorphList.Select(morph => Morph(writer, morph, meshesByPath)).ToArray();
        }
        var materials = model.MaterialList.Select(material => new GeometryMaterial(material.Name,
            Rgba(material.Diffuse), Rgba(material.Ambient), Rgba(material.Specular), Rgba(material.Emissive),
            Rgba(material.Reflection), material.Shininess, material.Transparency,
            material.Textures.Select(texture => new GeometryMaterialTexture(texture.Name, texture.Dest,
                new[] { texture.Offset.X, texture.Offset.Y }, new[] { texture.Scale.X, texture.Scale.Y })).ToArray())).ToArray();
        var textures = model.TextureList.Select(texture => WriteTexture(directory, texture)).ToArray();
        using var binary = File.OpenRead(binaryPath);
        var document = new GeometryDocument("EndfieldCharacterGeometry/1", "model.geometry.bin", binary.Length,
            Convert.ToHexString(SHA256.HashData(binary)).ToLowerInvariant(),
            "Converter coordinates: Unity source reflected X; Y up; meters; quaternion xyzw; unscaled.",
            "Column-major 4x4, column vectors; parentWorld * local; local=T*R*S; converter row-vector inverse binds transposed to column-vector convention.",
            nodes, meshes, morphs, materials, textures);
        var options = new JsonSerializerOptions { PropertyNamingPolicy = JsonNamingPolicy.SnakeCaseLower, WriteIndented = true };
        File.WriteAllText(Path.Combine(directory, "model.geometry.json"), JsonSerializer.Serialize(document, options));
    }

    private static IEnumerable<ImportedFrame> Walk(ImportedFrame frame)
    {
        yield return frame;
        for (var index = 0; index < frame.Count; index++)
            foreach (var child in Walk(frame[index])) yield return child;
    }

    private static float[] Vector(Vector3 value) => new[] { value.X, value.Y, value.Z };
    private static float[] Quaternion(Quaternion value) => new[] { value.X, value.Y, value.Z, value.W };
    private static float[] Rgba(Color value) => new[] { value.R, value.G, value.B, value.A };
    private static float[] Tangent(Vector4 value) => new[] { value.X, value.Y, value.Z, value.W };

    private static GeometryArray Floats(BinaryWriter writer, IEnumerable<float> values, int components)
    {
        var offset = writer.BaseStream.Position;
        var count = 0;
        foreach (var value in values)
        {
            if (!float.IsFinite(value)) throw new InvalidDataException($"Nonfinite portable geometry float at byte offset {writer.BaseStream.Position}.");
            writer.Write(value);
            count++;
        }
        if (count % components != 0) throw new InvalidDataException($"Portable float array has {count} scalars for {components} components.");
        return new(offset, count / components, components, "float32");
    }

    private static GeometryArray Integers(BinaryWriter writer, IEnumerable<int> values, int components)
    {
        var offset = writer.BaseStream.Position;
        var count = 0;
        foreach (var value in values) { writer.Write(value); count++; }
        if (count % components != 0) throw new InvalidDataException("Portable index component count is incomplete.");
        return new(offset, count / components, components, "int32");
    }

    private static GeometryArray Unsigned(BinaryWriter writer, IEnumerable<uint> values)
    {
        var offset = writer.BaseStream.Position;
        var count = 0;
        foreach (var value in values) { writer.Write(value); count++; }
        return new(offset, count, 1, "uint32");
    }

    private static GeometryMesh Mesh(BinaryWriter writer, ImportedMesh mesh, IReadOnlySet<string> paths)
    {
        if (!paths.Contains(mesh.Path)) throw new InvalidDataException($"Portable mesh has no hierarchy node: {mesh.Path}");
        var vertices = mesh.VertexList;
        var positions = Floats(writer, vertices.SelectMany(vertex => Vector(vertex.Vertex)), 3);
        var normals = mesh.hasNormal ? Floats(writer, vertices.SelectMany(vertex => Vector(vertex.Normal)), 3) : null;
        var tangents = mesh.hasTangent ? Floats(writer, vertices.SelectMany(vertex => Tangent(vertex.Tangent)), 4) : null;
        var colors = mesh.hasColor ? Floats(writer, vertices.SelectMany(vertex => Rgba(vertex.Color)), 4) : null;
        var uvs = Enumerable.Range(0, mesh.hasUV.Length).Where(channel => mesh.hasUV[channel]).Select(channel =>
        {
            if (vertices.Count == 0) throw new InvalidDataException($"UV channel on empty mesh: {mesh.Path}/{channel}");
            var components = vertices[0].UV[channel].Length;
            if (components < 2 || components > 4 || vertices.Any(vertex => vertex.UV[channel].Length != components))
                throw new InvalidDataException($"Inconsistent UV channel: {mesh.Path}/{channel}");
            return new GeometryUv(channel, mesh.uvType[channel], Floats(writer, vertices.SelectMany(vertex => vertex.UV[channel]), components));
        }).ToArray();
        var submeshes = mesh.SubmeshList.Select(submesh => new GeometrySubmesh(submesh.Material,
            Integers(writer, submesh.FaceList.SelectMany(face =>
            {
                if (face.VertexIndices.Length != 3) throw new InvalidDataException($"Nontriangle portable mesh: {mesh.Path}");
                var indices = face.VertexIndices.Select(index => checked(index + submesh.BaseVertex)).ToArray();
                if (indices.Any(index => index < 0 || index >= vertices.Count)) throw new InvalidDataException($"Out-of-range portable triangle: {mesh.Path}");
                return indices;
            }), 3))).ToArray();
        var bones = mesh.BoneList.Select(bone =>
        {
            if (bone.Path == null || !paths.Contains(bone.Path)) throw new InvalidDataException($"Unresolved portable bone: {mesh.Path}/{bone.Path}");
            return new GeometryBone(bone.Path, Enumerable.Range(0, 16).Select(index => bone.Matrix[index / 4, index % 4]).ToArray());
        }).ToArray();
        var skin = bones.Length == 0 ? null : Skin(writer, mesh);
        return new(mesh.Path, mesh.Path.Split('/').Last(), vertices.Count, positions, normals, tangents, colors, uvs, submeshes, bones, skin);
    }

    private static GeometrySkin Skin(BinaryWriter writer, ImportedMesh mesh)
    {
        var offsets = new List<uint> { 0 };
        var indices = new List<int>();
        var weights = new List<float>();
        foreach (var vertex in mesh.VertexList)
        {
            if (vertex.BoneIndices == null || vertex.Weights == null || vertex.BoneIndices.Length != vertex.Weights.Length)
                throw new InvalidDataException($"Incomplete portable skin: {mesh.Path}");
            for (var index = 0; index < vertex.Weights.Length; index++)
            {
                var weight = vertex.Weights[index];
                if (!float.IsFinite(weight) || weight < 0) throw new InvalidDataException($"Invalid portable skin weight: {mesh.Path}/{index}/{weight}");
                if (weight == 0) continue;
                var bone = vertex.BoneIndices[index];
                if (bone < 0 || bone >= mesh.BoneList.Count) throw new InvalidDataException($"Out-of-range portable skin bone: {mesh.Path}/{bone}");
                indices.Add(bone);
                weights.Add(weight);
            }
            offsets.Add(checked((uint)weights.Count));
        }
        return new(Unsigned(writer, offsets), Integers(writer, indices, 1), Floats(writer, weights, 1));
    }

    private static GeometryMorph Morph(BinaryWriter writer, ImportedMorph morph, IReadOnlyDictionary<string, GeometryMesh> meshes)
    {
        if (!meshes.TryGetValue(morph.Path, out var mesh)) throw new InvalidDataException($"Morph has no portable mesh: {morph.Path}");
        return new(morph.Path, morph.Channels.Select(channel => new GeometryMorphChannel(channel.Name,
            channel.KeyframeList.Select(frame =>
            {
                if (frame.VertexList.Any(vertex => vertex.Index >= mesh.VertexCount)) throw new InvalidDataException($"Out-of-range portable morph: {morph.Path}/{channel.Name}");
                return new GeometryMorphFrame(frame.Weight, Unsigned(writer, frame.VertexList.Select(vertex => vertex.Index)),
                    Floats(writer, frame.VertexList.SelectMany(vertex => Vector(vertex.Vertex.Vertex)), 3),
                    frame.hasNormals ? Floats(writer, frame.VertexList.SelectMany(vertex => Vector(vertex.Vertex.Normal)), 3) : null,
                    frame.hasTangents ? Floats(writer, frame.VertexList.SelectMany(vertex => Tangent(vertex.Vertex.Tangent)), 4) : null);
            }).ToArray())).ToArray());
    }

    private static GeometryTexture WriteTexture(string directory, ImportedTexture texture)
    {
        if (Path.GetFileName(texture.Name) != texture.Name) throw new InvalidDataException($"Unsafe portable texture name: {texture.Name}");
        var path = Path.Combine(directory, texture.Name);
        File.WriteAllBytes(path, texture.Data);
        return new(texture.Name, texture.Name);
    }
}
