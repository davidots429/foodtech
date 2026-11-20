using System.Security.Cryptography;
using System.Text;
using System.Text.Json;
using System.Text.Encodings.Web;
using System.Globalization;
using System.Diagnostics;

const string programVersion = "1.0.0";
var options = ParseArgs(args);
if (args.Length > 0 && args[0] == "serve-ua-json")
{
    await UaJsonMock.Run(Required(options, "fixture"), Required(options, "cert"),
                         int.Parse(Required(options, "port")));
    return;
}
if (args.Length > 0 && args[0] == "serve-opcua")
{
    await OpcUaMock.Run(Required(options, "fixture"), Required(options, "pki"),
                        int.Parse(Required(options, "port")));
    return;
}
if (args.Length > 0 && args[0] == "serve-mqtt")
{
    await MqttMock.Run(Required(options, "fixture"), Required(options, "cert"),
                       int.Parse(Required(options, "port")), options.GetValueOrDefault("fault"));
    return;
}
if (args.Length > 0 && args[0] == "simulate-jeyuk")
{
    JeyukSimulation.Emit(Required(options, "fixture"), Required(options, "output"));
    return;
}
if (args.Length > 0 && args[0] == "simulate-combi")
{
    CombiSimulation.Emit(Required(options, "fixture"), Required(options, "output"),
                         Required(options, "aggregates"));
    return;
}
var fixturePath = Required(options, "fixture");
using var fixture = JsonDocument.Parse(File.ReadAllText(fixturePath, Encoding.UTF8));
var root = fixture.RootElement;
var jeyuk = root.TryGetProperty("fixtureId", out _);
var combi = root.TryGetProperty("profileId", out var profile) && profile.GetString() == "mockprofile-120";
var xml = root.TryGetProperty("profileId", out var xmlProfile) && xmlProfile.GetString() == "mockprofile-101";
if (!jeyuk && !combi && !xml && (root.GetProperty("provenance").GetString() != "SYNTHETIC" ||
    root.GetProperty("instanceId").GetString() is not ("mockprofile-100-a-001" or "mockprofile-100-b-001" or "mockprofile-100-c-001")))
    throw new InvalidDataException("Only approved synthetic profiles are supported.");
if (xml && (root.GetProperty("provenance").GetString() != "SYNTHETIC" ||
            root.GetProperty("instanceId").GetString() is not ("mockprofile-101-a-001" or "mockprofile-101-b-001" or "mockprofile-101-c-001")))
    throw new InvalidDataException("Only approved synthetic XML is supported.");
if ((jeyuk || combi || xml) && (args.Length == 0 || args[0] != "serve"))
    throw new ArgumentException("Use the matching simulation command or serve for this fixture.");
var instanceId = jeyuk || combi ? "" : root.GetProperty("instanceId").GetString();
if (!jeyuk && !combi && !xml && root.GetProperty("profileId").GetString() != "mockprofile-100")
    throw new InvalidDataException("Unexpected synthetic profile.");
var records = jeyuk || combi ? Array.Empty<JsonElement>() :
    root.GetProperty("records").EnumerateArray().Select(item => item.Clone()).ToArray();
var recordLines = jeyuk ? JeyukSimulation.Expand(root).ToArray() : combi ? CombiSimulation.Generate(root).Raw :
    xml ? records.Select(item => item.GetString() ?? throw new InvalidDataException("Invalid XML fixture.")).ToArray() :
    records.Select(item => JsonSerializer.Serialize(item)).ToArray();
var profileId = jeyuk ? "mockprofile-110" : combi ? "mockprofile-120" : xml ? "mockprofile-101" : "mockprofile-100";

if (args.Length > 0 && args[0] == "hash")
{
    if (instanceId != "mockprofile-100-a-001") throw new ArgumentException("Hash vectors only support vendor-a.");
    foreach (var record in records)
    {
        var digest = SHA256.HashData(CanonicalBytes(record));
        Console.WriteLine(record.GetProperty("recordId").GetString() + " " + Convert.ToHexStringLower(digest));
    }
    return;
}

if (args.Length > 0 && args[0] == "emit")
{
    var output = Required(options, "output");
    using var writer = new StreamWriter(output, false, new UTF8Encoding(false));
    foreach (var record in records) writer.WriteLine(JsonSerializer.Serialize(record));
    Console.WriteLine(JsonSerializer.Serialize(new {
        programVersion, profileId = "mockprofile-100", instanceId,
        provenance = "SYNTHETIC", count = records.Length, output
    }));
    return;
}
if (args.Length == 0 || args[0] != "serve")
    throw new ArgumentException("Use emit or serve with --fixture.");

var clientId = Environment.GetEnvironmentVariable("FOODTECH_MOCK_CLIENT_ID");
var clientSecret = Environment.GetEnvironmentVariable("FOODTECH_MOCK_CLIENT_SECRET");
if (string.IsNullOrWhiteSpace(clientId) || string.IsNullOrWhiteSpace(clientSecret))
    throw new InvalidOperationException("Mock OAuth client credentials must be set in the environment.");
var certPath = Required(options, "cert");
var certPassword = Environment.GetEnvironmentVariable("FOODTECH_MOCK_CERT_PASSWORD");
if (string.IsNullOrWhiteSpace(certPassword))
    throw new InvalidOperationException("FOODTECH_MOCK_CERT_PASSWORD is required.");
var port = int.Parse(Required(options, "port"));
if (port is < 1024 or > 65535) throw new ArgumentOutOfRangeException(nameof(port));

var builder = WebApplication.CreateBuilder(Array.Empty<string>());
builder.WebHost.ConfigureKestrel(server => server.ListenLocalhost(port, listen => listen.UseHttps(certPath, certPassword)));
var app = builder.Build();
var accessToken = "";
var tokenExpires = DateTimeOffset.MinValue;
var disconnectOnce = options.ContainsKey("disconnect-once");
var disconnected = false;
var realtime = options.ContainsKey("realtime");
if (realtime && !combi) throw new ArgumentException("Real-time replay is only supported for the combi profile.");
var replayClock = Stopwatch.StartNew();
var fault = options.GetValueOrDefault("fault");
if (fault is not (null or "invalid-utf8") || (fault is not null && profileId != "mockprofile-100"))
    throw new ArgumentException("Unsupported REST fault scenario.");

app.MapPost("/oauth2/token", async (HttpContext context) =>
{
    if (!context.Request.HasFormContentType) return Results.BadRequest(new { error = "invalid_request" });
    var form = await context.Request.ReadFormAsync();
    var header = context.Request.Headers.Authorization.ToString();
    var expected = "Basic " + Convert.ToBase64String(Encoding.UTF8.GetBytes($"{clientId}:{clientSecret}"));
    if (!FixedEquals(header, expected)) return Results.Json(new { error = "invalid_client" }, statusCode: 401);
    if (form["grant_type"] != "client_credentials") return Results.BadRequest(new { error = "unsupported_grant_type" });
    accessToken = Convert.ToBase64String(RandomNumberGenerator.GetBytes(32)).TrimEnd('=').Replace('+', '-').Replace('/', '_');
    tokenExpires = DateTimeOffset.UtcNow.AddMinutes(5);
    return Results.Json(new { access_token = accessToken, token_type = "Bearer", expires_in = 300 });
});

app.MapGet("/v1/records", (HttpContext context, int? cursor) =>
{
    var bearer = context.Request.Headers.Authorization.ToString();
    if (tokenExpires <= DateTimeOffset.UtcNow || !FixedEquals(bearer, "Bearer " + accessToken))
        return Results.Unauthorized();
    if (cursor is < 0 || cursor > recordLines.Length) return Results.BadRequest(new { error = "invalid_cursor" });
    if (disconnectOnce && !disconnected)
    {
        disconnected = true;
        context.Abort();
        return Results.Empty;
    }
    var start = cursor ?? 0;
    // Reveal only complete five-second source windows; the cursor permits replay after disconnect.
    var available = realtime ? Math.Min(recordLines.Length, (int)(replayClock.Elapsed.TotalSeconds / 5) * 5)
                             : recordLines.Length;
    if (start > available) return Results.Conflict(new { error = "cursor_not_yet_available" });
    var end = xml ? Math.Min(start + 1, available) : available;
    context.Response.Headers["X-Stream-Complete"] = (end == recordLines.Length ? "true" : "false");
    context.Response.Headers["X-Contract-Version"] = "1.0";
    context.Response.Headers["X-Profile-ID"] = profileId;
    context.Response.Headers["X-Provenance"] = "SYNTHETIC";
    context.Response.Headers["X-Next-Cursor"] = end.ToString();
    if (xml) return Results.Text(start == recordLines.Length ? "" : recordLines[start], "application/xml", Encoding.UTF8);
    if (fault == "invalid-utf8" && start == 0)
    {
        var remaining = Encoding.UTF8.GetBytes(string.Join("\n", recordLines.Skip(1)) +
                                               (recordLines.Length > 1 ? "\n" : ""));
        var bytes = new byte[] { 0xff, 0xfe, 0x80, 0x0a }.Concat(remaining).ToArray();
        return Results.Bytes(bytes, "application/x-ndjson");
    }
    var lines = string.Join("\n", recordLines.Skip(start).Take(end - start));
    return Results.Text(lines.Length == 0 ? "" : lines + "\n", "application/x-ndjson", Encoding.UTF8);
});

app.MapGet("/health", () => Results.Json(new { status = "ok", programVersion, provenance = "SYNTHETIC" }));
app.Run();

static Dictionary<string, string> ParseArgs(string[] args)
{
    var result = new Dictionary<string, string>(StringComparer.Ordinal);
    for (var i = 1; i < args.Length; i++)
    {
        if (!args[i].StartsWith("--", StringComparison.Ordinal)) throw new ArgumentException("Unexpected argument.");
        var name = args[i][2..];
        result[name] = name is "disconnect-once" or "realtime" ? "true" : i + 1 < args.Length ? args[++i] : throw new ArgumentException($"Missing value for {name}.");
    }
    return result;
}

static string Required(Dictionary<string, string> options, string name) =>
    options.TryGetValue(name, out var value) && !string.IsNullOrWhiteSpace(value)
        ? value : throw new ArgumentException($"--{name} is required.");

static bool FixedEquals(string actual, string expected)
{
    var left = SHA256.HashData(Encoding.UTF8.GetBytes(actual));
    var right = SHA256.HashData(Encoding.UTF8.GetBytes(expected));
    return CryptographicOperations.FixedTimeEquals(left, right);
}

static byte[] CanonicalBytes(JsonElement record)
{
    var measurement = record.GetProperty("measurements");
    var extensions = new SortedDictionary<string, string>(StringComparer.Ordinal);
    foreach (var item in record.GetProperty("extensions").EnumerateObject())
    {
        if (item.Value.ValueKind != JsonValueKind.String)
            throw new InvalidDataException("This first hash vector supports string extensions only.");
        extensions[item.Name] = item.Value.GetString()!;
    }
    var moment = DateTimeOffset.Parse(record.GetProperty("eventTime").GetString()!, CultureInfo.InvariantCulture)
        .ToUniversalTime();
    var eventTime = moment.ToString("yyyy-MM-ddTHH:mm:ss", CultureInfo.InvariantCulture);
    var fraction = moment.ToString("ffffff", CultureInfo.InvariantCulture).TrimEnd('0');
    if (fraction.Length > 0) eventTime += "." + fraction;
    eventTime += "Z";
    var normalized = new SortedDictionary<string, object?>(StringComparer.Ordinal)
    {
        ["schema_version"] = record.GetProperty("schemaVersion").GetString(),
        ["source_id"] = record.GetProperty("sourceId").GetString(),
        ["device_id"] = record.GetProperty("deviceId").GetString(),
        ["batch_id"] = record.GetProperty("batchId").GetString(),
        ["record_id"] = record.GetProperty("recordId").GetString(),
        ["sequence_number"] = record.GetProperty("sequenceNumber").GetInt32(),
        ["event_time"] = eventTime,
        ["device_status"] = record.GetProperty("deviceStatus").GetString(),
        ["temperature_celsius"] = Number(measurement.GetProperty("temperatureCelsius")),
        ["speed_rpm"] = Number(measurement.GetProperty("speedRpm")),
        ["elapsed_seconds"] = Number(measurement.GetProperty("elapsedSeconds")),
        ["error_reason"] = record.GetProperty("errorReason").ValueKind == JsonValueKind.Null
            ? null : record.GetProperty("errorReason").GetString(),
        ["extensions"] = extensions,
    };
    return JsonSerializer.SerializeToUtf8Bytes(normalized, new JsonSerializerOptions {
        Encoder = JavaScriptEncoder.UnsafeRelaxedJsonEscaping
    });
}

static string Number(JsonElement value) => value.GetDecimal().ToString("G29", CultureInfo.InvariantCulture);

