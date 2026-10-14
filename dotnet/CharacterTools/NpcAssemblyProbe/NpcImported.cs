using AnimeStudio;
using System.Globalization;
using System.Security.Cryptography;
using NMatrix = System.Numerics.Matrix4x4;

namespace NpcAssemblyProbe;

internal sealed record NpcResolvedMesh(Mesh Mesh, string ExportName, string[] MaterialNames, Avatar RigAvatar,
    int RootBoneId, string RootBoneName, bool ConfiguredDisabled, string? ParentBoneTransformName,
    int SlotIndex, int LodIndex, int RendererIndex);
internal sealed record NpcPlacementAudit(string ExportName, string AvatarName, string? ParentBoneTransformName,
    int WeightedBoneCount, float[] SourceWorldMatrix, double MaximumTranslationResidual,
    double MaximumBasisResidual, string Method);
internal sealed record SourceBindWorldAudit(int Index, string Path, int ParentIndex, float[][] MatrixRows, string Provenance);
internal sealed record NpcSkeletonAudit(string AvatarName, string AvatarSource, string AvatarPathId,
    string AvatarRawDataSha256, int Bones, string[] Paths, int[] Parents, string PoseSource,
    string BindWorldCalibrationFormat, string MatrixConvention, SourceBindWorldAudit[] SourceBindWorlds,
    int MeshInverseBindConstrainedBoneCount, int AvatarLocalChainBoneCount);
internal sealed record BindWorldRecovery(Matrix4x4[] Worlds, bool[] MeshInverseBindConstrained);
internal sealed record NpcAttachmentAudit(string ParentBoneTransformName, string ChildAvatarSource,
    long ChildAvatarPathId, int RendererCount, string[] MeshNames, string LocalFramePathBefore, string LocalFramePathAfter);
internal sealed record NpcGeometryResult(IImported Model, NpcPlacementAudit[] PlacementAudit,
    NpcSkeletonAudit[] SkeletonAudit, Dictionary<string, ImportedFrame> HostBoneFrames,
    NpcAttachmentAudit[] Attachments);

/// <summary>Connects parsed source geometry to the shared imported-model contract.</summary>
internal sealed class NpcImported : IImported
{
    public required ImportedFrame RootFrame { get; init; }
    public List<ImportedMesh> MeshList { get; } = new();
    public List<ImportedMaterial> MaterialList { get; } = new();
    public List<ImportedTexture> TextureList { get; } = new();
    public List<ImportedKeyframedAnimation> AnimationList { get; } = new();
    public List<ImportedMorph> MorphList { get; } = new();
}

internal static class NpcGeometry
{
    internal static NpcGeometryResult Build(Avatar hostAvatar, IReadOnlyList<NpcResolvedMesh> meshes, double tolerance)
    {
        var hostMeshes = meshes.Where(row => row.ParentBoneTransformName == null).ToArray();
        if (hostMeshes.Length == 0)
            throw new InvalidDataException("Grouped NPC assembly requires at least one non-attached source slot as its host Avatar.");
        var host = BuildSingle(hostAvatar, hostMeshes, tolerance);
        var destination = (NpcImported)host.Model;
        var placements = host.PlacementAudit.ToList();
        var skeletons = host.SkeletonAudit.ToList();
        var attachments = new List<NpcAttachmentAudit>();
        var materialNames = destination.MaterialList.Select(material => material.Name).ToHashSet(StringComparer.Ordinal);

        var attachedGroups = meshes.Where(row => row.ParentBoneTransformName != null)
            .GroupBy(row => (Source: row.RigAvatar.assetsFile.fileName, PathId: row.RigAvatar.m_PathID,
                ParentPath: row.ParentBoneTransformName!));
        foreach (var group in attachedGroups)
        {
            var rows = group.ToArray();
            if (!host.HostBoneFrames.TryGetValue(group.Key.ParentPath, out var parentFrame))
                throw new InvalidDataException($"Self-avatar parent transform is absent from the host skeleton: parent={group.Key.ParentPath}.");
            var sourceAvatar = rows[0].RigAvatar;
            if (rows.Any(row => !ReferenceEquals(row.RigAvatar, sourceAvatar) || row.ParentBoneTransformName != group.Key.ParentPath))
                throw new InvalidDataException($"Self-avatar attachment group mixes source rigs or parent paths: parent={group.Key.ParentPath}.");
            var child = BuildSingle(sourceAvatar, rows, tolerance);
            var childModel = (NpcImported)child.Model;
            var oldRootPath = childModel.RootFrame.Path;
            childModel.RootFrame.Name = $"NPC_Root_{unchecked((ulong)sourceAvatar.m_PathID):x16}";
            parentFrame.AddChild(childModel.RootFrame);
            var newRootPath = childModel.RootFrame.Path;
            RebaseImportedPaths(childModel, oldRootPath, newRootPath);
            destination.MeshList.AddRange(childModel.MeshList);
            foreach (var material in childModel.MaterialList)
                if (materialNames.Add(material.Name)) destination.MaterialList.Add(material);
            destination.TextureList.AddRange(childModel.TextureList);
            destination.AnimationList.AddRange(childModel.AnimationList);
            destination.MorphList.AddRange(childModel.MorphList);
            placements.AddRange(child.PlacementAudit);
            skeletons.AddRange(child.SkeletonAudit);
            attachments.Add(new(group.Key.ParentPath, sourceAvatar.assetsFile.fileName, sourceAvatar.m_PathID,
                rows.Length, rows.Select(row => row.ExportName).ToArray(), oldRootPath, newRootPath));
        }

        return new(destination, placements.ToArray(), skeletons.ToArray(), host.HostBoneFrames, attachments.ToArray());
    }

    private static void RebaseImportedPaths(NpcImported model, string oldRootPath, string newRootPath)
    {
        string Rebase(string path)
        {
            if (path == oldRootPath) return newRootPath;
            if (path.StartsWith(oldRootPath + "/", StringComparison.Ordinal))
                return newRootPath + path[oldRootPath.Length..];
            throw new InvalidDataException($"Attached self-avatar path is outside its source rig root: root={oldRootPath}, path={path}.");
        }
        foreach (var mesh in model.MeshList)
        {
            mesh.Path = Rebase(mesh.Path);
            foreach (var bone in mesh.BoneList) bone.Path = Rebase(bone.Path);
        }
        foreach (var morph in model.MorphList) morph.Path = Rebase(morph.Path);
    }

    private static NpcGeometryResult BuildSingle(Avatar avatar, IReadOnlyList<NpcResolvedMesh> meshes, double tolerance)
    {
        if (!double.IsFinite(tolerance) || tolerance <= 0 || meshes.Count == 0)
            throw new InvalidDataException("NPC geometry requires positive tolerance and resolved source meshes.");
        if (meshes.Any(row => !ReferenceEquals(row.RigAvatar, avatar)))
            throw new InvalidDataException("NPC geometry group mixes meshes from different source Avatar objects.");
        var skeleton = avatar.m_Avatar.m_AvatarSkeleton;
        var poses = avatar.m_Avatar.m_DefaultPose.m_X;
        var ids = skeleton.m_ID;
        if (ids.Length != poses.Length || ids.Length != skeleton.m_Node.Count || ids.Length == 0)
            throw new InvalidDataException("Avatar skeleton ID, node and source default-pose arrays disagree.");
        var paths = ids.Select(id => avatar.FindBonePath(id) ?? throw new InvalidDataException($"Avatar hash has no path: {id}")).ToArray();
        if (paths.Distinct().Count() != paths.Length || paths[0] != "")
            throw new InvalidDataException("Avatar source paths must be unique and start with the empty root sentinel.");
        var parents = skeleton.m_Node.Select(node => node.m_ParentId).ToArray();
        var sourceWorld = new Matrix4x4[ids.Length];
        var frames = new ImportedFrame[ids.Length];
        for (var i = 0; i < ids.Length; i++)
        {
            if ((i == 0 && parents[i] != -1) || (i > 0 && (parents[i] < 0 || parents[i] >= i)))
                throw new InvalidDataException($"Avatar hierarchy is not source preorder: node={i}, parent={parents[i]}");
            var pose = poses[i];
            var local = Matrix4x4.Translate(pose.t) * Matrix4x4.Rotate(pose.q) * Matrix4x4.Scale(pose.s);
            sourceWorld[i] = i == 0 ? local : sourceWorld[parents[i]] * local;
            var name = i == 0 ? "NPC_Root" : paths[i].Split('/').Last();
            frames[i] = Frame(name, pose.t, pose.q, pose.s);
            if (i > 0)
            {
                var parentPath = paths[i].Contains('/') ? paths[i][..paths[i].LastIndexOf('/')] : "";
                if (parentPath != paths[parents[i]])
                    throw new InvalidDataException($"Avatar path and node parent disagree: {paths[i]}");
                frames[parents[i]].AddChild(frames[i]);
            }
        }
        var indexByPath = paths.Select((path, index) => (path, index)).ToDictionary(row => row.path, row => row.index);
        var bindRecovery = RecoverBindWorlds(avatar, meshes, sourceWorld, parents, indexByPath, tolerance);
        sourceWorld = bindRecovery.Worlds;
        var sourceBindWorlds = paths.Select((path, index) => new SourceBindWorldAudit(index, path, parents[index],
            MatrixRows(sourceWorld[index]), bindRecovery.MeshInverseBindConstrained[index]
                ? "mesh_inverse_bind_constraint_graph" : "avatar_default_pose_local_chain_unconstrained")).ToArray();
        for (var i = 0; i < frames.Length; i++)
        {
            var local = i == 0 ? sourceWorld[i] : Inverse(sourceWorld[parents[i]]) * sourceWorld[i];
            var recovered = MatrixFrame(frames[i].Name, local, tolerance);
            frames[i].LocalPosition = recovered.LocalPosition;
            frames[i].LocalRotation = recovered.LocalRotation;
            frames[i].LocalScale = recovered.LocalScale;
        }
        var model = new NpcImported { RootFrame = frames[0] };
        var placements = new List<NpcPlacementAudit>();
        var materialNames = new HashSet<string>();
        foreach (var row in meshes)
        {
            var mesh = row.Mesh;
            if (row.RootBoneId < 0 || row.RootBoneId >= paths.Length || paths[row.RootBoneId].Split('/').Last() != row.RootBoneName)
                throw new InvalidDataException($"Configured root bone does not match source skeleton: {row.ExportName}/{row.RootBoneId}/{row.RootBoneName}");
            if (mesh.m_BindPose.Length != mesh.m_BoneNameHashes.Length || mesh.m_BindPose.Length == 0 || mesh.m_Skin.Count != mesh.m_VertexCount)
                throw new InvalidDataException($"Source skin/bindpose arrays are incomplete: {row.ExportName}");
            var bonePaths = mesh.m_BoneNameHashes.Select(hash => avatar.FindBonePath(hash) ?? throw new InvalidDataException($"Unresolved weighted Avatar hash: {row.ExportName}/{hash}")).ToArray();
            var weighted = new HashSet<int>();
            foreach (var skin in mesh.m_Skin)
            {
                var sum = 0d;
                for (var k = 0; k < skin.weight.Length; k++)
                {
                    var weight = skin.weight[k];
                    if (!float.IsFinite(weight) || weight < 0) throw new InvalidDataException($"Invalid source skin weight: {row.ExportName}");
                    sum += weight;
                    if (weight == 0) continue;
                    var index = skin.boneIndex[k];
                    if (index < 0 || index >= bonePaths.Length || !indexByPath.ContainsKey(bonePaths[index]))
                        throw new InvalidDataException($"Source skin index/path unresolved: {row.ExportName}/{index}");
                    weighted.Add(index);
                }
                if (Math.Abs(sum - 1d) > 1e-4) throw new InvalidDataException($"Non-unit source weights: {row.ExportName}, sum={sum}");
            }
            var candidates = weighted.Order().Select(i => sourceWorld[indexByPath[bonePaths[i]]] * Transpose(mesh.m_BindPose[i])).ToArray();
            if (candidates.Length == 0) throw new InvalidDataException($"No weighted source bones: {row.ExportName}");
            var placement = candidates[0];
            var translationResidual = candidates.Max(matrix => Math.Sqrt(Enumerable.Range(0, 3).Sum(i => Math.Pow(matrix[i, 3] - placement[i, 3], 2))));
            var basisResidual = candidates.Max(matrix => Enumerable.Range(0, 16).Where(i => i != 12 && i != 13 && i != 14).Max(i => Math.Abs((double)matrix[i] - placement[i])));
            if (translationResidual > tolerance || basisResidual > tolerance)
                throw new InvalidDataException($"Source bindpose placement consensus failed: mesh={row.ExportName}, translation={translationResidual}, basis={basisResidual}, tolerance={tolerance}");
            var localPlacement = Inverse(sourceWorld[0]) * placement;
            var meshFrame = MatrixFrame(row.ExportName, localPlacement, tolerance);
            model.RootFrame.AddChild(meshFrame);
            var imported = ConvertMesh(row, meshFrame.Path, bonePaths.Select(path => frames[indexByPath[path]].Path).ToArray());
            model.MeshList.Add(imported);
            foreach (var name in row.MaterialNames.Where(materialNames.Add))
                model.MaterialList.Add(new ImportedMaterial { Name = name, Diffuse = new Color(.5f, .5f, .5f, 1), Textures = new List<ImportedMaterialTexture>() });
            var morph = ConvertMorph(mesh, imported);
            if (morph is not null) model.MorphList.Add(morph);
            placements.Add(new(row.ExportName, avatar.Name, row.ParentBoneTransformName, weighted.Count,
                Enumerable.Range(0, 16).Select(i => placement[i]).ToArray(), translationResidual, basisResidual,
                "Source inverse-bind constraint graph anchored to configured rootBone in its exact Avatar coordinates; disconnected components use verified Avatar local bone chains."));
        }
        var boneFrames = paths.Select((path, index) => (path, frame: frames[index]))
            .ToDictionary(row => row.path, row => row.frame, StringComparer.Ordinal);
        return new(model, placements.ToArray(),
            new[] { new NpcSkeletonAudit(avatar.Name, avatar.assetsFile.fileName,
                avatar.m_PathID.ToString(CultureInfo.InvariantCulture),
                Convert.ToHexString(SHA256.HashData(avatar.GetRawData())).ToLowerInvariant(), ids.Length, paths, parents,
                "Source Mesh inverse-bind rest worlds; exact Avatar hierarchy and default local transforms retained for unconstrained bones.",
                "EndfieldNpcBindWorldCalibration/1",
                "4x4 row arrays in Unity source coordinates; column-vector transforms; source hierarchy uses parentWorld * local, local=T*R*S.",
                sourceBindWorlds, bindRecovery.MeshInverseBindConstrained.Count(value => value),
                bindRecovery.MeshInverseBindConstrained.Count(value => !value)) },
            boneFrames, Array.Empty<NpcAttachmentAudit>());
    }

    private static BindWorldRecovery RecoverBindWorlds(Avatar avatar, IReadOnlyList<NpcResolvedMesh> meshes,
        Matrix4x4[] defaults, int[] parents, IReadOnlyDictionary<string, int> indices, double tolerance)
    {
        var knownBones = new Dictionary<int, Matrix4x4>();
        var knownMeshes = new Dictionary<int, Matrix4x4>();
        var pending = Enumerable.Range(0, meshes.Count).ToHashSet();
        var bindings = meshes.Select(row => row.Mesh.m_BoneNameHashes.Select((hash, i) =>
            (Bone: indices[avatar.FindBonePath(hash) ?? throw new InvalidDataException($"Unresolved source bone hash: {hash}")],
             Bind: Transpose(row.Mesh.m_BindPose[i]))).ToArray()).ToArray();
        var anchors = bindings[0].Where(row => row.Bone == meshes[0].RootBoneId).ToArray();
        if (anchors.Length != 1)
            throw new InvalidDataException("First configured mesh requires one exact rootBone inverse-bind anchor for Avatar coordinate conversion.");
        knownMeshes.Add(0, defaults[meshes[0].RootBoneId] * anchors[0].Bind);
        while (pending.Count > 0)
        {
            var progressed = false;
            foreach (var meshIndex in pending.ToArray())
            {
                if (!knownMeshes.ContainsKey(meshIndex))
                {
                    var knownAnchors = bindings[meshIndex].Where(row => knownBones.ContainsKey(row.Bone)).ToArray();
                    if (knownAnchors.Length == 0) continue;
                    knownMeshes.Add(meshIndex, knownBones[knownAnchors[0].Bone] * knownAnchors[0].Bind);
                }
                foreach (var row in bindings[meshIndex])
                {
                    var boneWorld = knownMeshes[meshIndex] * Inverse(row.Bind);
                    if (knownBones.TryGetValue(row.Bone, out var prior))
                    {
                        var residual = Enumerable.Range(0, 16).Max(i => Math.Abs(prior[i] - boneWorld[i]));
                        if (residual > tolerance)
                            throw new InvalidDataException($"Source inverse-bind graph disagrees: mesh={meshes[meshIndex].ExportName}, bone={row.Bone}, residual={residual}");
                    }
                    else knownBones.Add(row.Bone, boneWorld);
                }
                pending.Remove(meshIndex);
                progressed = true;
            }
            if (!progressed) break;
        }
        var result = new Matrix4x4[defaults.Length];
        for (var i = 0; i < result.Length; i++)
        {
            var local = parents[i] < 0 ? defaults[i] : Inverse(defaults[parents[i]]) * defaults[i];
            result[i] = knownBones.TryGetValue(i, out var source) ? source : parents[i] < 0 ? local : result[parents[i]] * local;
        }
        foreach (var meshIndex in pending)
        {
            var candidates = bindings[meshIndex].Select(row => result[row.Bone] * row.Bind).ToArray();
            if (candidates.Length == 0) throw new InvalidDataException($"Disconnected source mesh has no bones: {meshes[meshIndex].ExportName}");
            var residual = candidates.Max(matrix => Enumerable.Range(0, 16).Max(i => Math.Abs(matrix[i] - candidates[0][i])));
            if (residual > tolerance)
                throw new InvalidDataException($"Disconnected source mesh cannot be placed by Avatar local chain: mesh={meshes[meshIndex].ExportName}, residual={residual}");
        }
        return new(result, Enumerable.Range(0, result.Length).Select(knownBones.ContainsKey).ToArray());
    }

    private static float[][] MatrixRows(Matrix4x4 matrix) => Enumerable.Range(0, 4)
        .Select(row => Enumerable.Range(0, 4).Select(column => matrix[row, column]).ToArray()).ToArray();

    private static Matrix4x4 Transpose(Matrix4x4 source)
    {
        var result = new Matrix4x4();
        for (var row = 0; row < 4; row++)
            for (var column = 0; column < 4; column++) result[row, column] = source[column, row];
        return result;
    }

    private static ImportedFrame Frame(string name, Vector3 t, Quaternion q, Vector3 s) => new(0)
    { Name = name, LocalPosition = new Vector3(-t.X, t.Y, t.Z), LocalRotation = new Quaternion(q.X, -q.Y, -q.Z, q.W), LocalScale = s };

    private static NMatrix Numeric(Matrix4x4 m) => new(m[0], m[1], m[2], m[3], m[4], m[5], m[6], m[7], m[8], m[9], m[10], m[11], m[12], m[13], m[14], m[15]);

    private static Matrix4x4 Inverse(Matrix4x4 matrix)
    {
        if (!NMatrix.Invert(Numeric(matrix), out var inverse)) throw new InvalidDataException("Singular source root transform.");
        var values = new[] { inverse.M11, inverse.M12, inverse.M13, inverse.M14, inverse.M21, inverse.M22, inverse.M23, inverse.M24, inverse.M31, inverse.M32, inverse.M33, inverse.M34, inverse.M41, inverse.M42, inverse.M43, inverse.M44 };
        var result = new Matrix4x4();
        for (var i = 0; i < 16; i++) result[i] = values[i];
        return result;
    }

    private static ImportedFrame MatrixFrame(string name, Matrix4x4 matrix, double tolerance)
    {
        if (!NMatrix.Decompose(Numeric(matrix), out var s, out var q, out var t)) throw new InvalidDataException($"Source mesh placement has no TRS decomposition: {name}");
        var position = new Vector3(t.X, t.Y, t.Z);
        var rotation = new Quaternion(q.X, q.Y, q.Z, q.W);
        var scale = new Vector3(s.X, s.Y, s.Z);
        var rebuilt = Matrix4x4.Translate(position) * Matrix4x4.Rotate(rotation) * Matrix4x4.Scale(scale);
        if (Enumerable.Range(0, 16).Any(i => !float.IsFinite(matrix[i]) || Math.Abs(matrix[i] - rebuilt[i]) > tolerance))
            throw new InvalidDataException($"Source mesh placement contains unsupported shear: {name}");
        return Frame(name, position, rotation, scale);
    }

    private static int Stride(float[] values, int count, string label)
    {
        if (count == 0 || values.Length % count != 0 || values.Length / count < 2 || values.Length / count > 4)
            throw new InvalidDataException($"Source vertex channel has invalid stride: {label}/{values.Length}/{count}");
        return values.Length / count;
    }

    private static ImportedMesh ConvertMesh(NpcResolvedMesh row, string path, string[] bones)
    {
        var source = row.Mesh;
        var count = checked((int)source.m_VertexCount);
        var stride = Stride(source.m_Vertices, count, "position");
        if (stride < 3 || row.MaterialNames.Length < source.m_SubMeshes.Count) throw new InvalidDataException($"Incomplete source geometry/material slots: {row.ExportName}");
        var result = new ImportedMesh { Path = path, VertexList = new(), SubmeshList = new(), BoneList = new(),
            hasUV = Enumerable.Range(0, 8).Select(i => source.GetUV(i)?.Length > 0).ToArray(), uvType = new int[8],
            hasNormal = source.m_Normals?.Length > 0, hasTangent = source.m_Tangents?.Length > 0, hasColor = source.m_Colors?.Length > 0 };
        var normalStride = result.hasNormal ? Stride(source.m_Normals!, count, "normal") : 0;
        var colorStride = result.hasColor ? Stride(source.m_Colors!, count, "color") : 0;
        if ((result.hasNormal && normalStride < 3) || (result.hasColor && colorStride < 3) || (result.hasTangent && source.m_Tangents!.Length != count * 4))
            throw new InvalidDataException($"Unsupported source normal/color/tangent channel: {row.ExportName}");
        for (var i = 0; i < count; i++)
        {
            var skin = source.m_Skin[i];
            var vertex = new ImportedVertex { Vertex = new Vector3(-source.m_Vertices[i * stride], source.m_Vertices[i * stride + 1], source.m_Vertices[i * stride + 2]),
                BoneIndices = skin.boneIndex.ToArray(), Weights = skin.weight.ToArray(), UV = new float[8][] };
            if (result.hasNormal) vertex.Normal = new Vector3(-source.m_Normals![i * normalStride], source.m_Normals[i * normalStride + 1], source.m_Normals[i * normalStride + 2]);
            if (result.hasTangent) vertex.Tangent = new Vector4(-source.m_Tangents![i * 4], source.m_Tangents[i * 4 + 1], source.m_Tangents[i * 4 + 2], source.m_Tangents[i * 4 + 3]);
            if (result.hasColor) vertex.Color = new Color(source.m_Colors![i * colorStride], source.m_Colors[i * colorStride + 1], source.m_Colors[i * colorStride + 2], colorStride == 4 ? source.m_Colors[i * colorStride + 3] : 1f);
            for (var channel = 0; channel < 8; channel++)
                if (result.hasUV[channel])
                {
                    var uv = source.GetUV(channel);
                    var uvStride = Stride(uv, count, $"UV{channel}");
                    vertex.UV[channel] = new[] { uv[i * uvStride], uv[i * uvStride + 1] };
                }
            result.VertexList.Add(vertex);
        }
        var faceOffset = 0;
        for (var i = 0; i < source.m_SubMeshes.Count; i++)
        {
            var sub = source.m_SubMeshes[i];
            if (sub.indexCount % 3 != 0) throw new InvalidDataException($"Nontriangular source submesh: {row.ExportName}/{i}");
            var imported = new ImportedSubmesh { Material = row.MaterialNames[i], BaseVertex = checked((int)sub.firstVertex), FaceList = new() };
            for (var j = 0; j < sub.indexCount; j += 3)
            {
                var indices = new[] { 2, 1, 0 }.Select(k => checked((int)source.m_Indices[faceOffset + j + k])).ToArray();
                if (indices.Any(index => index < 0 || index >= count)) throw new InvalidDataException($"Invalid source triangle: {row.ExportName}/{i}");
                imported.FaceList.Add(new ImportedFace { VertexIndices = indices.Select(index => index - imported.BaseVertex).ToArray() });
            }
            faceOffset += checked((int)sub.indexCount);
            result.SubmeshList.Add(imported);
        }
        if (faceOffset != source.m_Indices.Count) throw new InvalidDataException($"Source triangle coverage mismatch: {row.ExportName}");
        var reflect = Matrix4x4.Scale(new Vector3(-1, 1, 1));
        for (var i = 0; i < bones.Length; i++) result.BoneList.Add(new ImportedBone { Path = bones[i], Matrix = reflect * source.m_BindPose[i] * reflect });
        return result;
    }

    private static ImportedMorph? ConvertMorph(Mesh mesh, ImportedMesh imported)
    {
        if (mesh.m_Shapes?.channels?.Count is not > 0) return null;
        var result = new ImportedMorph { Path = imported.Path, Channels = new() };
        foreach (var channel in mesh.m_Shapes.channels)
        {
            var target = new ImportedMorphChannel { Name = channel.name.Split('.').Last(), KeyframeList = new() };
            for (var index = channel.frameIndex; index < channel.frameIndex + channel.frameCount; index++)
            {
                var shape = mesh.m_Shapes.shapes[index];
                var frame = new ImportedMorphKeyframe { Weight = mesh.m_Shapes.fullWeights[index], hasNormals = shape.hasNormals, hasTangents = shape.hasTangents, VertexList = new() };
                for (var j = checked((int)shape.firstVertex); j < shape.firstVertex + shape.vertexCount; j++)
                {
                    var source = mesh.m_Shapes.vertices[j];
                    var vertex = new ImportedVertex { Vertex = imported.VertexList[checked((int)source.index)].Vertex + new Vector3(-source.vertex.X, source.vertex.Y, source.vertex.Z) };
                    if (shape.hasNormals) vertex.Normal = new Vector3(-source.normal.X, source.normal.Y, source.normal.Z);
                    if (shape.hasTangents) vertex.Tangent = new Vector4(-source.tangent.X, source.tangent.Y, source.tangent.Z, 0);
                    frame.VertexList.Add(new ImportedMorphVertex { Index = source.index, Vertex = vertex });
                }
                target.KeyframeList.Add(frame);
            }
            result.Channels.Add(target);
        }
        return result;
    }
}
