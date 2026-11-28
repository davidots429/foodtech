import { CfnOutput, CfnParameter, CfnRule, Fn, Stack, StackProps, Token } from "aws-cdk-lib";
import * as ecs from "aws-cdk-lib/aws-ecs";
import { Construct } from "constructs";
import { tonkatsuListenerHealth } from "./tonkatsu-private-services.js";

const invocationFields = {
  SERVER_CERT: "server_cert_pem",
  SERVER_KEY: "server_key_pem",
  BROKER_CA: "broker_ca_pem",
  BROKER_CERT: "broker_cert_pem",
  OLLAMA_SERVER_CERT: "inference_server_cert_pem",
  OLLAMA_CLIENT_CERT: "inference_client_cert_pem",
  OLLAMA_CLIENT_KEY: "inference_client_key_pem",
};
const recordsFields = {
  BUSINESS_GATEWAY_SERVER_CERT: "records_server_cert_pem",
  BUSINESS_GATEWAY_SERVER_KEY: "records_server_key_pem",
  BUSINESS_GATEWAY_CLIENT_CA: "records_client_ca_pem",
  BUSINESS_GATEWAY_CLIENT_CERT: "records_client_cert_pem",
};

export class TonkatsuGatewayTaskStack extends Stack {
  constructor(scope: Construct, id: string, props: StackProps, stage: string) {
    super(scope, id, props);
    if (stage !== "dev" || Token.isUnresolved(this.account) ||
        !/^\d{12}$/.test(this.account) || this.region !== "ap-southeast-2") {
      throw new Error("Tonkatsu Gateway task requires dev and an explicit regional account");
    }
    const parameter = (name: string, allowedPattern: string) =>
      new CfnParameter(this, name, { type: "String", allowedPattern }).valueAsString;
    const roleArn = (name: string) => `^arn:aws:iam::${this.account}:role/${name}$`;
    const taskRole = parameter("GatewayTaskRoleArn", roleArn("foodtech-tonkatsu-gateway-dev"));
    const executionRole = parameter("GatewayExecutionRoleArn", roleArn("foodtech-tonkatsu-gateway-execution-dev"));
    const sourceRole = parameter("SourceReadRoleArn", roleArn("foodtech-tonkatsu-source-read-dev"));
    const roleId = parameter("GatewayTaskRoleId", "^[A-Z0-9]{16,128}$");
    const sourceRoleId = parameter("SourceReadRoleId", "^[A-Z0-9]{16,128}$");
    new CfnRule(this, "SeparateRoleIds", { assertions: [{
      assert: Fn.conditionNot(Fn.conditionEquals(roleId, sourceRoleId)),
      assertDescription: "Gateway and delegated source role IDs must differ",
    }] });
    const image = parameter("GatewayImageUri",
      `^${this.account}\\.dkr\\.ecr\\.${this.region}\\.amazonaws\\.com/foodtech/tonkatsu-gateway-dev@sha256:[a-f0-9]{64}$`);
    const secret = parameter("GatewayTlsBundleArn",
      `^arn:aws:secretsmanager:${this.region}:${this.account}:secret:foodtech/tonkatsu-gateway-dev/tls-[A-Za-z0-9]{6}$`);
    const version = parameter("GatewayTlsBundleVersion", "^[A-Za-z0-9-]{32,64}$");
    const label = "[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?";
    const inference = parameter("InferencePrivateOrigin", `^https://${label}(?:[.]${label})*[.]internal:8448$`);

    const directory = "/run/foodtech-tls";
    const common = {
      FOODTECH_STAGE: stage,
      FOODTECH_PRINCIPAL_PROVISIONING_ENABLED: "true",
      FOODTECH_TONKATSU_LISTENER_ENABLED: "true",
      FOODTECH_TONKATSU_INVOCATION_ENABLED: "true",
      FOODTECH_TONKATSU_GATEWAY_TASK_ROLE_ARN: taskRole,
      FOODTECH_TONKATSU_GATEWAY_TASK_ROLE_ID: roleId,
      FOODTECH_TONKATSU_SOURCE_READ_ROLE_ARN: sourceRole,
      FOODTECH_TONKATSU_SOURCE_READ_ROLE_ID: sourceRoleId,
      FOODTECH_AI_REGISTRY_TABLE: "foodtech-ai-execution-registry-dev",
      FOODTECH_AUTH_TABLE: "foodtech-auth-sessions-dev",
      FOODTECH_RECIPE_TIER1_MODEL_ID: "llama3.1:8b",
      PYTHONDONTWRITEBYTECODE: "1",
      TMPDIR: directory,
    };
    const container = (name: string, port: number, environment: Record<string, string>,
                       fields: Record<string, string>): ecs.CfnTaskDefinition.ContainerDefinitionProperty => ({
      name, essential: true, image, user: "10001:10001", readonlyRootFilesystem: true,
      privileged: false, stopTimeout: 30, linuxParameters: { capabilities: { drop: ["ALL"] } },
      mountPoints: [{ sourceVolume: `${name}-tls`, containerPath: directory, readOnly: false }],
      portMappings: [{ containerPort: port, protocol: "tcp" }],
      healthCheck: tonkatsuListenerHealth(port),
      environment: Object.entries({ ...common, ...environment }).map(([envName, value]) => ({ name: envName, value })),
      secrets: Object.entries(fields).map(([suffix, field]) => ({
        name: `FOODTECH_TONKATSU_${suffix}_PEM`,
        valueFrom: Fn.join("", [secret, `:${field}::`, version]),
      })),
      logConfiguration: { logDriver: "awslogs", options: {
        "awslogs-group": "/foodtech/dev/tonkatsu-gateway", "awslogs-region": this.region, "awslogs-stream-prefix": name,
      } },
    });
    const task = new ecs.CfnTaskDefinition(this, "GatewayTask", {
      family: "foodtech-tonkatsu-gateway-dev",
      requiresCompatibilities: ["FARGATE"], networkMode: "awsvpc",
      runtimePlatform: { operatingSystemFamily: "LINUX", cpuArchitecture: "X86_64" },
      cpu: "512", memory: "1024", taskRoleArn: taskRole, executionRoleArn: executionRole,
      volumes: [{ name: "gateway-tls" }, { name: "records-tls" }],
      containerDefinitions: [
        container("gateway", 8445, {
          FOODTECH_SERVICE: "tonkatsu-gateway",
          FOODTECH_TONKATSU_BIND_HOST: "0.0.0.0", FOODTECH_TONKATSU_PORT: "8445",
          FOODTECH_TONKATSU_OLLAMA_PRIVATE_ORIGIN: inference,
        }, invocationFields),
        container("records", 8447, {
          FOODTECH_SERVICE: "tonkatsu-business-gateway",
          FOODTECH_TONKATSU_BUSINESS_GATEWAY_ENABLED: "true",
          FOODTECH_TONKATSU_BUSINESS_GATEWAY_BIND_HOST: "0.0.0.0",
          FOODTECH_TONKATSU_BUSINESS_GATEWAY_PORT: "8447",
        }, recordsFields),
      ],
    });
    this.templateOptions.description = "Tonkatsu Gateway task with separate private broker and customer record listeners";
    this.templateOptions.metadata = { AWSToolsMetrics: { AWSAgentToolkit: "aws-cloudformation@3" } };
    new CfnOutput(this, "GatewayTaskDefinitionArn", { value: task.ref });
  }
}
