using System.Collections.Specialized;
using AnimeStudio;
using Object = AnimeStudio.Object;

internal static partial class Program
{
    private static readonly (string Name, string Type)[] WaterHeaderSignature =
    [
        ("m_GameObject", "PPtr<GameObject>"),
        ("m_Enabled", "UInt8"),
        ("m_Script", "PPtr<MonoScript>"),
        ("m_Name", "string"),
    ];

    private static readonly (string Name, string Type)[] WaterGlobalSignature =
    [
        ("scenePath", "string"),
        ("sceneCenterOffset", "Vector2f"),
        ("useSectorData", "UInt8"),
        ("sectorDataPaths", "vector"),
        ("sectorDataAssetHashes", "vector"),
        ("sectorDataExists", "vector"),
        ("mapSize", "int"),
        ("sectorSize", "int"),
        ("flowmapPrecision", "float"),
        ("lodNum", "int"),
        ("meshNumPerLOD", "int"),
        ("meshSize", "int"),
        ("heightMapXZ", "int"),
        ("heightMapY", "int"),
        ("heightMapOffset", "float"),
        ("hgWaterConfigs", "vector"),
        ("flowMap", "PPtr<$Texture2D>"),
        ("causticMap", "PPtr<$Texture2D>"),
        ("rainMap", "PPtr<$Texture2D>"),
        ("normalMapArray", "PPtr<$Texture2DArray>"),
    ];

    private static readonly (string Name, string Type)[] WaterConfigSignature =
    [
        ("isGamePlayConfig", "UInt8"),
        ("useFlowMap", "UInt8"),
        ("materialIndex", "int"),
        ("flowDirection", "float"),
        ("flowDirectionVector2", "Vector2f"),
        ("smallWaveNormalType", "int"),
        ("flowSpeed", "float"),
        ("normalTilling", "float"),
        ("normalScale", "float"),
        ("flowFullCycle", "float"),
        ("waveVertexDisplacementScalerSmall", "float"),
        ("largeWaveNormalType", "int"),
        ("largeWaveFlowSpeed", "float"),
        ("largeWaveNormalTilling", "float"),
        ("largeWaveNormalScale", "float"),
        ("flowFullCycleLarge", "float"),
        ("waveVertexDisplacementScaler", "float"),
        ("largeWaveInvDir", "UInt8"),
        ("displacementNormalStrength", "float"),
        ("absorptionTint", "ColorRGBA"),
        ("absorptionTint2", "ColorRGBA"),
        ("scatterTint", "ColorRGBA"),
        ("waterRayleighScatteringMeter", "Vector4f"),
        ("waterRayleighAbsorptionMeter", "Vector4f"),
        ("waterRayleighAbsorptionMeter2", "Vector4f"),
        ("absorptionScale", "float"),
        ("distanceEdgeBlendFactor", "float"),
        ("rayleighMieLerpFactor", "float"),
        ("scatterScale", "float"),
        ("envLightIntensity", "float"),
        ("sceneColorEnvInfluence", "float"),
        ("safeFullAbsorpDistance", "float"),
        ("flowBaseColor", "ColorRGBA"),
        ("foamColorG", "ColorRGBA"),
        ("foamOpacity", "float"),
        ("foamRoughness", "float"),
        ("foamShapeOffset", "float"),
        ("foamFadeDistance", "float"),
        ("foamDisplacementDistance", "float"),
        ("emissiveStrength", "float"),
        ("roughness", "float"),
        ("specular", "float"),
        ("waterIOR", "float"),
        ("distanceFade", "float"),
        ("distanceNormalMinStrength", "float"),
        ("envLightSpecularIntensity", "float"),
        ("dirSpecNormalScaler", "float"),
        ("waterBaseColorSubsurfaceScaler", "float"),
        ("envSpecularDesaturation", "float"),
        ("TAAManualAlphaMode", "int"),
        ("causticTilling", "float"),
        ("causticOpacity", "float"),
        ("causticDistort", "float"),
        ("causticSpeed", "float"),
        ("causticStartDepth", "float"),
        ("causticEndDepth", "float"),
        ("distanceBlend", "float"),
        ("interactiveDamp", "float"),
        ("interactiveAlpha", "float"),
        ("interactiveBeta", "float"),
        ("interactiveSpeed", "float"),
        ("rippleNormalStrength", "float"),
        ("rippleNormalEdgeWidth", "float"),
        ("rippleHeightDisplacement", "float"),
        ("interactiveRippleSize", "float"),
        ("interactiveRippleForwardOffset", "float"),
        ("stillRippleFrequency", "float"),
        ("moveRippleFrequency", "float"),
    ];

    private static readonly (string Name, string Type)[] WaterSectorSignature =
    [
        ("waterSectorTexture0", "PPtr<$Texture2D>"),
        ("rippleLayerBuffer", "vector"),
    ];

    private static readonly string[] WaterGlobalMarkers =
    ["hgWaterConfigs", "sectorDataPaths", "normalMapArray"];

    private static readonly string[] WaterConfigMarkers =
    ["isGamePlayConfig", "flowDirectionVector2", "waterIOR"];

    private static readonly string[] WaterSectorMarkers =
    ["waterSectorTexture0", "rippleLayerBuffer"];

    /// <summary>Reads supported water MonoBehaviours after filtering their serialized type tree signatures.</summary>
    private static WaterRecords ReadWaterRecords(IEnumerable<Object> allObjects)
    {
        ArgumentNullException.ThrowIfNull(allObjects);
        var globals = new List<WaterGlobalConfigRecord>();
        var configs = new List<WaterConfigRecord>();
        var sectors = new List<WaterSectorRecord>();

        foreach (var obj in allObjects)
        {
            if (obj.type != ClassIDType.MonoBehaviour)
                continue;

            switch (WaterClassify(obj))
            {
                case WaterSchemaKind.Global:
                    globals.Add(ReadWaterGlobalConfig(obj));
                    break;
                case WaterSchemaKind.Config:
                    configs.Add(ReadWaterConfig(obj));
                    break;
                case WaterSchemaKind.Sector:
                    sectors.Add(ReadWaterSector(obj));
                    break;
                case WaterSchemaKind.None:
                    break;
                default:
                    throw new InvalidDataException(
                        $"WATER_SCHEMA_KIND_UNKNOWN source={obj.assetsFile.fileName} pathId={obj.m_PathID}");
            }
        }

        return new WaterRecords(
            globals.OrderBy(value => value.Header.Asset.Source, StringComparer.Ordinal)
                .ThenBy(value => value.Header.Asset.PathId).ToArray(),
            configs.OrderBy(value => value.Header.Asset.Source, StringComparer.Ordinal)
                .ThenBy(value => value.Header.Asset.PathId).ToArray(),
            sectors.OrderBy(value => value.Header.Asset.Source, StringComparer.Ordinal)
                .ThenBy(value => value.Header.Asset.PathId).ToArray());
    }

    private static WaterSchemaKind WaterClassify(Object obj)
    {
        var nodes = obj.serializedType?.m_Type?.m_Nodes;
        if (nodes is null || nodes.Count < 2)
            return WaterSchemaKind.None;

        var rootLevel = nodes[0].m_Level;
        var topFields = nodes.Where(node => node.m_Level == rootLevel + 1).ToArray();
        var hasGlobalMarker = WaterHasMarker(topFields, WaterGlobalMarkers);
        var hasConfigMarker = WaterHasMarker(topFields, WaterConfigMarkers);
        var hasSectorMarker = WaterHasMarker(topFields, WaterSectorMarkers);
        var markerCount = (hasGlobalMarker ? 1 : 0) + (hasConfigMarker ? 1 : 0) +
                          (hasSectorMarker ? 1 : 0);
        if (markerCount == 0)
            return WaterSchemaKind.None;
        if (markerCount != 1)
            throw new InvalidDataException(
                $"WATER_SCHEMA_CLASSIFICATION_AMBIGUOUS source={obj.assetsFile.fileName} pathId={obj.m_PathID}");

        var kind = hasGlobalMarker ? WaterSchemaKind.Global :
            hasConfigMarker ? WaterSchemaKind.Config : WaterSchemaKind.Sector;
        var specific = kind switch
        {
            WaterSchemaKind.Global => WaterGlobalSignature,
            WaterSchemaKind.Config => WaterConfigSignature,
            WaterSchemaKind.Sector => WaterSectorSignature,
            _ => throw new InvalidDataException(
                $"WATER_SCHEMA_KIND_UNKNOWN source={obj.assetsFile.fileName} pathId={obj.m_PathID}"),
        };
        WaterAssertSignature(obj, kind, topFields, specific);
        return kind;
    }

    private static bool WaterHasMarker(TypeTreeNode[] fields, string[] markers)
    {
        foreach (var field in fields)
        {
            foreach (var marker in markers)
            {
                if (field.m_Name == marker)
                    return true;
            }
        }
        return false;
    }

    private static void WaterAssertSignature(
        Object owner,
        WaterSchemaKind kind,
        TypeTreeNode[] actual,
        (string Name, string Type)[] specific)
    {
        var fieldsByName = new Dictionary<string, TypeTreeNode>(StringComparer.Ordinal);
        foreach (var field in actual)
        {
            if (!fieldsByName.TryAdd(field.m_Name, field))
                throw new InvalidDataException(
                    $"WATER_SCHEMA_UNSUPPORTED source={owner.assetsFile.fileName} pathId={owner.m_PathID} " +
                    $"kind={kind} duplicateField={field.m_Name}");
        }

        foreach (var expected in WaterHeaderSignature.Concat(specific))
        {
            if (!fieldsByName.TryGetValue(expected.Name, out var actualField))
                throw new InvalidDataException(
                    $"WATER_SCHEMA_UNSUPPORTED source={owner.assetsFile.fileName} pathId={owner.m_PathID} " +
                    $"kind={kind} missingField={expected.Name} expectedType={expected.Type}");
            if (actualField.m_Type != expected.Type)
                throw new InvalidDataException(
                    $"WATER_SCHEMA_UNSUPPORTED source={owner.assetsFile.fileName} pathId={owner.m_PathID} " +
                    $"kind={kind} field={expected.Name} expectedType={expected.Type} actualType={actualField.m_Type}");
        }
    }

    private static WaterComponentHeader ReadWaterHeader(OrderedDictionary fields, Object owner)
    {
        return new WaterComponentHeader(
            ToRef(owner),
            ReadWaterPointer(fields, "m_GameObject", owner, "GameObject"),
            WaterField<byte>(fields, "m_Enabled", owner),
            ReadWaterPointer(fields, "m_Script", owner, "MonoScript"),
            WaterField<string>(fields, "m_Name", owner));
    }

    private static WaterGlobalConfigRecord ReadWaterGlobalConfig(Object obj)
    {
        var fields = ReadWaterFields(obj, WaterSchemaKind.Global);
        var mapSize = WaterField<int>(fields, "mapSize", obj);
        var sectorSize = WaterField<int>(fields, "sectorSize", obj);
        if (mapSize <= 0 || sectorSize <= 0 || mapSize % sectorSize != 0)
            throw new InvalidDataException(
                $"WATER_GLOBAL_DIMENSIONS_INVALID source={obj.assetsFile.fileName} pathId={obj.m_PathID} " +
                $"mapSize={mapSize} sectorSize={sectorSize}");

        var paths = WaterArray(fields, "sectorDataPaths", obj,
            (value, index) => WaterValue<string>(value, $"sectorDataPaths[{index}]", obj));
        var hashes = WaterArray(fields, "sectorDataAssetHashes", obj,
            (value, index) => WaterValue<long>(value, $"sectorDataAssetHashes[{index}]", obj));
        var exists = WaterArray(fields, "sectorDataExists", obj,
            (value, index) => WaterFlagValue(value, $"sectorDataExists[{index}]", obj));
        var expectedSectorCount = checked((mapSize / sectorSize) * (mapSize / sectorSize));
        if (paths.Length != expectedSectorCount || hashes.Length != expectedSectorCount ||
            exists.Length != expectedSectorCount)
            throw new InvalidDataException(
                $"WATER_GLOBAL_SECTOR_ARRAY_LENGTH_INVALID source={obj.assetsFile.fileName} pathId={obj.m_PathID} " +
                $"expected={expectedSectorCount} paths={paths.Length} hashes={hashes.Length} exists={exists.Length}");

        var waterConfigs = WaterArray(fields, "hgWaterConfigs", obj, (value, index) =>
        {
            var config = ReadWaterPointerValue(value, $"hgWaterConfigs[{index}]", obj, "HGWaterConfig");
            if (config.IsResolved && config.Target.Type != ClassIDType.MonoBehaviour.ToString())
                throw new InvalidDataException(
                    $"WATER_CONFIG_POINTER_TYPE_INVALID source={obj.assetsFile.fileName} pathId={obj.m_PathID} " +
                    $"index={index} targetType={config.Target.Type}");
            return config;
        });

        return new WaterGlobalConfigRecord(
            ReadWaterHeader(fields, obj),
            WaterField<string>(fields, "scenePath", obj),
            ReadWaterVector2(fields, "sceneCenterOffset", obj),
            WaterField<byte>(fields, "useSectorData", obj),
            paths,
            hashes,
            exists,
            mapSize,
            sectorSize,
            WaterFloat(fields, "flowmapPrecision", obj),
            WaterField<int>(fields, "lodNum", obj),
            WaterField<int>(fields, "meshNumPerLOD", obj),
            WaterField<int>(fields, "meshSize", obj),
            WaterField<int>(fields, "heightMapXZ", obj),
            WaterField<int>(fields, "heightMapY", obj),
            WaterFloat(fields, "heightMapOffset", obj),
            waterConfigs,
            ReadWaterPointer(fields, "flowMap", obj, "Texture2D"),
            ReadWaterPointer(fields, "causticMap", obj, "Texture2D"),
            ReadWaterPointer(fields, "rainMap", obj, "Texture2D"),
            ReadWaterPointer(fields, "normalMapArray", obj, "Texture2DArray"));
    }

    private static WaterConfigRecord ReadWaterConfig(Object obj)
    {
        var fields = ReadWaterFields(obj, WaterSchemaKind.Config);
        var flow = new WaterConfigFlowRecord(
            WaterField<byte>(fields, "isGamePlayConfig", obj),
            WaterField<byte>(fields, "useFlowMap", obj),
            WaterField<int>(fields, "materialIndex", obj),
            WaterFloat(fields, "flowDirection", obj),
            ReadWaterVector2(fields, "flowDirectionVector2", obj),
            WaterField<int>(fields, "smallWaveNormalType", obj),
            WaterFloat(fields, "flowSpeed", obj),
            WaterFloat(fields, "normalTilling", obj),
            WaterFloat(fields, "normalScale", obj),
            WaterFloat(fields, "flowFullCycle", obj),
            WaterFloat(fields, "waveVertexDisplacementScalerSmall", obj),
            WaterField<int>(fields, "largeWaveNormalType", obj),
            WaterFloat(fields, "largeWaveFlowSpeed", obj),
            WaterFloat(fields, "largeWaveNormalTilling", obj),
            WaterFloat(fields, "largeWaveNormalScale", obj),
            WaterFloat(fields, "flowFullCycleLarge", obj),
            WaterFloat(fields, "waveVertexDisplacementScaler", obj),
            WaterField<byte>(fields, "largeWaveInvDir", obj),
            WaterFloat(fields, "displacementNormalStrength", obj));

        var optics = new WaterConfigOpticsRecord(
            ReadWaterColor(fields, "absorptionTint", obj),
            ReadWaterColor(fields, "absorptionTint2", obj),
            ReadWaterColor(fields, "scatterTint", obj),
            ReadWaterVector4(fields, "waterRayleighScatteringMeter", obj),
            ReadWaterVector4(fields, "waterRayleighAbsorptionMeter", obj),
            ReadWaterVector4(fields, "waterRayleighAbsorptionMeter2", obj),
            WaterFloat(fields, "absorptionScale", obj),
            WaterFloat(fields, "distanceEdgeBlendFactor", obj),
            WaterFloat(fields, "rayleighMieLerpFactor", obj),
            WaterFloat(fields, "scatterScale", obj),
            WaterFloat(fields, "envLightIntensity", obj),
            WaterFloat(fields, "sceneColorEnvInfluence", obj),
            WaterFloat(fields, "safeFullAbsorpDistance", obj));

        var appearance = new WaterConfigAppearanceRecord(
            ReadWaterColor(fields, "flowBaseColor", obj),
            ReadWaterColor(fields, "foamColorG", obj),
            WaterFloat(fields, "foamOpacity", obj),
            WaterFloat(fields, "foamRoughness", obj),
            WaterFloat(fields, "foamShapeOffset", obj),
            WaterFloat(fields, "foamFadeDistance", obj),
            WaterFloat(fields, "foamDisplacementDistance", obj),
            WaterFloat(fields, "emissiveStrength", obj),
            WaterFloat(fields, "roughness", obj),
            WaterFloat(fields, "specular", obj),
            WaterFloat(fields, "waterIOR", obj),
            WaterFloat(fields, "distanceFade", obj),
            WaterFloat(fields, "distanceNormalMinStrength", obj),
            WaterFloat(fields, "envLightSpecularIntensity", obj),
            WaterFloat(fields, "dirSpecNormalScaler", obj),
            WaterFloat(fields, "waterBaseColorSubsurfaceScaler", obj),
            WaterFloat(fields, "envSpecularDesaturation", obj),
            WaterField<int>(fields, "TAAManualAlphaMode", obj),
            WaterFloat(fields, "causticTilling", obj),
            WaterFloat(fields, "causticOpacity", obj),
            WaterFloat(fields, "causticDistort", obj),
            WaterFloat(fields, "causticSpeed", obj),
            WaterFloat(fields, "causticStartDepth", obj),
            WaterFloat(fields, "causticEndDepth", obj),
            WaterFloat(fields, "distanceBlend", obj));

        var interaction = new WaterConfigInteractionRecord(
            WaterFloat(fields, "interactiveDamp", obj),
            WaterFloat(fields, "interactiveAlpha", obj),
            WaterFloat(fields, "interactiveBeta", obj),
            WaterFloat(fields, "interactiveSpeed", obj),
            WaterFloat(fields, "rippleNormalStrength", obj),
            WaterFloat(fields, "rippleNormalEdgeWidth", obj),
            WaterFloat(fields, "rippleHeightDisplacement", obj),
            WaterFloat(fields, "interactiveRippleSize", obj),
            WaterFloat(fields, "interactiveRippleForwardOffset", obj),
            WaterFloat(fields, "stillRippleFrequency", obj),
            WaterFloat(fields, "moveRippleFrequency", obj));

        return new WaterConfigRecord(ReadWaterHeader(fields, obj), flow, optics, appearance, interaction);
    }

    private static WaterSectorRecord ReadWaterSector(Object obj)
    {
        var fields = ReadWaterFields(obj, WaterSchemaKind.Sector);
        var ripples = WaterArray(fields, "rippleLayerBuffer", obj, (value, index) =>
            ReadWaterVector4Value(value, $"rippleLayerBuffer[{index}]", obj));
        if (ripples.Length != 64)
            throw new InvalidDataException(
                $"WATER_SECTOR_RIPPLE_COUNT_INVALID source={obj.assetsFile.fileName} pathId={obj.m_PathID} " +
                $"expected=64 actual={ripples.Length}");
        return new WaterSectorRecord(
            ReadWaterHeader(fields, obj),
            ReadWaterPointer(fields, "waterSectorTexture0", obj, "Texture2D"),
            ripples);
    }

    private static OrderedDictionary ReadWaterFields(Object obj, WaterSchemaKind kind)
    {
        var fields = obj.ToType() ?? throw new InvalidDataException(
            $"WATER_TYPE_TREE_MISSING source={obj.assetsFile.fileName} pathId={obj.m_PathID} kind={kind}");
        var bytesRead = obj.reader.Position - obj.reader.byteStart;
        if (bytesRead != obj.byteSize)
            throw new InvalidDataException(
                $"WATER_TYPE_TREE_SIZE_MISMATCH source={obj.assetsFile.fileName} pathId={obj.m_PathID} " +
                $"kind={kind} bytesRead={bytesRead} byteSize={obj.byteSize}");
        return fields;
    }

    private static WaterAssetPointer ReadWaterPointer(
        OrderedDictionary fields,
        string name,
        Object owner,
        string declaredType)
    {
        var pointer = WaterField<OrderedDictionary>(fields, name, owner);
        return ReadWaterPointerValue(pointer, name, owner, declaredType);
    }

    private static WaterAssetPointer ReadWaterPointerValue(
        object value,
        string field,
        Object owner,
        string declaredType)
    {
        var pointerFields = WaterValue<OrderedDictionary>(value, field, owner);
        var fileId = WaterField<int>(pointerFields, "m_FileID", $"{field}.m_FileID", owner);
        var pathId = WaterField<long>(pointerFields, "m_PathID", $"{field}.m_PathID", owner);
        var pointer = new PPtr<Object>(fileId, pathId, owner.assetsFile);
        var isResolved = pointer.TryGet(out var target);
        if (isResolved)
            WaterAssertPointerTarget(target, declaredType, owner, field);

        var targetRef = isResolved
            ? ToRef(target)
            : new AssetRef(WaterPointerSource(pointer, owner), pathId, declaredType, null);
        return new WaterAssetPointer(fileId, pathId, declaredType, pointer.IsNull, isResolved, targetRef);
    }

    private static void WaterAssertPointerTarget(
        Object target,
        string declaredType,
        Object owner,
        string field)
    {
        if (declaredType == "HGWaterConfig")
        {
            if (target.type != ClassIDType.MonoBehaviour || WaterClassify(target) != WaterSchemaKind.Config)
                throw new InvalidDataException(
                    $"WATER_POINTER_TARGET_SCHEMA_INVALID source={owner.assetsFile.fileName} pathId={owner.m_PathID} " +
                    $"field={field} targetSource={target.assetsFile.fileName} targetPathId={target.m_PathID}");
            return;
        }

        if (!Enum.TryParse<ClassIDType>(declaredType, false, out var expectedType))
            throw new InvalidDataException(
                $"WATER_POINTER_DECLARED_TYPE_INVALID source={owner.assetsFile.fileName} pathId={owner.m_PathID} " +
                $"field={field} declaredType={declaredType}");
        if (target.type != expectedType)
            throw new InvalidDataException(
                $"WATER_POINTER_TARGET_TYPE_INVALID source={owner.assetsFile.fileName} pathId={owner.m_PathID} " +
                $"field={field} declaredType={declaredType} actualType={target.type}");
    }

    private static string WaterPointerSource(PPtr<Object> pointer, Object owner)
    {
        if (pointer.m_FileID == 0)
            return owner.assetsFile.fileName;
        if (pointer.m_FileID > 0 && pointer.m_FileID <= owner.assetsFile.m_Externals.Count)
            return owner.assetsFile.m_Externals[pointer.m_FileID - 1].fileName;
        throw new InvalidDataException(
            $"WATER_POINTER_FILE_ID_INVALID source={owner.assetsFile.fileName} pathId={owner.m_PathID} " +
            $"fileId={pointer.m_FileID} externalCount={owner.assetsFile.m_Externals.Count}");
    }

    private static WaterVector2 ReadWaterVector2(OrderedDictionary fields, string name, Object owner)
    {
        var vector = WaterField<OrderedDictionary>(fields, name, owner);
        return new WaterVector2(
            WaterFloat(vector, "x", $"{name}.x", owner),
            WaterFloat(vector, "y", $"{name}.y", owner));
    }

    private static WaterVector4 ReadWaterVector4(OrderedDictionary fields, string name, Object owner)
    {
        var vector = WaterField<OrderedDictionary>(fields, name, owner);
        return ReadWaterVector4Value(vector, name, owner);
    }

    private static WaterVector4 ReadWaterVector4Value(object value, string name, Object owner)
    {
        var vector = WaterValue<OrderedDictionary>(value, name, owner);
        return new WaterVector4(
            WaterFloat(vector, "x", $"{name}.x", owner),
            WaterFloat(vector, "y", $"{name}.y", owner),
            WaterFloat(vector, "z", $"{name}.z", owner),
            WaterFloat(vector, "w", $"{name}.w", owner));
    }

    private static WaterColor ReadWaterColor(OrderedDictionary fields, string name, Object owner)
    {
        var color = WaterField<OrderedDictionary>(fields, name, owner);
        return new WaterColor(
            WaterFloat(color, "r", $"{name}.r", owner),
            WaterFloat(color, "g", $"{name}.g", owner),
            WaterFloat(color, "b", $"{name}.b", owner),
            WaterFloat(color, "a", $"{name}.a", owner));
    }

    private static T[] WaterArray<T>(
        OrderedDictionary fields,
        string name,
        Object owner,
        Func<object, int, T> readElement)
    {
        var values = WaterField<List<object>>(fields, name, owner);
        var result = new T[values.Count];
        for (var index = 0; index < values.Count; index++)
            result[index] = readElement(values[index], index);
        return result;
    }

    private static float WaterFloat(OrderedDictionary fields, string name, Object owner)
    {
        return WaterFloat(fields, name, name, owner);
    }

    private static float WaterFloat(
        OrderedDictionary fields,
        string name,
        string context,
        Object owner)
    {
        var value = WaterField<float>(fields, name, context, owner);
        if (!float.IsFinite(value))
            throw new InvalidDataException(
                $"WATER_FLOAT_NONFINITE source={owner.assetsFile.fileName} pathId={owner.m_PathID} " +
                $"field={context} value={value}");
        return value;
    }

    private static T WaterField<T>(OrderedDictionary fields, string name, Object owner)
    {
        return WaterField<T>(fields, name, name, owner);
    }

    private static T WaterField<T>(OrderedDictionary fields, string name, string context, Object owner)
    {
        if (fields[name] is T value)
            return value;
        throw new InvalidDataException(
            $"WATER_FIELD_TYPE_INVALID source={owner.assetsFile.fileName} pathId={owner.m_PathID} " +
            $"field={context} expected={typeof(T).Name} actual={fields[name]?.GetType().Name ?? "missing"}");
    }

    private static T WaterValue<T>(object value, string context, Object owner)
    {
        if (value is T typed)
            return typed;
        throw new InvalidDataException(
            $"WATER_ARRAY_ELEMENT_TYPE_INVALID source={owner.assetsFile.fileName} pathId={owner.m_PathID} " +
            $"field={context} expected={typeof(T).Name} actual={value?.GetType().Name ?? "null"}");
    }

    private static bool WaterFlagValue(object value, string context, Object owner)
    {
        var raw = WaterValue<byte>(value, context, owner);
        return raw switch
        {
            0 => false,
            1 => true,
            _ => throw new InvalidDataException(
                $"WATER_FLAG_VALUE_INVALID source={owner.assetsFile.fileName} pathId={owner.m_PathID} " +
                $"field={context} value={raw}"),
        };
    }

    private enum WaterSchemaKind
    {
        None,
        Global,
        Config,
        Sector,
    }
}

internal sealed record WaterRecords(
    WaterGlobalConfigRecord[] GlobalConfigs,
    WaterConfigRecord[] Configs,
    WaterSectorRecord[] Sectors);

internal sealed record WaterComponentHeader(
    AssetRef Asset,
    WaterAssetPointer GameObject,
    byte Enabled,
    WaterAssetPointer Script,
    string Name);

internal sealed record WaterAssetPointer(
    int FileId,
    long PathId,
    string DeclaredType,
    bool IsNull,
    bool IsResolved,
    AssetRef Target);

internal sealed record WaterGlobalConfigRecord(
    WaterComponentHeader Header,
    string ScenePath,
    WaterVector2 SceneCenterOffset,
    byte UseSectorData,
    string[] SectorDataPaths,
    long[] SectorDataAssetHashes,
    bool[] SectorDataExists,
    int MapSize,
    int SectorSize,
    float FlowmapPrecision,
    int LodNum,
    int MeshNumPerLod,
    int MeshSize,
    int HeightMapXz,
    int HeightMapY,
    float HeightMapOffset,
    WaterAssetPointer[] HgWaterConfigs,
    WaterAssetPointer FlowMap,
    WaterAssetPointer CausticMap,
    WaterAssetPointer RainMap,
    WaterAssetPointer NormalMapArray);

internal sealed record WaterConfigRecord(
    WaterComponentHeader Header,
    WaterConfigFlowRecord Flow,
    WaterConfigOpticsRecord Optics,
    WaterConfigAppearanceRecord Appearance,
    WaterConfigInteractionRecord Interaction);

internal sealed record WaterConfigFlowRecord(
    byte IsGamePlayConfig,
    byte UseFlowMap,
    int MaterialIndex,
    float FlowDirection,
    WaterVector2 FlowDirectionVector2,
    int SmallWaveNormalType,
    float FlowSpeed,
    float NormalTilling,
    float NormalScale,
    float FlowFullCycle,
    float WaveVertexDisplacementScalerSmall,
    int LargeWaveNormalType,
    float LargeWaveFlowSpeed,
    float LargeWaveNormalTilling,
    float LargeWaveNormalScale,
    float FlowFullCycleLarge,
    float WaveVertexDisplacementScaler,
    byte LargeWaveInvDir,
    float DisplacementNormalStrength);

internal sealed record WaterConfigOpticsRecord(
    WaterColor AbsorptionTint,
    WaterColor AbsorptionTint2,
    WaterColor ScatterTint,
    WaterVector4 WaterRayleighScatteringMeter,
    WaterVector4 WaterRayleighAbsorptionMeter,
    WaterVector4 WaterRayleighAbsorptionMeter2,
    float AbsorptionScale,
    float DistanceEdgeBlendFactor,
    float RayleighMieLerpFactor,
    float ScatterScale,
    float EnvLightIntensity,
    float SceneColorEnvInfluence,
    float SafeFullAbsorpDistance);

internal sealed record WaterConfigAppearanceRecord(
    WaterColor FlowBaseColor,
    WaterColor FoamColorG,
    float FoamOpacity,
    float FoamRoughness,
    float FoamShapeOffset,
    float FoamFadeDistance,
    float FoamDisplacementDistance,
    float EmissiveStrength,
    float Roughness,
    float Specular,
    float WaterIor,
    float DistanceFade,
    float DistanceNormalMinStrength,
    float EnvLightSpecularIntensity,
    float DirSpecNormalScaler,
    float WaterBaseColorSubsurfaceScaler,
    float EnvSpecularDesaturation,
    int TaaManualAlphaMode,
    float CausticTilling,
    float CausticOpacity,
    float CausticDistort,
    float CausticSpeed,
    float CausticStartDepth,
    float CausticEndDepth,
    float DistanceBlend);

internal sealed record WaterConfigInteractionRecord(
    float InteractiveDamp,
    float InteractiveAlpha,
    float InteractiveBeta,
    float InteractiveSpeed,
    float RippleNormalStrength,
    float RippleNormalEdgeWidth,
    float RippleHeightDisplacement,
    float InteractiveRippleSize,
    float InteractiveRippleForwardOffset,
    float StillRippleFrequency,
    float MoveRippleFrequency);

internal sealed record WaterSectorRecord(
    WaterComponentHeader Header,
    WaterAssetPointer WaterSectorTexture0,
    WaterVector4[] RippleLayerBuffer);

internal readonly record struct WaterVector2(float X, float Y);

internal readonly record struct WaterVector4(float X, float Y, float Z, float W);

internal readonly record struct WaterColor(float R, float G, float B, float A);
