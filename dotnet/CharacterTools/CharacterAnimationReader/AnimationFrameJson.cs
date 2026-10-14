using System.Text.Json;

internal static class AnimationFrameJson
{
    private static readonly JsonSerializerOptions Options = new()
    {
        PropertyNamingPolicy = JsonNamingPolicy.SnakeCaseLower,
        WriteIndented = false,
    };

    internal static void Write<T>(string output, string filename, T value)
    {
        // Large frame arrays keep their schema and numeric precision without whitespace copies.
        using var stream = File.Create(Path.Combine(output, filename));
        JsonSerializer.Serialize(stream, value, Options);
    }

    internal static T Read<T>(string path, JsonSerializerOptions options)
    {
        using var stream = File.OpenRead(path);
        return JsonSerializer.Deserialize<T>(stream, options)
            ?? throw new InvalidDataException($"Animation intermediate JSON is null: path={path}");
    }
}
