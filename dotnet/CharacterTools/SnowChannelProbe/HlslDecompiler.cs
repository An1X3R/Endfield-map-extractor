using AnimeStudio.PInvoke;
using System.Runtime.InteropServices;

/// <summary>Retains the existing upstream native DXBC decompiler interface independently of model exporters.</summary>
internal static class HLSLDecompiler
{
    private const string Library = "HLSLDecompiler";

    static HLSLDecompiler() => DllLoader.PreloadDll(Library);

    internal static void DecompileShader(byte[] bytecode, int byteCount, out string text)
    {
        var code = Decompile(bytecode, byteCount, out var nativeText, out var textLength);
        if (code != 0) throw new InvalidDataException($"Native DXBC decompiler failed: status={code}, input_bytes={byteCount}.");
        if (nativeText == IntPtr.Zero || textLength <= 0) throw new InvalidDataException($"Native DXBC decompiler returned an empty result: input_bytes={byteCount}, output_bytes={textLength}.");
        try { text = Marshal.PtrToStringAnsi(nativeText, textLength) ?? throw new InvalidDataException("Native DXBC decompiler returned null text."); }
        finally { Marshal.FreeHGlobal(nativeText); }
    }

    [DllImport(Library, CallingConvention = CallingConvention.Cdecl)]
    private static extern int Decompile(byte[] bytecode, int byteCount, out IntPtr nativeText, out int textLength);
}
