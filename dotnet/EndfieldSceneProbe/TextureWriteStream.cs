using System.Diagnostics;

/// <summary>Measure synchronous file ownership without buffering the encoded PNG in memory.</summary>
internal sealed class TextureWriteStream : Stream
{
    private readonly FileStream file;
    public TimeSpan OpenDuration { get; }
    public TimeSpan WriteDuration { get; private set; }
    public TimeSpan CloseDuration { get; private set; }
    public long WriteCalls { get; private set; }
    public long WrittenBytes { get; private set; }

    public TextureWriteStream(string path)
    {
        var started = Stopwatch.GetTimestamp();
        file = new FileStream(path, FileMode.CreateNew, FileAccess.Write, FileShare.None);
        OpenDuration = Stopwatch.GetElapsedTime(started);
    }

    public override bool CanRead => false;
    public override bool CanSeek => file.CanSeek;
    public override bool CanWrite => file.CanWrite;
    public override long Length => file.Length;
    public override long Position { get => file.Position; set => file.Position = value; }
    public override int Read(byte[] buffer, int offset, int count) => throw new NotSupportedException();
    public override long Seek(long offset, SeekOrigin origin) => file.Seek(offset, origin);
    public override void SetLength(long value) => file.SetLength(value);

    public override void Write(byte[] buffer, int offset, int count) => Write(buffer.AsSpan(offset, count));

    public override void Write(ReadOnlySpan<byte> buffer)
    {
        var started = Stopwatch.GetTimestamp();
        WriteCalls++;
        try
        {
            file.Write(buffer);
            WrittenBytes += buffer.Length;
        }
        finally
        {
            WriteDuration += Stopwatch.GetElapsedTime(started);
        }
    }

    public override void Flush()
    {
        var started = Stopwatch.GetTimestamp();
        try { file.Flush(); }
        finally { WriteDuration += Stopwatch.GetElapsedTime(started); }
    }

    protected override void Dispose(bool disposing)
    {
        if (disposing)
        {
            var started = Stopwatch.GetTimestamp();
            try { file.Dispose(); }
            finally { CloseDuration += Stopwatch.GetElapsedTime(started); }
        }
        base.Dispose(disposing);
    }
}
