import { CfnOutput, CfnParameter, Fn, Stack, StackProps, Token } from "aws-cdk-lib";
import * as ecs from "aws-cdk-lib/aws-ecs";
import { Construct } from "constructs";
import { namespaceName } from "./tonkatsu-private-network.js";

const senders = [
  {
    name: "business-api", label: "Business", mode: "tonkatsu-business-api", image: "broker",
    origins: { BROKER_URL: ["broker", 8446], GATEWAY_RECORDS_URL: ["records", 8447] },
    fields: {
      BROKER_SERVER_CERT: "broker_server_cert_pem", BFF_CERT: "bff_cert_pem", BFF_KEY: "bff_key_pem",
      GATEWAY_RECORDS_SERVER_CERT: "records_server_cert_pem", GATEWAY_RECORDS_CLIENT_CERT: "records_client_cert_pem",
      GATEWAY_RECORDS_CLIENT_KEY: "records_client_key_pem",
    },
  },
  {
    name: "broker", label: "Broker", mode: "tonkatsu-broker", image: "broker",
    origins: { GATEWAY_URL: ["gateway", 8445] },
    fields: {
      GATEWAY_SERVER_CERT: "gateway_server_cert_pem", BROKER_CLIENT_CERT: "broker_client_cert_pem",
      BROKER_CLIENT_KEY: "broker_client_key_pem",
    },
  },
  {
    name: "gateway", label: "Gateway", mode: "tonkatsu-gateway", image: "gateway",
    origins: { OLLAMA_PRIVATE_ORIGIN: ["inference", 8448] },
    fields: {
      OLLAMA_SERVER_CERT: "inference_server_cert_pem", OLLAMA_CLIENT_CERT: "inference_client_cert_pem",
      OLLAMA_CLIENT_KEY: "inference_client_key_pem",
    },
  },
] as const;

export class TonkatsuPrivateProbeStack extends Stack {
  constructor(scope: Construct, id: string, props: StackProps, stage: string) {
    super(scope, id, props);
    if (stage !== "dev" || Token.isUnresolved(this.account) ||
        !/^\d{12}$/.test(this.account) || this.region !== "ap-southeast-2") {
      throw new Error("Tonkatsu private probes require dev and an explicit regional account");
    }
    const parameter = (name: string, allowedPattern: string) =>
      new CfnParameter(this, name, { type: "String", allowedPattern }).valueAsString;
    const image = (name: string, repository: string) => parameter(name,
      `^${this.account}\\.dkr\\.ecr\\.${this.region}\\.amazonaws\\.com/foodtech/${repository}@sha256:[a-f0-9]{64}$`);
    const images = {
      broker: image("BrokerImageUri", "tonkatsu-broker-dev"),
      gateway: image("GatewayImageUri", "tonkatsu-gateway-dev"),
    };
    const version = parameter("TlsBundleVersion", "^[A-Za-z0-9-]{32,64}$");
    for (const sender of senders) {
      const { name, label, mode, fields, origins } = sender;
      const executionRole = parameter(`${label}ExecutionRoleArn`,
        `^arn:aws:iam::${this.account}:role/foodtech-tonkatsu-${name}-execution-dev$`);
      const secret = parameter(`${label}TlsBundleArn`,
        `^arn:aws:secretsmanager:${this.region}:${this.account}:secret:foodtech/tonkatsu-${name}-dev/tls-[A-Za-z0-9]{6}$`);
      const directory = "/run/foodtech-tls";
      const task = new ecs.CfnTaskDefinition(this, `${label}Probe`, {
        family: `foodtech-tonkatsu-${name}-transport-probe-dev`,
        requiresCompatibilities: ["FARGATE"], networkMode: "awsvpc", executionRoleArn: executionRole,
        runtimePlatform: { operatingSystemFamily: "LINUX", cpuArchitecture: "X86_64" },
        cpu: "256", memory: "512",
        volumes: [{ name: "tls" }],
        containerDefinitions: [{
          name, image: images[sender.image], essential: true, user: "10001:10001", readonlyRootFilesystem: true,
          privileged: false, linuxParameters: { capabilities: { drop: ["ALL"] } },
          mountPoints: [{ sourceVolume: "tls", containerPath: directory, readOnly: false }],
          environment: [
            { name: "FOODTECH_SERVICE", value: mode },
            { name: "FOODTECH_TONKATSU_TRANSPORT_PROBE", value: "true" },
            { name: "TMPDIR", value: directory },
            { name: "PYTHONDONTWRITEBYTECODE", value: "1" },
            ...Object.entries(origins).map(([key, [host, port]]) => ({
              name: `FOODTECH_TONKATSU_${key}`, value: `https://${host}.${namespaceName}:${port}`,
            })),
          ],
          secrets: Object.entries(fields).map(([suffix, field]) => ({
            name: `FOODTECH_TONKATSU_${suffix}_PEM`,
            valueFrom: Fn.join("", [secret, `:${field}::`, version]),
          })),
          logConfiguration: { logDriver: "awslogs", options: {
            "awslogs-group": `/foodtech/dev/tonkatsu-${name}`, "awslogs-region": this.region, "awslogs-stream-prefix": name,
          } },
        }],
      });
      new CfnOutput(this, `${label}ProbeTaskDefinitionArn`, { value: task.ref });
    }
    this.templateOptions.description = "Stopped definitions for the three private Tonkatsu transport probes";
    this.templateOptions.metadata = { AWSToolsMetrics: { AWSAgentToolkit: "aws-cloudformation@3" } };
  }
}
