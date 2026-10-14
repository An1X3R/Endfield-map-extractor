using SixLabors.ImageSharp;
using SixLabors.ImageSharp.Formats.Png;
using SixLabors.ImageSharp.PixelFormats;

internal static partial class Program
{
    internal static void VerifyTextureWorkerOptions()
    {
        var defaultOptions = Options.Parse(["input", "output"]);
        var expectedWorkers = Math.Min(MaximumTextureWorkers, Environment.ProcessorCount);
        if (defaultOptions.TextureWorkers != expectedWorkers || defaultOptions.TextureMemoryMiB != MaximumTextureMemoryMiB)
            throw new InvalidOperationException("Texture worker defaults exceeded the processor or memory bounds");
        for (var workers = 1; workers <= MaximumTextureWorkers; workers++)
        {
            var options = Options.Parse(["input", "output", "--texture-workers",
                workers.ToString(System.Globalization.CultureInfo.InvariantCulture)]);
            if (options.TextureWorkers != workers || options.TextureMemoryMiB != MaximumTextureMemoryMiB)
                throw new InvalidOperationException($"Texture worker option was not preserved: workers={workers}");
        }
        foreach (var workers in new[] { 0, MaximumTextureWorkers + 1 })
        {
            try
            {
                Options.Parse(["input", "output", "--texture-workers",
                    workers.ToString(System.Globalization.CultureInfo.InvariantCulture)]);
            }
            catch (ArgumentException)
            {
                continue;
            }
            throw new InvalidOperationException($"Out-of-range texture worker count was accepted: workers={workers}");
        }
        foreach (var memoryMiB in new[] { 0, MaximumTextureMemoryMiB + 1 })
        {
            try
            {
                Options.Parse(["input", "output", "--texture-memory-mib",
                    memoryMiB.ToString(System.Globalization.CultureInfo.InvariantCulture)]);
            }
            catch (ArgumentException)
            {
                continue;
            }
            throw new InvalidOperationException($"Out-of-range texture memory budget was accepted: MiB={memoryMiB}");
        }
    }

    internal static void VerifyTexturePipeline(string output)
    {
        Directory.CreateDirectory(output);
        var pixels = Enumerable.Range(0, 1024).Select(index => new Bgra32(
            (byte)(index % 251), (byte)(index % 127), (byte)(index % 61), 255)).ToArray();
        var reference = new AssetRef("RegressionSource", 1, "Texture2D", "RegressionPixels");
        using var blocked = new ManualResetEventSlim(false);
        var first = Task.Run(() =>
        {
            blocked.Wait();
            return WriteTextureImage(Image.LoadPixelData<Bgra32>(pixels, 32, 32), reference,
                output, Path.Combine(output, "first.png"), CancellationToken.None);
        });
        var second = Task.Run(() => WriteTextureImage(Image.LoadPixelData<Bgra32>(pixels, 32, 32),
            reference, output, Path.Combine(output, "second.png"), CancellationToken.None));
        try
        {
            second.GetAwaiter().GetResult();
            var owners = new[] { new PendingTexture(0, first, 4096), new PendingTexture(1, second, 4096) };
            var completed = FirstCompletedTexture(owners);
            if (completed.Index != 1 || first.IsCompleted || owners[0].Index != 0)
                throw new InvalidOperationException("Completion reclamation changed input order or waited for the blocked owner");
        }
        finally
        {
            blocked.Set();
            first.GetAwaiter().GetResult();
            second.GetAwaiter().GetResult();
        }
        var measured = second.GetAwaiter().GetResult();
        var originalPath = Path.Combine(output, "original.png");
        using (var image = Image.LoadPixelData<Bgra32>(pixels, 32, 32))
        using (var file = new FileStream(originalPath, FileMode.CreateNew, FileAccess.Write, FileShare.None))
            image.SaveAsPng(file, new PngEncoder { CompressionLevel = TexturePngCompressionLevel });
        if (!File.ReadAllBytes(originalPath).SequenceEqual(File.ReadAllBytes(Path.Combine(output, "second.png"))) ||
            measured.WrittenBytes != new FileInfo(originalPath).Length || measured.WriteCalls <= 0)
            throw new InvalidOperationException("Measured stream changed encoded PNG bytes or reported invalid write counts");
        using var cancelled = new CancellationTokenSource();
        cancelled.Cancel();
        var cancelledImage = Image.LoadPixelData<Bgra32>(pixels, 32, 32);
        try
        {
            WriteTextureImage(cancelledImage, reference, output, Path.Combine(output, "cancelled.png"), cancelled.Token);
            throw new InvalidOperationException("Cancelled encoding did not propagate cancellation");
        }
        catch (OperationCanceledException) { }
        VerifyDisposedTexture(cancelledImage);
        var failedImage = Image.LoadPixelData<Bgra32>(pixels, 32, 32);
        var failedPath = Path.Combine(output, "failed.png");
        File.WriteAllText(failedPath + ".partial", "Existing output must remain unchanged");
        var failed = WriteTextureImage(failedImage, reference, output, failedPath, CancellationToken.None);
        if (failed.Record.Error is null || failed.Record.File is not null || File.Exists(failedPath))
            throw new InvalidOperationException("Failed output ownership was reported as a successful PNG");
        VerifyDisposedTexture(failedImage);
        Console.WriteLine("TEXTURE_PIPELINE_REGRESSION_PASSED");
    }

    private static void VerifyDisposedTexture(Image<Bgra32> image)
    {
        try
        {
            _ = image[0, 0];
            throw new InvalidOperationException("The encoder did not dispose its owned image");
        }
        catch (ObjectDisposedException) { }
    }
}

internal static class TexturePipelineTestEntry
{
    private static int Main(string[] args)
    {
        if (args.Length != 1)
            throw new ArgumentException("Texture pipeline regression requires a fresh output directory");
        Program.VerifyTextureWorkerOptions();
        Program.VerifyTexturePipeline(args[0]);
        return 0;
    }
}
