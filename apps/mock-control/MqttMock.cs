using System.Net;
using System.Security.Authentication;
using System.Security.Cryptography;
using System.Security.Cryptography.X509Certificates;
using System.Text;
using System.Text.Json;
using MQTTnet;
using MQTTnet.Formatter;
using MQTTnet.Protocol;
using MQTTnet.Server;

internal static class MqttMock
{
    private const string Publisher = "foodtech-internal-mock";

    public static async Task Run(string path, string certificatePath, int port, string? fault)
    {
        if (fault is not (null or "invalid-utf8" or "invalid-json"))
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
        if (root.GetProperty("provenance").GetString() != "SYNTHETIC" ||
            root.GetProperty("profileId").GetString() != "mockprofile-300" ||
            instance is not ("mockprofile-300-a-001" or "mockprofile-300-b-001" or "mockprofile-300-c-001"))
            throw new InvalidDataException("Only the approved synthetic MQTT profile is supported.");
        var topic = $"foodtech/synthetic/{instance}/observations";
        var b = instance == "mockprofile-300-b-001";
        var c = instance == "mockprofile-300-c-001";
        var records = root.GetProperty("records").EnumerateArray().Select((item, index) => {
            if (item.GetProperty(b ? "source_id" : c ? "SrcId" : "sourceId").GetString() != "synthetic-mqtt" ||
                item.GetProperty(b ? "device_id" : c ? "DeviceId" : "deviceId").GetString() != instance ||
                item.GetProperty(b ? "sequence_number" : c ? "SeqNo" : "sequenceNumber").GetInt32() != index + 1)
                throw new InvalidDataException("Invalid synthetic MQTT record identity or sequence.");
            return item.GetRawText();
        }).ToArray();
        if (records.Length is < 1 or > 5000) throw new InvalidDataException("Invalid fixture length.");
        using var cert = X509CertificateLoader.LoadPkcs12FromFile(certificatePath, certificatePassword);
        var config = new MqttServerOptionsBuilder().WithEncryptedEndpoint().WithEncryptedEndpointPort(port)
            .WithEncryptedEndpointBoundIPAddress(IPAddress.Loopback).WithEncryptedEndpointBoundIPV6Address(IPAddress.IPv6None)
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
