using System.Globalization;
using System.Text;
using System.Text.Json;
using System.Text.RegularExpressions;

internal static class CombiSimulation
{
    private const int MaximumSamples = 5000;
    private static readonly JsonSerializerOptions JsonOptions = new() { PropertyNamingPolicy = JsonNamingPolicy.CamelCase };

    private sealed record Observation(string EventId, string MealBatchId, string CookingJobId,
        string DeviceId, string MockInstanceId, string EventType, string OccurredAt, string ReceivedAt,
        int Sequence, string SchemaVersion, string ProgramVersion, string Provenance, string DataQuality,
        string Phase, int PhaseSecond, decimal ChamberCelsius, decimal ClimateSetpointPercent,
        decimal? MeasuredHumidityPercent, string HumidityValueSource, int FanLevel, string DoorState);

    public static void Emit(string fixturePath, string outputPath, string aggregatePath)
    {
        if (Path.GetFullPath(outputPath) == Path.GetFullPath(aggregatePath))
            throw new ArgumentException("Raw and aggregate outputs must be separate files.");
        using var fixture = JsonDocument.Parse(File.ReadAllText(fixturePath, Encoding.UTF8));
        var root = fixture.RootElement;
        if (String(root, "provenance") != "SYNTHETIC" || String(root, "profileId") != "mockprofile-120")
            throw new InvalidDataException("Only the synthetic combi profile is supported.");
        var meal = Id(root, "mealBatchId");
        var device = Id(root, "deviceId");
        var instance = Id(root, "mockInstanceId");
        if (instance != "mockprofile-120-a-001")
            throw new InvalidDataException("Unsupported synthetic combi instance.");
        var generator = Id(root, "generatorVersion");
        var seed = root.GetProperty("seed").GetInt32();
        var startText = String(root, "logicalStartAt");
        if (!(startText.EndsWith("Z", StringComparison.Ordinal) || startText.EndsWith("+00:00", StringComparison.Ordinal)) ||
            !DateTimeOffset.TryParse(startText, CultureInfo.InvariantCulture,
                                     DateTimeStyles.None, out var start) || start.Offset != TimeSpan.Zero)
            throw new InvalidDataException("logicalStartAt must include UTC offset.");
        var phases = root.GetProperty("phases");
        if (phases.ValueKind != JsonValueKind.Array || phases.GetArrayLength() != 3)
            throw new InvalidDataException("Exactly rice, transition and mackerel phases are required.");
        var raw = new List<Observation>();
        var phasesSeen = new List<string>();
        var jobsSeen = new HashSet<string>(StringComparer.Ordinal);
        foreach (var phase in phases.EnumerateArray())
        {
            var name = String(phase, "phase");
            phasesSeen.Add(name);
            var job = Id(phase, "cookingJobId");
            if (!jobsSeen.Add(job)) throw new InvalidDataException("Phase identifiers must be distinct.");
            var version = Id(phase, "programVersion");
            var segments = phase.GetProperty("segments");
            if (segments.ValueKind != JsonValueKind.Array || segments.GetArrayLength() == 0)
                throw new InvalidDataException("A phase needs at least one segment.");
            var phaseSecond = 0;
            foreach (var segment in segments.EnumerateArray())
            {
                var seconds = segment.GetProperty("seconds").GetInt32();
                if (seconds <= 0 || seconds > MaximumSamples - raw.Count)
                    throw new InvalidDataException("Invalid or excessive sample duration.");
                var temperature = Number(segment, "chamberCelsius", -50, 350);
                var climate = Number(segment, "climateSetpointPercent", 0, 100);
                var humiditySource = String(segment, "humidityValueSource");
                decimal? humidity = humiditySource == "NOT_SUPPORTED" ? null : Number(segment, "measuredHumidityPercent", 0, 100);
                if (humiditySource is not ("MEASURED" or "CONTROLLER_ESTIMATE" or "NOT_SUPPORTED") ||
                    (humiditySource == "NOT_SUPPORTED" && segment.TryGetProperty("measuredHumidityPercent", out var absent) &&
                     absent.ValueKind != JsonValueKind.Null))
                    throw new InvalidDataException("Invalid humidity source or value.");
                var fan = segment.GetProperty("fanLevel").GetInt32();
                var door = String(segment, "doorState");
                if (fan is < 0 or > 10 || door is not ("OPEN" or "CLOSED"))
                    throw new InvalidDataException("Invalid fan or door state.");
                for (var i = 0; i < seconds; i++)
                {
                    var sequence = raw.Count + 1;
                    var at = start.AddSeconds(sequence - 1).ToUniversalTime().ToString("O", CultureInfo.InvariantCulture);
                    raw.Add(new Observation($"{meal}-{sequence:D6}", meal, job, device, instance,
                        "OBSERVATION", at, at, sequence, "synthetic-combi-v1", version, "SYNTHETIC", "VALID",
                        name, ++phaseSecond, temperature, climate, humidity, humiditySource, fan, door));
                }
            }
            if (phaseSecond % 5 != 0)
                throw new InvalidDataException("Each phase must contain complete five-second windows.");
        }
        if (!phasesSeen.SequenceEqual(new[] { "RICE", "TRANSITION", "MACKEREL" }))
            throw new InvalidDataException("Phases must be rice, transition, then mackerel.");
        var aggregates = raw.Chunk(5).Select(group => new {
            mealBatchId = meal, cookingJobId = group[0].CookingJobId, phase = group[0].Phase,
            firstSequence = group[0].Sequence, lastSequence = group[^1].Sequence,
            firstPhaseSecond = group[0].PhaseSecond, lastPhaseSecond = group[^1].PhaseSecond,
            currentCelsius = group[^1].ChamberCelsius, minimumCelsius = group.Min(x => x.ChamberCelsius),
            maximumCelsius = group.Max(x => x.ChamberCelsius), averageCelsius = group.Average(x => x.ChamberCelsius),
            outsideTargetSeconds = group.Count(x => x.Phase == "RICE" ? x.ChamberCelsius < 98 || x.ChamberCelsius > 102 :
                x.ChamberCelsius < 215 || x.ChamberCelsius > 225), provenance = "SYNTHETIC"
        }).ToArray();
        File.WriteAllLines(outputPath, raw.Select(row => JsonSerializer.Serialize(row, JsonOptions)), new UTF8Encoding(false));
        File.WriteAllLines(aggregatePath, aggregates.Select(row => JsonSerializer.Serialize(row, JsonOptions)), new UTF8Encoding(false));
        Console.WriteLine(JsonSerializer.Serialize(new { mealBatchId = meal, generatorVersion = generator, seed,
            logicalStartAt = start, provenance = "SYNTHETIC", rawCount = raw.Count, aggregateCount = aggregates.Length }));
    }

    private static string String(JsonElement element, string name) =>
        element.GetProperty(name).GetString() is { Length: > 0 } value ? value :
        throw new InvalidDataException($"{name} is required.");

    private static string Id(JsonElement element, string name)
    {
        var value = String(element, name);
        return Regex.IsMatch(value, @"\A[A-Za-z0-9][A-Za-z0-9._:-]{0,79}\z", RegexOptions.CultureInvariant) ? value :
            throw new InvalidDataException($"{name} must be a valid identifier.");
    }

    private static decimal Number(JsonElement element, string name, decimal min, decimal max)
    {
        var value = element.GetProperty(name);
        if (value.ValueKind != JsonValueKind.String ||
            !decimal.TryParse(value.GetString(), NumberStyles.Number, CultureInfo.InvariantCulture, out var number) ||
            number < min || number > max)
            throw new InvalidDataException($"{name} must be a decimal string in range.");
        return number;
    }
}
