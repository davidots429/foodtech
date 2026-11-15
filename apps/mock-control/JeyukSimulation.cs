using System.Globalization;
using System.Text;
using System.Text.Json;
using System.Text.RegularExpressions;
using System.Text.Json.Serialization;

internal static class JeyukSimulation
{
    private const int MaximumSamples = 10_000;
    private static readonly JsonSerializerOptions JsonOptions = new() {
        PropertyNamingPolicy = JsonNamingPolicy.CamelCase,
        DefaultIgnoreCondition = JsonIgnoreCondition.WhenWritingNull,
    };

    public static void Emit(string fixturePath, string outputPath)
    {
        using var fixture = JsonDocument.Parse(File.ReadAllText(fixturePath, Encoding.UTF8));
        var root = fixture.RootElement;
        var lines = Expand(root);
        using (var writer = new StreamWriter(outputPath, false, new UTF8Encoding(false)))
            foreach (var line in lines)
                writer.WriteLine(line);
        Console.WriteLine(JsonSerializer.Serialize(new
        {
            fixtureId = RequiredString(root, "fixtureId"), provenance = "SYNTHETIC",
            profileId = "mockprofile-110", instanceId = "mockprofile-110-a-001",
            profile = RequiredString(root, "profile"), sampleIntervalSeconds = 1,
            count = lines.Count, output = outputPath,
        }));
    }

    public static IReadOnlyList<string> Expand(JsonElement root)
    {
        var fixtureId = RequiredString(root, "fixtureId");
        if (!Regex.IsMatch(fixtureId, @"\A[A-Za-z0-9-]{1,80}\z", RegexOptions.CultureInvariant))
            throw new InvalidDataException("fixtureId must contain only letters, digits, or hyphens (max 80).");
        var profile = RequiredString(root, "profile");
        if (root.GetProperty("provenance").GetString() != "SYNTHETIC" ||
            profile is not ("SMALL" or "MEDIUM" or "LARGE"))
            throw new InvalidDataException("Only approved synthetic jeyuk profiles are supported.");
        var actualLoadKg = PositiveDecimal(root, "actualLoadKg");
        var segments = root.GetProperty("samples");
        if (segments.ValueKind != JsonValueKind.Array || segments.GetArrayLength() == 0)
            throw new InvalidDataException("At least one sample segment is required.");

        // Validate the entire fixture before creating the output file.
        var samples = new List<JeyukSample>();
        bool? safetyFixture = null;
        var completionEvents = 0;
        foreach (var segment in segments.EnumerateArray())
        {
            var seconds = segment.GetProperty("seconds").GetInt32();
            if (seconds <= 0 || seconds > MaximumSamples - samples.Count)
                throw new InvalidDataException("Invalid or excessive sample duration.");
            var surface = Decimal(segment, "surfaceCelsius");
            var actualRpm = NonnegativeDecimal(segment, "actualRpm");
            var targetRpm = NonnegativeDecimal(segment, "targetRpm");
            var motorLoad = NonnegativeDecimal(segment, "motorLoadPercent");
            var hasSafety = segment.TryGetProperty("mixtureCelsius", out _);
            if (safetyFixture is not null && safetyFixture != hasSafety)
                throw new InvalidDataException("Safety fields must be present on every sample segment.");
            safetyFixture = hasSafety;
            decimal? mixture = null;
            string? calibration = null;
            string? sensorPosition = null;
            bool? lastIngredientCompleted = null;
            if (hasSafety)
            {
                mixture = NonnegativeDecimal(segment, "mixtureCelsius");
                calibration = RequiredString(segment, "calibrationStatus");
                sensorPosition = RequiredString(segment, "safetySensorPosition");
                lastIngredientCompleted = segment.GetProperty("lastIngredientAdditionCompleted").GetBoolean();
                if (calibration is not ("OK" or "EXPIRED") || sensorPosition != "LOWEST_MIXTURE_POINT")
                    throw new InvalidDataException("Unsupported synthetic safety sensor contract.");
                if (lastIngredientCompleted == true)
                {
                    if (seconds != 1) throw new InvalidDataException("The final addition event must be one sample.");
                    completionEvents++;
                }
            }
            else if (segment.TryGetProperty("calibrationStatus", out _) ||
                     segment.TryGetProperty("safetySensorPosition", out _) ||
                     segment.TryGetProperty("lastIngredientAdditionCompleted", out _))
                throw new InvalidDataException("Incomplete synthetic safety sensor fields.");
            for (var i = 0; i < seconds; i++)
                samples.Add(new JeyukSample(
                    fixtureId, "SYNTHETIC", "mockprofile-110", "mockprofile-110-a-001",
                    profile, actualLoadKg, samples.Count + 1, 1,
                    surface, actualRpm, targetRpm, motorLoad, mixture, calibration,
                    sensorPosition, lastIngredientCompleted));
        }
        if (safetyFixture == true && completionEvents != 1)
            throw new InvalidDataException("Exactly one final ingredient addition event is required.");

        return samples.Select(sample => JsonSerializer.Serialize(sample, JsonOptions)).ToArray();
    }

    private static string RequiredString(JsonElement root, string name)
    {
        var value = root.GetProperty(name).GetString();
        return !string.IsNullOrWhiteSpace(value) ? value : throw new InvalidDataException($"{name} is required.");
    }

    private static decimal PositiveDecimal(JsonElement root, string name)
    {
        var value = Decimal(root, name);
        return value > 0 ? value : throw new InvalidDataException($"{name} must be positive.");
    }

    private static decimal NonnegativeDecimal(JsonElement root, string name)
    {
        var value = Decimal(root, name);
        return value >= 0 ? value : throw new InvalidDataException($"{name} must be nonnegative.");
    }

    private static decimal Decimal(JsonElement root, string name)
    {
        var element = root.GetProperty(name);
        if (element.ValueKind != JsonValueKind.String ||
            !decimal.TryParse(element.GetString(), NumberStyles.Number, CultureInfo.InvariantCulture, out var value))
            throw new InvalidDataException($"{name} must be a decimal string.");
        return value;
    }

    private sealed record JeyukSample(
        string FixtureId, string Provenance, string ProfileId, string InstanceId,
        string Profile, decimal ActualLoadKg, int Second, int SampleIntervalSeconds,
        decimal SurfaceCelsius, decimal ActualRpm, decimal TargetRpm, decimal MotorLoadPercent,
        decimal? MixtureCelsius, string? CalibrationStatus, string? SafetySensorPosition,
        bool? LastIngredientAdditionCompleted);
}
