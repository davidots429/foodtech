using System.Diagnostics;
using System.Security.Cryptography;
using System.Text;
using System.Text.Json;

// A project-specific synthetic HTTPS contract, not an OPC UA standard implementation.
internal static class UaJsonMock
{
    public static async Task Run(string fixturePath, string certPath, int port, System.Net.IPAddress? bindAddress)
    {
        if (port is < 1024 or > 65535) throw new ArgumentOutOfRangeException(nameof(port));
        var clientId = Environment.GetEnvironmentVariable("FOODTECH_MOCK_CLIENT_ID");
        var secret = Environment.GetEnvironmentVariable("FOODTECH_MOCK_CLIENT_SECRET");
        var password = Environment.GetEnvironmentVariable("FOODTECH_MOCK_CERT_PASSWORD");
        if (string.IsNullOrWhiteSpace(clientId) || string.IsNullOrWhiteSpace(secret) || string.IsNullOrWhiteSpace(password))
            throw new InvalidOperationException("Mock credentials and certificate password are required.");
        using var fixture = JsonDocument.Parse(File.ReadAllText(fixturePath));
        var root = fixture.RootElement;
        var instance = root.GetProperty("instanceId").GetString();
        var profile = root.GetProperty("profileId").GetString();
        if (root.GetProperty("provenance").GetString() != "SYNTHETIC" ||
            profile is not ("mockprofile-200" or "mockprofile-210" or "mockprofile-220") ||
            instance is null || !new[] { $"{profile}-a-001", $"{profile}-b-001", $"{profile}-c-001" }.Contains(instance))
            throw new InvalidDataException("Only the approved synthetic fixture is supported.");
        var b = instance == $"{profile}-b-001";
        var c = instance == $"{profile}-c-001";
        var records = root.GetProperty("records").EnumerateArray().ToArray();
        if (records.Length is < 1 or > 5000 || records.Where((r, i) =>
            r.GetProperty(b ? "device_id" : c ? "DeviceId" : "deviceId").GetString() != instance ||
            r.GetProperty(b ? "source_id" : c ? "SrcId" : "sourceId").GetString() != "synthetic-opcua-json" ||
            r.GetProperty(b ? "sequence_number" : c ? "SeqNo" : "sequenceNumber").GetInt32() != i + 1).Any())
            throw new InvalidDataException("Invalid synthetic source records.");
        var lines = records.Select(item => JsonSerializer.Serialize(item)).ToArray();
        if (lines.Any(line => Encoding.UTF8.GetByteCount(line) > 2048) ||
            Encoding.UTF8.GetByteCount(JsonSerializer.Serialize(lines)) > 2_000_000)
            throw new InvalidDataException("Synthetic source history is too large.");
        var builder = WebApplication.CreateBuilder(Array.Empty<string>());
        builder.WebHost.ConfigureKestrel(server => {
            if (bindAddress is null) server.ListenLocalhost(port, listen => listen.UseHttps(certPath, password));
            else server.Listen(bindAddress, port, listen => listen.UseHttps(certPath, password));
        });
        var app = builder.Build();
        var tokens = new Dictionary<string, DateTimeOffset>();
        var gate = new object();
        var clock = new Stopwatch();
        bool Authorized(HttpContext ctx)
        {
            var header = ctx.Request.Headers.Authorization.ToString();
            if (!header.StartsWith("Bearer ", StringComparison.Ordinal)) return false;
            lock (gate) return tokens.TryGetValue(header[7..], out var expiry) && expiry > DateTimeOffset.UtcNow;
        }
        void Headers(HttpContext ctx)
        {
            ctx.Response.Headers["X-Contract-Version"] = "1.0";
            ctx.Response.Headers["X-Profile-ID"] = profile;
            ctx.Response.Headers["X-Provenance"] = "SYNTHETIC";
            ctx.Response.Headers.CacheControl = "no-store";
        }
        app.MapPost("/oauth2/token", async (HttpContext ctx) =>
        {
            if (!ctx.Request.HasFormContentType) return Results.BadRequest();
            var form = await ctx.Request.ReadFormAsync();
            var expected = "Basic " + Convert.ToBase64String(Encoding.UTF8.GetBytes($"{clientId}:{secret}"));
            var actualHash = SHA256.HashData(Encoding.UTF8.GetBytes(ctx.Request.Headers.Authorization.ToString()));
            var expectedHash = SHA256.HashData(Encoding.UTF8.GetBytes(expected));
            if (!CryptographicOperations.FixedTimeEquals(actualHash, expectedHash)) return Results.Unauthorized();
            if (form["grant_type"] != "client_credentials") return Results.BadRequest();
            var token = Convert.ToHexStringLower(RandomNumberGenerator.GetBytes(32));
            lock (gate)
            {
                foreach (var expired in tokens.Where(x => x.Value <= DateTimeOffset.UtcNow).Select(x => x.Key).ToArray()) tokens.Remove(expired);
                tokens[token] = DateTimeOffset.UtcNow.AddMinutes(5);
            }
            ctx.Response.Headers.CacheControl = "no-store";
            return Results.Json(new { access_token = token, token_type = "Bearer", expires_in = 300 });
        });
        app.MapGet("/ua-json/v1/browse", (HttpContext ctx) =>
        {
            if (!Authorized(ctx)) return Results.Unauthorized();
            Headers(ctx);
            return Results.Json(new { nodes = new[] { "DeviceId", "AvailableSnapshots" } });
        });
        app.MapGet("/ua-json/v1/read", (HttpContext ctx, string node) =>
        {
            if (!Authorized(ctx)) return Results.Unauthorized();
            Headers(ctx);
            if (node == "DeviceId") return Results.Json(new { node, value = instance });
            if (node != "AvailableSnapshots") return Results.NotFound();
            lock (gate)
            {
                if (!clock.IsRunning) clock.Start();
                var count = (int)Math.Min(lines.Length, 1 + clock.ElapsedMilliseconds / 5000);
                return Results.Json(new { node, value = lines.Take(count).ToArray() });
            }
        });
        app.MapGet("/health", () => Results.Json(new { status = "ok", provenance = "SYNTHETIC" }));
        await app.RunAsync();
    }
}
