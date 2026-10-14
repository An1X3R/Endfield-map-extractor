namespace AnimeStudio;

/// <summary>Retains the upstream CLI API without linking the Autodesk SDK.</summary>
public static class ModelExporter
{
    public static void ExportFbx(string path, IImported imported, Fbx.ExportOptions exportOptions)
    {
        throw new NotSupportedException("CHARACTER_FBX_EXPORT_UNAVAILABLE: use the character portable geometry pipeline; this reader contains no Autodesk SDK.");
    }
}

/// <summary>Managed option compatibility for the unused upstream FBX export entry point.</summary>
public static class Fbx
{
    public struct ExportOptions
    {
        public bool eulerFilter;
        public float filterPrecision;
        public bool exportAllNodes;
        public bool exportSkins;
        public bool exportAnimations;
        public bool exportBlendShape;
        public bool castToBone;
        public int boneSize;
        public float scaleFactor;
        public int fbxVersion;
        public int fbxFormat;
    }
}
