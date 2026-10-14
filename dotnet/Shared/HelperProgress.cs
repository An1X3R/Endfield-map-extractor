using System.Diagnostics;
using System.Runtime.InteropServices;
using System.Text;
using System.Text.Json;
using System.Text.Json.Serialization;

internal sealed class HelperProgress
{
    private static readonly JsonSerializerOptions JsonOptions = new()
    {
        PropertyNamingPolicy = JsonNamingPolicy.CamelCase,
        DefaultIgnoreCondition = JsonIgnoreCondition.WhenWritingNull,
    };
    private readonly string helper;
    private readonly string? reportPath;
    private readonly string phaseSemantics;
    private readonly Stopwatch timer = Stopwatch.StartNew();
    private readonly List<ProgressSample> samples = [];
    private readonly Dictionary<string, double> phaseTimingsSeconds = new(StringComparer.Ordinal);
    private readonly Dictionary<string, long> phaseCounters = new(StringComparer.Ordinal);
    private readonly IoCounters? startingIo;
    private bool finished;

    public HelperProgress(string helper, string? reportPath, string phaseSemantics)
    {
        this.helper = helper;
        this.reportPath = reportPath;
        this.phaseSemantics = phaseSemantics;
        startingIo = ReadIo();
        if (reportPath is not null)
        {
            var target = Path.GetFullPath(reportPath);
            if (File.Exists(target) || Directory.Exists(target))
                throw new IOException($"Refusing to overwrite existing performance report: {target}");
        }
    }

    public void Publish(string phase, long current, long? total)
    {
        if (finished || current < 0 || total is < 0 || total is not null && current > total)
            throw new ArgumentOutOfRangeException(nameof(current), $"Invalid helper progress: {phase} {current}/{total}");
        using var process = Process.GetCurrentProcess();
        process.Refresh();
        var io = ReadIo();
        var sample = new ProgressSample(
            "EndfieldHelperProgress/1", helper, phase, current, total,
            timer.Elapsed.TotalSeconds, process.PeakWorkingSet64,
            io is null || startingIo is null ? null : io.Value.ReadTransferCount - startingIo.Value.ReadTransferCount,
            io is null || startingIo is null ? null : io.Value.WriteTransferCount - startingIo.Value.WriteTransferCount,
            "Win32 GetProcessIoCounters process transfer-count delta; logical I/O, not physical disk bytes");
        samples.Add(sample);
        Console.Out.WriteLine(JsonSerializer.Serialize(sample, JsonOptions));
        Console.Out.Flush();
    }

    public void Finish(string status)
    {
        if (finished)
            throw new InvalidOperationException("Helper progress report was already completed");
        finished = true;
        if (reportPath is null)
            return;
        var target = Path.GetFullPath(reportPath);
        Directory.CreateDirectory(Path.GetDirectoryName(target)!);
        using var stream = new FileStream(target, FileMode.CreateNew, FileAccess.Write, FileShare.None);
        using var writer = new StreamWriter(stream, new UTF8Encoding(false));
        writer.Write(JsonSerializer.Serialize(new
        {
            format = "EndfieldHelperPerformanceReport/1",
            helper,
            status,
            phaseSemantics,
            phaseTimingsSeconds,
            phaseCounters,
            events = samples,
        }, new JsonSerializerOptions { WriteIndented = true, PropertyNamingPolicy = JsonNamingPolicy.CamelCase,
                                      DefaultIgnoreCondition = JsonIgnoreCondition.WhenWritingNull }));
    }

    public void AddPhaseDuration(string phase, TimeSpan duration)
    {
        if (finished || string.IsNullOrWhiteSpace(phase) || duration < TimeSpan.Zero)
            throw new ArgumentException($"Invalid helper phase duration: {phase}, {duration}");
        phaseTimingsSeconds[phase] = phaseTimingsSeconds.GetValueOrDefault(phase) + duration.TotalSeconds;
    }

    public void SetPhaseCounter(string name, long value)
    {
        if (finished || string.IsNullOrWhiteSpace(name) || value < 0)
            throw new ArgumentException($"Invalid helper phase counter: {name}, {value}");
        phaseCounters[name] = value;
    }

    private static IoCounters? ReadIo()
    {
        if (!OperatingSystem.IsWindows())
            return null;
        using var process = Process.GetCurrentProcess();
        if (!GetProcessIoCounters(process.Handle, out var counters))
            throw new IOException($"GetProcessIoCounters failed: {Marshal.GetLastWin32Error()}");
        return counters;
    }

    [DllImport("kernel32.dll", SetLastError = true)]
    private static extern bool GetProcessIoCounters(IntPtr process, out IoCounters counters);

    [StructLayout(LayoutKind.Sequential)]
    private struct IoCounters
    {
        public ulong ReadOperationCount;
        public ulong WriteOperationCount;
        public ulong OtherOperationCount;
        public ulong ReadTransferCount;
        public ulong WriteTransferCount;
        public ulong OtherTransferCount;
    }

    private sealed record ProgressSample(
        string Format, string Helper, string Phase, long Current, long? Total,
        double ElapsedSeconds, long PeakWorkingSetBytes, ulong? ReadBytes,
        ulong? WriteBytes, string IoSemantics);
}
