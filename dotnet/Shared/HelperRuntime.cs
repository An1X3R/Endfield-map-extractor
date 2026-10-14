using AnimeStudio;

internal static class HelperRuntime
{
    internal static void Initialize()
    {
        // Validate an optional local profile without replacing it or shipping game keys.
        _ = UnityCNManager.GetKeys();
    }
}
