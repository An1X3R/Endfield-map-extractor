using System.Security.Cryptography;
using System.Text.Json;
using AnimeStudio;

/// <summary>Selects the exact source clip for enemy receipts and preserves container lookup elsewhere.</summary>
internal static class AnimationClipSelector
{
    internal static AnimationClip Select(AnimationRequest request, IEnumerable<SerializedFile> files)
    {
        using var audit = JsonDocument.Parse(File.ReadAllBytes(request.SourceAudit));
        if (audit.RootElement.GetProperty("format").GetString() != "EndfieldEnemyAnimationSource/1")
            return SelectByContainer(request, files);

        JsonElement evidence = audit.RootElement.GetProperty("clip_source");
        string evidencePath = evidence.GetProperty("path").GetString() ?? throw new InvalidDataException("Enemy clip evidence path is null.");
        byte[] sourceBytes = File.ReadAllBytes(evidencePath);
        string evidenceHash = Convert.ToHexString(SHA256.HashData(sourceBytes));
        if (!evidenceHash.Equals(evidence.GetProperty("sha256").GetString(), StringComparison.OrdinalIgnoreCase))
            throw new InvalidDataException("Enemy clip source JSON changed after source verification.");

        using var source = JsonDocument.Parse(sourceBytes);
        JsonElement metadata = source.RootElement.GetProperty("$animestudio");
        if (metadata.GetProperty("type").GetString() != "AnimationClip")
            throw new InvalidDataException("Enemy clip evidence does not describe an AnimationClip.");
        string cab = metadata.GetProperty("sourceFile").GetString() ?? throw new InvalidDataException("Enemy clip CAB is null.");
        long pathId = metadata.GetProperty("pathId").GetInt64();
        if (pathId == 0 || !request.ResourcePath.Equals(metadata.GetProperty("container").GetString(), StringComparison.OrdinalIgnoreCase))
            throw new InvalidDataException("Enemy clip identity or container differs from the animation request.");

        AnimationClip[] matches = files.SelectMany(file => file.Objects.OfType<AnimationClip>())
            .Where(clip => clip.m_PathID == pathId && clip.assetsFile.fileName.Equals(cab, StringComparison.OrdinalIgnoreCase))
            .ToArray();
        if (matches.Length != 1)
            throw new InvalidDataException($"Expected one exact enemy AnimationClip: cab={cab}, path_id={pathId}, matches={matches.Length}.");

        AnimationClip selected = matches[0];
        long byteSize = metadata.GetProperty("byteSize").GetInt64();
        long rawDataLength = metadata.GetProperty("rawDataLength").GetInt64();
        string rawDataHash = metadata.GetProperty("rawDataSha256").GetString() ?? throw new InvalidDataException("Enemy clip raw data hash is null.");
        if (byteSize != selected.byteSize || rawDataLength != selected.byteSize ||
            !Convert.ToHexString(SHA256.HashData(selected.GetRawData())).Equals(rawDataHash, StringComparison.OrdinalIgnoreCase))
            throw new InvalidDataException($"Enemy clip source bytes differ from the exact bundle object: cab={cab}, path_id={pathId}.");
        return selected;
    }

    private static AnimationClip SelectByContainer(AnimationRequest request, IEnumerable<SerializedFile> files)
    {
        var containers = files.SelectMany(file => file.Objects.OfType<AssetBundle>())
            .SelectMany(bundle => bundle.m_Container)
            .Where(entry => string.Equals(entry.Key, request.ResourcePath, StringComparison.OrdinalIgnoreCase))
            .ToArray();
        if (containers.Length != 1 || !containers[0].Value.asset.TryGet(out var asset) || asset is not AnimationClip clip)
            throw new InvalidDataException($"Expected one exact AnimationClip container: resource={request.ResourcePath}, matches={containers.Length}.");
        return clip;
    }
}
