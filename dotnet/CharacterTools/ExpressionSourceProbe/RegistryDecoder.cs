using System.Text.Json;
using AnimeStudio;

internal sealed record ManagedReference(long Rid, string Class, string Namespace, string Assembly, int RecordOffset, int PayloadOffset, int PayloadBytes, JsonElement Data);

/// <summary>Reads a v2 registry envelope and delegates every payload to existing serialized reference TypeTrees.</summary>
internal static class RegistryDecoder
{
    private static TypeTree PayloadTree(TypeTree source)
    {
        var registries = source.m_Nodes.Select((node, index) => new { Node = node, Index = index }).Where(row => row.Node.m_Level == 1 && row.Node.m_Name == "references" && row.Node.m_Type == "ManagedReferencesRegistry").ToArray();
        if (registries.Length == 0) return source;
        if (registries.Length != 1 || source.m_Nodes.Skip(registries[0].Index + 1).Any(node => node.m_Level <= 1)) throw new InvalidDataException("Managed-reference schema has unexpected owner registry layout.");
        // The referenced class schema includes the owner's registry; its inline payload excludes that envelope.
        return new TypeTree { m_Nodes = source.m_Nodes.Take(registries[0].Index).ToList(), m_StringBuffer = source.m_StringBuffer };
    }
    internal static void Decode(byte[] raw, int registryOffset, List<SerializedType> referenceTypes, string output, JsonSerializerOptions options)
    {
        using var stream = new MemoryStream(raw, false);
        using var reader = new EndianBinaryReader(stream, EndianType.LittleEndian, false);
        reader.Position = registryOffset;
        int version = reader.ReadInt32();
        int count = reader.ReadInt32Count(20, "ManagedReferencesRegistry.RefIds");
        if (version != 2 || count > 1000) throw new InvalidDataException($"Unsupported bounded registry: version={version}, count={count}.");
        var records = new List<ManagedReference>();
        for (int index = 0; index < count; index++)
        {
            int recordOffset = checked((int)reader.Position);
            long rid = reader.ReadInt64();
            string className = reader.ReadAlignedString();
            string nameSpace = reader.ReadAlignedString();
            string assembly = reader.ReadAlignedString();
            int payloadOffset = checked((int)reader.Position);
            var matchingTypes = referenceTypes.Where(type => type.m_KlassName == className && type.m_NameSpace == nameSpace && type.m_AsmName == assembly).ToArray();
            if (matchingTypes.Length != 1) throw new InvalidDataException($"Referenced payload has no unique embedded TypeTree: index={index}, rid={rid}, class={className}, namespace={nameSpace}, assembly={assembly}, matches={matchingTypes.Length}, offset={payloadOffset}.");
            try
            {
                var payload = TypeTreeHelper.ReadTypePayload(PayloadTree(matchingTypes[0].m_Type), raw, payloadOffset, raw.Length - payloadOffset, out long payloadBytes);
                if (payloadBytes <= 0 || payloadBytes > raw.Length - payloadOffset) throw new InvalidDataException($"Invalid payload consumption: index={index}, rid={rid}, bytes={payloadBytes}.");
                JsonElement data = JsonDocument.Parse(Newtonsoft.Json.JsonConvert.SerializeObject(payload)).RootElement.Clone();
                records.Add(new ManagedReference(rid, className, nameSpace, assembly, recordOffset, payloadOffset, checked((int)payloadBytes), data));
                reader.Position = payloadOffset + payloadBytes;
                reader.AlignStream();
            }
            catch (Exception error) when (error is InvalidDataException or EndOfStreamException or ArgumentOutOfRangeException)
            {
                File.WriteAllText(Path.Combine(output, "registry_failure.json"), JsonSerializer.Serialize(new { stage = "existing_reference_typetree_payload", index, rid, class_name = className, payload_offset = payloadOffset, remaining = raw.Length - payloadOffset, error_type = error.GetType().Name, message = error.Message }, options));
                throw;
            }
        }
        if (reader.Position != raw.Length) throw new InvalidDataException($"Registry did not consume exact object tail: position={reader.Position}, bytes={raw.Length}.");
        if (records.Select(record => record.Rid).Distinct().Count() != records.Count) throw new InvalidDataException("Registry RID values are duplicated.");
        File.WriteAllText(Path.Combine(output, "managed_references.json"), JsonSerializer.Serialize(records.ToArray(), options));
        File.WriteAllText(Path.Combine(output, "registry_decode_receipt.json"), JsonSerializer.Serialize(new { status = "entire_registry_tail_decoded", registry_version = version, reference_count = count, registry_offset = registryOffset, bytes_consumed = reader.Position - registryOffset, object_bytes = raw.Length, final_offset = reader.Position, remaining_bytes = reader.Remaining, class_counts = records.GroupBy(record => record.Class).Select(group => new { class_name = group.Key, count = group.Count() }).ToArray(), decoder = "Existing TypeTreeHelper.ReadTypePayload with exact embedded SerializedFile.m_RefTypes; diagnostic reads v2 RID/type envelope and excludes owner registry metadata from inline referenced payload schema", shared_parser_modified = false }, options));
    }
}

