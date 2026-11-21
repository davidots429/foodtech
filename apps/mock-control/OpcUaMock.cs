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
        var instance = root.GetProperty("instanceId").GetString();
        var profile = root.GetProperty("profileId").GetString();
        if (root.GetProperty("provenance").GetString() != "SYNTHETIC" ||
            profile is not ("mockprofile-203" or "mockprofile-213" or "mockprofile-223") ||
            instance is null || !new[] { $"{profile}-a-001", $"{profile}-b-001", $"{profile}-c-001" }.Contains(instance))
            throw new InvalidDataException("Only the synthetic UA Binary profile is supported.");
        var b = instance == $"{profile}-b-001";
        var c = instance == $"{profile}-c-001";
        string Field(string a, string snake, string pascal) => b ? snake : c ? pascal : a;
        var records = root.GetProperty("records").EnumerateArray().Select((record, index) => {
            if (record.GetProperty(Field("sourceId", "source_id", "SrcId")).GetString() != "synthetic-opcua" ||
                record.GetProperty(Field("deviceId", "device_id", "DeviceId")).GetString() != instance ||
                record.GetProperty(Field("schemaVersion", "schema_version", "SchVer")).GetString() != "1.0" ||
                record.GetProperty(Field("sequenceNumber", "sequence_number", "SeqNo")).GetInt32() != index + 1)
                throw new InvalidDataException("Invalid synthetic OPC UA identity or sequence.");
            foreach (var name in new[] { Field("batchId", "batch_id", "BatchId"), Field("recordId", "record_id", "RecId") })
                if (string.IsNullOrWhiteSpace(record.GetProperty(name).GetString()))
                    throw new InvalidDataException("Missing synthetic OPC UA value.");
            if (profile is "mockprofile-213" or "mockprofile-223") {
                var equipment = b ? record.GetProperty("ext_equipment_type").GetString() : c ?
                    record.GetProperty("Extension").GetProperty("EquipmentType").GetString() :
                    record.GetProperty("extensions").GetProperty("equipmentType").GetString();
                if (equipment != (profile == "mockprofile-213" ? "STIR_FRYER" : "COMBI_OVEN"))
                    throw new InvalidDataException("Equipment type does not match the synthetic profile.");
            }
            if (b) {
                var timestampMs = record.GetProperty("event_time_ms").GetInt64();
                var temperature = record.GetProperty("temperature_f").GetDecimal();
                var output = record.GetProperty("output_percent").GetDecimal();
                var state = record.GetProperty("device_status").GetInt32();
                var elapsed = record.GetProperty("elapsed_ms").GetInt64();
                if (timestampMs is < 0 or > 253402300799999 || temperature is < -1000 or > 1000 ||
                    (temperature - 32) * 50 % 9 != 0 || output is < 0 or > 100 ||
                    state is not (0 or 1) || elapsed < 0)
                    throw new InvalidDataException("Invalid synthetic vendor-b value.");
            } else if (c) {
                var timestamp = record.GetProperty("Timestamp").GetString();
                var data = record.GetProperty("Readings");
                var temperature = data.GetProperty("TempC").GetDecimal();
                var level = data.GetProperty("SpeedLvl").GetInt32();
                var elapsed = data.GetProperty("ElapsedMin").GetDecimal();
                if (timestamp is null || !DateTimeOffset.TryParse(timestamp, out _) ||
                    !System.Text.RegularExpressions.Regex.IsMatch(timestamp, @"(Z|[+-]\d{2}:\d{2})$") ||
                    temperature is < -500 or > 500 || level is < 0 or > 10 || elapsed is < 0 or > 1000000 ||
                    record.GetProperty("Status").GetString() is not ("RUN" or "STP") ||
                    record.GetProperty("Alarm").GetString() is not ("Y" or "N"))
                    throw new InvalidDataException("Invalid synthetic vendor-c value.");
            } else {
                var data = record.GetProperty("measurements");
                _ = data.GetProperty("temperatureCelsius").GetDouble();
                _ = data.GetProperty("speedRpm").GetInt32();
                _ = data.GetProperty("elapsedSeconds").GetInt32();
                if (string.IsNullOrWhiteSpace(record.GetProperty("extensions").GetProperty("firmwareVersion").GetString()))
                    throw new InvalidDataException("Missing synthetic OPC UA firmware version.");
            }
            return record.Clone();
        }).ToArray();
        if (records.Length is < 1 or > 5000) throw new InvalidDataException("Invalid fixture length.");
        if (System.Text.Encoding.UTF8.GetByteCount(JsonSerializer.Serialize(records.Select(record => record.GetRawText()))) > 2_000_000)
            throw new InvalidDataException("Fixture exceeds the synthetic recovery size limit.");
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
            options.MaxStringLength = 2_500_000;
        }).AddNodeManager($"urn:foodtech:synthetic:opcua:{profile![12..]}:{(b ? "b" : c ? "c" : "a")}:v1", nodes => {
            var device = nodes.AddFolder("SyntheticDevice").Node;
            nodes.AddVariable<string>("SchemaVersion", device.NodeId).PollEvery(TimeSpan.FromMilliseconds(250), () => Current().GetProperty(Field("schemaVersion", "schema_version", "SchVer")).GetString()!);
            nodes.AddVariable<string>("SourceId", device.NodeId).PollEvery(TimeSpan.FromMilliseconds(250), () => Current().GetProperty(Field("sourceId", "source_id", "SrcId")).GetString()!);
            nodes.AddVariable<string>("DeviceId", device.NodeId).PollEvery(TimeSpan.FromMilliseconds(250), () => Current().GetProperty(Field("deviceId", "device_id", "DeviceId")).GetString()!);
            nodes.AddVariable<string>("BatchId", device.NodeId).PollEvery(TimeSpan.FromMilliseconds(250), () => Current().GetProperty(Field("batchId", "batch_id", "BatchId")).GetString()!);
            nodes.AddVariable<string>("RecordId", device.NodeId).PollEvery(TimeSpan.FromMilliseconds(250), () => Current().GetProperty(Field("recordId", "record_id", "RecId")).GetString()!);
            nodes.AddVariable<int>("Sequence", device.NodeId).PollEvery(TimeSpan.FromMilliseconds(250), () => Current().GetProperty(Field("sequenceNumber", "sequence_number", "SeqNo")).GetInt32());
            nodes.AddVariable<string>("EventTime", device.NodeId).PollEvery(TimeSpan.FromMilliseconds(250), () => b ?
                DateTimeOffset.FromUnixTimeMilliseconds(Current().GetProperty("event_time_ms").GetInt64()).ToString("O") :
                Current().GetProperty(c ? "Timestamp" : "eventTime").GetString()!);
            nodes.AddVariable<string>("DeviceStatus", device.NodeId).PollEvery(TimeSpan.FromMilliseconds(250), () => b ?
                (Current().GetProperty("device_status").GetInt32() == 1 ? "running" : "stopped") : c ?
                (Current().GetProperty("Status").GetString() == "RUN" ? "running" : "stopped") : Current().GetProperty("deviceStatus").GetString()!);
            nodes.AddVariable<double>("TemperatureCelsius", device.NodeId).PollEvery(TimeSpan.FromMilliseconds(250), () => b ?
                (Current().GetProperty("temperature_f").GetDouble() - 32) * 5 / 9 : c ?
                Current().GetProperty("Readings").GetProperty("TempC").GetDouble() :
                Current().GetProperty("measurements").GetProperty("temperatureCelsius").GetDouble());
            if (b) {
                nodes.AddVariable<double>("OutputPercent", device.NodeId).PollEvery(TimeSpan.FromMilliseconds(250), () => Current().GetProperty("output_percent").GetDouble());
                nodes.AddVariable<double>("ElapsedSeconds", device.NodeId).PollEvery(TimeSpan.FromMilliseconds(250), () => Current().GetProperty("elapsed_ms").GetDouble() / 1000);
            } else if (c) {
                nodes.AddVariable<int>("SpeedLevel", device.NodeId).PollEvery(TimeSpan.FromMilliseconds(250), () => Current().GetProperty("Readings").GetProperty("SpeedLvl").GetInt32());
                nodes.AddVariable<double>("ElapsedSeconds", device.NodeId).PollEvery(TimeSpan.FromMilliseconds(250), () => Current().GetProperty("Readings").GetProperty("ElapsedMin").GetDouble() * 60);
            } else {
                nodes.AddVariable<int>("SpeedRpm", device.NodeId).PollEvery(TimeSpan.FromMilliseconds(250), () => Current().GetProperty("measurements").GetProperty("speedRpm").GetInt32());
                nodes.AddVariable<int>("ElapsedSeconds", device.NodeId).PollEvery(TimeSpan.FromMilliseconds(250), () => Current().GetProperty("measurements").GetProperty("elapsedSeconds").GetInt32());
                nodes.AddVariable<string>("FirmwareVersion", device.NodeId).PollEvery(TimeSpan.FromMilliseconds(250), () => Current().GetProperty("extensions").GetProperty("firmwareVersion").GetString()!);
            }
            nodes.AddVariable<string>("RecordSnapshot", device.NodeId)
                .OnFirstSubscriber((_, _, _) => { clock.Start(); return ValueTask.CompletedTask; })
                .PollWhileMonitored(TimeSpan.FromMilliseconds(250), _ => Current().GetRawText());
            nodes.AddVariable<string>("AvailableSnapshots", device.NodeId)
                .PollEvery(TimeSpan.FromSeconds(1), () => JsonSerializer.Serialize(
                    records.Take(Math.Min((int)(clock.ElapsedMilliseconds / 5000) + 1, records.Length))
                        .Select(record => record.GetRawText())));
        });
        using var host = builder.Build();
        await host.StartAsync();
        await host.WaitForShutdownAsync();
    }
}
