using System.Security.Cryptography;
using System.Text.Json;
using AnimeStudio;

if (args.Length != 2) throw new ArgumentException("Expected controller bundle and output directory.");
string bundle = Path.GetFullPath(args[0]);
string output = Path.GetFullPath(args[1]);
Directory.CreateDirectory(output);
TypeFlags.SetTypes(new[] { ClassIDType.AssetBundle, ClassIDType.AnimatorController }.ToDictionary(type => type, _ => (true, false)));
var manager = new AssetsManager { Game = GameManager.GetGameByType(GameType.ArknightsEndfield), ResolveDependencies = false, SpecifyUnityVersion = "2021.3.34f5", Silent = true, SkipProcess = false };
try
{
    manager.LoadPreparedFiles([bundle]);
    var controller = manager.assetsFileList.SelectMany(file => file.Objects).Single(value => value.type == ClassIDType.AnimatorController);
    var tree = controller.ToType() ?? throw new InvalidDataException("Controller embedded TypeTree is missing.");
    string text = Newtonsoft.Json.JsonConvert.SerializeObject(tree, Newtonsoft.Json.Formatting.Indented);
    File.WriteAllText(Path.Combine(output, "controller_tree.json"), text);
    using var parsed = JsonDocument.Parse(text);
    var report = new { bundle, sha256 = Convert.ToHexString(SHA256.HashData(File.ReadAllBytes(bundle))).ToLowerInvariant(),
        cab = controller.assetsFile.fileName, path_id = controller.m_PathID.ToString(),
        name = parsed.RootElement.GetProperty("m_Name").GetString(),
        fields = parsed.RootElement.EnumerateObject().Select(property => property.Name).ToArray(),
        external_cabs = controller.assetsFile.m_Externals.Select(item => item.fileName).ToArray() };
    File.WriteAllText(Path.Combine(output, "controller_receipt.json"), JsonSerializer.Serialize(report, new JsonSerializerOptions { WriteIndented = true }));
    Console.WriteLine(JsonSerializer.Serialize(new { status = "exact_controller_tree_read", report.name, report.cab, report.path_id, report.fields }));
}
finally { manager.Clear(); }
