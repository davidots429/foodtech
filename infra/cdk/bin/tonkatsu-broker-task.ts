import { CfnOutput, CfnParameter, Fn, Stack, StackProps, Token } from "aws-cdk-lib";
import * as ecs from "aws-cdk-lib/aws-ecs";
import { Construct } from "constructs";

const tlsFields = {
  BROKER_SERVER_CERT: "broker_server_cert_pem",
  BROKER_SERVER_KEY: "broker_server_key_pem",
  BFF_CA: "bff_ca_pem",
  BFF_CERT: "bff_cert_pem",
  GATEWAY_SERVER_CERT: "gateway_server_cert_pem",
  BROKER_CLIENT_CERT: "broker_client_cert_pem",
  BROKER_CLIENT_KEY: "broker_client_key_pem",
} as const;

export class TonkatsuBrokerTaskStack extends Stack {
  constructor(scope: Construct, id: string, props: StackProps, stage: string) {
    super(scope, id, props);
    if (stage !== "dev" || Token.isUnresolved(this.account) ||
        !/^\d{12}$/.test(this.account) || this.region !== "ap-southeast-2") {
      throw new Error("Tonkatsu broker task requires dev and an explicit regional account");
    }

    const parameter = (id: string, allowedPattern: string, description: string) =>
      new CfnParameter(this, id, { type: "String", allowedPattern, description }).valueAsString;
    const taskRole = parameter("BrokerTaskRoleArn",
      `^arn:aws:iam::${this.account}:role/foodtech-tonkatsu-broker-dev$`,
      "Reviewed role for the standalone broker");
    const roleId = parameter("BrokerTaskRoleId", "^[A-Z0-9]{16,128}$",
      "Immutable RoleId read after applying the broker role");
    const executionRole = parameter("BrokerExecutionRoleArn",
      `^arn:aws:iam::${this.account}:role/foodtech-tonkatsu-broker-execution-dev$`,
      "Reviewed image, log and designated TLS secret execution role");
    const image = parameter("BrokerImageUri",
      `^${this.account}\\.dkr\\.ecr\\.${this.region}\\.amazonaws\\.com/foodtech/tonkatsu-broker-dev@sha256:[a-f0-9]{64}$`,
      "Reviewed main image digest from the private broker repository");
    const secret = parameter("BrokerTlsBundleArn",
      `^arn:aws:secretsmanager:${this.region}:${this.account}:secret:foodtech/tonkatsu-broker-dev/tls-[A-Za-z0-9]{6}$`,
      "Complete ARN of the broker TLS bundle");
    const secretVersion = parameter("BrokerTlsBundleVersion", "^[A-Za-z0-9-]{32,64}$",
      "Reviewed immutable Secrets Manager version ID");
    const gatewayUrl = parameter("GatewayPrivateOrigin",
      "^https://[a-z0-9](?:[a-z0-9-]*[a-z0-9])?(?:[.][a-z0-9](?:[a-z0-9-]*[a-z0-9])?)*[.]internal:8445$",
      "Private Gateway hostname covered by the pinned server certificate");
    const tlsDirectory = "/run/foodtech-tls";
    const variables = {
      FOODTECH_SERVICE: "tonkatsu-broker",
      FOODTECH_STAGE: stage,
      FOODTECH_PRINCIPAL_PROVISIONING_ENABLED: "true",
      FOODTECH_TONKATSU_BROKER_ENABLED: "true",
      FOODTECH_TONKATSU_BROKER_BIND_HOST: "0.0.0.0",
      FOODTECH_TONKATSU_BROKER_PORT: "8446",
      FOODTECH_TONKATSU_BROKER_TASK_ROLE_ARN: taskRole,
      FOODTECH_TONKATSU_BROKER_TASK_ROLE_ID: roleId,
      FOODTECH_TONKATSU_GATEWAY_URL: gatewayUrl,
      FOODTECH_AI_REGISTRY_TABLE: "foodtech-ai-execution-registry-dev",
      FOODTECH_AUTH_TABLE: "foodtech-auth-sessions-dev",
      PYTHONDONTWRITEBYTECODE: "1",
      TMPDIR: tlsDirectory,
    };
    const task = new ecs.CfnTaskDefinition(this, "BrokerTask", {
      family: "foodtech-tonkatsu-broker-dev",
      requiresCompatibilities: ["FARGATE"],
      networkMode: "awsvpc",
      runtimePlatform: { operatingSystemFamily: "LINUX", cpuArchitecture: "X86_64" },
      cpu: "256",
      memory: "512",
      taskRoleArn: taskRole,
      executionRoleArn: executionRole,
      volumes: [{ name: "broker-tls" }],
      containerDefinitions: [{
        name: "broker",
        essential: true,
        image,
        user: "10001:10001",
        readonlyRootFilesystem: true,
        privileged: false,
        stopTimeout: 30,
        linuxParameters: { capabilities: { drop: ["ALL"] } },
        mountPoints: [{ sourceVolume: "broker-tls", containerPath: tlsDirectory, readOnly: false }],
        portMappings: [{ containerPort: 8446, protocol: "tcp" }],
        environment: Object.entries(variables).map(([name, value]) => ({ name, value })),
        secrets: Object.entries(tlsFields).map(([suffix, key]) => ({
          name: `FOODTECH_TONKATSU_${suffix}_PEM`,
          valueFrom: Fn.join("", [secret, `:${key}::`, secretVersion]),
        })),
        logConfiguration: { logDriver: "awslogs", options: {
          "awslogs-group": "/foodtech/dev/tonkatsu-broker",
          "awslogs-region": this.region,
          "awslogs-stream-prefix": "broker",
        } },
      }],
    });
    this.templateOptions.description = "Manual Tonkatsu broker task definition with pinned image and TLS version";
    this.templateOptions.metadata = { AWSToolsMetrics: { AWSAgentToolkit: "aws-cloudformation@3" } };
    new CfnOutput(this, "BrokerTaskDefinitionArn", { value: task.ref });
  }
}
