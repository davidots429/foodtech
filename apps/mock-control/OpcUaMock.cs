using System.Diagnostics;
using System.Text.Json;
using Microsoft.Extensions.DependencyInjection;
using Microsoft.Extensions.Hosting;
using Opc.Ua.Server.Fluent;

internal static class OpcUaMock
{
    public static async Task Run(string fixturePath, string pkiDirectory, int port)
    {
        if (port is < 1024 or > 65535 || !Path.IsPathFullyQualified(pkiDirectory))
            throw new ArgumentException("A valid port and absolute PKI directory are required.");
        using var fixture = JsonDocument.Parse(File.ReadAllText(fixturePath));
        var root = fixture.RootElement;
        if (root.GetProperty("provenance").GetString() != "SYNTHETIC" ||
            root.GetProperty("profileId").GetString() != "mockprofile-203" ||
            root.GetProperty("instanceId").GetString() != "mockprofile-203-a-001")
            throw new InvalidDataException("Only the synthetic UA Binary profile is supported.");
        var records = root.GetProperty("records").EnumerateArray().Select((record, index) => {
            if (record.GetProperty("sourceId").GetString() != "synthetic-opcua" ||
                record.GetProperty("deviceId").GetString() != "mockprofile-203-a-001" ||
                record.GetProperty("schemaVersion").GetString() != "1.0" ||
                record.GetProperty("sequenceNumber").GetInt32() != index + 1)
                throw new InvalidDataException("Invalid synthetic OPC UA identity or sequence.");
            foreach (var name in new[] { "batchId", "recordId", "eventTime", "deviceStatus" })
                if (string.IsNullOrWhiteSpace(record.GetProperty(name).GetString()))
                    throw new InvalidDataException("Missing synthetic OPC UA value.");
            var measurements = record.GetProperty("measurements");
            _ = measurements.GetProperty("temperatureCelsius").GetDouble();
            _ = measurements.GetProperty("speedRpm").GetInt32();
            _ = measurements.GetProperty("elapsedSeconds").GetInt32();
            if (string.IsNullOrWhiteSpace(record.GetProperty("extensions").GetProperty("firmwareVersion").GetString()))
                throw new InvalidDataException("Missing synthetic OPC UA firmware version.");
            return record.Clone();
        }).ToArray();
        if (records.Length is < 1 or > 5000) throw new InvalidDataException("Invalid fixture length.");
        var clock = new Stopwatch();
        JsonElement Current() => records[Math.Min((int)(clock.ElapsedMilliseconds / 5000), records.Length - 1)];
        var builder = Host.CreateApplicationBuilder();
        builder.Services.AddOpcUa().AddServer(options => {
            options.ApplicationName = "FoodtechSyntheticUa";
            options.ApplicationUri = "urn:localhost:foodtech:synthetic-ua";
            options.ProductUri = "urn:foodtech:synthetic-ua";
            options.EndpointUrls.Add($"opc.tcp://localhost:{port}/foodtech");
            options.PkiRoot = pkiDirectory;
            options.AutoAcceptUntrustedCertificates = false;
            options.IncludeUnsecurePolicyNone = false;
            options.IncludeSignAndEncryptPolicies = true;
            options.RejectSHA1Certificates = true;
            options.MinCertificateKeySize = 2048;
        }).AddNodeManager("urn:foodtech:synthetic:opcua:203:a:v1", nodes => {
            var device = nodes.AddFolder("SyntheticDevice").Node;
            nodes.AddVariable<string>("SchemaVersion", device.NodeId).PollEvery(TimeSpan.FromMilliseconds(250), () => Current().GetProperty("schemaVersion").GetString()!);
            nodes.AddVariable<string>("SourceId", device.NodeId).PollEvery(TimeSpan.FromMilliseconds(250), () => Current().GetProperty("sourceId").GetString()!);
            nodes.AddVariable<string>("DeviceId", device.NodeId).PollEvery(TimeSpan.FromMilliseconds(250), () => Current().GetProperty("deviceId").GetString()!);
            nodes.AddVariable<string>("BatchId", device.NodeId).PollEvery(TimeSpan.FromMilliseconds(250), () => Current().GetProperty("batchId").GetString()!);
            nodes.AddVariable<string>("RecordId", device.NodeId).PollEvery(TimeSpan.FromMilliseconds(250), () => Current().GetProperty("recordId").GetString()!);
            nodes.AddVariable<int>("Sequence", device.NodeId).PollEvery(TimeSpan.FromMilliseconds(250), () => Current().GetProperty("sequenceNumber").GetInt32());
            nodes.AddVariable<string>("EventTime", device.NodeId).PollEvery(TimeSpan.FromMilliseconds(250), () => Current().GetProperty("eventTime").GetString()!);
            nodes.AddVariable<string>("DeviceStatus", device.NodeId).PollEvery(TimeSpan.FromMilliseconds(250), () => Current().GetProperty("deviceStatus").GetString()!);
            nodes.AddVariable<double>("TemperatureCelsius", device.NodeId).PollEvery(TimeSpan.FromMilliseconds(250), () => Current().GetProperty("measurements").GetProperty("temperatureCelsius").GetDouble());
            nodes.AddVariable<int>("SpeedRpm", device.NodeId).PollEvery(TimeSpan.FromMilliseconds(250), () => Current().GetProperty("measurements").GetProperty("speedRpm").GetInt32());
            nodes.AddVariable<int>("ElapsedSeconds", device.NodeId).PollEvery(TimeSpan.FromMilliseconds(250), () => Current().GetProperty("measurements").GetProperty("elapsedSeconds").GetInt32());
            nodes.AddVariable<string>("FirmwareVersion", device.NodeId).PollEvery(TimeSpan.FromMilliseconds(250), () => Current().GetProperty("extensions").GetProperty("firmwareVersion").GetString()!);
            nodes.AddVariable<string>("RecordSnapshot", device.NodeId)
                .OnFirstSubscriber((_, _, _) => { clock.Start(); return ValueTask.CompletedTask; })
                .PollWhileMonitored(TimeSpan.FromMilliseconds(250), _ => Current().GetRawText());
        });
        using var host = builder.Build();
        await host.StartAsync();
        await host.WaitForShutdownAsync();
    }
}
