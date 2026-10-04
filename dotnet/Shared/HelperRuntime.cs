using AnimeStudio;

internal static class HelperRuntime
{
    internal static void Initialize()
    {
        // Materialize the pinned library defaults before bootstrap hashes runtime files.
        UnityCNManager.SetKeys(UnityCNManager.GetDefaultKeys());
    }
}
