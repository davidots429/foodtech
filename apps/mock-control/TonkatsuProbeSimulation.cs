using System.Globalization;
using System.Text;
using System.Text.Json;

// Synthetic samples only: the platform, not the Mock, determines safety.
internal static class TonkatsuProbeSimulation
{
    public static void Emit(string fixturePath, string outputPath)
    {
        if (string.Equals(Path.GetFullPath(fixturePath), Path.GetFullPath(outputPath),
                          OperatingSystem.IsWindows() ? StringComparison.OrdinalIgnoreCase : StringComparison.Ordinal))
            throw new ArgumentException("Fixture and output must be different files.");
        using var fixture = JsonDocument.Parse(File.ReadAllText(fixturePath, Encoding.UTF8));
        var root = fixture.RootElement;
        if (root.GetProperty("provenance").GetString() != "SYNTHETIC" ||
            root.GetProperty("schemaVersion").GetString() != "tonkatsu-core-fixture-v1")
            throw new InvalidDataException("Only the approved synthetic probe fixture is supported.");
        var count = root.GetProperty("productCount").GetInt32();
        var scenario = root.GetProperty("scenario").GetString();
        var affected = root.GetProperty("affectedProduct").GetInt32();
        var seed = root.GetProperty("seed").GetInt32();
        var startText = root.GetProperty("measurementStart").GetString();
        if (count is < 1 or > 18 || affected < 1 || affected > count || seed < 0 ||
            scenario is not ("NORMAL" or "LOW_CORE" or "MISSING_SAMPLE" or "EXPIRED_CALIBRATION") ||
            startText is null || !(startText.EndsWith("Z", StringComparison.Ordinal) ||
                startText.EndsWith("+00:00", StringComparison.Ordinal)) ||
            !DateTimeOffset.TryParse(startText, CultureInfo.InvariantCulture, DateTimeStyles.None, out var start) ||
            start.Offset != TimeSpan.Zero)
            throw new InvalidDataException("Invalid synthetic probe fixture parameters.");
        var probes = Enumerable.Range(1, count).Select(index => new {
            item_id = $"product-{index:D2}",
            probe_id = $"synthetic-probe-{index:D2}",
            removal_sequence = index,
            measurement_start = start.ToString("O", CultureInfo.InvariantCulture),
            measurement_end = start.AddSeconds(60).ToString("O", CultureInfo.InvariantCulture),
            calibration_status = scenario == "EXPIRED_CALIBRATION" && index == affected ? "EXPIRED" : "OK",
            samples = Enumerable.Range(0, 61)
                .Where(second => !(scenario == "MISSING_SAMPLE" && index == affected && second == 30))
                .Select(second => new {
                    at_second = second,
                    celsius = scenario == "LOW_CORE" && index == affected && second == 30
                        ? 74m : 77m + ((seed % 10 + index + second) % 10) / 10m
                }).ToArray()
        }).ToArray();
        var result = new {
            schema_version = "tonkatsu-core-samples-v1", provenance = "SYNTHETIC",
            generator_version = "1.0.0", fixture_version = "tonkatsu-core-fixture-v1", seed,
            measurement_start_utc = start.ToString("O", CultureInfo.InvariantCulture),
            measurement_end_utc = start.AddSeconds(60).ToString("O", CultureInfo.InvariantCulture),
            scenario,
            probe_manifest = Enumerable.Range(1, count).Select(index => new {
                item_id = $"product-{index:D2}", probe_id = $"synthetic-probe-{index:D2}"
            }).ToArray(),
            probes
        };
        File.WriteAllText(outputPath, JsonSerializer.Serialize(result), new UTF8Encoding(false));
        Console.WriteLine(JsonSerializer.Serialize(new { provenance = "SYNTHETIC", products = count,
                                                         output = outputPath, scenario }));
    }
}
