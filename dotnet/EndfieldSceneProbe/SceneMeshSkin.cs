using System.Text;
using System.Text.Json;
using System.Reflection;
using System.Collections.Specialized;
using AnimeStudio;

internal static partial class Program
{
    private static readonly FieldInfo MeshVertexDataField = typeof(Mesh).GetField(
        "m_VertexData", BindingFlags.Instance | BindingFlags.NonPublic)
        ?? throw new MissingFieldException("AnimeStudio.Mesh.m_VertexData");
    private static readonly FieldInfo MeshCompressedDataField = typeof(Mesh).GetField(
        "m_CompressedMesh", BindingFlags.Instance | BindingFlags.NonPublic)
        ?? throw new MissingFieldException("AnimeStudio.Mesh.m_CompressedMesh");

    private static SceneSkinnedRendererRecord ReadSceneSkin(SkinnedMeshRenderer renderer)
    {
        var weights = renderer.m_BlendShapeWeights ?? throw new InvalidDataException(
            $"SCENE_SKIN_BLEND_WEIGHTS_MISSING source={renderer.assetsFile.fileName} pathId={renderer.m_PathID}");
        RequireSkinFinite(weights, "m_BlendShapeWeights", ToRef(renderer));
        return new SceneSkinnedRendererRecord(
            renderer.m_Bones.Select(pointer => ReadSkinPointer(pointer, renderer.assetsFile)).ToArray(),
            ReadSkinPointer(renderer.m_RootBone, renderer.assetsFile), weights.ToArray());
    }

    private static SceneSkinPointerRecord ReadSkinPointer(PPtr<Transform> pointer, SerializedFile owner) =>
        new(pointer.m_FileID, ToRef(pointer, owner), pointer.TryGet(out _));

    private static string? WriteSceneMeshSkin(Mesh mesh, string meshDir, string output)
    {
        if (mesh.m_BindPose is not { Length: > 0 })
            return null;
        var asset = ToRef(mesh);
        if (mesh.m_VertexCount <= 0)
            throw new InvalidDataException($"SCENE_SKIN_VERTEX_COUNT_INVALID source={asset.Source} pathId={asset.PathId} count={mesh.m_VertexCount}");
        var indexChannel = ReadSkinChannel(mesh, 13);
        var weightChannel = ReadSkinChannel(mesh, 12);
        var fields = mesh.ToType() ?? throw new InvalidDataException(
            $"SCENE_SKIN_SOURCE_FIELDS_MISSING source={asset.Source} pathId={asset.PathId}");
        var bonesPerVertex = fields["m_BonesPerVertex"] switch
        {
            uint value => value,
            int value when value >= 0 => (uint)value,
            _ => throw new InvalidDataException($"SCENE_SKIN_LAYOUT_MISSING source={asset.Source} pathId={asset.PathId} field=m_BonesPerVertex"),
        };
        if (indexChannel.Components != 4 || !MeshHelper.IsIntFormat(indexChannel.Format))
            throw new InvalidDataException($"SCENE_SKIN_INDEX_CHANNEL_UNSUPPORTED source={asset.Source} pathId={asset.PathId} components={indexChannel.Components} format={indexChannel.Format}");
        var sourceIndices = MeshHelper.BytesToIntArray(indexChannel.Bytes, indexChannel.Format);
        var compressed = MeshCompressedDataField.GetValue(mesh) as CompressedMesh;
        if (compressed?.m_Weights.m_NumItems > 0 || compressed?.m_BoneIndices.m_NumItems > 0)
            throw new InvalidDataException($"SCENE_SKIN_COMPRESSED_WEIGHTS_UNSUPPORTED source={asset.Source} pathId={asset.PathId}");
        float[] sourceWeights;
        string weightSource;
        if (weightChannel.Components == 0)
        {
            var nonzeroPadding = Enumerable.Range(0, mesh.m_VertexCount).Count(vertex =>
                Enumerable.Range(1, 3).Any(component => sourceIndices[vertex * 4 + component] != 0));
            if (bonesPerVertex != 1 || nonzeroPadding != 0)
                throw new InvalidDataException($"SCENE_SKIN_IMPLICIT_LAYOUT_UNSUPPORTED source={asset.Source} pathId={asset.PathId} serializedBonesPerVertex={bonesPerVertex} nonzeroIndexPaddingVertices={nonzeroPadding}");
            sourceWeights = new float[checked(mesh.m_VertexCount * 4)];
            for (var vertex = 0; vertex < mesh.m_VertexCount; vertex++) sourceWeights[vertex * 4] = 1;
            weightSource = "serialized_single_bone_layout_unit_weight";
        }
        else
        {
            if (weightChannel.Components != 4 || MeshHelper.IsIntFormat(weightChannel.Format))
                throw new InvalidDataException($"SCENE_SKIN_WEIGHT_CHANNEL_UNSUPPORTED source={asset.Source} pathId={asset.PathId} components={weightChannel.Components} format={weightChannel.Format}");
            sourceWeights = MeshHelper.BytesToFloatArray(weightChannel.Bytes, weightChannel.Format);
            weightSource = "serialized_vertex_channels_12_13_explicit_four_components_no_normalization";
        }
        var offsets = new uint[checked(mesh.m_VertexCount + 1)];
        var indices = new List<int>();
        var weights = new List<float>();
        var minimumSum = float.PositiveInfinity;
        var maximumSum = float.NegativeInfinity;
        var zeroWeightVertices = 0;
        var parserMismatchVertices = 0;
        for (var vertex = 0; vertex < mesh.m_VertexCount; vertex++)
        {
            var sum = 0f;
            var mismatch = false;
            for (var component = 0; component < 4; component++)
            {
                var position = checked(vertex * 4 + component);
                var weight = sourceWeights[position];
                var bone = sourceIndices[position];
                if (!float.IsFinite(weight) || weight < 0)
                    throw new InvalidDataException($"SCENE_SKIN_WEIGHT_INVALID source={asset.Source} pathId={asset.PathId} vertex={vertex} component={component} weight={weight}");
                if (mesh.m_Skin is not { Count: > 0 } || vertex >= mesh.m_Skin.Count || mesh.m_Skin[vertex].weight[component] != weight)
                    mismatch = true;
                sum += weight;
                if (weight == 0)
                    continue;
                if (bone < 0 || bone >= mesh.m_BindPose.Length)
                    throw new InvalidDataException($"SCENE_SKIN_BONE_INDEX_INVALID source={asset.Source} pathId={asset.PathId} vertex={vertex} bone={bone} bindPoses={mesh.m_BindPose.Length}");
                indices.Add(bone);
                weights.Add(weight);
            }
            minimumSum = Math.Min(minimumSum, sum);
            maximumSum = Math.Max(maximumSum, sum);
            if (sum == 0) zeroWeightVertices++;
            if (mismatch) parserMismatchVertices++;
            offsets[vertex + 1] = checked((uint)weights.Count);
        }
        if (fields["m_BindPose"] is not List<object> sourceBinds || sourceBinds.Count != mesh.m_BindPose.Length)
            throw new InvalidDataException($"SCENE_SKIN_BIND_POSE_FIELDS_INVALID source={asset.Source} pathId={asset.PathId}");
        var binds = sourceBinds.Select((value, bone) =>
        {
            if (value is not OrderedDictionary elements)
                throw new InvalidDataException($"SCENE_SKIN_BIND_POSE_MATRIX_INVALID source={asset.Source} pathId={asset.PathId} bone={bone}");
            var matrix = Enumerable.Range(0, 16).Select(index =>
            {
                var row = index % 4;
                var column = index / 4;
                var field = $"e{row}{column}";
                if (elements[field] is not float component || mesh.m_BindPose[bone][row * 4 + column] != component)
                    throw new InvalidDataException($"SCENE_SKIN_BIND_POSE_ELEMENT_INVALID source={asset.Source} pathId={asset.PathId} bone={bone} field={field}");
                return component;
            }).ToArray();
            RequireSkinFinite(matrix, "m_BindPose", asset);
            if (Math.Abs(matrix[3]) > 1e-5 || Math.Abs(matrix[7]) > 1e-5 || Math.Abs(matrix[11]) > 1e-5 || Math.Abs(matrix[15] - 1) > 1e-5)
                throw new InvalidDataException($"SCENE_SKIN_BIND_POSE_NOT_AFFINE source={asset.Source} pathId={asset.PathId} bone={bone}");
            return matrix;
        }).ToArray();
        var record = new SceneMeshSkinRecord(
            "EndfieldSceneMeshSkin/1", asset, "unity_mesh_local_xyz_unreflected",
            "column_major_4x4_column_vectors", weightSource,
            mesh.m_VertexCount, binds, offsets, indices.ToArray(), weights.ToArray(), bonesPerVertex,
            mesh.m_Shapes?.channels?.Count ?? 0, "serialized_type_tree_e_row_column", weightChannel.Evidence, indexChannel.Evidence,
            minimumSum, maximumSum, zeroWeightVertices, parserMismatchVertices);
        var path = Path.Combine(meshDir, UniqueFileName(mesh, ".skin.json"));
        File.WriteAllText(path, JsonSerializer.Serialize(record, JsonOptions), new UTF8Encoding(false));
        return Path.GetRelativePath(output, path);
    }

    private static SceneSkinChannel ReadSkinChannel(Mesh mesh, int channelIndex)
    {
        var data = MeshVertexDataField.GetValue(mesh) as VertexData;
        var identity = $"source={mesh.assetsFile.fileName} pathId={mesh.m_PathID} channel={channelIndex}";
        if (mesh.version[0] < 2018 || data is null || data.m_VertexCount != mesh.m_VertexCount || data.m_Channels.Count <= channelIndex)
            throw new InvalidDataException($"SCENE_SKIN_VERTEX_CHANNEL_MISSING {identity}");
        var channel = data.m_Channels[channelIndex];
        if (channel.stream >= data.m_Streams.Count)
            throw new InvalidDataException($"SCENE_SKIN_VERTEX_CHANNEL_INVALID {identity} dimension={channel.dimension} stream={channel.stream}");
        var stream = data.m_Streams[channel.stream];
        if (channel.dimension > 0 && (stream.channelMask & (1u << channelIndex)) == 0)
            throw new InvalidDataException($"SCENE_SKIN_VERTEX_CHANNEL_MASK_MISSING {identity}");
        var format = MeshHelper.ToVertexFormat(channel.format, mesh.version);
        var size = checked((int)MeshHelper.GetFormatSize(format));
        var bytes = new byte[checked(mesh.m_VertexCount * channel.dimension * size)];
        for (var vertex = 0; vertex < mesh.m_VertexCount; vertex++)
            for (var component = 0; component < channel.dimension; component++)
            {
                var source = checked((long)stream.offset + channel.offset + (long)stream.stride * vertex + size * component);
                var destination = checked((vertex * channel.dimension + component) * size);
                if (source < 0 || source + size > data.m_DataSize.LongLength)
                    throw new InvalidDataException($"SCENE_SKIN_VERTEX_CHANNEL_TRUNCATED {identity} vertex={vertex} byteOffset={source} bytes={data.m_DataSize.LongLength}");
                Buffer.BlockCopy(data.m_DataSize, checked((int)source), bytes, destination, size);
                if (mesh.reader.Endian == EndianType.BigEndian && size > 1)
                    Array.Reverse(bytes, destination, size);
            }
        return new SceneSkinChannel(bytes, format, channel.dimension,
            new SceneSkinChannelRecord(channelIndex, channel.stream, channel.offset, stream.offset, stream.stride,
                channel.format, format.ToString(), channel.dimension, mesh.reader.Endian.ToString()));
    }

    private static void RequireSkinFinite(float[] values, string field, AssetRef asset)
    {
        for (var index = 0; index < values.Length; index++)
            if (!float.IsFinite(values[index]))
                throw new InvalidDataException($"SCENE_SKIN_FLOAT_NONFINITE source={asset.Source} pathId={asset.PathId} field={field} index={index}");
    }
}

internal sealed record SceneSkinPointerRecord(int FileId, AssetRef Target, bool IsResolved);
internal sealed record SceneSkinnedRendererRecord(SceneSkinPointerRecord[] Bones, SceneSkinPointerRecord RootBone, float[] BlendShapeWeights);
internal sealed record SceneSkinChannelRecord(int Channel, int Stream, int ChannelByteOffset, uint StreamByteOffset, uint Stride,
    int SerializedFormat, string Format, int Components, string Endian);
internal sealed record SceneSkinChannel(byte[] Bytes, MeshHelper.VertexFormat Format, int Components, SceneSkinChannelRecord Evidence);
internal sealed record SceneMeshSkinRecord(string Format, AssetRef Asset, string CoordinateSpace, string MatrixConvention,
    string WeightSource, int VertexCount, float[][] BindPoses, uint[] Offsets, int[] Indices, float[] Weights, uint SerializedBonesPerVertex,
    int BlendShapeCount, string BindPoseSource, SceneSkinChannelRecord WeightChannel, SceneSkinChannelRecord IndexChannel,
    float MinimumWeightSum, float MaximumWeightSum, int ZeroWeightVertexCount, int ParserWeightMismatchVertexCount);
