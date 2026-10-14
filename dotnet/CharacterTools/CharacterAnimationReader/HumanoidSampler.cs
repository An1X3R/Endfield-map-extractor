using System.Numerics;
using System.Text.Json;
using System.Text.Json.Serialization;
using AnimeStudio;
using Ruri.RipperHook.Humanoid;
using Quat = System.Numerics.Quaternion;
using Vec = System.Numerics.Vector3;

internal sealed record HumanoidScalarBinding([property: JsonPropertyName("track")] int Track, [property: JsonPropertyName("custom_type")] int CustomType, [property: JsonPropertyName("attribute_name")] string AttributeName);
internal sealed record ScalarFrame([property: JsonPropertyName("time")] float Time, [property: JsonPropertyName("values")] float[] Values);
internal sealed record NodePose(int Node, string Path, float[] Position, float[] Rotation);
internal sealed record BodyFrame(float Time, NodePose[] Nodes, float[] MotionPosition, float[] MotionRotation);
internal sealed record BoneMetric(int Slot, int Node, string Path, string?[] Muscles, int[] Tracks, float NeutralRotationErrorDegrees, float MotionRangeDegrees);

internal static class HumanoidProgram
{
    private static readonly JsonSerializerOptions JsonOptions = new() { PropertyNamingPolicy = JsonNamingPolicy.SnakeCaseLower, WriteIndented = true };
    private static Vec Vector(AnimeStudio.Vector3 value) => new(value.X, value.Y, value.Z);
    private static Vec RequireVector(object value) => value is AnimeStudio.Vector3 vector ? Vector(vector) : throw new InvalidDataException($"Expected Vector3, got {value.GetType().FullName}.");
    private static Quat Rotation(AnimeStudio.Vector4 value) => new(value.X, value.Y, value.Z, value.W);
    private static Quat Rotation(AnimeStudio.Quaternion value) => new(value.X, value.Y, value.Z, value.W);
    private static float[] Array(Vec value) => [value.X, value.Y, value.Z];
    private static float[] Array(Quat value) => [value.X, value.Y, value.Z, value.W];
    private static float Angle(Quat first, Quat second)
    {
        Quat delta = Quat.Normalize(Quat.Inverse(first) * second);
        return 2f * MathF.Atan2(new Vec(delta.X, delta.Y, delta.Z).Length(), MathF.Abs(delta.W)) * 180f / MathF.PI;
    }
    private static void RequireRotation(Quat rotation, string context)
    {
        float norm = rotation.LengthSquared();
        if (!float.IsFinite(norm) || MathF.Abs(norm - 1f) > 2e-5f) throw new InvalidDataException($"Invalid unit rotation: {context}, normSquared={norm}.");
    }
    private static void RequirePosition(Vec position, string context)
    {
        if (!float.IsFinite(position.X) || !float.IsFinite(position.Y) || !float.IsFinite(position.Z)) throw new InvalidDataException($"Invalid finite position: {context}.");
    }
    private static AvatarRigInput Rig(Avatar avatar)
    {
        var human = avatar.m_Avatar.m_Human;
        var skeleton = human.m_Skeleton;
        int count = skeleton.m_Node.Count;
        if (count != skeleton.m_ID.Length || count != human.m_SkeletonPose.m_X.Length || human.m_HumanBoneIndex.Length != 25 || human.m_HumanBoneMass.Length != 25) throw new InvalidDataException("Inconsistent Avatar Human array dimensions.");
        for (int node = 0; node < count; node++)
        {
            if (skeleton.m_Node[node].m_ParentId >= node) throw new InvalidDataException($"Avatar Human hierarchy is not topologically ordered at {node}.");
            if (Vec.Distance(Vector(human.m_SkeletonPose.m_X[node].s), Vec.One) > 2e-5f) throw new InvalidDataException($"Non-unit Human pose scale at {node}.");
            RequireRotation(Rotation(human.m_SkeletonPose.m_X[node].q), $"reference node {node}");
        }
        foreach (int index in human.m_HumanBoneIndex.Concat(human.m_LeftHand.m_HandBoneIndex).Concat(human.m_RightHand.m_HandBoneIndex))
            if (index < -1 || index >= count) throw new InvalidDataException($"Invalid Human bone index {index}.");
        return new AvatarRigInput
        {
            NodeParent = skeleton.m_Node.Select(node => node.m_ParentId).ToArray(),
            NodeAxesId = skeleton.m_Node.Select(node => node.m_AxesId).ToArray(),
            NodeId = skeleton.m_ID.ToArray(),
            Axes = skeleton.m_AxesArray.Select(axis => new AvatarRigInput.AxesRow(Rotation(axis.m_PreQ), Rotation(axis.m_PostQ), RequireVector(axis.m_Sgn), RequireVector(axis.m_Limit.m_Min), RequireVector(axis.m_Limit.m_Max))).ToArray(),
            Tos = new Dictionary<uint, string>(avatar.m_TOS),
            HumanBoneIndex = human.m_HumanBoneIndex.ToArray(),
            LeftHandBoneIndex = human.m_LeftHand.m_HandBoneIndex.ToArray(),
            RightHandBoneIndex = human.m_RightHand.m_HandBoneIndex.ToArray(),
            HumanBoneMass = human.m_HumanBoneMass.ToArray(),
            RootRestQ = Rotation(human.m_RootX.q),
            ArmTwist = human.m_ArmTwist,
            ForeArmTwist = human.m_ForeArmTwist,
            UpperLegTwist = human.m_UpperLegTwist,
            LegTwist = human.m_LegTwist,
            SkeletonPose = human.m_SkeletonPose.m_X.Select(pose => (Vector(pose.t), Rotation(pose.q))).ToArray()
        };
    }
    private static (Vec[] Positions, Quat[] Rotations) ForwardKinematics(AvatarRigInput rig, Vec[] translations, Quat[] rotations)
    {
        var worldT = new Vec[rig.NodeParent.Length];
        var worldQ = new Quat[rig.NodeParent.Length];
        for (int node = 0; node < worldT.Length; node++)
        {
            int parent = rig.NodeParent[node];
            worldT[node] = parent < 0 ? translations[node] : worldT[parent] + Vec.Transform(translations[node], worldQ[parent]);
            worldQ[node] = parent < 0 ? rotations[node] : Quat.Normalize(worldQ[parent] * rotations[node]);
            RequirePosition(worldT[node], $"world node {node}");
            RequireRotation(worldQ[node], $"world node {node}");
        }
        return (worldT, worldQ);
    }
    private static void Write<T>(string output, string filename, T value) => File.WriteAllText(Path.Combine(output, filename), JsonSerializer.Serialize(value, JsonOptions));
    private static float Component(Vec vector, int axis) => axis switch { 0 => vector.X, 1 => vector.Y, 2 => vector.Z, _ => throw new ArgumentOutOfRangeException(nameof(axis)) };
    private static float[] ReferenceMuscles(AvatarMuscleReferential solver, AvatarRigInput rig, int scalarCount)
    {
        var values = new float[scalarCount];
        foreach (MuscleBone bone in solver.DrivenBones.Where(bone => !bone.IsHips))
        {
            Quat swing = Quat.Normalize(Quat.Inverse(bone.PreQ) * rig.SkeletonPose[bone.NodeIndex].Q * bone.PostQ);
            float tx = swing.X / swing.W;
            float y = swing.Y / swing.W;
            float z = swing.Z / swing.W;
            float ty = (y - tx * z) / (1f + tx * tx);
            float tz = (z + tx * y) / (1f + tx * tx);
            float[] angles = [2f * MathF.Atan(tx), 2f * MathF.Atan(ty), 2f * MathF.Atan(tz)];
            for (int axis = 0; axis < 3; axis++)
            {
                int channel = bone.DofChannel[axis];
                if (channel < 0) continue;
                float angle = angles[axis] / Component(bone.Sgn, axis);
                float denominator = angle >= 0f ? Component(bone.LimitMax, axis) : -Component(bone.LimitMin, axis);
                if (MathF.Abs(denominator) < 1e-6f && MathF.Abs(angle) > 1e-5f) throw new InvalidDataException($"Reference rotation outside zero-range DOF: {bone.Path}, axis={axis}.");
                values[channel] = MathF.Abs(denominator) < 1e-6f ? 0f : angle / denominator;
                if (!float.IsFinite(values[channel])) throw new InvalidDataException($"Invalid inverse reference muscle: {bone.Path}, axis={axis}.");
            }
        }
        return values;
    }
    internal static int Run(AnimationRequest request)
    {
        
        string output = Path.GetFullPath(Path.Combine(request.Output, "humanoid"));
        if (Directory.Exists(output)) throw new IOException($"Output already exists: {output}.");
        Directory.CreateDirectory(output);
        HumanoidScalarBinding[] bindings = JsonSerializer.Deserialize<HumanoidScalarBinding[]>(File.ReadAllText(Path.Combine(Path.Combine(request.Output, "native"), "scalar_bindings.json"))) ?? throw new InvalidDataException("Scalar bindings missing.");
        ScalarFrame[] frames = AnimationFrameJson.Read<ScalarFrame[]>(Path.Combine(request.Output, "native", "float_samples.json"), JsonSerializerOptions.Default);
        CommonFrameDomain frameDomain = JsonSerializer.Deserialize<CommonFrameDomain>(File.ReadAllText(Path.Combine(Path.Combine(request.Output, "native"), "frame_domain_verification.json")), JsonOptions) ?? throw new InvalidDataException("Common frame-domain verification missing.");
        if (frames.Length != frameDomain.Float.OriginalFrameCount || frameDomain.CommonFrameCount <= 0 || frameDomain.CommonFrameCount > frames.Length) throw new InvalidDataException($"Float buffer count disagrees with verified frame domain: samples={frames.Length}, reported={frameDomain.Float.OriginalFrameCount}, common={frameDomain.CommonFrameCount}.");
        if (frames.Any(frame => !float.IsFinite(frame.Time)) || MathF.Abs(frames[0].Time - frameDomain.Float.SourceStartTime) > frameDomain.TimestampToleranceSeconds || MathF.Abs(frames[frameDomain.CommonFrameCount - 1].Time - frameDomain.ExportedEndTime) > frameDomain.TimestampToleranceSeconds) throw new InvalidDataException("Float samples do not match the verified common export range.");
        frames = frames.Take(frameDomain.CommonFrameCount).ToArray();
        if (bindings.Length == 0 || frames.Length == 0 || frames.Any(frame => frame.Values.Length != bindings.Length || frame.Values.Any(value => !float.IsFinite(value)))) throw new InvalidDataException("Expected nonempty finite frames matching the actual scalar track count.");
        var logger = new DiagnosticLogger();
        Logger.Default = logger;
        Logger.Flags = LoggerEvent.Warning | LoggerEvent.Error;
        TypeFlags.SetTypes(new[] { ClassIDType.Avatar, ClassIDType.AnimationClip, ClassIDType.AssetBundle }.ToDictionary(type => type, type => (true, false)));
        var manager = new AssetsManager { Game = GameManager.GetGameByType(GameType.ArknightsEndfield), ResolveDependencies = false, SpecifyUnityVersion = request.UnityVersion, Silent = true, SkipProcess = false };
        try
        {
            manager.LoadPreparedFiles([Path.GetFullPath(request.AvatarBundle), Path.GetFullPath(request.ClipBundle)]);
            logger.RequireSuccess();
            Avatar avatar = manager.assetsFileList.SelectMany(file => file.Objects.OfType<Avatar>()).Single(value => value.m_PathID == request.AvatarPathId && value.assetsFile.fileName.Equals(request.AvatarCab, StringComparison.OrdinalIgnoreCase));
            AnimationClip clip = manager.assetsFileList.SelectMany(file => file.Objects.OfType<AnimationClip>()).Single(value => value.assetsFile.Objects.OfType<AssetBundle>().Any(bundle => bundle.m_Container.Any(entry => entry.Key.Equals(request.ResourcePath, StringComparison.OrdinalIgnoreCase) && entry.Value.asset.TryGet(out var asset) && ReferenceEquals(asset, value))));
            AvatarRigInput rig = Rig(avatar);
            AvatarMuscleReferential solver = AvatarMuscleReferential.TryCreate(rig) ?? throw new InvalidDataException("Real Avatar did not produce muscle referential.");
            Dictionary<string, int> channelIndex = bindings.Where(binding => binding.CustomType == 8).ToDictionary(binding => binding.AttributeName, binding => binding.Track, StringComparer.Ordinal);
            RootChannelPlan root = solver.BindClip(channelIndex);
            if (solver.DrivenBones.Count == 0 || solver.Hips is null || !root.HasRootTranslation) throw new InvalidDataException("Expected a nonempty real Human rig with hips and root translation channels.");
            var zeroQuats = new Quat[AvatarMuscleReferential.SlotCount];
            var zeroDriven = new bool[AvatarMuscleReferential.SlotCount];
            solver.BodyLocalQuats(new float[bindings.Length], zeroQuats, zeroDriven);
            float[] referenceMuscles = ReferenceMuscles(solver, rig, bindings.Length);
            var referenceQuats = new Quat[AvatarMuscleReferential.SlotCount];
            var referenceDriven = new bool[AvatarMuscleReferential.SlotCount];
            solver.BodyLocalQuats(referenceMuscles, referenceQuats, referenceDriven);
            var referenceChecks = solver.DrivenBones.Where(bone => !bone.IsHips).Select(bone => new { bone.Slot, bone.Path, zero_muscle_error_degrees = Angle(zeroQuats[bone.Slot], rig.SkeletonPose[bone.NodeIndex].Q), recovered_reference_error_degrees = Angle(referenceQuats[bone.Slot], rig.SkeletonPose[bone.NodeIndex].Q), reference_muscles = bone.DofChannel.Select(index => index < 0 ? (float?)null : referenceMuscles[index]).ToArray() }).ToArray();
            float maximumReferenceError = referenceChecks.Max(check => check.recovered_reference_error_degrees);
            if (maximumReferenceError > 1e-3f) throw new InvalidDataException($"Inverse/forward reference consistency failed: maximum error {maximumReferenceError} degrees.");
            Write(output, "reference_muscle_check.json", new { interpretation = "Zero normalized muscles are not the stored SkeletonPose for offset limbs. Inverting and reapplying the same upstream formulas establishes reference-pose representability and inverse consistency only; it does not independently prove game frame correctness.", formula = "tx=x/w; ty=(y/w-tx*z/w)/(1+tx^2); tz=(z/w+tx*y/w)/(1+tx^2); angle=2*atan(t); muscle=(angle/sign)/(positive?limitMax:-limitMin).", checks = referenceChecks, maximum_recovered_reference_error_degrees = maximumReferenceError });
            Write(output, "reference_muscle_frame.json", referenceMuscles);
            List<BodyFrame> solved = new();
            List<NodePose[]> world = new();
            float maximumUnitError = 0f;
            float maximumBoneLengthError = 0f;
            var metrics = new Dictionary<int, List<Quat>>();
            foreach (MuscleBone bone in solver.DrivenBones) metrics.Add(bone.Slot, new List<Quat>());
            foreach (ScalarFrame frame in frames)
            {
                var quats = new Quat[AvatarMuscleReferential.SlotCount];
                var driven = new bool[AvatarMuscleReferential.SlotCount];
                solver.BodyLocalQuats(frame.Values, quats, driven);
                var body = solver.BodyTransform(frame.Values, root, quats, driven, clip.m_MuscleClip.m_KeepOriginalPositionXZ, clip.m_MuscleClip.m_KeepOriginalPositionY, clip.m_MuscleClip.m_KeepOriginalOrientation) ?? throw new InvalidDataException("Root body solve missing.");
                var localT = rig.SkeletonPose.Select(pose => pose.T).ToArray();
                var localQ = rig.SkeletonPose.Select(pose => pose.Q).ToArray();
                foreach (MuscleBone bone in solver.DrivenBones)
                {
                    Quat rotation = bone.IsHips ? body.Rotation : quats[bone.Slot];
                    RequireRotation(rotation, $"frame {frame.Time}, {bone.Path}");
                    maximumUnitError = MathF.Max(maximumUnitError, MathF.Abs(rotation.LengthSquared() - 1f));
                    localQ[bone.NodeIndex] = rotation;
                    if (bone.IsHips) localT[bone.NodeIndex] = body.Position;
                    metrics[bone.Slot].Add(rotation);
                }
                RequirePosition(body.Position, $"hips {frame.Time}");
                RequirePosition(body.Motion.Position, $"motion {frame.Time}");
                RequireRotation(body.Motion.Rotation, $"motion {frame.Time}");
                var fk = ForwardKinematics(rig, localT, localQ);
                for (int node = 0; node < rig.NodeParent.Length; node++)
                {
                    int parent = rig.NodeParent[node];
                    if (parent < 0 || node == solver.Hips.NodeIndex) continue;
                    float error = MathF.Abs(Vec.Distance(fk.Positions[node], fk.Positions[parent]) - rig.SkeletonPose[node].T.Length());
                    maximumBoneLengthError = MathF.Max(maximumBoneLengthError, error);
                }
                NodePose[] localNodes = Enumerable.Range(0, rig.NodeParent.Length).Select(node => new NodePose(node, rig.Tos[rig.NodeId[node]], Array(localT[node]), Array(localQ[node]))).ToArray();
                NodePose[] worldNodes = Enumerable.Range(0, rig.NodeParent.Length).Select(node => new NodePose(node, rig.Tos[rig.NodeId[node]], Array(fk.Positions[node]), Array(fk.Rotations[node]))).ToArray();
                solved.Add(new BodyFrame(frame.Time, localNodes, Array(body.Motion.Position), Array(body.Motion.Rotation)));
                world.Add(worldNodes);
            }
            if (maximumBoneLengthError > 2e-5f) throw new InvalidDataException($"Bone length invariance failed: {maximumBoneLengthError}.");
            BoneMetric[] boneMetrics = solver.DrivenBones.Select(bone => new BoneMetric(bone.Slot, bone.NodeIndex, bone.Path, Enumerable.Range(0, 3).Select(bone.GetDofMuscle).ToArray(), bone.DofChannel.ToArray(), bone.IsHips ? 0f : Angle(zeroQuats[bone.Slot], rig.SkeletonPose[bone.NodeIndex].Q), metrics[bone.Slot].Max(rotation => Angle(rotation, metrics[bone.Slot][0])))).ToArray();
            AnimationFrameJson.Write(output, "local_body_frames.json", solved);
            AnimationFrameJson.Write(output, "world_body_frames.json", world);
            Write(output, "bone_metrics.json", boneMetrics);
            MergeSnapshot.Export(request.SourceAudit, Path.Combine(request.Output, "native"), solved.ToArray(), world.ToArray(), boneMetrics, bindings, frames, Path.Combine(output, "merged"), request.AnimatorRootPath, root, JsonOptions);
            Write(output, "human_node_map.json", rig.NodeId.Select((id, node) => new { node, id, parent = rig.NodeParent[node], path = rig.Tos[id], rest_position = Array(rig.SkeletonPose[node].T), rest_rotation = Array(rig.SkeletonPose[node].Q) }).ToArray());
            Write(output, "avatar_full_node_map.json", avatar.m_Avatar.m_AvatarSkeleton.m_ID.Select((id, node) => new { node, id, path = avatar.m_TOS[id], parent = avatar.m_Avatar.m_AvatarSkeleton.m_Node[node].m_ParentId, name_id = avatar.m_Avatar.m_SkeletonNameIDArray[node], human_node = avatar.m_Avatar.m_HumanSkeletonReverseIndexArray[node], rest_position = Array(Vector(avatar.m_Avatar.m_DefaultPose.m_X[node].t)), rest_rotation = Array(Rotation(avatar.m_Avatar.m_DefaultPose.m_X[node].q)) }).ToArray());
            var receipt = new { status = "upstream_solver_real_avatar_candidate_numeric_validation_only", frames = frames.Length, time_domain = frameDomain, joints = solver.DrivenBones.Count, mapped_muscle_axes = solver.DrivenBones.Sum(bone => bone.DofChannel.Count(index => index >= 0)), changing_joints = boneMetrics.Count(bone => bone.MotionRangeDegrees > 0.1f), neutral_rotation_max_error_degrees = boneMetrics.Max(bone => bone.NeutralRotationErrorDegrees), maximum_inverse_reference_consistency_error_degrees = maximumReferenceError, inverse_reference_validation_boundary = "Same-formula inverse/forward test proves representability and consistency, not independent Unity/runtime correctness.", human_reference_scale_maximum_unit_difference = avatar.m_Avatar.m_Human.m_SkeletonPose.m_X.Max(pose => Vector(pose.s - new AnimeStudio.Vector3(1, 1, 1)).Length()), maximum_unit_quaternion_error = maximumUnitError, maximum_bone_length_error_meters = maximumBoneLengthError, human_scale = avatar.m_Avatar.m_Human.m_Scale, keep_position_xz = clip.m_MuscleClip.m_KeepOriginalPositionXZ, keep_position_y = clip.m_MuscleClip.m_KeepOriginalPositionY, keep_orientation = clip.m_MuscleClip.m_KeepOriginalOrientation, limitations = new[] { "No rendered or Unity runtime reference comparison yet.", "Upstream solver does not consume Human.m_Scale; preserved unchanged for numerical audit.", "Zero-muscle neutral is different from stored reference. Inverse-reference test is mathematical consistency only. Hips is reconstructed from actual root/mass/orientation channels.", "Human hand mappings follow this exact Avatar; qvvf/Humanoid path conflicts fail explicitly.", "Do not treat this candidate as a validated complete Blender action." } };
            Write(output, "receipt.json", receipt);
            Write(output, "reader_logs.json", logger.Snapshot());
            Console.WriteLine(JsonSerializer.Serialize(receipt, JsonOptions));
            return 0;
        }
        finally { manager.Clear(); }
    }
}
