using System.Runtime.ExceptionServices;
using System.Diagnostics;
using AnimeStudio;
using SixLabors.ImageSharp;
using SixLabors.ImageSharp.Formats.Png;
using SixLabors.ImageSharp.PixelFormats;

internal static partial class Program
{
    private const PngCompressionLevel TexturePngCompressionLevel = PngCompressionLevel.Level1;
    private sealed record TextureEncodingResult(ExportRecord Record, TimeSpan SaveDuration,
        TimeSpan OpenDuration, TimeSpan WriteDuration, TimeSpan CloseDuration,
        TimeSpan RenameDuration, long WriteCalls, long WrittenBytes, TimeSpan OwnerDuration);
    private sealed record PendingTexture(int Index, Task<TextureEncodingResult> Result, long PixelBytes);

    private static List<ExportRecord> ExportTextures(Texture2D[] textures, string output, string textureDir,
        int workers, long pixelBudget, HelperProgress telemetry, CancellationToken cancellation)
    {
        if (workers is < 1 or > MaximumTextureWorkers)
            throw new ArgumentOutOfRangeException(nameof(workers), workers,
                $"Texture encoding requires 1..{MaximumTextureWorkers} workers");
        if (pixelBudget is < 1048576 or > MaximumTextureMemoryMiB * 1024L * 1024L)
            throw new ArgumentOutOfRangeException(nameof(pixelBudget), pixelBudget,
                $"Texture pixel budget must be between 1 and {MaximumTextureMemoryMiB} MiB");
        cancellation.ThrowIfCancellationRequested();
        var records = new ExportRecord?[textures.Length];
        var completedCount = 0;
        var pending = new List<PendingTexture>(workers);
        var paths = new HashSet<string>(StringComparer.OrdinalIgnoreCase);
        long pendingBytes = 0;
        long peakPendingBytes = 0;
        var peakPendingCount = 0;
        var peakPendingWithOversizedImage = 0;
        long oversizedImages = 0;
        long writeCalls = 0;
        long writtenBytes = 0;
        long outOfOrderCompletions = 0;
        long workerSlotWaits = 0;
        long pixelBudgetWaits = 0;
        var sourceFormats = new Dictionary<TextureFormat, long>();
        var initialAllocated = GC.GetTotalAllocatedBytes(false);
        var initialGcPause = GC.GetTotalPauseDuration();
        var initialCollections = Enumerable.Range(0, 3).Select(GC.CollectionCount).ToArray();
        var started = Stopwatch.GetTimestamp();
        ExceptionDispatchInfo? failure = null;
        telemetry.Publish("texture_export", 0, textures.Length);
        PublishTextureMemory(telemetry, "before");

        void RecordCompletion(int index, TextureEncodingResult result)
        {
            if (records[index] is not null)
                throw new InvalidOperationException($"Texture result was completed twice: index={index}");
            records[index] = result.Record;
            completedCount++;
            telemetry.AddPhaseDuration("texture_png_save_sum", result.SaveDuration);
            telemetry.AddPhaseDuration("texture_encoder_owner_sum", result.OwnerDuration);
            telemetry.AddPhaseDuration("texture_file_open_sum", result.OpenDuration);
            telemetry.AddPhaseDuration("texture_stream_write_sum", result.WriteDuration);
            telemetry.AddPhaseDuration("texture_file_close_sum", result.CloseDuration);
            telemetry.AddPhaseDuration("texture_rename_sum", result.RenameDuration);
            writeCalls += result.WriteCalls;
            writtenBytes += result.WrittenBytes;
            if (failure is null && completedCount % 100 == 0)
                telemetry.Publish("texture_export", completedCount, textures.Length);
        }

        try
        {
            for (var index = 0; index < textures.Length; index++)
            {
                var texture = textures[index];
                sourceFormats[texture.m_TextureFormat] = sourceFormats.GetValueOrDefault(texture.m_TextureFormat) + 1;
                cancellation.ThrowIfCancellationRequested();
                var bytes = texture.m_Width > 0 && texture.m_Height > 0
                    ? checked((long)texture.m_Width * texture.m_Height * 4) : 0;
                oversizedImages += bytes > pixelBudget ? 1 : 0;
                // Decode reads shared ResourceReader positions; only independent images enter workers.
                // An image larger than the budget drains the queue and runs alone.
                while (pending.Count > 0 && (pending.Count >= workers || pendingBytes + bytes > pixelBudget))
                {
                    var waitPhase = pending.Count >= workers ? "texture_worker_slot_wait" : "texture_pixel_budget_wait";
                    if (pending.Count >= workers) workerSlotWaits++;
                    else pixelBudgetWaits++;
                    var waitStarted = Stopwatch.GetTimestamp();
                    var next = FirstCompletedTexture(pending);
                    if (pending.IndexOf(next) > 0) outOfOrderCompletions++;
                    pending.Remove(next);
                    pendingBytes -= next.PixelBytes;
                    var result = next.Result.GetAwaiter().GetResult();
                    telemetry.AddPhaseDuration(waitPhase, Stopwatch.GetElapsedTime(waitStarted));
                    RecordCompletion(next.Index, result);
                }
                cancellation.ThrowIfCancellationRequested();
                var path = Path.Combine(textureDir, UniqueFileName(texture, ".png"));
                if (!paths.Add(path))
                    throw new IOException($"Texture output identity collides: source={texture.assetsFile.fileName}, pathId={texture.m_PathID}, path={path}");
                var task = PrepareTextureEncoding(texture, output, path, telemetry, cancellation);
                pending.Add(new PendingTexture(index, task, bytes));
                pendingBytes += bytes;
                peakPendingBytes = Math.Max(peakPendingBytes, pendingBytes);
                peakPendingCount = Math.Max(peakPendingCount, pending.Count);
                if (pending.Any(owner => owner.PixelBytes > pixelBudget))
                    peakPendingWithOversizedImage = Math.Max(peakPendingWithOversizedImage, pending.Count);
            }
        }
        catch (Exception error)
        {
            failure = ExceptionDispatchInfo.Capture(error);
        }
        finally
        {
            // Join every owner before propagating cancellation/errors; each worker disposes its image.
            while (pending.Count > 0)
            {
                var next = pending[0];
                pending.RemoveAt(0);
                try
                {
                    var waitStarted = Stopwatch.GetTimestamp();
                    var result = next.Result.GetAwaiter().GetResult();
                    telemetry.AddPhaseDuration("texture_final_join_wait", Stopwatch.GetElapsedTime(waitStarted));
                    RecordCompletion(next.Index, result);
                }
                catch (Exception error)
                {
                    failure ??= ExceptionDispatchInfo.Capture(error);
                }
            }
            telemetry.AddPhaseDuration("texture_export_wall", Stopwatch.GetElapsedTime(started));
            telemetry.SetPhaseCounter("texture_peak_pending_images", peakPendingCount);
            telemetry.SetPhaseCounter("texture_peak_pending_images_with_oversized_image", peakPendingWithOversizedImage);
            telemetry.SetPhaseCounter("texture_peak_pending_pixel_bytes", peakPendingBytes);
            telemetry.SetPhaseCounter("texture_oversized_images_admitted_alone", oversizedImages);
            telemetry.SetPhaseCounter("texture_stream_write_calls", writeCalls);
            telemetry.SetPhaseCounter("texture_stream_written_bytes", writtenBytes);
            telemetry.SetPhaseCounter("texture_png_compression_level", (int)TexturePngCompressionLevel);
            telemetry.SetPhaseCounter("texture_out_of_order_completions", outOfOrderCompletions);
            telemetry.SetPhaseCounter("texture_worker_slot_waits", workerSlotWaits);
            telemetry.SetPhaseCounter("texture_pixel_budget_waits", pixelBudgetWaits);
            foreach (var format in sourceFormats.OrderBy(row => row.Key))
                telemetry.SetPhaseCounter($"texture_source_format_{format.Key}_count", format.Value);
            telemetry.AddPhaseDuration("texture_gc_pause", GC.GetTotalPauseDuration() - initialGcPause);
            telemetry.SetPhaseCounter("texture_managed_allocated_bytes_approximate", GC.GetTotalAllocatedBytes(false) - initialAllocated);
            for (var generation = 0; generation < 3; generation++)
                telemetry.SetPhaseCounter($"texture_gc_generation_{generation}_collections", GC.CollectionCount(generation) - initialCollections[generation]);
            PublishTextureMemory(telemetry, "after");
        }
        failure?.Throw();
        cancellation.ThrowIfCancellationRequested();
        telemetry.Publish("texture_export", completedCount, textures.Length);
        return records.Select((record, index) => record ?? throw new InvalidOperationException(
            $"Texture export result is missing: index={index}")).ToList();
    }

    private static PendingTexture FirstCompletedTexture(IReadOnlyList<PendingTexture> pending)
    {
        if (pending.Count == 0)
            throw new ArgumentException("Texture completion requires at least one owner", nameof(pending));
        // Already finished owners free capacity immediately, irrespective of input order.
        var completed = pending.FirstOrDefault(owner => owner.Result.IsCompleted);
        if (completed is not null)
            return completed;
        var result = Task.WhenAny(pending.Select(owner => owner.Result)).GetAwaiter().GetResult();
        return pending.First(owner => owner.Result == result);
    }

    private static void PublishTextureMemory(HelperProgress telemetry, string phase)
    {
        var memory = GC.GetGCMemoryInfo();
        telemetry.SetPhaseCounter($"texture_managed_heap_estimate_{phase}_bytes", GC.GetTotalMemory(false));
        telemetry.SetPhaseCounter($"texture_last_gc_heap_{phase}_bytes", memory.HeapSizeBytes);
        telemetry.SetPhaseCounter($"texture_last_gc_committed_{phase}_bytes", memory.TotalCommittedBytes);
    }

    private static Task<TextureEncodingResult> PrepareTextureEncoding(Texture2D texture, string output, string path,
        HelperProgress telemetry, CancellationToken cancellation)
    {
        var started = Stopwatch.GetTimestamp();
        var reference = TextureReference(texture);
        Image<Bgra32>? image;
        try
        {
            image = texture.ConvertToImage(true);
        }
        catch (Exception error) when (error is not OperationCanceledException)
        {
            return Task.FromResult(new TextureEncodingResult(new ExportRecord(reference, null, error.Message),
                TimeSpan.Zero, TimeSpan.Zero, TimeSpan.Zero, TimeSpan.Zero, TimeSpan.Zero, 0, 0, TimeSpan.Zero));
        }
        finally
        {
            // Includes shared-resource reading, decoding, image copying and the vertical flip.
            telemetry.AddPhaseDuration("texture_prepare", Stopwatch.GetElapsedTime(started));
        }
        if (image is null)
            return Task.FromResult(new TextureEncodingResult(new ExportRecord(reference, null, "Texture decoder returned no image"),
                TimeSpan.Zero, TimeSpan.Zero, TimeSpan.Zero, TimeSpan.Zero, TimeSpan.Zero, 0, 0, TimeSpan.Zero));
        // Do not pass the token to Task.Run: even a cancelled queued task must enter and dispose its image.
        return Task.Run(() => WriteTextureImage(image, reference, output, path, cancellation));
    }

    private static TextureEncodingResult WriteTextureImage(Image<Bgra32> image, AssetRef reference, string output,
        string path, CancellationToken cancellation)
    {
        var ownerStarted = Stopwatch.GetTimestamp();
        TextureWriteStream? stream = null;
        var saveDuration = TimeSpan.Zero;
        var renameDuration = TimeSpan.Zero;
        ExportRecord record;
        try
        {
            using (image)
            {
                cancellation.ThrowIfCancellationRequested();
                var partial = path + ".partial";
                stream = new TextureWriteStream(partial);
                var saveStarted = Stopwatch.GetTimestamp();
                using (stream)
                    image.SaveAsPng(stream, new PngEncoder { CompressionLevel = TexturePngCompressionLevel });
                saveDuration = Stopwatch.GetElapsedTime(saveStarted);
                cancellation.ThrowIfCancellationRequested();
                // The manifest can only reference complete PNG files, never interrupted encoder output.
                var renameStarted = Stopwatch.GetTimestamp();
                File.Move(partial, path);
                renameDuration = Stopwatch.GetElapsedTime(renameStarted);
            }
            record = new ExportRecord(reference, Path.GetRelativePath(output, path), null);
        }
        catch (Exception error) when (error is not OperationCanceledException)
        {
            record = new ExportRecord(reference, null, error.Message);
        }
        return new TextureEncodingResult(record, saveDuration, stream?.OpenDuration ?? TimeSpan.Zero,
            stream?.WriteDuration ?? TimeSpan.Zero, stream?.CloseDuration ?? TimeSpan.Zero,
            renameDuration, stream?.WriteCalls ?? 0, stream?.WrittenBytes ?? 0, Stopwatch.GetElapsedTime(ownerStarted));
    }
}
