using System.Collections.Specialized;
using System.Text.Json.Serialization;
using AnimeStudio;
using Object = AnimeStudio.Object;

internal static partial class Program
{
    private static (ParticleModuleParameters Parameters, Mesh[] Meshes, Texture2D[] Textures)
        ReadParticleModuleParameters(OrderedDictionary fields, Object owner)
    {
        var initial = ModuleField<OrderedDictionary>(fields, "InitialModule", "ParticleSystem", owner);
        var emission = ModuleField<OrderedDictionary>(fields, "EmissionModule", "ParticleSystem", owner);
        var velocity = ModuleField<OrderedDictionary>(fields, "VelocityModule", "ParticleSystem", owner);
        var shape = ReadParticleShape(
            ModuleField<OrderedDictionary>(fields, "ShapeModule", "ParticleSystem", owner), owner);
        var size = ReadParticleSize(
            ModuleField<OrderedDictionary>(fields, "SizeModule", "ParticleSystem", owner), owner);
        var color = ReadParticleColorOverLifetime(
            ModuleField<OrderedDictionary>(fields, "ColorModule", "ParticleSystem", owner), owner);
        var trail = ReadParticleTrail(
            ModuleField<OrderedDictionary>(fields, "TrailModule", "ParticleSystem", owner), owner);
        var customData = ReadParticleCustomData(
            ModuleField<OrderedDictionary>(fields, "CustomDataModule", "ParticleSystem", owner), owner);
        var uv = ReadParticleUv(
            ModuleField<OrderedDictionary>(fields, "UVModule", "ParticleSystem", owner), owner);
        return (new ParticleModuleParameters(
            5, "serialized_initial_emission_shape_size_color_trail_custom_data_uv_velocity_no_simulation",
            ReadParticleInitial(initial, owner),
            ReadParticleEmission(emission, owner),
            "serialized_parameters_no_simulation",
            shape.Parameters, size, color, trail, customData, uv,
            ReadParticleVelocity(velocity, owner)),
            shape.Meshes, shape.Textures);
    }

    private static ParticleInitialParameters ReadParticleInitial(OrderedDictionary module, Object owner) =>
        new(
            ReadParticleMinMaxCurve(
                ModuleField<OrderedDictionary>(module, "startLifetime", "InitialModule", owner),
                "InitialModule.startLifetime", owner),
            ReadParticleMinMaxCurve(
                ModuleField<OrderedDictionary>(module, "startSpeed", "InitialModule", owner),
                "InitialModule.startSpeed", owner),
            ReadParticleMinMaxCurve(
                ModuleField<OrderedDictionary>(module, "startSize", "InitialModule", owner),
                "InitialModule.startSize", owner),
            ReadParticleMinMaxCurve(
                ModuleField<OrderedDictionary>(module, "startSizeY", "InitialModule", owner),
                "InitialModule.startSizeY", owner),
            ReadParticleMinMaxCurve(
                ModuleField<OrderedDictionary>(module, "startSizeZ", "InitialModule", owner),
                "InitialModule.startSizeZ", owner),
            ReadParticleMinMaxGradient(
                ModuleField<OrderedDictionary>(module, "startColor", "InitialModule", owner),
                "InitialModule.startColor", owner),
            ModuleField<bool>(module, "size3D", "InitialModule", owner),
            ModuleField<int>(module, "maxNumParticles", "InitialModule", owner),
            ReadParticleMinMaxCurve(
                ModuleField<OrderedDictionary>(module, "startRotationX", "InitialModule", owner),
                "InitialModule.startRotationX", owner),
            ReadParticleMinMaxCurve(
                ModuleField<OrderedDictionary>(module, "startRotationY", "InitialModule", owner),
                "InitialModule.startRotationY", owner),
            ReadParticleMinMaxCurve(
                ModuleField<OrderedDictionary>(module, "startRotation", "InitialModule", owner),
                "InitialModule.startRotation", owner),
            ReadParticleMinMaxCurve(
                ModuleField<OrderedDictionary>(module, "gravityModifier", "InitialModule", owner),
                "InitialModule.gravityModifier", owner),
            ModuleFloat(module, "randomizeRotationDirection", "InitialModule", owner),
            ModuleField<bool>(module, "rotation3D", "InitialModule", owner));

    private static ParticleEmissionParameters ReadParticleEmission(OrderedDictionary module, Object owner)
    {
        var burstCount = ModuleField<int>(module, "m_BurstCount", "EmissionModule", owner);
        var rows = ModuleField<List<object>>(module, "m_Bursts", "EmissionModule", owner);
        if (burstCount < 0 || burstCount > rows.Count)
            throw new InvalidDataException(
                $"PARTICLE_BURST_COUNT_INVALID source={owner.assetsFile.fileName} pathId={owner.m_PathID} " +
                $"burstCount={burstCount} serializedRows={rows.Count}");
        var bursts = rows.Select((value, index) =>
        {
            var context = $"EmissionModule.m_Bursts[{index}]";
            var row = ModuleValue<OrderedDictionary>(value, context, owner);
            return new ParticleEmissionBurst(
                ModuleFloat(row, "time", context, owner),
                ReadParticleMinMaxCurve(
                    ModuleField<OrderedDictionary>(row, "countCurve", context, owner),
                    $"{context}.countCurve", owner),
                ModuleField<int>(row, "cycleCount", context, owner),
                ModuleFloat(row, "repeatInterval", context, owner),
                ModuleFloat(row, "probability", context, owner));
        }).ToArray();
        return new ParticleEmissionParameters(
            ReadParticleMinMaxCurve(
                ModuleField<OrderedDictionary>(module, "rateOverTime", "EmissionModule", owner),
                "EmissionModule.rateOverTime", owner),
            ReadParticleMinMaxCurve(
                ModuleField<OrderedDictionary>(module, "rateOverDistance", "EmissionModule", owner),
                "EmissionModule.rateOverDistance", owner),
            burstCount, bursts);
    }

    private static ParticleVelocityParameters ReadParticleVelocity(OrderedDictionary module, Object owner) =>
        new(
            ModuleField<bool>(module, "enabled", "VelocityModule", owner),
            ReadParticleVelocityCurve(module, "x", owner),
            ReadParticleVelocityCurve(module, "y", owner),
            ReadParticleVelocityCurve(module, "z", owner),
            ReadParticleVelocityCurve(module, "orbitalX", owner),
            ReadParticleVelocityCurve(module, "orbitalY", owner),
            ReadParticleVelocityCurve(module, "orbitalZ", owner),
            ReadParticleVelocityCurve(module, "orbitalOffsetX", owner),
            ReadParticleVelocityCurve(module, "orbitalOffsetY", owner),
            ReadParticleVelocityCurve(module, "orbitalOffsetZ", owner),
            ReadParticleVelocityCurve(module, "radial", owner),
            ReadParticleVelocityCurve(module, "speedModifier", owner),
            ModuleField<bool>(module, "inWorldSpace", "VelocityModule", owner));

    private static ParticleMinMaxCurve ReadParticleVelocityCurve(
        OrderedDictionary module, string name, Object owner) =>
        ReadParticleMinMaxCurve(
            ModuleField<OrderedDictionary>(module, name, "VelocityModule", owner),
            $"VelocityModule.{name}", owner);

    private static ParticleMinMaxCurve ReadParticleMinMaxCurve(
        OrderedDictionary fields, string context, Object owner) =>
        new(
            ModuleField<ushort>(fields, "minMaxState", context, owner),
            ModuleFloat(fields, "scalar", context, owner),
            ModuleFloat(fields, "minScalar", context, owner),
            ReadParticleCurve(
                ModuleField<OrderedDictionary>(fields, "maxCurve", context, owner),
                $"{context}.maxCurve", owner),
            ReadParticleCurve(
                ModuleField<OrderedDictionary>(fields, "minCurve", context, owner),
                $"{context}.minCurve", owner));

    private static ParticleCurve ReadParticleCurve(
        OrderedDictionary fields, string context, Object owner)
    {
        var rows = ModuleField<List<object>>(fields, "m_Curve", context, owner);
        var keys = rows.Select((value, index) =>
        {
            var keyContext = $"{context}.m_Curve[{index}]";
            var row = ModuleValue<OrderedDictionary>(value, keyContext, owner);
            return new ParticleCurveKey(
                ModuleFloat(row, "time", keyContext, owner),
                ModuleFloat(row, "value", keyContext, owner),
                ModuleSlope(row, "inSlope", keyContext, owner),
                ModuleSlope(row, "outSlope", keyContext, owner),
                ModuleField<int>(row, "weightedMode", keyContext, owner),
                ModuleFloat(row, "inWeight", keyContext, owner),
                ModuleFloat(row, "outWeight", keyContext, owner));
        }).ToArray();
        return new ParticleCurve(
            keys,
            ModuleField<int>(fields, "m_PreInfinity", context, owner),
            ModuleField<int>(fields, "m_PostInfinity", context, owner),
            ModuleField<int>(fields, "m_RotationOrder", context, owner));
    }

    private static ParticleMinMaxGradient ReadParticleMinMaxGradient(
        OrderedDictionary fields, string context, Object owner) =>
        new(
            ModuleField<ushort>(fields, "minMaxState", context, owner),
            ReadParticleColor(
                ModuleField<OrderedDictionary>(fields, "minColor", context, owner),
                $"{context}.minColor", owner),
            ReadParticleColor(
                ModuleField<OrderedDictionary>(fields, "maxColor", context, owner),
                $"{context}.maxColor", owner),
            ReadParticleGradient(
                ModuleField<OrderedDictionary>(fields, "maxGradient", context, owner),
                $"{context}.maxGradient", owner),
            ReadParticleGradient(
                ModuleField<OrderedDictionary>(fields, "minGradient", context, owner),
                $"{context}.minGradient", owner));

    private static ParticleGradient ReadParticleGradient(
        OrderedDictionary fields, string context, Object owner)
    {
        var colors = Enumerable.Range(0, 8).Select(index =>
            ReadParticleColor(
                ModuleField<OrderedDictionary>(fields, $"key{index}", context, owner),
                $"{context}.key{index}", owner)).ToArray();
        var colorTimes = Enumerable.Range(0, 8).Select(index =>
            ModuleField<ushort>(fields, $"ctime{index}", context, owner)).ToArray();
        var alphaTimes = Enumerable.Range(0, 8).Select(index =>
            ModuleField<ushort>(fields, $"atime{index}", context, owner)).ToArray();
        var colorKeys = ModuleField<byte>(fields, "m_NumColorKeys", context, owner);
        var alphaKeys = ModuleField<byte>(fields, "m_NumAlphaKeys", context, owner);
        if (colorKeys > colors.Length || alphaKeys > colors.Length)
            throw new InvalidDataException(
                $"PARTICLE_GRADIENT_KEY_COUNT_INVALID source={owner.assetsFile.fileName} pathId={owner.m_PathID} " +
                $"field={context} colorKeys={colorKeys} alphaKeys={alphaKeys} slots={colors.Length}");
        return new ParticleGradient(
            colors, colorTimes, alphaTimes,
            ModuleField<int>(fields, "m_Mode", context, owner),
            colorKeys, alphaKeys);
    }

    private static ParticleColor ReadParticleColor(
        OrderedDictionary fields, string context, Object owner) =>
        new(
            ModuleFloat(fields, "r", context, owner),
            ModuleFloat(fields, "g", context, owner),
            ModuleFloat(fields, "b", context, owner),
            ModuleFloat(fields, "a", context, owner));

    private static T ModuleField<T>(
        OrderedDictionary fields, string name, string context, Object owner)
    {
        if (fields[name] is T value)
            return value;
        throw new InvalidDataException(
            $"PARTICLE_MODULE_FIELD_INVALID source={owner.assetsFile.fileName} pathId={owner.m_PathID} " +
            $"field={context}.{name} expected={typeof(T).Name} actual={fields[name]?.GetType().Name ?? "missing"}");
    }

    private static T ModuleValue<T>(object value, string context, Object owner)
    {
        if (value is T typed)
            return typed;
        throw new InvalidDataException(
            $"PARTICLE_MODULE_VALUE_INVALID source={owner.assetsFile.fileName} pathId={owner.m_PathID} " +
            $"field={context} expected={typeof(T).Name} actual={value?.GetType().Name ?? "null"}");
    }

    private static float ModuleFloat(
        OrderedDictionary fields, string name, string context, Object owner)
    {
        var value = ModuleField<float>(fields, name, context, owner);
        if (!float.IsFinite(value))
            throw new InvalidDataException(
                $"PARTICLE_MODULE_FLOAT_NONFINITE source={owner.assetsFile.fileName} pathId={owner.m_PathID} " +
                $"field={context}.{name} value={value}");
        return value;
    }

    private static float ModuleSlope(
        OrderedDictionary fields, string name, string context, Object owner)
    {
        var value = ModuleField<float>(fields, name, context, owner);
        if (float.IsNaN(value))
            throw new InvalidDataException(
                $"PARTICLE_MODULE_SLOPE_NAN source={owner.assetsFile.fileName} pathId={owner.m_PathID} " +
                $"field={context}.{name}");
        return value;
    }
}

internal sealed record ParticleModuleParameters(
    int SchemaVersion, string Scope,
    ParticleInitialParameters Initial, ParticleEmissionParameters Emission,
    string ShapeStatus, ParticleShapeParameters Shape,
    ParticleSizeOverLifetimeParameters SizeOverLifetime,
    ParticleColorOverLifetimeParameters ColorOverLifetime,
    ParticleTrailParameters Trail,
    ParticleCustomDataParameters CustomData,
    ParticleUvParameters Uv,
    ParticleVelocityParameters VelocityOverLifetime);
internal sealed record ParticleInitialParameters(
    ParticleMinMaxCurve StartLifetime, ParticleMinMaxCurve StartSpeed,
    ParticleMinMaxCurve StartSize, ParticleMinMaxCurve StartSizeY, ParticleMinMaxCurve StartSizeZ,
    ParticleMinMaxGradient StartColor, bool Size3D, int MaxNumParticles,
    ParticleMinMaxCurve StartRotationX, ParticleMinMaxCurve StartRotationY,
    ParticleMinMaxCurve StartRotation, ParticleMinMaxCurve GravityModifier,
    float RandomizeRotationDirection, bool Rotation3D);
internal sealed record ParticleEmissionParameters(
    ParticleMinMaxCurve RateOverTime, ParticleMinMaxCurve RateOverDistance,
    int BurstCount, ParticleEmissionBurst[] Bursts);
internal sealed record ParticleEmissionBurst(
    float Time, ParticleMinMaxCurve CountCurve, int CycleCount, float RepeatInterval, float Probability);
internal sealed record ParticleVelocityParameters(
    bool Enabled,
    ParticleMinMaxCurve X, ParticleMinMaxCurve Y, ParticleMinMaxCurve Z,
    ParticleMinMaxCurve OrbitalX, ParticleMinMaxCurve OrbitalY, ParticleMinMaxCurve OrbitalZ,
    ParticleMinMaxCurve OrbitalOffsetX, ParticleMinMaxCurve OrbitalOffsetY, ParticleMinMaxCurve OrbitalOffsetZ,
    ParticleMinMaxCurve Radial, ParticleMinMaxCurve SpeedModifier,
    bool InWorldSpace);
internal sealed record ParticleMinMaxCurve(
    ushort MinMaxState, float Scalar, float MinScalar, ParticleCurve MaxCurve, ParticleCurve MinCurve);
internal sealed record ParticleCurve(
    ParticleCurveKey[] Keys, int PreInfinity, int PostInfinity, int RotationOrder);
internal sealed record ParticleCurveKey(
    float Time, float Value,
    [property: JsonNumberHandling(JsonNumberHandling.AllowNamedFloatingPointLiterals)] float InSlope,
    [property: JsonNumberHandling(JsonNumberHandling.AllowNamedFloatingPointLiterals)] float OutSlope,
    int WeightedMode,
    float InWeight, float OutWeight);
internal sealed record ParticleMinMaxGradient(
    ushort MinMaxState, ParticleColor MinColor, ParticleColor MaxColor,
    ParticleGradient MaxGradient, ParticleGradient MinGradient);
internal sealed record ParticleGradient(
    ParticleColor[] Colors, ushort[] ColorTimes, ushort[] AlphaTimes,
    int Mode, byte NumColorKeys, byte NumAlphaKeys);
internal sealed record ParticleColor(float R, float G, float B, float A);
