using System.Collections.Specialized;
using AnimeStudio;
using Object = AnimeStudio.Object;

internal static partial class Program
{
    private static T DecalField<T>(OrderedDictionary fields, string name, Object owner)
    {
        if (fields[name] is T value)
            return value;
        throw new InvalidDataException(
            $"DECAL_FIELD_INVALID source={owner.assetsFile.fileName} pathId={owner.m_PathID} field={name} expected={typeof(T).Name}");
    }

    private static float DecalFloat(OrderedDictionary fields, string name, Object owner)
    {
        var value = DecalField<float>(fields, name, owner);
        if (!float.IsFinite(value))
            throw new InvalidDataException($"DECAL_NONFINITE pathId={owner.m_PathID} field={name} value={value}");
        return value;
    }

    private static float[] DecalVector(OrderedDictionary fields, string name, string[] channels, Object owner)
    {
        var vector = DecalField<OrderedDictionary>(fields, name, owner);
        return channels.Select(channel => DecalFloat(vector, channel, owner)).ToArray();
    }

    private static PPtr<T> DecalPointer<T>(OrderedDictionary fields, string name, Object owner) where T : Object
    {
        var pointer = DecalField<OrderedDictionary>(fields, name, owner);
        return new PPtr<T>(DecalField<int>(pointer, "m_FileID", owner),
            DecalField<long>(pointer, "m_PathID", owner), owner.assetsFile);
    }

    private static (DecalProjectorRecord Record, Material Material) ReadDecalProjector(Object obj)
    {
        var fields = obj.ToType() ?? throw new InvalidDataException(
            $"DECAL_TYPE_TREE_MISSING source={obj.assetsFile.fileName} pathId={obj.m_PathID}");
        var materialPointer = DecalPointer<Material>(fields, "m_Material", obj);
        if (!materialPointer.TryGet(out var material))
            throw new InvalidDataException(
                $"DECAL_MATERIAL_MISSING source={obj.assetsFile.fileName} pathId={obj.m_PathID} fileId={materialPointer.m_FileID} materialPathId={materialPointer.m_PathID}");
        var gameObjectPointer = DecalPointer<GameObject>(fields, "m_GameObject", obj);
        if (!gameObjectPointer.TryGet(out var gameObject))
            throw new InvalidDataException($"DECAL_GAME_OBJECT_MISSING source={obj.assetsFile.fileName} pathId={obj.m_PathID}");
        var customMesh = DecalPointer<Mesh>(fields, "m_CustomMesh", obj);
        return (new DecalProjectorRecord(
            ToRef(obj), ToRef(gameObject), ToRef(material),
            DecalField<byte>(fields, "m_Enabled", obj) != 0,
            DecalField<int>(fields, "m_ShapeType", obj),
            DecalField<int>(fields, "m_CullingOption", obj),
            DecalVector(fields, "m_BaseColor", ["r", "g", "b", "a"], obj),
            DecalFloat(fields, "m_BaseColorIntensity", obj),
            DecalVector(fields, "m_UvTilling", ["x", "y"], obj),
            DecalVector(fields, "m_UvOffset", ["x", "y"], obj),
            DecalFloat(fields, "m_SectorAngle", obj),
            DecalField<int>(fields, "m_SortOrderOffset", obj),
            DecalFloat(fields, "m_LODScreenSizeMin", obj),
            customMesh.IsNull ? null : ToRef(customMesh, obj.assetsFile),
            Convert.ToHexString(obj.GetRawData())), material);
    }
}

internal sealed record DecalProjectorRecord(
    AssetRef Asset, AssetRef GameObject, AssetRef Material, bool Enabled,
    int ShapeType, int CullingOption, float[] BaseColor, float BaseColorIntensity,
    float[] UvTiling, float[] UvOffset, float SectorAngle, int SortOrderOffset,
    float LodScreenSizeMin, AssetRef? CustomMesh, string RawHex);
