using System.Collections.Specialized;
using AnimeStudio;
using Object = AnimeStudio.Object;

internal static partial class Program
{
    private static (ParticleShapeParameters Parameters, Mesh[] Meshes, Texture2D[] Textures)
        ReadParticleShape(OrderedDictionary module, Object owner)
    {
        var (meshReference, mesh) = ParticlePointer<Mesh>(module, "m_Mesh", owner);
        var (meshRendererReference, _) = ParticlePointer<MeshRenderer>(module, "m_MeshRenderer", owner);
        var (skinnedReference, _) = ParticlePointer<SkinnedMeshRenderer>(
            module, "m_SkinnedMeshRenderer", owner);
        var spriteReference = ReadParticleClassPointer(module, "m_Sprite", ClassIDType.Sprite, owner);
        var spriteRendererReference = ReadParticleClassPointer(
            module, "m_SpriteRenderer", ClassIDType.SpriteRenderer, owner);
        var (textureReference, texture) = ParticlePointer<Texture2D>(module, "m_Texture", owner);
        var indirectDependencyStatus = new[]
        {
            meshRendererReference, skinnedReference, spriteReference, spriteRendererReference,
        }.Any(reference => reference.Target.PathId != 0)
            ? "unverified_renderer_sprite_dependencies"
            : "not_applicable";
        var references = new ParticleShapeReferences(
            meshReference, meshRendererReference, skinnedReference,
            spriteReference, spriteRendererReference, textureReference,
            indirectDependencyStatus);

        var shape = new ParticleShapeParameters(
            ModuleField<bool>(module, "enabled", "ShapeModule", owner),
            ModuleField<int>(module, "type", "ShapeModule", owner),
            ModuleFloat(module, "angle", "ShapeModule", owner),
            ModuleFloat(module, "length", "ShapeModule", owner),
            ReadParticleVector3(module, "boxThickness", owner),
            ModuleFloat(module, "radiusThickness", "ShapeModule", owner),
            ModuleFloat(module, "donutRadius", "ShapeModule", owner),
            ReadParticleVector3(module, "m_Position", owner),
            ReadParticleVector3(module, "m_Rotation", owner),
            ReadParticleVector3(module, "m_Scale", owner),
            ModuleField<int>(module, "placementMode", "ShapeModule", owner),
            ModuleField<int>(module, "m_MeshMaterialIndex", "ShapeModule", owner),
            ModuleFloat(module, "m_MeshNormalOffset", "ShapeModule", owner),
            ReadParticleShapeSpawn(module, owner),
            references,
            ModuleField<bool>(module, "m_UseMeshMaterialIndex", "ShapeModule", owner),
            ModuleField<bool>(module, "m_UseMeshColors", "ShapeModule", owner),
            ModuleField<bool>(module, "alignToDirection", "ShapeModule", owner),
            ReadParticleShapeTextureSettings(module, owner),
            ModuleFloat(module, "randomDirectionAmount", "ShapeModule", owner),
            ModuleFloat(module, "sphericalDirectionAmount", "ShapeModule", owner),
            ModuleFloat(module, "randomPositionAmount", "ShapeModule", owner),
            ReadParticleShapeDimension(module, "radius", owner),
            ReadParticleShapeDimension(module, "arc", owner));
        return (shape,
            mesh is null ? [] : [mesh],
            texture is null ? [] : [texture]);
    }

    private static ParticleAssetPointer ReadParticleClassPointer(
        OrderedDictionary module, string name, ClassIDType expected, Object owner)
    {
        var (reference, target) = ParticlePointer<Object>(module, name, owner);
        if (target is not null && target.type != expected)
            throw new InvalidDataException(
                $"PARTICLE_SHAPE_POINTER_TYPE_INVALID source={owner.assetsFile.fileName} " +
                $"pathId={owner.m_PathID} field=ShapeModule.{name} " +
                $"targetSource={target.assetsFile.fileName} targetPathId={target.m_PathID} " +
                $"expected={expected} actual={target.type}");
        return reference with { Target = reference.Target with { Type = expected.ToString() } };
    }

    private static ParticleVector3 ReadParticleVector3(
        OrderedDictionary module, string name, Object owner)
    {
        var vector = ModuleField<OrderedDictionary>(module, name, "ShapeModule", owner);
        var context = $"ShapeModule.{name}";
        return new ParticleVector3(
            ModuleFloat(vector, "x", context, owner),
            ModuleFloat(vector, "y", context, owner),
            ModuleFloat(vector, "z", context, owner));
    }

    private static ParticleShapeSpawn ReadParticleShapeSpawn(OrderedDictionary module, Object owner)
    {
        var spawn = ModuleField<OrderedDictionary>(module, "m_MeshSpawn", "ShapeModule", owner);
        return new ParticleShapeSpawn(
            ModuleField<int>(spawn, "mode", "ShapeModule.m_MeshSpawn", owner),
            ModuleFloat(spawn, "spread", "ShapeModule.m_MeshSpawn", owner),
            ReadParticleMinMaxCurve(
                ModuleField<OrderedDictionary>(spawn, "speed", "ShapeModule.m_MeshSpawn", owner),
                "ShapeModule.m_MeshSpawn.speed", owner));
    }

    private static ParticleShapeDimension ReadParticleShapeDimension(
        OrderedDictionary module, string name, Object owner)
    {
        var dimension = ModuleField<OrderedDictionary>(module, name, "ShapeModule", owner);
        var context = $"ShapeModule.{name}";
        return new ParticleShapeDimension(
            ModuleFloat(dimension, "value", context, owner),
            ModuleField<int>(dimension, "mode", context, owner),
            ModuleFloat(dimension, "spread", context, owner),
            ReadParticleMinMaxCurve(
                ModuleField<OrderedDictionary>(dimension, "speed", context, owner),
                $"{context}.speed", owner));
    }

    private static ParticleShapeTextureSettings ReadParticleShapeTextureSettings(
        OrderedDictionary module, Object owner) =>
        new(
            ModuleField<int>(module, "m_TextureClipChannel", "ShapeModule", owner),
            ModuleFloat(module, "m_TextureClipThreshold", "ShapeModule", owner),
            ModuleFloat(module, "m_TextureClipThresholdUpper", "ShapeModule", owner),
            ModuleField<int>(module, "m_TextureUVChannel", "ShapeModule", owner),
            ModuleField<bool>(module, "m_TextureColorAffectsParticles", "ShapeModule", owner),
            ModuleField<bool>(module, "m_TextureAlphaAffectsParticles", "ShapeModule", owner),
            ModuleField<bool>(module, "m_TextureBilinearFiltering", "ShapeModule", owner));

    private static ParticleSizeOverLifetimeParameters ReadParticleSize(
        OrderedDictionary module, Object owner) =>
        new(
            ModuleField<bool>(module, "enabled", "SizeModule", owner),
            ReadParticleMinMaxCurve(
                ModuleField<OrderedDictionary>(module, "curve", "SizeModule", owner),
                "SizeModule.curve", owner),
            ReadParticleMinMaxCurve(
                ModuleField<OrderedDictionary>(module, "y", "SizeModule", owner),
                "SizeModule.y", owner),
            ReadParticleMinMaxCurve(
                ModuleField<OrderedDictionary>(module, "z", "SizeModule", owner),
                "SizeModule.z", owner),
            ModuleField<bool>(module, "separateAxes", "SizeModule", owner));

    private static ParticleColorOverLifetimeParameters ReadParticleColorOverLifetime(
        OrderedDictionary module, Object owner) =>
        new(
            ModuleField<bool>(module, "enabled", "ColorModule", owner),
            ReadParticleMinMaxGradient(
                ModuleField<OrderedDictionary>(module, "gradient", "ColorModule", owner),
                "ColorModule.gradient", owner));

    private static ParticleTrailParameters ReadParticleTrail(
        OrderedDictionary module, Object owner) =>
        new(
            ModuleField<bool>(module, "enabled", "TrailModule", owner),
            ModuleField<int>(module, "mode", "TrailModule", owner),
            ModuleFloat(module, "ratio", "TrailModule", owner),
            ReadParticleMinMaxCurve(
                ModuleField<OrderedDictionary>(module, "lifetime", "TrailModule", owner),
                "TrailModule.lifetime", owner),
            ModuleFloat(module, "minVertexDistance", "TrailModule", owner),
            ModuleField<int>(module, "textureMode", "TrailModule", owner),
            ModuleField<int>(module, "ribbonCount", "TrailModule", owner),
            ModuleField<bool>(module, "worldSpace", "TrailModule", owner),
            ModuleField<bool>(module, "dieWithParticles", "TrailModule", owner),
            ModuleField<bool>(module, "sizeAffectsWidth", "TrailModule", owner),
            ModuleField<bool>(module, "sizeAffectsLifetime", "TrailModule", owner),
            ModuleField<bool>(module, "inheritParticleColor", "TrailModule", owner),
            ModuleField<bool>(module, "splitSubEmitterRibbons", "TrailModule", owner),
            ModuleField<bool>(module, "attachRibbonsToTransform", "TrailModule", owner),
            ReadParticleMinMaxGradient(
                ModuleField<OrderedDictionary>(module, "colorOverLifetime", "TrailModule", owner),
                "TrailModule.colorOverLifetime", owner),
            ReadParticleMinMaxCurve(
                ModuleField<OrderedDictionary>(module, "widthOverTrail", "TrailModule", owner),
                "TrailModule.widthOverTrail", owner),
            ReadParticleMinMaxGradient(
                ModuleField<OrderedDictionary>(module, "colorOverTrail", "TrailModule", owner),
                "TrailModule.colorOverTrail", owner));

    private static ParticleCustomDataParameters ReadParticleCustomData(
        OrderedDictionary module, Object owner) =>
        new(
            ModuleField<bool>(module, "enabled", "CustomDataModule", owner),
            Enumerable.Range(0, 2).Select(index =>
                new ParticleCustomDataStream(
                    ModuleField<int>(module, $"mode{index}", "CustomDataModule", owner),
                    ModuleField<int>(module, $"vectorComponentCount{index}", "CustomDataModule", owner),
                    ReadParticleMinMaxGradient(
                        ModuleField<OrderedDictionary>(module, $"color{index}", "CustomDataModule", owner),
                        $"CustomDataModule.color{index}", owner),
                    Enumerable.Range(0, 4).Select(axis => ReadParticleMinMaxCurve(
                        ModuleField<OrderedDictionary>(module, $"vector{index}_{axis}", "CustomDataModule", owner),
                        $"CustomDataModule.vector{index}_{axis}", owner)).ToArray())).ToArray());

    private static ParticleUvParameters ReadParticleUv(OrderedDictionary module, Object owner)
    {
        var speed = ModuleField<OrderedDictionary>(module, "speedRange", "UVModule", owner);
        var spriteRows = ModuleField<List<object>>(module, "sprites", "UVModule", owner);
        var sprites = spriteRows.Select((value, index) =>
        {
            var context = $"UVModule.sprites[{index}]";
            var sprite = ModuleValue<OrderedDictionary>(value, context, owner);
            var pointer = ModuleField<OrderedDictionary>(sprite, "sprite", context, owner);
            return ParticlePointerValue<Object>(pointer, $"{context}.sprite", owner).Reference;
        }).ToArray();
        return new ParticleUvParameters(
            ModuleField<bool>(module, "enabled", "UVModule", owner),
            ModuleField<int>(module, "mode", "UVModule", owner),
            ModuleField<int>(module, "timeMode", "UVModule", owner),
            ModuleFloat(module, "fps", "UVModule", owner),
            ReadParticleMinMaxCurve(
                ModuleField<OrderedDictionary>(module, "frameOverTime", "UVModule", owner),
                "UVModule.frameOverTime", owner),
            ReadParticleMinMaxCurve(
                ModuleField<OrderedDictionary>(module, "startFrame", "UVModule", owner),
                "UVModule.startFrame", owner),
            new ParticleVector2(ModuleFloat(speed, "x", "UVModule.speedRange", owner),
                                ModuleFloat(speed, "y", "UVModule.speedRange", owner)),
            ModuleField<int>(module, "tilesX", "UVModule", owner),
            ModuleField<int>(module, "tilesY", "UVModule", owner),
            ModuleField<int>(module, "animationType", "UVModule", owner),
            ModuleField<int>(module, "rowIndex", "UVModule", owner),
            ModuleFloat(module, "cycles", "UVModule", owner),
            ModuleField<int>(module, "uvChannelMask", "UVModule", owner),
            ModuleField<int>(module, "rowMode", "UVModule", owner),
            sprites,
            ModuleFloat(module, "flipU", "UVModule", owner),
            ModuleFloat(module, "flipV", "UVModule", owner));
    }
}

internal sealed record ParticleShapeParameters(
    bool Enabled, int Type, float Angle, float Length, ParticleVector3 BoxThickness,
    float RadiusThickness, float DonutRadius,
    ParticleVector3 Position, ParticleVector3 Rotation, ParticleVector3 Scale,
    int PlacementMode, int MeshMaterialIndex, float MeshNormalOffset,
    ParticleShapeSpawn MeshSpawn, ParticleShapeReferences References,
    bool UseMeshMaterialIndex, bool UseMeshColors, bool AlignToDirection,
    ParticleShapeTextureSettings TextureSettings,
    float RandomDirectionAmount, float SphericalDirectionAmount, float RandomPositionAmount,
    ParticleShapeDimension Radius, ParticleShapeDimension Arc);
internal sealed record ParticleVector3(float X, float Y, float Z);
internal sealed record ParticleShapeSpawn(int Mode, float Spread, ParticleMinMaxCurve Speed);
internal sealed record ParticleShapeDimension(float Value, int Mode, float Spread, ParticleMinMaxCurve Speed);
internal sealed record ParticleShapeReferences(
    ParticleAssetPointer Mesh, ParticleAssetPointer MeshRenderer,
    ParticleAssetPointer SkinnedMeshRenderer, ParticleAssetPointer Sprite,
    ParticleAssetPointer SpriteRenderer, ParticleAssetPointer Texture,
    string IndirectDependencyStatus);
internal sealed record ParticleShapeTextureSettings(
    int ClipChannel, float ClipThreshold, float ClipThresholdUpper, int UvChannel,
    bool ColorAffectsParticles, bool AlphaAffectsParticles, bool BilinearFiltering);
internal sealed record ParticleSizeOverLifetimeParameters(
    bool Enabled, ParticleMinMaxCurve Curve, ParticleMinMaxCurve Y,
    ParticleMinMaxCurve Z, bool SeparateAxes);
internal sealed record ParticleColorOverLifetimeParameters(bool Enabled, ParticleMinMaxGradient Gradient);
internal sealed record ParticleTrailParameters(
    bool Enabled, int Mode, float Ratio, ParticleMinMaxCurve Lifetime,
    float MinVertexDistance, int TextureMode, int RibbonCount,
    bool WorldSpace, bool DieWithParticles, bool SizeAffectsWidth,
    bool SizeAffectsLifetime, bool InheritParticleColor,
    bool SplitSubEmitterRibbons, bool AttachRibbonsToTransform,
    ParticleMinMaxGradient ColorOverLifetime, ParticleMinMaxCurve WidthOverTrail,
    ParticleMinMaxGradient ColorOverTrail);
internal sealed record ParticleCustomDataParameters(bool Enabled, ParticleCustomDataStream[] Streams);
internal sealed record ParticleCustomDataStream(
    int Mode, int VectorComponentCount, ParticleMinMaxGradient Color,
    ParticleMinMaxCurve[] Vector);
internal sealed record ParticleUvParameters(
    bool Enabled, int Mode, int TimeMode, float Fps,
    ParticleMinMaxCurve FrameOverTime, ParticleMinMaxCurve StartFrame,
    ParticleVector2 SpeedRange, int TilesX, int TilesY,
    int AnimationType, int RowIndex, float Cycles, int UvChannelMask,
    int RowMode, ParticleAssetPointer[] Sprites, float FlipU, float FlipV);
internal sealed record ParticleVector2(float X, float Y);
