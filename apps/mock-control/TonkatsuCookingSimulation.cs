using System.Globalization;
using System.Text;
using System.Text.Json;
using System.Text.RegularExpressions;

// Explicit synthetic 1Hz controller observations in ordinary 5-second REST records.
// Values are fixture data, never a manufacturer specification or a safety verdict.
internal static class TonkatsuCookingSimulation
{
    public static void Emit(string fixturePath, string outputPath)
    {
        if (string.Equals(Path.GetFullPath(fixturePath), Path.GetFullPath(outputPath),
                          OperatingSystem.IsWindows() ? StringComparison.OrdinalIgnoreCase : StringComparison.Ordinal))
            throw new ArgumentException("Fixture and output must be different files.");
        using var fixture = JsonDocument.Parse(File.ReadAllText(fixturePath, Encoding.UTF8));
        var root = fixture.RootElement;
        if (root.GetProperty("schemaVersion").GetString() != "tonkatsu-cooking-fixture-v1" ||
            root.GetProperty("provenance").GetString() != "SYNTHETIC")
            throw new InvalidDataException("An explicit synthetic fryer fixture is required.");
        var batch = root.GetProperty("fixtureId").GetString();
        var seed = root.GetProperty("seed").GetInt32();
        var startText = root.GetProperty("logicalStartAt").GetString();
        if (batch is null || !Regex.IsMatch(batch, @"\Abatch-a-[A-Za-z0-9-]{1,72}\z") || seed < 0 ||
            startText is null || !(startText.EndsWith("Z", StringComparison.Ordinal) ||
                startText.EndsWith("+00:00", StringComparison.Ordinal)) ||
            !DateTimeOffset.TryParse(startText, CultureInfo.InvariantCulture, DateTimeStyles.None, out var start) ||
            start.Offset != TimeSpan.Zero)
            throw new InvalidDataException("Invalid synthetic fryer identity, seed or logical time.");
        var samples = new List<object>();
        var temperatures = new List<decimal>();
        var segments = root.GetProperty("segments");
        if (segments.ValueKind != JsonValueKind.Array || segments.GetArrayLength() is < 1 or > 1000)
            throw new InvalidDataException("A bounded list of explicit temperature segments is required.");
        foreach (var segment in segments.EnumerateArray())
        {
            var seconds = segment.GetProperty("seconds").GetInt32();
            var temperature = segment.GetProperty("oilTemperatureCelsius").GetDecimal();
            var calibration = segment.GetProperty("calibrationStatus").GetString();
            if (seconds is < 1 or > 5000 || samples.Count + seconds > 5000 ||
                temperature is < -273.15m or > 300m || calibration is not ("OK" or "EXPIRED"))
                throw new InvalidDataException("Invalid synthetic fryer segment.");
            for (var index = 0; index < seconds; index++)
            {
                var second = samples.Count + 1;
                samples.Add(new {
                    second, observedAt = start.AddSeconds(second).ToString("O", CultureInfo.InvariantCulture),
                    oilTemperatureCelsius = temperature, calibrationStatus = calibration
                });
                temperatures.Add(temperature);
            }
        }
        if (samples.Count % 5 != 0)
            throw new InvalidDataException("Controller samples must fill complete 5-second envelopes.");
        var records = Enumerable.Range(1, samples.Count / 5).Select(sequence => new {
            schemaVersion = "1.0", sourceId = "synthetic-rest", deviceId = "mockprofile-100-a-001",
            batchId = batch, recordId = $"rec-a-{batch}-{seed}-{sequence:D4}", sequenceNumber = sequence,
            eventTime = start.AddSeconds(sequence * 5).ToString("O", CultureInfo.InvariantCulture), deviceStatus = "running",
            measurements = new { temperatureCelsius = temperatures[sequence * 5 - 1], speedRpm = 0, elapsedSeconds = sequence * 5 },
            extensions = new { tonkatsuSamples = new {
                schemaVersion = "tonkatsu-controller-samples-v1", sampleIntervalSeconds = 1,
                samples = samples.Skip((sequence - 1) * 5).Take(5).ToArray()
            }}
        }).ToArray();
        var result = new {
            contractVersion = "1.0", profileId = "mockprofile-100", instanceId = "mockprofile-100-a-001",
            provenance = "SYNTHETIC", generatorVersion = "tonkatsu-cooking-generator-v1", seed,
            logicalStartTime = start.ToString("O", CultureInfo.InvariantCulture), records
        };
        // No partial overwrite or reuse of an old generated trial file.
        using var output = new FileStream(outputPath, FileMode.CreateNew, FileAccess.Write);
        using var writer = new StreamWriter(output, new UTF8Encoding(false));
        writer.Write(JsonSerializer.Serialize(result));
        Console.WriteLine(JsonSerializer.Serialize(new { provenance = "SYNTHETIC", records = records.Length,
            controllerSamples = samples.Count, generatorVersion = "tonkatsu-cooking-generator-v1" }));
    }
}
