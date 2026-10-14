using System;
using System.Collections.Generic;
using System.IO;
using System.Linq;
using System.Text.Json;

namespace AnimeStudio
{
    public static class UnityCNManager
    {
        private static List<List<string>> storedData;
        private static readonly string UnityCNKeysPath = "CNKeys.json";

        public static List<List<string>> GetKeys()
        {
            storedData ??= ReadJson();
            return storedData;
        }

        public static List<List<string>> GetDefaultKeys() => new();

        public static void SetKeys(List<List<string>> keys)
        {
            ArgumentNullException.ThrowIfNull(keys);
            ValidateKeys(keys);
            var options = new JsonSerializerOptions { WriteIndented = true };
            var path = Path.Combine(AppDomain.CurrentDomain.BaseDirectory, UnityCNKeysPath);
            File.WriteAllText(path, JsonSerializer.Serialize(keys, options));
            storedData = keys;
        }

        public static List<List<string>> ReadJson()
        {
            var path = Path.Combine(AppDomain.CurrentDomain.BaseDirectory, UnityCNKeysPath);
            if (!File.Exists(path))
            {
                return ResetJson();
            }

            List<List<string>> keys;
            try
            {
                keys = JsonSerializer.Deserialize<List<List<string>>>(File.ReadAllText(path));
            }
            catch (JsonException exception)
            {
                throw new InvalidDataException($"The local {UnityCNKeysPath} file is not valid JSON.", exception);
            }

            if (keys is null)
            {
                throw new InvalidDataException($"The local {UnityCNKeysPath} file must contain a JSON array.");
            }

            ValidateKeys(keys);
            storedData = keys;
            return keys;
        }

        public static List<List<string>> ResetJson()
        {
            storedData = new List<List<string>>();
            return storedData;
        }

        public static void SetKey(UnityCN.Entry entry)
        {
            ArgumentNullException.ThrowIfNull(entry);
            if (!UnityCN.SetKey(entry))
            {
                throw new InvalidDataException($"UnityCN key profile '{entry.Name}' could not be initialized.");
            }

            Logger.Info($"[UnityCN] Selected key profile '{entry.Name}'.");
        }

        private static void ValidateKeys(List<List<string>> keys)
        {
            for (var index = 0; index < keys.Count; index++)
            {
                var entry = keys[index];
                if (entry is null || entry.Count != 3 || entry.Any(value => value is null))
                {
                    throw new InvalidDataException($"The local {UnityCNKeysPath} entry at index {index} must contain three string values.");
                }
            }
        }
    }
}
