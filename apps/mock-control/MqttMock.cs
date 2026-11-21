using System.Net;
using System.Security.Authentication;
using System.Security.Cryptography;
using System.Security.Cryptography.X509Certificates;
using System.Text;
using System.Text.Json;
using System.Xml;
using System.Xml.Linq;
using Microsoft.VisualBasic.FileIO;
using MQTTnet;
using MQTTnet.Formatter;
using MQTTnet.Protocol;
using MQTTnet.Server;

internal static class MqttMock
{
    private const string Publisher = "foodtech-internal-mock";

    public static async Task Run(string path, string certificatePath, int port, string? fault, IPAddress bindAddress)
    {
        if (fault is not (null or "invalid-utf8" or "invalid-json" or "invalid-xml" or "invalid-csv"))
            throw new ArgumentException("Unsupported MQTT fault scenario.");
        if (port is < 1024 or > 65535) throw new ArgumentOutOfRangeException(nameof(port));
        var user = Environment.GetEnvironmentVariable("FOODTECH_MQTT_USER");
        var password = Environment.GetEnvironmentVariable("FOODTECH_MQTT_PASSWORD");
        var certificatePassword = Environment.GetEnvironmentVariable("FOODTECH_MOCK_CERT_PASSWORD");
        if (string.IsNullOrWhiteSpace(user) || string.IsNullOrWhiteSpace(password) || string.IsNullOrWhiteSpace(certificatePassword))
            throw new InvalidOperationException("MQTT credentials and certificate password are required.");
        using var fixture = JsonDocument.Parse(File.ReadAllText(path, Encoding.UTF8));
        var root = fixture.RootElement;
        var instance = root.GetProperty("instanceId").GetString();
        var profile = root.GetProperty("profileId").GetString();
        if (root.GetProperty("provenance").GetString() != "SYNTHETIC" ||
            profile is not ("mockprofile-300" or "mockprofile-301" or "mockprofile-302" or "mockprofile-310" or "mockprofile-311" or "mockprofile-312" or "mockprofile-320" or "mockprofile-321" or "mockprofile-322") ||
            instance is null || !new[] { $"{profile}-a-001", $"{profile}-b-001", $"{profile}-c-001" }.Contains(instance))
            throw new InvalidDataException("Only the approved synthetic MQTT profile is supported.");
        var topic = $"foodtech/synthetic/{instance}/observations";
        var b = instance.EndsWith("-b-001", StringComparison.Ordinal);
        var c = instance.EndsWith("-c-001", StringComparison.Ordinal);
        var xml = profile is "mockprofile-301" or "mockprofile-311" or "mockprofile-321";
        var csv = profile is "mockprofile-302" or "mockprofile-312" or "mockprofile-322";
        if ((fault == "invalid-json" && (xml || csv)) || (fault == "invalid-xml" && !xml) ||
            (fault == "invalid-csv" && !csv))
            throw new ArgumentException("Fault does not match the source format.");
        var records = root.GetProperty("records").EnumerateArray().Select((item, index) => {
            if (csv) {
                var raw = item.GetString() ?? throw new InvalidDataException("Invalid CSV fixture.");
                using var parser = new TextFieldParser(new StringReader(raw)) { TextFieldType = FieldType.Delimited, HasFieldsEnclosedInQuotes = true };
                parser.SetDelimiters(",");
                var header = parser.ReadFields() ?? throw new InvalidDataException("Missing CSV header.");
                var fields = parser.ReadFields() ?? throw new InvalidDataException("Missing CSV row.");
                if (header.Length != fields.Length || header.Distinct().Count() != header.Length || !parser.EndOfData)
                    throw new InvalidDataException("Invalid CSV structure.");
                var cells = header.Zip(fields).ToDictionary(pair => pair.First, pair => pair.Second);
                if (!cells.TryGetValue(b ? "source_id" : c ? "SrcId" : "sourceId", out var source) || source != "synthetic-mqtt-csv" ||
                    !cells.TryGetValue(b ? "device_id" : c ? "DeviceId" : "deviceId", out var device) || device != instance ||
                    !cells.TryGetValue(b ? "sequence_number" : c ? "SeqNo" : "sequenceNumber", out var seq) || seq != (index + 1).ToString())
                    throw new InvalidDataException("Invalid synthetic MQTT CSV identity or sequence.");
                if (profile is "mockprofile-312" or "mockprofile-322") {
                    var equipmentKey = b ? "ext_equipment_type" : c ? "Extension_EquipmentType" : "extensions.equipmentType";
                    if (!cells.TryGetValue(equipmentKey, out var equipment) ||
                        equipment != (profile == "mockprofile-312" ? "STIR_FRYER" : "COMBI_OVEN"))
                        throw new InvalidDataException("Equipment type does not match the synthetic profile.");
                }
                return raw;
            }
            if (xml) {
                var raw = item.GetString() ?? throw new InvalidDataException("Invalid XML fixture.");
                using var reader = XmlReader.Create(new StringReader(raw), new XmlReaderSettings {
                    DtdProcessing = DtdProcessing.Prohibit, XmlResolver = null, MaxCharactersInDocument = 2_000_000 });
                var element = XDocument.Load(reader).Root ?? throw new InvalidDataException("Missing XML root.");
                if ((string?)element.Element(b ? "source_id" : c ? "SrcId" : "sourceId") != "synthetic-mqtt-xml" ||
                    (string?)element.Element(b ? "device_id" : c ? "DeviceId" : "deviceId") != instance ||
                    (int?)element.Element(b ? "sequence_number" : c ? "SeqNo" : "sequenceNumber") != index + 1)
                    throw new InvalidDataException("Invalid synthetic MQTT XML identity or sequence.");
                if (profile is "mockprofile-311" or "mockprofile-321") {
                    var equipment = b ? (string?)element.Element("ext_equipment_type") : c ?
                        (string?)element.Element("Extension")?.Element("EquipmentType") :
                        (string?)element.Element("extensions")?.Element("equipmentType");
                    if (equipment != (profile == "mockprofile-311" ? "STIR_FRYER" : "COMBI_OVEN"))
                        throw new InvalidDataException("Equipment type does not match the synthetic profile.");
                }
                return raw;
            }
            if (item.GetProperty(b ? "source_id" : c ? "SrcId" : "sourceId").GetString() != "synthetic-mqtt" ||
                item.GetProperty(b ? "device_id" : c ? "DeviceId" : "deviceId").GetString() != instance ||
                item.GetProperty(b ? "sequence_number" : c ? "SeqNo" : "sequenceNumber").GetInt32() != index + 1)
                throw new InvalidDataException("Invalid synthetic MQTT record identity or sequence.");
            if (profile is "mockprofile-310" or "mockprofile-320") {
                var equipment = b ? item.GetProperty("ext_equipment_type").GetString() : c ?
                    item.GetProperty("Extension").GetProperty("EquipmentType").GetString() :
                    item.GetProperty("extensions").GetProperty("equipmentType").GetString();
                if (equipment != (profile == "mockprofile-310" ? "STIR_FRYER" : "COMBI_OVEN"))
                    throw new InvalidDataException("Equipment type does not match the synthetic profile.");
            }
            return item.GetRawText();
        }).ToArray();
        if (records.Length is < 1 or > 5000) throw new InvalidDataException("Invalid fixture length.");
        using var cert = X509CertificateLoader.LoadPkcs12FromFile(certificatePath, certificatePassword);
        var config = new MqttServerOptionsBuilder().WithEncryptedEndpoint().WithEncryptedEndpointPort(port)
            .WithEncryptedEndpointBoundIPAddress(bindAddress).WithEncryptedEndpointBoundIPV6Address(IPAddress.IPv6None)
            .WithEncryptionCertificate(cert).WithEncryptionSslProtocol(SslProtocols.Tls12 | SslProtocols.Tls13).Build();
        config.EnablePersistentSessions = true;
        using var server = new MqttServerFactory().CreateMqttServer(config);
        server.ValidatingConnectionAsync += args => {
            if (args.ProtocolVersion != MqttProtocolVersion.V500)
                args.ReasonCode = MqttConnectReasonCode.UnsupportedProtocolVersion;
            else if (!args.ClientId.StartsWith("foodtech-collector-", StringComparison.Ordinal) ||
                !EqualsFixed(args.UserName, user) || !EqualsFixed(args.Password, password))
                args.ReasonCode = MqttConnectReasonCode.BadUserNameOrPassword;
            return Task.CompletedTask;
        };
        server.InterceptingSubscriptionAsync += args => {
            if (!string.Equals(args.TopicFilter.Topic, topic, StringComparison.Ordinal) ||
                args.TopicFilter.QualityOfServiceLevel != MqttQualityOfServiceLevel.AtLeastOnce)
                args.Response.ReasonCode = MqttSubscribeReasonCode.NotAuthorized;
            return Task.CompletedTask;
        };
        server.InterceptingPublishAsync += args => {
            if (args.ClientId != Publisher) { args.ProcessPublish = false; args.CloseConnection = true; }
            return Task.CompletedTask;
        };
        var started = 0;
        server.ClientSubscribedTopicAsync += args => {
            if (args.TopicFilter.Topic != topic || Interlocked.Exchange(ref started, 1) != 0) return Task.CompletedTask;
            _ = Task.Run(async () => {
                try
                {
                    await Task.Delay(250);
                    for (var index = 0; index < records.Length; index++)
                    {
                        var payload = index == 0 && fault == "invalid-utf8" ? new byte[] { 0xff, 0xfe, 0x80 } :
                            index == 0 && fault == "invalid-json" ? Encoding.UTF8.GetBytes("{\"incomplete\":") :
                            index == 0 && fault == "invalid-xml" ? Encoding.UTF8.GetBytes("<record><broken>") :
                            index == 0 && fault == "invalid-csv" ? Encoding.UTF8.GetBytes(records[index].Insert(records[index].IndexOf('\n') + 1, "=")) :
                            Encoding.UTF8.GetBytes(records[index]);
                        var message = new MqttApplicationMessageBuilder().WithTopic(topic).WithPayload(payload)
                            .WithQualityOfServiceLevel(MqttQualityOfServiceLevel.AtLeastOnce).Build();
                        await server.InjectApplicationMessage(new InjectedMqttApplicationMessage(message) { SenderClientId = Publisher });
                        if (index + 1 < records.Length) await Task.Delay(TimeSpan.FromSeconds(5));
                    }
                }
                catch (Exception error) { Console.Error.WriteLine($"MQTT fixture publishing failed: {error.GetType().Name}"); }
            });
            return Task.CompletedTask;
        };
        await server.StartAsync();
        await Task.Delay(Timeout.InfiniteTimeSpan);
    }

    private static bool EqualsFixed(string? left, string right) => CryptographicOperations.FixedTimeEquals(
        SHA256.HashData(Encoding.UTF8.GetBytes(left ?? "")), SHA256.HashData(Encoding.UTF8.GetBytes(right)));
}
