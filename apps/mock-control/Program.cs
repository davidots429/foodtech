using System.Security.Cryptography;
using System.Text;
using System.Text.Json;
using System.Text.Encodings.Web;
using System.Globalization;

const string programVersion = "1.0.0";
var options = ParseArgs(args);
var fixturePath = Required(options, "fixture");
using var fixture = JsonDocument.Parse(File.ReadAllText(fixturePath, Encoding.UTF8));
var root = fixture.RootElement;
if (root.GetProperty("provenance").GetString() != "SYNTHETIC" ||
    root.GetProperty("instanceId").GetString() != "mockprofile-100-a-001")
    throw new InvalidDataException("Only the approved synthetic profile is supported by this slice.");
var records = root.GetProperty("records").EnumerateArray().Select(item => item.Clone()).ToArray();

if (args.Length > 0 && args[0] == "hash")
{
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
        programVersion, profileId = "mockprofile-100", instanceId = "mockprofile-100-a-001",
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
    if (cursor is < 0 || cursor > records.Length) return Results.BadRequest(new { error = "invalid_cursor" });
    if (disconnectOnce && !disconnected)
    {
        disconnected = true;
        context.Abort();
        return Results.Empty;
    }
    var start = cursor ?? 0;
    context.Response.Headers["X-Contract-Version"] = "1.0";
    context.Response.Headers["X-Profile-ID"] = "mockprofile-100";
    context.Response.Headers["X-Provenance"] = "SYNTHETIC";
    context.Response.Headers["X-Next-Cursor"] = records.Length.ToString();
    var lines = string.Join("\n", records.Skip(start).Select(record => JsonSerializer.Serialize(record)));
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
        result[name] = name == "disconnect-once" ? "true" : i + 1 < args.Length ? args[++i] : throw new ArgumentException($"Missing value for {name}.");
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

