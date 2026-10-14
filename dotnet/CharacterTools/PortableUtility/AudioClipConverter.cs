namespace AnimeStudio;

/// <summary>Retains the CLI type without including proprietary audio SDK bindings.</summary>
public sealed class AudioClipConverter
{
    private const string UnsupportedMessage = "CHARACTER_AUDIO_CONVERSION_UNAVAILABLE: this JSON reader contains no FMOD SDK.";

    public AudioClipConverter(AudioClip audioClip)
    {
        ArgumentNullException.ThrowIfNull(audioClip);
    }

    public byte[] ConvertToWav()
    {
        throw new NotSupportedException(UnsupportedMessage);
    }

    public string GetExtensionName() => throw new NotSupportedException(UnsupportedMessage);

    public bool IsSupport => throw new NotSupportedException(UnsupportedMessage);
}
