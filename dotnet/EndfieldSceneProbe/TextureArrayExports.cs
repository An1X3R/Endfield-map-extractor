using System.Collections.Specialized;
using System.Security.Cryptography;
using AnimeStudio;
using SixLabors.ImageSharp;
using SixLabors.ImageSharp.PixelFormats;
using SixLabors.ImageSharp.Processing;
using Texture2DDecoder;
using Object = AnimeStudio.Object;

internal static class TextureArrayExports
{
    private const int RgbaBc7UnormGraphicsFormat = 109;
    private const int Bc7BlockWidth = 4;
    private const int Bc7BlockHeight = 4;
    private const int Bc7BlockBytes = 16;

    public static TextureArrayExportBatch Export(Object textureArray, string outputRoot, string textureDirectory)
    {
        ArgumentNullException.ThrowIfNull(textureArray);
        ArgumentException.ThrowIfNullOrWhiteSpace(outputRoot);
        ArgumentException.ThrowIfNullOrWhiteSpace(textureDirectory);
        if (textureArray.type != ClassIDType.Texture2DArray)
            throw new InvalidDataException(
                $"TEXTURE_ARRAY_TYPE_INVALID source={textureArray.assetsFile.fileName} " +
                $"pathId={textureArray.m_PathID} actual={textureArray.type}");

        var fields = textureArray.ToType() ?? throw new InvalidDataException(
            $"TEXTURE_ARRAY_TYPE_TREE_MISSING source={textureArray.assetsFile.fileName} " +
            $"pathId={textureArray.m_PathID}");
        var bytesRead = textureArray.reader.Position - textureArray.reader.byteStart;
        if (bytesRead != textureArray.byteSize)
            throw new InvalidDataException(
                $"TEXTURE_ARRAY_TYPE_TREE_SIZE_MISMATCH source={textureArray.assetsFile.fileName} " +
                $"pathId={textureArray.m_PathID} bytesRead={bytesRead} byteSize={textureArray.byteSize}");

        var name = ReadField<string>(fields, "m_Name", textureArray);
        var asset = new AssetRef(
            textureArray.assetsFile.fileName,
            textureArray.m_PathID,
            textureArray.type.ToString(),
            name);
        var width = ReadField<int>(fields, "m_Width", textureArray);
        var height = ReadField<int>(fields, "m_Height", textureArray);
        var depth = ReadField<int>(fields, "m_Depth", textureArray);
        var mipCount = ReadField<int>(fields, "m_MipCount", textureArray);
        var graphicsFormat = ReadField<int>(fields, "m_Format", textureArray);
        var dataSize = ReadField<uint>(fields, "m_DataSize", textureArray);
        if (width <= 0 || height <= 0 || depth <= 0 || mipCount <= 0)
            throw new InvalidDataException(
                $"TEXTURE_ARRAY_DIMENSIONS_INVALID source={asset.Source} pathId={asset.PathId} " +
                $"width={width} height={height} depth={depth} mipCount={mipCount}");

        var streamData = ReadField<OrderedDictionary>(fields, "m_StreamData", textureArray);
        var streamPath = ReadField<string>(streamData, "path", "m_StreamData.path", textureArray);
        var streamOffset = ReadField<ulong>(streamData, "offset", "m_StreamData.offset", textureArray);
        var streamSize = ReadField<uint>(streamData, "size", "m_StreamData.size", textureArray);
        var inlineData = ReadField<byte[]>(fields, "image data", textureArray);
        var payload = ReadPayload(
            textureArray,
            inlineData,
            streamPath,
            streamOffset,
            streamSize,
            dataSize);
        var payloadSha256 = Convert.ToHexString(SHA256.HashData(payload));
        var outputRootPath = Path.GetFullPath(outputRoot);
        var textureDirectoryPath = Path.GetFullPath(textureDirectory);
        AssertChildPath(outputRootPath, textureDirectoryPath, asset);

        if (graphicsFormat != RgbaBc7UnormGraphicsFormat)
        {
            var error =
                $"TEXTURE_ARRAY_FORMAT_UNSUPPORTED source={asset.Source} pathId={asset.PathId} " +
                $"format={graphicsFormat} supportedGraphicsFormat={RgbaBc7UnormGraphicsFormat}";
            var unsupportedSlices = Enumerable.Range(0, depth)
                .Select(sliceIndex => new TextureArraySliceExport(
                    asset,
                    sliceIndex,
                    null,
                    null,
                    [],
                    [],
                    null,
                    payloadSha256,
                    null,
                    error))
                .ToArray();
            return new TextureArrayExportBatch(
                asset,
                streamPath,
                streamOffset,
                streamSize,
                dataSize,
                width,
                height,
                depth,
                mipCount,
                graphicsFormat,
                [],
                payloadSha256,
                unsupportedSlices);
        }

        var mipByteLengths = ComputeBc7MipByteLengths(width, height, mipCount, asset);
        var slicePayloadLength = mipByteLengths.Sum(value => (long)value);
        var expectedPayloadLength = checked(slicePayloadLength * depth);
        if (expectedPayloadLength != payload.LongLength)
            throw new InvalidDataException(
                $"TEXTURE_ARRAY_MIPCHAIN_SIZE_MISMATCH source={asset.Source} pathId={asset.PathId} " +
                $"format={graphicsFormat} depth={depth} mipCount={mipCount} " +
                $"m_DataSize={dataSize} expectedBytes={expectedPayloadLength} actualBytes={payload.LongLength}");

        Directory.CreateDirectory(textureDirectoryPath);
        var mipOffsets = new long[mipByteLengths.Length];
        for (var mipIndex = 1; mipIndex < mipByteLengths.Length; mipIndex++)
            mipOffsets[mipIndex] = checked(mipOffsets[mipIndex - 1] + mipByteLengths[mipIndex - 1]);

        var slices = new TextureArraySliceExport[depth];
        for (var sliceIndex = 0; sliceIndex < depth; sliceIndex++)
        {
            var sliceOffset = checked(slicePayloadLength * sliceIndex);
            var sliceOffsetInt = checked((int)sliceOffset);
            var sliceLengthInt = checked((int)slicePayloadLength);
            var slicePayload = payload.AsSpan(sliceOffsetInt, sliceLengthInt);
            var slicePayloadSha256 = Convert.ToHexString(SHA256.HashData(slicePayload));
            var absoluteMipOffsets = mipOffsets.Select(offset => checked(sliceOffset + offset)).ToArray();
            var path = Path.Combine(textureDirectoryPath, SliceFileName(textureArray, name, sliceIndex));

            var baseMipLength = mipByteLengths[0];
            var decodedPixels = new byte[checked(width * height * 4)];
            var compressedBaseMip = payload.AsSpan(sliceOffsetInt, baseMipLength);
            if (!TextureDecoder.DecodeBC7(compressedBaseMip, width, height, decodedPixels))
            {
                var error =
                    $"TEXTURE_ARRAY_BC7_DECODE_FAILED source={asset.Source} pathId={asset.PathId} " +
                    $"sliceIndex={sliceIndex} width={width} height={height} " +
                    $"payloadOffset={sliceOffset} payloadLength={baseMipLength}";
                slices[sliceIndex] = new TextureArraySliceExport(
                    asset,
                    sliceIndex,
                    sliceOffset,
                    slicePayloadLength,
                    absoluteMipOffsets,
                    mipByteLengths,
                    slicePayloadSha256,
                    payloadSha256,
                    null,
                    error);
                continue;
            }

            using var image = Image.LoadPixelData<Bgra32>(decodedPixels, width, height);
            image.Mutate(processor => processor.Flip(FlipMode.Vertical));
            using var file = new FileStream(path, FileMode.CreateNew, FileAccess.Write, FileShare.None);
            image.SaveAsPng(file);
            slices[sliceIndex] = new TextureArraySliceExport(
                asset,
                sliceIndex,
                sliceOffset,
                slicePayloadLength,
                absoluteMipOffsets,
                mipByteLengths,
                slicePayloadSha256,
                payloadSha256,
                Path.GetRelativePath(outputRootPath, path),
                null);
        }

        return new TextureArrayExportBatch(
            asset,
            streamPath,
            streamOffset,
            streamSize,
            dataSize,
            width,
            height,
            depth,
            mipCount,
            graphicsFormat,
            mipByteLengths,
            payloadSha256,
            slices);
    }

    private static byte[] ReadPayload(
        Object owner,
        byte[] inlineData,
        string streamPath,
        ulong streamOffset,
        uint streamSize,
        uint dataSize)
    {
        if (streamSize > 0)
        {
            if (inlineData.Length > 0)
                throw new InvalidDataException(
                    $"TEXTURE_ARRAY_INLINE_AND_STREAM_DATA_PRESENT source={owner.assetsFile.fileName} " +
                    $"pathId={owner.m_PathID} inlineBytes={inlineData.Length} streamBytes={streamSize}");
            if (string.IsNullOrWhiteSpace(streamPath))
                throw new InvalidDataException(
                    $"TEXTURE_ARRAY_STREAM_PATH_MISSING source={owner.assetsFile.fileName} " +
                    $"pathId={owner.m_PathID} streamOffset={streamOffset} streamSize={streamSize}");
            if (streamOffset > long.MaxValue)
                throw new InvalidDataException(
                    $"TEXTURE_ARRAY_STREAM_OFFSET_OUT_OF_RANGE source={owner.assetsFile.fileName} " +
                    $"pathId={owner.m_PathID} streamOffset={streamOffset}");

            var payload = new ResourceReader(
                    streamPath,
                    owner.assetsFile,
                    checked((long)streamOffset),
                    streamSize)
                .GetData();
            if (payload.LongLength != streamSize)
                throw new InvalidDataException(
                    $"TEXTURE_ARRAY_STREAM_LENGTH_MISMATCH source={owner.assetsFile.fileName} " +
                    $"pathId={owner.m_PathID} streamPath={streamPath} streamOffset={streamOffset} " +
                    $"expected={streamSize} actual={payload.LongLength}");
            if (payload.LongLength != dataSize)
                throw new InvalidDataException(
                    $"TEXTURE_ARRAY_DATA_SIZE_MISMATCH source={owner.assetsFile.fileName} " +
                    $"pathId={owner.m_PathID} m_DataSize={dataSize} actual={payload.LongLength}");
            return payload;
        }

        if (inlineData.Length == 0)
            throw new InvalidDataException(
                $"TEXTURE_ARRAY_PAYLOAD_MISSING source={owner.assetsFile.fileName} pathId={owner.m_PathID} " +
                $"m_DataSize={dataSize} streamSize={streamSize}");
        if (inlineData.LongLength != dataSize)
            throw new InvalidDataException(
                $"TEXTURE_ARRAY_DATA_SIZE_MISMATCH source={owner.assetsFile.fileName} " +
                $"pathId={owner.m_PathID} m_DataSize={dataSize} actual={inlineData.LongLength}");
        return inlineData;
    }

    private static int[] ComputeBc7MipByteLengths(int width, int height, int mipCount, AssetRef asset)
    {
        var maximumMipCount = 1;
        var maximumMipWidth = width;
        var maximumMipHeight = height;
        while (maximumMipWidth > 1 || maximumMipHeight > 1)
        {
            maximumMipCount++;
            maximumMipWidth = Math.Max(1, maximumMipWidth / 2);
            maximumMipHeight = Math.Max(1, maximumMipHeight / 2);
        }
        if (mipCount > maximumMipCount)
            throw new InvalidDataException(
                $"TEXTURE_ARRAY_MIP_COUNT_INVALID source={asset.Source} pathId={asset.PathId} " +
                $"width={width} height={height} mipCount={mipCount} maximum={maximumMipCount}");

        var sizes = new int[mipCount];
        var mipWidthAtLevel = width;
        var mipHeightAtLevel = height;
        for (var mipIndex = 0; mipIndex < mipCount; mipIndex++)
        {
            var blocksWide = Math.Max(1, checked((mipWidthAtLevel + Bc7BlockWidth - 1) / Bc7BlockWidth));
            var blocksHigh = Math.Max(1, checked((mipHeightAtLevel + Bc7BlockHeight - 1) / Bc7BlockHeight));
            sizes[mipIndex] = checked(blocksWide * blocksHigh * Bc7BlockBytes);
            mipWidthAtLevel = Math.Max(1, mipWidthAtLevel / 2);
            mipHeightAtLevel = Math.Max(1, mipHeightAtLevel / 2);
        }
        return sizes;
    }

    private static string SliceFileName(Object textureArray, string name, int sliceIndex)
    {
        var source = Path.GetFileNameWithoutExtension(
            textureArray.assetsFile.originalPath ?? textureArray.assetsFile.fileName);
        return $"{SanitizeFilePart(source)}_{textureArray.m_PathID}_{SanitizeFilePart(name)}_slice_{sliceIndex}.png";
    }

    private static string SanitizeFilePart(string value)
    {
        var invalidCharacters = Path.GetInvalidFileNameChars().ToHashSet();
        var sanitized = new string(value
            .Select(character => invalidCharacters.Contains(character) || char.IsControl(character) ? '_' : character)
            .ToArray())
            .Trim()
            .TrimEnd('.');
        if (sanitized.Length == 0)
            return "unnamed";
        return sanitized.Length > 64 ? sanitized[..64] : sanitized;
    }

    private static void AssertChildPath(string rootPath, string childPath, AssetRef asset)
    {
        var relativePath = Path.GetRelativePath(rootPath, childPath);
        if (Path.IsPathRooted(relativePath) || relativePath == ".." ||
            relativePath.StartsWith($"..{Path.DirectorySeparatorChar}", StringComparison.Ordinal))
            throw new InvalidDataException(
                $"TEXTURE_ARRAY_OUTPUT_PATH_OUTSIDE_ROOT source={asset.Source} pathId={asset.PathId} " +
                $"root={rootPath} output={childPath}");
    }

    private static T ReadField<T>(OrderedDictionary fields, string fieldName, Object owner)
    {
        return ReadField<T>(fields, fieldName, fieldName, owner);
    }

    private static T ReadField<T>(OrderedDictionary fields, string fieldName, string context, Object owner)
    {
        if (fields[fieldName] is T value)
            return value;
        throw new InvalidDataException(
            $"TEXTURE_ARRAY_FIELD_TYPE_INVALID source={owner.assetsFile.fileName} pathId={owner.m_PathID} " +
            $"field={context} expected={typeof(T).Name} actual={fields[fieldName]?.GetType().Name ?? "missing"}");
    }
}

internal sealed record TextureArrayExportBatch(
    AssetRef Asset,
    string ResourcePath,
    ulong ResourceOffset,
    uint ResourceSize,
    uint DataSize,
    int Width,
    int Height,
    int Depth,
    int MipCount,
    int GraphicsFormat,
    int[] MipByteLengths,
    string PayloadSha256,
    TextureArraySliceExport[] Slices);

internal sealed record TextureArraySliceExport(
    AssetRef Asset,
    int SliceIndex,
    long? PayloadOffset,
    long? PayloadLength,
    long[] MipOffsets,
    int[] MipByteLengths,
    string? SlicePayloadSha256,
    string PayloadSha256,
    string? File,
    string? Error)
{
}
