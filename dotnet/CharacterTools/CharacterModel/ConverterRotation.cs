using AnimeStudio;

namespace EndfieldCharacterModel;

/// <summary>Provides the converter's XYZ Euler composition without native SDK calls.</summary>
internal static class ConverterRotation
{
    public static Quaternion EulerToQuaternion(Vector3 degrees)
    {
        var radians = Math.PI / 360d;
        var sx = Math.Sin(degrees.X * radians);
        var cx = Math.Cos(degrees.X * radians);
        var sy = Math.Sin(degrees.Y * radians);
        var cy = Math.Cos(degrees.Y * radians);
        var sz = Math.Sin(degrees.Z * radians);
        var cz = Math.Cos(degrees.Z * radians);
        return new Quaternion((float)(sx * cy * cz - cx * sy * sz),
            (float)(cx * sy * cz + sx * cy * sz),
            (float)(cx * cy * sz - sx * sy * cz),
            (float)(cx * cy * cz + sx * sy * sz));
    }
}
