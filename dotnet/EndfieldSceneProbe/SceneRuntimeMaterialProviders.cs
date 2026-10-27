using System.Collections.Specialized;
using AnimeStudio;
using Object = AnimeStudio.Object;

internal static partial class Program
{
    private const string RuntimeFogNamespace = "HG.Rendering.Runtime";
    private const string RuntimeFogClass = "VFXFakeVolumeFog";

    private static List<RuntimeMaterialProviderRecord> ReadRuntimeMaterialProviders(GameObject gameObject)
    {
        var providers = new List<RuntimeMaterialProviderRecord>();
        foreach (var componentPointer in gameObject.m_Components)
        {
            if (!componentPointer.TryGet(out var component) || component is not MonoBehaviour behaviour)
                continue;
            if (!behaviour.m_GameObject.TryGet(out var owner) || !SameAsset(owner, gameObject))
                continue;
            if (!behaviour.m_Script.TryGet(out var script) ||
                !string.Equals(script.m_Namespace, RuntimeFogNamespace, StringComparison.Ordinal) ||
                !string.Equals(script.m_ClassName, RuntimeFogClass, StringComparison.Ordinal) ||
                behaviour.m_Enabled == 0)
                continue;

            var fields = behaviour.ToType() ?? throw new InvalidDataException(
                $"RUNTIME_MATERIAL_PROVIDER_TYPE_TREE_MISSING source={behaviour.assetsFile.fileName} " +
                $"pathId={behaviour.m_PathID} namespace={script.m_Namespace} class={script.m_ClassName}");
            providers.Add(new RuntimeMaterialProviderRecord(
                ToRef(behaviour),
                ToRef(script),
                script.m_Namespace,
                script.m_ClassName,
                behaviour.m_Enabled != 0,
                ReadRuntimeBoolean(fields, "useFog", behaviour),
                ReadRuntimeFloat(fields, "fogIntensity", behaviour)));
        }
        return providers;
    }

    private static bool SameAsset(Object left, Object right) =>
        left.m_PathID == right.m_PathID &&
        string.Equals(left.assetsFile.fileName, right.assetsFile.fileName, StringComparison.Ordinal);

    private static bool ReadRuntimeBoolean(OrderedDictionary fields, string field, Object owner)
    {
        if (!fields.Contains(field))
            throw new InvalidDataException(
                $"RUNTIME_MATERIAL_PROVIDER_FIELD_MISSING source={owner.assetsFile.fileName} " +
                $"pathId={owner.m_PathID} field={field}");
        return fields[field] switch
        {
            bool value => value,
            byte value when value is 0 or 1 => value != 0,
            byte value => throw new InvalidDataException(
                $"RUNTIME_MATERIAL_PROVIDER_BOOLEAN_INVALID source={owner.assetsFile.fileName} " +
                $"pathId={owner.m_PathID} field={field} value={value}"),
            var value => throw new InvalidDataException(
                $"RUNTIME_MATERIAL_PROVIDER_BOOLEAN_TYPE_INVALID source={owner.assetsFile.fileName} " +
                $"pathId={owner.m_PathID} field={field} actual={value?.GetType().Name ?? "missing"}"),
        };
    }

    private static float ReadRuntimeFloat(OrderedDictionary fields, string field, Object owner)
    {
        if (!fields.Contains(field))
            throw new InvalidDataException(
                $"RUNTIME_MATERIAL_PROVIDER_FIELD_MISSING source={owner.assetsFile.fileName} " +
                $"pathId={owner.m_PathID} field={field}");
        if (fields[field] is not float value || !float.IsFinite(value))
            throw new InvalidDataException(
                $"RUNTIME_MATERIAL_PROVIDER_FLOAT_INVALID source={owner.assetsFile.fileName} " +
                $"pathId={owner.m_PathID} field={field} actual={fields[field]?.GetType().Name ?? "missing"}");
        return value;
    }
}

internal sealed record RuntimeMaterialProviderRecord(
    AssetRef ComponentAsset,
    AssetRef ScriptAsset,
    string Namespace,
    string Class,
    bool Enabled,
    bool UseFog,
    float FogIntensity);
